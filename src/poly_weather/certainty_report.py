from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from poly_weather.domain import SettlementSpec
from poly_weather.intraday_reversal import (
    certainty_curve_points,
    certainty_summary,
    daily_reversals,
    load_iem_asos_csv,
    percentile,
)


def download_iem_asos(
    *,
    station_id: str,
    timezone: str,
    start_date: date,
    end_date: date,
    output_path: Path,
) -> dict[str, Any]:
    """Download local-time temperature observations atomically from IEM."""
    params = {
        "station": station_id,
        "data": "tmpf",
        "year1": start_date.year,
        "month1": start_date.month,
        "day1": start_date.day,
        "year2": end_date.year,
        "month2": end_date.month,
        "day2": end_date.day,
        "tz": timezone,
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "null",
        "trace": "null",
        "direct": "no",
    }
    response = httpx.get(
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py",
        params=params,
        timeout=120,
        follow_redirects=True,
        headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
    )
    response.raise_for_status()
    header = response.text.splitlines()[0] if response.text else ""
    if header != "station,valid,tmpf":
        raise ValueError(f"unexpected IEM response for {station_id}: {header!r}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(response.text, encoding="utf-8", newline="")
    temporary.replace(output_path)
    return {
        "station_id": station_id,
        "request_url": str(response.request.url),
        "bytes": len(response.content),
        "row_count": max(0, len(response.text.splitlines()) - 1),
        "output_path": str(output_path.resolve()),
    }


def _clock(minutes: float | None) -> str:
    if minutes is None:
        return "未达到"
    rounded = int(round(minutes))
    return f"{rounded // 60:02d}:{rounded % 60:02d}"


def _first_threshold(curve: dict[Any, list[Any]], threshold: float) -> str:
    for scan_time, samples in curve.items():
        if samples and float(certainty_summary(samples)["hit_rate"]) >= threshold:
            return scan_time.strftime("%H:%M")
    return "未达到"


def station_certainty_summary_from_observations(
    spec: SettlementSpec,
    observations: list[Any],
) -> dict[str, Any]:
    """Summarize historical features from an explicitly selected observation series."""
    summer = [item for item in observations if item.valid.month in {6, 7, 8}]
    curve = certainty_curve_points(
        summer,
        unit=spec.unit,
        bucket_width_degrees=spec.bucket_width_degrees,
    )
    by_day: dict[date, list[Any]] = defaultdict(list)
    for item in summer:
        by_day[item.valid.date()].append(item)
    high_minutes = []
    for records in by_day.values():
        final = max(item.temperature_f for item in records)
        first = next(item for item in records if item.temperature_f == final)
        high_minutes.append(first.valid.hour * 60 + first.valid.minute)
    reversals = daily_reversals(summer)
    # A causal reversal must be a new rise after the decision observation.
    # Full-day high minus 16:30 temperature confounds earlier highs with later warming.
    remaining = [item.post_decision_warming_f for item in reversals]
    return {
        "station_id": spec.station_id,
        "unit": spec.unit,
        "coverage_start": observations[0].valid.date().isoformat() if observations else None,
        "coverage_end": observations[-1].valid.date().isoformat() if observations else None,
        "observation_count": len(observations),
        "summer_day_count": len(by_day),
        "median_high_minutes": percentile(high_minutes, 0.5) if high_minutes else None,
        "first_70": _first_threshold(curve, 0.70),
        "first_85": _first_threshold(curve, 0.85),
        "first_95": _first_threshold(curve, 0.95),
        "reversal_frequency": (
            statistics.fmean(value > 1.0 for value in remaining) if remaining else None
        ),
        "p90_remaining_warming_f": percentile(remaining, 0.90) if remaining else None,
        "reversal_sample_count": len(remaining),
    }


def station_certainty_summary(spec: SettlementSpec, csv_path: Path) -> dict[str, Any]:
    observations = load_iem_asos_csv(csv_path, station_id=spec.station_id or spec.key)
    return station_certainty_summary_from_observations(spec, observations)


def render_certainty_summary_report(rows: list[dict[str, Any]], output_path: Path) -> None:
    ordered = sorted(rows, key=lambda row: row["median_high_minutes"] or 10_000)
    lines = [
        "# 多城市确定性曲线汇总",
        "",
        "> **已废弃，不得用于当前结论。** 本报告使用 IEM 小时采样，会漏掉整点之间的真实高点。",
        "> 当前口径请使用 `data/high_frequency_weather_reanalysis.md`；例如 KLAX 逆转率已由 1.1% 修正为 17.2%。",
        "",
        "严格使用当地时刻之前（含）的观测；美国市场按 2°F 桶，中国市场按 1°C 桶。",
        "夏季为 6–8 月；逆转定义为 16:30 之后的最高温相对 16:30 观测上升 >1°F。",
        "",
        "| 站点 | 数据覆盖 | 夏季日数 | 夏季高点时刻中位数 | 首次≥70% | 首次≥85% | 首次≥95% | 夏季逆转率 | p90剩余升温 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ordered:
        reversal = row["reversal_frequency"]
        p90 = row["p90_remaining_warming_f"]
        lines.append(
            f"| {row['station_id']} | {row['coverage_start']}–{row['coverage_end']} | "
            f"{row['summer_day_count']} | {_clock(row['median_high_minutes'])} | "
            f"{row['first_70']} | {row['first_85']} | {row['first_95']} | "
            f"{reversal:.1%} | {p90:.1f}°F |"
        )
    lines.extend(
        [
            "",
            "注：高点时刻取每日首次达到全日最高温的观测；IEM 国际站通常整点一报，",
            "因此中国站时刻分辨率约为一小时。未达到表示截至 18:00 仍低于该阈值。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
