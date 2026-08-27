"""Forensic audit for shadow orders that never touched their limit.

This is intentionally separate from the fill model.  A zero-fill result can
mean a genuine absence of a matching quote, a routing/token issue, or a timeout
before the market returned.  The audit only scans snapshots strictly after the
submit timestamp and therefore cannot introduce look-ahead.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from poly_weather.shadow_orders import BookSnapshot, ShadowOrder, ShadowSide


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _levels(snapshot: BookSnapshot, side: ShadowSide) -> tuple[tuple[Decimal, Decimal], ...]:
    return snapshot.asks if side is ShadowSide.BUY else snapshot.bids


def _best(snapshot: BookSnapshot, side: ShadowSide) -> Decimal | None:
    return snapshot.best_ask if side is ShadowSide.BUY else snapshot.best_bid


def _key(snapshot: BookSnapshot) -> tuple[str, str, str]:
    return snapshot.event_id, snapshot.market_id, snapshot.token_id


def audit_shadow_order(
    order: ShadowOrder,
    snapshots: Sequence[BookSnapshot],
    *,
    timeout: timedelta | None = None,
) -> dict[str, Any]:
    """Explain one order's quote path using only later same-token snapshots."""
    matching = [
        row
        for row in snapshots
        if row.event_id == order.event_id
        and row.market_id == order.market_id
        and row.token_id == order.token_id
    ]
    matching.sort(key=lambda row: row.timestamp)
    submit_candidates = [row for row in matching if row.timestamp <= order.submitted_at]
    submit = submit_candidates[-1] if submit_candidates else None
    lifecycle_end = order.last_fill_at or order.cancelled_at or order.expired_at
    if lifecycle_end is None:
        lifecycle_end = matching[-1].timestamp if matching else order.submitted_at
    later = [
        row
        for row in matching
        if order.submitted_at < row.timestamp <= lifecycle_end
    ]
    # A snapshot that caused the timeout is still evidence about the quote at
    # that instant; it is included by the <= lifecycle boundary above.
    values = [_best(row, order.side) for row in later]
    valid_values = [value for value in values if value is not None]
    touched = any(
        value is not None
        and (value <= order.limit_price if order.side is ShadowSide.BUY else value >= order.limit_price)
        for value in values
    )
    nearest_distance = None
    if valid_values:
        nearest_distance = min(abs(value - order.limit_price) for value in valid_values)
    min_executable = min(valid_values, default=None) if order.side is ShadowSide.BUY else max(valid_values, default=None)
    max_executable = max(valid_values, default=None) if order.side is ShadowSide.BUY else min(valid_values, default=None)
    submit_best = _best(submit, order.side) if submit is not None else None
    timeout_value = timeout or timedelta(0)
    timeout_deadline = order.submitted_at + timeout_value if timeout_value > timedelta(0) else None
    timed_out_before_touch = (
        not touched
        and order.cancel_reason == "timeout"
        and (timeout_deadline is None or (order.expired_at or lifecycle_end) >= timeout_deadline)
    )
    all_later_quotes = bool(valid_values)
    if order.filled_shares > Decimal("0"):
        reason = "filled"
    elif not matching:
        reason = "snapshot_routing_or_token_error"
    elif not all_later_quotes:
        reason = "no_later_quote"
    elif touched:
        reason = "touched_but_not_filled"
    elif timed_out_before_touch:
        reason = "timeout_before_touch"
    elif order.side is ShadowSide.BUY and submit_best is not None and all(
        value > order.limit_price for value in valid_values
    ):
        reason = "market_never_returned_to_buy_limit"
    elif order.side is ShadowSide.SELL and submit_best is not None and all(
        value < order.limit_price for value in valid_values
    ):
        reason = "market_never_reached_sell_limit"
    else:
        reason = "never_touched"
    return {
        "order_id": order.order_id,
        "event_id": order.event_id,
        "market_id": order.market_id,
        "market_day": order.market_day,
        "token_id": order.token_id,
        "side": str(order.side),
        "submitted_at": order.submitted_at.isoformat(),
        "limit_price": str(order.limit_price),
        "state": str(order.state),
        "cancel_reason": order.cancel_reason,
        "submit_best_bid": str(submit.best_bid) if submit and submit.best_bid is not None else None,
        "submit_best_ask": str(submit.best_ask) if submit and submit.best_ask is not None else None,
        "tick_size": str(order.tick_size),
        "volume_ahead": str(order.volume_ahead),
        "better_level_shares": str(order.better_level_shares),
        "later_snapshot_count": len(later),
        "later_quote_count": len(valid_values),
        "lifecycle_end": lifecycle_end.isoformat(),
        "timeout_seconds": int(timeout_value.total_seconds()) if timeout_value else None,
        "minimum_later_ask_or_max_bid": str(min_executable) if min_executable is not None else None,
        "maximum_later_ask_or_min_bid": str(max_executable) if max_executable is not None else None,
        "nearest_distance_to_limit": str(nearest_distance) if nearest_distance is not None else None,
        "touched": touched,
        "timed_out_before_touch": timed_out_before_touch,
        "reason": reason,
        "execution_enabled": False,
    }


def audit_touch_zero_orders(
    orders: Iterable[ShadowOrder | Mapping[str, Any]],
    snapshots: Sequence[BookSnapshot],
    *,
    timeout: timedelta = timedelta(minutes=15),
) -> dict[str, Any]:
    """Return order-level evidence and reason counts for zero-touch orders."""
    parsed: list[ShadowOrder] = []
    for value in orders:
        if isinstance(value, ShadowOrder):
            parsed.append(value)
        else:
            parsed.append(ShadowOrder.from_dict(value))
    rows = [audit_shadow_order(order, snapshots, timeout=timeout) for order in parsed]
    reasons: dict[str, int] = {}
    for row in rows:
        reason = str(row["reason"])
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "order_count": len(rows),
        "touch_count": sum(bool(row["touched"]) for row in rows),
        "zero_touch_count": sum(not bool(row["touched"]) for row in rows),
        "reason_counts": reasons,
        "orders": rows,
        "timeout_seconds": int(timeout.total_seconds()),
        "execution_enabled": False,
    }


def timeout_sensitivity(
    orders_by_timeout: Mapping[int | str, Sequence[ShadowOrder | Mapping[str, Any]]],
    snapshots: Sequence[BookSnapshot],
) -> dict[str, Any]:
    """Summarize only the predeclared timeout grid (5/15/30/60 minutes)."""
    output: dict[str, Any] = {}
    for timeout_minutes in (5, 15, 30, 60):
        orders = orders_by_timeout.get(timeout_minutes) or orders_by_timeout.get(str(timeout_minutes)) or ()
        output[str(timeout_minutes)] = audit_touch_zero_orders(
            orders, snapshots, timeout=timedelta(minutes=timeout_minutes)
        )
    return output


__all__ = [
    "audit_shadow_order",
    "audit_touch_zero_orders",
    "timeout_sensitivity",
]
