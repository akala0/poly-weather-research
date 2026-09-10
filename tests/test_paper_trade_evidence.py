"""Trade evidence regressions: all ledgers are isolated in pytest temp paths."""

import json
from dataclasses import asdict, replace
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from paper_model_support import model_trade
from test_market_trade_tape import _row
from test_paper_seal_blockers import (
    STRATEGY_PATH,
    _runtime_base,
    _seed_cycle_inputs,
    _write_public_tape,
    _write_ws_trade,
    fill_trade,
    processor,
    restarted,
    snapshot,
    submit_first,
)

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.market_trade_tape import (
    build_shadow_trade_events,
    match_ws_trade,
    parse_market_ws_trade,
)
from poly_weather.paper_account import PaperLedger
from poly_weather.paper_spread_runtime import run_paper_spread_continuous
from poly_weather.shadow_orders import TradeEvent, canonical_trade_event_key
from poly_weather.shadow_runtime import ShadowCursor
from poly_weather.trade_evidence import parse_trade_timestamp


@pytest.mark.parametrize("change", [
    {"price": Decimal("0.750"), "size": Decimal("101.0")},
    {"source": "market_ws", "sequence": 1},
])
def test_restarted_economic_trade_is_consumed_once(tmp_path, change):
    paper = processor(tmp_path)
    submit_first(paper)
    # A sequenced unit fixture isolates duplicate economics. Unsequenced
    # singleton admission is separately forbidden by the F02 regressions.
    trade = replace(fill_trade(), source="data_api", sequence=1)
    assert sum(fill.shares for fill in model_trade(paper, trade)) == Decimal("1")
    recovered = restarted(tmp_path / "paper.jsonl")
    assert model_trade(recovered, replace(trade, **change)) == ()


def test_ws_quantity_conflict_quarantines_api_fallback():
    ws = replace(parse_market_ws_trade(_row()), size=Decimal("1000"))
    api = PublicTrade(
        proxy_wallet="wallet", asset_id=ws.asset_id, condition_id=ws.market_id,
        event_slug="event", market_slug="market", outcome="No", side="SELL",
        size=Decimal("1"), price=ws.price, timestamp=ws.source_timestamp,
        transaction_hash=ws.transaction_hash, available_at=ws.received_at,
    )
    events, validation = build_shadow_trade_events([ws], [api])
    assert events == ()
    assert validation["queue_use_allowed"] is False


def public_match(ws):
    return PublicTrade(
        proxy_wallet="wallet", asset_id=ws.asset_id, condition_id=ws.market_id,
        event_slug="event", market_slug="market", outcome="No", side=ws.side.value,
        size=ws.size, price=ws.price, timestamp=ws.source_timestamp,
        transaction_hash=ws.transaction_hash, available_at=ws.received_at,
    )


@pytest.mark.parametrize("first_source,second_source", [
    ("data_api", "market_ws"), ("market_ws", "data_api"),
])
@pytest.mark.parametrize("size", ["50", "101", "1000"])
def test_bidirectional_alias_repeated_restart_preserves_account(tmp_path, first_source, second_source, size):
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = replace(fill_trade(size=size), source=first_source,
                    sequence=1)
    model_trade(paper, trade)
    expected_order = order.as_dict()
    expected_cost = paper.account.inventory_cost_usd
    expected_economics = economic_state(paper)
    alias = replace(trade, source=second_source,
                    sequence=1 if second_source == "market_ws" else None,
                    price=Decimal("0.7500"), size=Decimal(size + ".00"))
    for _ in range(3):
        paper = restarted(tmp_path / "paper.jsonl")
        assert model_trade(paper, alias) == ()
        assert next(iter(paper.ledger.orders.values())).as_dict() == expected_order
        assert paper.account.inventory_cost_usd == expected_cost
        assert economic_state(paper) == expected_economics
        assert not paper.is_halted


@pytest.mark.parametrize("changes,reason", [
    ({"size": Decimal("1")}, "QUANTITY"),
    ({"price": Decimal("0.71")}, "PRICE"),
    ({"side": "BUY"}, "SIDE"),
    ({"asset_id": "other"}, "TOKEN"),
    ({"condition_id": "other"}, "MARKET"),
    ({"available_at": None}, "RECEIPT"),
])
def test_each_conflicting_field_is_quarantined(changes, reason):
    ws = parse_market_ws_trade(_row())
    api = replace(public_match(ws), **changes)
    assert reason in match_ws_trade(ws, [api])["reason"]
    assert build_shadow_trade_events([ws], [api])[0] == ()


def test_timestamp_precision_and_validation_dependency_receipt():
    ws = parse_market_ws_trade(_row())
    api = replace(public_match(ws), available_at=ws.received_at + timedelta(minutes=5))
    events, _ = build_shadow_trade_events([ws], [api])
    assert events[0].available_at == api.available_at
    equivalent = replace(api, timestamp=api.timestamp.astimezone(timezone(timedelta(hours=8))))
    assert match_ws_trade(ws, [equivalent])["allowed"]
    imprecise = replace(api, timestamp=api.timestamp + timedelta(milliseconds=1))
    assert not match_ws_trade(ws, [imprecise])["allowed"]


def test_mixed_batch_is_per_record_and_ambiguous_siblings_not_overwritten():
    first = parse_market_ws_trade(_row())
    second = replace(first, transaction_hash="tx-2")
    api_first = public_match(first)
    api_second = replace(public_match(second), size=Decimal("1"))
    events, validation = build_shadow_trade_events([first, second], [api_first, api_second])
    assert [row.event_id for row in events] == ["tx-1"]
    assert [row["allowed"] for row in validation["matches"]] == [True, False]
    assert build_shadow_trade_events([first], [api_first, api_first])[0] == ()
    assert build_shadow_trade_events([first, replace(first, sequence=99)], [api_first])[0] == ()


def test_sequence_conflict_is_durable_unknown(tmp_path):
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = fill_trade(size="50")
    model_trade(paper, trade)
    before = order.as_dict()
    paper = restarted(tmp_path / "paper.jsonl")
    assert model_trade(paper, replace(trade, sequence=2)) == ()
    assert paper.trade_evidence_counts["UNKNOWN_TRADE_IDENTITY_CONFLICT"] == 1
    again = restarted(tmp_path / "paper.jsonl")
    assert model_trade(again, trade) == ()
    assert next(iter(again.ledger.orders.values())).as_dict() == before


def test_indistinguishable_same_source_batch_never_consumes(tmp_path):
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = fill_trade()
    assert paper._process_ordered_model_trades([trade, trade]) == ()
    assert order.volume_ahead == Decimal("100")
    again = restarted(tmp_path / "paper.jsonl")
    assert model_trade(again, trade) == ()


@pytest.mark.parametrize("field", ["price", "size"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_trade_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(fill_trade(), **{field: Decimal(value)})


def test_pending_restart_uses_complete_validator(tmp_path):
    paper = processor(tmp_path)
    ws = parse_market_ws_trade(_row())
    paper._record_pending_ws_trade(ws)
    again = restarted(tmp_path / "paper.jsonl")
    bad = replace(public_match(ws), size=Decimal("1"))
    again._resolve_pending_ws_trades_from_public_tape([bad])
    assert again.pending_ws_trade_evidence
    again._resolve_pending_ws_trades_from_public_tape([public_match(ws), public_match(ws)])
    assert again.pending_ws_trade_evidence
    again._resolve_pending_ws_trades_from_public_tape([public_match(ws)])
    assert not again.pending_ws_trade_evidence


def test_delayed_evidence_does_not_fill_expired_order(tmp_path):
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = replace(fill_trade(), available_at=order.submitted_at + paper.strategy.order_timeout)
    assert model_trade(paper, trade) == ()
    assert not order.is_active
    assert order.filled_shares == 0
    again = restarted(tmp_path / "paper.jsonl")
    assert model_trade(again, fill_trade()) == ()


@pytest.mark.parametrize("first", ["data_api", "market_ws", "conflict"])
def test_real_follower_two_polls_then_restart(tmp_path, first):
    root = tmp_path / "isolated"
    book_path, cursor_path = _seed_cycle_inputs(root)
    # Real checkpoint conversion runs for another token; its missing weather
    # must not manufacture a second portfolio for the pre-existing test order.
    checkpoint_rows = [json.loads(line) for line in book_path.read_text(encoding="utf-8").splitlines()]
    for row in checkpoint_rows:
        row["asset_id"] = "checkpoint-" + row["asset_id"]
    book_path.write_text("".join(json.dumps(row) + "\n" for row in checkpoint_rows), encoding="utf-8")
    at = _runtime_base()
    ledger_path = root / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    paper.process_snapshot(snapshot(at=at))
    trade_at = at + timedelta(seconds=1)
    websocket = _write_ws_trade(root, transaction_hash="alias", at=trade_at)
    wire = websocket.read_text(encoding="utf-8")
    if first == "data_api":
        websocket.write_text("", encoding="utf-8")
        api_path = _write_public_tape(root, transaction_hash="alias", at=trade_at)
        payload = json.loads(api_path.read_text(encoding="utf-8"))
        payload["trades"][0].pop("sequence")
        api_path.write_text(json.dumps(payload), encoding="utf-8")
    cursor = ShadowCursor.load(cursor_path)
    cursor.position(websocket)
    cursor.save()
    completed_polls = 0

    def between_polls(stage):
        nonlocal completed_polls
        if stage != "lifecycle_processing":
            return
        completed_polls += 1
        if completed_polls != 1:
            return
        if first == "data_api":
            websocket.write_text(wire, encoding="utf-8")
        else:
            api_path = _write_public_tape(root, transaction_hash="alias", at=trade_at)
            payload = json.loads(api_path.read_text(encoding="utf-8"))
            payload["trades"][0].pop("sequence")
            if first == "conflict":
                payload["trades"][0]["size"] = "1"
            api_path.write_text(json.dumps(payload), encoding="utf-8")

    kwargs = dict(data_dir=root, ledger_path=ledger_path,
                  status_path=root / "status.json", cursor_path=cursor_path,
                  checkpoint_path=root / "checkpoint.json", strategy_config_path=STRATEGY_PATH,
                  bootstrap_at_tail=False, max_cycles=2, poll_seconds=0.01)
    run_paper_spread_continuous(**kwargs, _fault_injector=between_polls)
    assert completed_polls == 2
    recovered = restarted(ledger_path)
    expected = next(iter(recovered.ledger.orders.values())).as_dict()
    assert Decimal(expected["filled_shares"]) == 0  # matched evidence is not group closure
    assert bool(recovered.pending_ws_trade_evidence) == (first == "conflict")
    run_paper_spread_continuous(**kwargs)
    assert next(iter(restarted(ledger_path).ledger.orders.values())).as_dict() == expected


def economic_state(paper):
    return {
        "account": paper.account.as_dict(),
        "orders": [(row.state, row.volume_ahead, row.better_level_shares,
                    row.filled_shares, row.remaining_shares,
                    [(fill.shares, fill.price, fill.fee_usd) for fill in row.fills])
                   for row in paper.ledger.orders.values()],
        "station_cost": paper.station_day_buy_cost,
        "tranche_exit": {key: asdict(value) for key, value in paper.states.items()},
        "pending": paper.pending_ws_trade_evidence,
        "consumed": sorted(key for key in paper.ledger._seen_events if key.startswith("paper-trade-v2:")),
    }


@pytest.mark.parametrize("kind", ["observation", "order", "account_intent", "account_commit"])
@pytest.mark.parametrize("after", [False, True])
def test_crash_before_after_durable_identity_boundaries(tmp_path, monkeypatch, kind, after):
    class Crash(BaseException):
        pass

    paper = processor(tmp_path)
    submit_first(paper)
    trade = fill_trade()
    original_append = paper.ledger._append

    def crash_append(row):
        matches = (
            kind == "observation" and row.get("decision") == "trade_identity_observation_v2"
            or kind == "order" and str(row.get("event_key", "")).startswith("paper-trade-v2:")
            or kind == "account_intent" and row.get("record_type") == "transition_intent"
            or kind == "account_commit" and row.get("record_type") == "account_event"
        )
        if matches and not after:
            raise Crash()
        original_append(row)
        if matches and after:
            raise Crash()

    monkeypatch.setattr(paper.ledger, "_append", crash_append)
    with pytest.raises(Crash):
        model_trade(paper, trade)
    recovered = restarted(tmp_path / "paper.jsonl")
    print(f"crash boundary={kind} after={after} halted={recovered.is_halted}")
    if recovered.is_halted:
        before = economic_state(recovered)
        assert model_trade(recovered, trade) == ()
        assert economic_state(recovered) == before
    else:
        model_trade(recovered, trade)
        baseline = processor(tmp_path / "baseline")
        submit_first(baseline)
        model_trade(baseline, trade)
        assert economic_state(recovered) == economic_state(baseline)
        assert model_trade(restarted(tmp_path / "paper.jsonl"), trade) == ()


def test_explicit_normalized_row_ids_preserve_transaction_siblings(tmp_path):
    paper = processor(tmp_path)
    order = submit_first(paper)
    first = replace(fill_trade(size="100"), event_id="actual-row-1", transaction_hash="same-tx")
    second = replace(first, event_id="actual-row-2", size=Decimal("101"), sequence=2)
    paper._process_ordered_model_trades([first, second])
    assert order.filled_shares > 0
    assert len(order.metadata["paper_trade_consumptions_v2"]) == 2
    again = restarted(tmp_path / "paper.jsonl")
    before = economic_state(again)
    assert again._process_ordered_model_trades([first, second]) == ()
    assert economic_state(again) == before


def test_legacy_key_halts_paper_and_v2_key_is_unchanged(tmp_path):
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = fill_trade()
    legacy = canonical_trade_event_key(trade)
    assert legacy == "trade:trade-fill:token:2026-09-02T12:01:00+00:00:0.75:101:1"
    paper.ledger.save(order, event_key=legacy)
    again = restarted(tmp_path / "paper.jsonl")
    assert again.is_halted
    assert model_trade(again, trade) == ()
    assert canonical_trade_event_key(TradeEvent.from_mapping({
        "timestamp": trade.timestamp.isoformat(), "asset_id": "token", "side": "SELL",
        "price": "0.75", "size": "101", "id": "trade-fill", "sequence": 1,
    })) == legacy


@pytest.mark.parametrize("source_order", [("data_api", "market_ws"), ("market_ws", "data_api")])
def test_same_batch_cross_source_alias_and_scientific_decimal(tmp_path, source_order):
    paper = processor(tmp_path)
    order = submit_first(paper)
    rows = [replace(fill_trade(), source=source, sequence=1 if source == "market_ws" else None,
                    price=Decimal("7.50e-1"), size=Decimal("1.010e2")) for source in source_order]
    assert len(paper._process_ordered_model_trades(rows)) == 1
    assert order.filled_shares == 1
    summary = paper.status()["trade_evidence_summary"]
    assert summary["consumed_economic_event_count"] == 1
    assert Decimal(summary["queue_shares_consumed"]) == 100
    assert Decimal(summary["fill_shares_consumed"]) == 1


@pytest.mark.parametrize("value", ["1787820000000.0001", "2026-08-27T08:40:00.0000001Z",
                                    "2026-08-27T08:40:00", "NaN", "Infinity"])
def test_precision_loss_and_naive_time_are_not_silently_repaired(value):
    assert parse_trade_timestamp(value) is None


def test_failed_cycle_with_alias_partial_prefix_matches_clean_economics(tmp_path):
    at = _runtime_base()

    def run_case(root, *, fail):
        root.mkdir()
        ledger_path = root / "paper.jsonl"
        paper = processor(root, ledger_path=ledger_path)
        paper.process_snapshot(snapshot(at=at))
        api_path = _write_public_tape(root, transaction_hash="alias", at=at + timedelta(seconds=1))
        payload = json.loads(api_path.read_text(encoding="utf-8"))
        payload["trades"][0].pop("sequence")
        api_path.write_text(json.dumps(payload), encoding="utf-8")
        websocket = _write_ws_trade(root, transaction_hash="alias", at=at + timedelta(seconds=1))
        wire = websocket.read_text(encoding="utf-8")
        websocket.write_text("", encoding="utf-8")
        cursor_path = root / "cursor.json"
        cursor = ShadowCursor(cursor_path)
        cursor.position(websocket)
        cursor.save()
        injected = False

        def inject(stage):
            nonlocal injected
            if stage == "trade_processing" and not injected:
                injected = True
                websocket.write_text(wire, encoding="utf-8")
                if fail:
                    raise OSError("after committed API fill before cursor, WS alias arrives")

        kwargs = dict(data_dir=root, ledger_path=ledger_path, status_path=root / "status.json",
                      cursor_path=cursor_path, checkpoint_path=root / "checkpoint.json",
                      strategy_config_path=STRATEGY_PATH, max_cycles=2, poll_seconds=0.01,
                      bootstrap_at_tail=False)
        run_paper_spread_continuous(**kwargs, _fault_injector=inject)
        run_paper_spread_continuous(**kwargs)
        return economic_state(restarted(ledger_path))

    assert run_case(tmp_path / "failed", fail=True) == run_case(tmp_path / "clean", fail=False)


def test_pending_multiple_ws_and_future_receipt_cannot_resolve(tmp_path):
    paper = processor(tmp_path)
    ws = parse_market_ws_trade(_row())
    api = public_match(ws)
    paper._record_pending_ws_trade(ws)
    paper._resolve_pending_ws_trades_from_public_tape([api], as_of=api.available_at - timedelta(seconds=1))
    assert paper.pending_ws_trade_evidence
    wrong_time = replace(api, timestamp=api.timestamp + timedelta(milliseconds=1))
    paper._resolve_pending_ws_trades_from_public_tape([wrong_time])
    assert paper.pending_ws_trade_evidence
    paper._record_pending_ws_trade(replace(ws, sequence=2))
    recovered = restarted(tmp_path / "paper.jsonl")
    recovered._resolve_pending_ws_trades_from_public_tape([api])
    assert len(recovered.pending_ws_trade_evidence) == 2


def test_identity_write_oserror_does_not_commit_follower_cursor(tmp_path):
    class FailIdentityLedger(PaperLedger):
        def _append(self, row):
            if row.get("decision") == "trade_identity_observation_v2":
                raise OSError("identity observation write failed")
            super()._append(row)

    root = tmp_path / "isolated"
    root.mkdir()
    ledger_path = root / "paper.jsonl"
    paper = processor(root, ledger_path=ledger_path)
    at = _runtime_base()
    paper.process_snapshot(snapshot(at=at))
    _write_public_tape(root, at=at + timedelta(seconds=1))
    cursor_path = root / "cursor.json"
    ShadowCursor(cursor_path).save()
    cursor_before = cursor_path.read_bytes()
    status = run_paper_spread_continuous(
        data_dir=root, ledger_path=ledger_path, cursor_path=cursor_path,
        status_path=root / "status.json", checkpoint_path=root / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH, bootstrap_at_tail=False,
        max_cycles=1, poll_seconds=0.01, _ledger_factory=FailIdentityLedger,
    )
    assert status["state"] == "halted"
    assert status["cursor_commit_state"] == "not_committed_cycle_failure"
    assert cursor_path.read_bytes() == cursor_before
    recovered = restarted(ledger_path)
    before = economic_state(recovered)
    assert next(iter(recovered.ledger.orders.values())).filled_shares == 0
    assert model_trade(recovered, fill_trade()) == ()
    assert economic_state(recovered) == before


@pytest.mark.parametrize("after", [False, True])
def test_alias_observation_crash_after_prior_fill(tmp_path, monkeypatch, after):
    class Crash(BaseException):
        pass

    paper = processor(tmp_path)
    submit_first(paper)
    trade = replace(fill_trade(), source="data_api", sequence=1)
    model_trade(paper, trade)
    before = economic_state(paper)
    alias = replace(trade, source="market_ws", sequence=1)
    append = paper.ledger._append

    def interrupt_alias(row):
        selected = row.get("decision") == "trade_identity_observation_v2"
        if selected and not after:
            raise Crash()
        append(row)
        if selected and after:
            raise Crash()

    monkeypatch.setattr(paper.ledger, "_append", interrupt_alias)
    with pytest.raises(Crash):
        model_trade(paper, alias)
    recovered = restarted(tmp_path / "paper.jsonl")
    assert model_trade(recovered, alias) == ()
    assert economic_state(recovered) == before


def test_plain_shadow_cursor_does_not_gain_paper_state(tmp_path):
    path = tmp_path / "v2-cursor.json"
    cursor = ShadowCursor(path)
    cursor.save()
    assert "paper_state" not in json.loads(path.read_text(encoding="utf-8"))
    recovered = ShadowCursor.load(path)
    recovered.save()
    assert "paper_state" not in json.loads(path.read_text(encoding="utf-8"))
