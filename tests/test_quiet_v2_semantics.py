"""Failure-mode coverage for the v2 information and microstructure closure."""

import gzip
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.information_clock import ImpactClass, InformationClock, InformationEvent
from poly_weather.market_microstructure import (
    TradeIntensityTracker,
    build_cross_bucket_mass_index,
    build_l2_churn_index,
)
from poly_weather.market_regime import (
    MarketMetrics,
    MarketRegimeStateMachine,
    RegimeState,
    RegimeThresholds,
)
from poly_weather.quiet_window_strategy import (
    QuietWindowConfig,
    QuietWindowEngine,
    analyze_information_clock,
    render_information_reaction_report,
    write_quiet_snapshot_store,
)
from poly_weather.shadow_orders import BookSnapshot

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def _event(
    event_id: str,
    minute: int,
    *,
    kind: str = "wrh_observation",
    temperature: str | None = "80",
    risk_fields: dict[str, str] | None = None,
    impact_class: ImpactClass | None = None,
    payload_hash: str | None = None,
    metadata: dict[str, object] | None = None,
) -> InformationEvent:
    fields = risk_fields if risk_fields is not None else (
        {"temperature_f": temperature} if temperature is not None else {}
    )
    at = BASE + timedelta(minutes=minute)
    return InformationEvent(
        event_id=event_id,
        source="WRH",
        kind=kind,
        source_at=at,
        available_at=at,
        station_id="KLAX",
        market_day="2026-08-28",
        payload_hash=payload_hash or event_id,
        impact_class=impact_class,
        metadata={"temperature_f": temperature, "risk_fields": fields, **(metadata or {})},
    )


def _metric(minute: int, *, churn: Decimal | None = Decimal("0")) -> MarketMetrics:
    return MarketMetrics(
        timestamp=BASE + timedelta(minutes=minute),
        station_id="KLAX",
        market_day="2026-08-28",
        token_id="token-1",
        best_bid=Decimal("0.40"),
        best_ask=Decimal("0.45"),
        mid=Decimal("0.425"),
        spread=Decimal("0.05"),
        top_bid_depth=Decimal("20"),
        top_ask_depth=Decimal("20"),
        imbalance=Decimal("0"),
        price_slope=Decimal("0"),
        cumulative_move=Decimal("0"),
        trade_intensity_multiple=Decimal("1"),
        churn_rate=churn,
        cross_bucket_mass_error=Decimal("0"),
    )


def _snapshot(
    minute: int,
    *,
    token: str = "token-1",
    bid: str = "0.40",
    ask: str = "0.45",
    book_complete: bool = True,
) -> BookSnapshot:
    return BookSnapshot(
        timestamp=BASE + timedelta(minutes=minute),
        event_id="event-1",
        market_id=f"market-{token}",
        token_id=token,
        bids=((Decimal(bid), Decimal("100")),),
        asks=((Decimal(ask), Decimal("100")),),
        station_id="KLAX",
        market_day="2026-08-28",
        tick_size=Decimal("0.01"),
        season_version="heat_2026_v2",
        book_complete=book_complete,
        metadata={
            "trade_intensity_multiple": "1",
            "churn_rate": "0",
            "cross_bucket_mass_error": "0",
        },
    )


def _thresholds() -> RegimeThresholds:
    return RegimeThresholds(
        stable_windows=1,
        reaction_cooldown=timedelta(0),
        minimum_observation_spacing=timedelta(minutes=1),
    )


def test_many_same_or_lower_wrh_updates_are_no_op_and_do_not_block_quiet() -> None:
    clock = InformationClock()
    machine = MarketRegimeStateMachine(
        station_id="KLAX", market_day="2026-08-28", thresholds=_thresholds()
    )
    first = clock.ingest(_event("high-0", 0, temperature="80"))
    assert first is not None and first.impact_class is ImpactClass.HARD_RESET
    machine.observe(_metric(0), information_event=first)
    last = None
    for minute in range(1, 101):
        # Distinct receipt/payload identities, but only a non-high temperature
        # changes.  A polling timestamp must not keep resetting the regime.
        accepted = clock.ingest(
            _event(f"poll-{minute}", minute, temperature="79" if minute % 2 else "80")
        )
        assert accepted is not None
        assert accepted.impact_class is ImpactClass.NO_OP
        last = machine.observe(_metric(minute), information_event=accepted)
    assert last is not None and last.state is RegimeState.QUIET


def test_regular_weather_decimal_jitter_is_no_op_until_a_material_risk_band_changes() -> None:
    clock = InformationClock()
    baseline = clock.ingest(
        _event(
            "nws-baseline",
            0,
            kind="nws_observation",
            risk_fields={
                "temperature_f": "80",
                "wind_direction": "30:wmoUnit:degree_(angle)",
                "wind_speed": "9.252:wmoUnit:km_h-1",
                "cloud": "CLR",
                "weather": "clear",
                "dewpoint": "16:wmoUnit:degC",
            },
        )
    )
    jitter = clock.ingest(
        _event(
            "nws-jitter",
            1,
            kind="nws_observation",
            risk_fields={
                "temperature_f": "80",
                "wind_direction": "40:wmoUnit:degree_(angle)",
                "wind_speed": "11.124:wmoUnit:km_h-1",
                "cloud": "CLR",
                "weather": "clear",
                "dewpoint": "16.1:wmoUnit:degC",
            },
        )
    )
    material = clock.ingest(
        _event(
            "nws-breezy",
            2,
            kind="nws_observation",
            risk_fields={
                "temperature_f": "80",
                "wind_direction": "130:wmoUnit:degree_(angle)",
                "wind_speed": "35:wmoUnit:km_h-1",
                "cloud": "CLR",
                "weather": "clear",
                "dewpoint": "16.2:wmoUnit:degC",
            },
        )
    )
    assert baseline is not None and baseline.impact_class is ImpactClass.HARD_RESET
    assert jitter is not None and jitter.impact_class is ImpactClass.NO_OP
    assert material is not None and material.impact_class is ImpactClass.SOFT_UPDATE
    assert material.impact_reason == "material_weather_risk_regime_changed"


def test_next_observed_daily_high_is_a_hard_reset() -> None:
    clock = InformationClock()
    clock.ingest(_event("baseline", 0, temperature="80"))
    increased = clock.ingest(_event("high-101", 101, temperature="81"))
    assert increased is not None
    assert increased.impact_class is ImpactClass.HARD_RESET
    assert increased.impact_reason == "observed_daily_high_increased"


def test_settlement_unchanged_is_no_op_but_hash_change_is_hard() -> None:
    clock = InformationClock()
    initial = clock.ingest(
        _event(
            "settlement-a",
            0,
            kind="settlement_rule",
            temperature=None,
            metadata={"state_hash": "a"},
            payload_hash="a",
        )
    )
    repeated = clock.ingest(
        _event(
            "settlement-b",
            1,
            kind="settlement_rule",
            temperature=None,
            metadata={"state_hash": "a"},
            payload_hash="b",
        )
    )
    changed = clock.ingest(
        _event(
            "settlement-c",
            2,
            kind="settlement_rule",
            temperature=None,
            metadata={"state_hash": "c"},
            payload_hash="c",
        )
    )
    assert initial is not None and initial.impact_class is ImpactClass.NO_OP
    assert repeated is not None and repeated.impact_class is ImpactClass.NO_OP
    # The semantic state hash, rather than a transport payload revision,
    # defines a settlement-rule change.
    assert changed is not None and changed.impact_class is ImpactClass.HARD_RESET


def test_speci_is_not_hard_by_name_but_critical_field_change_is_hard() -> None:
    clock = InformationClock()
    first = clock.ingest(
        _event("speci-1", 0, kind="speci", risk_fields={"temperature_f": "80", "wind_speed": "5"})
    )
    same = clock.ingest(
        _event("speci-2", 1, kind="speci", risk_fields={"temperature_f": "80", "wind_speed": "5"})
    )
    changed = clock.ingest(
        _event("speci-3", 2, kind="speci", risk_fields={"temperature_f": "80", "wind_speed": "25"})
    )
    assert first is not None and first.impact_class is ImpactClass.HARD_RESET
    assert same is not None and same.impact_class is ImpactClass.NO_OP
    assert changed is not None and changed.impact_class is ImpactClass.HARD_RESET


def test_cross_archive_duplicate_payloads_do_not_create_extra_clock_events() -> None:
    clock = InformationClock()
    first = clock.ingest(_event("wrh-a", 0, payload_hash="same"))
    duplicate = clock.ingest(_event("wrh-b", 1, payload_hash="same"))
    assert first is not None
    assert duplicate is None
    assert clock.as_dict()["event_count"] == 1


def test_information_report_breaks_out_station_and_invalid_events(tmp_path) -> None:
    valid = _event("valid-high", 0, temperature="80")
    invalid = replace(
        _event("invalid-receipt", 1, temperature="79"),
        source_at=None,
        available_at=None,
    )
    result = analyze_information_clock([valid, invalid])
    report_path = tmp_path / "information.md"
    render_information_reaction_report(result, report_path)

    text = report_path.read_text(encoding="utf-8")
    assert result["events_by_station_and_impact_class"]["KLAX"] == {
        "HARD_RESET": 1,
        "INVALID": 1,
    }
    assert "| wrh_observation | 1 | 0 | 0 | 1 |" in text
    assert "| KLAX | 1 | 0 | 0 | 1 |" in text


def test_trade_intensity_has_explicit_warmup_then_uses_strict_prior_baseline() -> None:
    tracker = TradeIntensityTracker(station_timezones={"KLAX": "America/Los_Angeles"})
    snapshots = [_snapshot(index * 5) for index in range(5)]
    prior = None
    values = []
    for snapshot in snapshots:
        values.append(tracker.observe(snapshot, previous_at=prior, trades=()))
        prior = snapshot.timestamp
    assert values[0]["metric_status"]["trade_intensity_multiple"] == "WARMUP_INSUFFICIENT_BASELINE"
    assert values[3]["metric_status"]["trade_intensity_multiple"] == "WARMUP_INSUFFICIENT_BASELINE"
    assert values[4]["metric_status"]["trade_intensity_multiple"] == "OK"
    assert values[4]["trade_intensity_multiple"] == Decimal("0")


def test_trade_intensity_baseline_is_bounded_but_strictly_prior() -> None:
    tracker = TradeIntensityTracker(
        station_timezones={"KLAX": "America/Los_Angeles"},
        minimum_samples=2,
        baseline_window_samples=3,
    )
    prior = None
    for index in range(8):
        snapshot = _snapshot(index * 5)
        tracker.observe(snapshot, previous_at=prior, trades=())
        prior = snapshot.timestamp
    history = next(iter(tracker.history.values()))
    assert len(history) == 3


def test_l2_remove_without_trade_is_churn_not_a_shadow_fill(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    asset = "token-a"
    rows = [
        {
            "run_id": "run-1",
            "received_at": BASE.isoformat(),
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        },
        {
            "run_id": "run-1",
            "received_at": (BASE + timedelta(seconds=10)).isoformat(),
            "event_type": "price_change",
            "asset_id": asset,
            "raw": {"price_changes": [{"asset_id": asset, "side": "BUY", "price": "0.40", "size": "40"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    index = build_l2_churn_index([path], asset_ids=[asset])
    value = index.lookup(asset, BASE + timedelta(seconds=10))
    assert value["l2_removed"] == Decimal("60")
    assert value["l2_traded"] == Decimal("0")
    assert value["l2_cancelled_at_level"] == Decimal("60")


def test_sparse_l2_index_matches_target_snapshot_window(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    asset = "token-a"
    rows = [
        {
            "run_id": "run-1",
            "received_at": BASE.isoformat(),
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        },
        {
            "run_id": "run-1",
            "received_at": (BASE + timedelta(seconds=10)).isoformat(),
            "event_type": "price_change",
            "asset_id": asset,
            "raw": {
                "price_changes": [
                    {"asset_id": asset, "side": "BUY", "price": "0.40", "size": "40"}
                ]
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    at = BASE + timedelta(seconds=10)
    index = build_l2_churn_index(
        [path],
        asset_ids=[asset],
        lookup_times_by_asset={asset: (at,)},
    )
    value = index.lookup(asset, at)
    assert value["l2_removed"] == Decimal("60")
    assert value["l2_cancelled_at_level"] == Decimal("60")
    assert index.summary()["sparse_snapshot_index"] is True
    assert index.summary()["snapshot_window_observation_count"] == 1


def test_sparse_l2_marks_archive_silence_unknown_at_gap_boundary(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    asset = "token-a"
    first = BASE + timedelta(seconds=10)
    later = BASE + timedelta(minutes=20)
    rows = [
        {
            "run_id": "run-1",
            "received_at": BASE.isoformat(),
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        },
        # A later non-target archive row closes both snapshot windows without
        # supplying a recovery book for the target token.
        {
            "run_id": "run-1",
            "received_at": (later + timedelta(seconds=1)).isoformat(),
            "event_type": "book",
            "asset_id": "other-token",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    index = build_l2_churn_index(
        [path],
        asset_ids=[asset],
        lookup_times_by_asset={asset: (first, later)},
    )
    assert index.lookup(asset, first)["metric_status"]["churn_rate"] == "OK"
    later_value = index.lookup(asset, later)
    assert later_value["metric_status"]["churn_rate"] == "UNKNOWN_TAPE_GAP"
    assert later_value["l2_status_reason"] == "archive_gap"


def test_sparse_l2_carries_valid_book_across_snapshot_gap_when_archive_continuous(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    asset = "token-a"
    first = BASE + timedelta(seconds=10)
    later = BASE + timedelta(minutes=20)
    rows = [
        {
            "run_id": "run-1",
            "received_at": BASE.isoformat(),
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        }
    ]
    # Keep the archive clock continuous while the target token has no new
    # update.  A sparse strategy snapshot is not a tape gap and must retain
    # the causally reconstructed L2 state.
    for second in range(60, 20 * 60 + 2, 60):
        rows.append(
            {
                "run_id": "run-1",
                "received_at": (BASE + timedelta(seconds=second)).isoformat(),
                "event_type": "heartbeat",
                "asset_id": "other-token",
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    index = build_l2_churn_index(
        [path],
        asset_ids=[asset],
        lookup_times_by_asset={asset: (first, later)},
    )
    assert index.lookup(asset, first)["metric_status"]["churn_rate"] == "OK"
    later_value = index.lookup(asset, later)
    assert later_value["metric_status"]["churn_rate"] == "OK"
    assert later_value["l2_status_reason"] == "book_snapshot"


def test_snapshot_store_preserves_exact_token_schedule(tmp_path) -> None:
    first = _snapshot(1, token="token-a")
    second = _snapshot(2, token="token-b")
    store = write_quiet_snapshot_store([first, second], tmp_path / "books.sqlite3")
    assert store.snapshot_times_by_token() == {
        "token-a": (first.timestamp,),
        "token-b": (second.timestamp,),
    }


def test_l2_prefers_plain_archive_and_skips_unrelated_invalid_rows(tmp_path) -> None:
    partition = tmp_path / "2026-08-28"
    partition.mkdir()
    plain = partition / "events.jsonl"
    archived = partition / "events.jsonl.gz"
    asset = "token-a"
    rows = [
        # This deliberately malformed unrelated row has a usable envelope.
        # The focused L2 index must never JSON-decode it.
        (
            '{"run_id":"run-1","received_at":"2026-08-28T00:00:00+00:00",'
            '"asset_id":"other-token", broken}'
        ),
        json.dumps(
            {
                "run_id": "run-1",
                "received_at": (BASE + timedelta(seconds=1)).isoformat(),
                "event_type": "book",
                "asset_id": asset,
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.45", "size": "100"}],
            }
        ),
    ]
    plain.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with gzip.open(archived, "wt", encoding="utf-8") as handle:
        handle.write(rows[1] + "\n")

    index = build_l2_churn_index([archived, plain], asset_ids=[asset])
    value = index.lookup(asset, BASE + timedelta(seconds=1))
    assert value["metric_status"]["churn_rate"] == "OK"
    assert index.summary()["source_file_count"] == 1
    assert index.summary()["duplicate_archive_file_count"] == 1


def test_reconnect_or_archive_gap_makes_churn_unknown(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    asset = "token-a"
    rows = [
        {
            "run_id": "run-1",
            "received_at": BASE.isoformat(),
            "event_type": "book",
            "asset_id": asset,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.45", "size": "100"}],
        },
        {
            "run_id": "run-2",
            "received_at": (BASE + timedelta(minutes=3)).isoformat(),
            "event_type": "price_change",
            "asset_id": asset,
            "raw": {"price_changes": [{"asset_id": asset, "side": "BUY", "price": "0.40", "size": "40"}]},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    index = build_l2_churn_index([path], asset_ids=[asset])
    value = index.lookup(asset, BASE + timedelta(minutes=3))
    assert value["metric_status"]["churn_rate"] == "UNKNOWN_TAPE_GAP"


def test_cross_bucket_desynchronization_is_unknown_not_a_complement_proxy() -> None:
    def pair(market: str, at: datetime) -> dict[str, object]:
        return {
            "event_slug": "event-1",
            "market_slug": market,
            "observed_at": at,
            "yes": {"bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.45", "size": "10"}]},
        }

    rows = (pair("event-1/a", BASE), pair("event-1/b", BASE + timedelta(minutes=5)))
    index = build_cross_bucket_mass_index(lambda: iter(rows), synchronization_tolerance=timedelta(seconds=60))
    assert index.lookup("event-1/b", BASE + timedelta(minutes=5)).status == "UNKNOWN_CROSS_BUCKET_SYNC"


def test_incomplete_token_does_not_poison_healthy_token_same_station_day() -> None:
    engine = QuietWindowEngine(config=QuietWindowConfig(), thresholds=_thresholds())
    event = _event("anchor", 0)
    engine.process_snapshot(_snapshot(0, token="good"), information_events=(event,))
    engine.process_snapshot(_snapshot(0, token="bad", book_complete=False), information_events=(event,))
    good = engine.process_snapshot(_snapshot(1, token="good"))
    bad = engine.process_snapshot(_snapshot(1, token="bad", book_complete=False))
    assert good.state is RegimeState.QUIET
    assert bad.state is RegimeState.DIGESTION


def test_hard_reset_reanchors_but_no_op_does_not() -> None:
    engine = QuietWindowEngine(config=QuietWindowConfig(), thresholds=_thresholds())
    engine.process_snapshot(_snapshot(0), information_events=(_event("anchor", 0),))
    engine.process_snapshot(_snapshot(1))
    key = next(iter(engine._previous_metrics))
    original_anchor = engine._previous_metrics[key].metadata["anchor_mid"]
    engine.process_snapshot(
        _snapshot(2, bid="0.50", ask="0.55"),
        information_events=(_event("noop", 2, impact_class=ImpactClass.NO_OP),),
    )
    assert engine._previous_metrics[key].metadata["anchor_mid"] == original_anchor
    engine.process_snapshot(
        _snapshot(3, bid="0.60", ask="0.65"),
        information_events=(_event("hard", 3, impact_class=ImpactClass.HARD_RESET),),
    )
    assert engine._previous_metrics[key].metadata["anchor_mid"] == "0.625"


def test_unknown_coverage_and_true_violation_have_distinct_reasons() -> None:
    machine = MarketRegimeStateMachine(
        station_id="KLAX", market_day="2026-08-28", thresholds=_thresholds()
    )
    anchor = _event("anchor", 0)
    unknown = _metric(1, churn=None)
    machine.observe(_metric(0), information_event=anchor)
    coverage = machine.observe(unknown)
    assert "UNKNOWN_TAPE_GAP" in coverage.coverage_reasons
    unstable = machine.observe(
        MarketMetrics(
            **{
                **_metric(2).as_dict(),
                "timestamp": BASE + timedelta(minutes=2),
                "churn_rate": Decimal("1"),
            }
        )
    )
    assert any(value.startswith("UNSTABLE_TRUE_VIOLATION:") for value in unstable.true_violations)


def test_future_receipt_never_reaches_information_clock_state_machine() -> None:
    clock = InformationClock()
    event = InformationEvent(
        event_id="future-receipt",
        source="WRH",
        kind="wrh_observation",
        source_at=BASE,
        available_at=BASE + timedelta(minutes=5),
        station_id="KLAX",
        market_day="2026-08-28",
        payload_hash="future",
        metadata={"risk_fields": {"temperature_f": "80"}},
    )
    assert clock.ingest(event, decision_at=BASE + timedelta(minutes=4)) is None
    assert clock.as_dict()["event_count"] == 0
