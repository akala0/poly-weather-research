"""Event-driven replay for the read-only maker shadow strategy.

The replay deliberately uses one finite state machine per station/market-day.
It consumes true NO bid/ask ladders and (when available) public taker trades;
it never turns a trade price, midpoint or YES complement into a quote.  The
result is a simulator diagnostic, not an executable-strategy claim.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
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
    ShadowSide,
    ShadowStrategyConfig,
    TradeEvent,
    quote_limit,
)

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
        version="shadow-spread-v1",
        quote_mode=QuoteMode.BEST_BID,
        fill_model=FillModel.QUEUE_AWARE,
        entry_bands=DEFAULT_ENTRY_BANDS,
        target_rise=Decimal("0.10"),
        order_timeout=timedelta(minutes=15),
        max_active_orders=1,
        market_budget_usd=Decimal("200"),
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
        book_complete=bool(no.get("book_complete", row.get("book_complete", True))),
        market_stale=bool(row.get("market_stale", False)),
        weather_stale=bool(row.get("weather_stale", False)),
        settlement_verified=bool(row.get("settlement_verified", True)),
        in_season=bool(row.get("in_season", True)),
        season_version=(
            str(row["season_version"]) if row.get("season_version") is not None else "unknown"
        ),
        warming_valid=bool(row.get("warming_valid", True)),
        upstream_status=str(row.get("upstream_status") or "normal"),
        quality_excluded=bool(row.get("quality_excluded", False)),
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
            source="data_api",
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
    return (snapshot.station_id or "unknown", snapshot.market_day or snapshot.timestamp.date().isoformat())


def _run_model(
    snapshots: Sequence[BookSnapshot],
    trades: Sequence[TradeEvent],
    *,
    config: ShadowStrategyConfig,
    model: FillModel,
    global_capital_usd: Decimal,
) -> dict[str, Any]:
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
    all_snapshots_by_token: dict[str, list[BookSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        all_snapshots_by_token[snapshot.token_id].append(snapshot)
    missed_opportunities = 0
    band_candidates = 0
    rejection_reasons: dict[str, int] = defaultdict(int)
    global_peak_risk = ZERO
    global_capital = Decimal(str(global_capital_usd))
    for day, day_rows in sorted(grouped.items()):
        day_rows = sorted(day_rows, key=lambda row: row.timestamp)
        day_trades = sorted(trades_by_day.get(day, ()), key=lambda row: row.timestamp)
        engine = ShadowOrderEngine(
            budget_usd=config.market_budget_usd,
            max_active_orders=config.max_active_orders,
            fill_model=model,
            order_timeout=config.order_timeout,
            market_day=day[1],
            require_season_version=config.require_season_version,
        )
        snapshots_by_token: dict[str, list[BookSnapshot]] = defaultdict(list)
        for row in day_rows:
            snapshots_by_token[row.token_id].append(row)
        timeline: dict[datetime, dict[str, list[Any]]] = defaultdict(lambda: {"snapshots": [], "trades": []})
        for row in day_rows:
            timeline[row.timestamp]["snapshots"].append(row)
        for trade in day_trades:
            timeline[trade.timestamp]["trades"].append(trade)
        entry_count = 0
        round_trips = 0
        had_inventory = False
        next_tranche = 0
        last_healthy: BookSnapshot | None = None
        for timestamp in sorted(timeline):
            current_snapshots = sorted(timeline[timestamp]["snapshots"], key=lambda row: row.token_id)
            for snapshot in current_snapshots:
                bands = config.entry_bands.get(snapshot.station_id or "", ())
                raw_entry_candidate = _band_match(snapshot.best_ask, bands)
                if raw_entry_candidate:
                    band_candidates += 1
                was_inventory = engine.inventory_shares > ZERO
                engine.process_snapshot(snapshot)
                if snapshot.health_ok:
                    last_healthy = snapshot
                if not snapshot.health_ok and was_inventory and engine.inventory_shares > ZERO:
                    engine.simulate_taker_exit(snapshot, reason="health_gate_risk_exit")
                if not snapshot.health_ok:
                    if raw_entry_candidate:
                        rejection_reasons["health_gate"] += 1
                    continue
                entry_candidate = raw_entry_candidate
                if (
                    entry_candidate
                    and not engine.active_orders
                    and engine.inventory_shares <= ZERO
                    and next_tranche < len(config.tranche_usd)
                    and bool(engine.available_budget_usd >= config.tranche_usd[next_tranche])
                ):
                    lag_trigger = bool(snapshot.metadata.get("weather_market_lag"))
                    weather_trigger = lag_trigger and bool(snapshot.metadata.get("weather_improving"))
                    if config.entry_bands and not (entry_candidate or weather_trigger):
                        continue
                    quote = quote_limit(snapshot, mode=config.quote_mode)
                    if quote is None:
                        missed_opportunities += 1
                        continue
                    try:
                        order = engine.submit_limit(
                            snapshot,
                            side=ShadowSide.BUY,
                            limit_price=quote,
                            size_usd=config.tranche_usd[0],
                            idempotency_key=f"{day[0]}:{day[1]}:{snapshot.market_id}:{snapshot.timestamp.isoformat()}:entry",
                            strategy_version=config.version,
                            season_version=snapshot.season_version,
                            trigger_reason=("weather_market_lag" if weather_trigger else "price_band"),
                            maker_assumption=True,
                        )
                        next_tranche += 1
                    except (ValueError, KeyError) as exc:
                        missed_opportunities += 1
                        reason = getattr(exc, "reason", None)
                        rejection_reasons[str(getattr(reason, "value", reason or "validation"))] += 1
                    else:
                        entry_count += 1
                        del order
                elif entry_candidate and (engine.active_orders or engine.inventory_shares > ZERO):
                    missed_opportunities += 1
                if (
                    engine.inventory_shares > ZERO
                    and not engine.active_orders
                    and next_tranche < len(config.tranche_usd)
                    and config.replenish_mode is not ReplenishMode.NONE
                ):
                    target = (engine.average_inventory_cost or ZERO) + config.target_rise
                    try:
                        engine.submit_replenishment(
                            snapshot,
                            limit_price=quote_limit(snapshot, mode=config.quote_mode) or snapshot.best_bid or ZERO,
                            size_usd=config.tranche_usd[next_tranche],
                            mode=config.replenish_mode,
                            target_price=target,
                            prior_order_resolved=True,
                            weather_improving=bool(snapshot.metadata.get("weather_improving")),
                            weather_unchanged=bool(snapshot.metadata.get("weather_unchanged")),
                            weather_worsening=bool(snapshot.metadata.get("weather_worsening")),
                            duplicate_event=False,
                            strategy_version=config.version,
                            idempotency_key=f"{day[0]}:{day[1]}:{snapshot.market_id}:{snapshot.timestamp.isoformat()}:replenish:{next_tranche}",
                        )
                    except (ValueError, KeyError):
                        pass
                    else:
                        next_tranche += 1
                if engine.inventory_shares > ZERO:
                    had_inventory = True
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
            # timestamp.  The engine also rejects a trade at submit time.
            group = timeline[timestamp]["trades"]
            if group:
                engine.process_trades(group, reject_ambiguous_same_second=True)
            if had_inventory and engine.inventory_shares <= ZERO:
                round_trips += 1
                had_inventory = False
            global_peak_risk = max(global_peak_risk, engine.total_risk_usd)
            if engine.total_risk_usd > global_capital:
                # This is diagnostic only; do not create another order when
                # simultaneous cities would exceed the global capital cap.
                engine.cancel_all(timestamp=timestamp, reason="global_capital_cap")
        if engine.inventory_shares > ZERO and last_healthy is not None:
            engine.simulate_taker_exit(last_healthy, reason="local_day_end_risk_exit")
        orders = list(engine.orders)
        fills = [fill for order in orders for fill in order.fills]
        all_orders.extend(orders)
        all_fills.extend(fills)
        day_summaries.append(
            {
                "station_id": day[0],
                "market_day": day[1],
                "raw_snapshot_count": len(day_rows),
                "shadow_order_count": len(orders),
                "fill_count": len(fills),
                "round_trips": round_trips,
                "entry_candidate_count": entry_count,
                "price_band_candidate_count": sum(
                    1
                    for row in day_rows
                    if _band_match(row.best_ask, config.entry_bands.get(row.station_id or "", ()))
                ),
                "missed_opportunities": missed_opportunities,
                "inventory_at_end_shares": float(engine.inventory_shares),
                "realized_pnl_usd": float(engine.realized_pnl_usd),
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
    return_on_capital = (
        float(sum(row["realized_pnl_usd"] for row in day_summaries))
        / (capital_minutes_float / 60)
        if capital_minutes_float and capital_minutes_float > 0
        else None
    )
    return {
        "fill_model": str(model),
        "raw_snapshot_count": len(snapshots),
        "shadow_order_count": model_orders,
        "fill_count": len(all_fills),
        "independent_market_day_count": total_days,
        "market_day_unit": "station/market-day; same-day buckets are correlated",
        "entry_candidate_count": sum(row["entry_candidate_count"] for row in day_summaries),
        "price_band_candidate_count": band_candidates,
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
        "gross_pnl_usd": float(sum((order.filled_usd for order in all_orders if order.side is ShadowSide.SELL), start=ZERO) - sum((order.filled_usd for order in all_orders if order.side is ShadowSide.BUY), start=ZERO)),
        "net_pnl_usd": float(sum(row["realized_pnl_usd"] for row in day_summaries)),
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
        "unrealized_pnl_usd": None,
        "global_capital_missed_opportunities": None,
        "capital_reuse_across_cities": None,
        "round_trips": sum(row["round_trips"] for row in day_summaries),
        "day_summaries": day_summaries,
        "orders": [order.as_dict() for order in all_orders],
        "n_a_reason": (
            "no eligible market-day snapshots"
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
    station_days = sorted({_day_key(row) for row in snapshots})
    primary_model = models[str(FillModel.QUEUE_AWARE)]
    snapshot_tokens = {row.token_id for row in snapshots}
    matched_trade_count = sum(row.asset_id in snapshot_tokens for row in trade_rows)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": max((row.timestamp for row in snapshots), default=None).isoformat()
        if snapshots
        else None,
        "strategy_version": strategy.version,
        "strategy": {
            "quote_mode": str(strategy.quote_mode),
            "target_rise": str(strategy.target_rise),
            "market_budget_usd": str(strategy.market_budget_usd),
            "tranche_usd": [str(value) for value in strategy.tranche_usd],
            "entry_bands": {
                station: [[str(lower), str(upper)] for lower, upper in bands]
                for station, bands in strategy.entry_bands.items()
            },
            "replenish_mode": str(strategy.replenish_mode),
            "fixed_candidate_family": True,
            "walk_forward_required": True,
        },
        "raw_snapshot_count": len(snapshots),
        "independent_market_day_count": len(station_days),
        "settled_event_count": len(set(settled_event_slugs)),
        "settled_depth_overlap_count": 0,
        "settled_result_status": "N/A: settlement overlap must be supplied separately",
        "trade_count": len(trade_rows),
        "matched_trade_count": matched_trade_count,
        "execution_result_status": (
            "N/A: no public trade events share a token with the archived depth"
            if matched_trade_count == 0
            else "diagnostic only; shadow fills are not executable fills"
        ),
        "price_band_candidate_count": primary_model["price_band_candidate_count"],
        "same_second_policy": "ambiguous Data API groups are skipped; no intra-second order is invented",
        "models": models,
        "schedule_comparison": schedule_comparison,
        "global_capital_comparison": {
            str(cap): {
                "status": "diagnostic_cap_only",
                "capital_usd": cap,
                "note": "Global cap is not an execution authorization; same-day bucket risk remains correlated.",
            }
            for cap in (200, 400)
        },
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
        "",
        f"独立 market-day：**{result.get('independent_market_day_count', 0)}**；原始快照：**{result.get('raw_snapshot_count', 0)}**；",
        f"公开成交：**{result.get('trade_count', 0)}**。同站同日多个桶相关，n<30 的比例只能作统计不可靠诊断。",
        f"与归档 token 实际重叠的成交：**{result.get('matched_trade_count', 0)}**；结果状态："
        f"**{result.get('execution_result_status', 'N/A')}**。",
        f"真实 NO ask 落入配置价带的候选点：**{result.get('price_band_candidate_count', 0)}**；"
        "若订单为 0，优先检查下方 fail-closed 拒绝原因，而不是用代理报价补齐。",
        "",
        "| 模型 | 影子订单 | 成交 | 完整成交率 | market-day round-trip 率 | 首次成交等待 p50/p90（分钟） | 净 PnL |",
        "|---|---:|---:|---|---|---|---:|",
    ]
    for key, model in (result.get("models") or {}).items():
        rate = model.get("full_fill_rate") or {}
        day_rate = model.get("market_day_round_trip_rate") or {}
        waits = model.get("first_fill_wait_minutes") or {}
        lines.append(
            f"| {key} | {model.get('shadow_order_count', 0)} | {model.get('fill_count', 0)} | "
            f"{rate.get('count', 0)}/{rate.get('sample_count', 0)} "
            f"(Wilson {rate.get('wilson_low')}–{rate.get('wilson_high')}) | "
            f"{day_rate.get('count', 0)}/{day_rate.get('sample_count', 0)} "
            f"(Wilson {day_rate.get('wilson_low')}–{day_rate.get('wilson_high')}) | "
            f"{waits.get('p50')} / {waits.get('p90')} | {model.get('net_pnl_usd')} |"
        )
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
            f"{schedule.get('net_pnl_usd')} |"
        )
    lines.extend(
        [
            "",
            "## 解释与停止条件",
            "",
            "- 固定 $200 taker `price_path_report.md` 仍是压力基线；它不否定本处 $20/$50/$100 maker 分批假设。",
            "- 总预算是每个 market-day 的累计库存上限；未成交活动单计入潜在风险，未成交不计入库存。",
            "- 正常退出优先 maker（maker fee=0）；只有维护、stale、日终等风险处置才模拟真实 bid 深度 taker 费。",
            "- 当前深度历史很短，必须按日期 walk-forward；没有至少 30 个独立 market-day 时不得宣布正期望。",
            "- 全局资本 $200/$400 仅为冲突诊断上限，不是执行授权；同站同日多个桶仍按相关风险处理。",
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
