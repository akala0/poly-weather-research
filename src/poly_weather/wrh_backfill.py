"""Batch WRH history collection, isolated from realtime no-lookahead data."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from poly_weather.adapters.wrh import WrhHistory, WrhTimeseriesClient
from poly_weather.temperature import fahrenheit_to_celsius
from poly_weather.weather_provenance import HISTORICAL_BACKFILL, REALTIME
from poly_weather.weather_stream import WeatherEvent, WeatherStreamSink

_TOKEN_QUERY = re.compile(r"([?&]token=)[^&]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WrhBackfillRequest:
    station_id: str
    timezone: str
    start_date: date
    end_date: date

    def utc_interval(self) -> tuple[datetime, datetime]:
        if self.end_date < self.start_date:
            raise ValueError("WRH backfill end date cannot precede start date")
        day_count = (self.end_date - self.start_date).days + 1
        if day_count > 30:
            raise ValueError("WRH backfill cannot exceed 30 calendar days")
        zone = ZoneInfo(self.timezone)
        start = datetime.combine(self.start_date, datetime_time.min, tzinfo=zone)
        exclusive_end = datetime.combine(
            self.end_date + timedelta(days=1), datetime_time.min, tzinfo=zone
        )
        return start.astimezone(UTC), (exclusive_end - timedelta(seconds=1)).astimezone(UTC)


def partition_wrh_backfill_range(
    *,
    station_id: str,
    timezone: str,
    start_date: date,
    end_date: date,
    days_per_request: int = 29,
) -> list[WrhBackfillRequest]:
    """Partition a long range without ever exceeding the public 30-day limit."""
    if end_date < start_date:
        raise ValueError("WRH backfill end date cannot precede start date")
    if not 1 <= days_per_request <= 29:
        raise ValueError("days_per_request must be between 1 and 29")
    requests = []
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(end_date, cursor + timedelta(days=days_per_request - 1))
        requests.append(
            WrhBackfillRequest(
                station_id=station_id,
                timezone=timezone,
                start_date=cursor,
                end_date=chunk_end,
            )
        )
        cursor = chunk_end + timedelta(days=1)
    return requests


async def cache_wrh_temperature_history(
    requests: list[WrhBackfillRequest],
    *,
    data_dir: Path,
    max_concurrency: int = 4,
) -> dict[str, Any]:
    """Cache only timestamp/temperature arrays for large historical reanalysis.

    The full backfill path remains available when a normalized DuckDB copy and
    all response fields are needed. This lean path preserves the same explicit
    post-hoc provenance while avoiding multi-year duplication of unrelated
    cloud, wind, and pressure arrays.
    """
    if not requests:
        raise ValueError("at least one WRH backfill request is required")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    for request in requests:
        request.utc_interval()
    semaphore = asyncio.Semaphore(max_concurrency)
    timeout = httpx.Timeout(90, connect=20, read=90, write=30, pool=90)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=max_concurrency,
            max_keepalive_connections=max_concurrency,
        ),
        headers={"User-Agent": "poly-weather/0.1 (historical research; read-only)"},
        follow_redirects=True,
    ) as http:
        client = WrhTimeseriesClient(http)

        async def fetch(request: WrhBackfillRequest) -> dict[str, Any]:
            station = request.station_id.upper()
            directory = data_dir / "raw" / "wrh_history_batches" / station
            prefix = f"{request.start_date}_{request.end_date}_"
            existing = sorted(directory.glob(f"{prefix}*.json"))
            if existing:
                return {
                    "station_id": station,
                    "start_date": request.start_date.isoformat(),
                    "end_date": request.end_date.isoformat(),
                    "status": "cached",
                    "path": str(existing[-1].resolve()),
                }
            start, end = request.utc_interval()
            history = None
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    async with semaphore:
                        history = await client.history(station, start=start, end=end)
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt == 3:
                        raise
                    await asyncio.sleep(0.5 * (2**attempt))
            if history is None:
                raise RuntimeError("WRH history retry loop ended without a result") from last_error
            path = directory / f"{prefix}temperature_only.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "collection_mode": HISTORICAL_BACKFILL,
                "station_id": station,
                "timezone": request.timezone,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "request_url": _redact_token(history.request_url),
                "qc_status": "preliminary_and_subject_to_post_hoc_qc",
                "temperature_only": True,
                "payload": {
                    "STATION": [
                        {
                            "OBSERVATIONS": {
                                "date_time": [
                                    row.observed_at.isoformat().replace("+00:00", "Z")
                                    for row in history.observations
                                ],
                                "air_temp_set_1": [
                                    float(row.temperature_f) for row in history.observations
                                ],
                            }
                        }
                    ]
                },
            }
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
            return {
                "station_id": station,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "status": "fetched",
                "observation_count": len(history.observations),
                "path": str(path.resolve()),
            }

        results = await asyncio.gather(*(fetch(request) for request in requests))
    return {
        "collection_mode": HISTORICAL_BACKFILL,
        "request_count": len(results),
        "fetched_count": sum(row["status"] == "fetched" for row in results),
        "cached_count": sum(row["status"] == "cached" for row in results),
        "observation_count": sum(int(row.get("observation_count") or 0) for row in results),
        "results": results,
        "execution_enabled": False,
    }


def _redact_token(url: str) -> str:
    return _TOKEN_QUERY.sub(r"\1REDACTED", url)


def _payload_points(payload: dict[str, Any]) -> dict[int, float]:
    stations = payload.get("STATION")
    if not isinstance(stations, list) or not stations or not isinstance(stations[0], dict):
        return {}
    observations = stations[0].get("OBSERVATIONS")
    if not isinstance(observations, dict):
        return {}
    timestamps = observations.get("date_time")
    temperatures = observations.get("air_temp_set_1")
    if not isinstance(timestamps, list) or not isinstance(temperatures, list):
        return {}
    return {
        int(
            datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            .astimezone(UTC)
            .timestamp()
            * 1000
        ): float(temperature)
        for timestamp, temperature in zip(timestamps, temperatures, strict=False)
        if temperature is not None
    }


def realtime_wrh_points(
    data_dir: Path,
    *,
    station_id: str,
    start: datetime,
    end: datetime,
) -> dict[int, float]:
    """Read only realtime archives; historical directories are never considered."""
    return realtime_wrh_points_many(
        data_dir,
        intervals={station_id.upper(): (start, end)},
    )[station_id.upper()]


def realtime_wrh_points_many(
    data_dir: Path,
    *,
    intervals: dict[str, tuple[datetime, datetime]],
) -> dict[str, dict[int, float]]:
    """Scan realtime JSONL once for every requested station interval."""
    output: dict[str, dict[int, float]] = {station: {} for station in intervals}
    for path in sorted((data_dir / "raw" / "weather_daemon").glob("*/events.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if "wrh_timeseries_observation" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("collection_mode") != REALTIME:
                    continue
                station_id = str(row.get("station_id") or "").upper()
                if station_id not in intervals:
                    continue
                raw = row.get("raw")
                payload = raw.get("source_payload") if isinstance(raw, dict) else None
                if not isinstance(payload, dict):
                    continue
                start, end = intervals[station_id]
                for timestamp_ms, temperature_f in _payload_points(payload).items():
                    observed = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
                    if start <= observed <= end:
                        output[station_id][timestamp_ms] = temperature_f
    return output


def compare_history_to_realtime(
    history: WrhHistory,
    realtime: dict[int, float],
) -> dict[str, Any]:
    historical = {
        int(row.observed_at.timestamp() * 1000): float(row.temperature_f)
        for row in history.observations
    }
    overlap = sorted(set(historical) & set(realtime))
    mismatches = [
        timestamp
        for timestamp in overlap
        if abs(historical[timestamp] - realtime[timestamp]) > 1e-9
    ]
    return {
        "station_id": history.station_id,
        "historical_point_count": len(historical),
        "realtime_point_count": len(realtime),
        "overlap_count": len(overlap),
        "matching_overlap_count": len(overlap) - len(mismatches),
        "mismatch_count": len(mismatches),
        "mismatch_rate": len(mismatches) / len(overlap) if overlap else None,
        "backfilled_missing_count": len(set(historical) - set(realtime)),
        "max_absolute_difference_f": max(
            (abs(historical[timestamp] - realtime[timestamp]) for timestamp in mismatches),
            default=0.0,
        ),
    }


async def backfill_wrh_history(
    requests: list[WrhBackfillRequest],
    *,
    data_dir: Path,
) -> dict[str, Any]:
    """Fetch normalized history into a separate archive and DuckDB writer."""
    if not requests:
        raise ValueError("at least one WRH backfill request is required")
    for request in requests:
        request.utc_interval()
    intervals = {
        request.station_id.upper(): request.utc_interval() for request in requests
    }
    realtime_by_station = realtime_wrh_points_many(data_dir, intervals=intervals)
    run_id = f"wrh-history-{uuid4()}"
    received_at_ns = time.time_ns()
    sink = WeatherStreamSink(
        data_dir=data_dir,
        run_id=run_id,
        archive_name="wrh_historical_backfill",
        warehouse_name="weather_history.duckdb",
    )
    sink.warehouse.start_weather_stream_run(
        run_id=run_id,
        started_at=datetime.now(UTC),
        station_id=",".join(request.station_id for request in requests),
    )
    station_results: list[dict[str, Any]] = []
    sequence = 0
    try:
        timeout = httpx.Timeout(60, connect=15, pool=60)
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            headers={"User-Agent": "poly-weather/0.1 (historical research; read-only)"},
            follow_redirects=True,
        ) as http:
            client = WrhTimeseriesClient(http)
            for request in requests:
                start, end = request.utc_interval()
                history = await client.history(
                    request.station_id,
                    start=start,
                    end=end,
                )
                realtime = realtime_by_station[request.station_id.upper()]
                comparison = compare_history_to_realtime(history, realtime)
                batch_path = (
                    data_dir
                    / "raw"
                    / "wrh_history_batches"
                    / request.station_id.upper()
                    / f"{request.start_date}_{request.end_date}_{run_id}.json"
                )
                batch_path.parent.mkdir(parents=True, exist_ok=True)
                batch_path.write_text(
                    json.dumps(
                        {
                            "collection_mode": HISTORICAL_BACKFILL,
                            "station_id": request.station_id.upper(),
                            "timezone": request.timezone,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "request_url": _redact_token(history.request_url),
                            "qc_status": "preliminary_and_subject_to_post_hoc_qc",
                            "payload": history.raw,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                events: list[WeatherEvent] = []
                for observation in history.observations:
                    sequence += 1
                    events.append(
                        WeatherEvent(
                            run_id=run_id,
                            sequence=sequence,
                            received_at_ns=received_at_ns + sequence,
                            source_timestamp_ms=int(
                                observation.observed_at.timestamp() * 1000
                            ),
                            provider="NOAA weather.gov WRH/Synoptic",
                            product="wrh_timeseries_observation",
                            station_id=observation.station_id,
                            temperature_c=float(
                                fahrenheit_to_celsius(observation.temperature_f)
                            ),
                            latency_ms=None,
                            raw={
                                "temperature_f": str(observation.temperature_f),
                                "request_url": _redact_token(history.request_url),
                                "batch_path": str(batch_path.resolve()),
                                "qc_status": (
                                    "preliminary_and_subject_to_post_hoc_qc"
                                ),
                            },
                            collection_mode=HISTORICAL_BACKFILL,
                        )
                    )
                sink.write(events)
                station_results.append(
                    {
                        **comparison,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "batch_path": str(batch_path.resolve()),
                    }
                )
        totals = {
            key: sum(int(row[key]) for row in station_results)
            for key in (
                "historical_point_count",
                "realtime_point_count",
                "overlap_count",
                "matching_overlap_count",
                "mismatch_count",
                "backfilled_missing_count",
            )
        }
        result = {
            "run_id": run_id,
            "collection_mode": HISTORICAL_BACKFILL,
            "stations": station_results,
            "totals": {
                **totals,
                "mismatch_rate": (
                    totals["mismatch_count"] / totals["overlap_count"]
                    if totals["overlap_count"]
                    else None
                ),
            },
            "execution_enabled": False,
        }
        sink.warehouse.finish_weather_stream_run(
            run_id=run_id,
            finished_at=datetime.now(UTC),
            metrics=result,
        )
        return result
    finally:
        sink.close()


def render_wrh_backfill_report(result: dict[str, Any], output_path: Path) -> None:
    def pct(value: Any) -> str:
        return "N/A" if value is None else f"{float(value):.2%}"

    lines = [
        "# WRH 历史批量回填报告",
        "",
        "历史回填与实时数据物理隔离；回填可用于训练和补洞，禁止用于严格无前视复现。",
        "",
        "| 站点 | 历史点 | 实时点 | 重叠 | 数值不一致 | QC差异率 | 补齐缺失 | 最大差值 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["stations"]:
        lines.append(
            f"| {row['station_id']} | {row['historical_point_count']} | "
            f"{row['realtime_point_count']} | {row['overlap_count']} | "
            f"{row['mismatch_count']} | {pct(row['mismatch_rate'])} | "
            f"{row['backfilled_missing_count']} | "
            f"{row['max_absolute_difference_f']:.3f}°F |"
        )
    totals = result["totals"]
    lines.extend(
        [
            "",
            f"- 总历史点：{totals['historical_point_count']}",
            f"- 补齐实时缺失：{totals['backfilled_missing_count']}",
            f"- 重叠点：{totals['overlap_count']}",
            f"- QC 数值不一致：{totals['mismatch_count']} "
            f"({pct(totals['mismatch_rate'])})",
            "- `collection_mode=historical_backfill`；`execution_enabled=false`。",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
