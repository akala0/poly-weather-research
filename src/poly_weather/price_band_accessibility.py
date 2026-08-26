"""Read-only accessibility analysis for intermediate NO price bands.

The analysis in this module is deliberately about *reachability*, not expected
returns.  A band is assigned from the NO token's own resting best ask.  When a
snapshot has no ask, assigning it a current price would require a proxy (or a
future observation), so the primary summaries use a strict last-observed-ask
cohort.  The cohort is labelled and its age is retained in every result.

All entry and exit numbers are walked from the archived order-book ladders.
Public trade prices and ``prices-history.p`` are not used to create a quote.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.entry_accessibility import (
    _levels,
    _no_quote_at,
    _number_summary,
    _peak_phase,
    _top,
)
from poly_weather.execution_cost import (
    ExecutionCostEstimate,
    estimate_execution_by_shares,
    estimate_execution_cost,
)
from poly_weather.no_forward import wilson_interval

PRICE_BANDS: tuple[tuple[str, Decimal | None, Decimal | None], ...] = (
    ("<0.30", None, Decimal("0.30")),
    ("0.30–0.50", Decimal("0.30"), Decimal("0.50")),
    ("0.50–0.70", Decimal("0.50"), Decimal("0.70")),
    ("0.70–0.85", Decimal("0.70"), Decimal("0.85")),
    ("0.85–0.95", Decimal("0.85"), Decimal("0.95")),
    ("0.95–0.99", Decimal("0.95"), Decimal("0.99")),
    ("≥0.99", Decimal("0.99"), None),
)
PRICE_BAND_LABELS = tuple(row[0] for row in PRICE_BANDS)
NO_ASK_LABEL = "no_ask"
NO_PRIOR_ASK_LABEL = "no_ask_no_prior"
ENTRY_SIZES_USD: tuple[Decimal, ...] = (
    Decimal("20"),
    Decimal("50"),
    Decimal("100"),
    Decimal("150"),
    Decimal("200"),
)
DIAGNOSTIC_DEPTH_SIZE_USD = Decimal("1000")


def _size_key(size: Decimal) -> str:
    value = format(size, "f")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    return value or "0"


def price_band_for_ask(price: Decimal | float | str | None) -> str | None:
    """Return the inclusive/lower-exclusive band for a true NO best ask."""
    if price is None:
        return None
    try:
        value = Decimal(str(price))
    except (ArithmeticError, ValueError):
        return None
    if value < 0:
        return None
    for label, lower, upper in PRICE_BANDS:
        if (lower is None or value >= lower) and (upper is None or value < upper):
            return label
    return None


def _number(value: Any, digits: int = 4) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def _pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.1%}"


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


def _rate_text(value: Mapping[str, Any]) -> str:
    return (
        f"{int(value['count'])}/{int(value['sample_count'])} ({_pct(value['rate'])}; "
        f"Wilson 95% {_pct(value['wilson_low'])}–{_pct(value['wilson_high'])})"
    )


def _unreliable(value: Mapping[str, Any]) -> str:
    return "；n<30，统计不可靠" if value.get("statistically_unreliable") else ""


def _ordered_levels(value: Any, *, reverse: bool = False) -> tuple[tuple[Decimal, Decimal], ...]:
    levels = _levels(value)
    return tuple(sorted(levels, key=lambda row: row[0], reverse=reverse))


def _level_amount_summary(levels: Sequence[tuple[Decimal, Decimal]]) -> dict[str, Any]:
    if not levels:
        return {
            "level_count": _number_summary([]),
            "top_shares": _number_summary([]),
            "top_usd": _number_summary([]),
            "first_three_shares": _number_summary([]),
            "first_three_usd": _number_summary([]),
        }
    first_three = levels[:3]
    top_price, top_size = levels[0]
    return {
        "level_count": _number_summary([float(len(levels))]),
        "top_shares": _number_summary([float(top_size)]),
        "top_usd": _number_summary([float(top_price * top_size)]),
        "first_three_shares": _number_summary(
            [float(sum((size for _price, size in first_three), start=Decimal(0)))]
        ),
        "first_three_usd": _number_summary(
            [float(sum((price * size for price, size in first_three), start=Decimal(0)))]
        ),
    }


def _levels_summary(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return _number_summary(values)


def _estimate_payload(estimate: ExecutionCostEstimate | None) -> dict[str, Any]:
    if estimate is None:
        return {
            "complete": False,
            "filled_fraction": 0.0,
            "filled_usd": None,
            "filled_shares": None,
            "average": None,
            "slippage": None,
            "fee_usdc": None,
            "fee_per_share": None,
        }
    return {
        "complete": estimate.filled_fraction >= 1,
        "filled_fraction": estimate.filled_fraction,
        "filled_usd": float(estimate.filled_usd),
        "filled_shares": float(estimate.filled_shares),
        "average": float(estimate.average_fill_price),
        "slippage": float(estimate.slippage_vs_top),
        "fee_usdc": float(estimate.fee_usdc),
        "fee_per_share": float(estimate.fee_per_share),
    }


def _path_payload(
    entry: ExecutionCostEstimate | None,
    exit: ExecutionCostEstimate | None,
) -> dict[str, Any]:
    entry_payload = _estimate_payload(entry)
    exit_payload = _estimate_payload(exit)
    round_trip = bool(entry_payload["complete"] and exit_payload["complete"])
    hurdle = None
    if round_trip:
        hurdle = sum(
            Decimal(str(entry_payload[name])) + Decimal(str(exit_payload[name]))
            for name in ("slippage", "fee_per_share")
        )
    return {
        "entry": entry_payload,
        "exit": exit_payload,
        "round_trip_complete": round_trip,
        # This is the additional gross price movement required to cover costs
        # when the top quote is used as the gross-price baseline.  It is not a
        # realized PnL or a claim about a future exit book.
        "gross_price_move_hurdle": float(hurdle) if hurdle is not None else None,
    }


def _record_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    sizes_usd: Sequence[Decimal],
) -> dict[str, Any]:
    count = len(rows)
    asks = [row for row in rows if row.get("no_ask") is not None]
    level_metrics = {
        "level_count": _levels_summary(asks, "no_ask_level_count"),
        "top_shares": _levels_summary(asks, "no_top_ask_shares"),
        "top_usd": _levels_summary(asks, "no_top_ask_usd"),
        "first_three_shares": _levels_summary(asks, "no_first_three_ask_shares"),
        "first_three_usd": _levels_summary(asks, "no_first_three_ask_usd"),
    }
    spread_values = [float(row["no_spread"]) for row in rows if row.get("no_spread") is not None]

    size_summaries: list[dict[str, Any]] = []
    for size in (*sizes_usd, DIAGNOSTIC_DEPTH_SIZE_USD):
        key = _size_key(size)
        no_rows = [row["fills"][key]["no_buy"] for row in rows]
        yes_rows = [row["fills"][key]["yes_sell"] for row in rows]

        def path_summary(path_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            complete = [row for row in path_rows if row["entry"]["complete"]]
            round_trip = [row for row in path_rows if row["round_trip_complete"]]
            return {
                "entry_full_fill": _rate(len(complete), len(path_rows)),
                "round_trip_full": _rate(len(round_trip), len(path_rows)),
                "entry_average": _number_summary(
                    [float(row["entry"]["average"]) for row in complete]
                ),
                "entry_slippage": _number_summary(
                    [float(row["entry"]["slippage"]) for row in complete]
                ),
                "entry_fee_per_share": _number_summary(
                    [float(row["entry"]["fee_per_share"]) for row in complete]
                ),
                "exit_average": _number_summary(
                    [float(row["exit"]["average"]) for row in round_trip]
                ),
                "exit_slippage": _number_summary(
                    [float(row["exit"]["slippage"]) for row in round_trip]
                ),
                "exit_fee_per_share": _number_summary(
                    [float(row["exit"]["fee_per_share"]) for row in round_trip]
                ),
                "gross_price_move_hurdle": _number_summary(
                    [float(row["gross_price_move_hurdle"]) for row in round_trip]
                ),
            }

        no_summary = path_summary(no_rows)
        yes_summary = path_summary(yes_rows)
        size_summaries.append(
            {
                "size_usd": float(size),
                "diagnostic_only": size == DIAGNOSTIC_DEPTH_SIZE_USD,
                "no_buy": no_summary,
                "yes_sell": yes_summary,
                "no_minus_yes_entry_rate": (
                    no_summary["entry_full_fill"]["rate"]
                    - yes_summary["entry_full_fill"]["rate"]
                    if no_summary["entry_full_fill"]["rate"] is not None
                    and yes_summary["entry_full_fill"]["rate"] is not None
                    else None
                ),
            }
        )
    source_counts = {
        source: _rate(
            sum(row.get("cohort_source") == source for row in rows),
            count,
        )
        for source in ("current_true_ask", "prior_true_ask", "no_prior_true_ask")
    }
    cohort_ages = [
        float(row["cohort_ask_age_minutes"])
        for row in rows
        if row.get("cohort_ask_age_minutes") is not None
    ]
    return {
        "snapshot_count": count,
        "event_count": len({str(row["event_slug"]) for row in rows}),
        "market_count": len({str(row["market_slug"]) for row in rows}),
        "statistically_unreliable": count < 30,
        "current_ask_present": _rate(len(asks), count),
        "current_no_ask": _rate(count - len(asks), count),
        "cohort_sources": source_counts,
        "cohort_ask_age_minutes": _number_summary(cohort_ages),
        "ladder": level_metrics,
        "no_spread": _number_summary(spread_values),
        "sizes": size_summaries,
    }


def _compact_time_summary(
    rows: Sequence[Mapping[str, Any]], *, sizes_usd: Sequence[Decimal]
) -> dict[str, Any]:
    count = len(rows)
    output: dict[str, Any] = {
        "snapshot_count": count,
        "statistically_unreliable": count < 30,
        "current_no_ask": _rate(sum(row["no_ask"] is None for row in rows), count),
    }
    for size in (sizes_usd[0], sizes_usd[-1]):
        key = _size_key(size)
        no = [row["fills"][key]["no_buy"] for row in rows]
        yes = [row["fills"][key]["yes_sell"] for row in rows]
        output[f"no_buy_{key}"] = _rate(
            sum(row["entry"]["complete"] for row in no), count
        )
        output[f"yes_sell_{key}"] = _rate(
            sum(row["entry"]["complete"] for row in yes), count
        )
    return output


def _downsample_hour(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    selected: dict[tuple[str, str, str, int], Mapping[str, Any]] = {}
    for row in sorted(records, key=lambda value: str(value["quote_at"])):
        key = (
            str(row["station_id"]),
            str(row["market_slug"]),
            str(row["local_date"]),
            int(row["local_hour"]),
        )
        selected.setdefault(key, row)
    return list(selected.values())


def _time_summaries(
    records: Sequence[Mapping[str, Any]], *, sizes_usd: Sequence[Decimal]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    downsampled = _downsample_hour(records)
    hourly_groups: dict[tuple[str, int, str], list[Mapping[str, Any]]] = defaultdict(list)
    phase_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in downsampled:
        hourly_groups[
            (str(row["station_id"]), int(row["local_hour"]), str(row["cohort_ask_band"]))
        ].append(row)
        phase_groups[
            (str(row["station_id"]), str(row["peak_phase"]), str(row["cohort_ask_band"]))
        ].append(row)
        if row.get("candidate_pre_peak_0-4h"):
            phase_groups[
                (
                    str(row["station_id"]),
                    "candidate_pre_peak_0-4h",
                    str(row["cohort_ask_band"]),
                )
            ].append(row)

    hourly = [
        {
            "station_id": station,
            "local_hour": hour,
            "ask_band": band,
            **_compact_time_summary(rows, sizes_usd=sizes_usd),
        }
        for (station, hour, band), rows in sorted(hourly_groups.items())
    ]
    phases = [
        {
            "station_id": station,
            "peak_phase": phase,
            "ask_band": band,
            **_compact_time_summary(rows, sizes_usd=sizes_usd),
        }
        for (station, phase, band), rows in sorted(phase_groups.items())
    ]
    return hourly, phases


def analyze_price_band_accessibility(
    pairs: Sequence[Mapping[str, Any]],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]],
    typical_peak_minutes_by_station: Mapping[str, int],
    sizes_usd: Sequence[Decimal] = ENTRY_SIZES_USD,
) -> dict[str, Any]:
    """Analyze true NO ask bands and the equivalent YES-sell path.

    The fallback cohort for an empty ask is always the last true ask at a
    strictly earlier timestamp for the same asset.  Thus a later ask can never
    classify an earlier empty snapshot.
    """
    records: list[dict[str, Any]] = []
    last_ask_by_asset: dict[str, tuple[datetime, str]] = {}
    ordered_pairs = sorted(
        pairs,
        key=lambda pair: (
            _no_quote_at(pair) or datetime.min.replace(tzinfo=UTC),
            str(pair.get("market_slug") or ""),
        ),
    )
    for pair in ordered_pairs:
        event_slug = str(pair.get("event_slug") or "")
        metadata = event_metadata.get(event_slug)
        no = pair.get("no")
        yes = pair.get("yes")
        quote_at = _no_quote_at(pair)
        if (
            metadata is None
            or not isinstance(no, Mapping)
            or not isinstance(yes, Mapping)
            or quote_at is None
        ):
            continue
        asset_id = str(no.get("asset_id") or "")
        if not asset_id:
            continue
        try:
            timezone = ZoneInfo(str(metadata["timezone"]))
            target_date = date.fromisoformat(str(metadata["target_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        local = quote_at.astimezone(timezone)
        no_bids = _ordered_levels(no.get("bids"), reverse=True)
        no_asks = _ordered_levels(no.get("asks"))
        yes_bids = _ordered_levels(yes.get("bids"), reverse=True)
        yes_asks = _ordered_levels(yes.get("asks"))
        no_bid = _top(no_bids, bids=True)
        no_ask = _top(no_asks, bids=False)
        yes_bid = _top(yes_bids, bids=True)
        yes_ask = _top(yes_asks, bids=False)
        current_band = price_band_for_ask(no_ask)
        prior = last_ask_by_asset.get(asset_id)
        if current_band is not None:
            cohort_band = current_band
            cohort_source = "current_true_ask"
            cohort_age_minutes = 0.0
        elif prior is not None and prior[0] < quote_at:
            cohort_band = prior[1]
            cohort_source = "prior_true_ask"
            cohort_age_minutes = (quote_at - prior[0]).total_seconds() / 60
        else:
            cohort_band = NO_PRIOR_ASK_LABEL
            cohort_source = "no_prior_true_ask"
            cohort_age_minutes = None

        fills: dict[str, dict[str, Any]] = {}
        for size in (*sizes_usd, DIAGNOSTIC_DEPTH_SIZE_USD):
            key = _size_key(size)
            no_entry = estimate_execution_cost(no_asks, size, "buy")
            no_exit = (
                estimate_execution_by_shares(no_bids, no_entry.filled_shares, "sell")
                if no_entry is not None and no_entry.filled_fraction >= 1
                else None
            )
            yes_entry = estimate_execution_cost(yes_bids, size, "sell")
            yes_exit = (
                estimate_execution_by_shares(yes_asks, yes_entry.filled_shares, "buy")
                if yes_entry is not None and yes_entry.filled_fraction >= 1
                else None
            )
            fills[key] = {
                "no_buy": _path_payload(no_entry, no_exit),
                "yes_sell": _path_payload(yes_entry, yes_exit),
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
        first_three = no_asks[:3]
        records.append(
            {
                "quote_at": quote_at.isoformat(),
                "event_slug": event_slug,
                "market_slug": str(pair.get("market_slug") or ""),
                "asset_id": asset_id,
                "station_id": str(metadata["station_id"]),
                "target_date": target_date.isoformat(),
                "local_date": local.date().isoformat(),
                "local_hour": local.hour,
                "hours_to_typical_peak": hours_to_peak,
                "peak_phase": _peak_phase(hours_to_peak),
                "candidate_pre_peak_0-4h": hours_to_peak is not None
                and 0 <= hours_to_peak <= 4,
                "no_bid": float(no_bid) if no_bid is not None else None,
                "no_ask": float(no_ask) if no_ask is not None else None,
                "yes_bid": float(yes_bid) if yes_bid is not None else None,
                "yes_ask": float(yes_ask) if yes_ask is not None else None,
                "no_spread": float(no_ask - no_bid)
                if no_ask is not None and no_bid is not None
                else None,
                "yes_spread": float(yes_ask - yes_bid)
                if yes_ask is not None and yes_bid is not None
                else None,
                "current_ask_band": current_band or NO_ASK_LABEL,
                "cohort_ask_band": cohort_band,
                "cohort_source": cohort_source,
                "cohort_ask_age_minutes": cohort_age_minutes,
                "no_ask_level_count": len(no_asks) if no_asks else None,
                "no_top_ask_shares": float(no_asks[0][1]) if no_asks else None,
                "no_top_ask_usd": float(no_asks[0][0] * no_asks[0][1]) if no_asks else None,
                "no_first_three_ask_shares": (
                    float(sum((size for _price, size in first_three), start=Decimal(0)))
                    if no_asks
                    else None
                ),
                "no_first_three_ask_usd": (
                    float(sum((price * size for price, size in first_three), start=Decimal(0)))
                    if no_asks
                    else None
                ),
                "fills": fills,
            }
        )
        if current_band is not None and (prior is None or quote_at > prior[0]):
            last_ask_by_asset[asset_id] = (quote_at, current_band)

    scope_records: dict[str, list[Mapping[str, Any]]] = {
        "all_ten_cities": records,
        "KLAX": [row for row in records if row["station_id"] == "KLAX"],
        "KLGA": [row for row in records if row["station_id"] == "KLGA"],
        "other_eight_cities": [
            row for row in records if row["station_id"] not in {"KLAX", "KLGA"}
        ],
    }
    band_summaries = {
        scope: {
            band: _record_summary(
                [row for row in rows if row["cohort_ask_band"] == band], sizes_usd=sizes_usd
            )
            for band in (*PRICE_BAND_LABELS, NO_PRIOR_ASK_LABEL)
            if any(row["cohort_ask_band"] == band for row in rows)
        }
        for scope, rows in scope_records.items()
    }
    current_summaries = {
        scope: {
            band: _record_summary(
                [row for row in rows if row["current_ask_band"] == band], sizes_usd=sizes_usd
            )
            for band in (*PRICE_BAND_LABELS, NO_ASK_LABEL)
            if any(row["current_ask_band"] == band for row in rows)
        }
        for scope, rows in scope_records.items()
    }
    hourly, phases = _time_summaries(records, sizes_usd=sizes_usd)
    cutoff = max((row["quote_at"] for row in records), default=None)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": cutoff,
        "paired_snapshot_count": len(pairs),
        "analyzable_snapshot_count": len(records),
        "price_bands": [label for label, _lower, _upper in PRICE_BANDS],
        "sizes_usd": [float(size) for size in sizes_usd],
        "diagnostic_depth_size_usd": float(DIAGNOSTIC_DEPTH_SIZE_USD),
        "overall_summary": _record_summary(records, sizes_usd=sizes_usd),
        "band_summaries": band_summaries,
        "contemporaneous_band_summaries": current_summaries,
        "time_of_day": {
            "downsampled_snapshot_count": len(_downsample_hour(records)),
            "hourly": hourly,
            "peak_phase": phases,
            "semantics": (
                "one earliest eligible snapshot per market/local-date/local-hour; "
                "snapshots are correlated and are not independent trade attempts"
            ),
        },
        "semantics": {
            "ask_band": (
                "band is the true NO token best resting ask; empty asks are not assigned "
                "a current price or a proxy"
            ),
            "empty_ask_by_band": (
                "cohort summaries classify an empty snapshot by the last true ask for the "
                "same asset at a strictly earlier timestamp; no-prior rows stay separate"
            ),
            "strict_no_lookahead": (
                "YES/NO books are read at the paired NO quote timestamp; the fallback ask "
                "cohort uses only strictly earlier observations"
            ),
            "trade_price": "成交价不是可成交 ask；public trades are not used as book quotes",
            "no_entry": "NO entry consumes the archived NO ask ladder",
            "yes_entry": "the equivalent alternative sells YES into the archived YES bid ladder",
            "exit": (
                "same-book exits consume the opposite ladder for the exact shares bought or sold; "
                "future exit liquidity is not reconstructed"
            ),
            "net_price_spread": (
                "net(G) = gross price move G - entry slippage - exit slippage - both fees; "
                "the reported gross_price_move_hurdle is the cost term only, not realized PnL"
            ),
            "fee": "fee per share uses 0.05 × actual fill price × (1 - actual fill price)",
        },
        "execution_enabled": False,
        "records": records,
    }


def _short_rate(value: Mapping[str, Any]) -> str:
    if value.get("rate") is None:
        return "N/A"
    marker = "†" if value.get("statistically_unreliable") else ""
    return f"{float(value['rate']):.1%} ({float(value['wilson_low']):.1%}–{float(value['wilson_high']):.1%}){marker}"


def _size_row(summary: Mapping[str, Any], size: Decimal) -> Mapping[str, Any]:
    key = _size_key(size)
    for row in summary["sizes"]:
        if _size_key(Decimal(str(row["size_usd"]))) == key:
            return row
    raise KeyError(key)


def _cell_number_summary(summary: Mapping[str, Any], digits: int = 3) -> str:
    return (
        f"{_number(summary.get('p10'), digits)}/{_number(summary.get('p50'), digits)}"
        f"/{_number(summary.get('p90'), digits)}"
    )


def _scope_label(scope: str) -> str:
    return {"all_ten_cities": "十城", "other_eight_cities": "其余八城"}.get(scope, scope)


def _recommendation(scope: Mapping[str, Mapping[str, Any]]) -> str:
    candidates: list[tuple[float, float, str, Mapping[str, Any]]] = []
    # Keep the recommendation inside the genuinely intermediate range.  The
    # higher bands can have good entry rates but leave little gross-price room,
    # while the <0.30 band is often dominated by depth costs.
    for band in ("0.30–0.50", "0.50–0.70", "0.70–0.85"):
        summary = scope.get(band)
        if not summary or summary["snapshot_count"] < 30:
            continue
        size_200 = _size_row(summary, Decimal("200"))
        ask_rate = summary["current_ask_present"]["rate"] or 0.0
        fill_rate = size_200["no_buy"]["entry_full_fill"]["rate"] or 0.0
        hurdles = size_200["no_buy"]["gross_price_move_hurdle"]
        p90 = hurdles.get("p90")
        if ask_rate < 0.95 or fill_rate < 0.95 or p90 is None:
            continue
        # First require a well-populated ask and $200 entry book, then choose
        # the lowest observed p90 cost.  This is a screening recommendation,
        # not a trading signal or a claim of positive expected value.
        candidates.append((float(p90), -fill_rate, band, summary))
    if not candidates:
        return "没有 n≥30 的价格带可作稳定迁移建议；继续纸面测量。"
    _p90, _negative_fill, band, summary = min(candidates, key=lambda item: item[:2])
    return (
        f"按该范围的 ask 存在率、$200 完整买入/同簿往返率和 p50/p90 成本门槛作筛选，"
        f"候选重心是 `{band}`（n={summary['snapshot_count']}）。这只说明较容易进入，"
        "不说明价差为正；仍需把目标毛价差 G 与下表 hurdle 比较并观察逆转风险。"
    )


def render_price_band_accessibility_report(
    result: Mapping[str, Any], output_path: Path
) -> None:
    """Render the middle-band analysis with Wilson intervals and caveats."""
    lines = [
        "# 中间 NO 价位桶可达性分析",
        "",
        f"生成时间：{result['generated_at']}；订单簿数据截止：{result.get('data_cutoff') or 'N/A'}。"
        f"配对深度 {result['paired_snapshot_count']} 个，可分析 {result['analyzable_snapshot_count']} 个。",
        "输入默认排除了官方维护/故障和实测恢复质量窗；如命令调用者提供了排除计数，"
        f"本次排除配对 {result.get('quality_window_pairs_excluded', 0)} 个。",
        "价格分箱只使用**真实 NO best ask**。当前 asks 为空时没有当前价格，"
        "因此不能用 `p`、midpoint、`1−YES` 或成交价填补；主表把该快照归到同一 token 的**严格此前最后真实 ask cohort**，"
        "并单列 cohort 年龄与无此前 ask 样本。",
        "",
        "## 直接回答：是否应从尾桶迁移",
        "",
        "尾桶 `≥0.99` 的结构性空盘问题不会因换一种收益计算消失。中间价位只有在真实 ask 存在、"
        "指定金额能完整吃到深度、且目标毛价差覆盖双边成本时才有意义；**能进和划算是两件事**。",
        "按优先城市分别筛选：",
        f"- KLAX：{_recommendation(result['band_summaries'].get('KLAX', {}))}",
        f"- KLGA：{_recommendation(result['band_summaries'].get('KLGA', {}))}",
        f"- 十城对照：{_recommendation(result['band_summaries'].get('all_ten_cities', {}))}",
        f"十城所有可分析快照中，当前真实 NO asks 为空：{_rate_text(result['overall_summary']['current_no_ask'])}。",
        "这不是执行建议；`execution_enabled=false`，当前仍只有纸面研究依据。",
        "",
        "## Q1：按真实 NO ask 价格区间",
        "",
        "`ask cohort` 是当前真实 ask 所在区间，或当前空盘时严格此前最后真实 ask 所在区间。"
        "`无 ask` 是当前 asks 阶梯完全没有正数量挂单；不是把摘要 `best_ask=1.000` 当作挂单。",
        "",
        "先看不做任何 cohort 标签的同期事实（十城）：",
        "",
        "| 当前真实 NO ask | 快照数 | 占全部快照 | 当前无 ask |",
        "|---|---:|---:|---|",
    ]
    all_current = result["contemporaneous_band_summaries"].get("all_ten_cities", {})
    total_records = result["analyzable_snapshot_count"]
    for band in (*PRICE_BAND_LABELS, NO_ASK_LABEL):
        summary = all_current.get(band)
        if summary is None:
            continue
        share = _rate(summary["snapshot_count"], total_records)
        lines.append(
            f"| {band} | {summary['snapshot_count']} | {_short_rate(share)} | "
            f"{_rate_text(summary['current_no_ask'])}{_unreliable(summary['current_no_ask'])} |"
        )
    lines.append("")
    for scope in ("KLAX", "KLGA", "other_eight_cities", "all_ten_cities"):
        summaries = result["band_summaries"].get(scope, {})
        lines.extend(
            [
                f"### {_scope_label(scope)}",
                "",
                "| ask cohort | n | 当前有 ask | 当前无 ask | 此前 ask 年龄 p50/p90 分钟 | ask 档数 p10/p50/p90 | 顶层份数 p10/p50/p90 | 顶层名义 USD p10/p50/p90 | 前三档份数 p10/p50/p90 | 前三档名义 USD p10/p50/p90 | NO spread p50/p90 | $20/$50/$100/$150/$200 NO 完整买入率 |",
                "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for band in (*PRICE_BAND_LABELS, NO_PRIOR_ASK_LABEL):
            summary = summaries.get(band)
            if summary is None:
                continue
            rates = []
            for size in ENTRY_SIZES_USD:
                rates.append(_short_rate(_size_row(summary, size)["no_buy"]["entry_full_fill"]))
            lines.append(
                f"| {band} | {summary['snapshot_count']} | {_rate_text(summary['current_ask_present'])} | "
                f"{_rate_text(summary['current_no_ask'])} | "
                f"{_number(summary['cohort_ask_age_minutes']['p50'], 1)}/{_number(summary['cohort_ask_age_minutes']['p90'], 1)} | "
                f"{_cell_number_summary(summary['ladder']['level_count'], 1)} | "
                f"{_cell_number_summary(summary['ladder']['top_shares'], 2)} | "
                f"{_cell_number_summary(summary['ladder']['top_usd'], 2)} | "
                f"{_cell_number_summary(summary['ladder']['first_three_shares'], 2)} | "
                f"{_cell_number_summary(summary['ladder']['first_three_usd'], 2)} | "
                f"{_number(summary['no_spread']['p50'], 4)}/{_number(summary['no_spread']['p90'], 4)} | "
                f"{' / '.join(rates)}{_unreliable(summary['current_no_ask'])} |"
            )
        lines.append("")

    lines.extend(
        [
            "### 深度均价、相对顶层滑点和手续费（$200 以内）",
            "",
            "NO 列是买 NO 吃 ask；YES 列是等价的卖 YES 吃 bid。均价、滑点和手续费只在该路径完整成交的样本上汇总；"
            "手续费使用实际深度成交价的 `0.05 × p × (1−p)`，不是顶层价。",
            "",
            "| 范围 | ask cohort | 金额 | NO 均价 mean/p50 | NO 入场滑点 p50/p90 | NO 入场费/份 p50 | YES 均价 mean/p50 | YES 入场滑点 p50/p90 | YES 入场费/份 p50 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scope in ("KLAX", "KLGA", "other_eight_cities", "all_ten_cities"):
        for band in PRICE_BAND_LABELS:
            summary = result["band_summaries"].get(scope, {}).get(band)
            if summary is None:
                continue
            for size in ENTRY_SIZES_USD:
                row = _size_row(summary, size)
                no = row["no_buy"]
                yes = row["yes_sell"]
                lines.append(
                    f"| {_scope_label(scope)} | {band} | ${int(size)} | "
                    f"{_number(no['entry_average']['mean'])}/{_number(no['entry_average']['p50'])} | "
                    f"{_number(no['entry_slippage']['p50'])}/{_number(no['entry_slippage']['p90'])} | "
                    f"{_number(no['entry_fee_per_share']['p50'])} | "
                    f"{_number(yes['entry_average']['mean'])}/{_number(yes['entry_average']['p50'])} | "
                    f"{_number(yes['entry_slippage']['p50'])}/{_number(yes['entry_slippage']['p90'])} | "
                    f"{_number(yes['entry_fee_per_share']['p50'])} |"
                )
    lines.extend(
        [
            "",
            "$1000 仅保留为盘口总厚度诊断，不参与仓位建议。",
            "",
            "## Q2：直接买 NO 与卖 YES 的可达性",
            "",
            "二者必须按各自真实梯度比较，不能先验假设 NO 更容易。下表每格为 `NO 买入完整率 / YES 卖出完整率`，"
            "各自均带 Wilson 95%；† 表示 n<30、统计不可靠。它们是同一时刻的两条路径，"
            "不是用互补价格拼出的单一订单簿。",
            "",
            "| 范围 | ask cohort | $20 | $50 | $100 | $150 | $200 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for scope in ("KLAX", "KLGA", "other_eight_cities", "all_ten_cities"):
        for band in PRICE_BAND_LABELS:
            summary = result["band_summaries"].get(scope, {}).get(band)
            if summary is None:
                continue
            cells = []
            for size in ENTRY_SIZES_USD:
                row = _size_row(summary, size)
                cells.append(
                    f"{_short_rate(row['no_buy']['entry_full_fill'])} / "
                    f"{_short_rate(row['yes_sell']['entry_full_fill'])}"
                )
            lines.append(f"| {_scope_label(scope)} | {band} | " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "## Q3：价差策略的净价差门槛",
            "",
            "对任意目标毛价差 `G`，这里使用 `净价差 = G − 入场滑点 − 出场滑点 − 两笔手续费`。"
            "表中的 `hurdle` 是成本项 p50/p90，所以 `G > hurdle` 才只是成本意义上的正值；"
            "它不是已实现 PnL，也没有假设未来出场簿等于当前簿。成交价不是可成交 ask，成交带不能替代这些深度结果。",
            "† 表示对应路径样本 n<30、统计不可靠。",
            "",
            "| 范围 | ask cohort | 金额 | NO 双边完整率 | NO hurdle p50/p90 | YES 双边完整率 | YES hurdle p50/p90 |",
            "|---|---|---:|---|---:|---|---:|",
        ]
    )
    for scope in ("KLAX", "KLGA", "other_eight_cities", "all_ten_cities"):
        for band in PRICE_BAND_LABELS:
            summary = result["band_summaries"].get(scope, {}).get(band)
            if summary is None:
                continue
            for size in ENTRY_SIZES_USD:
                row = _size_row(summary, size)
                no = row["no_buy"]
                yes = row["yes_sell"]
                lines.append(
                    f"| {_scope_label(scope)} | {band} | ${int(size)} | "
                    f"{_short_rate(no['round_trip_full'])} | "
                    f"{_number(no['gross_price_move_hurdle']['p50'])}/{_number(no['gross_price_move_hurdle']['p90'])} | "
                    f"{_short_rate(yes['round_trip_full'])} | "
                    f"{_number(yes['gross_price_move_hurdle']['p50'])}/{_number(yes['gross_price_move_hurdle']['p90'])} |"
                )
    lines.extend(
        [
            "",
            "没有给定固定的目标 G，因此本报告不把任何区间宣称为正期望；逆转/跳空风险也未被这张当前簿成本表消除。"
            "依赖真实入场 ask × 已结算结果的历史 PnL 仍因重叠 N=0 而为 N/A。",
            "",
            "## Q4：本地小时与典型高点阶段",
            "",
            "小时表先按每个 market/local-date/local-hour 取最早合格快照，再按 ask cohort 分组，避免把连续 tick 当成独立交易。"
            "候选窗口用典型高点前 0–4 小时的 phase 标签；n<30 的行明确统计不可靠，不跨本质不同的时段合并。"
            "本归档没有直接记录 warming_window_no 触发事件，因此这里的 phase 是时间代理，不是触发样本。",
            "",
            "| 城市 | 本地小时 | ask cohort | n | 当前无 ask | $20 NO/YES | $200 NO/YES |",
            "|---|---:|---|---:|---|---|---|",
        ]
    )
    for row in result["time_of_day"]["hourly"]:
        if row["station_id"] not in {"KLAX", "KLGA"}:
            continue
        lines.append(
            f"| {row['station_id']} | {row['local_hour']:02d} | {row['ask_band']} | {row['snapshot_count']} | "
            f"{_rate_text(row['current_no_ask'])}{_unreliable(row['current_no_ask'])} | "
            f"{_short_rate(row['no_buy_20'])}/{_short_rate(row['yes_sell_20'])} | "
            f"{_short_rate(row['no_buy_200'])}/{_short_rate(row['yes_sell_200'])} |"
        )
    lines.extend(
        [
            "",
            "### 典型高点阶段",
            "",
            "| 城市 | 阶段 | ask cohort | n | 当前无 ask | $20 NO/YES | $200 NO/YES |",
            "|---|---|---|---:|---|---|---|",
        ]
    )
    for row in result["time_of_day"]["peak_phase"]:
        if row["station_id"] not in {"KLAX", "KLGA"}:
            continue
        lines.append(
            f"| {row['station_id']} | {row['peak_phase']} | {row['ask_band']} | {row['snapshot_count']} | "
            f"{_rate_text(row['current_no_ask'])}{_unreliable(row['current_no_ask'])} | "
            f"{_short_rate(row['no_buy_20'])}/{_short_rate(row['yes_sell_20'])} | "
            f"{_short_rate(row['no_buy_200'])}/{_short_rate(row['yes_sell_200'])} |"
        )
    lines.extend(
        [
            "",
            "候选高点前 0–4 小时在 KLAX/KLGA 的中间 ask cohort 多数 n<30，"
            "因此当前数据不能证明“一天中某个小时稳定可进入”的模式；完整 phase 明细仍保留在分析 JSON。",
        ]
    )
    lines.extend(
        [
            "",
            "## 口径边界",
            "",
            "- 分箱严格使用真实 NO resting ask；空 asks 的 cohort 只是严格此前真实 ask 的标签，不是当前价格估计。",
            "- `data-api/trades` 的成交价是真实成交，但成交价不是可成交 ask，不能反推 resting bid/ask 或深度。",
            "- 入口和退出均为指定金额/指定份数的深度行走；顶层 ask/bid 只用于计算相对滑点。",
            "- 所有比率带 Wilson 95%；任何 n<30 的分层只作方向性观察，统计不可靠。",
            "- 维护/恢复窗口已默认排除；没有因为维护而停止采集，也没有放宽 fail-closed 或执行边界。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
