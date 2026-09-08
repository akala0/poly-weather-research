"""Crash, restart, and fail-closed seal tests for the isolated Paper V1 model.

Every fixture is rooted below ``tmp_path``.  These tests call the local runtime
function directly only to exercise its file-boundary recovery contract; they
never invoke the Paper CLI or create a formal ``data/`` artifact.
"""

from __future__ import annotations

import copy
import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from poly_weather.paper_account import PaperLedger
from poly_weather.paper_spread_runtime import (
    PaperSpreadProcessor,
    PaperStrategyConfig,
    _rebuild_paper_join_state_from_cursor,
    replay_paper_spread,
    run_paper_spread_continuous,
)
from poly_weather.polymarket_status import UpstreamQualityWindow
from poly_weather.shadow_orders import BookSnapshot, ShadowOrderState, ShadowSide, TradeEvent
from poly_weather.shadow_runtime import ShadowCursor
from poly_weather.weather_market_join import align_weather_to_snapshots

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)
STRATEGY_PATH = Path("configs/paper_spread_strategy_v1.json")


def strategy() -> PaperStrategyConfig:
    return PaperStrategyConfig.load(STRATEGY_PATH)


def snapshot(
    *,
    at: datetime = BASE,
    bid: str = "0.75",
    ask: str = "0.80",
    event_id: str = "event",
    market_id: str = "market",
    token_id: str = "token",
    station_id: str = "KLAX",
    market_stale: bool = False,
    metadata: dict[str, object] | None = None,
) -> BookSnapshot:
    source_at = at - timedelta(minutes=1)
    received_at = at - timedelta(seconds=30)
    return BookSnapshot(
        timestamp=at,
        event_id=event_id,
        market_id=market_id,
        token_id=token_id,
        station_id=station_id,
        market_day="2026-09-02",
        season_version="season-v1",
        bids=((bid, "100"),),
        asks=((ask, "100"),),
        market_stale=market_stale,
        metadata={
            "weather_join_status": "aligned",
            "weather_observation_id": f"weather-{source_at.isoformat()}",
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


def processor(tmp_path: Path, *, ledger_path: Path | None = None) -> PaperSpreadProcessor:
    paper = PaperSpreadProcessor(
        ledger=PaperLedger(ledger_path or tmp_path / "paper.jsonl"), strategy=strategy()
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


def submit_first(paper: PaperSpreadProcessor):
    order = paper.process_snapshot(snapshot())
    assert order is not None
    return order


def fill_trade(*, event_id: str = "trade-fill", size: str = "101") -> TradeEvent:
    return TradeEvent(
        BASE + timedelta(minutes=1),
        "token",
        ShadowSide.SELL,
        "0.75",
        size,
        event_id,
        sequence=1,
        available_at=BASE + timedelta(minutes=2),
    )


def restarted(path: Path) -> PaperSpreadProcessor:
    return PaperSpreadProcessor(ledger=PaperLedger(path), strategy=strategy())


def ledger_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _seed_cycle_inputs(root: Path) -> tuple[Path, Path]:
    """Create a deliberately ordinary paired-book source and a zero cursor."""

    checkpoint = root / "raw" / "polymarket_book_checkpoints" / "fixture" / "events.jsonl"
    weather = root / "raw" / "weather_daemon" / "fixture" / "events.jsonl"
    checkpoint.parent.mkdir(parents=True)
    weather.parent.mkdir(parents=True)
    received_at = _runtime_base()
    rows = [
        {
            "received_at": received_at.isoformat(),
            "market_slug": f"event/market:{outcome}",
            "asset_id": asset_id,
            "bids": [{"price": "0.75", "size": "100"}],
            "asks": [{"price": "0.80", "size": "100"}],
            "book_complete": True,
        }
        for outcome, asset_id in (("Yes", "yes-token"), ("No", "token"))
    ]
    checkpoint.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    weather.write_text("", encoding="utf-8")
    cursor = ShadowCursor(root / "runtime" / "cursor.json")
    cursor.position(checkpoint)
    cursor.position(weather)
    cursor.save()
    return checkpoint, cursor.path


def _runtime_base() -> datetime:
    """A recent, already-received fixture instant for the continuous follower."""

    return datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)


@pytest.mark.parametrize(
    "stage",
    ("source_conversion", "snapshot_conversion", "snapshot_processing", "trade_processing", "lifecycle_processing"),
)
def test_handled_cycle_exception_does_not_commit_source_cursor(
    tmp_path: Path, stage: str
) -> None:
    root = tmp_path / "temporary-data"
    checkpoint, cursor_path = _seed_cycle_inputs(root)

    def fail_at(current: str) -> None:
        if current == stage:
            raise OSError(f"fixture fault at {stage}")

    failed = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
        _fault_injector=fail_at,
    )

    assert failed["cursor_commit_state"] == "not_committed_cycle_failure"
    assert ShadowCursor.load(cursor_path).position(checkpoint)["offset"] == 0

    recovered = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    assert recovered["data_coverage"]["new_market_rows"] == 2
    assert ShadowCursor.load(cursor_path).position(checkpoint)["offset"] == checkpoint.stat().st_size


def _write_public_tape(
    root: Path,
    *,
    transaction_hash: str = "trade-fill",
    at: datetime | None = None,
) -> Path:
    destination = root / "public_trades" / "event.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    timestamp = at or _runtime_base()
    payload = {
        "event_slug": "event",
        "fetched_at": (timestamp + timedelta(seconds=2)).isoformat(),
        "trades": [
            {
                "proxy_wallet": "",
                "asset_id": "token",
                "condition_id": "market",
                "event_slug": "event",
                "market_slug": "event/market",
                "outcome": "NO",
                "side": "SELL",
                "size": "101",
                "price": "0.75",
                "timestamp": timestamp.isoformat(),
                "available_at": (timestamp + timedelta(seconds=2)).isoformat(),
                "transaction_hash": transaction_hash,
                "sequence": 1,
            }
        ],
    }
    destination.write_text(json.dumps(payload), encoding="utf-8")
    return destination


def test_replayed_successful_prefix_is_idempotent_after_cycle_failure(tmp_path: Path) -> None:
    root = tmp_path / "temporary-data"
    root.mkdir()
    cursor_path = root / "runtime" / "cursor.json"
    ShadowCursor(cursor_path).save()
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    at = _runtime_base()
    order = paper.process_snapshot(snapshot(at=at))
    assert order is not None
    _write_public_tape(root, at=at + timedelta(seconds=1))

    def fail_after_trade(stage: str) -> None:
        if stage == "trade_processing":
            raise OSError("after durable queue transition")

    first = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
        _fault_injector=fail_after_trade,
    )
    assert first["cursor_commit_state"] == "not_committed_cycle_failure"
    assert order.filled_shares == Decimal("0")  # the runner restored a separate live object
    after_failure = PaperLedger(ledger_path)
    filled_after_failure = next(iter(after_failure.orders.values())).filled_shares
    assert filled_after_failure == Decimal("1")

    run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    recovered = PaperLedger(ledger_path)
    restored_order = next(iter(recovered.orders.values()))
    assert restored_order.filled_shares == Decimal("1")
    assert len(restored_order.fills) == 1


def test_account_commit_oserror_halts_without_cursor_advance(tmp_path: Path) -> None:
    """A failed durable fill commit cannot acknowledge any staged source row."""

    root = tmp_path / "temporary-data"
    weather = root / "raw" / "weather_daemon" / "fixture" / "events.jsonl"
    weather.parent.mkdir(parents=True)
    weather.write_text(
        json.dumps(
            _weather_row(
                at=_runtime_base(),
                temperature_f="70",
                observation_id="staged-weather-boundary",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    cursor_path = root / "runtime" / "cursor.json"
    source_cursor = ShadowCursor(cursor_path)
    source_cursor.position(weather)
    source_cursor.save()
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    order_at = _runtime_base()
    assert paper.process_snapshot(snapshot(at=order_at)) is not None
    _write_public_tape(root, transaction_hash="account-commit-fault", at=order_at + timedelta(seconds=1))

    def inject(stage: str) -> None:
        if stage == "account_commit":
            raise OSError("fixture account commit failure")

    failed = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
        _ledger_factory=lambda path: PaperLedger(path, persistence_fault_injector=inject),
    )

    assert failed["state"] == "halted"
    assert failed["paper_score_eligible"] is False
    assert failed["cursor_commit_state"] == "not_committed_cycle_failure"
    assert ShadowCursor.load(cursor_path).position(weather)["offset"] == 0

    # The post-trade order snapshot remains durable, but this injected append
    # failure left a durable discrepancy. The next process must retain that
    # HALT rather than treating its in-memory account reconstruction as a
    # clean recovery; replaying the same tape still cannot create another
    # fill.
    repaired = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "repaired-status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "repaired-checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    restored = PaperLedger(ledger_path)
    restored_order = next(iter(restored.orders.values()))
    assert repaired["state"] == "halted"
    assert restored.invalid is True
    assert restored_order.filled_shares == Decimal("1")
    assert len(restored_order.fills) == 1
    assert sum(row["event"] == "fill_buy" for row in restored.account_events) == 0


def test_partial_fill_and_trade_consumption_are_one_durable_fact(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = fill_trade(size="101")
    fills = paper.process_trade(trade)
    assert fills and order.filled_shares == Decimal("1")
    trade_key = paper._durable_trade_event_key(trade)
    trade_rows = [row for row in ledger_rows(paper.ledger.path) if row.get("event_key") == trade_key]
    assert len(trade_rows) == 1
    assert trade_rows[0]["record_type"] == "order"
    assert trade_rows[0]["order"]["filled_shares"] == "1"


def test_restart_cannot_consume_partial_fill_trade_twice(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    submit_first(paper)
    trade = fill_trade(size="101")
    assert paper.process_trade(trade)
    restored = restarted(paper.ledger.path)
    assert restored.process_trade(trade) == ()
    order = next(iter(restored.ledger.orders.values()))
    assert order.filled_shares == Decimal("1")
    assert len(order.fills) == 1
    assert restored.account.inventory_cost_usd == Decimal("0.75")


def test_queue_only_trade_replay_does_not_reduce_queue_twice(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    order = submit_first(paper)
    trade = fill_trade(event_id="queue-only", size="100")
    assert paper.process_trade(trade) == ()
    assert order.volume_ahead == Decimal("0")
    restored = restarted(paper.ledger.path)
    restored_order = next(iter(restored.ledger.orders.values()))
    assert restored.process_trade(trade) == ()
    assert restored_order.volume_ahead == Decimal("0")
    assert restored_order.filled_shares == Decimal("0")


def test_unproven_same_transaction_siblings_remain_unknown(tmp_path: Path) -> None:
    """Archive sequence is not a verified fill ID; never sum these rows."""

    paper = processor(tmp_path)
    order = submit_first(paper)
    at = BASE + timedelta(minutes=1)
    first = TradeEvent(at, "token", ShadowSide.SELL, "0.75", "100", "same-tx", sequence=1)
    second = TradeEvent(at, "token", ShadowSide.SELL, "0.75", "101", "same-tx", sequence=2)

    fills = paper.process_trades((first, second))
    assert fills == ()
    assert order.filled_shares == Decimal("0")
    trade_rows = [
        row
        for row in ledger_rows(paper.ledger.path)
        if str(row.get("event_key") or "").startswith("paper-trade-v2:")
    ]
    assert trade_rows == []
    assert paper.trade_evidence_counts["UNKNOWN_TRADE_IDENTITY_CONFLICT"] == 1

    restored = restarted(paper.ledger.path)
    assert restored.process_trades((first, second)) == ()
    restored_order = next(iter(restored.ledger.orders.values()))
    assert restored_order.filled_shares == order.filled_shares
    assert len(restored_order.fills) == 0


def test_multiple_eligible_active_orders_recovered_durably_halts(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    order = submit_first(paper)
    duplicate = copy.deepcopy(order)
    duplicate.order_id = "paper-fixture-second-active-order"
    duplicate.idempotency_key = "paper-fixture-second-active-order"
    paper.ledger.save(duplicate, event_key="fixture:second-active-order")

    restored = restarted(paper.ledger.path)
    durable = PaperLedger(paper.ledger.path)
    assert restored.is_halted is True
    assert any(
        row.get("code") == "multiple_eligible_active_orders_recovered"
        for row in durable.discrepancies
    )


@pytest.mark.parametrize(
    "fault",
    ("transition_intent", "order_save", "account_effect_apply", "account_commit"),
)
def test_persistence_fault_halts_current_processor(tmp_path: Path, fault: str) -> None:
    def inject(stage: str) -> None:
        if stage == fault:
            raise OSError(f"fixture {fault} failure")

    ledger = PaperLedger(tmp_path / "paper.jsonl", persistence_fault_injector=inject)
    paper = PaperSpreadProcessor(ledger=ledger, strategy=strategy())
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    # The hook may halt while persisting either prerequisite evidence.  Reset
    # the fixture only when the chosen point is reached by submit logic.
    if paper.is_halted:
        assert paper.status()["paper_score_eligible"] is False
        return
    assert paper.process_snapshot(snapshot()) is None
    assert paper.is_halted is True
    assert paper.status()["paper_score_eligible"] is False


def test_transition_abort_oserror_halts_current_processor(tmp_path: Path) -> None:
    """The abort branch is a persistence boundary too, not a safe retry."""

    paper = processor(tmp_path)
    submit_first(paper)

    def inject(stage: str) -> None:
        if stage == "transition_abort":
            raise OSError("fixture abort failure")

    paper.ledger._persistence_fault_injector = inject
    assert paper._paper_buy(
        snapshot(at=BASE + timedelta(minutes=1)),
        tranche_index=1,
        evidence=None,
        gate_reason="fixture",
    ) is None
    assert paper.is_halted is True
    assert paper.status()["paper_score_eligible"] is False


def test_unwritable_halt_record_terminates_follower_fail_closed(tmp_path: Path) -> None:
    def inject(stage: str) -> None:
        if stage == "halt_append":
            raise OSError("fixture halt append failure")

    paper = processor(tmp_path)
    paper.ledger._persistence_fault_injector = inject
    paper._halt("fixture_halt")
    assert paper.is_halted is True
    assert paper.fatal_persistence_failure is True
    assert paper.status()["paper_score_eligible"] is False


def test_halted_processor_rejects_snapshot_trade_and_lifecycle_mutation(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    order = submit_first(paper)
    paper._halt("fixture_halt")
    before = {
        "ledger": paper.ledger.path.read_bytes(),
        "account": paper.account.as_dict(),
        "order": order.as_dict(),
        "state": copy.deepcopy(next(iter(paper.states.values()))),
    }

    assert paper.process_snapshot(snapshot(at=BASE + timedelta(minutes=2))) is None
    assert paper.process_trade(fill_trade()) == ()
    assert paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=20)) == ()
    paper.close_portfolio(snapshot(at=BASE + timedelta(minutes=20)), reason="fixture-close")
    assert paper._submit_exit_if_eligible(snapshot(at=BASE + timedelta(minutes=20))) is None

    assert paper.ledger.path.read_bytes() == before["ledger"]
    assert paper.account.as_dict() == before["account"]
    assert order.as_dict() == before["order"]
    assert next(iter(paper.states.values())) == before["state"]


def _weather_row(*, at: datetime, temperature_f: str, observation_id: str) -> dict[str, object]:
    return {
        "product": "latest_observation",
        "collection_mode": "realtime",
        "station_id": "KLAX",
        "source_timestamp_ms": int(at.timestamp() * 1000),
        "received_at": (at + timedelta(seconds=1)).isoformat(),
        "temperature_f": temperature_f,
        "raw": {"source_payload": {"id": observation_id}},
    }


@pytest.mark.parametrize("compressed", (False, True))
def test_restart_weather_join_uses_only_cursor_visible_prefix(
    tmp_path: Path, compressed: bool
) -> None:
    root = tmp_path / "temporary-data"
    suffix = "events.jsonl.gz" if compressed else "events.jsonl"
    source = root / "raw" / "weather_daemon" / "fixture" / suffix
    source.parent.mkdir(parents=True)
    prefix_row = _weather_row(
        at=BASE, temperature_f="70", observation_id="visible-prefix"
    )
    # These later high/low observations must never become a restart baseline
    # while the cursor still commits only the first record.
    future_rows = (
        _weather_row(
            at=BASE + timedelta(minutes=10), temperature_f="95", observation_id="future-high"
        ),
        _weather_row(
            at=BASE + timedelta(minutes=11), temperature_f="40", observation_id="future-low"
        ),
    )
    lines = [json.dumps(row) + "\n" for row in (prefix_row, *future_rows)]
    if compressed:
        with gzip.open(source, "wt", encoding="utf-8") as handle:
            handle.writelines(lines)
    else:
        source.write_text("".join(lines), encoding="utf-8")

    cursor = ShadowCursor(root / "runtime" / "cursor.json")
    position = cursor.position(source)
    if compressed:
        position["offset"] = source.stat().st_size
        position["line"] = 1
    else:
        position["offset"] = len(lines[0].encode("utf-8"))
        position["line"] = 1
    cursor.save()

    visible, restart_state = _rebuild_paper_join_state_from_cursor(
        weather_paths=(source,), checkpoint_paths=(), cursor=cursor
    )
    assert [row.observation_id for row in visible] == ["visible-prefix"]
    assert restart_state["previous_temperature"][("KLAX", "latest_observation")] == Decimal("70")

    # A physically trimmed archive produces the same pre-replay decision
    # metadata. This compares the restart path to the no-future-row control.
    control = root / "control" / suffix
    control.parent.mkdir(parents=True)
    if compressed:
        with gzip.open(control, "wt", encoding="utf-8") as handle:
            handle.write(lines[0])
    else:
        control.write_text(lines[0], encoding="utf-8")
    control_cursor = ShadowCursor(root / "runtime" / "control-cursor.json")
    control_position = control_cursor.position(control)
    control_position["offset"] = control.stat().st_size
    control_position["line"] = 1
    control_cursor.save()
    control_visible, control_state = _rebuild_paper_join_state_from_cursor(
        weather_paths=(control,), checkpoint_paths=(), cursor=control_cursor
    )
    assert [row.observation_id for row in control_visible] == ["visible-prefix"]
    assert restart_state == control_state

    replay_row = {
        "observed_at": (BASE + timedelta(minutes=2)).isoformat(),
        "event_id": "event",
        "market_id": "market",
        "station_id": "KLAX",
        "market_day": "2026-09-02",
        "season_version": "season-v1",
        "no": {
            "asset_id": "token",
            "bids": [{"price": "0.75", "size": "100"}],
            "asks": [{"price": "0.80", "size": "100"}],
            "tick_size": "0.01",
        },
    }
    restarted_rows, _ = align_weather_to_snapshots(
        (replay_row,), visible, state=copy.deepcopy(restart_state)
    )
    control_rows, _ = align_weather_to_snapshots(
        (replay_row,), control_visible, state=copy.deepcopy(control_state)
    )
    assert restarted_rows[0]["metadata"] == control_rows[0]["metadata"]
    restarted_paper = processor(tmp_path / "restart-paper")
    control_paper = processor(tmp_path / "control-paper")
    assert restarted_paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=2), metadata=restarted_rows[0]["metadata"])
    ) is None
    assert control_paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=2), metadata=control_rows[0]["metadata"])
    ) is None


def test_profit_exit_precedes_rejected_future_tranche(tmp_path: Path) -> None:
    for tranche_index in (1, 2, 3):
        paper = processor(tmp_path / str(tranche_index))
        submit_first(paper)
        assert paper.process_trade(fill_trade(size="1000"))
        state = next(iter(paper.states.values()))
        state.tranche_index = tranche_index
        exit_order = paper.process_snapshot(
            snapshot(
                at=BASE + timedelta(minutes=2),
                bid="0.80",
                ask="0.85",
                metadata={"weather_improving": False, "weather_market_lag": False},
            )
        )
        assert exit_order is not None
        assert exit_order.side is ShadowSide.SELL
        assert exit_order.metadata["exit_stage"] == 0


def test_risk_exit_rejects_quality_window_between_quote_and_decision(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    quote = snapshot(at=BASE + timedelta(minutes=2))
    window = UpstreamQualityWindow(
        incident_id="fixture-incident",
        title="fixture",
        incident_type="maintenance",
        start_at=quote.timestamp + timedelta(seconds=1),
        end_at=quote.timestamp + timedelta(seconds=5),
        affected_components=("market",),
        affects_market_data=True,
        affects_trading=False,
        status="resolved",
        source_url="",
        default_excluded=True,
    )
    paper.quality_windows = (window,)
    assert paper._risk_exit_eligible(
        quote, as_of=quote.timestamp + timedelta(seconds=2), key=quote.portfolio_key
    ) is False
    assert paper.trade_evidence_counts["RISK_EXIT_QUALITY_INTERVAL_OVERLAP"] == 1


def test_risk_exit_interval_keeps_all_native_quote_boundaries_fail_closed(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    quote = snapshot(at=BASE + timedelta(minutes=2))
    key = quote.portfolio_key
    assert paper._risk_exit_eligible(quote, as_of=quote.timestamp, key=key) is True

    ended_but_overlapping = UpstreamQualityWindow(
        incident_id="ended-overlap",
        title="fixture",
        incident_type="maintenance",
        start_at=quote.timestamp - timedelta(seconds=1),
        end_at=quote.timestamp + timedelta(seconds=1),
        affected_components=("market",),
        affects_market_data=True,
        affects_trading=False,
        status="resolved",
        source_url="",
        default_excluded=True,
    )
    paper.quality_windows = (ended_but_overlapping,)
    assert paper._risk_exit_eligible(
        quote, as_of=quote.timestamp + timedelta(seconds=2), key=key
    ) is False

    paper.quality_windows = ()
    assert paper._risk_exit_eligible(
        quote, as_of=quote.timestamp - timedelta(seconds=1), key=key
    ) is False
    assert paper._risk_exit_eligible(
        quote,
        as_of=quote.timestamp + paper.strategy.risk_exit_snapshot_age + timedelta(seconds=1),
        key=key,
    ) is False
    no_bid = BookSnapshot(
        timestamp=quote.timestamp,
        event_id=quote.event_id,
        market_id=quote.market_id,
        token_id=quote.token_id,
        station_id=quote.station_id,
        market_day=quote.market_day,
        season_version=quote.season_version,
        bids=(),
        asks=quote.asks,
        metadata=quote.metadata,
    )
    assert paper._risk_exit_eligible(no_bid, as_of=no_bid.timestamp, key=key) is False


def test_inactive_supervisor_event_cannot_open_first_order(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids=set(),
        integrity="verified",
        as_of=BASE,
    )
    assert paper.process_snapshot(snapshot()) is None
    assert not paper.ledger.orders
    rejected = [row for row in paper.ledger.decisions if row.get("decision") == "tranche_rejected"]
    assert rejected[-1]["details"]["reason_code"] == "event_not_active_in_verified_supervisor"


def test_supervisor_unreadable_empty_and_active_sets_have_distinct_entry_outcomes(tmp_path: Path) -> None:
    unreadable = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "unreadable.jsonl"), strategy=strategy()
    )
    unreadable.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    unreadable.set_supervisor_evidence(
        generation=None, active_event_ids=None, integrity="unreadable", as_of=BASE
    )
    assert unreadable.process_snapshot(snapshot()) is None
    first = [
        row for row in unreadable.ledger.decisions if row.get("decision") == "tranche_rejected"
    ][-1]
    assert first["details"]["reason_code"] == "supervisor_evidence_unreadable"

    paper = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "verified-empty.jsonl"), strategy=strategy()
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    paper.set_supervisor_evidence(
        generation="fixture-generation", active_event_ids=set(), integrity="verified", as_of=BASE
    )
    assert paper.process_snapshot(snapshot()) is None
    second = [row for row in paper.ledger.decisions if row.get("decision") == "tranche_rejected"][-1]
    assert second["details"]["reason_code"] == "event_not_active_in_verified_supervisor"

    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE + timedelta(minutes=1),
    )
    assert paper.process_snapshot(snapshot(at=BASE + timedelta(minutes=1))) is not None


def test_stale_snapshot_cannot_open_entry(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    assert paper.process_snapshot(snapshot(market_stale=True)) is None
    assert not paper.ledger.orders


def test_processor_portfolios_share_one_global_200_account(tmp_path: Path) -> None:
    paper = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=strategy()
    )
    event_ids = {f"event-{index}" for index in range(11)}
    paper.set_supervisor_evidence(
        generation="fixture-generation", active_event_ids=event_ids, integrity="verified", as_of=BASE
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )

    submitted = []
    for index in range(10):
        station = "KLGA" if index == 1 else "KLAX"
        bid, ask = ("0.60", "0.65") if station == "KLGA" else ("0.75", "0.80")
        submitted.append(
            paper.process_snapshot(
                snapshot(
                    at=BASE + timedelta(seconds=index),
                    bid=bid,
                    ask=ask,
                    event_id=f"event-{index}",
                    market_id=f"market-{index}",
                    token_id=f"token-{index}",
                    station_id=station,
                )
            )
        )
    assert all(order is not None for order in submitted)
    assert paper.account.buy_reserved_usd == Decimal("200")
    assert paper.account.available_cash_usd == Decimal("0")

    blocked = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(seconds=11),
            event_id="event-10",
            market_id="market-10",
            token_id="token-10",
        )
    )
    assert blocked is None
    assert len(paper.ledger.orders) == 10
    assert {order.station_id for order in paper.ledger.orders.values()} == {"KLAX", "KLGA"}


def test_closed_event_releases_only_its_own_portfolio_reservation(tmp_path: Path) -> None:
    paper = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=strategy()
    )
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event-a", "event-b"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    first = paper.process_snapshot(
        snapshot(event_id="event-a", market_id="market-a", token_id="token-a")
    )
    second = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(seconds=1),
            event_id="event-b",
            market_id="market-b",
            token_id="token-b",
        )
    )
    assert first is not None and second is not None
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=1), closed_event_ids={"event-a"})
    assert first.state is ShadowOrderState.CANCELLED
    assert second.is_active is True
    assert paper.account.buy_reserved_usd == Decimal("20")


def test_restart_replays_downtime_public_trade_once(tmp_path: Path) -> None:
    root = tmp_path / "temporary-data"
    root.mkdir()
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    order_at = _runtime_base()
    assert paper.process_snapshot(snapshot(at=order_at)) is not None
    tape = _write_public_tape(
        root, transaction_hash="downtime", at=order_at + timedelta(seconds=1)
    )
    cursor_path = root / "runtime" / "cursor.json"
    ShadowCursor(cursor_path).save()

    first = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    assert first["data_coverage"]["new_data_api_trade_rows"] == 1
    first_order = next(iter(PaperLedger(ledger_path).orders.values()))
    assert first_order.filled_shares == Decimal("1")

    # Keep the tape byte-for-byte and mtime-stable. A restart must replay it
    # through the durable trade key, not baseline it or fill a second time.
    before_stat = tape.stat()
    second = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    assert tape.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert second["data_coverage"]["new_data_api_trade_rows"] == 1
    second_order = next(iter(PaperLedger(ledger_path).orders.values()))
    assert second_order.filled_shares == Decimal("1")
    assert len(second_order.fills) == 1


def _write_ws_trade(
    root: Path, *, transaction_hash: str, at: datetime | None = None
) -> Path:
    websocket = root / "raw" / "polymarket_clob_websocket" / "fixture" / "events.jsonl"
    websocket.parent.mkdir(parents=True, exist_ok=True)
    timestamp = at or _runtime_base()
    websocket.write_text(
        json.dumps(
            {
                "run_id": "fixture",
                "sequence": 1,
                "received_at": (timestamp + timedelta(seconds=2)).isoformat(),
                "source_timestamp_ms": int(timestamp.timestamp() * 1000),
                "event_type": "last_trade_price",
                "market_slug": "event/market:No",
                "raw": {
                    "market": "market",
                    "asset_id": "token",
                    "price": "0.75",
                    "size": "101",
                    "side": "SELL",
                    "timestamp": int(timestamp.timestamp() * 1000),
                    "transaction_hash": transaction_hash,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return websocket


def test_unmatched_ws_trade_is_durable_pending_unknown(tmp_path: Path) -> None:
    root = tmp_path / "temporary-data"
    root.mkdir()
    order_at = _runtime_base()
    trade_at = order_at + timedelta(seconds=1)
    websocket = _write_ws_trade(root, transaction_hash="pending", at=trade_at)
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    assert paper.process_snapshot(snapshot(at=order_at)) is not None
    cursor_path = root / "runtime" / "cursor.json"
    cursor = ShadowCursor(cursor_path)
    cursor.position(websocket)
    cursor.save()

    status = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        checkpoint_path=root / "runtime" / "checkpoint.json",
        strategy_config_path=STRATEGY_PATH,
        bootstrap_at_tail=False,
        max_cycles=1,
        poll_seconds=0.01,
    )
    restored = restarted(ledger_path)
    assert status["data_coverage"]["unmatched_ws_trade_count"] == 1
    assert restored.pending_ws_trade_evidence
    assert restored.feed_continuity == "unknown"
    assert restored.status()["paper_score_eligible"] is False


def test_later_public_match_resolves_unknown_and_consumes_once(tmp_path: Path) -> None:
    root = tmp_path / "temporary-data"
    root.mkdir()
    order_at = _runtime_base()
    trade_at = order_at + timedelta(seconds=1)
    websocket = _write_ws_trade(root, transaction_hash="resolvable", at=trade_at)
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    assert paper.process_snapshot(snapshot(at=order_at)) is not None
    cursor_path = root / "runtime" / "cursor.json"
    cursor = ShadowCursor(cursor_path)
    cursor.position(websocket)
    cursor.save()
    kwargs = {
        "data_dir": root,
        "ledger_path": ledger_path,
        "status_path": root / "runtime" / "status.json",
        "cursor_path": cursor_path,
        "checkpoint_path": root / "runtime" / "checkpoint.json",
        "strategy_config_path": STRATEGY_PATH,
        "bootstrap_at_tail": False,
        "max_cycles": 1,
        "poll_seconds": 0.01,
    }
    run_paper_spread_continuous(**kwargs)
    _write_public_tape(root, transaction_hash="resolvable", at=trade_at)
    resolved = run_paper_spread_continuous(**kwargs)
    restored = restarted(ledger_path)
    order = next(iter(restored.ledger.orders.values()))
    assert not restored.pending_ws_trade_evidence
    assert order.filled_shares == Decimal("1")
    assert len(order.fills) == 1
    assert any(row.get("decision") == "trade_evidence_resolved" for row in restored.ledger.decisions)
    assert resolved["data_coverage"]["accepted_queue_trade_rows"] >= 1

    run_paper_spread_continuous(**kwargs)
    again = next(iter(PaperLedger(ledger_path).orders.values()))
    assert again.filled_shares == Decimal("1")
    assert len(again.fills) == 1


def test_unrelated_pending_ws_trade_never_changes_another_token_queue(tmp_path: Path) -> None:
    paper = processor(tmp_path)
    order = submit_first(paper)
    pending = SimpleNamespace(
        asset_id="unrelated-token",
        market_id="unrelated-market",
        transaction_hash="unrelated-pending",
        event_id="unrelated-event",
        source_timestamp=BASE + timedelta(minutes=1),
        received_at=BASE + timedelta(minutes=1, seconds=1),
        price=Decimal("0.75"),
        size=Decimal("100"),
        side="SELL",
        sequence=1,
    )
    paper._record_pending_ws_trade(pending)
    assert order.volume_ahead == Decimal("100")

    assert paper.process_trade(fill_trade(event_id="target-token-queue-only", size="100")) == ()
    assert order.volume_ahead == Decimal("0")
    assert order.filled_shares == Decimal("0")
    assert paper.pending_ws_trade_evidence
    assert "unresolved_ws_public_trade_evidence" in paper.status()["paper_score_ineligible_reasons"]


def test_exact_paper_config_rejects_each_tampered_field(tmp_path: Path) -> None:
    source = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
    tampered_values = {
        "schema_version": 2,
        "version": "paper-spread-v1-other",
        "execution_enabled": True,
        "initial_cash_usd": "201",
        "trigger_strategy": "price_band",
        "quote_mode": "improve_one_tick",
        "fill_model": "touch",
        "entry_bands": {"KLAX": [["0.70", "0.84"]], "KLGA": [["0.50", "0.70"]]},
        "tranche_plan": [{"usd": "21", "gate": "initial"}],
        "exit_plan": [{"rise": "0.06", "fraction_of_initial_shares": "0.25"}],
        "order_timeout_seconds": 901,
        "max_hold_seconds": 7201,
        "risk_exit_snapshot_age_seconds": 301,
        "station_day_budget_mode": "current_open_exposure",
    }
    for field, value in tampered_values.items():
        payload = copy.deepcopy(source)
        payload[field] = value
        candidate = tmp_path / f"{field}.json"
        candidate.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError):
            PaperStrategyConfig.load(candidate)


def test_paper_ledger_rejects_non_paper_schema_collision(tmp_path: Path) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "ledger_kind": "shadow_orders_v2_token_scoped",
                "record_type": "order",
                "execution_enabled": False,
                "order": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ledger = PaperLedger(path)
    assert ledger.invalid is True
    assert any(row["reason"] == "non_paper_ledger_kind" for row in ledger.legacy_invalid_records)


def test_continuous_poll_expires_without_new_token_frame_exactly_once(tmp_path: Path) -> None:
    """The continuous production loop advances timeout without a new book."""

    root = tmp_path / "temporary-data"
    ledger_path = root / "raw" / "shadow_orders" / "paper.jsonl"
    order_at = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=901)
    paper = processor(tmp_path, ledger_path=ledger_path)
    order = paper.process_snapshot(snapshot(at=order_at))
    assert order is not None
    kwargs = {
        "data_dir": root,
        "ledger_path": ledger_path,
        "status_path": root / "runtime" / "status.json",
        "cursor_path": root / "runtime" / "cursor.json",
        "checkpoint_path": root / "runtime" / "checkpoint.json",
        "strategy_config_path": STRATEGY_PATH,
        "bootstrap_at_tail": False,
        "max_cycles": 1,
        "poll_seconds": 0.01,
    }

    run_paper_spread_continuous(**kwargs)
    after_first = PaperLedger(ledger_path)
    expired = next(iter(after_first.orders.values()))
    assert expired.state is ShadowOrderState.EXPIRED
    assert sum(row["event"] == "release_buy" for row in after_first.account_events) == 1
    assert after_first.invalid is False

    run_paper_spread_continuous(**kwargs)
    after_second = PaperLedger(ledger_path)
    assert sum(row["event"] == "release_buy" for row in after_second.account_events) == 1
    assert next(iter(after_second.orders.values())).state is ShadowOrderState.EXPIRED


def test_replay_expiration_uses_resting_order_event_clock(tmp_path: Path) -> None:
    """A historical replay must use its own event time, never wall time."""

    order_at = datetime(2020, 1, 1, 12, tzinfo=UTC)
    ledger_path = tmp_path / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger_path)
    resting = paper.process_snapshot(snapshot(at=order_at))
    assert resting is not None

    before_timeout = replay_paper_spread(
        [snapshot(at=order_at + timedelta(seconds=899))],
        ledger_path=ledger_path,
        strategy_config_path=STRATEGY_PATH,
    )
    assert before_timeout["checked_at"] == (order_at + timedelta(seconds=899)).isoformat()
    assert next(iter(PaperLedger(ledger_path).orders.values())).state is ShadowOrderState.RESTING

    at_timeout = order_at + timedelta(seconds=900)
    at_timeout_status = replay_paper_spread(
        [snapshot(at=at_timeout)],
        ledger_path=ledger_path,
        strategy_config_path=STRATEGY_PATH,
    )
    expired = next(iter(PaperLedger(ledger_path).orders.values()))
    assert at_timeout_status["checked_at"] == at_timeout.isoformat()
    assert expired.state is ShadowOrderState.EXPIRED
    assert expired.expired_at == at_timeout


def test_no_native_bid_ignores_all_surrogate_prices(tmp_path: Path) -> None:
    """No diagnostic or historical price may silently price a Paper exit."""

    paper = processor(tmp_path)
    submit_first(paper)
    assert paper.process_trade(fill_trade(size="1000"))
    closing = BookSnapshot(
        timestamp=BASE + timedelta(minutes=2),
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=(),
        asks=(("0.80", "100"),),
        book_complete=False,
        metadata={
            "midpoint": "0.79",
            "last_trade_price": "0.78",
            "opposite_token_price": "0.21",
            "one_minus_opposite_token_price": "0.79",
            "historical_price": "0.77",
            "settlement_price": "1",
        },
    )

    paper.close_portfolio(closing, reason="fixture-no-native-bid", as_of=closing.timestamp)

    engine = next(iter(paper.engines.values()))
    state = next(iter(paper.states.values()))
    assert state.stranded is True
    assert paper.account.stranded_inventory_cost_usd > Decimal("0")
    assert paper.account.realized_pnl_usd == Decimal("0")
    assert all(order.side is not ShadowSide.SELL for order in engine.orders)
    assert paper.status()["stranded_positions"] == 1
