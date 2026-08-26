"""Post-hoc WRH reanalysis of hourly-observation weather conclusions.

This module intentionally accepts only ``historical_backfill`` batch files and
labels its output as hindsight feature analysis. It must never be used to claim
what a trader could see at a historical decision time.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.certainty_report import station_certainty_summary_from_observations
from poly_weather.domain import SettlementSpec
from poly_weather.intraday_reversal import (
    TemperatureObservation,
    load_iem_asos_csv,
    temperature_bucket_key,
)
from poly_weather.temperature import round_whole_degree
from poly_weather.weather_provenance import HISTORICAL_BACKFILL, collection_mode


def load_wrh_historical_observations(
    data_dir: Path,
    *,
    station_id: str,
    timezone: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[TemperatureObservation]:
    """Load/deduplicate post-hoc WRH batch payloads in station-local civil time."""
    station = station_id.upper()
    zone = ZoneInfo(timezone)
    by_timestamp: dict[datetime, TemperatureObservation] = {}
    paths = sorted((data_dir / "raw" / "wrh_history_batches" / station).glob("*.json"))
    for path in paths:
        row = json.loads(path.read_text(encoding="utf-8"))
        if collection_mode(row) != HISTORICAL_BACKFILL:
            raise ValueError(f"WRH history batch lacks historical provenance: {path}")
        payload = row.get("payload")
        stations = payload.get("STATION") if isinstance(payload, dict) else None
        if not isinstance(stations, list) or not stations:
            continue
        observations = stations[0].get("OBSERVATIONS")
        if not isinstance(observations, dict):
            continue
        timestamps = observations.get("date_time")
        temperatures = observations.get("air_temp_set_1")
        if not isinstance(timestamps, list) or not isinstance(temperatures, list):
            continue
        for timestamp, temperature in zip(timestamps, temperatures, strict=False):
            if temperature is None:
                continue
            aware = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            local = aware.astimezone(zone).replace(tzinfo=None)
            if start_date is not None and local.date() < start_date:
                continue
            if end_date is not None and local.date() > end_date:
                continue
            by_timestamp[local] = TemperatureObservation(
                station_id=station,
                valid=local,
                temperature_f=float(temperature),
            )
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def _daily(observations: list[TemperatureObservation]) -> dict[date, list[TemperatureObservation]]:
    grouped: dict[date, list[TemperatureObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.valid.date()].append(observation)
    return grouped


def _clock_to_minutes(value: str) -> int | None:
    if value == "未达到":
        return None
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _clock_delta(old: str, new: str) -> int | None:
    old_minutes = _clock_to_minutes(old)
    new_minutes = _clock_to_minutes(new)
    return None if old_minutes is None or new_minutes is None else new_minutes - old_minutes


def compare_station_history(
    spec: SettlementSpec,
    hourly: list[TemperatureObservation],
    high_frequency: list[TemperatureObservation],
) -> dict[str, Any]:
    """Compare like-for-like summer metrics and daily settlement bucket outcomes."""
    old = station_certainty_summary_from_observations(spec, hourly)
    new = station_certainty_summary_from_observations(spec, high_frequency)
    old_days = _daily(hourly)
    new_days = _daily(high_frequency)
    common = sorted(set(old_days) & set(new_days))
    raw_changed = 0
    rounded_changed = 0
    bucket_changed = 0
    absolute_changes: list[float] = []
    for target_date in common:
        old_high = max(row.temperature_f for row in old_days[target_date])
        new_high = max(row.temperature_f for row in new_days[target_date])
        difference = new_high - old_high
        absolute_changes.append(abs(difference))
        raw_changed += abs(difference) > 0.05
        rounded_changed += round_whole_degree(old_high) != round_whole_degree(new_high)
        bucket_changed += temperature_bucket_key(
            old_high,
            unit=spec.unit,
            width=spec.bucket_width_degrees,
        ) != temperature_bucket_key(
            new_high,
            unit=spec.unit,
            width=spec.bucket_width_degrees,
        )
    metric_deltas = {
        key: _clock_delta(str(old[key]), str(new[key]))
        for key in ("first_70", "first_85", "first_95")
    }
    return {
        "station_id": spec.station_id,
        "timezone": spec.timezone,
        "unit": spec.unit,
        "bucket_width_degrees": spec.bucket_width_degrees,
        "hourly": old,
        "high_frequency": new,
        "common_day_count": len(common),
        "raw_daily_high_changed_count": raw_changed,
        "rounded_daily_high_changed_count": rounded_changed,
        "bucket_changed_count": bucket_changed,
        "bucket_changed_rate": bucket_changed / len(common) if common else None,
        "mean_absolute_daily_high_change_f": (
            sum(absolute_changes) / len(absolute_changes) if absolute_changes else None
        ),
        "median_high_time_delta_minutes": (
            float(new["median_high_minutes"]) - float(old["median_high_minutes"])
            if old["median_high_minutes"] is not None
            and new["median_high_minutes"] is not None
            else None
        ),
        "threshold_delta_minutes": metric_deltas,
        "reversal_frequency_delta": (
            float(new["reversal_frequency"]) - float(old["reversal_frequency"])
            if old["reversal_frequency"] is not None
            and new["reversal_frequency"] is not None
            else None
        ),
        "p90_remaining_warming_delta_f": (
            float(new["p90_remaining_warming_f"])
            - float(old["p90_remaining_warming_f"])
            if old["p90_remaining_warming_f"] is not None
            and new["p90_remaining_warming_f"] is not None
            else None
        ),
        "historical_feature_only": True,
        "collection_mode": HISTORICAL_BACKFILL,
    }


def build_high_frequency_reanalysis(
    specs: list[SettlementSpec],
    *,
    data_dir: Path,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    rows = []
    for spec in specs:
        if not spec.station_id or not spec.timezone:
            continue
        hourly = [
            row
            for row in load_iem_asos_csv(
                data_dir / "hourly_obs" / f"{spec.station_id}.csv",
                station_id=spec.station_id,
            )
            if start_date <= row.valid.date() <= end_date
        ]
        high_frequency = load_wrh_historical_observations(
            data_dir,
            station_id=spec.station_id,
            timezone=spec.timezone,
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(compare_station_history(spec, hourly, high_frequency))
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "collection_mode": HISTORICAL_BACKFILL,
        "strict_no_lookahead_eligible": False,
        "stations": rows,
    }


def render_high_frequency_reanalysis(result: dict[str, Any], output_path: Path) -> None:
    def clock(minutes: Any) -> str:
        if minutes is None:
            return "未达到"
        rounded = int(round(float(minutes)))
        return f"{rounded // 60:02d}:{rounded % 60:02d}"

    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.1%}"

    lines = [
        "# WRH 高频结算序列历史重算",
        "",
        f"覆盖：{result['start_date']} 至 {result['end_date']}。",
        "",
        "本报告使用 `collection_mode=historical_backfill` 的事后 WRH/Synoptic 高频序列。",
        "它适合日高、峰值时刻和气候特征重算；不代表交易当时可见的数据，严禁替代 realtime 无前视回测。",
        "",
        "## 日最高温与分桶变化",
        "",
        "| 站点 | 可比天 | 原始日高变化 | 整数日高变化 | 跨桶 | 跨桶率 | 日高绝对变化均值 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["stations"]:
        lines.append(
            f"| {row['station_id']} | {row['common_day_count']} | "
            f"{row['raw_daily_high_changed_count']} | "
            f"{row['rounded_daily_high_changed_count']} | {row['bucket_changed_count']} | "
            f"{pct(row['bucket_changed_rate'])} | "
            f"{row['mean_absolute_daily_high_change_f']:.2f}°F |"
        )
    lines.extend(
        [
            "",
            "## 确定性曲线与逆转指标前后对比",
            "",
            "| 站点 | 高点中位数 IEM→WRH | ≥70% | ≥85% | ≥95% | 逆转率 IEM→WRH | p90剩余升温 IEM→WRH |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in result["stations"]:
        old = row["hourly"]
        new = row["high_frequency"]
        lines.append(
            f"| {row['station_id']} | {clock(old['median_high_minutes'])}→"
            f"{clock(new['median_high_minutes'])} | {old['first_70']}→{new['first_70']} | "
            f"{old['first_85']}→{new['first_85']} | {old['first_95']}→{new['first_95']} | "
            f"{pct(old['reversal_frequency'])}→{pct(new['reversal_frequency'])} | "
            f"{old['p90_remaining_warming_f']:.1f}→"
            f"{new['p90_remaining_warming_f']:.1f}°F |"
        )
    lines.extend(
        [
            "",
            "## 数据边界",
            "",
            "- 美国站 WRH 通常约 5 分钟一条；ZUCK/ZUUU 约每小时一条，且 METAR T 组覆盖为 0%，中国站仍属精度降级，不能与美国站同等解读。",
            "- NCEI GHCN-Daily `TMAX` 是国家气象服务提供并经质控的官方日最大温度；美国机场按当地午夜日界。校准真值不由 IEM 小时序列计算，因此无需因本次采样频率问题重建。",
            "- 任何按历史盘口时刻声称可交易的物理余量仍必须使用 realtime 归档；本报告只做事后特征和结算日高重算。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
