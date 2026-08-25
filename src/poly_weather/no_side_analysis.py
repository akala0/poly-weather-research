"""Strict no-lookahead diagnostics for NO-side price proxies and real books."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.intraday_reversal import TemperatureObservation


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _activity_tier(change_count: int) -> str:
    if change_count < 5:
        return "0-4"
    if change_count < 20:
        return "5-19"
    if change_count < 50:
        return "20-49"
    return ">=50"


def _last_change_age_minutes(
    history: Sequence[Mapping[str, Any]], index: int
) -> float | None:
    """Age since the last different p value; not a claim about trade time."""
    current = float(history[index]["p"])
    for previous_index in range(index - 1, -1, -1):
        if float(history[previous_index]["p"]) != current:
            return (int(history[index]["t"]) - int(history[previous_index]["t"])) / 60
    return None


def audit_no_proxy_distortion(
    catalog: Mapping[str, Any],
    *,
    histories_by_event: Mapping[str, Mapping[str, Any]],
    observations_by_station: Mapping[str, Sequence[TemperatureObservation]],
    strong_elimination_f: float = 5.0,
    no_proxy_threshold: float = 0.95,
) -> dict[str, Any]:
    """Measure stale p contradictions using only observations available at each p timestamp.

    The canonical physical margin is ``observed high - finite bucket upper``.
    Thus the legacy wording ``margin < -5F`` is represented here as
    ``distance_past_upper_f > 5F`` and means the bucket is strongly eliminated.
    """
    observations = {
        station: sorted(rows, key=lambda row: row.valid)
        for station, rows in observations_by_station.items()
    }
    all_rows: list[dict[str, Any]] = []
    contradiction_rows: list[dict[str, Any]] = []
    eliminated_bucket_days: set[tuple[str, str]] = set()
    contradiction_bucket_days: set[tuple[str, str]] = set()

    for event in catalog.get("events") or []:
        event_slug = str(event["event_slug"])
        target_date = date.fromisoformat(str(event["target_date"]))
        station_id = str(event["station_id"])
        timezone = ZoneInfo(str(event["timezone"]))
        station_rows = [
            row for row in observations.get(station_id, ()) if row.valid.date() == target_date
        ]
        history_payload = histories_by_event[event_slug]
        for market in history_payload.get("markets") or []:
            upper = market.get("upper_f")
            if upper is None:
                continue
            market_slug = str(market["market_slug"])
            history = sorted(
                (row for row in market.get("history") or [] if "t" in row and "p" in row),
                key=lambda row: int(row["t"]),
            )
            target_day_points = sum(
                datetime.fromtimestamp(int(row["t"]), tz=UTC)
                .astimezone(timezone)
                .date()
                == target_date
                for row in history
            )
            target_day_history = [
                row
                for row in history
                if datetime.fromtimestamp(int(row["t"]), tz=UTC)
                .astimezone(timezone)
                .date()
                == target_date
            ]
            target_day_changes = sum(
                float(right["p"]) != float(left["p"])
                for left, right in zip(
                    target_day_history, target_day_history[1:], strict=False
                )
            )
            tier = _activity_tier(target_day_changes)
            observed_index = 0
            observed_high: float | None = None
            for index, point in enumerate(history):
                sample_utc = datetime.fromtimestamp(int(point["t"]), tz=UTC)
                sample_local = sample_utc.astimezone(timezone).replace(tzinfo=None)
                if sample_local.date() != target_date:
                    continue
                while (
                    observed_index < len(station_rows)
                    and station_rows[observed_index].valid <= sample_local
                ):
                    value = station_rows[observed_index].temperature_f
                    observed_high = value if observed_high is None else max(observed_high, value)
                    observed_index += 1
                if observed_high is None:
                    continue
                physical_margin_f = observed_high - float(upper)
                yes_p = float(point["p"])
                no_proxy = 1.0 - yes_p
                eliminated = physical_margin_f > 0
                strongly_eliminated = physical_margin_f > strong_elimination_f
                contradiction = strongly_eliminated and no_proxy < no_proxy_threshold
                row = {
                    "event_slug": event_slug,
                    "station_id": station_id,
                    "target_date": target_date.isoformat(),
                    "sample_at": sample_utc.isoformat(),
                    "market_slug": market_slug,
                    "upper_f": float(upper),
                    "observed_high_f": observed_high,
                    "physical_margin_f": physical_margin_f,
                    "yes_p": yes_p,
                    "no_proxy": no_proxy,
                    "target_day_p_point_count": target_day_points,
                    "target_day_p_change_count": target_day_changes,
                    "activity_tier": tier,
                    "p_unchanged_age_minutes": _last_change_age_minutes(history, index),
                    "eliminated": eliminated,
                    "strongly_eliminated": strongly_eliminated,
                    "contradiction": contradiction,
                }
                all_rows.append(row)
                if eliminated:
                    eliminated_bucket_days.add((event_slug, market_slug))
                if contradiction:
                    contradiction_rows.append(row)
                    contradiction_bucket_days.add((event_slug, market_slug))

    eliminated = [row for row in all_rows if row["eliminated"]]
    strongly_eliminated = [row for row in all_rows if row["strongly_eliminated"]]
    opposite_sign_cases = [
        row
        for row in all_rows
        if row["physical_margin_f"] < -strong_elimination_f
        and row["no_proxy"] < no_proxy_threshold
    ]
    known_ages = [
        float(row["p_unchanged_age_minutes"])
        for row in contradiction_rows
        if row["p_unchanged_age_minutes"] is not None
    ]
    activity: list[dict[str, Any]] = []
    for tier in ("0-4", "5-19", "20-49", ">=50"):
        selected = [row for row in strongly_eliminated if row["activity_tier"] == tier]
        contradictions = [row for row in selected if row["contradiction"]]
        activity.append(
            {
                "activity_tier": tier,
                "strongly_eliminated_points": len(selected),
                "contradiction_points": len(contradictions),
                "contradiction_rate": len(contradictions) / len(selected) if selected else None,
                "mean_no_proxy": fmean(row["no_proxy"] for row in selected)
                if selected
                else None,
            }
        )
    unique_rate = (
        len(contradiction_bucket_days) / len(eliminated_bucket_days)
        if eliminated_bucket_days
        else None
    )
    point_rate = len(contradiction_rows) / len(eliminated) if eliminated else None
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "event_count": len(catalog.get("events") or []),
        "aligned_point_count": len(all_rows),
        "eliminated_point_count": len(eliminated),
        "strongly_eliminated_point_count": len(strongly_eliminated),
        "contradiction_point_count": len(contradiction_rows),
        "contradiction_share_of_eliminated_points": point_rate,
        "contradiction_share_of_strongly_eliminated_points": (
            len(contradiction_rows) / len(strongly_eliminated)
            if strongly_eliminated
            else None
        ),
        "eliminated_bucket_day_count": len(eliminated_bucket_days),
        "contradiction_bucket_day_count": len(contradiction_bucket_days),
        "contradiction_share_of_eliminated_bucket_days": unique_rate,
        "p_unchanged_age_known_count": len(known_ages),
        "p_unchanged_age_unknown_count": len(contradiction_rows) - len(known_ages),
        "p_unchanged_age_p50_minutes": median(known_ages) if known_ages else None,
        "p_unchanged_age_p90_minutes": _percentile(known_ages, 0.90),
        "activity_strata": activity,
        "opposite_sign_case_count": len(opposite_sign_cases),
        "opposite_sign_no_proxy_min": min(
            (row["no_proxy"] for row in opposite_sign_cases), default=None
        ),
        "opposite_sign_no_proxy_max": max(
            (row["no_proxy"] for row in opposite_sign_cases), default=None
        ),
        "cases": contradiction_rows,
        "price_semantics": (
            "prices-history p is neither an ask nor a trade tape. Age is time since the "
            "last different returned p value; true last-trade age is not observable."
        ),
        "historical_no_backtest_usable": False,
        "execution_enabled": False,
    }


def render_no_proxy_audit(result: Mapping[str, Any], output_path: Path) -> None:
    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    def number(value: Any, suffix: str = "") -> str:
        return "N/A" if value is None else f"{float(value):.1f}{suffix}"

    lines = [
        "# NO 侧 p 代理失真审计",
        "",
        "严格无前视：每个 p 时间点只使用 timestamp <= 该点的 IEM 观测。",
        "T1 旧口径 `< -5°F` 在本文转换为统一新口径 `H_t - 桶上界 > 5°F`。",
        "",
        "## 核心结果",
        "",
        f"- 事件：{result['event_count']}",
        f"- 对齐点：{result['aligned_point_count']}",
        f"- 已出局点：{result['eliminated_point_count']}",
        f"- 强出局点（越过上界 >5°F）：{result['strongly_eliminated_point_count']}",
        f"- 矛盾点（强出局但 1-p<0.95）：{result['contradiction_point_count']}",
        "- 矛盾占全部已出局点："
        f"{pct(result['contradiction_share_of_eliminated_points'])}",
        "- 矛盾占强出局点："
        f"{pct(result['contradiction_share_of_strongly_eliminated_points'])}",
        f"- 已出局桶日：{result['eliminated_bucket_day_count']}；其中出现矛盾："
        f"{result['contradiction_bucket_day_count']} "
        f"({pct(result['contradiction_share_of_eliminated_bucket_days'])})",
        "- 若错误使用相反符号 `H_t-上界<-5°F`，会找到 "
        f"{result['opposite_sign_case_count']} 个所谓矛盾点；但这些桶仍高于当前温度，并未出局。"
        f"其 NO 代理范围为 {number(result['opposite_sign_no_proxy_min'])}–"
        f"{number(result['opposite_sign_no_proxy_max'])}。",
        "",
        "## p 陈旧程度",
        "",
        "`prices-history` 不是成交逐笔数据，无法恢复真正的最后成交时刻。以下仅为最后一次不同 p 值距采样点的时间，是陈旧度下界/代理，不冒充成交年龄。",
        "",
        f"- 可测案例：{result['p_unchanged_age_known_count']}；从序列开始即未变化："
        f"{result['p_unchanged_age_unknown_count']}",
        f"- p50：{number(result['p_unchanged_age_p50_minutes'], ' 分钟')}",
        f"- p90：{number(result['p_unchanged_age_p90_minutes'], ' 分钟')}",
        "",
        "## 按当日 p 变化次数分层",
        "",
        "prices-history 约每十分钟返回一个采样点，不能称为成交次数；这里使用当日 p 值实际变化次数作为活跃度代理。",
        "",
        "| 当日 p 变化次数 | 强出局点 | 矛盾点 | 矛盾率 | 平均 NO 代理 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in result["activity_strata"]:
        lines.append(
            f"| {row['activity_tier']} | {row['strongly_eliminated_points']} | "
            f"{row['contradiction_points']} | {pct(row['contradiction_rate'])} | "
            f"{number(row['mean_no_proxy'])} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            "NO 侧历史执行回测不可使用 `1-p`。它既不是 NO ask，也不是 NO bid，且没有成交时间语义。历史结果只可用于候选生成，收益与可成交性必须转为真实深度前向验证。",
            "",
            "## 矛盾案例样例（前 30 条）",
            "",
            "| 时刻UTC | 站点 | 桶上界 | 已观测高温 | 越界 | YES p | NO代理 | p未变化分钟 | 当日p变化 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["cases"][:30]:
        lines.append(
            f"| {row['sample_at']} | {row['station_id']} | {row['upper_f']:.0f} | "
            f"{row['observed_high_f']:.1f} | {row['physical_margin_f']:.1f} | "
            f"{row['yes_p']:.3f} | {row['no_proxy']:.3f} | "
            f"{number(row['p_unchanged_age_minutes'])} | "
            f"{row['target_day_p_change_count']} |"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_history_directory(path: Path) -> dict[str, dict[str, Any]]:
    return {
        file.stem: json.loads(file.read_text(encoding="utf-8"))
        for file in sorted(path.glob("*.json"))
    }
