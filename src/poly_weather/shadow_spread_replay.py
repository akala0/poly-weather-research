"""Event-driven replay for the read-only maker shadow strategy.

Each token has a separate finite state machine.  A station/market-day remains
one correlated statistical and risk cluster, but it never owns inventory or
supplies a cost basis to a different temperature bucket.  The replay consumes
true NO bid/ask ladders and (when available) public taker trades; it never
turns a trade price, midpoint or YES complement into a quote.  The result is a
simulator diagnostic, not an executable-strategy claim.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from poly_weather.no_forward import wilson_interval
from poly_weather.shadow_orders import (
    CANDIDATE_INVENTORY_SCHEDULES,
    BookSnapshot,
    FillModel,
    QuoteMode,
    ReplenishMode,
    ShadowOrder,
    ShadowOrderEngine,
    ShadowOrderState,
    ShadowPortfolioInvariantError,
    ShadowSide,
    ShadowStrategyConfig,
    StationDayBudgetMode,
    StationDayRiskManager,
    TokenPortfolioKey,
    TradeEvent,
    quote_limit,
)
from poly_weather.shadow_touch_audit import audit_touch_zero_orders

ZERO = Decimal("0")
DEFAULT_ENTRY_BANDS: dict[str, tuple[tuple[Decimal, Decimal], ...]] = {
    "KLAX": ((Decimal("0.70"), Decimal("0.85")),),
    "KLGA": ((Decimal("0.50"), Decimal("0.70")),),
}


def default_shadow_strategy_config() -> ShadowStrategyConfig:
    """Return the fixed candidate family used for diagnostics.

    These are configuration defaults from the observed candidate bands, not
    values searched by the replay.  Callers can provide another versioned
    config explicitly.
    """
    return ShadowStrategyConfig(
        version="shadow-spread-v2-token-scoped",
        quote_mode=QuoteMode.BEST_BID,
        fill_model=FillModel.QUEUE_AWARE,
        entry_bands=DEFAULT_ENTRY_BANDS,
        target_rise=Decimal("0.10"),
        order_timeout=timedelta(minutes=15),
        max_active_orders=1,
        market_budget_usd=Decimal("200"),
        station_day_budget_mode=StationDayBudgetMode.CUMULATIVE_BUY_COST,
        tranche_usd=(Decimal("50"),),
        replenish_mode=ReplenishMode.NONE,
    )


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
    except (ArithmeticError, ValueError):
        return None


def _flag(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _rate(successes: int, total: int) -> dict[str, Any]:
    interval = wilson_interval(successes, total)
    return {
        "count": successes,
        "sample_count": total,
        "rate": successes / total if total else None,
        "wilson_low": interval[0] if interval else None,
        "wilson_high": interval[1] if interval else None,
        "statistically_unreliable": total < 30,
    }


def _summary(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"sample_count": 0, "p50": None, "p90": None}
    def position(quantile: float) -> float:
        return quantile * (len(ordered) - 1)

    def percentile(quantile: float) -> float:
        point = position(quantile)
        lower = int(point)
        upper = min(len(ordered) - 1, lower + 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (point - lower)
    return {"sample_count": len(ordered), "p50": median(ordered), "p90": percentile(0.9)}


def _band_match(price: Decimal | None, bands: Sequence[tuple[Decimal, Decimal]]) -> bool:
    return price is not None and any(lower <= price < upper for lower, upper in bands)


def _row_snapshot(
    row: Mapping[str, Any],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> BookSnapshot:
    """Convert a paired archive row to a token-native NO snapshot."""
    no = row.get("no") if isinstance(row.get("no"), Mapping) else row
    event_id = str(row.get("event_id") or row.get("event_slug") or "")
    market_id = str(row.get("market_id") or row.get("market_slug") or "")
    token_id = str(no.get("asset_id") or row.get("token_id") or "")
    timestamp_value = no.get("_timestamp") or row.get("observed_at") or row.get("timestamp")
    if timestamp_value is None:
        raise ValueError("paired row has no timestamp")
    metadata: dict[str, Any] = {}
    if isinstance(row.get("metadata"), Mapping):
        metadata.update(row["metadata"])
    if isinstance(no.get("rule_provenance"), Mapping):
        # This comes from the same archived receipt as the book.  It is not
        # current CLOB metadata and may deliberately say UNKNOWN.
        metadata["rule_provenance"] = dict(no["rule_provenance"])
    elif isinstance(row.get("rule_provenance"), Mapping):
        metadata["rule_provenance"] = dict(row["rule_provenance"])
    event_value = (event_metadata or {}).get(str(row.get("event_slug") or ""), {})
    station = str(row.get("station_id") or event_value.get("station_id") or "") or None
    market_day = str(row.get("market_day") or event_value.get("target_date") or "") or None
    for key in (
        "physical_margin_f",
        "minutes_to_peak",
        "weather_improving",
        "weather_unchanged",
        "weather_worsening",
        "weather_market_lag",
        "peak_phase",
    ):
        if key in row:
            metadata[key] = row[key]
    return BookSnapshot(
        timestamp=_utc(timestamp_value),
        event_id=event_id,
        market_id=market_id,
        token_id=token_id,
        bids=no.get("bids") if isinstance(no, Mapping) else (),
        asks=no.get("asks") if isinstance(no, Mapping) else (),
        station_id=station,
        market_day=market_day,
        tick_size=_decimal(no.get("tick_size") or row.get("tick_size") or "0.01") or Decimal("0.01"),
        min_order_size=(
            _decimal(no.get("min_order_size") or row.get("min_order_size") or "0") or ZERO
        ),
        book_complete=_flag(no.get("book_complete", row.get("book_complete", True)), default=True),
        market_stale=_flag(row.get("market_stale", False)),
        weather_stale=_flag(row.get("weather_stale", False)),
        settlement_verified=_flag(row.get("settlement_verified", True), default=True),
        in_season=_flag(row.get("in_season", True), default=True),
        season_version=(
            str(row["season_version"]) if row.get("season_version") is not None else "unknown"
        ),
        warming_valid=_flag(row.get("warming_valid", True), default=True),
        upstream_status=str(row.get("upstream_status") or "normal"),
        quality_excluded=_flag(row.get("quality_excluded", False)),
        metadata=metadata,
    )


def _trade_value(value: Any) -> TradeEvent:
    if isinstance(value, TradeEvent):
        return value
    if hasattr(value, "asset_id"):
        timestamp = value.timestamp
        return TradeEvent(
            timestamp=_utc(timestamp),
            asset_id=str(value.asset_id),
            side=str(value.side),
            price=Decimal(str(value.price)),
            size=Decimal(str(value.size)),
            event_id=(str(value.transaction_hash) if hasattr(value, "transaction_hash") else "") or None,
            source=str(getattr(value, "source", "data_api")),
            sequence=(int(value.sequence) if getattr(value, "sequence", None) is not None else None),
            available_at=(
                _utc(value.available_at)
                if getattr(value, "available_at", None) is not None
                else None
            ),
        )
    if isinstance(value, Mapping):
        return TradeEvent.from_mapping(value)
    raise TypeError(f"unsupported trade value {type(value)!r}")


def _normalise_trades(
    trades: Mapping[str, Sequence[Any]] | Sequence[Any] | None,
) -> tuple[TradeEvent, ...]:
    if trades is None:
        return ()
    values: list[Any] = []
    if isinstance(trades, Mapping):
        for rows in trades.values():
            values.extend(rows)
    else:
        values.extend(trades)
    output: dict[tuple[str, datetime, str | None, Decimal, Decimal], TradeEvent] = {}
    for value in values:
        try:
            trade = _trade_value(value)
        except (TypeError, ValueError, KeyError):
            continue
        key = (trade.asset_id, trade.timestamp, trade.event_id, trade.price, trade.size)
        output[key] = trade
    return tuple(sorted(output.values(), key=lambda row: (row.timestamp, row.sequence is None, row.sequence or 0)))


def _day_key(snapshot: BookSnapshot) -> tuple[str, str]:
    """Return the correlated station-day cluster, never an inventory key."""
    return (snapshot.station_id or "unknown", snapshot.market_day or snapshot.timestamp.date().isoformat())


@dataclass
class _PortfolioReplayState:
    """Replay-only control state around one token-scoped order engine."""

    engine: ShadowOrderEngine
    raw_snapshot_count: int = 0
    next_tranche: int = 0
    entry_order_count: int = 0
    missed_opportunities: int = 0
    last_healthy: BookSnapshot | None = None


def _portfolio_fill_audit(state: _PortfolioReplayState) -> list[dict[str, Any]]:
    """Return token-native fill provenance, including later same-token exits."""
    rows = [
        (order, fill)
        for order in state.engine.orders
        for fill in order.fills
    ]
    rows.sort(key=lambda item: (item[1].timestamp, item[0].order_id, item[1].fill_id))
    output: list[dict[str, Any]] = []
    for order, fill in rows:
        later_exits = [
            {
                "order_id": later_order.order_id,
                "timestamp": later_fill.timestamp.isoformat(),
                "shares": float(later_fill.shares),
                "price": float(later_fill.price),
                "source": later_fill.source,
                "trigger_reason": later_order.trigger_reason,
            }
            for later_order, later_fill in rows
            if order.side is ShadowSide.BUY
            and later_order.side is ShadowSide.SELL
            and later_fill.timestamp > fill.timestamp
        ]
        output.append(
            {
                "portfolio_key": state.engine.portfolio_key.as_dict()
                if state.engine.portfolio_key
                else None,
                "order_id": order.order_id,
                "token_id": order.token_id,
                "side": str(order.side),
                "timestamp": fill.timestamp.isoformat(),
                "shares": float(fill.shares),
                "price": float(fill.price),
                "fee_usd": float(fill.fee_usd),
                "source": fill.source,
                "trigger_reason": order.trigger_reason,
                "maker_assumption": order.maker_assumption,
                "later_same_token_exits": later_exits,
            }
        )
    return output


def _portfolio_summary(
    state: _PortfolioReplayState,
    *,
    station_id: str,
    market_day: str,
) -> dict[str, Any]:
    engine = state.engine
    orders = list(engine.orders)
    fills = [fill for order in orders for fill in order.fills]
    maker_fees = sum(
        (fill.fee_usd for order in orders if order.maker_assumption for fill in order.fills),
        start=ZERO,
    )
    taker_fees = sum(
        (fill.fee_usd for order in orders if not order.maker_assumption for fill in order.fills),
        start=ZERO,
    )
    return {
        "portfolio_key": engine.portfolio_key.as_dict() if engine.portfolio_key else None,
        "station_id": station_id,
        "market_day": market_day,
        "raw_snapshot_count": state.raw_snapshot_count,
        "shadow_order_count": len(orders),
        "fill_count": len(fills),
        "entry_order_count": state.entry_order_count,
        "missed_opportunities": state.missed_opportunities,
        "inventory_at_end_shares": float(engine.inventory_shares),
        "inventory_cost_at_end_usd": float(engine.inventory_cost_usd),
        "cumulative_buy_cost_usd": float(engine.cumulative_buy_cost_usd),
        "active_reserved_usd": float(engine.active_reserved_usd),
        "realized_pnl_usd": float(engine.realized_pnl_usd),
        "unrealized_pnl_usd": None,
        "unrealized_pnl_status": (
            "N/A: all inventory was closed with token-native bid depth"
            if engine.inventory_shares == ZERO
            else "N/A: remaining token inventory is not valued with a quote proxy"
        ),
        "emergency_taker_exit_pnl_usd": float(
            engine.realized_pnl_by_exit_source.get("emergency_taker_depth", ZERO)
        ),
        "day_end_taker_exit_pnl_usd": float(
            engine.realized_pnl_by_exit_reason.get("local_day_end_risk_exit", ZERO)
        ),
        "realized_pnl_by_exit_source": {
            source: float(value)
            for source, value in sorted(engine.realized_pnl_by_exit_source.items())
        },
        "realized_pnl_by_exit_reason": {
            reason: float(value)
            for reason, value in sorted(engine.realized_pnl_by_exit_reason.items())
        },
        "fees_usd": float(engine.fees_usd),
        "maker_fees_usd": float(maker_fees),
        "taker_fees_usd": float(taker_fees),
        "round_trips": engine.round_trip_count,
        "orders": [order.as_dict() for order in orders],
        "fill_audit": _portfolio_fill_audit(state),
    }


def _run_model(
    snapshots: Sequence[BookSnapshot],
    trades: Sequence[TradeEvent],
    *,
    config: ShadowStrategyConfig,
    model: FillModel,
    global_capital_usd: Decimal,
) -> dict[str, Any]:
    """Replay token portfolios while clustering statistics and risk by day.

    The former implementation created one engine per ``(station, market_day)``.
    This implementation deliberately has a separate ``ShadowOrderEngine`` for
    every ``TokenPortfolioKey``.  ``StationDayRiskManager`` only aggregates
    budget/exposure and is incapable of routing a fill or holding inventory.
    """
    grouped: dict[tuple[str, str], list[BookSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[_day_key(snapshot)].append(snapshot)
    trades_by_day: dict[tuple[str, str], list[TradeEvent]] = defaultdict(list)
    token_days = {
        (snapshot.token_id, _day_key(snapshot)) for snapshot in snapshots
    }
    for trade in trades:
        for token_id, day in token_days:
            if token_id == trade.asset_id:
                trades_by_day[day].append(trade)
    day_summaries: list[dict[str, Any]] = []
    all_orders: list[ShadowOrder] = []
    all_fills: list[Any] = []
    portfolio_summaries: list[dict[str, Any]] = []
    all_fill_audit: list[dict[str, Any]] = []
    discrepancies: list[dict[str, Any]] = []
    all_snapshots_by_token: dict[str, list[BookSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        all_snapshots_by_token[snapshot.token_id].append(snapshot)
    missed_opportunities = 0
    trigger_candidates = 0
    rejection_reasons: dict[str, int] = defaultdict(int)
    global_peak_risk = ZERO
    global_capital = Decimal(str(global_capital_usd))
    for day, day_rows in sorted(grouped.items()):
        day_rows = sorted(day_rows, key=lambda row: row.timestamp)
        day_trades = sorted(trades_by_day.get(day, ()), key=lambda row: row.timestamp)
        risk_manager = StationDayRiskManager(
            station_id=day[0],
            market_day=day[1],
            budget_usd=config.market_budget_usd,
            budget_mode=config.station_day_budget_mode,
        )
        portfolios: dict[TokenPortfolioKey, _PortfolioReplayState] = {}
        portfolios_by_token: dict[str, _PortfolioReplayState] = {}

        def engines(
            portfolios: Mapping[TokenPortfolioKey, _PortfolioReplayState] = portfolios,
        ) -> tuple[ShadowOrderEngine, ...]:
            return tuple(state.engine for state in portfolios.values())

        def state_for(
            snapshot: BookSnapshot,
            portfolios: dict[TokenPortfolioKey, _PortfolioReplayState] = portfolios,
            portfolios_by_token: dict[str, _PortfolioReplayState] = portfolios_by_token,
            day: tuple[str, str] = day,
        ) -> _PortfolioReplayState:
            key = snapshot.portfolio_key
            state = portfolios.get(key)
            if state is None:
                state = _PortfolioReplayState(
                    engine=ShadowOrderEngine(
                        budget_usd=config.market_budget_usd,
                        max_active_orders=config.max_active_orders,
                        fill_model=model,
                        order_timeout=config.order_timeout,
                        portfolio_key=key,
                        budget_mode=config.station_day_budget_mode,
                        require_season_version=config.require_season_version,
                    )
                )
                portfolios[key] = state
                existing = portfolios_by_token.get(snapshot.token_id)
                if existing is not None and existing.engine.portfolio_key != key:
                    raise ShadowPortfolioInvariantError(
                        "token_id_maps_to_multiple_portfolios_in_station_day",
                        details={
                            "station_id": day[0],
                            "market_day": day[1],
                            "token_id": snapshot.token_id,
                            "first": existing.engine.portfolio_key.as_dict()
                            if existing.engine.portfolio_key
                            else None,
                            "second": key.as_dict(),
                        },
                    )
                portfolios_by_token[snapshot.token_id] = state
            return state

        timeline: dict[datetime, dict[str, list[Any]]] = defaultdict(lambda: {"snapshots": [], "trades": []})
        for row in day_rows:
            timeline[row.timestamp]["snapshots"].append(row)
        for trade in day_trades:
            # The exchange timestamp is retained for strict order-vs-trade
            # comparisons, while local processing cannot happen before the
            # archive receipt/availability timestamp.
            available_at = trade.available_at or trade.timestamp
            timeline[max(trade.timestamp, available_at)]["trades"].append(trade)
        for timestamp in sorted(timeline):
            current_snapshots = sorted(timeline[timestamp]["snapshots"], key=lambda row: row.token_id)
            for snapshot in current_snapshots:
                if risk_manager.halted:
                    break
                bands = config.entry_bands.get(snapshot.station_id or "", ())
                raw_entry_candidate = _band_match(snapshot.best_ask, bands)
                lag_trigger = _flag(snapshot.metadata.get("weather_market_lag"))
                weather_trigger = lag_trigger and _flag(snapshot.metadata.get("weather_improving"))
                trigger_candidate = (
                    raw_entry_candidate
                    if config.trigger_strategy == "price_band"
                    else weather_trigger
                    if config.trigger_strategy == "weather_market_lag"
                    else raw_entry_candidate or weather_trigger
                )
                if trigger_candidate:
                    trigger_candidates += 1
                try:
                    state = state_for(snapshot)
                    engine = state.engine
                    state.raw_snapshot_count += 1
                    engine.process_snapshot(snapshot)
                except ShadowPortfolioInvariantError as exc:
                    details = {
                        "station_id": day[0],
                        "market_day": day[1],
                        "timestamp": snapshot.timestamp.isoformat(),
                        **exc.details,
                    }
                    risk_manager.halt(exc.code, details=details)
                    discrepancies.append({"code": exc.code, "details": details})
                    break
                if snapshot.health_ok:
                    state.last_healthy = snapshot
                if not snapshot.health_ok and engine.inventory_shares > ZERO:
                    try:
                        engine.simulate_taker_exit(snapshot, reason="health_gate_risk_exit")
                    except ShadowPortfolioInvariantError as exc:
                        details = {
                            "station_id": day[0],
                            "market_day": day[1],
                            "timestamp": snapshot.timestamp.isoformat(),
                            **exc.details,
                        }
                        risk_manager.halt(exc.code, details=details)
                        discrepancies.append({"code": exc.code, "details": details})
                        break
                if not snapshot.health_ok:
                    if trigger_candidate:
                        rejection_reasons["health_gate"] += 1
                    continue
                entry_candidate = trigger_candidate
                if (
                    entry_candidate
                    and not engine.active_orders
                    and engine.inventory_shares <= ZERO
                    and state.next_tranche < len(config.tranche_usd)
                ):
                    tranche = config.tranche_usd[state.next_tranche]
                    if not risk_manager.can_reserve_buy(engines(), requested_usd=tranche):
                        state.missed_opportunities += 1
                        missed_opportunities += 1
                        rejection_reasons["station_day_budget_exceeded"] += 1
                        continue
                    quote = quote_limit(snapshot, mode=config.quote_mode)
                    if quote is None:
                        state.missed_opportunities += 1
                        missed_opportunities += 1
                        continue
                    try:
                        order = engine.submit_limit(
                            snapshot,
                            side=ShadowSide.BUY,
                            limit_price=quote,
                            size_usd=tranche,
                            idempotency_key=f"{day[0]}:{day[1]}:{snapshot.market_id}:{snapshot.timestamp.isoformat()}:entry",
                            strategy_version=config.version,
                            season_version=snapshot.season_version,
                            trigger_reason=("weather_market_lag" if weather_trigger else "price_band"),
                            maker_assumption=True,
                        )
                        state.next_tranche += 1
                    except (ValueError, KeyError) as exc:
                        state.missed_opportunities += 1
                        missed_opportunities += 1
                        reason = getattr(exc, "reason", None)
                        rejection_reasons[str(getattr(reason, "value", reason or "validation"))] += 1
                    else:
                        state.entry_order_count += 1
                        del order
                elif entry_candidate and (engine.active_orders or engine.inventory_shares > ZERO):
                    state.missed_opportunities += 1
                    missed_opportunities += 1
                if (
                    engine.inventory_shares > ZERO
                    and not engine.active_orders
                    and state.next_tranche < len(config.tranche_usd)
                    and config.replenish_mode is not ReplenishMode.NONE
                ):
                    target = (engine.average_inventory_cost or ZERO) + config.target_rise
                    tranche = config.tranche_usd[state.next_tranche]
                    if not risk_manager.can_reserve_buy(engines(), requested_usd=tranche):
                        rejection_reasons["station_day_budget_exceeded"] += 1
                    else:
                        try:
                            engine.submit_replenishment(
                                snapshot,
                                limit_price=quote_limit(snapshot, mode=config.quote_mode)
                                or snapshot.best_bid
                                or ZERO,
                                size_usd=tranche,
                                mode=config.replenish_mode,
                                target_price=target,
                                prior_order_resolved=True,
                                weather_improving=_flag(snapshot.metadata.get("weather_improving")),
                                weather_unchanged=_flag(snapshot.metadata.get("weather_unchanged")),
                                weather_worsening=_flag(snapshot.metadata.get("weather_worsening")),
                                duplicate_event=False,
                                strategy_version=config.version,
                                idempotency_key=(
                                    f"{day[0]}:{day[1]}:{snapshot.market_id}:"
                                    f"{snapshot.timestamp.isoformat()}:replenish:{state.next_tranche}"
                                ),
                            )
                        except (ValueError, KeyError):
                            pass
                        else:
                            state.next_tranche += 1
                if engine.inventory_shares > ZERO:
                    target = (engine.average_inventory_cost or ZERO) + config.target_rise
                    if snapshot.best_bid is not None and snapshot.best_bid >= target and not engine.active_orders:
                        exit_quote = quote_limit(snapshot, side=ShadowSide.SELL, mode=QuoteMode.BEST_BID)
                        if exit_quote is not None and exit_quote >= target:
                            try:
                                engine.submit_limit(
                                    snapshot,
                                    side=ShadowSide.SELL,
                                    limit_price=exit_quote,
                                    shares=engine.inventory_shares,
                                    idempotency_key=f"{day[0]}:{day[1]}:{snapshot.market_id}:{snapshot.timestamp.isoformat()}:exit",
                                    strategy_version=config.version,
                                    season_version=snapshot.season_version,
                                    trigger_reason="target_maker_exit",
                                    maker_assumption=True,
                                )
                            except (ValueError, KeyError):
                                pass
            # Trades are intentionally processed after snapshots at the same
            # timestamp.  Trades are routed only to their own token engine;
            # unrelated same-day buckets never enter its state machine.
            group = timeline[timestamp]["trades"]
            by_token: dict[str, list[TradeEvent]] = defaultdict(list)
            for trade in group:
                by_token[trade.asset_id].append(trade)
            for token_id, token_trades in by_token.items():
                state = portfolios_by_token.get(token_id)
                if state is None or risk_manager.halted:
                    continue
                try:
                    state.engine.process_trades(token_trades, reject_ambiguous_same_second=True)
                except ShadowPortfolioInvariantError as exc:
                    details = {
                        "station_id": day[0],
                        "market_day": day[1],
                        "timestamp": timestamp.isoformat(),
                        **exc.details,
                    }
                    risk_manager.halt(exc.code, details=details)
                    discrepancies.append({"code": exc.code, "details": details})
            if risk_manager.halted:
                break
            try:
                risk_manager.assert_conservation(engines())
            except ShadowPortfolioInvariantError as exc:
                discrepancies.append({"code": exc.code, "details": dict(exc.details)})
                break
            current_risk = risk_manager.current_open_exposure_usd(engines())
            global_peak_risk = max(global_peak_risk, current_risk)
            if current_risk > global_capital:
                # This is diagnostic only; do not create another order when
                # simultaneous cities would exceed the global capital cap.
                for state in portfolios.values():
                    state.engine.cancel_all(timestamp=timestamp, reason="global_capital_cap")
        if not risk_manager.halted:
            for state in portfolios.values():
                if state.engine.inventory_shares > ZERO and state.last_healthy is not None:
                    try:
                        state.engine.simulate_taker_exit(
                            state.last_healthy, reason="local_day_end_risk_exit"
                        )
                    except ShadowPortfolioInvariantError as exc:
                        details = {
                            "station_id": day[0],
                            "market_day": day[1],
                            **exc.details,
                        }
                        risk_manager.halt(exc.code, details=details)
                        discrepancies.append({"code": exc.code, "details": details})
                        break
            if not risk_manager.halted:
                try:
                    risk_manager.assert_conservation(engines())
                except ShadowPortfolioInvariantError as exc:
                    discrepancies.append({"code": exc.code, "details": dict(exc.details)})
        summaries = [
            _portfolio_summary(state, station_id=day[0], market_day=day[1])
            for _key, state in sorted(portfolios.items())
        ]
        portfolio_summaries.extend(summaries)
        for summary in summaries:
            all_fill_audit.extend(summary["fill_audit"])
        orders = [order for state in portfolios.values() for order in state.engine.orders]
        fills = [fill for order in orders for fill in order.fills]
        all_orders.extend(orders)
        all_fills.extend(fills)
        risk_summary = risk_manager.as_dict(engines())
        day_summaries.append(
            {
                "station_id": day[0],
                "market_day": day[1],
                "raw_snapshot_count": len(day_rows),
                "shadow_order_count": len(orders),
                "fill_count": len(fills),
                "token_portfolio_count": len(summaries),
                "round_trips": sum(summary["round_trips"] for summary in summaries),
                "entry_candidate_count": sum(summary["entry_order_count"] for summary in summaries),
                "price_band_candidate_count": sum(
                    1
                    for row in day_rows
                    if _band_match(row.best_ask, config.entry_bands.get(row.station_id or "", ()))
                ),
                "missed_opportunities": sum(
                    (summary["missed_opportunities"] for summary in summaries), start=0
                ),
                "inventory_at_end_shares": sum(
                    (summary["inventory_at_end_shares"] for summary in summaries), start=0.0
                ),
                "realized_pnl_usd": sum(
                    (summary["realized_pnl_usd"] for summary in summaries), start=0.0
                ),
                "emergency_taker_exit_pnl_usd": sum(
                    (summary["emergency_taker_exit_pnl_usd"] for summary in summaries), start=0.0
                ),
                "day_end_taker_exit_pnl_usd": sum(
                    (summary["day_end_taker_exit_pnl_usd"] for summary in summaries), start=0.0
                ),
                "fees_usd": sum((summary["fees_usd"] for summary in summaries), start=0.0),
                "risk": risk_summary,
                "halted": risk_manager.halted,
                "halt_reason": risk_manager.halt_reason,
            }
        )
    fill_waits = [
        (fill.timestamp - order.submitted_at).total_seconds() / 60
        for order in all_orders
        for fill in order.fills[:1]
    ]
    full_waits = [
        (order.last_fill_at - order.submitted_at).total_seconds() / 60
        for order in all_orders
        if order.last_fill_at is not None and order.state is ShadowOrderState.FILLED
    ]
    partial = sum(order.filled_shares > ZERO and order.state is not ShadowOrderState.FILLED for order in all_orders)
    cancelled = sum(order.state is ShadowOrderState.CANCELLED for order in all_orders)
    requoted = sum(order.cancel_reason == "requote" for order in all_orders)
    model_orders = len(all_orders)
    total_days = len(day_summaries)
    touch_audit = audit_touch_zero_orders(
        all_orders,
        snapshots,
        timeout=config.order_timeout,
    )
    capital_minutes = ZERO
    adverse_moves: list[float] = []
    markouts: dict[str, list[float]] = {f"{minutes}m": [] for minutes in (1, 5, 15, 30)}
    failure_exit_prices: list[float] = []
    for order in all_orders:
        end_at = order.last_fill_at or order.cancelled_at or order.expired_at
        if end_at is None:
            end_at = max((row.timestamp for row in snapshots), default=order.submitted_at)
        capital_minutes += order.requested_usd * Decimal(
            max(0.0, (end_at - order.submitted_at).total_seconds() / 60)
        )
        rows = all_snapshots_by_token.get(order.token_id, ())
        for fill in order.fills:
            later = [row for row in rows if row.timestamp > fill.timestamp]
            for minutes in (1, 5, 15, 30):
                candidates = [
                    row
                    for row in later
                    if row.timestamp >= fill.timestamp + timedelta(minutes=minutes)
                    and row.best_bid is not None
                ]
                if candidates:
                    markouts[f"{minutes}m"].append(
                        float(candidates[0].best_bid - fill.price)
                    )
            if order.side is ShadowSide.BUY:
                later_bids = [row.best_bid for row in later if row.best_bid is not None]
                if later_bids:
                    adverse_moves.append(float(min(later_bids) - fill.price))
            if fill.source == "emergency_taker_depth":
                failure_exit_prices.append(float(fill.price))
    capital_minutes_float = float(capital_minutes) if capital_minutes else None
    all_portfolios_flat = all(
        summary["inventory_at_end_shares"] == 0 for summary in portfolio_summaries
    )
    gross_pnl = (
        sum(
            (order.filled_usd for order in all_orders if order.side is ShadowSide.SELL),
            start=ZERO,
        )
        - sum(
            (order.filled_usd for order in all_orders if order.side is ShadowSide.BUY),
            start=ZERO,
        )
        if all_fills and all_portfolios_flat and not discrepancies
        else None
    )
    net_pnl = (
        sum(row["realized_pnl_usd"] for row in day_summaries)
        if all_fills and all_portfolios_flat and not discrepancies
        else None
    )
    return_on_capital = (
        float(net_pnl)
        / (capital_minutes_float / 60)
        if net_pnl is not None and capital_minutes_float and capital_minutes_float > 0
        else None
    )
    return {
        "fill_model": str(model),
        "raw_snapshot_count": len(snapshots),
        "shadow_order_count": model_orders,
        "fill_count": len(all_fills),
        "independent_market_day_count": total_days,
        "market_day_unit": "station/market-day; same-day buckets are correlated",
        "token_portfolio_count": len(portfolio_summaries),
        "station_day_cluster_count": total_days,
        "inventory_scope": "event_id + market_id + token_id + market_day",
        "budget_scope": "station_id + market_day; risk aggregation only",
        "station_day_budget_mode": str(config.station_day_budget_mode),
        "entry_candidate_count": sum(row["entry_candidate_count"] for row in day_summaries),
        "trigger_candidate_count": trigger_candidates,
        "price_band_candidate_count": sum(
            row["price_band_candidate_count"] for row in day_summaries
        ),
        "fill_rate": _rate(len([order for order in all_orders if order.filled_shares > ZERO]), model_orders),
        "full_fill_rate": _rate(sum(order.state is ShadowOrderState.FILLED for order in all_orders), model_orders),
        "market_day_round_trip_rate": _rate(
            sum(row["round_trips"] > 0 for row in day_summaries), total_days
        ),
        "partial_fill_rate": _rate(partial, model_orders),
        "cancel_rate": _rate(cancelled, model_orders),
        "requote_count": requoted,
        "missed_opportunity_count": missed_opportunities,
        "rejection_reasons": dict(rejection_reasons),
        "first_fill_wait_minutes": _summary(fill_waits),
        "full_fill_wait_minutes": _summary(full_waits),
        "gross_pnl_usd": float(gross_pnl) if gross_pnl is not None else None,
        "net_pnl_usd": float(net_pnl) if net_pnl is not None else None,
        "pnl_status": (
            "HALTED: token-portfolio invariant discrepancy"
            if discrepancies
            else "N/A: an inventory remains open and is not marked with a quote proxy"
            if not all_portfolios_flat
            else "diagnostic_token_scoped_closed_portfolios"
            if all_fills
            else "N/A: no shadow fills"
        ),
        "realized_pnl_usd": sum(
            (summary["realized_pnl_usd"] for summary in portfolio_summaries), start=0.0
        ),
        "unrealized_pnl_usd": None,
        "emergency_taker_exit_pnl_usd": sum(
            (summary["emergency_taker_exit_pnl_usd"] for summary in portfolio_summaries),
            start=0.0,
        ),
        "day_end_taker_exit_pnl_usd": sum(
            (summary["day_end_taker_exit_pnl_usd"] for summary in portfolio_summaries),
            start=0.0,
        ),
        "fees_usd": sum((summary["fees_usd"] for summary in portfolio_summaries), start=0.0),
        "max_inventory_risk_usd": float(global_peak_risk),
        "global_capital_usd": float(global_capital),
        "capital_minutes": capital_minutes_float,
        "return_on_capital_per_hour": return_on_capital,
        "capital_minutes_usd": capital_minutes_float,
        "settlement_hold_capital_minutes": None,
        "settlement_hold_contrast_status": "N/A: shadow strategy exits before settlement; no settlement-hold assumption",
        "max_adverse_move_per_share": min(adverse_moves, default=None),
        "maker_adverse_selection": {
            key: _summary(values) for key, values in markouts.items()
        },
        "markout_by_horizon": {
            key: _summary(values) for key, values in markouts.items()
        },
        "failure_path_exit_prices": failure_exit_prices,
        "gap_loss_usd": None,
        "max_drawdown_usd": None,
        "global_capital_missed_opportunities": None,
        "capital_reuse_across_cities": None,
        "round_trips": sum(row["round_trips"] for row in day_summaries),
        "day_summaries": day_summaries,
        "portfolio_summaries": portfolio_summaries,
        "fill_audit": all_fill_audit,
        "discrepancies": discrepancies,
        "orders": [order.as_dict() for order in all_orders],
        "touch_audit": touch_audit,
        "n_a_reason": (
            "HALTED: token portfolio invariant discrepancy"
            if discrepancies
            else "no eligible market-day snapshots"
            if total_days == 0
            else "fewer than 30 independent market-days; diagnostic only"
            if total_days < 30
            else None
        ),
        "execution_enabled": False,
    }


def replay_shadow_spread(
    pairs: Sequence[Mapping[str, Any] | BookSnapshot],
    *,
    trades: Mapping[str, Sequence[Any]] | Sequence[Any] | None = None,
    event_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    config: ShadowStrategyConfig | None = None,
    global_capital_usd: Decimal | float | str = Decimal("200"),
    settled_event_slugs: Sequence[str] = (),
    train_cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Run touch, queue-aware and trade-through replays over fixed inputs."""
    strategy = config or default_shadow_strategy_config()
    strategy.validate()
    snapshots = tuple(
        row if isinstance(row, BookSnapshot) else _row_snapshot(row, event_metadata=event_metadata)
        for row in pairs
    )
    snapshots = tuple(sorted(snapshots, key=lambda row: row.timestamp))
    trade_rows = _normalise_trades(trades)
    if train_cutoff is not None:
        cutoff = _utc(train_cutoff)
        # A train/test split is metadata only; no future row is allowed into
        # the test replay when a cutoff is requested.
        snapshots = tuple(row for row in snapshots if row.timestamp > cutoff)
        trade_rows = tuple(row for row in trade_rows if row.timestamp > cutoff)
    models = {
        str(model): _run_model(
            snapshots,
            trade_rows,
            config=strategy,
            model=model,
            global_capital_usd=Decimal(str(global_capital_usd)),
        )
        for model in (FillModel.TOUCH, FillModel.QUEUE_AWARE, FillModel.TRADE_THROUGH)
    }
    schedule_comparison: dict[str, dict[str, Any]] = {}
    for schedule in CANDIDATE_INVENTORY_SCHEDULES:
        schedule_config = replace(strategy, tranche_usd=schedule.tranches_usd)
        schedule_result = _run_model(
            snapshots,
            trade_rows,
            config=schedule_config,
            model=FillModel.QUEUE_AWARE,
            global_capital_usd=Decimal(str(global_capital_usd)),
        )
        schedule_comparison[schedule.name] = {
            "tranches_usd": [str(value) for value in schedule.tranches_usd],
            "total_usd": str(schedule.total_usd),
            "control": schedule.control,
            "independent_market_day_count": schedule_result["independent_market_day_count"],
            "shadow_order_count": schedule_result["shadow_order_count"],
            "fill_count": schedule_result["fill_count"],
            "full_fill_rate": schedule_result["full_fill_rate"],
            "net_pnl_usd": schedule_result["net_pnl_usd"],
            "diagnostic_only": True,
        }
    trigger_comparison: dict[str, dict[str, Any]] = {}
    for trigger_strategy in ("price_band", "weather_market_lag"):
        trigger_config = replace(strategy, trigger_strategy=trigger_strategy)
        trigger_result = _run_model(
            snapshots,
            trade_rows,
            config=trigger_config,
            model=FillModel.QUEUE_AWARE,
            global_capital_usd=Decimal(str(global_capital_usd)),
        )
        trigger_comparison[trigger_strategy] = {
            "trigger_candidate_count": trigger_result["trigger_candidate_count"],
            "price_band_candidate_count": trigger_result["price_band_candidate_count"],
            "entry_candidate_count": trigger_result["entry_candidate_count"],
            "independent_market_day_count": trigger_result["independent_market_day_count"],
            "token_portfolio_count": trigger_result["token_portfolio_count"],
            "station_day_cluster_count": trigger_result["station_day_cluster_count"],
            "shadow_order_count": trigger_result["shadow_order_count"],
            "fill_count": trigger_result["fill_count"],
            "full_fill_rate": trigger_result["full_fill_rate"],
            "market_day_round_trip_rate": trigger_result["market_day_round_trip_rate"],
            "round_trips": trigger_result["round_trips"],
            "net_pnl_usd": trigger_result["net_pnl_usd"],
            "pnl_status": trigger_result["pnl_status"],
            "realized_pnl_usd": trigger_result["realized_pnl_usd"],
            "unrealized_pnl_usd": trigger_result["unrealized_pnl_usd"],
            "emergency_taker_exit_pnl_usd": trigger_result["emergency_taker_exit_pnl_usd"],
            "day_end_taker_exit_pnl_usd": trigger_result["day_end_taker_exit_pnl_usd"],
            "fees_usd": trigger_result["fees_usd"],
            "portfolio_summaries": trigger_result["portfolio_summaries"],
            "fill_audit": trigger_result["fill_audit"],
            "discrepancies": trigger_result["discrepancies"],
            "diagnostic_only": True,
        }
    timeout_sensitivity: dict[str, dict[str, Any]] = {}
    # This is a predeclared operational sensitivity grid, not parameter
    # searching.  It keeps the same trigger family, bands, budget and fill
    # model while changing only the order timeout.
    for timeout_minutes in (5, 15, 30, 60):
        timeout_config = replace(strategy, order_timeout=timedelta(minutes=timeout_minutes))
        timeout_result = _run_model(
            snapshots,
            trade_rows,
            config=timeout_config,
            model=FillModel.QUEUE_AWARE,
            global_capital_usd=Decimal(str(global_capital_usd)),
        )
        timeout_sensitivity[str(timeout_minutes)] = {
            "order_timeout_seconds": timeout_minutes * 60,
            "shadow_order_count": timeout_result["shadow_order_count"],
            "fill_count": timeout_result["fill_count"],
            "full_fill_rate": timeout_result["full_fill_rate"],
            "touch_audit": timeout_result["touch_audit"],
            "rejection_reasons": timeout_result["rejection_reasons"],
            "diagnostic_only": True,
        }
    station_days = sorted({_day_key(row) for row in snapshots})
    primary_model = models[str(FillModel.QUEUE_AWARE)]
    snapshot_tokens = {row.token_id for row in snapshots}
    matched_trade_count = sum(row.asset_id in snapshot_tokens for row in trade_rows)
    candidate_days: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in snapshots:
        if _band_match(row.best_ask, strategy.entry_bands.get(row.station_id or "", ())):
            candidate_days[row.timestamp.date().isoformat()].add(_day_key(row))
    tranche = strategy.tranche_usd[0]
    global_capital_comparison = {}
    for cap in (Decimal("200"), Decimal("400")):
        capacity = int(cap // tranche) if tranche > ZERO else 0
        conflicts = sum(max(0, len(days) - capacity) for days in candidate_days.values())
        global_capital_comparison[str(cap)] = {
            "status": "diagnostic_cap_only",
            "capital_usd": float(cap),
            "candidate_overlap_days": sum(len(days) > 1 for days in candidate_days.values()),
            "candidate_conflict_count": conflicts,
            "capacity_at_first_tranche": capacity,
            "note": "Candidate overlap is not a fill or execution result; same-day buckets remain correlated.",
        }
    priority_station_summary: dict[str, dict[str, Any]] = {}
    for station in ("KLAX", "KLGA"):
        rows = [row for row in primary_model["day_summaries"] if row["station_id"] == station]
        priority_station_summary[station] = {
            "independent_market_day_count": len(rows),
            "raw_snapshot_count": sum(row["raw_snapshot_count"] for row in rows),
            "price_band_candidate_count": sum(
                row["price_band_candidate_count"] for row in rows
            ),
            "shadow_order_count": sum(row["shadow_order_count"] for row in rows),
            "fill_count": sum(row["fill_count"] for row in rows),
            "round_trips": sum(row["round_trips"] for row in rows),
            "status": "diagnostic only; no executable claim",
        }
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": max((row.timestamp for row in snapshots), default=None).isoformat()
        if snapshots
        else None,
        "strategy_version": strategy.version,
        "strategy": {
            "trigger_strategy": strategy.trigger_strategy,
            "quote_mode": str(strategy.quote_mode),
            "target_rise": str(strategy.target_rise),
            "market_budget_usd": str(strategy.market_budget_usd),
            "station_day_budget_mode": str(strategy.station_day_budget_mode),
            "station_day_budget_note": (
                "cumulative buy cost remains charged after an exit; current open exposure "
                "is reported separately"
            ),
            "tranche_usd": [str(value) for value in strategy.tranche_usd],
            "entry_bands": {
                station: [[str(lower), str(upper)] for lower, upper in bands]
                for station, bands in strategy.entry_bands.items()
            },
            "replenish_mode": str(strategy.replenish_mode),
            "partial_exit_fraction": str(strategy.partial_exit_fraction),
            "max_hold_seconds": (
                strategy.max_hold.total_seconds() if strategy.max_hold is not None else None
            ),
            "fixed_candidate_family": True,
            "walk_forward_required": True,
        },
        "raw_snapshot_count": len(snapshots),
        "independent_market_day_count": len(station_days),
        "settled_event_count": len(set(settled_event_slugs)),
        "settled_depth_overlap_count": len(
            set(str(value) for value in settled_event_slugs)
            & {row.event_id for row in snapshots}
        ),
        "settled_result_status": (
            "N/A: no settled event overlaps archived depth"
            if not (
                set(str(value) for value in settled_event_slugs)
                & {row.event_id for row in snapshots}
            )
            else "diagnostic only; settlement result is not used for shadow PnL"
        ),
        "trade_count": len(trade_rows),
        "matched_trade_count": matched_trade_count,
        "execution_result_status": (
            "N/A: no public trade events share a token with the archived depth"
            if matched_trade_count == 0
            else "diagnostic only; shadow fills are not executable fills"
        ),
        "price_band_candidate_count": primary_model["price_band_candidate_count"],
        "trigger_candidate_count": primary_model["trigger_candidate_count"],
        "same_second_policy": "ambiguous Data API groups are skipped; no intra-second order is invented",
        "models": models,
        "priority_station_summary": priority_station_summary,
        "schedule_comparison": schedule_comparison,
        "trigger_comparison": trigger_comparison,
        "timeout_sensitivity": timeout_sensitivity,
        "global_capital_comparison": global_capital_comparison,
        "primary_model": str(FillModel.QUEUE_AWARE),
        "limitations": [
            "shadow fills are model estimates, not actual order fills",
            "成交价不是可成交 ask/bid; public trades only consume queue in the model",
            "no resting order ID or exact queue position is available",
            "sample counts are market-day clustered; n<30 is statistically unreliable",
            "no executable-strategy claim; execution_enabled=false",
        ],
        "execution_enabled": False,
    }


analyze_shadow_spread = replay_shadow_spread


def render_shadow_spread_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    """Render a compact report while retaining the machine-readable result."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 只读影子限价/价差策略回放",
        "",
        f"生成时间（UTC）：{result.get('generated_at', 'N/A')}",
        f"数据截止（UTC）：{result.get('data_cutoff', 'N/A')}",
        f"策略版本：`{result.get('strategy_version', 'N/A')}`；`execution_enabled=false`。",
        "",
        "> 本报告是历史盘口上的影子模型，不是执行记录。成交价不是可成交 ask/bid；不得把 touch 上界当成可执行收益。",
        "> queue-aware 只用相反方向真实 taker 成交消耗记录的队列，trade-through 还要求严格穿价；盘口数量下降不算成交。",
        "> 库存、成本基准、订单、PnL 和 round trip 按 event/market/token/market-day 隔离；station-day 仅用于 $200 风险聚合和相关样本统计，绝不跨 token 平仓。",
        "",
        f"独立 market-day：**{result.get('independent_market_day_count', 0)}**；原始快照：**{result.get('raw_snapshot_count', 0)}**；",
        f"公开成交：**{result.get('trade_count', 0)}**。同站同日多个桶相关，n<30 的比例只能作统计不可靠诊断。",
        f"与归档 token 实际重叠的成交：**{result.get('matched_trade_count', 0)}**；结果状态："
        f"**{result.get('execution_result_status', 'N/A')}**。",
        f"真实 NO ask 落入配置价带的候选点：**{result.get('price_band_candidate_count', 0)}**；"
        f"当前触发族候选点：**{result.get('trigger_candidate_count', 0)}**；"
        "若订单为 0，优先检查下方 fail-closed 拒绝原因，而不是用代理报价补齐。",
        "",
        "| 模型 | token portfolios / station-day clusters | 影子订单 | 成交 | 完整成交率 | market-day round-trip 率 | 首次成交等待 p50/p90（分钟） | 净 PnL | 状态 |",
        "|---|---|---:|---:|---|---|---|---:|---|",
    ]
    for key, model in (result.get("models") or {}).items():
        rate = model.get("full_fill_rate") or {}
        day_rate = model.get("market_day_round_trip_rate") or {}
        waits = model.get("first_fill_wait_minutes") or {}
        lines.append(
            f"| {key} | {model.get('token_portfolio_count', 0)} / "
            f"{model.get('station_day_cluster_count', 0)} | "
            f"{model.get('shadow_order_count', 0)} | {model.get('fill_count', 0)} | "
            f"{rate.get('count', 0)}/{rate.get('sample_count', 0)} "
            f"(Wilson {rate.get('wilson_low')}–{rate.get('wilson_high')}) | "
            f"{day_rate.get('count', 0)}/{day_rate.get('sample_count', 0)} "
            f"(Wilson {day_rate.get('wilson_low')}–{day_rate.get('wilson_high')}) | "
            f"{waits.get('p50')} / {waits.get('p90')} | "
            f"{'N/A' if model.get('net_pnl_usd') is None else model.get('net_pnl_usd')} | "
            f"`{model.get('pnl_status', 'N/A')}` |"
        )
    lines.extend(
        [
            "",
            "## 触发策略固定候选族比较（queue-aware）",
            "",
            "| 触发族 | token portfolios / clusters | 触发候选 | 价带候选 | 影子订单 | 成交 | 完整成交率 | round-trip 率 | 净 PnL | 状态 |",
            "|---|---|---:|---:|---:|---:|---|---|---:|---|",
        ]
    )
    for name, trigger in (result.get("trigger_comparison") or {}).items():
        rate = trigger.get("full_fill_rate") or {}
        day_rate = trigger.get("market_day_round_trip_rate") or {}
        lines.append(
            f"| {name} | {trigger.get('token_portfolio_count', 0)} / "
            f"{trigger.get('station_day_cluster_count', 0)} | "
            f"{trigger.get('trigger_candidate_count', 0)} | "
            f"{trigger.get('price_band_candidate_count', 0)} | "
            f"{trigger.get('shadow_order_count', 0)} | {trigger.get('fill_count', 0)} | "
            f"{rate.get('count', 0)}/{rate.get('sample_count', 0)} "
            f"(Wilson {rate.get('wilson_low')}–{rate.get('wilson_high')}) | "
            f"{day_rate.get('count', 0)}/{day_rate.get('sample_count', 0)} "
            f"(Wilson {day_rate.get('wilson_low')}–{day_rate.get('wilson_high')}) | "
            f"{'N/A' if trigger.get('net_pnl_usd') is None else trigger.get('net_pnl_usd')} | "
            f"`{trigger.get('pnl_status', 'N/A')}` |"
        )
    lines.extend(
        [
            "",
            "## Token-scoped PnL 与逐笔成交证据（queue-aware 触发族）",
            "",
            "下表的成本基准只来自同一 token。`emergency_taker_depth` 是真实 bid 阶梯上的风险退出模拟；"
            "成交价从不被当作 ask/bid 代理。若 PnL 状态为 N/A 或 HALTED，不得引用该金额。",
        ]
    )
    for name, trigger in (result.get("trigger_comparison") or {}).items():
        lines.extend(
            [
                "",
                f"### {name}",
                "",
                f"状态：`{trigger.get('pnl_status', 'N/A')}`；"
                f"realized={trigger.get('realized_pnl_usd')}，unrealized={trigger.get('unrealized_pnl_usd')}，"
                f"日终 taker={trigger.get('day_end_taker_exit_pnl_usd')}，"
                f"所有 emergency taker={trigger.get('emergency_taker_exit_pnl_usd')}，"
                f"手续费={trigger.get('fees_usd')}。",
                "",
                "| token | 订单 | 方向 | 成交时间 | shares | 价格 | fee | 来源 | 后续同 token 退出 |",
                "|---|---|---|---|---:|---:|---:|---|---|",
            ]
        )
        fill_rows = trigger.get("fill_audit") or ()
        if not fill_rows:
            lines.append("| N=0 | — | — | — | — | — | — | — | — |")
        for fill in fill_rows:
            exits = "; ".join(
                f"{row.get('order_id', '')}@{row.get('price', '')}"
                for row in fill.get("later_same_token_exits", ())
            ) or "—"
            lines.append(
                f"| `{fill.get('token_id', '')}` | `{fill.get('order_id', '')}` | "
                f"{fill.get('side', '')} | {fill.get('timestamp', '')} | "
                f"{fill.get('shares', '')} | {fill.get('price', '')} | "
                f"{fill.get('fee_usd', '')} | `{fill.get('source', '')}` | {exits} |"
            )
        discrepancies = trigger.get("discrepancies") or ()
        if discrepancies:
            lines.append(f"跨 token / 预算不变量证据：`{discrepancies}`。")
    lines.extend(
        [
            "",
            "## 有限库存方案（queue-aware 诊断）",
            "",
            "| 方案 | 分批金额 | 影子订单 | 成交 | 完整成交率 | 净 PnL |",
            "|---|---|---:|---:|---|---:|",
        ]
    )
    for name, schedule in (result.get("schedule_comparison") or {}).items():
        rate = schedule.get("full_fill_rate") or {}
        lines.append(
            f"| {name} | {', '.join(schedule.get('tranches_usd', []))} | "
            f"{schedule.get('shadow_order_count', 0)} | {schedule.get('fill_count', 0)} | "
            f"{rate.get('count', 0)}/{rate.get('sample_count', 0)} "
            f"(Wilson {rate.get('wilson_low')}–{rate.get('wilson_high')}) | "
            f"{'N/A' if schedule.get('net_pnl_usd') is None else schedule.get('net_pnl_usd')} |"
        )
    queue_model = (result.get("models") or {}).get("queue_aware", {})
    touch_audit = queue_model.get("touch_audit") or {}
    lines.extend(
        [
            "",
            "## touch=0 逐单审计与 timeout 敏感性",
            "",
            f"主模型订单 **{touch_audit.get('order_count', 0)}**；触价 **{touch_audit.get('touch_count', 0)}**；"
            f"未触价 **{touch_audit.get('zero_touch_count', 0)}**。原因计数：`{touch_audit.get('reason_counts', {})}`。",
            "以下只使用提交之后、订单生命周期内的同 token 真实盘口；成交价不是可成交 ask/bid。",
            "",
            "| timeout（分钟） | 订单 | 成交 | 未触价 | 原因计数 |",
            "|---:|---:|---:|---:|---|",
        ]
    )
    for minutes, sensitivity in (result.get("timeout_sensitivity") or {}).items():
        audit = sensitivity.get("touch_audit") or {}
        lines.append(
            f"| {minutes} | {sensitivity.get('shadow_order_count', 0)} | "
            f"{sensitivity.get('fill_count', 0)} | {audit.get('zero_touch_count', 0)} | "
            f"`{audit.get('reason_counts', {})}` |"
        )
    lines.extend(
        [
            "",
            "逐单证据（主模型）如下；没有后续盘口不归因于价格未触价，标为 `snapshot_routing_or_token_error`/`no_later_quote`。",
            "",
            "| 订单 | market-day | token | 方向 | 限价 | 后续盘口 | 触价 | 原因 |",
            "|---|---|---|---|---:|---:|---|---|",
        ]
    )
    for order in touch_audit.get("orders", ()):
        lines.append(
            f"| `{order.get('order_id', '')}` | {order.get('market_day', '')} | "
            f"`{order.get('token_id', '')}` | {order.get('side', '')} | "
            f"{order.get('limit_price', '')} | {order.get('later_quote_count', 0)} | "
            f"{'是' if order.get('touched') else '否'} | `{order.get('reason', '')}` |"
        )
    trade_sources = result.get("trade_sources") or {}
    weather_join = result.get("weather_join") or {}
    lines.extend(
        [
            "",
            "## 成交源与天气严格时间对齐",
            "",
            f"WS 成交：{(trade_sources.get('market_ws') or {}).get('trade_count', 0)}；"
            f"Data API：{(trade_sources.get('data_api') or {}).get('trade_count', result.get('trade_count', 0))}；"
            f"side 闸门：{(trade_sources.get('side_validation') or {}).get('status', 'N/A')}。",
            f"天气观测：{weather_join.get('observation_count', 0)}；"
            f"join 原因：`{weather_join.get('reason_counts', {})}`；"
            "只接受 source timestamp 与 receipt/available timestamp 均不晚于盘口快照。",
            "若 queue-aware/trade-through 没有真实 token 重叠，只报告 N/A；touch 是乐观上界，不能转成 PnL。",
        ]
    )
    lines.extend(
        [
            "",
            "## KLAX/KLGA 优先摘要（queue-aware）",
            "",
            "| 站点 | 独立 market-day | 快照 | 价带候选 | 影子订单 | 成交 | round trips |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for station, summary in (result.get("priority_station_summary") or {}).items():
        lines.append(
            f"| {station} | {summary.get('independent_market_day_count', 0)} | "
            f"{summary.get('raw_snapshot_count', 0)} | {summary.get('price_band_candidate_count', 0)} | "
            f"{summary.get('shadow_order_count', 0)} | {summary.get('fill_count', 0)} | "
            f"{summary.get('round_trips', 0)} |"
        )
    lines.extend(
        [
            "",
            "## 解释与停止条件",
            "",
            "- 固定 $200 taker `price_path_report.md` 仍是压力基线；它不否定本处 $20/$50/$100 maker 分批假设。",
            "- 每个 token 独立持有库存与成本基准。$200 只在同 station-day 风险管理器内聚合：未成交 BUY 计潜在风险；本配置使用累计买入成本上限，平仓会释放当前敞口但不会重置当日累计额度。",
            "- 正常退出优先 maker（maker fee=0）；只有维护、stale、日终等风险处置才模拟真实 bid 深度 taker 费。",
            "- 当前深度历史很短，必须按日期 walk-forward；没有至少 30 个独立 market-day 时不得宣布正期望。",
            "- 全局资本 $200/$400 仅为冲突诊断上限，不是执行授权；同站同日多个桶仍按相关风险处理。",
            f"- 全局候选冲突诊断：`{result.get('global_capital_comparison', {})}`；候选重叠不等于成交。",
            f"- 影子订单拒绝原因计数（主模型）：`{result.get('models', {}).get('queue_aware', {}).get('rejection_reasons', {})}`。",
            f"- 主模型成交重叠状态：`{result.get('execution_result_status', 'N/A')}`；未实现 PnL、跳空损失和全局资本错失机会没有可靠输入时保持 N/A。",
            "- 结算重叠和 realized PnL 若为 N/A，不使用 `p`、midpoint、`1−YES` 或 trade price 填补。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_shadow_spread_result(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
