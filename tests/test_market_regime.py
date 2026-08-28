from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.information_clock import InformationEvent
from poly_weather.market_regime import (
    MarketMetrics,
    MarketRegimeStateMachine,
    RegimeState,
    RegimeThresholds,
    derive_predictable_release,
)

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def _metric(minutes: int, *, token_id: str = "token-1") -> MarketMetrics:
    return MarketMetrics(
        timestamp=BASE + timedelta(minutes=minutes),
        station_id="KLAX",
        market_day="2026-08-28",
        token_id=token_id,
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
        churn_rate=Decimal("0"),
        cross_bucket_mass_error=Decimal("0"),
    )


def _event(event_id: str, minute: int = 0) -> InformationEvent:
    at = BASE + timedelta(minutes=minute)
    return InformationEvent(
        event_id=event_id,
        source="WRH",
        kind="wrh_observation",
        source_at=at,
        available_at=at,
        station_id="KLAX",
        market_day="2026-08-28",
        metadata={"revision": event_id},
    )


def _machine(*, stable_windows: int = 2) -> MarketRegimeStateMachine:
    thresholds = RegimeThresholds(
        stable_windows=stable_windows,
        minimum_observation_spacing=timedelta(minutes=1),
        reaction_cooldown=timedelta(0),
    )
    return MarketRegimeStateMachine(
        station_id="KLAX",
        market_day="2026-08-28",
        thresholds=thresholds,
    )


def test_new_information_resets_event_and_duplicate_payload_does_not() -> None:
    machine = _machine()
    first = machine.observe(_metric(0), information_event=_event("one"))
    assert first.state is RegimeState.EVENT
    assert first.cancel_required is True
    machine.observe(_metric(1))
    duplicate = machine.observe(_metric(2), information_event=_event("one"))
    assert duplicate.state is RegimeState.QUIET
    assert duplicate.event_id is None
    revision = machine.observe(_metric(3), information_event=_event("two", minute=0))
    assert revision.state is RegimeState.EVENT
    assert revision.event_id == "two"


def test_reaction_end_is_recorded_once_and_quiet_persists() -> None:
    machine = _machine(stable_windows=2)
    machine.observe(_metric(0), information_event=_event("one"))
    machine.observe(_metric(1))
    quiet = machine.observe(_metric(2))
    assert quiet.state is RegimeState.QUIET
    assert quiet.reaction_duration_seconds == 120
    continued = machine.observe(_metric(3))
    assert continued.state is RegimeState.QUIET
    assert continued.reason == "quiet_window_stable"
    assert machine.reaction_durations_seconds == [120]


def test_pre_release_and_health_failure_are_fail_closed() -> None:
    machine = _machine(stable_windows=1)
    machine.observe(_metric(0), information_event=_event("one"))
    pre_release = machine.observe(
        _metric(1),
        next_release_at=BASE + timedelta(minutes=4),
        schedule_verified=True,
    )
    assert pre_release.state is RegimeState.PRE_RELEASE
    assert pre_release.cancel_required is True
    halted = machine.observe(_metric(2), health_ok=False, health_reason="maintenance_gap")
    assert halted.state is RegimeState.HALTED
    assert halted.reduce_only is True
    assert machine.allow_new_orders is False


def test_predictable_release_uses_adjacent_intervals_without_lookahead() -> None:
    events = tuple(_event(f"event-{minute}", minute) for minute in (0, 5, 10))
    release = derive_predictable_release(
        events,
        now=BASE + timedelta(minutes=11),
        scope=("KLAX", "2026-08-28"),
    )
    assert release is not None
    assert release.release_at == BASE + timedelta(minutes=15)
