from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from poly_weather.adapters.aviation_weather import parse_metar_report
from poly_weather.adapters.open_meteo import (
    DEFAULT_MULTI_MODELS,
    parse_multi_model_forecasts,
)
from poly_weather.adapters.wrh import WrhTimeseriesClient
from poly_weather.modeling import DEFAULT_MULTI_MODEL_WEIGHTS, blend_multi_model_forecasts
from poly_weather.research_store import ResearchWarehouse
from poly_weather.temperature import fahrenheit_to_celsius
from poly_weather.weather_provenance import REALTIME, CollectionMode


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
    collection_mode: CollectionMode = REALTIME


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
    last_successful_request_at: str | None = None
    last_new_observation_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class WeatherStation:
    station_id: str
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    nws_api_enabled: bool = True
    open_meteo_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "station_id", self.station_id.strip().upper())
        if not self.station_id:
            raise ValueError("station id cannot be empty")


class WeatherStreamSink:
    def __init__(
        self,
        *,
        data_dir: Path,
        run_id: str,
        archive_name: str = "weather_daemon",
        warehouse_name: str = "weather_stream.duckdb",
    ) -> None:
        self.data_dir = data_dir
        self.run_id = run_id
        self.archive_name = archive_name
        self.warehouse = ResearchWarehouse(data_dir / warehouse_name)
        self.handles: dict[tuple[str, str], Any] = {}

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        self.warehouse.close()

    def _handle(self, day: str, collection_mode: str) -> Any:
        key = (collection_mode, day)
        handle = self.handles.get(key)
        if handle is None:
            path = self.data_dir / "raw" / self.archive_name / day / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            self.handles[key] = handle
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
            if event.product == "multi_model_deterministic_forecast" and isinstance(
                event.raw, dict
            ):
                envelope["models"] = event.raw.get("models")
                envelope["blended"] = event.raw.get("blended")
            handle = self._handle(received.date().isoformat(), event.collection_mode)
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
        observation_interval_seconds: float = 120,
        metar_interval_seconds: float = 900,
        international_observation_interval_seconds: float = 1800,
        taf_interval_seconds: float = 3600,
        forecast_interval_seconds: float = 10_800,
        queue_size: int = 10_000,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.5,
        stations: Sequence[WeatherStation] | None = None,
        model_weights_by_station: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        if observation_interval_seconds < 60:
            raise ValueError("observation interval cannot be less than 60 seconds")
        if metar_interval_seconds < 60:
            raise ValueError("METAR interval cannot be less than 60 seconds")
        if international_observation_interval_seconds < 60:
            raise ValueError("international observation interval cannot be less than 60 seconds")
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
        self.metar_interval_seconds = metar_interval_seconds
        self.international_observation_interval_seconds = (
            international_observation_interval_seconds
        )
        self.taf_interval_seconds = taf_interval_seconds
        self.forecast_interval_seconds = forecast_interval_seconds
        provided_weights = model_weights_by_station or {}
        self.model_weights_by_station: dict[str, dict[str, float]] = {}
        for item in self.stations:
            weights = dict(provided_weights.get(item.station_id, DEFAULT_MULTI_MODEL_WEIGHTS))
            if set(weights) != set(DEFAULT_MULTI_MODELS):
                raise ValueError(
                    f"model weights for {item.station_id} must contain gfs, icon, and gem"
                )
            total_weight = sum(float(value) for value in weights.values())
            if total_weight <= 0:
                raise ValueError(f"model weights for {item.station_id} must sum above zero")
            self.model_weights_by_station[item.station_id] = {
                model: float(value) / total_weight for model, value in weights.items()
            }
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[WeatherEvent] = asyncio.Queue(maxsize=queue_size)
        self.stop_event = asyncio.Event()
        self.run_id = str(uuid4())
        self.sequence = 0
        self.metrics = WeatherMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.request_outcomes: deque[tuple[float, bool]] = deque(maxlen=1000)
        self.last_source_timestamp_by_product: dict[tuple[str, str], int] = {}
        self.product_metrics: dict[str, dict[str, Any]] = {}
        self.station_metrics: dict[str, dict[str, Any]] = {
            item.station_id: {
                "requests": 0,
                "events": 0,
                "errors": 0,
                "last_event_at": None,
                "last_successful_request_at": None,
                "last_error": None,
            }
            for item in self.stations
        }
        self.status_path = data_dir / "runtime" / "weather_daemon_status.json"
        self.sink = WeatherStreamSink(data_dir=data_dir, run_id=self.run_id)

    def intervals_for_station(self, station: WeatherStation) -> dict[str, float]:
        """Return the fixed realtime schedule selected for one station."""
        is_international = station.station_id.startswith("Z")
        observation_interval = (
            self.international_observation_interval_seconds
            if is_international
            else self.observation_interval_seconds
        )
        metar_interval = (
            self.international_observation_interval_seconds
            if is_international
            else self.metar_interval_seconds
        )
        return {
            "wrh_timeseries_observation": observation_interval,
            "nws": self.observation_interval_seconds,
            "metar": metar_interval,
            "taf": self.taf_interval_seconds,
            "multi_model_deterministic_forecast": self.forecast_interval_seconds,
        }

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
            wrh = WrhTimeseriesClient(client)
            workers = []
            for station in self.stations:
                intervals = self.intervals_for_station(station)
                workers.append(
                    asyncio.create_task(
                        self._scheduled(
                            name="wrh_timeseries_observation",
                            station_id=station.station_id,
                            interval=intervals["wrh_timeseries_observation"],
                            fetch=lambda station=station: self._fetch_wrh(wrh, station),
                        )
                    )
                )
                if station.nws_api_enabled:
                    workers.append(
                        asyncio.create_task(
                            self._scheduled(
                                name="nws",
                                station_id=station.station_id,
                                interval=intervals["nws"],
                                fetch=lambda station=station: self._fetch_nws(client, station),
                            )
                        )
                    )
                workers.extend(
                    (
                        asyncio.create_task(
                            self._scheduled(
                                name="metar",
                                station_id=station.station_id,
                                interval=intervals["metar"],
                                fetch=lambda station=station: self._fetch_metar(client, station),
                            )
                        ),
                        asyncio.create_task(
                            self._scheduled(
                                name="taf",
                                station_id=station.station_id,
                                interval=intervals["taf"],
                                fetch=lambda station=station: self._fetch_taf(client, station),
                            )
                        ),
                    )
                )
                if (
                    station.open_meteo_enabled
                    and station.latitude is not None
                    and station.longitude is not None
                    and station.timezone is not None
                ):
                    workers.append(
                        asyncio.create_task(
                            self._scheduled(
                                name="multi_model_deterministic_forecast",
                                station_id=station.station_id,
                                interval=intervals["multi_model_deterministic_forecast"],
                                fetch=lambda station=station: self._fetch_multi_model(
                                    client, station
                                ),
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
                writer_result = await asyncio.gather(writer, return_exceptions=True)
                status_heartbeat.cancel()
                await asyncio.gather(status_heartbeat, return_exceptions=True)
                writer_errors = [
                    result for result in writer_result if isinstance(result, BaseException)
                ]
                self.metrics.state = "failed" if writer_errors else "stopped"
                self._write_status()
                self.sink.warehouse.finish_weather_stream_run(
                    run_id=self.run_id,
                    finished_at=datetime.now(UTC),
                    metrics=asdict(self.metrics),
                )
                self.sink.close()
                if writer_errors:
                    raise RuntimeError("weather stream writer failed") from writer_errors[0]
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
                product_key = f"{station_id}:{name}"
                product_metrics = self.product_metrics.setdefault(
                    product_key,
                    {
                        "requests": 0,
                        "events": 0,
                        "errors": 0,
                        "last_successful_request_at": None,
                        "last_event_at": None,
                        "last_error": None,
                    },
                )
                product_metrics["requests"] += 1
                event = await fetch()
                now_iso = datetime.now(UTC).isoformat()
                self.request_outcomes.append((time.monotonic(), True))
                self.metrics.last_successful_request_at = now_iso
                station_metrics["last_successful_request_at"] = now_iso
                product_metrics["last_successful_request_at"] = now_iso
                product_metrics["last_error"] = None
                if event is not None:
                    event_key = (station_id, event.product)
                    source_timestamp = event.source_timestamp_ms
                    is_new = source_timestamp is None or source_timestamp > (
                        self.last_source_timestamp_by_product.get(event_key, -1)
                    )
                    if is_new:
                        if source_timestamp is not None:
                            self.last_source_timestamp_by_product[event_key] = source_timestamp
                        await self.queue.put(event)
                        self.metrics.events += 1
                        self.metrics.queue_high_water = max(
                            self.metrics.queue_high_water, self.queue.qsize()
                        )
                        self.metrics.last_event_at = now_iso
                        if event.product in {
                            "wrh_timeseries_observation",
                            "latest_observation",
                            "metar",
                        }:
                            self.metrics.last_new_observation_at = now_iso
                        station_metrics["events"] += 1
                        station_metrics["last_event_at"] = now_iso
                        product_metrics["events"] += 1
                        product_metrics["last_event_at"] = now_iso
                station_errors = [
                    value["last_error"]
                    for key, value in self.product_metrics.items()
                    if key.startswith(f"{station_id}:") and value["last_error"]
                ]
                station_metrics["last_error"] = station_errors[-1] if station_errors else None
                if not any(value["last_error"] for value in self.product_metrics.values()):
                    self.metrics.last_error = None
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                self.metrics.errors += 1
                station_metrics = self.station_metrics[station_id]
                station_metrics["errors"] += 1
                self.request_outcomes.append((time.monotonic(), False))
                error = f"{station_id}:{name}: {type(exc).__name__}: {exc}"
                product_key = f"{station_id}:{name}"
                product_metrics = self.product_metrics.setdefault(
                    product_key,
                    {
                        "requests": 1,
                        "events": 0,
                        "errors": 0,
                        "last_successful_request_at": None,
                        "last_event_at": None,
                        "last_error": None,
                    },
                )
                product_metrics["errors"] += 1
                product_metrics["last_error"] = error
                station_metrics["last_error"] = error
                self.metrics.last_error = error
                self._write_status()
                await self._wait(max(interval, min(300.0, backoff)))
                backoff = min(300.0, backoff * 2)
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
        temperature_value = temperature.get("value")
        temperature_decimal = (
            None if temperature_value is None else Decimal(str(temperature_value))
        )
        return self._event(
            provider="NOAA/NWS",
            product="latest_observation",
            source_time=source_time,
            temperature_c=temperature_value,
            raw={
                **payload,
                "_poly_weather": {
                    "temperature_precision_degraded": (
                        temperature_decimal is not None
                        and temperature_decimal.as_tuple().exponent >= 0
                    ),
                    "conversion_rounding": "convert first; round only at settlement boundary",
                },
            },
            station_id=station.station_id,
        )

    async def _fetch_wrh(
        self,
        client: WrhTimeseriesClient,
        station: WeatherStation | None = None,
    ) -> WeatherEvent:
        """Fetch the exact high-frequency series used by the WRH settlement page."""
        station = station or self.stations[0]
        observation = await client.latest(station.station_id)
        temperature_c = fahrenheit_to_celsius(observation.temperature_f)
        return self._event(
            provider="NOAA weather.gov WRH/Synoptic",
            product="wrh_timeseries_observation",
            source_time=observation.observed_at,
            temperature_c=float(temperature_c),
            raw={
                "request_url": observation.request_url,
                "temperature_f": str(observation.temperature_f),
                "settlement_source_exact": True,
                "conversion_rounding": "convert first; round only at settlement boundary",
                "source_payload": observation.raw,
            },
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
        report = parse_metar_report(latest, station_id=station.station_id)
        source_time = datetime.fromtimestamp(int(latest["obsTime"]), tz=UTC)
        return self._event(
            provider="NOAA Aviation Weather Center",
            product="metar",
            source_time=source_time,
            temperature_c=(
                float(report.temperature_c) if report.temperature_c is not None else None
            ),
            raw={
                "reports": payload,
                "selected_temperature_source": report.temperature_source,
                "temperature_precision_degraded": report.temperature_precision_degraded,
            },
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
            key=lambda row: datetime.fromisoformat(str(row["issueTime"]).replace("Z", "+00:00")),
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

    async def _fetch_multi_model(
        self, client: httpx.AsyncClient, station: WeatherStation | None = None
    ) -> WeatherEvent:
        station = station or self.stations[0]
        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": station.latitude,
                "longitude": station.longitude,
                "hourly": "temperature_2m",
                "models": ",".join(DEFAULT_MULTI_MODELS.values()),
                "temperature_unit": "fahrenheit",
                "timezone": station.timezone,
                "forecast_days": 2,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("multi-model deterministic response is not an object")
        forecasts = parse_multi_model_forecasts(
            payload,
            requested_latitude=float(station.latitude),
            requested_longitude=float(station.longitude),
            timezone=str(station.timezone),
        )
        first_forecast = next(iter(forecasts.values()))
        times = [timestamp.isoformat(timespec="minutes") for timestamp in first_forecast.times]
        models_payload = {
            model_name: {
                "model": forecast.model,
                "latitude": forecast.latitude,
                "longitude": forecast.longitude,
                "timezone": forecast.timezone,
                "temperature_unit": forecast.temperature_unit,
                "time": times,
                "temperature_2m": [
                    None if value is None else float(value) for value in forecast.values
                ],
            }
            for model_name, forecast in forecasts.items()
        }
        weights = self.model_weights_by_station[station.station_id]
        blended_values: list[float | None] = []
        for index in range(len(times)):
            values = {
                model_name: forecast.values[index]
                for model_name, forecast in forecasts.items()
                if forecast.values[index] is not None
            }
            blended_values.append(
                blend_multi_model_forecasts(values, weights)
                if len(values) == len(forecasts)
                else None
            )
        event_payload = {
            "latitude": first_forecast.latitude,
            "longitude": first_forecast.longitude,
            "timezone": first_forecast.timezone,
            "models": models_payload,
            "blended": {
                "time": times,
                "temperature_2m": blended_values,
                "temperature_unit": first_forecast.temperature_unit,
                "weights": weights,
            },
            "source_payload": payload,
        }
        return self._event(
            provider="Open-Meteo GFS+ICON+GEM deterministic blend",
            product="multi_model_deterministic_forecast",
            source_time=None,
            temperature_c=None,
            raw=event_payload,
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
        try:
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
        except Exception as exc:
            self.metrics.state = "failed"
            self.metrics.last_error = f"weather writer {type(exc).__name__}: {exc}"
            self.stop_event.set()
            self._write_status()
            raise

    async def _status_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            self._write_status()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _write_status(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        now_monotonic = time.monotonic()
        recent = [success for observed, success in self.request_outcomes if now_monotonic - observed <= 300]
        recent_errors = sum(not success for success in recent)
        recent_error_rate = recent_errors / len(recent) if recent else 0.0
        effective_state = self.metrics.state
        success_age_seconds: float | None = None
        new_observation_age_seconds: float | None = None
        if self.metrics.state == "running":
            try:
                last_success = datetime.fromisoformat(
                    str(self.metrics.last_successful_request_at)
                ).astimezone(UTC)
                success_age_seconds = (datetime.now(UTC) - last_success).total_seconds()
            except (TypeError, ValueError):
                success_age_seconds = float("inf")
            try:
                last_new_observation = datetime.fromisoformat(
                    str(self.metrics.last_new_observation_at)
                ).astimezone(UTC)
                new_observation_age_seconds = (
                    datetime.now(UTC) - last_new_observation
                ).total_seconds()
            except (TypeError, ValueError):
                new_observation_age_seconds = float("inf")
            if self.metrics.requests >= 10 and (
                success_age_seconds > 300 or new_observation_age_seconds > 1200
            ):
                effective_state = "stalled"
            elif len(recent) >= 10 and recent_error_rate >= 0.25:
                effective_state = "degraded"
        payload = {
            **asdict(self.metrics),
            "state": effective_state,
            "base_state": self.metrics.state,
            "pid": os.getpid(),
            "station_id": self.station_id if len(self.stations) == 1 else None,
            "station_ids": [station.station_id for station in self.stations],
            "stations": self.station_metrics,
            "products": self.product_metrics,
            "recent_request_count_5m": len(recent),
            "recent_error_count_5m": recent_errors,
            "recent_error_rate_5m": recent_error_rate,
            "last_success_age_seconds": success_age_seconds,
            "last_new_observation_age_seconds": new_observation_age_seconds,
            "new_observation_stall_threshold_seconds": 1200,
            "intervals_seconds": {
                "wrh_nws_us": self.observation_interval_seconds,
                "metar_us": self.metar_interval_seconds,
                "wrh_metar_international": self.international_observation_interval_seconds,
                "taf": self.taf_interval_seconds,
                "forecast": self.forecast_interval_seconds,
            },
            "model_weights_by_station": self.model_weights_by_station,
            "queue_depth": self.queue.qsize(),
            "updated_at": datetime.now(UTC).isoformat(),
            "read_only": True,
        }
        temporary = self.status_path.with_name(f".{self.status_path.name}.{self.run_id}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(self.status_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
