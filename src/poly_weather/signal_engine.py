from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from poly_weather.calibration import (
    fit_bias_calibration,
    learn_model_weights,
    rolling_origin_evaluate,
)
from poly_weather.domain import CalibrationSample, Market
from poly_weather.execution_cost import estimate_execution_cost
from poly_weather.fees import LiquidityRole, fee_per_share
from poly_weather.modeling import blend_multi_model_forecasts, build_bucket_forecast
from poly_weather.no_forward import NoForwardTracker
from poly_weather.research_store import ResearchWarehouse
from poly_weather.temperature import celsius_to_fahrenheit, round_whole_degree
from poly_weather.weather_provenance import require_realtime_for_no_lookahead

IMPLAUSIBLE_EDGE_THRESHOLD = Decimal("0.15")
EXECUTION_NOTIONALS_USD = (Decimal("50"), Decimal("200"), Decimal("1000"))
TYPICAL_SUMMER_PEAK_MINUTES = {
    "KLAX": 12 * 60 + 53,
    "KMIA": 13 * 60 + 34,
    "KLGA": 14 * 60 + 51,
    "KORD": 14 * 60 + 51,
    "KHOU": 14 * 60 + 53,
    "ZUCK": 15 * 60,
    "ZUUU": 15 * 60,
    "KATL": 15 * 60 + 52,
    "KDAL": 15 * 60 + 53,
    "KSEA": 15 * 60 + 53,
}


def physical_bucket_state(
    observed_high_f: Decimal | None,
    *,
    upper: int | None,
    unit: str,
) -> tuple[float | None, str | None, bool | None]:
    """Return settlement-rounded margin, tier, and irreversible elimination."""
    if observed_high_f is None or upper is None:
        return None, None, None
    if unit == "celsius":
        observed_c = (observed_high_f - Decimal(32)) * Decimal(5) / Decimal(9)
        margin_f = (round_whole_degree(observed_c) - Decimal(upper)) * Decimal(9) / Decimal(5)
    else:
        margin_f = round_whole_degree(observed_high_f) - Decimal(upper)
    numeric = float(margin_f)
    tier = (
        "< -3F"
        if numeric < -3
        else "-3..-2F"
        if numeric < -2
        else "-2..-1F"
        if numeric < -1
        else "-1..0F"
        if numeric <= 0
        else "> 0F"
    )
    return numeric, tier, numeric > 0


def warming_rate_f_per_hour(
    observations: list[tuple[datetime, Decimal]], *, lookback_hours: float = 2.0
) -> float | None:
    if len(observations) < 2:
        return None
    ordered = sorted(observations, key=lambda row: row[0])
    latest_time, latest_temperature = ordered[-1]
    cutoff = latest_time.timestamp() - lookback_hours * 3600
    eligible = [row for row in ordered[:-1] if row[0].timestamp() >= cutoff]
    if not eligible:
        return None
    first_time, first_temperature = eligible[0]
    elapsed_hours = (latest_time - first_time).total_seconds() / 3600
    if elapsed_hours < lookback_hours * 0.75:
        return None
    return float((latest_temperature - first_temperature) / Decimal(str(elapsed_hours)))


@dataclass(frozen=True, slots=True)
class LiveCalibration:
    station_id: str
    lead_days: int
    sample_count: int
    sample_date_min: date
    sample_date_max: date
    bias_f: float
    residual_std_f: float
    raw_error_rms_f: float
    validation_test_samples: int
    mae_raw: float | None
    mae_calibrated: float | None
    rmse_raw: float | None
    rmse_calibrated: float | None
    brier_raw: float | None
    brier_calibrated: float | None
    log_loss_raw: float | None
    log_loss_calibrated: float | None
    model_weights: dict[str, float] | None
    ready: bool
    apply_bias: bool
    strategy: str
    reason: str


@dataclass(frozen=True, slots=True)
class LiveSignalConfig:
    event_id: str
    event_slug: str
    station_id: str
    timezone: str
    target_date: date
    markets: tuple[Market, ...]
    contract_verified: bool
    contract_reason: str
    calibration: LiveCalibration | None = None


@dataclass(slots=True)
class SignalMetrics:
    run_id: str
    started_at: str
    state: str = "starting"
    market_rows_read: int = 0
    weather_rows_read: int = 0
    evaluations: int = 0
    snapshots_written: int = 0
    parse_errors: int = 0
    config_updates: int = 0
    config_generation: int | None = None
    config_update_error: str | None = None
    last_evaluation_at: str | None = None
    last_error: str | None = None


class JsonlTail:
    """Incrementally reads a daily append-only JSONL stream."""

    def __init__(self, path_factory: Any) -> None:
        self.path_factory = path_factory
        self.path: Path | None = None
        self.offset = 0
        self.remainder = b""

    def poll(self) -> list[dict[str, Any]]:
        path = self.path_factory()
        if path != self.path:
            self.path = path
            self.offset = 0
            self.remainder = b""
        if not path.exists():
            return []
        size = path.stat().st_size
        if size < self.offset:
            self.offset = 0
            self.remainder = b""
        with path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
            self.offset = handle.tell()
        if not chunk:
            return []
        parts = (self.remainder + chunk).split(b"\n")
        self.remainder = parts.pop()
        rows: list[dict[str, Any]] = []
        for part in parts:
            if not part:
                continue
            try:
                value = json.loads(part)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def seek_to_end(self) -> None:
        """Start with only newly appended rows; callers must preload state separately."""
        path = self.path_factory()
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0
        self.remainder = b""


def deterministic_daily_high_f(
    payload: dict[str, Any], target_date: date
) -> Decimal | None:
    hourly = payload.get("blended") or payload.get("hourly")
    if not isinstance(hourly, dict):
        return None
    times = hourly.get("time")
    values = hourly.get("temperature_2m")
    if not isinstance(times, list) or not isinstance(values, list):
        return None
    prefix = target_date.isoformat()
    available = [
        Decimal(str(values[index]))
        for index, timestamp in enumerate(times)
        if str(timestamp).startswith(prefix)
        and index < len(values)
        and values[index] is not None
    ]
    return max(available) if available else None


def build_live_calibration(
    samples: list[CalibrationSample],
    *,
    station_id: str,
    lead_days: int,
    min_samples: int = 30,
) -> LiveCalibration | None:
    if lead_days < 1:
        raise ValueError("lead_days=0 contains look-ahead and cannot be used for calibration")
    ordered = sorted(samples, key=lambda sample: sample.target_date)
    if len(ordered) < 2:
        return None
    model_weights = None
    calibration_samples = ordered
    if all(sample.forecast_high_f_by_model for sample in ordered):
        model_weights = learn_model_weights(ordered)
        calibration_samples = [
            sample.model_copy(
                update={
                    "forecast_high_f": blend_multi_model_forecasts(
                        sample.forecast_high_f_by_model or {},
                        model_weights,
                    )
                }
            )
            for sample in ordered
        ]
    fitted = fit_bias_calibration(calibration_samples)
    if len(ordered) <= min_samples:
        return LiveCalibration(
            station_id=station_id,
            lead_days=lead_days,
            sample_count=len(ordered),
            sample_date_min=ordered[0].target_date,
            sample_date_max=ordered[-1].target_date,
            bias_f=fitted.bias_f,
            residual_std_f=fitted.residual_std_f,
            raw_error_rms_f=fitted.raw_error_rms_f,
            validation_test_samples=0,
            mae_raw=None,
            mae_calibrated=None,
            rmse_raw=None,
            rmse_calibrated=None,
            brier_raw=None,
            brier_calibrated=None,
            log_loss_raw=None,
            log_loss_calibrated=None,
            model_weights=model_weights,
            ready=False,
            apply_bias=False,
            strategy="insufficient_history",
            reason=f"requires more than {min_samples} samples for walk-forward validation",
        )
    evaluation = rolling_origin_evaluate(
        ordered,
        min_train_size=min_samples,
        test_size=10,
    )
    raw_validated = (
        evaluation.test_sample_count >= min_samples
        and evaluation.no_lookahead
        and evaluation.rmse_raw <= 4.0
    )
    apply_bias = (
        raw_validated
        and evaluation.brier_calibrated <= evaluation.brier_raw
        and evaluation.log_loss_calibrated <= evaluation.log_loss_raw
        and evaluation.rmse_calibrated <= evaluation.rmse_raw * 1.10
    )
    strategy = "bias_corrected" if apply_bias else "raw_validated_no_bias"
    reason = (
        "bias correction passed walk-forward probability and RMSE gates"
        if apply_bias
        else "raw model validated; bias correction rejected by walk-forward quality gates"
    )
    return LiveCalibration(
        station_id=station_id,
        lead_days=lead_days,
        sample_count=len(ordered),
        sample_date_min=ordered[0].target_date,
        sample_date_max=ordered[-1].target_date,
        bias_f=fitted.bias_f,
        residual_std_f=fitted.residual_std_f,
        raw_error_rms_f=fitted.raw_error_rms_f,
        validation_test_samples=evaluation.test_sample_count,
        mae_raw=evaluation.mae_raw,
        mae_calibrated=evaluation.mae_calibrated,
        rmse_raw=evaluation.rmse_raw,
        rmse_calibrated=evaluation.rmse_calibrated,
        brier_raw=evaluation.brier_raw,
        brier_calibrated=evaluation.brier_calibrated,
        log_loss_raw=evaluation.log_loss_raw,
        log_loss_calibrated=evaluation.log_loss_calibrated,
        model_weights=model_weights,
        ready=raw_validated,
        apply_bias=apply_bias,
        strategy=strategy,
        reason=reason,
    )


def _calibration_payload(calibration: LiveCalibration | None) -> dict[str, Any] | None:
    if calibration is None:
        return None
    payload = asdict(calibration)
    payload["sample_date_min"] = calibration.sample_date_min.isoformat()
    payload["sample_date_max"] = calibration.sample_date_max.isoformat()
    return payload


def _fahrenheit(value_c: float | None) -> Decimal | None:
    if value_c is None:
        return None
    return celsius_to_fahrenheit(value_c)


def _age_minutes(timestamp_ms: int | None, now: datetime) -> float | None:
    if timestamp_ms is None:
        return None
    source = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return (now - source).total_seconds() / 60


class SignalSink:
    def __init__(self, *, data_dir: Path, run_id: str) -> None:
        self.data_dir = data_dir
        self.run_id = run_id
        self.warehouse = ResearchWarehouse(data_dir / "signal_stream.duckdb")
        self.handles: dict[str, Any] = {}
        self.last_database_maintenance = time.monotonic()

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        self.warehouse.close()

    def write(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        for row in rows:
            day = row["generated_at"].date().isoformat()
            handle = self.handles.get(day)
            if handle is None:
                path = self.data_dir / "raw" / "signal_snapshot" / day / "events.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
                self.handles[day] = handle
            envelope = {
                "run_id": row["run_id"],
                "sequence": row["sequence"],
                "generated_at": row["generated_at"].isoformat(),
                "event_slug": row["event_slug"],
                "station_id": row["station_id"],
                "status": row["status"],
                "payload": row["payload"],
            }
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        for handle in self.handles.values():
            handle.flush()
        self.warehouse.append_signal_snapshots(rows)
        if time.monotonic() - self.last_database_maintenance >= 86_400:
            self.warehouse.checkpoint_signal_database()
            self.last_database_maintenance = time.monotonic()


class LiveSignalEngine:
    """Deterministic, read-only market/weather state and signal engine."""

    def __init__(
        self,
        *,
        configs: tuple[LiveSignalConfig, ...],
        data_dir: Path,
        interval_seconds: float = 0.25,
        min_net_edge: Decimal = Decimal("0.03"),
        cost_buffer: Decimal = Decimal("0.01"),
        config_update_path: Path | None = None,
        config_loader: Callable[[list[dict[str, Any]]], tuple[LiveSignalConfig, ...]]
        | None = None,
    ) -> None:
        if not configs:
            raise ValueError("at least one signal config is required")
        self.configs = configs
        self.data_dir = data_dir
        self.interval_seconds = interval_seconds
        self.min_net_edge = min_net_edge
        self.cost_buffer = cost_buffer
        self.config_update_path = config_update_path
        self.config_loader = config_loader
        self._last_config_generation: int | None = None
        self.run_id = str(uuid4())
        self.sequence = 0
        self.stop_event = asyncio.Event()
        self.metrics = SignalMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.books: dict[str, dict[str, Any]] = {}
        self.awaiting_authoritative_books: set[str] = set()
        self.weather: dict[tuple[str, str], dict[str, Any]] = {}
        self.observed_highs: dict[tuple[str, date], Decimal] = {}
        self.observation_history: dict[
            tuple[str, date], dict[int, Decimal]
        ] = defaultdict(dict)
        self.asset_ids = {
            token_id
            for config in configs
            for market in config.markets
            for token_id in market.clob_token_ids
        }
        self.last_fingerprint: str | None = None
        self.last_evaluation_monotonic = 0.0
        self.current_signals: list[dict[str, Any]] = []
        self.status_path = data_dir / "runtime" / "signal_engine_status.json"
        self.state_path = data_dir / "runtime" / "signal_state.json"
        self.sink = SignalSink(data_dir=data_dir, run_id=self.run_id)
        self.no_forward = NoForwardTracker(data_dir)
        self.market_tail = JsonlTail(
            lambda: self.data_dir
            / "raw"
            / "polymarket_clob_websocket"
            / datetime.now(UTC).date().isoformat()
            / "events.jsonl"
        )
        self.market_tail.seek_to_end()
        self.weather_tail = JsonlTail(
            lambda: self.data_dir
            / "raw"
            / "weather_daemon"
            / datetime.now(UTC).date().isoformat()
            / "events.jsonl"
        )
        self._bootstrap_market_books()

    async def run(self, *, runtime_seconds: float = 0) -> SignalMetrics:
        self.sink.warehouse.start_signal_stream_run(
            run_id=self.run_id,
            started_at=datetime.now(UTC),
            event_slugs=[config.event_slug for config in self.configs],
        )
        deadline = time.monotonic() + runtime_seconds if runtime_seconds > 0 else None
        self.metrics.state = "running"
        try:
            while not self.stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self._reload_config_if_needed()
                market_rows = self.market_tail.poll()
                weather_rows = self.weather_tail.poll()
                self.metrics.market_rows_read += len(market_rows)
                self.metrics.weather_rows_read += len(weather_rows)
                for row in market_rows:
                    self._ingest_market(row)
                for row in weather_rows:
                    self._ingest_weather(row)
                evaluation_due = time.monotonic() - self.last_evaluation_monotonic >= 1.0
                if market_rows or weather_rows or not self.current_signals or evaluation_due:
                    try:
                        self._evaluate()
                        self.last_evaluation_monotonic = time.monotonic()
                        self.metrics.last_error = None
                    except (ValueError, KeyError, TypeError) as exc:
                        self.metrics.last_error = f"{type(exc).__name__}: {exc}"
                self._write_status()
                remaining = None if deadline is None else deadline - time.monotonic()
                delay = self.interval_seconds if remaining is None else min(self.interval_seconds, remaining)
                if delay <= 0:
                    break
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self.metrics.state = "stopped"
            self._write_status()
            self.sink.warehouse.finish_signal_stream_run(
                run_id=self.run_id,
                finished_at=datetime.now(UTC),
                metrics=asdict(self.metrics),
            )
            self.sink.close()
        return self.metrics

    def _reload_config_if_needed(self) -> None:
        """Atomically apply a supervisor generation after full validation/loading."""
        if self.config_update_path is None or self.config_loader is None:
            return
        try:
            payload = json.loads(self.config_update_path.read_text(encoding="utf-8"))
            generation = int(payload["generation"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        if generation == self._last_config_generation:
            return
        self._last_config_generation = generation
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            self.metrics.config_update_error = "supervisor config contains no verified events"
            return
        event_rows = [item for item in events if isinstance(item, dict)]
        notified_slugs = [str(item.get("event_slug") or "") for item in event_rows]
        current_slugs = [config.event_slug for config in self.configs]
        if notified_slugs == current_slugs:
            self.metrics.config_generation = generation
            self.metrics.config_update_error = None
            return
        try:
            loaded = self.config_loader(event_rows)
        except Exception as exc:
            self.metrics.config_update_error = (
                f"supervisor config rejected: {type(exc).__name__}: {exc}"
            )
            return
        if not loaded:
            self.metrics.config_update_error = (
                "supervisor config loader returned no verified events"
            )
            return
        self.configs = loaded
        previous_asset_ids = self.asset_ids
        self.asset_ids = {
            token_id
            for config in loaded
            for market in config.markets
            for token_id in market.clob_token_ids
        }
        self._bootstrap_market_books(asset_ids=self.asset_ids - previous_asset_ids)
        self.metrics.config_updates += 1
        self.metrics.config_generation = generation
        self.metrics.config_update_error = None
        self.metrics.last_error = None
        self.last_fingerprint = None

    def _ingest_market(self, row: dict[str, Any]) -> None:
        if row.get("event_type") == "market_resolved" and isinstance(row.get("raw"), dict):
            received_at = datetime.fromtimestamp(
                int(row.get("received_at_ns") or time.time_ns()) / 1_000_000_000,
                tz=UTC,
            )
            self.no_forward.record_settlement(row["raw"], received_at)
            return
        asset_id = str(row.get("asset_id") or "")
        if asset_id not in self.asset_ids:
            return
        book = self.books.setdefault(asset_id, {})
        event_type = str(row.get("event_type") or "")
        authoritative_depth = isinstance(row.get("bids"), list) and isinstance(
            row.get("asks"), list
        )
        book.update(
            {
                "best_bid": row.get("best_bid"),
                "best_ask": row.get("best_ask"),
                "last_trade_price": row.get("last_trade_price"),
                "received_at_ns": row.get("received_at_ns"),
            }
        )
        if event_type in {"book", "price_change", "best_bid_ask"}:
            book["book_updated_at_ns"] = row.get("received_at_ns")
        if isinstance(row.get("bids"), list):
            book["bids"] = row["bids"]
        if isinstance(row.get("asks"), list):
            book["asks"] = row["asks"]
        if event_type == "price_change" and isinstance(row.get("raw"), dict):
            changes = row["raw"].get("price_changes")
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    if str(change.get("asset_id") or "") != asset_id:
                        continue
                    self._apply_book_delta(book, change)
        if authoritative_depth:
            self.awaiting_authoritative_books.discard(asset_id)
        if row.get("book_complete") is not None:
            book["book_complete"] = bool(row["book_complete"]) and all(
                isinstance(book.get(side), list) for side in ("bids", "asks")
            )
        if asset_id in self.awaiting_authoritative_books:
            book["book_complete"] = False

    def _bootstrap_market_books(self, *, asset_ids: set[str] | None = None) -> None:
        """Warm from compact checkpoints without replaying a multi-GB tick file."""
        selected = self.asset_ids if asset_ids is None else asset_ids
        path = (
            self.data_dir
            / "raw"
            / "polymarket_book_checkpoints"
            / datetime.now(UTC).date().isoformat()
            / "events.jsonl"
        )
        if not path.exists() or not selected:
            return
        latest: dict[str, dict[str, Any]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                asset_id = str(row.get("asset_id") or "") if isinstance(row, dict) else ""
                if (
                    asset_id in selected
                    and isinstance(row.get("bids"), list)
                    and isinstance(row.get("asks"), list)
                ):
                    latest[asset_id] = row
        for asset_id, row in latest.items():
            self._ingest_market(row)
            self.awaiting_authoritative_books.add(asset_id)
            self.books[asset_id]["book_complete"] = False

    @staticmethod
    def _apply_book_delta(book: dict[str, Any], change: dict[str, Any]) -> None:
        side = "bids" if str(change.get("side") or "").upper() == "BUY" else "asks"
        if not isinstance(book.get(side), list):
            return
        price = Decimal(str(change.get("price")))
        size = Decimal(str(change.get("size")))
        levels = {
            Decimal(str(level["price"])): Decimal(str(level["size"]))
            for level in book[side]
            if isinstance(level, dict) and "price" in level and "size" in level
        }
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        ordered = sorted(levels, reverse=side == "bids")
        book[side] = [
            {"price": str(level_price), "size": str(levels[level_price])}
            for level_price in ordered
        ]

    def _ingest_weather(self, row: dict[str, Any]) -> None:
        require_realtime_for_no_lookahead((row,))
        station_id = str(row.get("station_id") or "").upper()
        product = str(row.get("product") or "")
        if station_id not in {config.station_id for config in self.configs}:
            return
        key = (station_id, product)
        existing = self.weather.get(key)
        if existing is None or int(row.get("received_at_ns") or 0) >= int(
            existing.get("received_at_ns") or 0
        ):
            self.weather[key] = row
        # Physical elimination and warming signals must use the exact WRH
        # settlement series. NWS/latest and routine METAR remain cross-checks.
        if product != "wrh_timeseries_observation":
            return
        observations: list[tuple[int, Decimal]] = []
        raw = row.get("raw")
        source_payload = raw.get("source_payload") if isinstance(raw, dict) else None
        stations = source_payload.get("STATION") if isinstance(source_payload, dict) else None
        if isinstance(stations, list) and stations and isinstance(stations[0], dict):
            values = stations[0].get("OBSERVATIONS")
            if isinstance(values, dict):
                timestamps = values.get("date_time")
                temperatures = values.get("air_temp_set_1")
                if isinstance(timestamps, list) and isinstance(temperatures, list):
                    for timestamp, temperature_f in zip(
                        timestamps, temperatures, strict=False
                    ):
                        if temperature_f is None:
                            continue
                        observed_at = datetime.fromisoformat(
                            str(timestamp).replace("Z", "+00:00")
                        ).astimezone(UTC)
                        observations.append(
                            (int(observed_at.timestamp() * 1000), Decimal(str(temperature_f)))
                        )
        if not observations:
            temperature = _fahrenheit(row.get("temperature_c"))
            source_timestamp_ms = row.get("source_timestamp_ms")
            if temperature is not None and source_timestamp_ms is not None:
                observations.append((int(source_timestamp_ms), temperature))
        received_at_ms = int(row.get("received_at_ns") or 0) // 1_000_000
        for timestamp_ms, temperature in observations:
            # Explicit no-lookahead: never ingest a source observation stamped
            # after the event was received by this process.
            if received_at_ms and timestamp_ms > received_at_ms:
                continue
            source_time = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
            for config in self.configs:
                if config.station_id != station_id:
                    continue
                local_date = source_time.astimezone(ZoneInfo(config.timezone)).date()
                if local_date != config.target_date:
                    continue
                high_key = (station_id, local_date)
                self.observed_highs[high_key] = max(
                    temperature, self.observed_highs.get(high_key, temperature)
                )
                self.observation_history[high_key][timestamp_ms] = temperature

    def _evaluate(self) -> None:
        generated_at = datetime.now(UTC)
        outputs = [self._event_signal(config, generated_at) for config in self.configs]
        self.current_signals = outputs
        self.no_forward.observe(outputs, self.configs, self.books, generated_at)
        self.metrics.evaluations += len(outputs)
        self.metrics.last_evaluation_at = generated_at.isoformat()
        state_payload = {
            "run_id": self.run_id,
            "generated_at": generated_at.isoformat(),
            "read_only": True,
            "model_in_loop": False,
            "events": outputs,
        }
        self._atomic_json(self.state_path, state_payload)
        stable = [
            {
                k: v
                for k, v in output.items()
                if k not in {"generated_at", "nws_age_minutes", "metar_age_minutes"}
            }
            for output in outputs
        ]
        fingerprint = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if fingerprint == self.last_fingerprint:
            return
        rows = []
        for output in outputs:
            self.sequence += 1
            rows.append(
                {
                    "run_id": self.run_id,
                    "sequence": self.sequence,
                    "generated_at": generated_at,
                    "event_slug": output["event_slug"],
                    "station_id": output["station_id"],
                    "status": output["status"],
                    "payload": output,
                }
            )
        self.sink.write(rows)
        self.metrics.snapshots_written += len(rows)
        self.last_fingerprint = fingerprint

    def _event_signal(self, config: LiveSignalConfig, generated_at: datetime) -> dict[str, Any]:
        nws = self.weather.get((config.station_id, "latest_observation"))
        metar = self.weather.get((config.station_id, "metar"))
        wrh = self.weather.get((config.station_id, "wrh_timeseries_observation"))
        deterministic = self.weather.get(
            (config.station_id, "multi_model_deterministic_forecast")
        )
        nws_temperature = _fahrenheit(nws.get("temperature_c")) if nws else None
        metar_temperature = _fahrenheit(metar.get("temperature_c")) if metar else None
        wrh_temperature = _fahrenheit(wrh.get("temperature_c")) if wrh else None
        nws_age = _age_minutes(nws.get("source_timestamp_ms"), generated_at) if nws else None
        metar_age = _age_minutes(metar.get("source_timestamp_ms"), generated_at) if metar else None
        wrh_age = _age_minutes(wrh.get("source_timestamp_ms"), generated_at) if wrh else None
        source_delta = (
            abs(nws_temperature - metar_temperature)
            if nws_temperature is not None and metar_temperature is not None
            else None
        )
        if deterministic and isinstance(deterministic.get("blended"), dict):
            raw_deterministic = {
                "models": deterministic.get("models"),
                "blended": deterministic["blended"],
            }
        else:
            raw_deterministic = (
                deterministic.get("raw")
                if deterministic and isinstance(deterministic.get("raw"), dict)
                else {}
            )
        stream_weights = (
            raw_deterministic.get("blended", {}).get("weights")
            if isinstance(raw_deterministic.get("blended"), dict)
            else None
        )
        raw_high = deterministic_daily_high_f(raw_deterministic, config.target_date)
        observed_high = self.observed_highs.get((config.station_id, config.target_date))
        observation_rows = [
            (datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC), temperature)
            for timestamp_ms, temperature in self.observation_history.get(
                (config.station_id, config.target_date), {}
            ).items()
        ]
        warming_rate = warming_rate_f_per_hour(observation_rows)
        local_generated = generated_at.astimezone(ZoneInfo(config.timezone))
        peak_minutes = (
            TYPICAL_SUMMER_PEAK_MINUTES.get(config.station_id)
            if local_generated.month in {6, 7, 8}
            and local_generated.date() == config.target_date
            else None
        )
        hours_to_peak = (
            (peak_minutes - (local_generated.hour * 60 + local_generated.minute)) / 60
            if peak_minutes is not None
            else None
        )
        if raw_high is not None and observed_high is not None:
            raw_high = max(raw_high, observed_high)
        calibration = config.calibration
        calibration_ready = calibration is not None and calibration.ready
        selected_high = raw_high
        if calibration_ready and calibration is not None and calibration.apply_bias:
            bias = Decimal(str(calibration.bias_f))
            if selected_high is not None:
                selected_high += bias
                if observed_high is not None:
                    selected_high = max(selected_high, observed_high)

        stale_reasons: list[str] = []
        warning_reasons: list[str] = []
        wrh_max_age_minutes = 75 if config.station_id.startswith("Z") else 20
        if wrh_age is None or wrh_temperature is None:
            stale_reasons.append("WRH settlement-source observation unavailable")
        elif wrh_age < -0.1 or wrh_age > wrh_max_age_minutes:
            stale_reasons.append("WRH settlement-source observation timestamp invalid or stale")
        if nws_age is None or nws_temperature is None:
            stale_reasons.append("NWS observation unavailable")
        elif nws_age < -0.1 or nws_age > 75:
            stale_reasons.append("NWS observation timestamp invalid or stale")
        if metar_age is None or metar_temperature is None:
            warning_reasons.append("METAR cross-check unavailable")
        elif metar_age < -0.1 or metar_age > 70:
            warning_reasons.append("METAR cross-check timestamp invalid or stale")
        if source_delta is not None and source_delta > Decimal(2):
            warning_reasons.append("NWS and METAR differ by more than 2F")
        if raw_high is None:
            stale_reasons.append("multi-model deterministic daily-high forecast unavailable")
        if calibration is not None and calibration.model_weights is not None:
            if (
                not isinstance(stream_weights, dict)
                or set(stream_weights) != set(calibration.model_weights)
                or any(
                    abs(float(stream_weights[model]) - expected_weight) > 1e-9
                    for model, expected_weight in calibration.model_weights.items()
                )
            ):
                stale_reasons.append("multi-model stream weights differ from calibration")
        market_status = self._runtime_status("polymarket_ws_status.json")
        weather_status = self._runtime_status("weather_daemon_status.json")
        if market_status.get("state") != "connected":
            stale_reasons.append("market websocket is not connected")
        elif self._status_age_minutes(market_status, generated_at) > 1:
            stale_reasons.append("market websocket heartbeat is stale")
        if weather_status.get("state") != "running":
            stale_reasons.append("weather daemon is not running")
        elif self._status_age_minutes(weather_status, generated_at) > 3:
            stale_reasons.append("weather daemon heartbeat is stale")

        signals: list[dict[str, Any]] = []
        if raw_high is not None and calibration is not None:
            raw_forecast = build_bucket_forecast(
                markets=config.markets,
                deterministic_high_f=raw_high,
                residual_std_f=calibration.residual_std_f,
                target_date=config.target_date,
                forecast_model="Open-Meteo GFS+ICON+GEM deterministic blend",
                tradeable=False,
                tradeable_reason=config.contract_reason,
            )
            selected_forecast = None
            if calibration_ready and selected_high is not None:
                selected_forecast = build_bucket_forecast(
                    markets=config.markets,
                    deterministic_high_f=selected_high,
                    residual_std_f=calibration.residual_std_f,
                    target_date=config.target_date,
                    forecast_model="Open-Meteo GFS+ICON+GEM deterministic blend",
                    calibration_applied=calibration.apply_bias,
                    calibration_sample_count=calibration.sample_count,
                    calibration_bias_f=(calibration.bias_f if calibration.apply_bias else None),
                    calibration_basis=(
                        "Open-Meteo Previous Runs + same-station NOAA NCEI daily truth; "
                        f"walk-forward strategy={calibration.strategy}"
                    ),
                    tradeable=False,
                    tradeable_reason=config.contract_reason,
                )
            market_by_id = {market.market_id: market for market in config.markets}
            selected_by_market = (
                {
                    probability.bucket.market_id: probability
                    for probability in selected_forecast.probabilities
                }
                if selected_forecast is not None
                else {}
            )
            for raw_probability in raw_forecast.probabilities:
                market = market_by_id[raw_probability.bucket.market_id]
                selected_probability = selected_by_market.get(market.market_id)
                outcome_tokens = {
                    outcome.casefold(): token
                    for outcome, token in zip(
                        market.outcomes, market.clob_token_ids, strict=False
                    )
                }
                yes_book = self.books.get(outcome_tokens.get("yes", ""), {})
                no_book = self.books.get(outcome_tokens.get("no", ""), {})
                no_book_updated_ns = no_book.get("book_updated_at_ns")
                no_book_age_minutes = (
                    (
                        generated_at.timestamp()
                        - int(no_book_updated_ns) / 1_000_000_000
                    )
                    / 60
                    if no_book_updated_ns is not None
                    else None
                )
                no_book_fresh = bool(
                    no_book.get("book_complete")
                    and no_book_age_minutes is not None
                    and -0.1 <= no_book_age_minutes <= 2
                )
                yes_ask = self._decimal(yes_book.get("best_ask"))
                no_ask = self._decimal(no_book.get("best_ask"))
                yes_taker_fee = fee_per_share(yes_ask) if yes_ask is not None else None
                no_taker_fee = fee_per_share(no_ask) if no_ask is not None else None
                physical_margin_f, margin_tier, eliminated = physical_bucket_state(
                    observed_high,
                    upper=raw_probability.bucket.upper_f,
                    unit=raw_probability.bucket.unit,
                )
                raw_yes_edge = (
                    raw_probability.probability
                    - yes_ask
                    - yes_taker_fee
                    - self.cost_buffer
                    if yes_ask is not None and yes_taker_fee is not None
                    else None
                )
                raw_no_edge = (
                    Decimal(1)
                    - raw_probability.probability
                    - no_ask
                    - no_taker_fee
                    - self.cost_buffer
                    if no_ask is not None and no_taker_fee is not None
                    else None
                )
                selected_value = (
                    selected_probability.probability
                    if selected_probability is not None
                    else None
                )
                yes_edge = (
                    selected_value - yes_ask - yes_taker_fee - self.cost_buffer
                    if selected_value is not None
                    and yes_ask is not None
                    and yes_taker_fee is not None
                    else None
                )
                no_edge = (
                    Decimal(1) - selected_value - no_ask - no_taker_fee - self.cost_buffer
                    if selected_value is not None
                    and no_ask is not None
                    and no_taker_fee is not None
                    else None
                )
                raw_available = [
                    (side, edge)
                    for side, edge in (("buy_yes", raw_yes_edge), ("buy_no", raw_no_edge))
                    if edge is not None
                ]
                raw_side, raw_edge = (
                    max(raw_available, key=lambda item: item[1])
                    if raw_available
                    else (None, None)
                )
                candidates = [("buy_yes", yes_edge), ("buy_no", no_edge)]
                available = [(side, edge) for side, edge in candidates if edge is not None]
                candidate_side, candidate_edge = (
                    max(available, key=lambda item: item[1]) if available else (None, None)
                )
                execution_estimates = []
                for size_usd in EXECUTION_NOTIONALS_USD:
                    yes_fill = self._buy_fill_estimate(yes_book, size_usd)
                    no_fill = self._buy_fill_estimate(no_book, size_usd)
                    fill_candidates = []
                    if (
                        selected_value is not None
                        and yes_fill is not None
                        and yes_fill["filled_fraction"] >= 1.0
                    ):
                        fill_candidates.append(
                            (
                                "buy_yes",
                                 selected_value
                                 - Decimal(str(yes_fill["estimated_fill_price"]))
                                 - Decimal(str(yes_fill["taker_fee_per_share"]))
                                 - self.cost_buffer,
                            )
                        )
                    if (
                        selected_value is not None
                        and no_fill is not None
                        and no_fill["filled_fraction"] >= 1.0
                    ):
                        fill_candidates.append(
                            (
                                "buy_no",
                                Decimal(1)
                                 - selected_value
                                 - Decimal(str(no_fill["estimated_fill_price"]))
                                 - Decimal(str(no_fill["taker_fee_per_share"]))
                                 - self.cost_buffer,
                            )
                        )
                    executable_side, executable_edge = (
                        max(fill_candidates, key=lambda item: item[1])
                        if fill_candidates
                        else (None, None)
                    )
                    execution_estimates.append(
                        {
                            "size_usd": float(size_usd),
                            "estimated_fill_yes": (
                                yes_fill["estimated_fill_price"] if yes_fill else None
                            ),
                            "estimated_fill_no": (
                                no_fill["estimated_fill_price"] if no_fill else None
                            ),
                            "slippage_bps_yes": (
                                yes_fill["slippage_bps"] if yes_fill else None
                            ),
                            "slippage_bps_no": (
                                no_fill["slippage_bps"] if no_fill else None
                            ),
                            "slippage_per_share_yes": (
                                yes_fill["slippage_per_share"] if yes_fill else None
                            ),
                            "slippage_per_share_no": (
                                no_fill["slippage_per_share"] if no_fill else None
                            ),
                            "taker_fee_yes_usdc": (
                                yes_fill["taker_fee_usdc"] if yes_fill else None
                            ),
                            "taker_fee_no_usdc": (
                                no_fill["taker_fee_usdc"] if no_fill else None
                            ),
                            "taker_fee_per_share_yes": (
                                yes_fill["taker_fee_per_share"] if yes_fill else None
                            ),
                            "taker_fee_per_share_no": (
                                no_fill["taker_fee_per_share"] if no_fill else None
                            ),
                            "fee_assumption": "weather_taker_official_curve",
                            "filled_fraction_yes": (
                                yes_fill["filled_fraction"] if yes_fill else 0.0
                            ),
                            "filled_fraction_no": (
                                no_fill["filled_fraction"] if no_fill else 0.0
                            ),
                            "executable_candidate": executable_side,
                            "executable_net_edge_after_buffer": (
                                float(executable_edge) if executable_edge is not None else None
                            ),
                        }
                    )
                implausible_edge = bool(
                    candidate_edge is not None
                    and candidate_edge > IMPLAUSIBLE_EDGE_THRESHOLD
                )
                if (
                    implausible_edge
                    and "implausible edge suggests model error" not in warning_reasons
                ):
                    warning_reasons.append("implausible edge suggests model error")
                signals.append(
                    {
                        "market_id": market.market_id,
                        "market_slug": market.slug,
                        "bucket": raw_probability.bucket.label,
                        "bucket_lower_f": (
                            float(raw_probability.bucket.lower_f)
                            if raw_probability.bucket.lower_f is not None
                            and raw_probability.bucket.unit == "fahrenheit"
                            else (
                                float(
                                    celsius_to_fahrenheit(raw_probability.bucket.lower_f)
                                )
                                if raw_probability.bucket.lower_f is not None
                                else None
                            )
                        ),
                        "bucket_upper_f": (
                            float(raw_probability.bucket.upper_f)
                            if raw_probability.bucket.upper_f is not None
                            and raw_probability.bucket.unit == "fahrenheit"
                            else (
                                float(
                                    celsius_to_fahrenheit(raw_probability.bucket.upper_f)
                                )
                                if raw_probability.bucket.upper_f is not None
                                else None
                            )
                        ),
                        "bucket_lower_value": raw_probability.bucket.lower_f,
                        "bucket_upper_value": raw_probability.bucket.upper_f,
                        "temperature_unit": raw_probability.bucket.unit,
                        "bucket_width_degrees": raw_probability.bucket.width_degrees,
                        "raw_model_probability": float(raw_probability.probability),
                        "calibrated_model_probability": (
                            float(selected_value) if selected_value is not None else None
                        ),
                        "model_probability": (
                            float(selected_value)
                            if selected_value is not None
                            else float(raw_probability.probability)
                        ),
                        "yes_best_bid": self._float(yes_book.get("best_bid")),
                        "yes_best_ask": self._float(yes_book.get("best_ask")),
                        "no_best_bid": self._float(no_book.get("best_bid")),
                        "no_best_ask": self._float(no_book.get("best_ask")),
                        "yes_taker_fee_per_share": (
                            float(yes_taker_fee) if yes_taker_fee is not None else None
                        ),
                        "no_taker_fee_per_share": (
                            float(no_taker_fee) if no_taker_fee is not None else None
                        ),
                        "liquidity_role_assumption": LiquidityRole.TAKER.value,
                        "market_fee_category": "weather",
                        "no_book_complete": bool(no_book.get("book_complete")),
                        "no_book_age_minutes": no_book_age_minutes,
                        "physical_margin_f": physical_margin_f,
                        "margin_tier": margin_tier,
                        "eliminated": eliminated,
                        "warming_window_no_conditions_met": bool(
                            raw_probability.bucket.upper_f is not None
                            and warming_rate is not None
                            and warming_rate > 0.3
                            and hours_to_peak is not None
                            and hours_to_peak > 0
                            and physical_margin_f is not None
                            and physical_margin_f <= -2.0
                            and no_ask is not None
                            and no_ask <= Decimal("0.95")
                            and no_book_fresh
                        ),
                        "warming_window_no": False,
                        "raw_research_candidate": raw_side,
                        "raw_net_edge_after_buffer": (
                            float(raw_edge) if raw_edge is not None else None
                        ),
                        "research_candidate": candidate_side,
                        "net_edge_after_buffer": (
                            float(candidate_edge) if candidate_edge is not None else None
                        ),
                        "execution_estimates": execution_estimates,
                        "paper_alert_eligible": False,
                        "edge_gate_blocked": implausible_edge,
                        "action": "skip",
                    }
                )
        if any(
            signal["yes_best_ask"] is None or signal["no_best_ask"] is None
            for signal in signals
        ):
            warning_reasons.append("one or more books are not two-sided")
        if any(
            any(
                estimate["estimated_fill_yes"] is None
                or estimate["estimated_fill_no"] is None
                for estimate in signal["execution_estimates"]
            )
            for signal in signals
        ):
            warning_reasons.append("one or more books lack complete depth")

        if stale_reasons:
            status = "stale"
        elif not config.contract_verified or not calibration_ready:
            status = "blocked"
        elif warning_reasons:
            status = "warning"
        else:
            status = "healthy"
        for signal in signals:
            signal["warming_window_no"] = bool(
                signal["warming_window_no_conditions_met"]
                and status in {"healthy", "warning"}
                and config.contract_verified
                and calibration_ready
            )
        top = max(
            (signal for signal in signals if signal["net_edge_after_buffer"] is not None),
            key=lambda signal: signal["net_edge_after_buffer"],
            default=None,
        )
        paper_alert_eligible = bool(
            top is not None
            and not top["edge_gate_blocked"]
            and status in {"healthy", "warning"}
            and top["net_edge_after_buffer"] >= float(self.min_net_edge)
        )
        if top is not None:
            top["paper_alert_eligible"] = paper_alert_eligible
        reasons = stale_reasons + warning_reasons
        if not config.contract_verified:
            reasons.insert(0, config.contract_reason)
        if not calibration_ready:
            calibration_reason = (
                calibration.reason
                if calibration is not None
                else "calibration history is unavailable"
            )
            reasons.insert(0, f"calibration gate blocked: {calibration_reason}")
        return {
            "generated_at": generated_at.isoformat(),
            "event_id": config.event_id,
            "event_slug": config.event_slug,
            "station_id": config.station_id,
            "target_date": config.target_date.isoformat(),
            "status": status,
            "contract_verified": config.contract_verified,
            "calibration_ready": calibration_ready,
            "calibration": _calibration_payload(calibration),
            "reasons": reasons or ["all deterministic health gates passed"],
            "current_observed_high_f": float(observed_high) if observed_high is not None else None,
            "warming_rate_f_per_hour": warming_rate,
            "hours_to_typical_peak": hours_to_peak,
            "typical_peak_local": (
                f"{peak_minutes // 60:02d}:{peak_minutes % 60:02d}"
                if peak_minutes is not None
                else None
            ),
            "nws_temperature_f": float(nws_temperature) if nws_temperature is not None else None,
            "nws_age_minutes": nws_age,
            "wrh_temperature_f": float(wrh_temperature) if wrh_temperature is not None else None,
            "wrh_age_minutes": wrh_age,
            "metar_temperature_f": (
                float(metar_temperature) if metar_temperature is not None else None
            ),
            "metar_age_minutes": metar_age,
            "source_delta_f": float(source_delta) if source_delta is not None else None,
            "deterministic": {
                "raw": {
                    "daily_high_f": float(raw_high) if raw_high is not None else None,
                },
                "selected": {
                    "daily_high_f": (
                        float(selected_high)
                        if calibration_ready and selected_high is not None
                        else None
                    ),
                    "residual_std_f": (
                        calibration.residual_std_f if calibration is not None else None
                    ),
                    "bias_applied": bool(calibration and calibration.apply_bias),
                },
            },
            "cost_buffer": float(self.cost_buffer),
            "min_net_edge": float(self.min_net_edge),
            "top_research_candidate": top,
            "paper_alert_eligible": paper_alert_eligible,
            "signals": signals,
            "execution_enabled": False,
        }

    def _runtime_status(self, filename: str) -> dict[str, Any]:
        path = self.data_dir / "runtime" / filename
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _status_age_minutes(status: dict[str, Any], now: datetime) -> float:
        try:
            updated_at = datetime.fromisoformat(str(status["updated_at"]))
        except (KeyError, TypeError, ValueError):
            return float("inf")
        if updated_at.tzinfo is None:
            return float("inf")
        return max(0.0, (now - updated_at.astimezone(UTC)).total_seconds() / 60)

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    @staticmethod
    def _float(value: Any) -> float | None:
        return float(value) if value is not None else None

    @staticmethod
    def _buy_fill_estimate(
        book: dict[str, Any], size_usd: Decimal
    ) -> dict[str, float] | None:
        asks = book.get("asks")
        if not book.get("book_complete") or not isinstance(asks, list):
            return None
        levels = [
            (row.get("price"), row.get("size"))
            for row in asks
            if isinstance(row, dict)
            and row.get("price") is not None
            and row.get("size") is not None
        ]
        estimate = estimate_execution_cost(
            levels,
            size_usd,
            "buy",
            liquidity_role=LiquidityRole.TAKER,
            market_category="weather",
        )
        if estimate is None:
            return None
        top_price = min(Decimal(str(price)) for price, _size in levels)
        slippage_bps = (
            float(estimate.slippage_vs_top / top_price * Decimal(10_000))
            if top_price > 0
            else None
        )
        return {
            "estimated_fill_price": float(estimate.average_fill_price),
            "slippage_bps": slippage_bps,
            "slippage_per_share": float(estimate.slippage_vs_top),
            "filled_fraction": estimate.filled_fraction,
            "filled_shares": float(estimate.filled_shares),
            "taker_fee_usdc": float(estimate.fee_usdc),
            "taker_fee_per_share": float(estimate.fee_per_share),
        }

    def _write_status(self) -> None:
        payload = {
            **asdict(self.metrics),
            "pid": os.getpid(),
            "event_slugs": [config.event_slug for config in self.configs],
            "book_count": len(self.books),
            "weather_product_count": len(self.weather),
            "updated_at": datetime.now(UTC).isoformat(),
            "state_path": str(self.state_path.resolve()),
            "read_only": True,
            "model_in_loop": False,
            "execution_enabled": False,
        }
        self._atomic_json(self.status_path, payload)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for attempt in range(5):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
