"""Read-only diagnostics for why a high-NO weather bucket cannot be entered.

The module deliberately separates four different phenomena that can feel the
same in the UI: no resting ask, an ask priced at the endpoint, insufficient
depth at a requested notional, and a display based on an old trade.  It never
substitutes ``1 - p`` or a trade price for the NO token's resting ask.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.adapters.clob import OrderBookSnapshot
from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.domain import MarketPricePoint
from poly_weather.execution_cost import (
    estimate_execution_by_shares,
    estimate_execution_cost,
)
from poly_weather.no_forward import wilson_interval

TAIL_FLOOR = Decimal("0.99")
NEAR_ENDPOINT_PRICE = Decimal("0.999")
DISPLAY_WIDE_SPREAD = Decimal("0.10")
DEFAULT_ENTRY_SIZES_USD = (
    Decimal("20"),
    Decimal("50"),
    Decimal("100"),
    Decimal("150"),
    Decimal("200"),
)
DIAGNOSTIC_DEPTH_SIZE_USD = Decimal("1000")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _levels(value: Any) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(value, list):
        return ()
    output: list[tuple[Decimal, Decimal]] = []
    for row in value:
        if not isinstance(row, Mapping):
            continue
        price = _decimal(row.get("price"))
        size = _decimal(row.get("size"))
        if price is not None and size is not None and price > 0 and size > 0:
            output.append((price, size))
    return tuple(output)


def _top(levels: Sequence[tuple[Decimal, Decimal]], *, bids: bool) -> Decimal | None:
    if not levels:
        return None
    return max(price for price, _size in levels) if bids else min(
        price for price, _size in levels
    )


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _number_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "sample_count": len(values),
        "mean": fmean(values) if values else None,
        "p10": _percentile(values, 0.10),
        "p50": median(values) if values else None,
        "p90": _percentile(values, 0.90),
    }


def _rate(successes: int, sample_count: int) -> dict[str, Any]:
    interval = wilson_interval(successes, sample_count)
    return {
        "count": successes,
        "sample_count": sample_count,
        "rate": successes / sample_count if sample_count else None,
        "wilson_low": interval[0] if interval else None,
        "wilson_high": interval[1] if interval else None,
        "statistically_unreliable": sample_count < 30,
    }


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _no_quote_at(pair: Mapping[str, Any]) -> datetime | None:
    no = pair.get("no")
    if isinstance(no, Mapping) and (timestamp := _timestamp(no.get("_timestamp"))) is not None:
        return timestamp
    return _timestamp(pair.get("observed_at"))


def _latest_at_or_before(
    index: Mapping[str, tuple[tuple[datetime, ...], tuple[Any, ...]]],
    asset_id: str,
    cutoff: datetime,
) -> Any | None:
    values = index.get(asset_id)
    if values is None:
        return None
    timestamps, rows = values
    position = bisect_right(timestamps, cutoff) - 1
    return rows[position] if position >= 0 else None


def _price_index(
    price_points_by_asset: Mapping[str, Sequence[MarketPricePoint]],
) -> dict[str, tuple[tuple[datetime, ...], tuple[MarketPricePoint, ...]]]:
    output: dict[str, tuple[tuple[datetime, ...], tuple[MarketPricePoint, ...]]] = {}
    for asset_id, points in price_points_by_asset.items():
        ordered = tuple(sorted(points, key=lambda point: point.timestamp.astimezone(UTC)))
        output[str(asset_id)] = (
            tuple(point.timestamp.astimezone(UTC) for point in ordered),
            ordered,
        )
    return output


def _trade_index(
    trades_by_asset: Mapping[str, Sequence[PublicTrade]],
) -> dict[str, tuple[tuple[datetime, ...], tuple[PublicTrade, ...]]]:
    output: dict[str, tuple[tuple[datetime, ...], tuple[PublicTrade, ...]]] = {}
    for asset_id, trades in trades_by_asset.items():
        ordered = tuple(sorted(trades, key=lambda trade: trade.timestamp.astimezone(UTC)))
        output[str(asset_id)] = (
            tuple(trade.timestamp.astimezone(UTC) for trade in ordered),
            ordered,
        )
    return output


def _frontend_display(
    *,
    bid: Decimal | None,
    ask: Decimal | None,
    last_trade_price: Decimal | None,
) -> tuple[Decimal | None, str]:
    """Apply Polymarket's documented display-price rule where reconstructible."""
    if bid is None or ask is None:
        return None, "one_sided_book_rule_not_documented"
    if ask - bid <= DISPLAY_WIDE_SPREAD:
        return (bid + ask) / Decimal(2), "midpoint"
    if last_trade_price is None:
        return None, "wide_spread_without_last_trade"
    return last_trade_price, "last_trade"


def _trade_age_band(age_minutes: float | None, *, tape_collected: bool) -> str:
    if age_minutes is None:
        return "no_prior_trade_in_tape" if tape_collected else "trade_tape_not_collected"
    if age_minutes < 60:
        return "<1h"
    if age_minutes < 180:
        return "1-3h"
    return ">3h"


def _peak_phase(hours_to_peak: float | None) -> str:
    if hours_to_peak is None:
        return "typical_peak_unknown"
    if hours_to_peak < 0:
        return "after_typical_peak"
    if hours_to_peak <= 1:
        return "pre_peak_0-1h"
    if hours_to_peak <= 2:
        return "pre_peak_1-2h"
    if hours_to_peak <= 4:
        return "pre_peak_2-4h"
    return "pre_peak_>4h"


def _bucket_label(market_slug: str) -> str:
    base = market_slug.split("/", 1)[-1].casefold()
    if "forbelow" in base:
        value = base.rsplit("-", 1)[-1].removesuffix("forbelow")
        return f"≤{value}°F"
    if "forhigher" in base:
        value = base.rsplit("-", 1)[-1].removesuffix("forhigher")
        return f"≥{value}°F"
    suffix = base.rsplit("-", 1)[-1]
    if suffix.endswith("f"):
        previous = base[: -len(suffix) - 1].rsplit("-", 1)[-1]
        if previous.lstrip("-").isdigit() and suffix[:-1].isdigit():
            return f"{previous}-{suffix[:-1]}°F"
    return market_slug


def _record_is_tail(
    bid: Decimal | None,
    ask: Decimal | None,
    last_trade_price: Decimal | None,
) -> bool:
    return any(value is not None and value >= TAIL_FLOOR for value in (bid, ask, last_trade_price))


def current_tail_rule_targets(
    pairs: Sequence[Mapping[str, Any]],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]],
    as_of: datetime,
    stations: frozenset[str] = frozenset({"KLAX", "KLGA"}),
) -> list[dict[str, Any]]:
    """Pick currently relevant priority-city tail tokens for public rule checks."""
    latest_by_asset: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        event_slug = str(pair.get("event_slug") or "")
        metadata = event_metadata.get(event_slug)
        if metadata is None or str(metadata.get("station_id")) not in stations:
            continue
        quote_at = _no_quote_at(pair)
        no = pair.get("no")
        if quote_at is None or not isinstance(no, Mapping):
            continue
        asset_id = str(no.get("asset_id") or "")
        if not asset_id:
            continue
        timezone = ZoneInfo(str(metadata["timezone"]))
        target_date = date.fromisoformat(str(metadata["target_date"]))
        if target_date < as_of.astimezone(timezone).date():
            continue
        asks = _levels(no.get("asks"))
        bids = _levels(no.get("bids"))
        bid = _top(bids, bids=True)
        ask = _top(asks, bids=False)
        last_trade_price = _decimal(no.get("last_trade_price"))
        if not _record_is_tail(bid, ask, last_trade_price):
            continue
        candidate = {
            "asset_id": asset_id,
            "station_id": str(metadata["station_id"]),
            "event_slug": event_slug,
            "market_slug": str(pair.get("market_slug") or ""),
            "bucket": _bucket_label(str(pair.get("market_slug") or "")),
            "target_date": target_date.isoformat(),
            "observed_at": quote_at.isoformat(),
        }
        existing = latest_by_asset.get(asset_id)
        if existing is None or candidate["observed_at"] > existing["observed_at"]:
            latest_by_asset[asset_id] = candidate
    return sorted(
        latest_by_asset.values(),
        key=lambda row: (str(row["station_id"]), str(row["market_slug"])),
    )


def current_rule_rows(
    targets: Sequence[Mapping[str, Any]],
    *,
    books_by_asset: Mapping[str, OrderBookSnapshot],
    errors_by_asset: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Join live public rule observations to the archived tail-token identities."""
    errors_by_asset = errors_by_asset or {}
    rows: list[dict[str, Any]] = []
    for target in targets:
        asset_id = str(target["asset_id"])
        book = books_by_asset.get(asset_id)
        if book is None:
            rows.append({**target, "status": "unavailable", "error": errors_by_asset.get(asset_id)})
            continue
        top_ask = min((price for price, size in book.asks if size > 0), default=None)
        rows.append(
            {
                **target,
                "status": "observed",
                "observed_at": book.fetched_at.isoformat(),
                "min_order_size": float(book.min_order_size),
                "tick_size": float(book.tick_size),
                "top_ask": float(top_ask) if top_ask is not None else None,
                "minimum_notional_at_top": (
                    float(book.min_order_size * top_ask) if top_ask is not None else None
                ),
            }
        )
    return rows


def _scope_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    sizes_usd: Sequence[Decimal],
    diagnostic_size_usd: Decimal,
) -> dict[str, Any]:
    count = len(records)
    with_asks = [row for row in records if row["no_ask"] is not None]
    ladder = {
        "top_ask_shares": _number_summary(
            [float(row["top_ask_shares"]) for row in with_asks]
        ),
        "top_ask_usd": _number_summary([float(row["top_ask_usd"]) for row in with_asks]),
        "first_three_ask_shares": _number_summary(
            [float(row["first_three_ask_shares"]) for row in with_asks]
        ),
        "first_three_ask_usd": _number_summary(
            [float(row["first_three_ask_usd"]) for row in with_asks]
        ),
    }
    size_rows: list[dict[str, Any]] = []
    for size in (*sizes_usd, diagnostic_size_usd):
        key = str(size)
        fills = [row["fills"][key] for row in records]
        complete = [row for row in fills if row["entry_complete"]]
        round_trip = [row for row in fills if row["round_trip_complete"]]
        top_too_small = sum(
            row["top_ask_usd"] is not None and Decimal(str(row["top_ask_usd"])) < size
            for row in records
        )
        reasons = {
            "no_resting_ask": sum(row["no_ask"] is None for row in records),
            "endpoint_priced_ask": sum(
                row["no_ask"] is not None
                and Decimal(str(row["no_ask"])) >= NEAR_ENDPOINT_PRICE
                for row in records
            ),
            "insufficient_depth_below_endpoint": sum(
                row["no_ask"] is not None
                and Decimal(str(row["no_ask"])) < NEAR_ENDPOINT_PRICE
                and not row["fills"][key]["entry_complete"]
                for row in records
            ),
            "full_entry_below_endpoint": sum(
                row["no_ask"] is not None
                and Decimal(str(row["no_ask"])) < NEAR_ENDPOINT_PRICE
                and row["fills"][key]["entry_complete"]
                for row in records
            ),
        }
        size_rows.append(
            {
                "size_usd": float(size),
                "diagnostic_only": size == diagnostic_size_usd,
                "entry_full_fill": _rate(len(complete), count),
                "entry_partial_or_missing": _rate(count - len(complete), count),
                "top_level_insufficient": _rate(top_too_small, count),
                "round_trip_same_book_full": _rate(len(round_trip), count),
                "mean_entry_average": _number_summary(
                    [float(row["entry_average"]) for row in complete]
                ),
                "entry_slippage_per_share": _number_summary(
                    [float(row["entry_slippage"]) for row in complete]
                ),
                "entry_fee_per_share": _number_summary(
                    [float(row["entry_fee_per_share"]) for row in complete]
                ),
                "exit_slippage_per_share": _number_summary(
                    [float(row["exit_slippage"]) for row in round_trip]
                ),
                "exit_fee_per_share": _number_summary(
                    [float(row["exit_fee_per_share"]) for row in round_trip]
                ),
                "gross_price_rise_hurdle_per_share": _number_summary(
                    [float(row["gross_price_rise_hurdle_per_share"]) for row in round_trip]
                ),
                "primary_reasons": {name: _rate(value, count) for name, value in reasons.items()},
            }
        )
    return {
        "snapshot_count": count,
        "event_count": len({str(row["event_slug"]) for row in records}),
        "market_count": len({str(row["market_slug"]) for row in records}),
        "statistically_unreliable": count < 30,
        "literal_no_ask": _rate(sum(row["no_ask"] is None for row in records), count),
        "ask_exactly_one": _rate(
            sum(row["no_ask"] is not None and Decimal(str(row["no_ask"])) == 1 for row in records),
            count,
        ),
        "reported_best_ask_exactly_one": _rate(
            sum(
                row["reported_best_ask"] is not None
                and Decimal(str(row["reported_best_ask"])) == 1
                for row in records
            ),
            count,
        ),
        "reported_best_ask_one_without_depth": _rate(
            sum(
                row["reported_best_ask"] is not None
                and Decimal(str(row["reported_best_ask"])) == 1
                and row["no_ask"] is None
                for row in records
            ),
            count,
        ),
        "ask_at_or_above_0999": _rate(
            sum(
                row["no_ask"] is not None
                and Decimal(str(row["no_ask"])) >= NEAR_ENDPOINT_PRICE
                for row in records
            ),
            count,
        ),
        "ladder": ladder,
        "sizes": size_rows,
    }


def _p_visibility_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    price_history_collected: bool,
    trade_tape_collected: bool,
) -> dict[str, Any]:
    p_rows = [row for row in records if row["p"] is not None]
    comparable = [row for row in p_rows if row["no_ask"] is not None]
    by_age: list[dict[str, Any]] = []
    for band in ("<1h", "1-3h", ">3h", "no_prior_trade_in_tape"):
        selected = [row for row in p_rows if row["trade_age_band"] == band]
        comparable_band = [row for row in selected if row["no_ask"] is not None]
        differences = [float(row["ask_minus_p"]) for row in comparable_band]
        underquote = sum(value >= 0.01 for value in differences)
        by_age.append(
            {
                "trade_age_band": band,
                "sample_count": len(selected),
                "ask_comparable_count": len(comparable_band),
                "no_ask": _rate(
                    len(selected) - len(comparable_band), len(selected)
                ),
                "ask_minus_p": _number_summary(differences),
                "ask_exceeds_p_by_1c": _rate(underquote, len(comparable_band)),
            }
        )
    p_tail = [row for row in p_rows if Decimal(str(row["p"])) >= TAIL_FLOOR]
    p_tail_stale = sum(
        row["trade_age_minutes"] is None or float(row["trade_age_minutes"]) >= 60
        for row in p_tail
    )
    frontend_tail = [
        row
        for row in records
        if row["frontend_display"] is not None
        and Decimal(str(row["frontend_display"])) >= TAIL_FLOOR
    ]
    stale_frontend = sum(
        row["frontend_source"] == "last_trade"
        and (row["trade_age_minutes"] is None or float(row["trade_age_minutes"]) >= 60)
        for row in frontend_tail
    )
    return {
        "price_history_collected": price_history_collected,
        "trade_tape_collected": trade_tape_collected,
        "p_available": _rate(len(p_rows), len(records)),
        "p_ask_comparable": _rate(len(comparable), len(p_rows)),
        "by_trade_age": by_age,
        "p_tail_sample_count": len(p_tail),
        "p_tail_stale_or_no_prior": _rate(p_tail_stale, len(p_tail)),
        "frontend_tail_sample_count": len(frontend_tail),
        "frontend_tail_sources": {
            source: _rate(
                sum(row["frontend_source"] == source for row in frontend_tail),
                len(frontend_tail),
            )
            for source in (
                "midpoint",
                "last_trade",
                "one_sided_book_rule_not_documented",
                "wide_spread_without_last_trade",
            )
        },
        "frontend_tail_stale_last_trade": _rate(stale_frontend, len(frontend_tail)),
    }


def _time_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One record per market/local hour prevents full-window archive oversampling."""
    selected: dict[tuple[str, str, str, int], Mapping[str, Any]] = {}
    for row in sorted(records, key=lambda item: str(item["quote_at"])):
        key = (
            str(row["station_id"]),
            str(row["market_slug"]),
            str(row["local_date"]),
            int(row["local_hour"]),
        )
        selected.setdefault(key, row)
    downsampled = list(selected.values())

    def compact(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        count = len(rows)
        fills_20 = [row["fills"]["20"] for row in rows]
        fills_100 = [row["fills"]["100"] for row in rows]
        fills_200 = [row["fills"]["200"] for row in rows]
        complete_200 = [row for row in fills_200 if row["entry_complete"]]
        return {
            "snapshot_count": count,
            "literal_no_ask": _rate(sum(row["no_ask"] is None for row in rows), count),
            "entry_20_full": _rate(sum(row["entry_complete"] for row in fills_20), count),
            "entry_100_full": _rate(sum(row["entry_complete"] for row in fills_100), count),
            "entry_200_full": _rate(sum(row["entry_complete"] for row in fills_200), count),
            "entry_200_slippage": _number_summary(
                [float(row["entry_slippage"]) for row in complete_200]
            ),
        }

    hourly = []
    by_hour: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in downsampled:
        by_hour[(str(row["station_id"]), int(row["local_hour"]))].append(row)
    for (station_id, hour), rows in sorted(by_hour.items()):
        hourly.append({"station_id": station_id, "local_hour": hour, **compact(rows)})

    by_phase: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in downsampled:
        by_phase[(str(row["station_id"]), str(row["peak_phase"]))].append(row)
    phases = [
        {"station_id": station_id, "peak_phase": phase, **compact(rows)}
        for (station_id, phase), rows in sorted(by_phase.items())
    ]
    return {
        "downsampled_snapshot_count": len(downsampled),
        "hourly": hourly,
        "typical_peak_phase": phases,
        "semantics": (
            "one earliest eligible snapshot per market/local-hour; these are correlated "
            "market snapshots, not independent trade attempts"
        ),
    }


def _rule_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    observed = [row for row in rows if row.get("status") == "observed"]
    min_notionals = [
        float(row["minimum_notional_at_top"])
        for row in observed
        if row.get("minimum_notional_at_top") is not None
    ]
    return {
        "target_count": len(rows),
        "observed": _rate(len(observed), len(rows)),
        "tick_sizes": dict(
            sorted(Counter(str(row.get("tick_size")) for row in observed).items())
        ),
        "min_order_sizes": dict(
            sorted(Counter(str(row.get("min_order_size")) for row in observed).items())
        ),
        "minimum_notional_at_top": _number_summary(min_notionals),
        "minimum_notional_at_top_sample_count": len(min_notionals),
        "minimum_notional_at_or_below_20": _rate(
            sum(value <= 20 for value in min_notionals), len(min_notionals)
        ),
        "rows": list(rows),
    }


def analyze_no_entry_accessibility(
    pairs: Sequence[Mapping[str, Any]],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]],
    typical_peak_minutes_by_station: Mapping[str, int],
    price_points_by_asset: Mapping[str, Sequence[MarketPricePoint]] | None = None,
    trades_by_asset: Mapping[str, Sequence[PublicTrade]] | None = None,
    current_rules: Sequence[Mapping[str, Any]] = (),
    sizes_usd: Sequence[Decimal] = DEFAULT_ENTRY_SIZES_USD,
    diagnostic_size_usd: Decimal = DIAGNOSTIC_DEPTH_SIZE_USD,
) -> dict[str, Any]:
    """Quantify entry accessibility from real NO books with no price proxy.

    Every historical p/trade lookup uses the NO quote's own timestamp as the
    cutoff.  Future p points and future executions are therefore unavailable to
    the computation even when they were fetched in the same public API batch.
    """
    price_history_collected = price_points_by_asset is not None
    trade_tape_collected = trades_by_asset is not None
    price_index = _price_index(price_points_by_asset or {})
    trade_index = _trade_index(trades_by_asset or {})
    records: list[dict[str, Any]] = []

    for pair in pairs:
        event_slug = str(pair.get("event_slug") or "")
        metadata = event_metadata.get(event_slug)
        no = pair.get("no")
        quote_at = _no_quote_at(pair)
        if metadata is None or not isinstance(no, Mapping) or quote_at is None:
            continue
        asset_id = str(no.get("asset_id") or "")
        if not asset_id:
            continue
        timezone = ZoneInfo(str(metadata["timezone"]))
        local = quote_at.astimezone(timezone)
        target_date = date.fromisoformat(str(metadata["target_date"]))
        bid_levels = _levels(no.get("bids"))
        ask_levels = tuple(sorted(_levels(no.get("asks")), key=lambda level: level[0]))
        no_bid = _top(bid_levels, bids=True)
        no_ask = _top(ask_levels, bids=False)
        reported_best_ask = _decimal(no.get("best_ask"))
        last_trade_price = _decimal(no.get("last_trade_price"))
        p_point = _latest_at_or_before(price_index, asset_id, quote_at)
        trade = _latest_at_or_before(trade_index, asset_id, quote_at)
        p_age_minutes = (
            (quote_at - p_point.timestamp.astimezone(UTC)).total_seconds() / 60
            if p_point is not None
            else None
        )
        trade_age_minutes = (
            (quote_at - trade.timestamp.astimezone(UTC)).total_seconds() / 60
            if trade is not None
            else None
        )
        frontend_display, frontend_source = _frontend_display(
            bid=no_bid,
            ask=no_ask,
            last_trade_price=last_trade_price,
        )
        top_ask_shares = ask_levels[0][1] if ask_levels else None
        top_ask_usd = no_ask * top_ask_shares if no_ask is not None and top_ask_shares else None
        first_three = ask_levels[:3]
        first_three_shares = sum((size for _price, size in first_three), start=Decimal(0))
        first_three_usd = sum((price * size for price, size in first_three), start=Decimal(0))
        fills: dict[str, dict[str, Any]] = {}
        for size in (*sizes_usd, diagnostic_size_usd):
            buy = estimate_execution_cost(ask_levels, size, "buy")
            entry_complete = buy is not None and buy.filled_fraction >= 1
            sell = (
                estimate_execution_by_shares(bid_levels, buy.filled_shares, "sell")
                if entry_complete and buy is not None
                else None
            )
            exit_complete = sell is not None and sell.filled_fraction >= 1
            hurdle = (
                buy.slippage_vs_top
                + buy.fee_per_share
                + sell.slippage_vs_top
                + sell.fee_per_share
                if entry_complete and exit_complete and buy is not None and sell is not None
                else None
            )
            fills[str(size)] = {
                "entry_complete": entry_complete,
                "entry_average": float(buy.average_fill_price) if entry_complete and buy else None,
                "entry_slippage": float(buy.slippage_vs_top) if entry_complete and buy else None,
                "entry_fee_per_share": float(buy.fee_per_share) if entry_complete and buy else None,
                "entry_filled_fraction": buy.filled_fraction if buy else 0.0,
                "exit_complete": exit_complete,
                "exit_average": float(sell.average_fill_price) if exit_complete and sell else None,
                "exit_slippage": float(sell.slippage_vs_top) if exit_complete and sell else None,
                "exit_fee_per_share": float(sell.fee_per_share) if exit_complete and sell else None,
                "round_trip_complete": bool(entry_complete and exit_complete),
                "gross_price_rise_hurdle_per_share": float(hurdle) if hurdle is not None else None,
            }
        peak_minutes = typical_peak_minutes_by_station.get(str(metadata["station_id"]))
        peak_at = (
            datetime.combine(target_date, time.min, tzinfo=timezone)
            + timedelta(minutes=peak_minutes)
            if peak_minutes is not None
            else None
        )
        hours_to_peak = (
            (peak_at - local).total_seconds() / 3600 if peak_at is not None else None
        )
        records.append(
            {
                "quote_at": quote_at.isoformat(),
                "event_slug": event_slug,
                "market_slug": str(pair.get("market_slug") or ""),
                "bucket": _bucket_label(str(pair.get("market_slug") or "")),
                "asset_id": asset_id,
                "station_id": str(metadata["station_id"]),
                "target_date": target_date.isoformat(),
                "local_date": local.date().isoformat(),
                "local_hour": local.hour,
                "hours_to_typical_peak": hours_to_peak,
                "peak_phase": _peak_phase(hours_to_peak),
                "no_bid": float(no_bid) if no_bid is not None else None,
                "no_ask": float(no_ask) if no_ask is not None else None,
                "reported_best_ask": (
                    float(reported_best_ask) if reported_best_ask is not None else None
                ),
                "last_trade_price": float(last_trade_price) if last_trade_price is not None else None,
                "p": float(p_point.price) if p_point is not None else None,
                "p_at": p_point.timestamp.isoformat() if p_point is not None else None,
                "p_age_minutes": p_age_minutes,
                "last_public_trade_at": trade.timestamp.isoformat() if trade is not None else None,
                "trade_age_minutes": trade_age_minutes,
                "trade_age_band": _trade_age_band(
                    trade_age_minutes, tape_collected=trade_tape_collected
                ),
                "ask_minus_p": (
                    float(no_ask - p_point.price)
                    if no_ask is not None and p_point is not None
                    else None
                ),
                "frontend_display": float(frontend_display) if frontend_display is not None else None,
                "frontend_source": frontend_source,
                "tail_book": _record_is_tail(no_bid, no_ask, last_trade_price),
                "tail_p": p_point is not None and p_point.price >= TAIL_FLOOR,
                "tail_frontend": frontend_display is not None and frontend_display >= TAIL_FLOOR,
                "top_ask_shares": float(top_ask_shares) if top_ask_shares is not None else None,
                "top_ask_usd": float(top_ask_usd) if top_ask_usd is not None else None,
                "first_three_ask_shares": float(first_three_shares) if ask_levels else None,
                "first_three_ask_usd": float(first_three_usd) if ask_levels else None,
                "ask_level_count": len(ask_levels),
                "fills": fills,
            }
        )

    tail_records = [row for row in records if row["tail_book"]]
    scopes = {
        "all_ten_cities": tail_records,
        "KLAX": [row for row in tail_records if row["station_id"] == "KLAX"],
        "KLGA": [row for row in tail_records if row["station_id"] == "KLGA"],
        "other_eight_cities": [
            row for row in tail_records if row["station_id"] not in {"KLAX", "KLGA"}
        ],
    }
    priority_tail_buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tail_records:
        if row["station_id"] in {"KLAX", "KLGA"}:
            priority_tail_buckets[f"{row['station_id']} {row['bucket']}"].append(row)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "tail_floor": float(TAIL_FLOOR),
        "paired_snapshot_count": len(pairs),
        "analyzable_snapshot_count": len(records),
        "tail_book_snapshot_count": len(tail_records),
        "tail_definition": (
            "NO best bid, NO best ask, or NO token latest trade in the archived book is >=0.99; "
            "this uses no complementary-price proxy"
        ),
        "strict_no_lookahead": (
            "each p point and public trade is selected only when its timestamp is <= the NO book's own timestamp"
        ),
        "price_history_collected": price_history_collected,
        "trade_tape_collected": trade_tape_collected,
        "scope_summaries": {
            name: _scope_summary(
                rows, sizes_usd=sizes_usd, diagnostic_size_usd=diagnostic_size_usd
            )
            for name, rows in scopes.items()
        },
        "priority_bucket_summaries": {
            name: _scope_summary(
                rows, sizes_usd=sizes_usd, diagnostic_size_usd=diagnostic_size_usd
            )
            for name, rows in sorted(priority_tail_buckets.items())
        },
        "p_visibility": {
            name: _p_visibility_summary(
                rows,
                price_history_collected=price_history_collected,
                trade_tape_collected=trade_tape_collected,
            )
            for name, rows in {
                "all_ten_cities": records,
                "KLAX": [row for row in records if row["station_id"] == "KLAX"],
                "KLGA": [row for row in records if row["station_id"] == "KLGA"],
            }.items()
        },
        "time_of_day": _time_summary(
            [row for row in tail_records if row["station_id"] in {"KLAX", "KLGA"}]
        ),
        "current_rules": _rule_summary(current_rules),
        "semantics": {
            "frontend_display": (
                "Polymarket documents midpoint when bid-ask spread is <=$0.10 and last trade when wider; "
                "one-sided-book fallback is not documented and remains unknown"
            ),
            "p": "prices-history p is a historical price point, not a resting bid, ask, or guaranteed UI value",
            "trade_price": "public trades are executions only and never substitute for a resting NO ask",
            "endpoint_ask": (
                "an ask at 1.000 is a literal resting sell order, not an empty book; it is separately "
                "classified because it leaves no profitable upward price room"
            ),
            "round_trip_hurdle": (
                "entry depth slippage + same-book opposite-side depth slippage + both taker fees; "
                "future exit liquidity can differ, so this is a current depth-cost hurdle rather than PnL"
            ),
        },
        "execution_enabled": False,
        "records": records,
    }


def _pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.1%}"


def _number(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def _rate_text(row: Mapping[str, Any]) -> str:
    return (
        f"{int(row['count'])}/{int(row['sample_count'])} ({_pct(row['rate'])}; "
        f"Wilson 95% {_pct(row['wilson_low'])}–{_pct(row['wilson_high'])})"
    )


def _unreliable(row: Mapping[str, Any]) -> str:
    return "；n<30，统计不可靠" if row.get("statistically_unreliable") else ""


def _size_summary(summary: Mapping[str, Any], size_usd: Decimal) -> Mapping[str, Any]:
    for row in summary["sizes"]:
        if Decimal(str(row["size_usd"])) == size_usd:
            return row
    raise KeyError(f"missing size summary {size_usd}")


def render_no_entry_accessibility_report(result: Mapping[str, Any], output_path: Path) -> None:
    """Render the actionable, non-executable entry-accessibility report."""
    all_scope = result["scope_summaries"]["all_ten_cities"]
    klax = result["scope_summaries"]["KLAX"]
    klga = result["scope_summaries"]["KLGA"]
    klax_20 = _size_summary(klax, Decimal("20"))
    klax_50 = _size_summary(klax, Decimal("50"))
    klga_20 = _size_summary(klga, Decimal("20"))
    klga_50 = _size_summary(klga, Decimal("50"))
    lines = [
        "# NO 尾桶为什么进不了场：真实深度可达性审计",
        "",
        f"生成时间：{result['generated_at']}。分析配对深度 {result['paired_snapshot_count']} 个，"
        f"其中满足 NO≥{result['tail_floor']:.2f} 的真实尾桶快照 {result['tail_book_snapshot_count']} 个。",
        "所有归档输入默认排除了官方维护及实测恢复质量窗口。每个历史 p 点和公开成交均只允许 "
        "`timestamp <= NO 订单簿自身时间戳`；没有使用 `1-p`、`1-YES` 或成交价来替代 NO ask。",
        "",
        "## 直接回答：为什么会“看到 99% 却买不进”",
        "",
        f"- **字面没有卖单**（NO asks 为空）：十城 {_rate_text(all_scope['literal_no_ask'])}；"
        f"KLAX {_rate_text(klax['literal_no_ask'])}；KLGA {_rate_text(klga['literal_no_ask'])}。",
        f"- **有卖单但卡在价格端点**（best ask≥0.999）：十城 {_rate_text(all_scope['ask_at_or_above_0999'])}；"
        "这不是空盘；ask=0.999 在 0.001 tick 下最多只剩 0.1¢ 名义上行，ask=1.000 则没有上行空间。",
        f"- **ask 恰为 1.000**：十城 {_rate_text(all_scope['ask_exactly_one'])}。"
        "它与“完全没有 ask”分开，不能把端点报价误写成无卖家。",
        f"- **行情摘要写 `best_ask=1.000`、但正数量 asks 阶梯为空**：十城 "
        f"{_rate_text(all_scope['reported_best_ask_one_without_depth'])}；"
        f"KLAX {_rate_text(klax['reported_best_ask_one_without_depth'])}；"
        f"KLGA {_rate_text(klga['reported_best_ask_one_without_depth'])}。"
        "这类记录在可达性分类中属于“没有卖单”，不把摘要字段 1.000 当作真实挂单。",
        "- **有低于端点的 ask，但指定金额穿透不足**：见下方每个金额的“完整成交率”和互斥主因。"
        "顶层价格不是该金额的入场成本。",
        "- **显示陈旧**是另一个、可叠加的原因：官方前端并不等同于 `prices-history.p`；"
        "其显示规则和 p/真实 ask 差距见 Q2。",
        "",
        "## Q1：尾桶 NO ask 是否真实存在、量有多大",
        "",
        "尾桶定义为 NO 自己的 best bid、best ask 或最新成交任一达到 0.99；不是 YES 的补数。"
        "挂单量只统计正数量的 NO asks。",
        "",
        "| 范围 | 快照 | 无 ask (Wilson 95%) | 实际 asks 阶梯=1.000 | 摘要 best_ask=1 且阶梯空 | ask≥0.999 | 顶层份数 p10/p50/p90 | 顶层名义 p10/p50/p90 | 前三档份数 p10/p50/p90 | 前三档名义 p10/p50/p90 | 可靠性 |",
        "|---|---:|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for label, scope in (("KLAX", klax), ("KLGA", klga), ("其余八城", result["scope_summaries"]["other_eight_cities"]), ("十城", all_scope)):
        ladder = scope["ladder"]
        lines.append(
            f"| {label} | {scope['snapshot_count']} | {_rate_text(scope['literal_no_ask'])} | "
            f"{_rate_text(scope['ask_exactly_one'])} | "
            f"{_rate_text(scope['reported_best_ask_one_without_depth'])} | "
            f"{_rate_text(scope['ask_at_or_above_0999'])} | "
            f"{_number(ladder['top_ask_shares']['p10'], 2)} / {_number(ladder['top_ask_shares']['p50'], 2)} / {_number(ladder['top_ask_shares']['p90'], 2)} | "
            f"{_number(ladder['top_ask_usd']['p10'], 2)} / {_number(ladder['top_ask_usd']['p50'], 2)} / {_number(ladder['top_ask_usd']['p90'], 2)} | "
            f"{_number(ladder['first_three_ask_shares']['p10'], 2)} / {_number(ladder['first_three_ask_shares']['p50'], 2)} / {_number(ladder['first_three_ask_shares']['p90'], 2)} | "
            f"{_number(ladder['first_three_ask_usd']['p10'], 2)} / {_number(ladder['first_three_ask_usd']['p50'], 2)} / {_number(ladder['first_three_ask_usd']['p90'], 2)} | "
            f"{'n<30，统计不可靠' if scope['statistically_unreliable'] else '快照量可用'} |"
        )
    lines.extend(
        [
            "",
            "### 用户点名的 KLAX 桶",
            "",
            "以下表只列出归档中实际出现且处于 NO≥0.99 尾部状态的 KLAX 桶；没有出现的精确桶不以相邻桶替代。",
            "",
            "| 桶 | 快照 | 无 ask (Wilson 95%) | ask≥0.999 (Wilson 95%) | 顶层名义 p50 | 前三档名义 p50 | $200 完整成交 (Wilson 95%) | $200 顶层滑点 p50 | 可靠性 |",
            "|---|---:|---|---|---:|---:|---|---:|---|",
        ]
    )
    for label, scope in result["priority_bucket_summaries"].items():
        if not label.startswith("KLAX "):
            continue
        buy_200 = _size_summary(scope, Decimal("200"))
        ladder = scope["ladder"]
        lines.append(
            f"| {label.removeprefix('KLAX ')} | {scope['snapshot_count']} | {_rate_text(scope['literal_no_ask'])} | "
            f"{_rate_text(scope['ask_at_or_above_0999'])} | {_number(ladder['top_ask_usd']['p50'], 2)} | "
            f"{_number(ladder['first_three_ask_usd']['p50'], 2)} | {_rate_text(buy_200['entry_full_fill'])} | "
            f"{_number(buy_200['entry_slippage_per_share']['p50'], 4)} | "
            f"{'n<30，统计不可靠' if scope['statistically_unreliable'] else '快照量可用'} |"
        )

    rules = result["current_rules"]
    lines.extend(
        [
            "",
            "### tick size 与最小单量：历史边界和当前实测",
            "",
            "历史 WebSocket 帧会间歇保留 `tick_size`，但不可靠地保留 `min_order_size`，"
            "因此**不能倒推历史最小单量**。下列为生成本报告时对仍活跃的 KLAX/KLGA 尾桶逐 token 调用公开 `/book` 的当前实测，不能回写成历史规则。",
            f"- 目标 token：{rules['target_count']}；成功读到当前规则：{_rate_text(rules['observed'])}。",
            f"- 当前 tick size 分布：{rules['tick_sizes'] or 'N/A'}；当前 min_order_size 分布：{rules['min_order_sizes'] or 'N/A'}。",
            f"- 按当前 top ask 计算的最小名义（仅 {rules['minimum_notional_at_top_sample_count']} 个实际有 top ask 的 token）："
            f"p50={_number(rules['minimum_notional_at_top']['p50'], 3)}，"
            f"p90={_number(rules['minimum_notional_at_top']['p90'], 3)}；≤$20：{_rate_text(rules['minimum_notional_at_or_below_20'])}"
            f"{'；n<30，统计不可靠' if rules['minimum_notional_at_top_sample_count'] < 30 else ''}。",
            "- 因而 $20–$200 档若受阻，优先归因于空 ask、端点报价或深度，而不是把未经历史留存验证的 min_order_size 当作原因。",
            "",
            "## Q2：用户看到的价格与真实可成交 NO ask",
            "",
            "[Polymarket 官方说明](https://docs.polymarket.com/concepts/prices-orderbook)：显示价在 spread≤$0.10 时为 bid/ask 中点，"
            "spread>$0.10 时显示最近成交价；买入仍须支付 ask。单边 book 的前端回退规则未在该说明中定义，以下不猜测。"
            "因此 `prices-history.p` 只是历史成交价点的比较基准，**不是前端显示价的同义词，也不是 ask**。",
            "",
        ]
    )
    for scope_name, label in (("KLAX", "KLAX"), ("KLGA", "KLGA"), ("all_ten_cities", "十城")):
        visibility = result["p_visibility"][scope_name]
        lines.extend(
            [
                f"### {label}",
                "",
                f"- p 可在 NO 订单簿时刻前取得：{_rate_text(visibility['p_available'])}；"
                f"有 p 且有真实 ask 可比较：{_rate_text(visibility['p_ask_comparable'])}。",
                f"- p≥0.99 的样本：{visibility['p_tail_sample_count']}；其中最后一笔 NO token 公开成交已超过 1 小时或在本次规范成交带中无此前成交："
                f"{_rate_text(visibility['p_tail_stale_or_no_prior'])}。这是 p 陈旧度，不把 p 当作 ask。",
                f"- 可按官方规则重建且显示≥0.99 的样本：{visibility['frontend_tail_sample_count']}；"
                f"其中由陈旧 last-trade 回退而来（>1h 或本带无此前成交）：{_rate_text(visibility['frontend_tail_stale_last_trade'])}。",
                f"- 这些可重建的高显示价来源：midpoint {_rate_text(visibility['frontend_tail_sources']['midpoint'])}；"
                f"last-trade {_rate_text(visibility['frontend_tail_sources']['last_trade'])}；"
                f"单边簿口径未知 {_rate_text(visibility['frontend_tail_sources']['one_sided_book_rule_not_documented'])}。",
                "",
                "| 最后一笔 NO 成交年龄 | p 样本 | 有真实 ask | 无 ask (Wilson 95%) | ask−p p50 | ask−p p90 | ask 比 p 高≥1¢ (Wilson 95%) | 可靠性 |",
                "|---|---:|---:|---|---:|---:|---|---|",
            ]
        )
        for row in visibility["by_trade_age"]:
            lines.append(
                f"| {row['trade_age_band']} | {row['sample_count']} | {row['ask_comparable_count']} | "
                f"{_rate_text(row['no_ask'])} | {_number(row['ask_minus_p']['p50'], 4)} | "
                f"{_number(row['ask_minus_p']['p90'], 4)} | {_rate_text(row['ask_exceeds_p_by_1c'])} | "
                f"{'n<30，统计不可靠' if row['ask_exceeds_p_by_1c']['statistically_unreliable'] else '快照量可用'} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Q3：$200 以内的真实买入可成交性",
            "",
            "完整成交率的分母包括空 ask；均价、滑点和手续费只在完整成交的快照上计算。"
            "“同簿往返门槛”不是预测未来出场价：它是同一时刻买入指定金额后，卖出相同份额所需克服的 "
            "入场滑点 + 出场深度滑点 + 两笔 Weather taker fee。",
            "",
            "令未来目标 best bid 相对于**入场 top ask**的毛上升为 `G`，则净价差为："
            "`G − 同簿往返门槛`。因此未指定目标价差时，不能诚实地编造“净剩多少”；表中的 p50/p90 门槛就是每档必须超过的 G。",
            "",
            "| 范围 | 名义 | 完整买入 (Wilson 95%) | 深度均价 mean / p50 | 顶层滑点 p50 / p90 | 入场手续费/份 p50 | 同簿往返完整 (Wilson 95%) | 毛上升门槛 p50 / p90 | 可靠性 |",
            "|---|---:|---|---:|---:|---:|---|---:|---|",
        ]
    )
    for scope_name, label in (("KLAX", "KLAX"), ("KLGA", "KLGA"), ("other_eight_cities", "其余八城"), ("all_ten_cities", "十城")):
        scope = result["scope_summaries"][scope_name]
        for size in DEFAULT_ENTRY_SIZES_USD:
            row = _size_summary(scope, size)
            lines.append(
                f"| {label} | ${int(size)} | {_rate_text(row['entry_full_fill'])} | "
                f"{_number(row['mean_entry_average']['mean'], 4)} / {_number(row['mean_entry_average']['p50'], 4)} | "
                f"{_number(row['entry_slippage_per_share']['p50'], 4)} / {_number(row['entry_slippage_per_share']['p90'], 4)} | "
                f"{_number(row['entry_fee_per_share']['p50'], 5)} | {_rate_text(row['round_trip_same_book_full'])} | "
                f"{_number(row['gross_price_rise_hurdle_per_share']['p50'], 4)} / {_number(row['gross_price_rise_hurdle_per_share']['p90'], 4)} | "
                f"{'n<30，统计不可靠' if row['entry_full_fill']['statistically_unreliable'] else '快照量可用'} |"
            )
    diagnostic = _size_summary(all_scope, DIAGNOSTIC_DEPTH_SIZE_USD)
    lines.extend(
        [
            "",
            f"$1000 仅作盘口总厚度诊断，不参与仓位建议：十城尾桶完整买入 {_rate_text(diagnostic['entry_full_fill'])}。",
            "",
            "### 互斥主因：同一快照为什么无法作为有实质上行空间的入场",
            "",
            "以下四类按顺序互斥且覆盖每个尾桶快照：空 asks；真实 top ask≥0.999 的近端点（最多只剩 0.1¢ 名义上行）；"
            "低于端点但指定金额深度不足；低于端点且完整买入。最后一类只是“可进入”，不是收益或执行许可。",
            "",
            "| 范围 | 名义 | 无真实 ask | 端点 ask≥0.999 | 低于端点但深度不足 | 低于端点且完整买入 | 可靠性 |",
            "|---|---:|---|---|---|---|---|",
        ]
    )
    for scope, label in ((klax, "KLAX"), (klga, "KLGA")):
        for size in DEFAULT_ENTRY_SIZES_USD:
            row = _size_summary(scope, size)
            reasons = row["primary_reasons"]
            lines.append(
                f"| {label} | ${int(size)} | {_rate_text(reasons['no_resting_ask'])} | "
                f"{_rate_text(reasons['endpoint_priced_ask'])} | "
                f"{_rate_text(reasons['insufficient_depth_below_endpoint'])} | "
                f"{_rate_text(reasons['full_entry_below_endpoint'])} | "
                f"{'n<30，统计不可靠' if row['entry_full_fill']['statistically_unreliable'] else '快照量可用'} |"
            )
    lines.extend(
        [
            "",
            "### 仓位结论（研究口径，不构成执行授权）",
            "",
            "- 没有任何 $20–$200 档能提供“无条件可进场”的建议：完整填充率的分母包含所有尾桶时刻，"
            "而主要损失来自空 asks 和端点价格，缩小金额不会使它们变成有流动性的市场。",
            f"- 若只为后续**纸面**研究设置一个最小风险上限，采用 $20：KLAX/KLGA 的同簿往返 p90 门槛分别为 "
            f"{_number(klax_20['gross_price_rise_hurdle_per_share']['p90'], 4)} / "
            f"{_number(klga_20['gross_price_rise_hurdle_per_share']['p90'], 4)}，低于 $50 的 "
            f"{_number(klax_50['gross_price_rise_hurdle_per_share']['p90'], 4)} / "
            f"{_number(klga_50['gross_price_rise_hurdle_per_share']['p90'], 4)}；"
            "这只是成本最小化，不表示 $20 更容易无条件成交。",
            "- 此纸面上限仍须同时满足：真实 NO asks 非空、top ask<0.999、该档深度完整填充，"
            "并且预设毛价差 `G` 大于该档 p90 同簿往返门槛。未满足任一条件即 N/A / skip。",
            "- 在所有执行前置条件仍未满足时，上述只用于确定后续纸面研究的名义上限；"
            "`execution_enabled=false`，没有下单建议或下单路径。",
            "",
            "## Q4：本地时段与典型高点窗口",
            "",
            "为避免 09:00–20:00 本地全量归档比夜间小时快照权重更高，以下每个市场/本地小时只保留最早一个合格快照。"
            "这仍是相关的盘口快照，不是独立交易样本；n<30 明确标注。典型高点分箱仅使用已配置的典型高点时钟，"
            "不声称该时刻一定正在升温。",
            "",
            "| 城市 | 本地小时 | 快照 | 无 ask (Wilson 95%) | $20 完整 (Wilson 95%) | $100 完整 (Wilson 95%) | $200 完整 (Wilson 95%) | $200 滑点 p50 | 可靠性 |",
            "|---|---:|---:|---|---|---|---|---:|---|",
        ]
    )
    for row in result["time_of_day"]["hourly"]:
        lines.append(
            f"| {row['station_id']} | {int(row['local_hour']):02}:00 | {row['snapshot_count']} | "
            f"{_rate_text(row['literal_no_ask'])} | {_rate_text(row['entry_20_full'])} | "
            f"{_rate_text(row['entry_100_full'])} | {_rate_text(row['entry_200_full'])} | "
            f"{_number(row['entry_200_slippage']['p50'], 4)} | "
            f"{'n<30，统计不可靠' if row['snapshot_count'] < 30 else '快照量可用'} |"
        )
    lines.extend(
        [
            "",
            "| 城市 | 相对典型高点 | 快照 | 无 ask (Wilson 95%) | $200 完整 (Wilson 95%) | $200 滑点 p50 | 可靠性 |",
            "|---|---|---:|---|---|---:|---|",
        ]
    )
    for row in result["time_of_day"]["typical_peak_phase"]:
        lines.append(
            f"| {row['station_id']} | {row['peak_phase']} | {row['snapshot_count']} | "
            f"{_rate_text(row['literal_no_ask'])} | {_rate_text(row['entry_200_full'])} | "
            f"{_number(row['entry_200_slippage']['p50'], 4)} | "
            f"{'n<30，统计不可靠' if row['snapshot_count'] < 30 else '快照量可用'} |"
        )
    phase_rows = {
        (str(row["station_id"]), str(row["peak_phase"])): row
        for row in result["time_of_day"]["typical_peak_phase"]
    }
    klax_after = phase_rows.get(("KLAX", "after_typical_peak"))
    klga_after = phase_rows.get(("KLGA", "after_typical_peak"))
    lines.extend(
        [
            "",
            "- **典型高点后**的可达性明显更差："
            f"KLAX 无 ask {_rate_text(klax_after['literal_no_ask']) if klax_after else 'N/A'}、"
            f"$200 完整 {_rate_text(klax_after['entry_200_full']) if klax_after else 'N/A'}；"
            f"KLGA 无 ask {_rate_text(klga_after['literal_no_ask']) if klga_after else 'N/A'}、"
            f"$200 完整 {_rate_text(klga_after['entry_200_full']) if klga_after else 'N/A'}。",
            "- 本地深夜各小时和典型高点前 0–4 小时的多数格子 n<30；不能据此宣称“深夜必然更薄”。"
            "当前可复现结论仅是高点后变差，候选升温窗口仍需继续积累独立前向快照。",
        ]
    )
    lines.extend(
        [
            "",
            "## 能力边界与下一步",
            "",
            "- 本报告回答的是实时/归档 CLOB 的**可达性**，不是持有到结算的赌注分析。价格路径仍由温度确认驱动，"
            "逆转风险另按物理余量分层评估。",
            "- 成交带只提供成交时间、成交价和成交量；成交价不是 resting ask/bid，不能填补深度缺口。",
            "- 当前历史真实深度与已结算事件仍无重叠，任何收益结论继续为 N/A；本报告不把未结算快照伪装成已验证利润。",
            "- 所有比例均附 Wilson 95%。快照间和同市场连续时点相关，区间描述的是样本频率，不是独立策略胜率。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
