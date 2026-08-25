"""Historical METAR/IEM precision comparison for settlement-risk auditing."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from poly_weather.adapters.aviation_weather import parse_metar_temperature
from poly_weather.intraday_reversal import TemperatureObservation
from poly_weather.modeling import two_degree_bucket_lower
from poly_weather.temperature import celsius_to_fahrenheit, round_whole_degree

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def download_iem_precision_rows(
    *,
    station_id: str,
    timezone: str,
    start_date: date,
    end_date: date,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    owns_client = client is None
    http_client = client or httpx.Client(timeout=120, follow_redirects=True)
    try:
        response = http_client.get(
            IEM_ASOS_URL,
            params={
                "station": station_id,
                "data": ["tmpf", "metar"],
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
            },
        )
        response.raise_for_status()
        return list(csv.DictReader(io.StringIO(response.text)))
    finally:
        if owns_client:
            http_client.close()


def precision_comparison(
    rows: Sequence[dict[str, str]], *, station_id: str
) -> dict[str, Any]:
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    t_group_rows = 0
    fallback_rows = 0
    exact_pair_differences = 0
    comparable_rows = 0
    for row in rows:
        raw_metar = str(row.get("metar") or "")
        if not raw_metar or raw_metar.lower() == "null":
            continue
        try:
            valid = datetime.strptime(str(row["valid"]), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        body_temperature = None
        # The IEM tmpf value is the direct comparison; the fallback parser value
        # is intentionally omitted when tmpf is missing.
        temperature_c, _dewpoint, source, degraded = parse_metar_temperature(
            {"rawOb": raw_metar, "temp": body_temperature, "dewp": None}
        )
        if source == "metar_remarks_t_group" and temperature_c is not None:
            t_group_rows += 1
            exact_f = celsius_to_fahrenheit(temperature_c)
        else:
            fallback_rows += 1
            exact_f = None
        tmpf_raw = str(row.get("tmpf") or "").strip()
        tmpf = None if not tmpf_raw or tmpf_raw.lower() == "null" else Decimal(tmpf_raw)
        if exact_f is not None and tmpf is not None:
            comparable_rows += 1
            if round_whole_degree(exact_f) != round_whole_degree(tmpf):
                exact_pair_differences += 1
        by_day[valid.date()].append(
            {
                "exact_f": exact_f,
                "tmpf": tmpf,
                "degraded": degraded,
            }
        )

    day_rows: list[dict[str, Any]] = []
    for target_date, records in sorted(by_day.items()):
        exact_values = [row["exact_f"] for row in records if row["exact_f"] is not None]
        tmpf_values = [row["tmpf"] for row in records if row["tmpf"] is not None]
        exact_high = max(exact_values) if exact_values else None
        tmpf_high = max(tmpf_values) if tmpf_values else None
        if exact_high is None and tmpf_high is None:
            continue
        exact_rounded = round_whole_degree(exact_high) if exact_high is not None else None
        tmpf_rounded = round_whole_degree(tmpf_high) if tmpf_high is not None else None
        bucket_differs = (
            exact_rounded is not None
            and tmpf_rounded is not None
            and two_degree_bucket_lower(float(exact_rounded))
            != two_degree_bucket_lower(float(tmpf_rounded))
        )
        half_distance = None
        if exact_high is not None:
            fractional = float(exact_high % Decimal(1))
            half_distance = abs(fractional - 0.5)
        day_rows.append(
            {
                "target_date": target_date.isoformat(),
                "exact_high_f": float(exact_high) if exact_high is not None else None,
                "exact_rounded_f": int(exact_rounded) if exact_rounded is not None else None,
                "iem_tmpf_high_f": float(tmpf_high) if tmpf_high is not None else None,
                "iem_tmpf_rounded_f": int(tmpf_rounded) if tmpf_rounded is not None else None,
                "whole_degree_differs": exact_rounded != tmpf_rounded,
                "two_degree_bucket_differs": bucket_differs,
                "within_0_3f_of_half_boundary": (
                    half_distance is not None and half_distance <= 0.3
                ),
            }
        )

    comparable_days = [
        row
        for row in day_rows
        if row["exact_rounded_f"] is not None and row["iem_tmpf_rounded_f"] is not None
    ]
    return {
        "station_id": station_id,
        "raw_row_count": len(rows),
        "t_group_row_count": t_group_rows,
        "fallback_row_count": fallback_rows,
        "t_group_frequency": t_group_rows / (t_group_rows + fallback_rows)
        if t_group_rows + fallback_rows
        else None,
        "comparable_row_count": comparable_rows,
        "row_rounding_difference_count": exact_pair_differences,
        "day_count": len(day_rows),
        "comparable_day_count": len(comparable_days),
        "daily_whole_degree_difference_count": sum(
            row["whole_degree_differs"] for row in comparable_days
        ),
        "daily_bucket_difference_count": sum(
            row["two_degree_bucket_differs"] for row in comparable_days
        ),
        "daily_bucket_difference_rate": (
            sum(row["two_degree_bucket_differs"] for row in comparable_days)
            / len(comparable_days)
            if comparable_days
            else None
        ),
        "half_boundary_day_count": sum(
            row["within_0_3f_of_half_boundary"] for row in day_rows
        ),
        "days": day_rows,
    }


def temperature_observations_from_precision_rows(
    rows: Sequence[dict[str, str]], *, station_id: str
) -> list[TemperatureObservation]:
    """Use T-group tenths when present, otherwise fall back to IEM tmpf."""
    observations: list[TemperatureObservation] = []
    for row in rows:
        try:
            valid = datetime.strptime(str(row["valid"]), "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        temperature_c, _dewpoint, source, _degraded = parse_metar_temperature(
            {"rawOb": str(row.get("metar") or ""), "temp": None, "dewp": None}
        )
        if source == "metar_remarks_t_group" and temperature_c is not None:
            temperature_f = float(celsius_to_fahrenheit(temperature_c))
        else:
            tmpf_raw = str(row.get("tmpf") or "").strip()
            if not tmpf_raw or tmpf_raw.lower() == "null":
                continue
            temperature_f = float(tmpf_raw)
        observations.append(
            TemperatureObservation(
                station_id=station_id,
                valid=valid,
                temperature_f=temperature_f,
            )
        )
    return sorted(observations, key=lambda row: row.valid)


def render_precision_audit(results: Sequence[dict[str, Any]], output_path: Path) -> None:
    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    lines = [
        "# 温度精度与结算显示审计",
        "",
        "## 代码路径",
        "",
        "- 摄氏转华氏统一保持 Decimal 源精度，最后才 ROUND_HALF_UP 到整数。",
        "- METAR 优先 remarks T 组十分度；缺失才回退 body/API，并标记 precision degraded。",
        "- api.weather.gov 保留 JSON 原始 Decimal；实测值可能是整数，不能承诺每条都有十分度。",
        "",
        "## WRH 页面实测",
        "",
        "- Standard 表格显示整数 °F；Metric 表格显示整数 °C。",
        "- 前端 obs.js 对表格值使用 Math.round；底层 air_temp_set_1 可能带小数。",
        "- 因此页面显示值与 T 组精确换算值在半度边界附近存在可量化基差。",
        "",
        "## IEM tmpf 与 METAR T 组历史比较",
        "",
        "| 站点 | 原始行 | T组覆盖 | 可比天 | 整数日高不同 | 2°F桶不同 | 桶差异率 | ±0.3°F边界日 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        lines.append(
            f"| {row['station_id']} | {row['raw_row_count']} | "
            f"{pct(row['t_group_frequency'])} | {row['comparable_day_count']} | "
            f"{row['daily_whole_degree_difference_count']} | "
            f"{row['daily_bucket_difference_count']} | "
            f"{pct(row['daily_bucket_difference_rate'])} | "
            f"{row['half_boundary_day_count']} |"
        )
    lines.extend(
        [
            "",
            "ZUCK 大多数国际 METAR 不含美国式 remarks T 组，因此其 T 组可比样本可能为零；这不是零风险，而是精度无法从报文恢复，必须作为降级数据处理。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
