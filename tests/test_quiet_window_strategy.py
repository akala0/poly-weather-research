from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poly_weather.information_clock import ImpactClass, InformationEvent
from poly_weather.market_regime import RegimeState, RegimeThresholds
from poly_weather.quiet_window_strategy import (
    QuietWindowConfig,
    QuietWindowEngine,
    ThreeStrategyRiskBook,
    _scope_events_from_scopes,
    compute_decision_regret,
    derive_size_grid_from_no_quiet_profile,
    replay_quiet_window,
    replay_quiet_window_size_grid,
)
from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    ShadowPortfolioInvariantError,
    TradeEvent,
)

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def _snapshot(
    minute: int,
    *,
    bid: str = "0.40",
    ask: str = "0.45",
    weather_stale: bool = False,
    metadata: dict[str, object] | None = None,
) -> BookSnapshot:
    return BookSnapshot(
        timestamp=BASE + timedelta(minutes=minute),
        event_id="event-1",
        market_id="market-1",
        token_id="token-1",
        bids=((Decimal(bid), Decimal("100")),),
        asks=((Decimal(ask), Decimal("100")),),
        station_id="KLAX",
        market_day="2026-08-28",
        tick_size=Decimal("0.01"),
        season_version="heat_2026_v2",
        weather_stale=weather_stale,
        metadata={
            "trade_intensity_multiple": "1",
            "churn_rate": "0",
            "cross_bucket_mass_error": "0",
            **(metadata or {}),
        },
    )


def _event(event_id: str = "weather-1") -> InformationEvent:
    return InformationEvent(
        event_id=event_id,
        source="WRH",
        kind="wrh_observation",
        source_at=BASE,
        available_at=BASE,
        station_id="KLAX",
        market_day="2026-08-28",
        metadata={"revision": event_id},
    )


def _thresholds() -> RegimeThresholds:
    return RegimeThresholds(
        stable_windows=1,
        minimum_observation_spacing=timedelta(minutes=1),
        reaction_cooldown=timedelta(0),
        require_cross_bucket_quality=True,
    )


def test_quiet_maker_enters_only_after_reaction_and_new_event_cancels_quote() -> None:
    strategy = QuietWindowConfig(fill_model=FillModel.TOUCH)
    engine = QuietWindowEngine(config=strategy, thresholds=_thresholds())
    first = engine.process_snapshot(_snapshot(0), information_events=(_event(),))
    assert first.state is RegimeState.EVENT
    assert engine.engines[next(iter(engine.engines))].orders == ()
    quiet = engine.process_snapshot(_snapshot(1))
    assert quiet.state is RegimeState.QUIET
    portfolio = next(iter(engine.engines.values()))
    assert len(portfolio.active_orders) == 1
    reset = engine.process_snapshot(_snapshot(2), information_events=(_event("weather-2"),))
    assert reset.state is RegimeState.EVENT
    assert not portfolio.active_orders
    assert all(order.side.value == "BUY" for order in portfolio.orders)


def test_transition_observer_receives_existing_token_queue_without_mutating_strategy() -> None:
    observed: list[dict[str, object]] = []

    def capture(snapshot, _machine, _transition) -> None:
        observed.append(dict(snapshot.metadata))

    engine = QuietWindowEngine(
        config=QuietWindowConfig(fill_model=FillModel.QUEUE_AWARE),
        thresholds=_thresholds(),
        transition_observer=capture,
    )
    engine.process_snapshot(_snapshot(0), information_events=(_event(),))
    engine.process_snapshot(_snapshot(1))
    engine.process_snapshot(_snapshot(2))

    queues = observed[-1]["quiet_state_order_queue"]
    assert isinstance(queues, list)
    assert len(queues) == 1
    assert queues[0]["volume_ahead"] >= 0
    portfolio = next(iter(engine.engines.values()))
    assert len(portfolio.active_orders) == 1


def test_health_gap_stops_everything_and_uses_only_real_bid_depth_for_reduce_only_exit() -> None:
    engine = QuietWindowEngine(
        config=QuietWindowConfig(fill_model=FillModel.QUEUE_AWARE),
        thresholds=_thresholds(),
    )
    engine.process_snapshot(_snapshot(0), information_events=(_event(),))
    engine.process_snapshot(_snapshot(1))
    # A real opposite-side trade consumes the recorded queue ahead.  No book
    # touch or complement price is used as a fill proxy.
    engine.process_trade(
        TradeEvent(
            timestamp=BASE + timedelta(minutes=2),
            asset_id="token-1",
            side="SELL",
            price=Decimal("0.40"),
            size=Decimal("200"),
            event_id="trade-1",
        )
    )
    engine.process_snapshot(_snapshot(2))
    portfolio = next(iter(engine.engines.values()))
    assert portfolio.inventory_shares > 0
    halted = engine.process_snapshot(
        _snapshot(3, bid="0.39", ask="0.45", weather_stale=True)
    )
    assert halted.action == "HALTED"
    assert engine.halted is True
    assert not portfolio.active_orders
    assert any(
        fill.source == "emergency_taker_depth"
        for order in portfolio.orders
        for fill in order.fills
    )


def test_replay_keeps_fixed_thresholds_and_marks_small_clusters_unreliable() -> None:
    rows = [_snapshot(index) for index in range(4)]
    result = replay_quiet_window(rows, information_events=[_event()])
    assert set(result["models"]) == {"strict", "neutral", "lenient"}
    assert result["no_parameter_search"] is True
    assert result["models"]["neutral"]["causal_metrics"]["statistical_gate"][
        "all_rates_have_wilson_95"
    ] is True
    assert result["models"]["neutral"]["reaction_duration"]["statistically_unreliable"] is True


def test_three_strategy_risk_book_rejects_shared_cap_overrun() -> None:
    risk = ThreeStrategyRiskBook()
    for family in risk.registrations:
        risk.update(
            family,
            "KLAX",
            "2026-08-28",
            inventory_cost_usd="0" if family != "weather_lead_lag_maker" else "100",
            active_reserved_usd="0",
            cumulative_buy_cost_usd="1" if family == "complement_pair_maker" else "100",
            portfolio_count=1,
        )
    with pytest.raises(ShadowPortfolioInvariantError, match="station_day_cap_exceeded"):
        risk.assert_station_day("KLAX", "2026-08-28")


def test_decision_regret_is_after_the_fact_only() -> None:
    engine = QuietWindowEngine(config=QuietWindowConfig(), thresholds=_thresholds())
    decision = engine.process_snapshot(_snapshot(0), information_events=(_event(),))
    rows = [_snapshot(0), _snapshot(5, bid="0.50", ask="0.55")]
    regrets = compute_decision_regret(
        [decision],
        rows,
        [
            replace(
                _event("future"),
                source_at=BASE + timedelta(minutes=1),
                available_at=BASE + timedelta(minutes=1),
            )
        ],
    )
    assert len(regrets) == 1
    assert regrets[0].lookahead_only is True
    assert regrets[0].no_cancel_best_bid_change == 0.10
    assert regrets[0].next_event_gap == 60


def test_replay_with_quiet_quote_and_later_event_records_regret_gap() -> None:
    first = replace(_event("initial"), impact_class=ImpactClass.HARD_RESET)
    later = replace(
        _event("later"),
        source_at=BASE + timedelta(minutes=7),
        available_at=BASE + timedelta(minutes=7),
        impact_class=ImpactClass.HARD_RESET,
    )
    result = replay_quiet_window(
        [_snapshot(minute) for minute in range(8)],
        information_events=[first, later],
    )
    profile = result["models"]["neutral"]
    assert profile["state_observation_counts"]["QUIET"] >= 1
    regrets = profile["causal_metrics"]["decision_regret"]
    assert regrets["count"] >= 1
    assert any(row["next_event_gap"] == 60 for row in regrets["rows"])
    size_grid = replay_quiet_window_size_grid(
        [_snapshot(minute) for minute in range(8)],
        information_events=[first, later],
    )
    assert set(size_grid["grid"]) == {"20", "50", "100", "200"}
    assert all(
        row["causal_metrics"]["decision_regret"]["count"] >= 1
        for row in size_grid["grid"].values()
    )


def test_global_status_is_expanded_to_observed_station_days() -> None:
    status = InformationEvent(
        event_id="status-1",
        source="polymarket_status",
        kind="official_status",
        source_at=BASE,
        available_at=BASE,
    )
    scoped = _scope_events_from_scopes(
        (status,),
        (("KLAX", "2026-08-28"), ("KLGA", "2026-08-28")),
    )
    assert {(event.station_id, event.market_day) for event in scoped} == {
        ("KLAX", "2026-08-28"),
        ("KLGA", "2026-08-28"),
    }


def test_no_quiet_size_grid_shortcut_is_not_a_parameter_search() -> None:
    profile = {
        "raw_snapshot_count": 12,
        "state_observation_counts": {"EVENT": 12},
        "halted": False,
        "risk": {},
        "causal_metrics": {},
    }
    result = derive_size_grid_from_no_quiet_profile(profile, QuietWindowConfig())
    assert result["replay_source"] == "exact_no_quiet_entry_gate_short_circuit"
    assert result["no_parameter_search"] is True
    assert all(row["short_circuited_no_quiet"] for row in result["grid"].values())
