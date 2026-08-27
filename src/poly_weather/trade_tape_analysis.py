"""Historical public trade-tape diagnostics without pretending trades are quotes."""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.intraday_reversal import TemperatureObservation
from poly_weather.no_forward import wilson_interval
from poly_weather.signal_engine import physical_bucket_state


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _trade_tier(count: int) -> str:
    if count == 0:
        return "0"
    if count < 5:
        return "1-4"
    if count < 20:
        return "5-19"
    if count < 100:
        return "20-99"
    return ">=100"


def load_event_trade_tapes(path: Path) -> dict[str, list[PublicTrade]]:
    output: dict[str, list[PublicTrade]] = {}
    for source in sorted(path.glob("*.json")):
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or not payload.get("event_slug"):
            # Collection audit/index files live beside event tapes but do not
            # represent a trade tape themselves.
            continue
        event_slug = str(payload["event_slug"])
        fetched_at: datetime | None = None
        if payload.get("fetched_at"):
            try:
                fetched_at = datetime.fromisoformat(
                    str(payload["fetched_at"]).replace("Z", "+00:00")
                ).astimezone(UTC)
            except (TypeError, ValueError):
                fetched_at = None
        output[event_slug] = [
            PublicTrade(
                proxy_wallet=str(row.get("proxy_wallet") or ""),
                asset_id=str(row["asset_id"]),
                condition_id=str(row["condition_id"]),
                event_slug=str(row["event_slug"]),
                market_slug=str(row["market_slug"]),
                outcome=str(row["outcome"]),
                side=str(row["side"]),
                size=Decimal(str(row["size"])),
                price=Decimal(str(row["price"])),
                timestamp=datetime.fromisoformat(str(row["timestamp"])).astimezone(UTC),
                transaction_hash=str(row["transaction_hash"]),
                available_at=(
                    datetime.fromisoformat(str(row["available_at"])).astimezone(UTC)
                    if row.get("available_at")
                    else fetched_at
                ),
            )
            for row in payload.get("trades") or ()
        ]
    return output


def physical_elimination_times(
    catalog: Mapping[str, Any],
    observations_by_station: Mapping[str, Sequence[TemperatureObservation]],
) -> dict[tuple[str, str], datetime]:
    """Find the first observation that irreversibly moves above a finite bucket."""
    output: dict[tuple[str, str], datetime] = {}
    for event in catalog.get("events") or ():
        event_slug = str(event["event_slug"])
        station_id = str(event["station_id"])
        target_date = date.fromisoformat(str(event["target_date"]))
        timezone = ZoneInfo(str(event["timezone"]))
        observations = sorted(
            (
                row
                for row in observations_by_station.get(station_id, ())
                if row.valid.date() == target_date
            ),
            key=lambda row: row.valid,
        )
        for market in event.get("markets") or ():
            upper = market.get("upper_f")
            if upper is None:
                continue
            running_high: float | None = None
            for observation in observations:
                running_high = (
                    observation.temperature_f
                    if running_high is None
                    else max(running_high, observation.temperature_f)
                )
                _margin, _tier, eliminated = physical_bucket_state(
                    running_high, upper=int(upper), unit="fahrenheit"
                )
                if eliminated:
                    output[(event_slug, str(market["market_slug"]))] = (
                        observation.valid.replace(tzinfo=timezone).astimezone(UTC)
                    )
                    break
    return output


def analyze_trade_tape_staleness(
    catalog: Mapping[str, Any],
    *,
    histories_by_event: Mapping[str, Mapping[str, Any]],
    trades_by_event: Mapping[str, Sequence[PublicTrade]],
    elimination_times: Mapping[tuple[str, str], datetime] | None = None,
) -> dict[str, Any]:
    """Measure actual last-trade age at every prices-history sample cutoff."""
    elimination_times = elimination_times or {}
    sample_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    all_no_trades: list[PublicTrade] = []
    all_late_no_trades: list[PublicTrade] = []
    for event in catalog.get("events") or ():
        event_slug = str(event["event_slug"])
        event_trades = list(trades_by_event.get(event_slug, ()))
        target_date = date.fromisoformat(str(event["target_date"]))
        timezone = ZoneInfo(str(event["timezone"]))
        late_start = datetime.combine(target_date, time(18), tzinfo=timezone).astimezone(UTC)
        late_end = datetime.combine(
            target_date + timedelta(days=1), time(10), tzinfo=timezone
        ).astimezone(UTC)
        history = histories_by_event[event_slug]
        for market in history.get("markets") or ():
            market_slug = str(market["market_slug"])
            yes_asset = str(market["yes_token_id"])
            yes_trades = [
                trade
                for trade in event_trades
                if trade.market_slug == market_slug
                and trade.outcome.casefold() == "yes"
                and trade.asset_id == yes_asset
            ]
            no_trades = [
                trade
                for trade in event_trades
                if trade.market_slug == market_slug and trade.outcome.casefold() == "no"
            ]
            yes_trades.sort(key=lambda item: item.timestamp)
            no_trades.sort(key=lambda item: item.timestamp)
            yes_timestamps = [trade.timestamp for trade in yes_trades]
            target_day_trades = [
                trade
                for trade in yes_trades + no_trades
                if trade.timestamp.astimezone(timezone).date() == target_date
            ]
            actual_trade_count = len(target_day_trades)
            tier = _trade_tier(actual_trade_count)
            for point in market.get("history") or ():
                cutoff = datetime.fromtimestamp(int(point["t"]), tz=UTC)
                if cutoff.astimezone(timezone).date() != target_date:
                    continue
                position = bisect_right(yes_timestamps, cutoff) - 1
                trade = yes_trades[position] if position >= 0 else None
                age_minutes = (
                    (cutoff - trade.timestamp).total_seconds() / 60 if trade else None
                )
                sample_rows.append(
                    {
                        "event_slug": event_slug,
                        "market_slug": market_slug,
                        "sample_at": cutoff.isoformat(),
                        "prices_history_p": float(point["p"]),
                        "last_yes_trade_at": trade.timestamp.isoformat() if trade else None,
                        "last_yes_trade_price": float(trade.price) if trade else None,
                        "last_trade_age_minutes": age_minutes,
                        "market_trade_count": actual_trade_count,
                        "activity_tier": tier,
                    }
                )
            no_prices = [float(trade.price) for trade in no_trades]
            yes_prices = [float(trade.price) for trade in yes_trades]
            all_market_trades = sorted(yes_trades + no_trades, key=lambda item: item.timestamp)
            gaps = [
                (right.timestamp - left.timestamp).total_seconds() / 3600
                for left, right in zip(all_market_trades, all_market_trades[1:], strict=False)
            ]
            late_no_trades = [
                trade for trade in no_trades if late_start <= trade.timestamp <= late_end
            ]
            all_no_trades.extend(no_trades)
            all_late_no_trades.extend(late_no_trades)
            late_notional = sum(float(trade.size) for trade in late_no_trades)
            late_vwap = (
                sum(float(trade.price * trade.size) for trade in late_no_trades)
                / late_notional
                if late_notional
                else None
            )
            elimination_at = elimination_times.get((event_slug, market_slug))
            post_elimination_trades = (
                [
                    trade
                    for trade in all_market_trades
                    if elimination_at <= trade.timestamp <= late_end
                ]
                if elimination_at is not None
                else []
            )
            post_elimination_no_trades = [
                trade for trade in post_elimination_trades if trade.outcome.casefold() == "no"
            ]
            first_post_elimination = (
                post_elimination_trades[0] if post_elimination_trades else None
            )
            zero_trade_hours = (
                max(
                    0.0,
                    (
                        (first_post_elimination.timestamp if first_post_elimination else late_end)
                        - elimination_at
                    ).total_seconds()
                    / 3600,
                )
                if elimination_at is not None
                else None
            )
            post_no_notional = sum(float(trade.size) for trade in post_elimination_no_trades)
            market_rows.append(
                {
                    "event_slug": event_slug,
                    "market_slug": market_slug,
                    "yes_asset_id": yes_asset,
                    "no_asset_id": no_trades[0].asset_id if no_trades else None,
                    "yes_trade_count": len(yes_trades),
                    "no_trade_count": len(no_trades),
                    "target_day_trade_count": actual_trade_count,
                    "no_trade_price_min": min(no_prices, default=None),
                    "no_trade_price_max": max(no_prices, default=None),
                    "yes_trade_price_min": min(yes_prices, default=None),
                    "yes_trade_price_max": max(yes_prices, default=None),
                    "maximum_zero_trade_gap_hours": max(gaps, default=None),
                    "late_window": "target local 18:00 through next day 10:00",
                    "late_no_trade_count": len(late_no_trades),
                    "late_no_trade_at_or_above_0_97_count": sum(
                        trade.price >= Decimal("0.97") for trade in late_no_trades
                    ),
                    "late_no_trade_vwap": late_vwap,
                    "late_no_trade_shares": late_notional,
                    "physical_elimination_at": (
                        elimination_at.isoformat() if elimination_at is not None else None
                    ),
                    "post_elimination_trade_count": len(post_elimination_trades),
                    "post_elimination_no_trade_count": len(post_elimination_no_trades),
                    "hours_to_first_post_elimination_trade": zero_trade_hours,
                    "post_elimination_no_trade_vwap": (
                        sum(float(trade.price * trade.size) for trade in post_elimination_no_trades)
                        / post_no_notional
                        if post_no_notional
                        else None
                    ),
                }
            )

    known = [float(row["last_trade_age_minutes"]) for row in sample_rows if row["last_trade_age_minutes"] is not None]
    stale_count = sum(age > 60 for age in known)
    stale_ci = wilson_interval(stale_count, len(known)) if known else (None, None)
    strata = []
    for tier in ("0", "1-4", "5-19", "20-99", ">=100"):
        selected = [row for row in sample_rows if row["activity_tier"] == tier]
        ages = [
            float(row["last_trade_age_minutes"])
            for row in selected
            if row["last_trade_age_minutes"] is not None
        ]
        over = sum(age > 60 for age in ages)
        lower, upper = wilson_interval(over, len(ages)) if ages else (None, None)
        strata.append(
            {
                "activity_tier": tier,
                "sample_count": len(selected),
                "known_age_count": len(ages),
                "missing_age_count": len(selected) - len(ages),
                "age_p50_minutes": median(ages) if ages else None,
                "age_p90_minutes": _percentile(ages, 0.90),
                "over_60m_rate": over / len(ages) if ages else None,
                "over_60m_wilson_low": lower,
                "over_60m_wilson_high": upper,
                "statistically_unreliable": len(ages) < 30,
            }
        )
    no_trade_markets = sum(row["no_trade_count"] == 0 for row in market_rows)
    no_trade_markets_ci = (
        wilson_interval(no_trade_markets, len(market_rows)) if market_rows else None
    )
    eliminated_rows = [
        row for row in market_rows if row["physical_elimination_at"] is not None
    ]
    no_post_elimination = sum(
        row["post_elimination_trade_count"] == 0 for row in eliminated_rows
    )
    no_trade_within_3h = sum(
        row["hours_to_first_post_elimination_trade"] is not None
        and row["hours_to_first_post_elimination_trade"] >= 3
        for row in eliminated_rows
    )
    no_post_ci = (
        wilson_interval(no_post_elimination, len(eliminated_rows))
        if eliminated_rows
        else None
    )
    no_3h_ci = (
        wilson_interval(no_trade_within_3h, len(eliminated_rows))
        if eliminated_rows
        else None
    )
    missing_count = len(sample_rows) - len(known)
    missing_ci = wilson_interval(missing_count, len(sample_rows)) if sample_rows else (None, None)
    no_notional = sum(float(trade.size) for trade in all_no_trades)
    late_no_notional = sum(float(trade.size) for trade in all_late_no_trades)
    late_097_count = sum(
        trade.price >= Decimal("0.97") for trade in all_late_no_trades
    )
    late_097_ci = (
        wilson_interval(late_097_count, len(all_late_no_trades))
        if all_late_no_trades
        else None
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "event_count": len(catalog.get("events") or ()),
        "market_count": len(market_rows),
        "prices_history_sample_count": len(sample_rows),
        "known_last_trade_age_count": len(known),
        "no_prior_trade_sample_count": missing_count,
        "no_prior_trade_rate": missing_count / len(sample_rows) if sample_rows else None,
        "no_prior_trade_wilson_low": missing_ci[0],
        "no_prior_trade_wilson_high": missing_ci[1],
        "last_trade_age_p50_minutes": median(known) if known else None,
        "last_trade_age_p90_minutes": _percentile(known, 0.90),
        "last_trade_age_over_60m_rate": stale_count / len(known) if known else None,
        "last_trade_age_over_60m_wilson_low": stale_ci[0],
        "last_trade_age_over_60m_wilson_high": stale_ci[1],
        "markets_without_no_trades": no_trade_markets,
        "markets_without_no_trades_rate": no_trade_markets / len(market_rows) if market_rows else None,
        "markets_without_no_trades_wilson_low": (
            no_trade_markets_ci[0] if no_trade_markets_ci else None
        ),
        "markets_without_no_trades_wilson_high": (
            no_trade_markets_ci[1] if no_trade_markets_ci else None
        ),
        "no_trade_count": len(all_no_trades),
        "no_trade_share_count": no_notional,
        "no_trade_vwap": (
            sum(float(trade.price * trade.size) for trade in all_no_trades) / no_notional
            if no_notional
            else None
        ),
        "late_no_trade_count": len(all_late_no_trades),
        "late_no_trade_share_count": late_no_notional,
        "late_no_trade_vwap": (
            sum(float(trade.price * trade.size) for trade in all_late_no_trades)
            / late_no_notional
            if late_no_notional
            else None
        ),
        "late_no_trade_at_or_above_0_97_count": late_097_count,
        "late_no_trade_at_or_above_0_97_rate": (
            late_097_count / len(all_late_no_trades) if all_late_no_trades else None
        ),
        "late_no_trade_at_or_above_0_97_wilson_low": (
            late_097_ci[0] if late_097_ci else None
        ),
        "late_no_trade_at_or_above_0_97_wilson_high": (
            late_097_ci[1] if late_097_ci else None
        ),
        "physically_eliminated_market_count": len(eliminated_rows),
        "no_post_elimination_trade_count": no_post_elimination,
        "no_post_elimination_trade_rate": (
            no_post_elimination / len(eliminated_rows) if eliminated_rows else None
        ),
        "no_post_elimination_trade_wilson_low": no_post_ci[0] if no_post_ci else None,
        "no_post_elimination_trade_wilson_high": no_post_ci[1] if no_post_ci else None,
        "no_trade_within_3h_count": no_trade_within_3h,
        "no_trade_within_3h_rate": (
            no_trade_within_3h / len(eliminated_rows) if eliminated_rows else None
        ),
        "no_trade_within_3h_wilson_low": no_3h_ci[0] if no_3h_ci else None,
        "no_trade_within_3h_wilson_high": no_3h_ci[1] if no_3h_ci else None,
        "activity_strata": strata,
        "market_rows": market_rows,
        "sample_rows": sample_rows,
        "semantics": {
            "trade_price": "executed price only; not a resting bid, ask, or guaranteed entry cost",
            "prices_history_p": "sampled p series; not an executable quote",
            "strict_no_lookahead": "last trade timestamp is <= each sample cutoff",
            "physical_elimination": (
                "post-hoc observation-timestamp diagnostic; every counted trade is at or "
                "after the first eliminating observation, never before it"
            ),
            "orderbook_history": "unsupported and frozen before the August study; never used",
        },
        "capability_matrix": {
            "last_trade_age": "available",
            "own_no_token_trade_price": "available_when_traded",
            "tail_bucket_trade_count": "available",
            "trade_vwap": "available",
            "historical_ask": "N/A: trades do not reveal resting asks",
            "historical_bid": "N/A: trades do not reveal resting bids",
            "historical_spread": "N/A: trades do not reveal both sides of the book",
            "historical_depth_and_slippage": "N/A: only locally archived WebSocket depth can answer",
        },
        "execution_enabled": False,
    }


def render_trade_tape_report(result: Mapping[str, Any], output_path: Path) -> None:
    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    def num(value: Any, suffix: str = "") -> str:
        return "N/A" if value is None else f"{float(value):.1f}{suffix}"

    def price(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.3f}"

    lines = [
        "# Polymarket 公开成交流水审计",
        "",
        "`data-api/trades` 是已成交逐笔，不是历史挂单簿。NO 使用 NO token 自己的成交价；绝不以 `1-YES` 替代，也不把成交价冒充 ask。",
        "",
        "## p 陈旧度实测",
        "",
        "只统计目标当地日的 prices-history 采样点；每个点只查 timestamp <= 采样时刻的最后一笔 YES token 成交。",
        "Wilson 区间按采样点计算，仅描述陈旧点比例；同一市场的连续采样相关，不能当作独立策略胜负样本。",
        "",
        f"- 事件：{result['event_count']}；市场：{result['market_count']}；prices-history 采样点：{result['prices_history_sample_count']}",
        f"- 找到此前成交：{result['known_last_trade_age_count']}；此前零成交：{result['no_prior_trade_sample_count']} ({pct(result['no_prior_trade_rate'])}, Wilson 95% {pct(result['no_prior_trade_wilson_low'])}–{pct(result['no_prior_trade_wilson_high'])})",
        f"- 最后成交年龄 p50/p90：{num(result['last_trade_age_p50_minutes'], ' 分钟')} / {num(result['last_trade_age_p90_minutes'], ' 分钟')}",
        f"- 超过 60 分钟：{pct(result['last_trade_age_over_60m_rate'])}（Wilson 95% {pct(result['last_trade_age_over_60m_wilson_low'])}–{pct(result['last_trade_age_over_60m_wilson_high'])}）",
        "",
        "| 桶当日实际成交笔数 | 采样点 | 已知年龄 | 无此前成交 | 年龄p50 | 年龄p90 | >60分钟率 (Wilson 95%) | 可靠性 |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in result["activity_strata"]:
        ci = f"{pct(row['over_60m_rate'])} ({pct(row['over_60m_wilson_low'])}–{pct(row['over_60m_wilson_high'])})"
        lines.append(
            f"| {row['activity_tier']} | {row['sample_count']} | {row['known_age_count']} | {row['missing_age_count']} | {num(row['age_p50_minutes'])} | {num(row['age_p90_minutes'])} | {ci} | {'n<30，统计不可靠' if row['statistically_unreliable'] else '可用'} |"
        )
    lines.extend(
        [
            "",
        "## NO token 与退出流动性边界",
            "",
            f"- 完全没有 NO 成交的市场：{result['markets_without_no_trades']}/{result['market_count']} "
            f"({pct(result['markets_without_no_trades_rate'])}, Wilson 95% "
            f"{pct(result['markets_without_no_trades_wilson_low'])}–{pct(result['markets_without_no_trades_wilson_high'])})。",
            f"- 有可定位物理出局时刻的桶：{result['physically_eliminated_market_count']}；"
            f"出局后截至观察窗结束仍零成交：{result['no_post_elimination_trade_count']} "
            f"({pct(result['no_post_elimination_trade_rate'])}, Wilson 95% "
            f"{pct(result['no_post_elimination_trade_wilson_low'])}–{pct(result['no_post_elimination_trade_wilson_high'])})。",
            f"- 出局后至少连续 3 小时零成交：{result['no_trade_within_3h_count']} "
            f"({pct(result['no_trade_within_3h_rate'])}, Wilson 95% "
            f"{pct(result['no_trade_within_3h_wilson_low'])}–{pct(result['no_trade_within_3h_wilson_high'])})。",
            f"- NO token 规范成交：{result['no_trade_count']} 笔、{num(result['no_trade_share_count'])} 份，"
            f"成交量加权均价 {price(result['no_trade_vwap'])}。",
            f"- 临近结算窗（当地 18:00 至次日 10:00）：{result['late_no_trade_count']} 笔、"
            f"{num(result['late_no_trade_share_count'])} 份，VWAP {price(result['late_no_trade_vwap'])}；"
            f"价格≥0.97 的成交 {result['late_no_trade_at_or_above_0_97_count']} 笔 "
            f"({pct(result['late_no_trade_at_or_above_0_97_rate'])}, Wilson 95% "
            f"{pct(result['late_no_trade_at_or_above_0_97_wilson_low'])}–{pct(result['late_no_trade_at_or_above_0_97_wilson_high'])})。",
            "- NO 自己的成交价可证明某个价位曾成交，但不能证明我们在当时能以该价买入或卖出。",
            "- 连续零成交和最大成交间隔可从 tape 测量；$200/$1000 即时可成交量、bid/ask、spread 与滑点仍只能靠本地 WebSocket 深度归档。",
            "",
            "## N/A 能否被填补",
            "",
            "| 指标 | 结果 |",
            "|---|---|",
        ]
    )
    for name, value in result["capability_matrix"].items():
        lines.append(f"| {name} | {value} |")
    lines.extend(
        [
            "",
            "## orderbook-history",
            "",
            "未使用。该未公开端点已在 2026-02-20 左右停止写入，8 月事件无法获得任何历史深度，不能作为管线依赖。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
