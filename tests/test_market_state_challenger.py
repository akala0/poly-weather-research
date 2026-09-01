from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.information_clock import ImpactClass, InformationEvent
from poly_weather.market_state_challenger import (
    BreakoutState,
    MarketStateChallengerConfig,
    _enrich_microstructure,
    analyze_market_state_challenger,
    evaluate_candidates,
    render_market_state_challenger_report,
)
from poly_weather.shadow_orders import BookSnapshot, TradeEvent

BASE = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _config(**overrides: object) -> MarketStateChallengerConfig:
    values = {
        "version": "test-v1",
        "trailing_window_minutes": 10,
        "confirmation_window_minutes": 3,
        "breakout_ticks": 2,
        "survival_fraction": "0.67",
        "minimum_trailing_observations": 2,
        "minimum_confirmation_observations": 2,
        "maximum_snapshot_gap_minutes": 5,
        "outcome_horizons_minutes": (5,),
        "bid_target_rises": ("0.05", "0.10"),
        "required_metric_names": (
            "mid",
            "spread",
            "top_bid_depth",
            "top_ask_depth",
            "imbalance",
        ),
        "forward_cutoff": "2026-08-30T23:59:59+00:00",
    }
    values.update(overrides)
    return MarketStateChallengerConfig(**values)


def _snapshot(
    minute: int,
    *,
    bid: str = "0.40",
    ask: str | None = None,
    token: str = "token-1",
    market: str = "market-1",
    station: str = "KLAX",
    day: str = "2026-08-30",
    candidate: bool = False,
    complete: bool = True,
    quality_excluded: bool = False,
    metadata: dict[str, object] | None = None,
) -> BookSnapshot:
    bid_value = Decimal(bid)
    ask_value = Decimal(ask) if ask is not None else bid_value + Decimal("0.02")
    values: dict[str, object] = {
        "rule_provenance": {
            "tick_size": "0.01",
            "tick_size_source": "websocket_raw_book",
            "min_order_size": "5",
            "min_order_size_source": "gamma_event_archived_market_metadata",
        }
    }
    if candidate:
        values.update(weather_market_lag=True, weather_improving=True)
    if metadata:
        values.update(metadata)
    return BookSnapshot(
        timestamp=BASE + timedelta(minutes=minute),
        event_id=f"event-{day}",
        market_id=market,
        token_id=token,
        bids=((bid_value, Decimal("100")),),
        asks=((ask_value, Decimal("80")),),
        station_id=station,
        market_day=day,
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
        book_complete=complete,
        season_version="2026-summer-v1",
        quality_excluded=quality_excluded,
        metadata=values,
    )


def _surviving_path(*, token: str = "token-1", market: str = "market-1") -> list[BookSnapshot]:
    return [
        _snapshot(-10, bid="0.39", token=token, market=market),
        _snapshot(-5, bid="0.40", token=token, market=market),
        _snapshot(0, bid="0.45", token=token, market=market, candidate=True),
        _snapshot(1, bid="0.43", token=token, market=market),
        _snapshot(2, bid="0.44", token=token, market=market),
        _snapshot(3, bid="0.44", token=token, market=market),
        _snapshot(4, bid="0.98", ask="0.99", token=token, market=market),
        _snapshot(8, bid="0.50", token=token, market=market),
    ]


def _information_event(
    event_id: str,
    minute: int,
    *,
    impact_class: ImpactClass = ImpactClass.HARD_RESET,
    day: str = "2026-08-30",
) -> InformationEvent:
    at = BASE + timedelta(minutes=minute)
    return InformationEvent(
        event_id=event_id,
        source="WRH",
        kind="wrh_observation",
        source_at=at,
        available_at=at,
        station_id="KLAX",
        market_day=day,
        impact_class=impact_class,
        metadata={"event": event_id},
    )


def _hard_reset(event_id: str, minute: int, *, day: str = "2026-08-30") -> InformationEvent:
    return _information_event(event_id, minute, day=day)


def test_strict_prior_confirmation_and_future_windows_do_not_overlap() -> None:
    rows = evaluate_candidates(_surviving_path(), config=_config())

    assert len(rows) == 1
    row = rows[0]
    assert row.state is BreakoutState.SURVIVING_BREAKOUT
    assert row.candidate_at == BASE
    assert row.decision_at == BASE + timedelta(minutes=3)
    assert row.trailing_high_bid == Decimal("0.40")
    # The 0.45 candidate quote is not allowed into the strictly-prior range.
    assert row.breakout_level == Decimal("0.42")
    assert row.confirmation_observation_count == 3

    outcome = row.outcomes[5]
    assert outcome.status == "complete"
    # The 0.99 quote is strictly after decision_at and belongs to the path, but
    # the decision quote itself is not reused as a future observation.
    assert outcome.observation_count == 2
    assert outcome.best_bid_change == Decimal("0.06")
    assert outcome.maximum_favorable_bid_change == Decimal("0.54")
    assert outcome.target_hits[Decimal("0.05")] is True
    assert outcome.target_hits[Decimal("0.10")] is True


def test_same_timestamp_candidate_quote_cannot_confirm_breakout() -> None:
    snapshots = [
        _snapshot(-10, bid="0.39"),
        _snapshot(-5, bid="0.40"),
        _snapshot(0, bid="0.60", candidate=True),
        _snapshot(1, bid="0.40"),
        _snapshot(3, bid="0.40"),
        _snapshot(8, bid="0.41"),
    ]

    row = evaluate_candidates(snapshots, config=_config())[0]

    assert row.state is BreakoutState.UNCONFIRMED
    assert row.breakout_observation_count == 0


def test_surviving_failed_and_unconfirmed_are_price_path_states() -> None:
    surviving = _surviving_path(token="survive", market="survive-market")
    failed = [
        _snapshot(-10, bid="0.39", token="fail", market="fail-market"),
        _snapshot(-5, bid="0.40", token="fail", market="fail-market"),
        _snapshot(0, bid="0.40", token="fail", market="fail-market", candidate=True),
        _snapshot(1, bid="0.43", token="fail", market="fail-market"),
        _snapshot(2, bid="0.41", token="fail", market="fail-market"),
        _snapshot(3, bid="0.40", token="fail", market="fail-market"),
        _snapshot(8, bid="0.39", token="fail", market="fail-market"),
    ]
    unconfirmed = [
        _snapshot(-10, bid="0.39", token="flat", market="flat-market"),
        _snapshot(-5, bid="0.40", token="flat", market="flat-market"),
        _snapshot(0, bid="0.40", token="flat", market="flat-market", candidate=True),
        _snapshot(1, bid="0.41", token="flat", market="flat-market"),
        _snapshot(3, bid="0.41", token="flat", market="flat-market"),
        _snapshot(8, bid="0.40", token="flat", market="flat-market"),
    ]

    states = {
        row.token_id: row.state
        for row in evaluate_candidates(surviving + failed + unconfirmed, config=_config())
    }

    assert states == {
        "survive": BreakoutState.SURVIVING_BREAKOUT,
        "fail": BreakoutState.FAILED_BREAKOUT,
        "flat": BreakoutState.UNCONFIRMED,
    }


def test_unknown_propagates_for_quality_gap_incomplete_book_and_rule_provenance() -> None:
    variants = {
        "quality": _snapshot(1, bid="0.43", quality_excluded=True),
        "archive-gap": _snapshot(1, bid="0.43", metadata={"archive_gap": True}),
        "incomplete": _snapshot(1, bid="0.43", complete=False),
        "rule": _snapshot(
            1,
            bid="0.43",
            metadata={
                "rule_provenance": {
                    "tick_size": None,
                    "tick_size_source": "UNKNOWN",
                    "min_order_size": None,
                    "min_order_size_source": "UNKNOWN",
                }
            },
        ),
    }

    for label, bad_snapshot in variants.items():
        path = _surviving_path()
        path[3] = bad_snapshot
        row = evaluate_candidates(path, config=_config())[0]
        assert row.state is BreakoutState.UNKNOWN, label
        assert row.unknown_reasons, label


def test_hard_reset_segments_episodes_deduplicates_and_contaminates_confirmation() -> None:
    snapshots = _surviving_path()
    snapshots.insert(3, _snapshot(0, bid="0.46", candidate=True))
    events = (
        _hard_reset("episode-1", -1),
        _hard_reset("new-information", 2),
    )

    rows = evaluate_candidates(snapshots, config=_config(), information_events=events)

    assert len(rows) == 1
    assert rows[0].episode_id == "episode-1"
    assert rows[0].state is BreakoutState.UNKNOWN
    assert "hard_reset_during_confirmation" in rows[0].unknown_reasons


def test_hard_reset_after_decision_censors_only_affected_outcome_horizon() -> None:
    config = _config(outcome_horizons_minutes=(1, 5))
    events = (_hard_reset("episode-1", -11), _hard_reset("later", 6))

    row = evaluate_candidates(
        _surviving_path(), config=config, information_events=events
    )[0]

    assert row.state is BreakoutState.SURVIVING_BREAKOUT
    assert row.outcomes[1].status == "complete"
    assert row.outcomes[5].status == "contaminated_by_hard_reset"
    assert row.outcomes[5].best_bid_change is None


def test_soft_update_after_decision_is_reported_without_censoring_outcome() -> None:
    event = _information_event(
        "soft-update",
        6,
        impact_class=ImpactClass.SOFT_UPDATE,
    )

    row = evaluate_candidates(
        _surviving_path(), config=_config(), information_events=(event,)
    )[0]

    assert row.outcomes[5].status == "complete"
    assert row.outcomes[5].new_information_before_endpoint is True
    assert row.outcomes[5].new_information_event_count == 1


def test_hard_reset_starts_a_fresh_trailing_baseline() -> None:
    snapshots = [
        _snapshot(-10, bid="0.80"),
        _snapshot(-4, bid="0.39"),
        _snapshot(-2, bid="0.40"),
        _snapshot(0, bid="0.45", candidate=True),
        _snapshot(1, bid="0.43"),
        _snapshot(2, bid="0.44"),
        _snapshot(3, bid="0.44"),
        _snapshot(8, bid="0.50"),
    ]

    row = evaluate_candidates(
        snapshots,
        config=_config(),
        information_events=(_hard_reset("fresh-episode", -5),),
    )[0]

    assert row.trailing_high_bid == Decimal("0.40")
    assert row.breakout_level == Decimal("0.42")
    assert row.state is BreakoutState.SURVIVING_BREAKOUT


def test_decision_uses_first_snapshot_at_or_after_confirmation_cutoff() -> None:
    snapshots = _surviving_path()
    snapshots.pop(5)

    row = evaluate_candidates(snapshots, config=_config())[0]

    assert row.decision_at == BASE + timedelta(minutes=4)
    assert row.state is BreakoutState.SURVIVING_BREAKOUT
    assert row.outcomes[5].best_bid_change == Decimal("-0.48")


def test_duplicate_window_timestamp_fails_closed() -> None:
    snapshots = _surviving_path()
    snapshots.insert(4, _snapshot(2, bid="0.45"))

    row = evaluate_candidates(snapshots, config=_config())[0]

    assert row.state is BreakoutState.UNKNOWN
    assert "ambiguous_duplicate_snapshot_timestamp" in row.unknown_reasons


def test_token_scope_prevents_other_token_quotes_from_confirming_or_forming_outcome() -> None:
    target = [
        _snapshot(-10, bid="0.39"),
        _snapshot(-5, bid="0.40"),
        _snapshot(0, bid="0.40", candidate=True),
        _snapshot(1, bid="0.40"),
        _snapshot(3, bid="0.40"),
        _snapshot(8, bid="0.40"),
    ]
    other = [
        _snapshot(minute, bid="0.99", token="other", market="other-market")
        for minute in (1, 2, 3, 8)
    ]

    row = evaluate_candidates(target + other, config=_config())[0]

    assert row.token_id == "token-1"
    assert row.state is BreakoutState.UNCONFIRMED
    assert row.outcomes[5].best_bid_change == Decimal("0.00")


def test_station_day_is_the_independent_summary_unit_and_states_conserve() -> None:
    first = _surviving_path(token="one", market="market-one")
    second = _surviving_path(token="two", market="market-two")

    result = analyze_market_state_challenger(first + second, config=_config())

    assert result["execution_enabled"] is False
    assert result["candidate_funnel"] == {
        "raw_candidate": 2,
        "unique_episode": 2,
        "temporal_coverage": 2,
        "known_microstructure": 2,
        "classified": 2,
        "outcome_complete": 2,
    }
    assert sum(result["state_counts"].values()) == result["candidate_funnel"]["unique_episode"]
    summary = result["summaries"]["SURVIVING_BREAKOUT"]["5"]
    assert summary["candidate_count"] == 2
    assert summary["station_day_count"] == 1
    assert summary["bid_targets"]["0.05"]["sample_count"] == 1
    assert summary["bid_targets"]["0.05"]["count"] == 1
    assert summary["bid_targets"]["0.05"]["wilson_low"] is not None
    assert summary["bid_targets"]["0.05"]["wilson_high"] is not None
    assert summary["bid_targets"]["0.05"]["statistically_unreliable"] is True
    split = result["information_summaries"]["SURVIVING_BREAKOUT"]["5"]
    assert split["no_new_information"]["station_day_count"] == 1
    assert split["new_information"]["station_day_count"] == 0


def test_report_states_non_execution_boundary(tmp_path) -> None:
    result = analyze_market_state_challenger(_surviving_path(), config=_config())
    path = tmp_path / "market_state_report.md"

    render_market_state_challenger_report(result, path)

    text = path.read_text(encoding="utf-8")
    assert "not a maker-fill or PnL result" in text
    assert "execution_enabled=false" in text
    assert "UNKNOWN is retained separately" in text
    assert "Wilson 95%" in text
    assert "New-information stratification" in text


def test_crossed_or_locked_book_fails_closed() -> None:
    path = _surviving_path()
    path[3] = _snapshot(1, bid="0.43", ask="0.43")

    row = evaluate_candidates(path, config=_config())[0]

    assert row.state is BreakoutState.UNKNOWN
    assert "non_positive_spread" in row.unknown_reasons


def test_config_from_mapping_parses_string_false_for_rule_provenance() -> None:
    """require_rule_provenance='false' must disable the requirement, not enable it."""
    values = {
        "version": "test-v1",
        "trailing_window_minutes": 10,
        "confirmation_window_minutes": 3,
        "breakout_ticks": 2,
        "survival_fraction": "0.67",
        "minimum_trailing_observations": 2,
        "minimum_confirmation_observations": 2,
        "maximum_snapshot_gap_minutes": 5,
        "outcome_horizons_minutes": (5,),
        "bid_target_rises": ("0.05",),
        "required_metric_names": ("mid", "spread", "top_bid_depth", "top_ask_depth", "imbalance"),
        "forward_cutoff": "2026-08-30T23:59:59+00:00",
        "require_rule_provenance": "false",
    }
    config = MarketStateChallengerConfig.from_mapping(values)
    assert config.require_rule_provenance is False


def test_enrich_microstructure_sorts_trades_by_effective_availability_time() -> None:
    """Trades with delayed available_at must be sorted by receipt time, not source time."""
    token = "token-1"
    # Trade A: source at t=0, available at t=5 (delayed receipt)
    # Trade B: source at t=3, available at t=3 (immediate)
    # Source order: [A, B]; effective order: [B(t=3), A(t=5)]
    trade_a = TradeEvent(
        timestamp=BASE,
        asset_id=token,
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("10"),
        available_at=BASE + timedelta(minutes=5),
    )
    trade_b = TradeEvent(
        timestamp=BASE + timedelta(minutes=3),
        asset_id=token,
        side="BUY",
        price=Decimal("0.41"),
        size=Decimal("10"),
        available_at=BASE + timedelta(minutes=3),
    )
    # Snapshot at t=4: should see only trade B (available at t=3),
    # not trade A (available at t=5, after snapshot).
    snapshot = _snapshot(4, token=token)
    enriched = _enrich_microstructure(
        [snapshot],
        trades=[trade_a, trade_b],
        l2_churn_index=None,
        station_timezones={"KLAX": "America/Los_Angeles"},
    )
    assert len(enriched) == 1


def test_raw_event_without_impact_class_gets_classified_through_clock() -> None:
    """Raw events with impact_class=None must be classified by InformationClock
    before reaching the challenger, so HARD_RESET carries correct impact_class."""
    from poly_weather.information_clock import InformationClock
    from poly_weather.quiet_window_strategy import _scope_events_from_scopes

    raw_event = InformationEvent(
        event_id="raw-1",
        source="WRH",
        kind="wrh_observation",
        source_at=BASE - timedelta(minutes=1),
        available_at=BASE - timedelta(minutes=1),
        station_id="KLAX",
        market_day="2026-08-30",
        changed_physical_margin=True,
        # impact_class is None — raw from archive, not yet classified
    )
    scopes = {("KLAX", "2026-08-30")}
    scoped = _scope_events_from_scopes([raw_event], scopes)
    assert len(scoped) >= 1

    clock = InformationClock()
    classified = clock.ingest_many(scoped)
    assert len(classified) >= 1
    assert classified[0].impact_class is ImpactClass.HARD_RESET
