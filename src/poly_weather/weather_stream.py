from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from poly_weather.research_store import ResearchWarehouse


@dataclass(slots=True)
class WeatherEvent:
    run_id: str
    sequence: int
    received_at_ns: int
    source_timestamp_ms: int | None
    provider: str
    product: str
    station_id: str
    temperature_c: float | None
    latency_ms: int | None
    raw: dict[str, Any] | list[dict[str, Any]]


@dataclass(slots=True)
class WeatherMetrics:
    run_id: str
    started_at: str
    state: str = "starting"
    requests: int = 0
    events: int = 0
    rows_written: int = 0
    errors: int = 0
    queue_high_water: int = 0
    last_event_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class WeatherStation:
    station_id: str
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "station_id", self.station_id.strip().upper())
        if not self.station_id:
            raise ValueError("station id cannot be empty")


class WeatherStreamSink:
    def __init__(self, *, data_dir: Path, run_id: str) -> None:
        self.data_dir = data_dir
        self.run_id = run_id
        self.warehouse = ResearchWarehouse(data_dir / "weather_stream.duckdb")
        self.handles: dict[str, Any] = {}

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        self.warehouse.close()

    def _handle(self, day: str) -> Any:
        handle = self.handles.get(day)
        if handle is None:
            path = self.data_dir / "raw" / "weather_daemon" / day / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            self.handles[day] = handle
        return handle

    def write(self, events: list[WeatherEvent]) -> None:
        if not events:
            return
        for event in events:
            received = datetime.fromtimestamp(event.received_at_ns / 1_000_000_000, tz=UTC)
            envelope = {
                **asdict(event),
                "received_at": received.isoformat(),
            }
            handle = self._handle(received.date().isoformat())
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        for handle in self.handles.values():
            handle.flush()
        self.warehouse.append_weather_stream_events(events)


class WeatherDaemon:
    """Independent multi-station NOAA/NWS and Aviation Weather daemon."""

    def __init__(
        self,
        *,
        station_id: str | None = None,
        data_dir: Path,
        latitude: float | None = None,
        longitude: float | None = None,
        timezone: str | None = None,
        observation_interval_seconds: float = 60,
        taf_interval_seconds: float = 600,
        forecast_interval_seconds: float = 10_800,
        queue_size: int = 10_000,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.5,
        stations: Sequence[WeatherStation] | None = None,
    ) -> None:
        if observation_interval_seconds < 60:
            raise ValueError("observation interval cannot be less than 60 seconds")
        if taf_interval_seconds < 600:
            raise ValueError("TAF interval cannot be less than 600 seconds")
        if forecast_interval_seconds < 3_600:
            raise ValueError("forecast interval cannot be less than 3600 seconds")
        if stations is not None and station_id is not None:
            raise ValueError("provide station_id or stations, not both")
        if stations is None:
            if station_id is None:
                raise ValueError("at least one weather station is required")
            stations = (
                WeatherStation(
                    station_id=station_id,
                    latitude=latitude,
                    longitude=longitude,
                    timezone=timezone,
                ),
            )
        normalized_stations = tuple(stations)
        if not normalized_stations:
            raise ValueError("at least one weather station is required")
        station_ids = [station.station_id for station in normalized_stations]
        if len(station_ids) != len(set(station_ids)):
            raise ValueError("weather station ids must be unique")
        self.stations = normalized_stations
        self.station_id = normalized_stations[0].station_id
        self.data_dir = data_dir
        self.latitude = normalized_stations[0].latitude
        self.longitude = normalized_stations[0].longitude
        self.timezone = normalized_stations[0].timezone
        self.observation_interval_seconds = observation_interval_seconds
        self.taf_interval_seconds = taf_interval_seconds
        self.forecast_interval_seconds = forecast_interval_seconds
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[WeatherEvent] = asyncio.Queue(maxsize=queue_size)
        self.stop_event = asyncio.Event()
        self.run_id = str(uuid4())
        self.sequence = 0
        self.metrics = WeatherMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.station_metrics: dict[str, dict[str, Any]] = {
            item.station_id: {
                "requests": 0,
                "events": 0,
                "errors": 0,
                "last_event_at": None,
                "last_error": None,
            }
            for item in self.stations
        }
        self.status_path = data_dir / "runtime" / "weather_daemon_status.json"
        self.sink = WeatherStreamSink(data_dir=data_dir, run_id=self.run_id)

    async def run(self, *, runtime_seconds: float = 0) -> WeatherMetrics:
        self.sink.warehouse.start_weather_stream_run(
            run_id=self.run_id,
            started_at=datetime.now(UTC),
            station_id=",".join(station.station_id for station in self.stations),
        )
        timeout = httpx.Timeout(30, connect=10, pool=30)
        # Ten stations can schedule WRH, NWS, METAR, TAF, and forecast fetches
        # together at startup. Keep the client pool above that deterministic burst.
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
        headers = {
            "User-Agent": "poly-weather/0.1 (research; read-only)",
            "Accept": "application/json, application/geo+json",
        }
        async with httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers=headers,
            follow_redirects=True,
            http2=False,
        ) as client:
            workers = []
            for station in self.stations:
                workers.extend(
                    (
                        asyncio.create_task(
                            self._scheduled(
                                name="nws",
                                station_id=station.station_id,
                                interval=self.observation_interval_seconds,
                                fetch=lambda station=station: self._fetch_nws(client, station),
                            )
                        ),
                        asyncio.create_task(
                            self._scheduled(
                                name="metar",
                                station_id=station.station_id,
                                interval=self.observation_interval_seconds,
                                fetch=lambda station=station: self._fetch_metar(client, station),
                            )
                        ),
                        asyncio.create_task(
                            self._scheduled(
                                name="taf",
                                station_id=station.station_id,
                                interval=self.taf_interval_seconds,
                                fetch=lambda station=station: self._fetch_taf(client, station),
                            )
                        ),
                    )
                )
                if (
                    station.latitude is not None
                    and station.longitude is not None
                    and station.timezone is not None
                ):
                    workers.append(
                        asyncio.create_task(
                            self._scheduled(
                                name="gefs",
                                station_id=station.station_id,
                                interval=self.forecast_interval_seconds,
                                fetch=lambda station=station: self._fetch_gefs(client, station),
                            )
                        )
                    )
            writer = asyncio.create_task(self._writer())
            status_heartbeat = asyncio.create_task(self._status_heartbeat())
            timer = (
                asyncio.create_task(self._stop_after(runtime_seconds))
                if runtime_seconds > 0
                else None
            )
            self.metrics.state = "running"
            self._write_status()
            try:
                await self.stop_event.wait()
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                if timer:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                await writer
                status_heartbeat.cancel()
                await asyncio.gather(status_heartbeat, return_exceptions=True)
                self.metrics.state = "stopped"
                self._write_status()
                self.sink.warehouse.finish_weather_stream_run(
                    run_id=self.run_id,
                    finished_at=datetime.now(UTC),
                    metrics=asdict(self.metrics),
                )
                self.sink.close()
        return self.metrics

    async def _stop_after(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.stop_event.set()

    async def _scheduled(
        self,
        *,
        name: str,
        station_id: str,
        interval: float,
        fetch: Callable[[], Awaitable[WeatherEvent | None]],
    ) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.metrics.requests += 1
                station_metrics = self.station_metrics[station_id]
                station_metrics["requests"] += 1
                event = await fetch()
                if event is not None:
                    await self.queue.put(event)
                    self.metrics.events += 1
                    self.metrics.queue_high_water = max(
                        self.metrics.queue_high_water, self.queue.qsize()
                    )
                    self.metrics.last_event_at = datetime.now(UTC).isoformat()
                    station_metrics["events"] += 1
                    station_metrics["last_event_at"] = self.metrics.last_event_at
                station_metrics["last_error"] = None
                if not any(item["last_error"] for item in self.station_metrics.values()):
                    self.metrics.last_error = None
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.metrics.errors += 1
                station_metrics = self.station_metrics[station_id]
                station_metrics["errors"] += 1
                error = f"{station_id}:{name}: {type(exc).__name__}: {exc}"
                station_metrics["last_error"] = error
                self.metrics.last_error = error
                self._write_status()
                await self._wait(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)
                continue
            elapsed = time.monotonic() - started
            await self._wait(max(0.0, interval - elapsed))

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _fetch_nws(
        self, client: httpx.AsyncClient, station: WeatherStation | None = None
    ) -> WeatherEvent:
        station = station or self.stations[0]
        response = await client.get(
            f"https://api.weather.gov/stations/{station.station_id}/observations/latest"
        )
        response.raise_for_status()
        payload = response.json()
        properties = payload["properties"]
        source_time = datetime.fromisoformat(str(properties["timestamp"]).replace("Z", "+00:00"))
        temperature = properties.get("temperature") or {}
        return self._event(
            provider="NOAA/NWS",
            product="latest_observation",
            source_time=source_time,
            temperature_c=temperature.get("value"),
            raw=payload,
            station_id=station.station_id,
        )

    async def _fetch_metar(
        self, client: httpx.AsyncClient, station: WeatherStation | None = None
    ) -> WeatherEvent | None:
        station = station or self.stations[0]
        response = await client.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": station.station_id, "format": "json", "hours": 2},
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None
        latest = max(payload, key=lambda row: int(row["obsTime"]))
        source_time = datetime.fromtimestamp(int(latest["obsTime"]), tz=UTC)
        return self._event(
            provider="NOAA Aviation Weather Center",
            product="metar",
            source_time=source_time,
            temperature_c=latest.get("temp"),
            raw=payload,
            station_id=station.station_id,
        )

    async def _fetch_taf(
        self, client: httpx.AsyncClient, station: WeatherStation | None = None
    ) -> WeatherEvent | None:
        station = station or self.stations[0]
        response = await client.get(
            "https://aviationweather.gov/api/data/taf",
            params={"ids": station.station_id, "format": "json"},
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            return None
        latest = max(
            payload,
            key=lambda row: datetime.fromisoformat(
                str(row["issueTime"]).replace("Z", "+00:00")
            ),
        )
        source_time = datetime.fromisoformat(str(latest["issueTime"]).replace("Z", "+00:00"))
        return self._event(
            provider="NOAA Aviation Weather Center",
            product="taf",
            source_time=source_time,
            temperature_c=None,
            raw=payload,
            station_id=station.station_id,
        )

    async def _fetch_gefs(
        self, client: httpx.AsyncClient, station: WeatherStation | None = None
    ) -> WeatherEvent:
        station = station or self.stations[0]
        response = await client.get(
            "https://ensemble-api.open-meteo.com/v1/ensemble",
            params={
                "latitude": station.latitude,
                "longitude": station.longitude,
                "hourly": "temperature_2m",
                "models": "gfs_seamless",
                "temperature_unit": "fahrenheit",
                "timezone": station.timezone,
                "forecast_days": 2,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
            raise ValueError("GEFS response has no hourly object")
        return self._event(
            provider="NOAA GEFS via Open-Meteo",
            product="gefs_ensemble_forecast",
            source_time=None,
            temperature_c=None,
            raw=payload,
            station_id=station.station_id,
        )

    def _event(
        self,
        *,
        provider: str,
        product: str,
        source_time: datetime | None,
        temperature_c: float | None,
        raw: dict[str, Any] | list[dict[str, Any]],
        station_id: str | None = None,
    ) -> WeatherEvent:
        self.sequence += 1
        received_at_ns = time.time_ns()
        source_timestamp_ms = int(source_time.timestamp() * 1000) if source_time else None
        return WeatherEvent(
            run_id=self.run_id,
            sequence=self.sequence,
            received_at_ns=received_at_ns,
            source_timestamp_ms=source_timestamp_ms,
            provider=provider,
            product=product,
            station_id=station_id or self.station_id,
            temperature_c=None if temperature_c is None else float(temperature_c),
            latency_ms=(
                received_at_ns // 1_000_000 - source_timestamp_ms
                if source_timestamp_ms is not None
                else None
            ),
            raw=raw,
        )

    async def _writer(self) -> None:
        while not self.stop_event.is_set() or not self.queue.empty():
            batch: list[WeatherEvent] = []
            try:
                first = await asyncio.wait_for(
                    self.queue.get(), timeout=self.flush_interval_seconds
                )
                batch.append(first)
            except TimeoutError:
                self._write_status()
                continue
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await asyncio.to_thread(self.sink.write, batch)
            for _ in batch:
                self.queue.task_done()
            self.metrics.rows_written += len(batch)
            self._write_status()

    async def _status_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            self._write_status()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _write_status(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **asdict(self.metrics),
            "pid": os.getpid(),
            "station_id": self.station_id if len(self.stations) == 1 else None,
            "station_ids": [station.station_id for station in self.stations],
            "stations": self.station_metrics,
            "queue_depth": self.queue.qsize(),
            "updated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.status_path)
