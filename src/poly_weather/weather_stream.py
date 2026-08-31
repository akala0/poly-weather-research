from __future__ import annotations

import asyncio
import json
import os
import subprocess
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
from poly_weather.information_clock import payload_hash
from poly_weather.modeling import DEFAULT_MULTI_MODEL_WEIGHTS, blend_multi_model_forecasts
from poly_weather.research_store import ResearchWarehouse
from poly_weather.runtime_safety import atomic_json_write, process_memory_status
from poly_weather.temperature import fahrenheit_to_celsius
from poly_weather.weather_provenance import REALTIME, CollectionMode

HTTP_POOL_DIAGNOSTIC_INTERVAL_SECONDS = 30.0
HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD = 10
HTTP_POOL_REBUILD_CONSECUTIVE_SAMPLES = 3
HTTP_REQUEST_CONCURRENCY = 20
WEATHER_DUCKDB_MEMORY_LIMIT = "256MB"
WEATHER_DUCKDB_MAX_TEMP_DIRECTORY_SIZE = "4GB"
WEATHER_DUCKDB_BATCH_SIZE = 16


def _process_tcp_connections(pid: int) -> dict[str, Any]:
    """Count this process' TCP endpoints without adding a psutil dependency.

    Windows is the production runtime. ``netstat`` is sampled off the event loop
    by the diagnostics heartbeat, so this command cannot delay weather fetches.
    """
    if os.name != "nt":
        return {
            "supported": False,
            "reason": f"TCP process accounting is not implemented for {os.name}",
        }
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"supported": False, "reason": f"{type(exc).__name__}: {exc}"}

    states: dict[str, int] = {}
    total = 0
    external = 0
    established = 0
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[-1] != str(pid):
            continue
        total += 1
        state = parts[-2].upper()
        states[state] = states.get(state, 0) + 1
        if state == "ESTABLISHED":
            established += 1
        remote = parts[2].casefold()
        is_loopback = remote.startswith("127.") or remote.startswith("[::1]")
        is_unconnected = remote.startswith("0.0.0.0:") or remote.startswith("[::]:")
        if not is_loopback and not is_unconnected:
            external += 1
    return {
        "supported": True,
        "total": total,
        "established": established,
        "external": external,
        "states": states,
    }


def _http_pool_diagnostics(client: httpx.AsyncClient | None) -> dict[str, Any]:
    """Snapshot all httpcore pools, including environment-proxy mounts.

    This deliberately tolerates httpx/httpcore private-attribute drift. Losing
    diagnostics must never take down the read-only weather collector.
    """
    if client is None:
        return {"supported": False, "reason": "HTTP client not initialized"}
    try:
        transports: list[tuple[str, Any]] = [("default", client._transport)]  # noqa: SLF001
        for pattern, transport in client._mounts.items():  # noqa: SLF001
            if transport is not None:
                transports.append((str(pattern.pattern), transport))
        seen: set[int] = set()
        pools: list[dict[str, Any]] = []
        for name, transport in transports:
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            pool = getattr(transport, "_pool", None)
            connections = list(getattr(pool, "_connections", ()))
            requests = list(getattr(pool, "_requests", ()))
            infos: dict[str, int] = {}
            idle = available = closed = active = 0
            for connection in connections:
                try:
                    is_idle = bool(connection.is_idle())
                    is_available = bool(connection.is_available())
                    is_closed = bool(connection.is_closed())
                    idle += is_idle
                    available += is_available
                    closed += is_closed
                    active += not is_idle and not is_closed
                    info = str(connection.info())
                except Exception as exc:  # diagnostics only; private API may change
                    info = f"diagnostic-error:{type(exc).__name__}"
                state = (
                    "connecting"
                    if info == "CONNECTING"
                    else "failed"
                    if info == "CONNECTION FAILED"
                    else "other"
                )
                infos[state] = infos.get(state, 0) + 1
            queued = sum(
                bool(request.is_queued())
                for request in requests
                if callable(getattr(request, "is_queued", None))
            )
            pools.append(
                {
                    "name": name,
                    "pool_type": type(pool).__name__ if pool is not None else None,
                    "max_connections": getattr(pool, "_max_connections", None),
                    "max_keepalive_connections": getattr(
                        pool, "_max_keepalive_connections", None
                    ),
                    "connections_total": len(connections),
                    "connections_idle": idle,
                    "connections_in_use": active,
                    "connections_available": available,
                    "connections_closed": closed,
                    "connections_connecting": infos.get("connecting", 0),
                    "connections_failed": infos.get("failed", 0),
                    "requests_total": len(requests),
                    "requests_queued": queued,
                    "requests_assigned": len(requests) - queued,
                }
            )
        return {
            "supported": True,
            "proxy_mounts_present": any(row["pool_type"] == "AsyncHTTPProxy" for row in pools),
            "pools": pools,
            "connections_total": sum(row["connections_total"] for row in pools),
            "connections_idle": sum(row["connections_idle"] for row in pools),
            "connections_in_use": sum(row["connections_in_use"] for row in pools),
            "connections_connecting": sum(row["connections_connecting"] for row in pools),
            "requests_total": sum(row["requests_total"] for row in pools),
            "requests_queued": sum(row["requests_queued"] for row in pools),
        }
    except Exception as exc:  # diagnostics only; private API may change
        return {"supported": False, "reason": f"{type(exc).__name__}: {exc}"}


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
        warehouse_path: Path | None = None,
        memory_limit: str = WEATHER_DUCKDB_MEMORY_LIMIT,
        temp_directory: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.run_id = run_id
        self.archive_name = archive_name
        self.warehouse_path = warehouse_path or data_dir / warehouse_name
        self.temp_directory = temp_directory or data_dir / "tmp" / "duckdb-weather"
        self.memory_limit = memory_limit
        self.warehouse = ResearchWarehouse(
            self.warehouse_path,
            memory_limit=memory_limit,
            temp_directory=self.temp_directory,
            threads=1,
            max_temp_directory_size=WEATHER_DUCKDB_MAX_TEMP_DIRECTORY_SIZE,
        )
        self.handles: dict[tuple[str, str], Any] = {}
        self.last_raw_batch_rows = 0
        self.last_raw_batch_bytes = 0

    def close(self) -> None:
        close_error: BaseException | None = None
        for handle in self.handles.values():
            try:
                handle.close()
            except BaseException as exc:
                close_error = close_error or exc
        self.handles.clear()
        try:
            self.warehouse.close()
        except BaseException as exc:
            close_error = close_error or exc
        if close_error is not None:
            raise RuntimeError("weather sink close failed") from close_error

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
        encoded_bytes = 0
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
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            encoded_bytes += len(encoded.encode("utf-8")) + 1
            handle.write(encoded)
            handle.write("\n")
        for handle in self.handles.values():
            handle.flush()
        self.last_raw_batch_rows = len(events)
        self.last_raw_batch_bytes = encoded_bytes
        self.warehouse.append_weather_stream_events(events)

    def status(self) -> dict[str, Any]:
        return {
            "raw_batch_rows": self.last_raw_batch_rows,
            "raw_batch_bytes": self.last_raw_batch_bytes,
            "configured_batch_size": WEATHER_DUCKDB_BATCH_SIZE,
            "process_memory": process_memory_status(),
            "database": self.warehouse.database_status(),
        }


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
        warehouse_path: Path | None = None,
        database_memory_limit: str = WEATHER_DUCKDB_MEMORY_LIMIT,
        database_temp_directory: Path | None = None,
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
        if batch_size < 1:
            raise ValueError("weather writer batch size must be positive")
        self.batch_size = min(batch_size, WEATHER_DUCKDB_BATCH_SIZE)
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[WeatherEvent] = asyncio.Queue(maxsize=queue_size)
        self.stop_event = asyncio.Event()
        self.run_id = str(uuid4())
        self.sequence = 0
        self.metrics = WeatherMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.request_outcomes: deque[tuple[float, bool]] = deque(maxlen=1000)
        self.last_source_timestamp_by_product: dict[tuple[str, str], int] = {}
        self.last_payload_hash_by_product: dict[tuple[str, str], str] = {}
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
        self.pool_samples_path = data_dir / "runtime" / "weather_http_pool_samples.jsonl"
        self.http_client: httpx.AsyncClient | None = None
        self.wrh_client: WrhTimeseriesClient | None = None
        self.http_request_slots = asyncio.Semaphore(HTTP_REQUEST_CONCURRENCY)
        self.http_pool_gap_consecutive_samples = 0
        self.http_pool_rebuild_count = 0
        self.last_http_pool_rebuild_at: str | None = None
        self.last_http_pool_rebuild_reason: str | None = None
        self.http_pool_health: dict[str, Any] = {
            "sampled_at": None,
            "pool": {"supported": False, "reason": "not sampled"},
            "process_tcp": {"supported": False, "reason": "not sampled"},
            "connection_accounting_gap": None,
        }
        self.sink = WeatherStreamSink(
            data_dir=data_dir,
            run_id=self.run_id,
            warehouse_path=warehouse_path,
            memory_limit=database_memory_limit,
            temp_directory=database_temp_directory,
        )

    @staticmethod
    def _new_http_client() -> httpx.AsyncClient:
        # Keep all four timeout phases explicit. Requests are never wrapped in
        # an outer wait_for; httpx owns timeout/finally semantics end-to-end.
        timeout = httpx.Timeout(connect=10, read=30, write=30, pool=30)
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=40)
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            headers={
                "User-Agent": "poly-weather/0.1 (research; read-only)",
                "Accept": "application/json, application/geo+json",
            },
            follow_redirects=True,
            http2=False,
        )

    def _require_http_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            raise RuntimeError("weather HTTP client is not initialized")
        return self.http_client

    def _require_wrh_client(self) -> WrhTimeseriesClient:
        if self.wrh_client is None:
            raise RuntimeError("WRH client is not initialized")
        return self.wrh_client

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
        workers: list[asyncio.Task[Any]] = []
        writer: asyncio.Task[Any] | None = None
        status_heartbeat: asyncio.Task[Any] | None = None
        pool_heartbeat: asyncio.Task[Any] | None = None
        timer: asyncio.Task[Any] | None = None
        stream_run_started = False
        run_error: BaseException | None = None
        shutdown_error: BaseException | None = None
        try:
            self.sink.warehouse.start_weather_stream_run(
                run_id=self.run_id,
                started_at=datetime.now(UTC),
                station_id=",".join(station.station_id for station in self.stations),
            )
            stream_run_started = True
            self.http_client = self._new_http_client()
            self.wrh_client = WrhTimeseriesClient(self.http_client)
            for station in self.stations:
                intervals = self.intervals_for_station(station)
                workers.append(
                    asyncio.create_task(
                        self._scheduled(
                            name="wrh_timeseries_observation",
                            station_id=station.station_id,
                            interval=intervals["wrh_timeseries_observation"],
                            fetch=lambda station=station: self._fetch_wrh(
                                self._require_wrh_client(), station
                            ),
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
                                fetch=lambda station=station: self._fetch_nws(
                                    self._require_http_client(), station
                                ),
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
                                fetch=lambda station=station: self._fetch_metar(
                                    self._require_http_client(), station
                                ),
                            )
                        ),
                        asyncio.create_task(
                            self._scheduled(
                                name="taf",
                                station_id=station.station_id,
                                interval=intervals["taf"],
                                fetch=lambda station=station: self._fetch_taf(
                                    self._require_http_client(), station
                                ),
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
                                    self._require_http_client(), station
                                ),
                            )
                        )
                    )
            writer = asyncio.create_task(self._writer())
            status_heartbeat = asyncio.create_task(self._status_heartbeat())
            pool_heartbeat = asyncio.create_task(self._http_pool_diagnostics_heartbeat())
            timer = (
                asyncio.create_task(self._stop_after(runtime_seconds))
                if runtime_seconds > 0
                else None
            )
            self.metrics.state = "running"
            self._write_status()
            await self.stop_event.wait()
        except BaseException as exc:
            run_error = exc
            self.stop_event.set()
            if not isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                self.metrics.state = "failed"
                self.metrics.last_error = (
                    self.metrics.last_error
                    or f"weather stream {type(exc).__name__}: {exc}"
                )
        finally:
            self.stop_event.set()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            if timer is not None:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            if writer is not None:
                writer_result = await asyncio.gather(writer, return_exceptions=True)
                writer_errors = [
                    result for result in writer_result if isinstance(result, BaseException)
                ]
                if writer_errors and run_error is None:
                    run_error = RuntimeError("weather stream writer failed")
                    self.metrics.state = "failed"
                    self.metrics.last_error = str(
                        self.metrics.last_error or writer_errors[0]
                    )
                    run_error.__cause__ = writer_errors[0]
            for heartbeat in (status_heartbeat, pool_heartbeat):
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
            if run_error is None and self.metrics.state == "running":
                self.metrics.state = "stopped"
            elif run_error is not None and self.metrics.state == "running":
                self.metrics.state = "failed"
            try:
                self._write_status()
            except BaseException as exc:
                shutdown_error = exc
            if stream_run_started:
                try:
                    self.sink.warehouse.finish_weather_stream_run(
                        run_id=self.run_id,
                        finished_at=datetime.now(UTC),
                        metrics=asdict(self.metrics),
                    )
                except BaseException as exc:
                    # A fatal DuckDB error can invalidate the connection, so
                    # final bookkeeping is best effort and must not hide the
                    # original classified writer failure.
                    shutdown_error = shutdown_error or exc
            try:
                self.sink.close()
            except BaseException as exc:
                shutdown_error = shutdown_error or exc
            client = self.http_client
            self.http_client = None
            self.wrh_client = None
            if client is not None:
                try:
                    await client.aclose()
                except BaseException as exc:
                    shutdown_error = shutdown_error or exc
        if run_error is not None:
            raise run_error
        if shutdown_error is not None:
            self.metrics.state = "failed"
            self.metrics.last_error = (
                f"weather shutdown {type(shutdown_error).__name__}: {shutdown_error}"
            )
            try:
                self._write_status()
            except BaseException:
                pass
            raise RuntimeError("weather stream shutdown failed") from shutdown_error
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
                async with self.http_request_slots:
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
                    event_hash = payload_hash(
                        {
                            "station_id": station_id,
                            "product": event.product,
                            "source_timestamp_ms": source_timestamp,
                            "temperature_c": event.temperature_c,
                            "raw": event.raw,
                        }
                    )
                    prior_source_timestamp = self.last_source_timestamp_by_product.get(
                        event_key, -1
                    )
                    prior_hash = self.last_payload_hash_by_product.get(event_key)
                    is_new = (
                        prior_hash is None
                        or event_hash != prior_hash
                        or (
                            source_timestamp is not None
                            and source_timestamp > prior_source_timestamp
                        )
                    )
                    if is_new:
                        if source_timestamp is not None:
                            self.last_source_timestamp_by_product[event_key] = max(
                                source_timestamp, prior_source_timestamp
                            )
                        self.last_payload_hash_by_product[event_key] = event_hash
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
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
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
        try:
            if response.status_code == 204:
                return None
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
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
        try:
            if response.status_code == 204:
                return None
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
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
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
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
            code = getattr(exc, "code", None)
            prefix = f"weather writer {code}" if code else f"weather writer {type(exc).__name__}"
            self.metrics.last_error = f"{prefix}: {exc}"
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

    async def _http_pool_diagnostics_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            pool = _http_pool_diagnostics(self.http_client)
            process_tcp = await asyncio.to_thread(_process_tcp_connections, os.getpid())
            pool_total = pool.get("connections_total")
            tcp_established = process_tcp.get("established")
            gap = (
                int(pool_total) - int(tcp_established)
                if isinstance(pool_total, int) and isinstance(tcp_established, int)
                else None
            )
            sampled_at = datetime.now(UTC).isoformat()
            self.http_pool_health = {
                "sampled_at": sampled_at,
                "pool": pool,
                "process_tcp": process_tcp,
                "connection_accounting_gap": gap,
                "gap_consecutive_samples": self.http_pool_gap_consecutive_samples,
                "rebuild_count": self.http_pool_rebuild_count,
                "last_rebuild_at": self.last_http_pool_rebuild_at,
                "last_rebuild_reason": self.last_http_pool_rebuild_reason,
            }
            if isinstance(gap, int) and gap >= HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD:
                self.http_pool_gap_consecutive_samples += 1
            else:
                self.http_pool_gap_consecutive_samples = 0
            self.http_pool_health["gap_consecutive_samples"] = (
                self.http_pool_gap_consecutive_samples
            )
            self.pool_samples_path.parent.mkdir(parents=True, exist_ok=True)
            sample = {
                "run_id": self.run_id,
                **self.http_pool_health,
                "requests": self.metrics.requests,
                "errors": self.metrics.errors,
                "last_error": self.metrics.last_error,
            }
            await asyncio.to_thread(self._append_pool_sample, sample)
            self._write_status()
            if (
                self.http_pool_gap_consecutive_samples
                >= HTTP_POOL_REBUILD_CONSECUTIVE_SAMPLES
            ):
                await self._rebuild_http_client(
                    reason=(
                        "httpcore/process TCP accounting gap remained at "
                        f"{gap} for {self.http_pool_gap_consecutive_samples} samples"
                    )
                )
            await self._wait(HTTP_POOL_DIAGNOSTIC_INTERVAL_SECONDS)

    async def _rebuild_http_client(self, *, reason: str) -> None:
        """Replace a poisoned httpcore proxy pool after active requests drain."""
        acquired = 0
        try:
            for _ in range(HTTP_REQUEST_CONCURRENCY):
                await self.http_request_slots.acquire()
                acquired += 1
            old_client = self.http_client
            new_client = self._new_http_client()
            self.http_client = new_client
            self.wrh_client = WrhTimeseriesClient(new_client)
            if old_client is not None:
                await old_client.aclose()
            self.http_pool_rebuild_count += 1
            self.http_pool_gap_consecutive_samples = 0
            self.last_http_pool_rebuild_at = datetime.now(UTC).isoformat()
            self.last_http_pool_rebuild_reason = reason
            self.http_pool_health.update(
                {
                    "rebuild_count": self.http_pool_rebuild_count,
                    "last_rebuild_at": self.last_http_pool_rebuild_at,
                    "last_rebuild_reason": reason,
                    "gap_consecutive_samples": 0,
                }
            )
            self._write_status()
        finally:
            for _ in range(acquired):
                self.http_request_slots.release()

    def _append_pool_sample(self, sample: dict[str, Any]) -> None:
        with self.pool_samples_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

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
            accounting_gap = self.http_pool_health.get("connection_accounting_gap")
            if (
                effective_state == "running"
                and isinstance(accounting_gap, int)
                and accounting_gap >= HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD
            ):
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
            "http_pool_health": self.http_pool_health,
            "http_pool_accounting_gap_degraded_threshold": (
                HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD
            ),
            "queue_depth": self.queue.qsize(),
            "updated_at": datetime.now(UTC).isoformat(),
            "heartbeat": datetime.now(UTC).isoformat(),
            "writer": self.sink.status(),
            "read_only": True,
        }
        atomic_json_write(self.status_path, payload)
