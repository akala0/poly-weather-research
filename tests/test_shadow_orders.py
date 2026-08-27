from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poly_weather.shadow_orders import (
    CANDIDATE_INVENTORY_SCHEDULES,
    BookSnapshot,
    FillModel,
    QuoteMode,
    ReplenishMode,
    ShadowExitPolicy,
    ShadowOrderEngine,
    ShadowOrderRejected,
    ShadowOrderState,
    ShadowSide,
    TradeEvent,
    build_exit_plan,
    decide_replenish,
    evaluate_stop_loss,
    exit_action,
    inventory_risk_summary,
    maker_fee_usdc,
    quote_ladder,
    quote_limit,
    taker_fee_usdc,
    validate_inventory_schedule,
)

BASE = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def book(
    minute: int = 0,
    *,
    bids: tuple[tuple[str, str], ...] = (("0.70", "10"), ("0.69", "20")),
    asks: tuple[tuple[str, str], ...] = (("0.80", "100"), ("0.81", "100")),
    **kwargs: object,
) -> BookSnapshot:
    return BookSnapshot(
        timestamp=BASE + timedelta(minutes=minute),
        event_id="event-1",
        market_id="market-1",
        token_id="no-1",
        bids=bids,
        asks=asks,
        station_id="KLAX",
        market_day="2026-08-26",
        min_order_size=Decimal("5"),
        season_version="test-v1",
        **kwargs,
    )


def test_post_only_quote_modes_and_ladder_never_cross() -> None:
    snapshot = book()
    assert quote_limit(snapshot) == Decimal("0.70")
    assert quote_limit(snapshot, mode=QuoteMode.IMPROVE_ONE_TICK) == Decimal("0.71")
    assert quote_limit(book(asks=(("0.71", "100"),)), mode=QuoteMode.IMPROVE_ONE_TICK) is None
    assert quote_ladder(snapshot) == (Decimal("0.70"), Decimal("0.69"), Decimal("0.68"))


def test_tick_minimum_and_post_only_reject_fail_closed() -> None:
    engine = ShadowOrderEngine(budget_usd="200")
    with pytest.raises(ShadowOrderRejected) as tick:
        engine.submit_limit(book(), limit_price="0.705", size_usd="20")
    assert tick.value.reason.value == "invalid_tick"
    with pytest.raises(ShadowOrderRejected) as minimum:
        engine.submit_limit(book(), limit_price="0.70", shares="1")
    assert minimum.value.reason.value == "below_min_order_size"
    with pytest.raises(ShadowOrderRejected) as crossing:
        engine.submit_limit(book(), limit_price="0.80", size_usd="20")
    assert crossing.value.reason.value == "post_only_would_cross"
    with pytest.raises(ShadowOrderRejected) as season:
        engine.submit_limit(
            BookSnapshot(
                timestamp=BASE,
                event_id="event-1",
                market_id="market-1",
                token_id="no-1",
                bids=(("0.70", "10"),),
                asks=(("0.80", "100"),),
            ),
            limit_price="0.70",
            shares="5",
        )
    assert season.value.reason.value == "invalid_season_version"


def test_queue_ahead_consumes_only_matching_taker_side_and_partial_fills() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.QUEUE_AWARE, budget_usd="200")
    order = engine.submit_limit(book(), limit_price="0.70", shares="20")
    assert order.state is ShadowOrderState.RESTING
    assert order.better_level_shares == Decimal("0")
    assert order.volume_ahead == Decimal("10")

    # A BUY trade cannot consume a passive BUY order.  A same-side SELL first
    # consumes the ten shares ahead, then only the remainder reaches us.
    assert engine.process_trade(
        TradeEvent(BASE + timedelta(minutes=1), "no-1", ShadowSide.BUY, "0.70", "100")
    ) == ()
    fills = engine.process_trade(
        TradeEvent(BASE + timedelta(minutes=2), "no-1", ShadowSide.SELL, "0.70", "15", "t1")
    )
    assert len(fills) == 1
    assert fills[0].shares == Decimal("5")
    assert order.state is ShadowOrderState.PARTIALLY_FILLED
    assert order.remaining_shares == Decimal("15")
    assert engine.process_trade(
        TradeEvent(BASE + timedelta(minutes=2), "no-1", ShadowSide.SELL, "0.70", "15", "t1")
    ) == ()


def test_trade_through_requires_strict_cross_and_book_decrease_is_not_fill() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.TRADE_THROUGH, budget_usd="200")
    order = engine.submit_limit(book(), limit_price="0.70", shares="20")
    assert engine.process_snapshot(book(1, bids=(("0.70", "1"),), asks=(("0.80", "1"),))) == ()
    assert order.filled_shares == 0
    assert engine.process_trade(
        TradeEvent(BASE + timedelta(minutes=2), "no-1", ShadowSide.SELL, "0.70", "100", "touch")
    ) == ()
    fills = engine.process_trade(
        TradeEvent(BASE + timedelta(minutes=3), "no-1", ShadowSide.SELL, "0.69", "100", "cross")
    )
    assert fills[0].model is FillModel.TRADE_THROUGH
    assert order.state is ShadowOrderState.FILLED


def test_touch_is_explicit_optimistic_upper_bound() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.TOUCH, budget_usd="200")
    order = engine.submit_limit(book(), limit_price="0.70", shares="20")
    fills = engine.process_snapshot(
        book(1, bids=(("0.69", "10"),), asks=(("0.70", "1"),))
    )
    assert fills[0].model is FillModel.TOUCH
    assert fills[0].shares == Decimal("20")
    assert order.state is ShadowOrderState.FILLED
    assert engine.process_snapshot(
        book(1, bids=(("0.69", "10"),), asks=(("0.70", "1"),))
    ) == ()


def test_budget_counts_active_orders_and_filled_inventory_but_not_unfilled_as_inventory() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.QUEUE_AWARE, budget_usd="20")
    order = engine.submit_limit(book(), limit_price="0.70", size_usd="20")
    assert engine.inventory_shares == 0
    assert engine.active_reserved_usd == Decimal("20")
    with pytest.raises(ShadowOrderRejected) as rejected:
        engine.submit_limit(book(1), limit_price="0.69", size_usd="1", idempotency_key="second")
    assert rejected.value.reason.value == "active_order_limit"
    engine.cancel(order.order_id, timestamp=BASE + timedelta(minutes=1), reason="timeout")
    assert engine.inventory_shares == 0


def test_restart_and_idempotency_restore_order_and_inventory(tmp_path) -> None:
    path = tmp_path / "shadow_orders.jsonl"
    first = ShadowOrderEngine(
        ledger_path=path, fill_model=FillModel.QUEUE_AWARE, budget_usd="200"
    )
    order = first.submit_limit(book(), limit_price="0.70", shares="20", idempotency_key="same")
    duplicate = first.submit_limit(book(), limit_price="0.70", shares="20", idempotency_key="same")
    assert duplicate.order_id == order.order_id
    assert order.as_dict()["execution_enabled"] is False
    first.process_trade(
        TradeEvent(BASE + timedelta(minutes=1), "no-1", ShadowSide.SELL, "0.70", "30", "trade-1")
    )
    second = ShadowOrderEngine(
        ledger_path=path, fill_model=FillModel.QUEUE_AWARE, budget_usd="200"
    )
    assert second.orders[0].order_id == order.order_id
    assert second.inventory_shares == Decimal("20")
    assert second.submit_limit(book(), limit_price="0.70", shares="20", idempotency_key="same").order_id == order.order_id


def test_in_memory_idempotency_is_also_restart_replay_safe_within_process() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.QUEUE_AWARE, budget_usd="200")
    first = engine.submit_limit(book(), limit_price="0.70", shares="5", idempotency_key="local")
    second = engine.submit_limit(book(), limit_price="0.70", shares="5", idempotency_key="local")
    assert second is first


def test_same_second_unordered_trades_are_skipped_conservatively() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.TRADE_THROUGH, budget_usd="200")
    order = engine.submit_limit(book(), limit_price="0.70", shares="5")
    fills = engine.process_trades(
        [
            TradeEvent(BASE + timedelta(minutes=1), "no-1", ShadowSide.SELL, "0.69", "100"),
            TradeEvent(BASE + timedelta(minutes=1), "no-1", ShadowSide.SELL, "0.69", "100"),
        ]
    )
    assert fills == ()
    assert order.filled_shares == 0


def test_health_gates_cancel_active_and_reject_new_orders() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.QUEUE_AWARE, budget_usd="200")
    order = engine.submit_limit(book(), limit_price="0.70", shares="5")
    degraded = book(1, upstream_status="maintenance")
    assert engine.process_snapshot(degraded) == ()
    assert order.state is ShadowOrderState.CANCELLED
    with pytest.raises(ShadowOrderRejected) as rejected:
        engine.submit_limit(degraded, limit_price="0.70", shares="5")
    assert rejected.value.reason.value == "upstream_maintenance"


def test_requote_cancels_old_order_and_preserves_remaining() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.QUEUE_AWARE, budget_usd="200")
    old = engine.submit_limit(book(), limit_price="0.70", shares="10")
    new = engine.requote(old.order_id, book(1), limit_price="0.69")
    assert old.state is ShadowOrderState.CANCELLED
    assert new.state is ShadowOrderState.RESTING
    assert new.remaining_shares == Decimal("10")


def test_replenish_modes_do_not_allow_unconditional_averaging_down() -> None:
    kwargs = dict(
        current_ask="0.70",
        target_price="0.85",
        prior_order_resolved=True,
        budget_remaining_usd="100",
        season_version_valid=True,
    )
    assert not decide_replenish(mode=ReplenishMode.CONFIRMATION, **kwargs).allowed
    assert decide_replenish(mode=ReplenishMode.CONFIRMATION, weather_improving=True, **kwargs).allowed
    assert not decide_replenish(mode=ReplenishMode.CONDITIONAL_DIP, weather_worsening=True, **kwargs).allowed
    assert decide_replenish(mode=ReplenishMode.CONDITIONAL_DIP, weather_unchanged=True, **kwargs).allowed


def test_exit_plan_and_fee_safe_inventory_summary() -> None:
    plan = build_exit_plan(
        inventory_shares="100", average_cost="0.70", targets=("0.05", "0.10", "0.13")
    )
    assert plan[0].fraction == Decimal("0.5")
    assert plan[0].target_price == Decimal("0.75")
    assert [leg.target_price for leg in plan] == [Decimal("0.75"), Decimal("0.80"), Decimal("0.83")]
    assert sum((leg.fraction for leg in plan), start=Decimal("0")) == Decimal("1")
    engine = ShadowOrderEngine(budget_usd="200")
    assert inventory_risk_summary(engine)["execution_enabled"] is False
    assert maker_fee_usdc("10", "0.50") == Decimal("0")
    assert taker_fee_usdc("10", "0.50") == Decimal("0.125")
    assert validate_inventory_schedule(("20", "30", "50", "100"))[-1] == Decimal("100")
    assert sum(row.total_usd for row in CANDIDATE_INVENTORY_SCHEDULES if not row.control) >= Decimal("200")


def test_exit_action_is_maker_first_and_health_fail_closed() -> None:
    policy = ShadowExitPolicy(target_rises=(Decimal("0.05"),))
    assert exit_action(
        book(1, bids=(("0.76", "100"),), asks=(("0.80", "100"),)),
        average_cost="0.70",
        submitted_at=BASE,
        policy=policy,
    ) == "target"
    assert exit_action(
        book(1, upstream_status="maintenance"),
        average_cost="0.70",
        submitted_at=BASE,
        policy=policy,
    ) == "stale_or_health_risk"


def test_stop_loss_reports_touch_not_guaranteed_execution() -> None:
    result = evaluate_stop_loss(
        entry_price="0.80",
        stop_price="0.76",
        snapshots=[book(1, bids=(("0.75", "100"),), asks=(("0.80", "100"),))],
        token_id="no-1",
        entry_time=BASE,
    )
    assert result.touched is True
    assert result.first_post_gap_bid == Decimal("0.75")
    assert result.note.startswith("Observed bids")


def test_emergency_taker_exit_uses_real_bids_and_official_fee() -> None:
    engine = ShadowOrderEngine(fill_model=FillModel.TOUCH, budget_usd="200")
    engine.submit_limit(book(), limit_price="0.70", shares="10")
    engine.process_snapshot(book(1, bids=(("0.69", "100"),), asks=(("0.70", "100"),)))
    degraded = book(2, bids=(("0.60", "100"),), asks=(("0.80", "100"),), market_stale=True)
    fills = engine.simulate_taker_exit(degraded)
    assert fills[0].price == Decimal("0.60")
    assert fills[0].fee_usd > 0
    assert engine.inventory_shares == 0
