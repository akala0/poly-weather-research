import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.paper_account import PaperAccount, PaperLedger, record_account_action
from poly_weather.paper_spread_runtime import (
    PaperSpreadProcessor,
    PaperStrategyConfig,
    _load_paper_supervisor_cursor,
    _save_paper_supervisor_cursor,
    run_paper_spread_continuous,
)
from poly_weather.polymarket_status import UpstreamQualityWindow
from poly_weather.runtime_safety import atomic_json_write
from poly_weather.shadow_orders import BookSnapshot, ShadowSide, TradeEvent

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)


def strategy() -> PaperStrategyConfig:
    return PaperStrategyConfig.load("configs/paper_spread_strategy_v1.json")


def snapshot(
    *,
    at: datetime = BASE,
    bid: str = "0.75",
    ask: str = "0.80",
    event_id: str = "event",
    market_id: str = "market",
    token_id: str = "token",
    metadata: dict[str, object] | None = None,
) -> BookSnapshot:
    source_at = at - timedelta(minutes=1)
    received_at = at - timedelta(seconds=30)
    return BookSnapshot(
        timestamp=at,
        event_id=event_id,
        market_id=market_id,
        token_id=token_id,
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=((bid, "100"),),
        asks=((ask, "100"),),
        metadata={
            "weather_join_status": "aligned",
            "weather_observation_id": f"observation-{source_at.isoformat()}",
            "weather_source_timestamp": source_at.isoformat(),
            "weather_received_at": received_at.isoformat(),
            "weather_observation_new": True,
            "weather_market_lag": True,
            "weather_improving": True,
            "weather_worsening": False,
            "weather_unchanged": False,
            **(metadata or {}),
        },
    )


def make_processor(tmp_path) -> PaperSpreadProcessor:
    paper = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=strategy()
    )
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    return paper


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replace_rows(path, payload) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in payload),
        encoding="utf-8",
    )


def submit_first(paper: PaperSpreadProcessor):
    order = paper.process_snapshot(snapshot())
    assert order is not None
    return order


def fill_first(paper: PaperSpreadProcessor):
    order = submit_first(paper)
    fills = paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=1),
            "token",
            ShadowSide.SELL,
            "0.75",
            "1000",
            "first-fill",
        )
    )
    assert fills
    return order, fills[0]


def remove_account_transition(path, transition_id: str) -> None:
    replace_rows(
        path,
        [
            row
            for row in rows(path)
            if not (
                row["record_type"] == "account_event"
                and row.get("transition_id") == transition_id
            )
        ],
    )


def restarted(tmp_path) -> PaperSpreadProcessor:
    return PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=strategy()
    )


def test_recovery_repairs_order_persisted_before_reserve_account_event(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    remove_account_transition(paper.ledger.path, f"submit:{order.order_id}")

    restored = restarted(tmp_path)

    assert restored.is_halted is False
    assert restored.account.buy_reserved_usd == order.requested_shares * order.limit_price
    assert restored.recovery["recovery_repairs"] == [f"submit:{order.order_id}"]


def test_recovery_repairs_fill_persisted_before_account_fill_event(tmp_path) -> None:
    paper = make_processor(tmp_path)
    _order, fill = fill_first(paper)
    remove_account_transition(paper.ledger.path, f"fill:{fill.fill_id}")

    restored = restarted(tmp_path)

    assert restored.is_halted is False
    assert restored.account.inventory_cost_usd == Decimal("20")
    assert restored.recovery["recovery_repairs"] == [f"fill:{fill.fill_id}"]


def test_recovery_repairs_expire_before_residual_release_event(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=15))
    transition_id = f"expire:{order.order_id}:{order.expired_at.isoformat()}"
    remove_account_transition(paper.ledger.path, transition_id)

    restored = restarted(tmp_path)

    assert restored.is_halted is False
    assert restored.account.buy_reserved_usd == Decimal("0")
    assert transition_id in restored.recovery["recovery_repairs"]


def test_recovery_repairs_risk_sell_persisted_before_account_event(tmp_path) -> None:
    paper = make_processor(tmp_path)
    _order, _fill = fill_first(paper)
    paper.close_portfolio(
        snapshot(at=BASE + timedelta(minutes=2), bid="0.75", ask="0.80"),
        reason="fixture-risk-exit",
    )
    sell_fill = next(
        fill
        for order in paper.ledger.orders.values()
        if order.side is ShadowSide.SELL
        for fill in order.fills
    )
    remove_account_transition(paper.ledger.path, f"fill:{sell_fill.fill_id}")

    restored = restarted(tmp_path)

    assert restored.is_halted is False
    assert restored.account.positions[next(iter(restored.account.positions))].shares == Decimal("0")
    assert f"fill:{sell_fill.fill_id}" in restored.recovery["recovery_repairs"]


def test_unfinished_intent_without_order_fact_is_durable_halt(tmp_path) -> None:
    path = tmp_path / "paper.jsonl"
    ledger = PaperLedger(path)
    account = PaperAccount()
    ledger.begin_transition(
        transition_id="submit:missing-order",
        event="reserve_buy",
        details={
            "portfolio_key": {
                "event_id": "event",
                "market_id": "market",
                "token_id": "token",
                "market_day": "2026-09-02",
            },
            "order_id": "missing-order",
            "notional_usd": "20",
        },
        before_account=account.as_dict(),
    )

    restored = PaperSpreadProcessor(ledger=PaperLedger(path), strategy=strategy())
    again = PaperLedger(path)

    assert restored.is_halted is True
    assert any(
        row.get("code") == "unfinished_transition_without_durable_fact"
        for row in again.discrepancies
    )


def test_duplicate_transition_replay_is_exactly_once(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl")
    account = PaperAccount()
    key = {
        "event_id": "event",
        "market_id": "market",
        "token_id": "token",
        "market_day": "2026-09-02",
    }
    details = {"portfolio_key": key, "notional_usd": "20"}
    record_account_action(
        ledger, account, event_key="reserve:one", event="reserve_buy", details=details
    )
    record_account_action(
        ledger, account, event_key="reserve:one", event="reserve_buy", details=details
    )

    assert account.buy_reserved_usd == Decimal("20")
    assert len(PaperLedger(ledger.path).account_events) == 1


def test_truncated_tail_and_middle_corruption_are_distinct_and_halted(tmp_path) -> None:
    tail = tmp_path / "tail.jsonl"
    tail.write_bytes(b'{"schema_version": 2, "record_type": "decision"}\n{"schema_version"')
    corrupt_tail = PaperLedger(tail)
    middle = tmp_path / "middle.jsonl"
    middle.write_bytes(
        b'{"schema_version": 2, "record_type": "decision", "execution_enabled": false, "details": {}}\n'
        b'not-json\n'
        b'{"schema_version": 2, "record_type": "decision", "execution_enabled": false, "details": {}}\n'
    )
    corrupt_middle = PaperLedger(middle)

    assert corrupt_tail.ledger_state == "recoverable_incomplete_tail"
    assert corrupt_middle.ledger_state == "corrupt"
    assert PaperSpreadProcessor(ledger=corrupt_tail, strategy=strategy()).is_halted is True
    assert PaperSpreadProcessor(ledger=corrupt_middle, strategy=strategy()).is_halted is True


def test_missing_confirmed_empty_and_corrupt_ledgers_are_not_equivalent(tmp_path) -> None:
    missing = PaperLedger(tmp_path / "missing.jsonl")
    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("", encoding="utf-8")
    empty = PaperLedger(empty_path)
    corrupt_path = tmp_path / "corrupt.jsonl"
    corrupt_path.write_text("not-json\n", encoding="utf-8")
    corrupt = PaperLedger(corrupt_path)

    assert missing.ledger_state == "missing"
    assert empty.ledger_state == "confirmed_empty"
    assert corrupt.ledger_state == "corrupt"


def test_order_account_reservation_and_position_conflicts_halt_durably(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order, fill = fill_first(paper)
    payload = rows(paper.ledger.path)
    for row in payload:
        if row["record_type"] == "account_event" and row.get("transition_id") == f"submit:{order.order_id}":
            row["details"]["notional_usd"] = "19"
        if row["record_type"] == "account_event" and row.get("transition_id") == f"fill:{fill.fill_id}":
            row["details"]["shares"] = "1"
    replace_rows(paper.ledger.path, payload)

    restored = restarted(tmp_path)
    durable = PaperLedger(paper.ledger.path)

    assert restored.is_halted is True
    codes = {str(row.get("code")) for row in durable.discrepancies}
    assert "order_account_transition_conflict" in codes


def test_recovery_repair_is_unique_across_restarts(tmp_path) -> None:
    paper = make_processor(tmp_path)
    _order, fill = fill_first(paper)
    transition_id = f"fill:{fill.fill_id}"
    remove_account_transition(paper.ledger.path, transition_id)

    first = restarted(tmp_path)
    second = restarted(tmp_path)
    committed = [
        row
        for row in PaperLedger(paper.ledger.path).account_events
        if row.get("transition_id") == transition_id
    ]

    assert first.is_halted is False
    assert second.is_halted is False
    assert len(committed) == 1


def _run_with_checkpoint(tmp_path, checkpoint_payload: dict[str, object]) -> dict[str, object]:
    root = tmp_path / "data"
    checkpoint = root / "runtime" / "checkpoint.json"
    atomic_json_write(checkpoint, checkpoint_payload)
    return run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=root / "runtime" / "cursor.json",
        checkpoint_path=checkpoint,
        strategy_config_path="configs/paper_spread_strategy_v1.json",
        max_cycles=1,
        poll_seconds=0.01,
    )


def test_corrupt_checkpoint_keeps_valid_ledger_ineligible_not_replayed(tmp_path) -> None:
    root = tmp_path / "data"
    checkpoint = root / "runtime" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text("{", encoding="utf-8")

    status = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=root / "runtime" / "cursor.json",
        checkpoint_path=checkpoint,
        strategy_config_path="configs/paper_spread_strategy_v1.json",
        max_cycles=1,
        poll_seconds=0.01,
    )

    assert status["checkpoint_integrity"] == "failed"
    assert status["paper_score_eligible"] is False


def test_checkpoint_behind_rebuilds_and_ahead_halts(tmp_path) -> None:
    root = tmp_path / "data"
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    ledger = PaperLedger(ledger_path)
    ledger.record_decision(
        event_key="fixture-frontier", decision="fixture", details={"execution_enabled": False}
    )
    behind = _run_with_checkpoint(
        tmp_path,
        {
            "execution_enabled": False,
            "committed_transition_frontier": 0,
            "clean_exit": True,
            "score_started_at": "2026-01-01T00:00:00+00:00",
        },
    )
    assert behind["checkpoint_integrity"] == "behind_ledger_rebuilt"
    assert behind["score_started_at"] == "2026-01-01T00:00:00+00:00"

    other = tmp_path / "ahead"
    ahead = _run_with_checkpoint(
        other,
        {
            "execution_enabled": False,
            "committed_transition_frontier": 999,
            "clean_exit": True,
            "score_started_at": "2026-01-01T00:00:00+00:00",
        },
    )
    assert ahead["halted"] is True
    assert ahead["halt_reason"] == "checkpoint_ahead_of_ledger"


def test_unclean_checkpoint_preserves_prior_score_start(tmp_path) -> None:
    status = _run_with_checkpoint(
        tmp_path,
        {
            "execution_enabled": False,
            "committed_transition_frontier": 0,
            "clean_exit": False,
            "score_started_at": "2026-01-01T00:00:00+00:00",
        },
    )

    assert status["score_started_at"] == "2026-01-01T00:00:00+00:00"
    assert status["recovery"]["unclean_prior_run"] is True


def test_weather_evidence_uses_only_production_metadata_and_strict_tuple(tmp_path) -> None:
    paper = make_processor(tmp_path)
    legacy_only = BookSnapshot(
        timestamp=BASE,
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=(("0.75", "100"),),
        asks=(("0.80", "100"),),
        metadata={
            "weather_receipt_eligible": True,
            "weather_observed_at": BASE.isoformat(),
            "weather_improving": True,
            "weather_market_lag": True,
        },
    )
    assert paper.process_snapshot(legacy_only) is None

    fill_first(paper)
    second = paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=2), metadata={"weather_observation_id": "second"})
    )
    assert second is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=3),
            "token",
            ShadowSide.SELL,
            "0.75",
            "1000",
            "second-fill",
        )
    )
    third = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(minutes=4),
            bid="0.73",
            ask="0.78",
            metadata={
                "weather_observation_id": "third",
                "weather_improving": False,
                "weather_unchanged": True,
                "weather_market_lag": False,
            },
        )
    )
    assert third is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=5),
            "token",
            ShadowSide.SELL,
            "0.73",
            "1000",
            "third-fill",
        )
    )
    prior = snapshot(
        at=BASE + timedelta(minutes=6),
        metadata={
            "weather_observation_id": "before-second",
            "weather_source_timestamp": (BASE + timedelta(minutes=1)).isoformat(),
            "weather_received_at": (BASE + timedelta(minutes=1, seconds=30)).isoformat(),
        },
    )
    assert paper.process_snapshot(prior) is None
    state = next(iter(paper.states.values()))
    assert "before-second" not in state.consumed_observations
    fourth = paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=7), metadata={"weather_observation_id": "fourth"})
    )
    assert fourth is not None


def test_stale_cached_book_cannot_price_max_hold_risk_exit(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)

    paper.sweep_lifecycle(as_of=BASE + timedelta(hours=2, minutes=1))

    state = next(iter(paper.states.values()))
    assert state.stranded is True
    assert paper.account.stranded_inventory_cost_usd > Decimal("0")
    assert paper.status(as_of=BASE + timedelta(hours=2, minutes=1))["risk_exit"][
        "last_book_age_seconds"
    ] > 300


def test_partial_exit_timeout_retries_only_stage_residual(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)
    state = next(iter(paper.states.values()))
    first = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=2), bid="0.80", ask="0.85")
    )
    assert first is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=3),
            "token",
            ShadowSide.BUY,
            "0.85",
            "103",
            "partial-exit",
        )
    )
    partial = state.exit_stage_filled_shares[0]
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=17))
    retry = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=18), bid="0.80", ask="0.85")
    )

    assert retry is not None
    assert retry.metadata["exit_attempt"] == 2
    expected = state.cumulative_bought_shares * Decimal("0.25") - partial
    assert retry.requested_shares == expected
    assert retry.idempotency_key != first.idempotency_key


def test_restart_derives_completed_exit_stage_after_fill_before_strategy_decision(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)
    first = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=2), bid="0.80", ask="0.85")
    )
    assert first is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=3),
            "token",
            ShadowSide.BUY,
            "0.85",
            "1000",
            "exit-fill-before-decision",
        )
    )
    # Model SIGKILL after the durable order/fill and account effect but before
    # the derived strategy-state decision records are appended.
    replace_rows(
        paper.ledger.path,
        [
            row
            for row in rows(paper.ledger.path)
            if not (
                row["record_type"] == "decision"
                and row.get("decision") in {"exit_stage_fill", "exit_stage_filled"}
            )
        ],
    )

    restored = restarted(tmp_path)
    state = next(iter(restored.states.values()))

    assert restored.is_halted is False
    assert state.exit_stage_filled_shares[0] == first.requested_shares
    assert 0 in state.completed_exit_stages
    next_stage = restored._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=4), bid="0.85", ask="0.90")
    )
    assert next_stage is not None
    assert next_stage.metadata["exit_stage"] == 1


def test_same_second_unsequenced_trade_group_is_unknown_not_a_fill(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    fills = paper.process_trades(
        (
            TradeEvent(BASE + timedelta(minutes=1), "token", ShadowSide.SELL, "0.75", "200", "a"),
            TradeEvent(BASE + timedelta(minutes=1), "token", ShadowSide.SELL, "0.75", "200", "b"),
        )
    )

    assert fills == ()
    assert order.fills == []
    assert paper.trade_evidence_counts["UNKNOWN_TRADE_SEQUENCE"] == 2
    restored = restarted(tmp_path)
    assert restored.trade_evidence_counts["UNKNOWN_TRADE_SEQUENCE"] == 2


def test_sequenced_same_second_trades_are_ordered_and_cross_source_duplicate_is_deduped(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    at = BASE + timedelta(minutes=1)
    # Feed the later trade first. Sequence 1 consumes the 100-share queue,
    # so only sequence 2 can fill the maker order.
    fills = paper.process_trades(
        (
            TradeEvent(at, "token", ShadowSide.SELL, "0.75", "3", "seq-2", sequence=2),
            TradeEvent(at, "token", ShadowSide.SELL, "0.75", "100", "seq-1", sequence=1),
        )
    )

    assert len(fills) == 1
    assert fills[0].trade_id == "seq-2"
    assert order.filled_shares == Decimal("3")
    duplicate_from_ws = TradeEvent(
        at,
        "token",
        ShadowSide.SELL,
        "0.75",
        "3",
        "seq-2",
        source="websocket",
        sequence=2,
    )
    assert paper.process_trades((duplicate_from_ws,)) == ()
    assert paper.trade_evidence_counts["duplicate_trade"] == 1


def test_restart_labels_persisted_trade_as_duplicate_without_reconsuming_queue(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    trade = TradeEvent(
        BASE + timedelta(minutes=1),
        "token",
        ShadowSide.SELL,
        "0.75",
        "101",
        "durable-trade",
        sequence=1,
    )
    assert paper.process_trades((trade,))
    assert order.filled_shares == Decimal("1")

    restored = restarted(tmp_path)
    assert restored.process_trades((trade,)) == ()
    restored_order = next(iter(restored.ledger.orders.values()))
    assert restored_order.filled_shares == Decimal("1")
    assert restored.trade_evidence_counts["duplicate_trade"] == 1


def test_first_verified_supervisor_read_after_restart_closes_removed_event(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)
    paper.set_supervisor_evidence(
        generation="generation-a",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE + timedelta(minutes=1),
    )

    restored = restarted(tmp_path)
    restored.set_supervisor_evidence(
        generation="generation-b",
        active_event_ids=set(),
        integrity="verified",
        as_of=BASE + timedelta(minutes=2),
    )

    state = next(iter(restored.states.values()))
    # A restart has no contemporaneous in-memory quote. The verified removal
    # therefore cancels exposure but conservatively strands the inventory
    # rather than inventing a current risk-exit bid.
    assert state.stranded is True
    assert state.closed is False
    assert restored.account.stranded_inventory_cost_usd > Decimal("0")


def test_isolated_supervisor_cursor_hash_is_verified_and_rejects_tampering(tmp_path) -> None:
    path = tmp_path / "paper-supervisor.json"
    _save_paper_supervisor_cursor(
        path,
        generation="generation-a",
        active_event_ids={"event-a", "event-b"},
        ledger_frontier=7,
    )

    generation, active, integrity = _load_paper_supervisor_cursor(path)

    assert (generation, active, integrity) == (
        "generation-a",
        {"event-a", "event-b"},
        "verified",
    )
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["active_event_hash"] = "not-the-set-hash"
    atomic_json_write(path, tampered)
    assert _load_paper_supervisor_cursor(path)[2] == "failed"


def test_continuous_restart_uses_isolated_cursor_to_strand_removed_event(tmp_path) -> None:
    root = tmp_path / "data"
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = PaperSpreadProcessor(ledger=PaperLedger(ledger_path), strategy=strategy())
    paper.set_supervisor_evidence(
        generation="generation-a",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    order = paper.process_snapshot(snapshot())
    assert order is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=1),
            "token",
            ShadowSide.SELL,
            "0.75",
            "1000",
            "entry",
        )
    )
    cursor_path = root / "runtime" / "paper-cursor.json"
    supervisor_cursor = cursor_path.with_name("paper-cursor_paper_supervisor.json")
    _save_paper_supervisor_cursor(
        supervisor_cursor,
        generation="generation-a",
        active_event_ids={"event"},
        ledger_frontier=paper.ledger.frontier,
    )
    atomic_json_write(
        root / "runtime" / "market_supervisor_status.json",
        {"execution_enabled": False, "generation": "generation-b", "active_events": []},
    )

    status = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "paper-status.json",
        cursor_path=cursor_path,
        strategy_config_path="configs/paper_spread_strategy_v1.json",
        max_cycles=1,
        poll_seconds=0.01,
    )

    assert status["stranded_positions"] == 1
    assert status["paper_supervisor_cursor_integrity"] == "verified"


def test_new_quality_window_cancels_active_order_and_blocks_new_snapshot(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    window = UpstreamQualityWindow(
        incident_id="incident",
        title="fixture",
        incident_type="maintenance",
        start_at=BASE + timedelta(seconds=1),
        end_at=None,
        affected_components=("clob websocket",),
        affects_market_data=True,
        affects_trading=False,
        status="active",
        source_url="",
        default_excluded=True,
    )
    paper.set_quality_evidence(
        integrity="verified",
        refreshed_at=BASE + timedelta(minutes=1),
        window_hash="fixture-quality",
        windows=(window,),
    )
    affected = paper.apply_quality_windows((window,), as_of=BASE + timedelta(minutes=1))

    assert affected == 1
    assert order.is_active is False
    assert paper.account.buy_reserved_usd == Decimal("0")
    quality_status = paper.status(as_of=BASE + timedelta(minutes=1))["upstream_quality"]
    assert quality_status["active_at_checked_at"] is True
    assert quality_status["affected_active_orders"] == 1
    second = snapshot(
        at=BASE + timedelta(minutes=2),
        event_id="event-two",
        market_id="market-two",
        token_id="token-two",
        metadata={"weather_observation_id": "quality-window-later"},
    )
    assert paper.process_snapshot(second) is None


def test_restart_preserves_account_orders_strategy_evidence_and_capital_time(tmp_path) -> None:
    paper = make_processor(tmp_path)
    paper.set_supervisor_evidence(
        generation="generation-a",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="quality-a"
    )
    for snapshot_at, bid, ask, event_id, trade_at, trade_price, improving, unchanged in (
        (BASE, "0.75", "0.80", "one", BASE + timedelta(minutes=1), "0.75", True, False),
        (
            BASE + timedelta(minutes=2),
            "0.75",
            "0.80",
            "two",
            BASE + timedelta(minutes=3),
            "0.75",
            True,
            False,
        ),
        (
            BASE + timedelta(minutes=4),
            "0.73",
            "0.78",
            "three",
            BASE + timedelta(minutes=5),
            "0.73",
            False,
            True,
        ),
        (
            BASE + timedelta(minutes=6),
            "0.75",
            "0.80",
            "four",
            BASE + timedelta(minutes=7),
            "0.75",
            True,
            False,
        ),
    ):
        order = paper.process_snapshot(
            snapshot(
                at=snapshot_at,
                bid=bid,
                ask=ask,
                metadata={
                    "weather_observation_id": event_id,
                    "weather_improving": improving,
                    "weather_unchanged": unchanged,
                    "weather_market_lag": improving,
                },
            )
        )
        assert order is not None
        paper.process_trade(
            TradeEvent(
                trade_at,
                "token",
                ShadowSide.SELL,
                trade_price,
                "1000",
                f"fill-{event_id}",
            )
        )
    exit_order = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=8), bid="0.81", ask="0.85")
    )
    assert exit_order is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=9),
            "token",
            ShadowSide.BUY,
            "0.85",
            "110",
            "partial-exit",
        )
    )
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=10))

    restored = restarted(tmp_path)
    old = paper.status(as_of=BASE + timedelta(minutes=10))
    new = restored.status(as_of=BASE + timedelta(minutes=10))
    old_state = next(iter(paper.states.values()))
    new_state = next(iter(restored.states.values()))

    assert new["account"] == old["account"]
    assert new["capital_utilization"] == old["capital_utilization"]
    assert new["station_day"] == old["station_day"]
    assert new["active_orders"] == old["active_orders"]
    assert restored.supervisor_generation == paper.supervisor_generation
    assert restored.supervisor_active_events == paper.supervisor_active_events
    assert restored.quality_window_hash == paper.quality_window_hash
    assert new_state.cumulative_bought_shares == old_state.cumulative_bought_shares
    assert new_state.exit_stage_filled_shares == old_state.exit_stage_filled_shares
    assert new_state.exit_stage_attempts == old_state.exit_stage_attempts
    assert new_state.consumed_observations == old_state.consumed_observations
    assert new_state.position_opened_at == old_state.position_opened_at


def test_paper_records_and_status_are_strictly_read_only_and_scan_clear(tmp_path) -> None:
    paper = make_processor(tmp_path)
    submit_first(paper)
    status = paper.status(as_of=BASE)

    assert status["execution_enabled"] is False
    assert status["execution_dependency_scan"]["status"] == "clear"
    for row in rows(paper.ledger.path):
        assert row["execution_enabled"] is False


def test_unreadable_upstream_and_health_gates_cannot_create_paper_orders(tmp_path) -> None:
    bare = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "bare.jsonl"), strategy=strategy()
    )
    assert bare.process_snapshot(snapshot()) is None
    assert any(
        row.get("details", {}).get("reason_code") == "supervisor_evidence_unreadable"
        for row in bare.ledger.decisions
    )

    healthy = make_processor(tmp_path)
    maintenance = BookSnapshot(
        timestamp=BASE,
        event_id="maintenance-event",
        market_id="maintenance-market",
        token_id="maintenance-token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=(("0.75", "100"),),
        asks=(("0.80", "100"),),
        upstream_status="maintenance",
        metadata={
            "weather_join_status": "aligned",
            "weather_observation_id": "maintenance-observation",
            "weather_source_timestamp": (BASE - timedelta(minutes=1)).isoformat(),
            "weather_received_at": (BASE - timedelta(seconds=30)).isoformat(),
            "weather_observation_new": True,
            "weather_market_lag": True,
            "weather_improving": True,
            "weather_worsening": False,
            "weather_unchanged": False,
        },
    )
    assert healthy.process_snapshot(maintenance) is None


def test_closed_event_cancels_active_buy_and_releases_only_its_reservation(tmp_path) -> None:
    paper = make_processor(tmp_path)
    order = submit_first(paper)

    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=2), closed_event_ids={"event"})

    state = next(iter(paper.states.values()))
    assert order.is_active is False
    assert paper.account.buy_reserved_usd == Decimal("0")
    assert state.closed is True


def test_four_exit_stages_conserve_shares_and_final_stage_clears_remainder(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)
    state = next(iter(paper.states.values()))
    stage_snapshots = (
        (BASE + timedelta(minutes=2), "0.80", "0.85", "exit-one"),
        (BASE + timedelta(minutes=4), "0.85", "0.90", "exit-two"),
        (BASE + timedelta(minutes=6), "0.88", "0.93", "exit-three"),
        (BASE + timedelta(minutes=8), "0.95", "0.99", "exit-four"),
    )
    sold = Decimal("0")
    for index, (at, bid, ask, event_id) in enumerate(stage_snapshots):
        order = paper._submit_exit_if_eligible(snapshot(at=at, bid=bid, ask=ask))
        assert order is not None
        fills = paper.process_trade(
            TradeEvent(
                at + timedelta(minutes=1),
                "token",
                ShadowSide.BUY,
                ask,
                "1000",
                event_id,
            )
        )
        sold += sum((fill.shares for fill in fills), start=Decimal("0"))
        assert index in state.completed_exit_stages

    engine = next(iter(paper.engines.values()))
    assert engine.inventory_shares == Decimal("0")
    assert sold == state.cumulative_bought_shares
    assert paper.account.inventory_cost_usd == Decimal("0")
    assert paper.account.cash_usd >= Decimal("0")
    assert paper.account.realized_pnl_usd >= Decimal("0")


def test_later_buy_expands_only_incomplete_exit_stage_basis(tmp_path) -> None:
    paper = make_processor(tmp_path)
    fill_first(paper)
    state = next(iter(paper.states.values()))
    first_exit = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=2), bid="0.80", ask="0.85")
    )
    assert first_exit is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=3),
            "token",
            ShadowSide.BUY,
            "0.85",
            "1000",
            "stage-zero",
        )
    )
    assert 0 in state.completed_exit_stages
    second_buy = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(minutes=4),
            metadata={"weather_observation_id": "later-confirmation"},
        )
    )
    assert second_buy is not None
    paper.process_trade(
        TradeEvent(
            BASE + timedelta(minutes=5),
            "token",
            ShadowSide.SELL,
            "0.75",
            "1000",
            "later-buy-fill",
        )
    )
    stage_one = paper._submit_exit_if_eligible(
        snapshot(at=BASE + timedelta(minutes=6), bid="0.85", ask="0.90")
    )

    assert stage_one is not None
    assert stage_one.metadata["exit_stage"] == 1
    assert 0 in state.completed_exit_stages
    expected = state.cumulative_bought_shares * Decimal("0.25")
    assert stage_one.requested_shares == expected
