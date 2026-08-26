"""Analysis of executable NO-side quotes from archived full order books."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.archive_io import open_jsonl_text
from poly_weather.domain import SettlementSpec
from poly_weather.execution_cost import estimate_execution_cost
from poly_weather.intraday_reversal import TemperatureObservation
from poly_weather.no_forward import wilson_interval
from poly_weather.polymarket_status import (
    load_quality_windows,
    market_record_is_analysis_eligible,
)
from poly_weather.signal_engine import physical_bucket_state

_EVENT_DATE_SUFFIX = re.compile(
    r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$"
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


def archived_event_metadata(
    event_slugs: Sequence[str],
    settlement_specs: Sequence[SettlementSpec],
) -> dict[str, dict[str, str]]:
    """Rebuild verified event metadata from immutable archive identities.

    Unknown or ambiguous slugs fail closed.  In particular, this deliberately does
    not depend on the supervisor's rotating current-event configuration.
    """
    output: dict[str, dict[str, str]] = {}
    for event_slug in sorted(set(event_slugs)):
        matches = [
            spec
            for spec in settlement_specs
            if re.fullmatch(spec.market_slug_pattern, event_slug) is not None
            and spec.station_id
            and spec.timezone
        ]
        date_match = _EVENT_DATE_SUFFIX.search(event_slug)
        if len(matches) != 1 or date_match is None:
            continue
        month = _MONTHS.get(date_match.group("month"))
        if month is None:
            continue
        try:
            target_date = date(
                int(date_match.group("year")),
                month,
                int(date_match.group("day")),
            )
        except ValueError:
            continue
        spec = matches[0]
        output[event_slug] = {
            "station_id": str(spec.station_id),
            "timezone": str(spec.timezone),
            "target_date": target_date.isoformat(),
        }
    return output


def _levels(rows: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(rows, list):
        return ()
    return tuple(
        (str(row["price"]), str(row["size"]))
        for row in rows
        if isinstance(row, dict) and row.get("price") is not None and row.get("size") is not None
    )


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    value = str(row.get("market_slug") or "")
    base, separator, outcome = value.rpartition(":")
    if not separator or outcome.casefold() not in {"yes", "no"}:
        return None
    event_slug = base.split("/", 1)[0]
    return event_slug, base, outcome.casefold()


def paired_book_snapshots(
    checkpoint_paths: Sequence[Path],
    *,
    minimum_interval: timedelta = timedelta(minutes=5),
    maximum_side_age: timedelta = timedelta(minutes=5),
    exclude_upstream_degraded: bool = True,
) -> list[dict[str, Any]]:
    """Pair YES/NO books without using a future update from either side."""
    rows: list[dict[str, Any]] = []
    quality_windows = ()
    if checkpoint_paths:
        quality_windows = load_quality_windows(
            checkpoint_paths[0].parents[3] / "runtime" / "polymarket_quality_windows.json"
        )
    for path in checkpoint_paths:
        if not path.exists():
            continue
        with open_jsonl_text(path) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    timestamp = datetime.fromisoformat(str(row["received_at"])).astimezone(UTC)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if exclude_upstream_degraded and not market_record_is_analysis_eligible(
                    row, timestamp, quality_windows
                ):
                    continue
                if row.get("book_complete") and isinstance(row.get("bids"), list) and isinstance(
                    row.get("asks"), list
                ):
                    row["_timestamp"] = timestamp
                    rows.append(row)
    rows.sort(key=lambda row: row["_timestamp"])
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    last_emitted: dict[str, datetime] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        identity = _identity(row)
        if identity is None:
            continue
        event_slug, base, outcome = identity
        latest[(base, outcome)] = row
        yes = latest.get((base, "yes"))
        no = latest.get((base, "no"))
        if yes is None or no is None:
            continue
        timestamp = row["_timestamp"]
        if abs(yes["_timestamp"] - no["_timestamp"]) > maximum_side_age:
            continue
        if base in last_emitted and timestamp - last_emitted[base] < minimum_interval:
            continue
        last_emitted[base] = timestamp
        output.append(
            {
                "observed_at": timestamp,
                "event_slug": event_slug,
                "market_slug": base,
                "yes": yes,
                "no": no,
                "side_age_seconds": abs(
                    (yes["_timestamp"] - no["_timestamp"]).total_seconds()
                ),
            }
        )
    return output


def _top(levels: tuple[tuple[str, str], ...], *, bids: bool) -> Decimal | None:
    prices = [Decimal(price) for price, size in levels if Decimal(size) > 0]
    return (max(prices) if bids else min(prices)) if prices else None


def analyze_real_no_books(
    pairs: Sequence[Mapping[str, Any]],
    *,
    sizes_usd: Sequence[Decimal] = (Decimal("50"), Decimal("200"), Decimal("1000")),
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for pair in pairs:
        yes_bids = _levels(pair["yes"].get("bids"))
        yes_asks = _levels(pair["yes"].get("asks"))
        no_bids = _levels(pair["no"].get("bids"))
        no_asks = _levels(pair["no"].get("asks"))
        yes_bid = _top(yes_bids, bids=True)
        yes_ask = _top(yes_asks, bids=False)
        no_bid = _top(no_bids, bids=True)
        no_ask = _top(no_asks, bids=False)
        if None in {yes_bid, yes_ask, no_bid, no_ask}:
            continue
        fills: dict[str, Any] = {}
        for size in sizes_usd:
            buy = estimate_execution_cost(no_asks, size, "buy")
            sell = estimate_execution_cost(no_bids, size, "sell")
            fills[str(size)] = {
                "buy_no_average": float(buy.average_fill_price) if buy else None,
                "buy_no_taker_fee_usdc": float(buy.fee_usdc) if buy else None,
                "buy_no_fee_per_share": float(buy.fee_per_share) if buy else None,
                "buy_no_all_in_per_share": (
                    float(buy.average_fill_price + buy.fee_per_share) if buy else None
                ),
                "buy_no_filled_fraction": buy.filled_fraction if buy else 0.0,
                "sell_no_average": float(sell.average_fill_price) if sell else None,
                "sell_no_taker_fee_usdc": float(sell.fee_usdc) if sell else None,
                "sell_no_fee_per_share": float(sell.fee_per_share) if sell else None,
                "sell_no_net_per_share": (
                    float(sell.average_fill_price - sell.fee_per_share) if sell else None
                ),
                "sell_no_filled_fraction": sell.filled_fraction if sell else 0.0,
            }
        yes_last = pair["yes"].get("last_trade_price")
        no_proxy = None if yes_last is None else Decimal("1") - Decimal(str(yes_last))
        records.append(
            {
                "observed_at": pair["observed_at"].isoformat(),
                "event_slug": pair["event_slug"],
                "market_slug": pair["market_slug"],
                "yes_bid": float(yes_bid),
                "yes_ask": float(yes_ask),
                "no_bid": float(no_bid),
                "no_ask": float(no_ask),
                "yes_spread": float(yes_ask - yes_bid),
                "no_spread": float(no_ask - no_bid),
                "buy_no_vs_sell_yes_gap": float(no_ask + yes_bid - Decimal("1")),
                "buy_yes_vs_sell_no_gap": float(yes_ask + no_bid - Decimal("1")),
                "no_proxy_from_yes_last": float(no_proxy) if no_proxy is not None else None,
                "no_ask_minus_proxy": float(no_ask - no_proxy)
                if no_proxy is not None
                else None,
                "no_bid_minus_proxy": float(no_bid - no_proxy)
                if no_proxy is not None
                else None,
                "side_age_seconds": pair["side_age_seconds"],
                "fills": fills,
            }
        )
    complement_gaps = [
        abs(row[name])
        for row in records
        for name in ("buy_no_vs_sell_yes_gap", "buy_yes_vs_sell_no_gap")
    ]
    proxy_rows = [row for row in records if row["no_proxy_from_yes_last"] is not None]
    size_summaries = []
    for size in sizes_usd:
        key = str(size)
        buy = [row["fills"][key] for row in records]
        buy_successes = sum(row["buy_no_filled_fraction"] >= 1 for row in buy)
        sell_successes = sum(row["sell_no_filled_fraction"] >= 1 for row in buy)
        buy_interval = wilson_interval(buy_successes, len(buy)) if buy else None
        sell_interval = wilson_interval(sell_successes, len(buy)) if buy else None
        size_summaries.append(
            {
                "size_usd": float(size),
                "sample_count": len(buy),
                "buy_no_fully_filled_count": buy_successes,
                "buy_no_fully_filled_rate": fmean(
                    row["buy_no_filled_fraction"] >= 1 for row in buy
                )
                if buy
                else None,
                "buy_no_fully_filled_wilson_low": (
                    buy_interval[0] if buy_interval else None
                ),
                "buy_no_fully_filled_wilson_high": (
                    buy_interval[1] if buy_interval else None
                ),
                "sell_no_fully_filled_count": sell_successes,
                "sell_no_fully_filled_rate": fmean(
                    row["sell_no_filled_fraction"] >= 1 for row in buy
                )
                if buy
                else None,
                "sell_no_fully_filled_wilson_low": (
                    sell_interval[0] if sell_interval else None
                ),
                "sell_no_fully_filled_wilson_high": (
                    sell_interval[1] if sell_interval else None
                ),
                "statistically_unreliable": len(buy) < 30,
                "mean_buy_no_average": fmean(
                    row["buy_no_average"] for row in buy if row["buy_no_average"] is not None
                )
                if any(row["buy_no_average"] is not None for row in buy)
                else None,
                "mean_buy_no_all_in_per_share": fmean(
                    row["buy_no_all_in_per_share"]
                    for row in buy
                    if row["buy_no_all_in_per_share"] is not None
                )
                if any(row["buy_no_all_in_per_share"] is not None for row in buy)
                else None,
            }
        )
    over_2c_count = sum(gap > 0.02 for gap in complement_gaps)
    over_2c_interval = (
        wilson_interval(over_2c_count, len(complement_gaps)) if complement_gaps else None
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "paired_snapshot_count": len(pairs),
        "usable_snapshot_count": len(records),
        "event_count": len({row["event_slug"] for row in records}),
        "market_count": len({row["market_slug"] for row in records}),
        "mean_yes_spread": fmean(row["yes_spread"] for row in records) if records else None,
        "mean_no_spread": fmean(row["no_spread"] for row in records) if records else None,
        "complement_gap_p50": median(complement_gaps) if complement_gaps else None,
        "complement_gap_max": max(complement_gaps, default=None),
        "complement_gap_over_2c_rate": fmean(gap > 0.02 for gap in complement_gaps)
        if complement_gaps
        else None,
        "complement_gap_over_2c_count": over_2c_count,
        "complement_gap_sample_count": len(complement_gaps),
        "complement_gap_over_2c_wilson_low": (
            over_2c_interval[0] if over_2c_interval else None
        ),
        "complement_gap_over_2c_wilson_high": (
            over_2c_interval[1] if over_2c_interval else None
        ),
        "proxy_comparable_count": len(proxy_rows),
        "mean_no_ask_minus_proxy": fmean(row["no_ask_minus_proxy"] for row in proxy_rows)
        if proxy_rows
        else None,
        "mean_no_bid_minus_proxy": fmean(row["no_bid_minus_proxy"] for row in proxy_rows)
        if proxy_rows
        else None,
        "size_summaries": size_summaries,
        "records": records,
        "historical_settled_depth_overlap_count": 0,
        "execution_enabled": False,
    }


def _bucket_upper_and_unit(market_slug: str) -> tuple[int | None, str] | None:
    import re

    slug = market_slug.lower()
    if match := re.search(r"-(\d+)forbelow$", slug):
        return int(match.group(1)), "fahrenheit"
    if match := re.search(r"-\d+-(\d+)f$", slug):
        return int(match.group(1)), "fahrenheit"
    if re.search(r"-\d+forhigher$", slug):
        return None, "fahrenheit"
    if match := re.search(r"-(-?\d+)c(?:or)?below$", slug):
        return int(match.group(1)), "celsius"
    if match := re.search(r"-(-?\d+)c$", slug):
        return int(match.group(1)), "celsius"
    if re.search(r"-(-?\d+)c(?:or)?higher$", slug):
        return None, "celsius"
    return None


def analyze_eliminated_no_exit(
    pairs: Sequence[Mapping[str, Any]],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]],
    observations_by_station: Mapping[str, Sequence[TemperatureObservation]],
    sizes_usd: Sequence[Decimal] = (
        Decimal("20"),
        Decimal("50"),
        Decimal("200"),
        Decimal("1000"),
    ),
) -> dict[str, Any]:
    """Measure NO bid exits only after settlement-rounded physical elimination."""
    records_by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in sorted(pairs, key=lambda row: row["observed_at"]):
        metadata = event_metadata.get(str(pair["event_slug"]))
        if metadata is None:
            continue
        station = str(metadata["station_id"])
        target_date = datetime.fromisoformat(str(metadata["target_date"])).date()
        timezone = ZoneInfo(str(metadata["timezone"]))
        local_cutoff = pair["observed_at"].astimezone(timezone).replace(tzinfo=None)
        if local_cutoff.date() != target_date:
            continue
        available = [
            row
            for row in observations_by_station.get(station, ())
            if row.valid.date() == target_date and row.valid <= local_cutoff
        ]
        if not available:
            continue
        parsed = _bucket_upper_and_unit(str(pair["market_slug"]))
        if parsed is None:
            continue
        upper, unit = parsed
        observed_high_f = Decimal(str(max(row.temperature_f for row in available)))
        margin, tier, eliminated = physical_bucket_state(
            observed_high_f, upper=upper, unit=unit
        )
        if eliminated is not True:
            continue
        running_high: Decimal | None = None
        elimination_at: datetime | None = None
        for observation in sorted(available, key=lambda row: row.valid):
            value = Decimal(str(observation.temperature_f))
            running_high = value if running_high is None else max(running_high, value)
            _candidate_margin, _candidate_tier, candidate_eliminated = (
                physical_bucket_state(running_high, upper=upper, unit=unit)
            )
            if candidate_eliminated:
                elimination_at = observation.valid.replace(tzinfo=timezone).astimezone(UTC)
                break
        if elimination_at is None:
            continue
        no_bids = _levels(pair["no"].get("bids"))
        no_asks = _levels(pair["no"].get("asks"))
        top_bid = _top(no_bids, bids=True)
        top_ask = _top(no_asks, bids=False)
        fills = {}
        for size in sizes_usd:
            fill = estimate_execution_cost(no_bids, size, "sell")
            fills[str(size)] = {
                "average": float(fill.average_fill_price) if fill else None,
                "taker_fee_usdc": float(fill.fee_usdc) if fill else None,
                "fee_per_share": float(fill.fee_per_share) if fill else None,
                "net_per_share": (
                    float(fill.average_fill_price - fill.fee_per_share) if fill else None
                ),
                "filled_fraction": fill.filled_fraction if fill else 0.0,
            }
        records_by_market[str(pair["market_slug"])].append(
            {
                "observed_at": pair["observed_at"],
                "physical_elimination_at": elimination_at,
                "event_slug": pair["event_slug"],
                "station_id": station,
                "market_slug": pair["market_slug"],
                "physical_margin_f": margin,
                "margin_tier": tier,
                "no_bid": float(top_bid) if top_bid is not None else None,
                "no_ask": float(top_ask) if top_ask is not None else None,
                "fills": fills,
            }
        )

    market_results: list[dict[str, Any]] = []
    for market_slug, rows in records_by_market.items():
        ordered = sorted(rows, key=lambda row: row["observed_at"])
        first = ordered[0]
        elimination_time = min(row["physical_elimination_at"] for row in ordered)
        first_book_time = first["observed_at"]

        def threshold_delay(
            threshold: float,
            *,
            selected: list[dict[str, Any]] = ordered,
            started_at: datetime = elimination_time,
        ) -> float | None:
            matches = [
                row
                for row in selected
                if row["no_bid"] is not None and row["no_bid"] >= threshold
            ]
            return (
                (matches[0]["observed_at"] - started_at).total_seconds() / 60
                if matches
                else None
            )

        def latest_within(
            minutes: int,
            *,
            selected: list[dict[str, Any]] = ordered,
            started_at: datetime = elimination_time,
        ) -> dict[str, Any] | None:
            cutoff = started_at + timedelta(minutes=minutes)
            eligible = [row for row in selected if row["observed_at"] <= cutoff]
            return eligible[-1] if eligible else None

        row_15 = latest_within(15)
        row_60 = latest_within(60)
        within_15 = [
            row
            for row in ordered
            if elimination_time <= row["observed_at"] <= elimination_time + timedelta(minutes=15)
        ]
        market_results.append(
            {
                "event_slug": first["event_slug"],
                "station_id": first["station_id"],
                "market_slug": market_slug,
                "physical_elimination_at": elimination_time.isoformat(),
                "first_eliminated_book_at": first_book_time.isoformat(),
                "first_book_delay_minutes": (
                    first_book_time - elimination_time
                ).total_seconds()
                / 60,
                "first_no_bid": first["no_bid"],
                "physical_margin_f": first["physical_margin_f"],
                "minutes_to_no_bid_0_95": threshold_delay(0.95),
                "minutes_to_no_bid_0_98": threshold_delay(0.98),
                "first_fills": first["fills"],
                "after_15m": row_15,
                "after_60m": row_60,
                "latest": ordered[-1],
                "snapshot_count": len(ordered),
                "has_depth_within_15m": bool(within_15),
                "sell_200_at_0_97_within_15m": any(
                    row["fills"]["200"]["filled_fraction"] >= 1
                    and row["fills"]["200"]["net_per_share"] is not None
                    and row["fills"]["200"]["net_per_share"] >= 0.97
                    for row in within_15
                ),
            }
        )
    size_summaries = []
    for size in sizes_usd:
        key = str(size)
        fills = [row["first_fills"][key] for row in market_results]
        size_summaries.append(
            {
                "size_usd": float(size),
                "market_count": len(fills),
                "fully_filled_rate_at_first_snapshot": fmean(
                    fill["filled_fraction"] >= 1 for fill in fills
                )
                if fills
                else None,
                "mean_first_exit_price": fmean(
                    fill["average"] for fill in fills if fill["average"] is not None
                )
                if any(fill["average"] is not None for fill in fills)
                else None,
                "mean_first_exit_net_after_fee": fmean(
                    fill["net_per_share"]
                    for fill in fills
                    if fill["net_per_share"] is not None
                )
                if any(fill["net_per_share"] is not None for fill in fills)
                else None,
                "at_least_0_97_fully_filled_rate": fmean(
                    fill["filled_fraction"] >= 1
                    and fill["net_per_share"] is not None
                    and fill["net_per_share"] >= 0.97
                    for fill in fills
                )
                if fills
                else None,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "eliminated_market_count": len(market_results),
        "stations": sorted({row["station_id"] for row in market_results}),
        "bid_0_95_reached_rate": fmean(
            row["minutes_to_no_bid_0_95"] is not None for row in market_results
        )
        if market_results
        else None,
        "bid_0_98_reached_rate": fmean(
            row["minutes_to_no_bid_0_98"] is not None for row in market_results
        )
        if market_results
        else None,
        "depth_within_15m_coverage_rate": fmean(
            row["has_depth_within_15m"] for row in market_results
        )
        if market_results
        else None,
        "sell_200_at_0_97_within_15m_rate": fmean(
            row["sell_200_at_0_97_within_15m"]
            for row in market_results
            if row["has_depth_within_15m"]
        )
        if any(row["has_depth_within_15m"] for row in market_results)
        else None,
        "size_summaries": size_summaries,
        "markets": market_results,
        "settled_event_count": 0,
        "partial_forward_day": True,
        "execution_enabled": False,
    }


def render_eliminated_exit_report(result: Mapping[str, Any], output_path: Path) -> None:
    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    def number(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.3f}"

    def interval(successes: int, sample_count: int) -> str:
        value = wilson_interval(successes, sample_count)
        return "N/A" if value is None else f"{value[0]:.1%}–{value[1]:.1%}"

    market_count = int(result["eliminated_market_count"])
    reached_095 = sum(
        row["minutes_to_no_bid_0_95"] is not None for row in result["markets"]
    )
    reached_098 = sum(
        row["minutes_to_no_bid_0_98"] is not None for row in result["markets"]
    )
    covered_15 = sum(row["has_depth_within_15m"] for row in result["markets"])
    executable_200 = sum(
        row["sell_200_at_0_97_within_15m"]
        for row in result["markets"]
        if row["has_depth_within_15m"]
    )

    def summary_rate(name: str, *, clean_successes: int) -> tuple[str, str]:
        unfiltered = result.get("unfiltered_summary", {})
        unfiltered_count = int(
            unfiltered.get("eliminated_market_count", market_count)
        )
        unfiltered_rate = unfiltered.get(name)
        unfiltered_successes = (
            round(float(unfiltered_rate) * unfiltered_count)
            if unfiltered_rate is not None
            else 0
        )
        return (
            f"{pct(unfiltered_rate)} ({interval(unfiltered_successes, unfiltered_count)})",
            f"{pct(result[name])} ({interval(clean_successes, market_count)})",
        )

    bid_095_compare = summary_rate(
        "bid_0_95_reached_rate", clean_successes=reached_095
    )
    bid_098_compare = summary_rate(
        "bid_0_98_reached_rate", clean_successes=reached_098
    )
    coverage_compare = summary_rate(
        "depth_within_15m_coverage_rate", clean_successes=covered_15
    )

    lines = [
        "# 已出局桶 NO 退出可行性",
        "",
        "使用高频 METAR T 组重放物理日高；每个市场快照只使用其时刻之前的观测，",
        "并且只在物理出局之后评估真实 NO bids。",
        "当前仍是未结算的前向归档样本，尚无已结算深度重叠样本。",
        "",
        "- 默认排除 CLOB WebSocket 官方维护/故障及实测恢复期质量窗口；"
        f"相对未过滤输入减少 {result.get('upstream_degraded_pairs_excluded', 0)} 个配对快照，"
        f"出局桶 {result.get('unfiltered_eliminated_market_count', result['eliminated_market_count'])}→{result['eliminated_market_count']}。",
        f"- 已观察出局桶：{result['eliminated_market_count']}",
        f"- 可靠性：{'统计不可靠（n < 30）' if market_count < 30 else '可用'}。",
        "",
        "| T6 指标 | 未过滤 (Wilson 95%) | 默认排除后 (Wilson 95%) |",
        "|---|---:|---:|",
        f"| NO bid≥0.95 | {bid_095_compare[0]} | {bid_095_compare[1]} |",
        f"| NO bid≥0.98 | {bid_098_compare[0]} | {bid_098_compare[1]} |",
        f"| 15分钟深度覆盖 | {coverage_compare[0]} | {coverage_compare[1]} |",
        "",
        f"- NO bid 达到 0.95：{pct(result['bid_0_95_reached_rate'])} "
        f"(Wilson 95% {interval(reached_095, market_count)})",
        f"- NO bid 达到 0.98：{pct(result['bid_0_98_reached_rate'])} "
        f"(Wilson 95% {interval(reached_098, market_count)})",
        "- 出局后 15 分钟内有深度覆盖："
        f"{pct(result['depth_within_15m_coverage_rate'])} "
        f"(Wilson 95% {interval(covered_15, market_count)})",
        "- 在有覆盖的样本中，$200 以 ≥0.97 完整卖出："
        f"{pct(result['sell_200_at_0_97_within_15m_rate'])} "
        f"(Wilson 95% {interval(executable_200, covered_15)})",
        "",
        "默认按主动吃 bid 的 Weather taker 计算官方手续费；卖出净价=深度均价−手续费/份。",
        "",
        "| 名义金额 | 桶数 | 首快照完整成交率 | Wilson 95% | 毛卖价 | 扣费净卖价 | 净价≥0.97完整成交率 | Wilson 95% |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["size_summaries"]:
        lines.append(
            f"| ${row['size_usd']:.0f} | {row['market_count']} | "
            f"{pct(row['fully_filled_rate_at_first_snapshot'])} | "
            f"{interval(round(row['fully_filled_rate_at_first_snapshot'] * row['market_count']), row['market_count'])} | "
            f"{number(row['mean_first_exit_price'])} | "
            f"{number(row['mean_first_exit_net_after_fee'])} | "
            f"{pct(row['at_least_0_97_fully_filled_rate'])} | "
            f"{interval(round(row['at_least_0_97_fully_filled_rate'] * row['market_count']), row['market_count'])} |"
        )
    lines.extend(
        [
            "",
            "这些数字只测当前前向可成交性，不是收益回测；事件结算前不会计算胜率或期望。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_real_no_report(result: Mapping[str, Any], output_path: Path) -> None:
    def number(value: Any, digits: int = 3) -> str:
        return "N/A" if value is None else f"{float(value):.{digits}f}"

    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    def interval(low: Any, high: Any) -> str:
        return "N/A" if low is None or high is None else f"{pct(low)}–{pct(high)}"

    lines = [
        "# NO 侧真实 bid/ask 与深度报告",
        "",
        "所有价格来自 NO token 自己的订单簿；没有深度的时刻保持 N/A，不回退到 1-p。",
        "",
        "## 覆盖",
        "",
        f"- 配对快照：{result['paired_snapshot_count']}；可用：{result['usable_snapshot_count']}",
        "- 默认排除 CLOB WebSocket 官方维护/故障及实测恢复期质量窗口；"
        f"相对未过滤输入减少 {result.get('upstream_degraded_pairs_excluded', 0)} 个配对快照。",
        f"- 事件：{result['event_count']}；二元桶：{result['market_count']}",
        "- 22 个已结算事件与深度归档重叠：0，因此历史真实执行收益为 N/A。",
        "",
        "### 质量窗口剔除差异",
        "",
        "| 指标 | 未过滤 | 默认排除后 |",
        "|---|---:|---:|",
        f"| 配对快照 | {result.get('unfiltered_summary', {}).get('paired_snapshot_count', result['paired_snapshot_count'])} | {result['paired_snapshot_count']} |",
        f"| 可用快照 | {result.get('unfiltered_summary', {}).get('usable_snapshot_count', result['usable_snapshot_count'])} | {result['usable_snapshot_count']} |",
        f"| 互补差最大值 | {number(result.get('unfiltered_summary', {}).get('complement_gap_max'))} | {number(result['complement_gap_max'])} |",
        f"| NO ask−(1−YES last) | {number(result.get('unfiltered_summary', {}).get('mean_no_ask_minus_proxy'))} | {number(result['mean_no_ask_minus_proxy'])} |",
        "",
        "## YES/NO 双侧一致性",
        "",
        f"- 互补价差绝对值 p50：{number(result['complement_gap_p50'])}",
        f"- 最大值：{number(result['complement_gap_max'])}",
        f"- 超过 2¢ 比例：{pct(result['complement_gap_over_2c_rate'])} "
        f"(Wilson 95% {interval(result.get('complement_gap_over_2c_wilson_low'), result.get('complement_gap_over_2c_wilson_high'))})",
        "- 检验式：NO ask + YES bid = 1；YES ask + NO bid = 1。",
        "",
        "## 1-p 与真实 NO 盘口",
        "",
        f"- 有可比 last-trade 代理的快照：{result['proxy_comparable_count']}",
        f"- 平均 NO ask − (1−YES last)：{number(result['mean_no_ask_minus_proxy'])}",
        f"- 平均 NO bid − (1−YES last)：{number(result['mean_no_bid_minus_proxy'])}",
        "",
        "## 深度",
        "",
        "默认按主动吃单（taker）估算；手续费按每个真实成交档位的 token 自身价格计算，与滑点分列。",
        "",
        "| 名义金额 | 买 NO 完整成交率 (Wilson 95%) | 卖 NO 完整成交率 (Wilson 95%) | 深度均价 | 扣费全成本 | 手续费影响/份 | 可靠性 |",
        "|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["size_summaries"]:
        lines.append(
            f"| ${row['size_usd']:.0f} | {pct(row['buy_no_fully_filled_rate'])} "
            f"({interval(row['buy_no_fully_filled_wilson_low'], row['buy_no_fully_filled_wilson_high'])}) | "
            f"{pct(row['sell_no_fully_filled_rate'])} "
            f"({interval(row['sell_no_fully_filled_wilson_low'], row['sell_no_fully_filled_wilson_high'])}) | "
            f"{number(row['mean_buy_no_average'])} | "
            f"{number(row['mean_buy_no_all_in_per_share'])} | "
            f"{number(row['mean_buy_no_all_in_per_share'] - row['mean_buy_no_average']) if row['mean_buy_no_all_in_per_share'] is not None and row['mean_buy_no_average'] is not None else 'N/A'} | "
            f"{'n<30，统计不可靠' if row['statistically_unreliable'] else '可用'} |"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
