from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from poly_weather.calibration import fit_bias_calibration, rolling_origin_evaluate
from poly_weather.domain import CalibrationSample, Market
from poly_weather.modeling import build_bucket_forecast
from poly_weather.research_store import ResearchWarehouse


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


def gefs_daily_highs_f(payload: dict[str, Any], target_date: date) -> tuple[Decimal, ...]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return ()
    times = hourly.get("time")
    if not isinstance(times, list):
        return ()
    prefix = target_date.isoformat()
    indices = [index for index, value in enumerate(times) if str(value).startswith(prefix)]
    if not indices:
        return ()
    highs: list[Decimal] = []
    for name, values in hourly.items():
        if not str(name).startswith("temperature_2m") or not isinstance(values, list):
            continue
        available = [values[index] for index in indices if index < len(values) and values[index] is not None]
        if available:
            highs.append(max(Decimal(str(value)) for value in available))
    return tuple(highs)


def build_live_calibration(
    samples: list[CalibrationSample],
    *,
    station_id: str,
    lead_days: int,
    min_samples: int = 30,
) -> LiveCalibration | None:
    ordered = sorted(samples, key=lambda sample: sample.target_date)
    if len(ordered) < 2:
        return None
    fitted = fit_bias_calibration(ordered)
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
    return Decimal(str(value_c)) * Decimal(9) / Decimal(5) + Decimal(32)


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
    ) -> None:
        if not configs:
            raise ValueError("at least one signal config is required")
        self.configs = configs
        self.data_dir = data_dir
        self.interval_seconds = interval_seconds
        self.min_net_edge = min_net_edge
        self.cost_buffer = cost_buffer
        self.run_id = str(uuid4())
        self.sequence = 0
        self.stop_event = asyncio.Event()
        self.metrics = SignalMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.books: dict[str, dict[str, Any]] = {}
        self.weather: dict[tuple[str, str], dict[str, Any]] = {}
        self.observed_highs: dict[tuple[str, date], Decimal] = {}
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
        self.market_tail = JsonlTail(
            lambda: self.data_dir
            / "raw"
            / "polymarket_clob_websocket"
            / datetime.now(UTC).date().isoformat()
            / "events.jsonl"
        )
        self.weather_tail = JsonlTail(
            lambda: self.data_dir
            / "raw"
            / "weather_daemon"
            / datetime.now(UTC).date().isoformat()
            / "events.jsonl"
        )

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

    def _ingest_market(self, row: dict[str, Any]) -> None:
        asset_id = str(row.get("asset_id") or "")
        if asset_id not in self.asset_ids:
            return
        self.books[asset_id] = {
            "best_bid": row.get("best_bid"),
            "best_ask": row.get("best_ask"),
            "last_trade_price": row.get("last_trade_price"),
            "received_at_ns": row.get("received_at_ns"),
        }

    def _ingest_weather(self, row: dict[str, Any]) -> None:
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
        temperature = _fahrenheit(row.get("temperature_c"))
        source_timestamp_ms = row.get("source_timestamp_ms")
        if temperature is None or source_timestamp_ms is None:
            return
        source_time = datetime.fromtimestamp(int(source_timestamp_ms) / 1000, tz=UTC)
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

    def _evaluate(self) -> None:
        generated_at = datetime.now(UTC)
        outputs = [self._event_signal(config, generated_at) for config in self.configs]
        self.current_signals = outputs
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
        gefs = self.weather.get((config.station_id, "gefs_ensemble_forecast"))
        nws_temperature = _fahrenheit(nws.get("temperature_c")) if nws else None
        metar_temperature = _fahrenheit(metar.get("temperature_c")) if metar else None
        nws_age = _age_minutes(nws.get("source_timestamp_ms"), generated_at) if nws else None
        metar_age = _age_minutes(metar.get("source_timestamp_ms"), generated_at) if metar else None
        source_delta = (
            abs(nws_temperature - metar_temperature)
            if nws_temperature is not None and metar_temperature is not None
            else None
        )
        raw_gefs = gefs.get("raw") if gefs and isinstance(gefs.get("raw"), dict) else {}
        raw_highs = gefs_daily_highs_f(raw_gefs, config.target_date)
        observed_high = self.observed_highs.get((config.station_id, config.target_date))
        if observed_high is not None:
            raw_highs = tuple(max(value, observed_high) for value in raw_highs)
        calibration = config.calibration
        calibration_ready = calibration is not None and calibration.ready
        selected_highs = raw_highs
        if calibration_ready and calibration is not None and calibration.apply_bias:
            bias = Decimal(str(calibration.bias_f))
            selected_highs = tuple(value + bias for value in raw_highs)
            if observed_high is not None:
                selected_highs = tuple(max(value, observed_high) for value in selected_highs)

        stale_reasons: list[str] = []
        warning_reasons: list[str] = []
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
        if not raw_highs:
            stale_reasons.append("GEFS daily-high ensemble unavailable")
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
        if raw_highs:
            raw_forecast = build_bucket_forecast(
                markets=config.markets,
                member_highs_f=raw_highs,
                target_date=config.target_date,
                ensemble_model="NOAA GEFS via Open-Meteo",
                tradeable=False,
                tradeable_reason=config.contract_reason,
            )
            selected_forecast = None
            if calibration_ready and calibration is not None:
                selected_forecast = build_bucket_forecast(
                    markets=config.markets,
                    member_highs_f=selected_highs,
                    target_date=config.target_date,
                    ensemble_model="NOAA GEFS via Open-Meteo",
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
                yes_ask = self._decimal(yes_book.get("best_ask"))
                no_ask = self._decimal(no_book.get("best_ask"))
                raw_yes_edge = (
                    raw_probability.probability - yes_ask - self.cost_buffer
                    if yes_ask is not None
                    else None
                )
                raw_no_edge = (
                    Decimal(1) - raw_probability.probability - no_ask - self.cost_buffer
                    if no_ask is not None
                    else None
                )
                selected_value = (
                    selected_probability.probability
                    if selected_probability is not None
                    else None
                )
                yes_edge = (
                    selected_value - yes_ask - self.cost_buffer
                    if selected_value is not None and yes_ask is not None
                    else None
                )
                no_edge = (
                    Decimal(1) - selected_value - no_ask - self.cost_buffer
                    if selected_value is not None and no_ask is not None
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
                signals.append(
                    {
                        "market_id": market.market_id,
                        "market_slug": market.slug,
                        "bucket": raw_probability.bucket.label,
                        "raw_model_probability": float(raw_probability.probability),
                        "calibrated_model_probability": (
                            float(selected_value) if selected_value is not None else None
                        ),
                        "model_probability": (
                            float(selected_value)
                            if selected_value is not None
                            else float(raw_probability.probability)
                        ),
                        "member_count": raw_probability.member_count,
                        "yes_best_bid": self._float(yes_book.get("best_bid")),
                        "yes_best_ask": self._float(yes_book.get("best_ask")),
                        "no_best_bid": self._float(no_book.get("best_bid")),
                        "no_best_ask": self._float(no_book.get("best_ask")),
                        "raw_research_candidate": raw_side,
                        "raw_net_edge_after_buffer": (
                            float(raw_edge) if raw_edge is not None else None
                        ),
                        "research_candidate": candidate_side,
                        "net_edge_after_buffer": (
                            float(candidate_edge) if candidate_edge is not None else None
                        ),
                        "paper_alert_eligible": False,
                        "action": "skip",
                    }
                )
        if any(
            signal["yes_best_ask"] is None or signal["no_best_ask"] is None
            for signal in signals
        ):
            warning_reasons.append("one or more books are not two-sided")

        if stale_reasons:
            status = "stale"
        elif not config.contract_verified or not calibration_ready:
            status = "blocked"
        elif warning_reasons:
            status = "warning"
        else:
            status = "healthy"
        top = max(
            (signal for signal in signals if signal["net_edge_after_buffer"] is not None),
            key=lambda signal: signal["net_edge_after_buffer"],
            default=None,
        )
        paper_alert_eligible = bool(
            top is not None
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
            "nws_temperature_f": float(nws_temperature) if nws_temperature is not None else None,
            "nws_age_minutes": nws_age,
            "metar_temperature_f": (
                float(metar_temperature) if metar_temperature is not None else None
            ),
            "metar_age_minutes": metar_age,
            "source_delta_f": float(source_delta) if source_delta is not None else None,
            "ensemble": {
                "members": len(raw_highs),
                "raw": {
                    "minimum_f": float(min(raw_highs)) if raw_highs else None,
                    "median_f": float(statistics.median(raw_highs)) if raw_highs else None,
                    "maximum_f": float(max(raw_highs)) if raw_highs else None,
                },
                "selected": {
                    "minimum_f": float(min(selected_highs)) if calibration_ready else None,
                    "median_f": (
                        float(statistics.median(selected_highs)) if calibration_ready else None
                    ),
                    "maximum_f": float(max(selected_highs)) if calibration_ready else None,
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
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
