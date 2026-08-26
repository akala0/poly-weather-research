"""Intraday temperature reversal analysis for local-time ASOS observations."""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from poly_weather.modeling import two_degree_bucket_lower
from poly_weather.temperature import round_whole_degree

CERTAINTY_SCAN_TIMES = tuple(
    time(hour, minute)
    for hour in range(10, 19)
    for minute in (0, 30)
    if not (hour == 18 and minute == 30)
)


@dataclass(frozen=True)
class TemperatureObservation:
    station_id: str
    valid: datetime
    temperature_f: float


@dataclass(frozen=True)
class DailyReversal:
    station_id: str
    target_date: date
    decision_observed_at: datetime
    decision_temperature_f: float
    final_high_f: float
    final_high_first_at: datetime
    post_decision_high_f: float

    @property
    def remaining_warming_f(self) -> float:
        """Requested metric: full-day high minus the decision observation."""
        return self.final_high_f - self.decision_temperature_f

    @property
    def post_decision_warming_f(self) -> float:
        """Causal check: high after the selected observation minus that observation."""
        return self.post_decision_high_f - self.decision_temperature_f

    @property
    def decision_offset_minutes(self) -> float:
        decision_clock = self.decision_observed_at.replace(
            hour=16,
            minute=30,
            second=0,
            microsecond=0,
        )
        return (self.decision_observed_at - decision_clock).total_seconds() / 60.0


@dataclass(frozen=True)
class CertaintyPoint:
    station_id: str
    target_date: date
    scan_at: datetime
    last_observation_at: datetime
    observed_high_f: float
    final_high_f: float
    unit: str = "fahrenheit"
    bucket_width_degrees: int = 2

    @property
    def bucket_hit(self) -> bool:
        return _bucket_key(
            self.observed_high_f, unit=self.unit, width=self.bucket_width_degrees
        ) == _bucket_key(
            self.final_high_f, unit=self.unit, width=self.bucket_width_degrees
        )

    @property
    def remaining_warming_f(self) -> float:
        return self.final_high_f - self.observed_high_f

    @property
    def locked(self) -> bool:
        return self.remaining_warming_f == 0.0

    @property
    def observation_lag_minutes(self) -> float:
        return (self.scan_at - self.last_observation_at).total_seconds() / 60.0


def load_iem_asos_csv(path: Path, *, station_id: str) -> list[TemperatureObservation]:
    """Read IEM onlycomma output, ignoring missing and non-finite temperatures."""
    observations: list[TemperatureObservation] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["station", "valid", "tmpf"]:
            raise ValueError(f"unexpected IEM CSV fields in {path}: {reader.fieldnames}")
        for row in reader:
            raw_temperature = (row.get("tmpf") or "").strip()
            if not raw_temperature or raw_temperature.lower() == "null":
                continue
            temperature_f = float(raw_temperature)
            if not math.isfinite(temperature_f):
                continue
            observations.append(
                TemperatureObservation(
                    station_id=station_id.upper(),
                    valid=datetime.strptime(row["valid"].strip(), "%Y-%m-%d %H:%M"),
                    temperature_f=temperature_f,
                )
            )
    return sorted(observations, key=lambda item: item.valid)


def temperature_bucket_key(value_f: float, *, unit: str, width: int) -> int:
    """Map a Fahrenheit observation to the configured settlement bucket."""
    if unit == "fahrenheit" and width == 2:
        return two_degree_bucket_lower(value_f)
    value = Decimal(str(value_f))
    if unit == "celsius":
        value = (value - Decimal(32)) * Decimal(5) / Decimal(9)
    elif unit != "fahrenheit":
        raise ValueError(f"unsupported temperature unit: {unit}")
    rounded = int(round_whole_degree(value))
    return (rounded // width) * width


# Backward-compatible private alias for callers/tests written before the
# multi-source historical comparison exposed this as a shared primitive.
_bucket_key = temperature_bucket_key


def daily_reversals(
    observations: Iterable[TemperatureObservation],
) -> list[DailyReversal]:
    """Build daily samples using the observation nearest 16:30 within 16:00-17:00."""
    by_date: dict[date, list[TemperatureObservation]] = defaultdict(list)
    for observation in observations:
        by_date[observation.valid.date()].append(observation)

    decision_clock = time(16, 30)
    window_start = time(16, 0)
    window_end = time(17, 0)
    results: list[DailyReversal] = []
    for target_date, records in sorted(by_date.items()):
        ordered = sorted(records, key=lambda item: item.valid)
        candidates = [
            item for item in ordered if window_start <= item.valid.time() <= window_end
        ]
        if not candidates:
            continue
        decision_at = datetime.combine(target_date, decision_clock)
        decision = min(
            candidates,
            key=lambda item: (abs((item.valid - decision_at).total_seconds()), item.valid),
        )
        final_high_f = max(item.temperature_f for item in ordered)
        first_high = next(item for item in ordered if item.temperature_f == final_high_f)
        post_decision = [item for item in ordered if item.valid >= decision.valid]
        if not post_decision:
            continue
        results.append(
            DailyReversal(
                station_id=decision.station_id,
                target_date=target_date,
                decision_observed_at=decision.valid,
                decision_temperature_f=decision.temperature_f,
                final_high_f=final_high_f,
                final_high_first_at=first_high.valid,
                post_decision_high_f=max(item.temperature_f for item in post_decision),
            )
        )
    return results


def percentile(values: Sequence[float], probability: float) -> float:
    """Return a linearly interpolated sample percentile (R-7 / NumPy default)."""
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def reversal_summary(samples: Sequence[DailyReversal]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("reversal summary requires at least one daily sample")
    remaining = [sample.remaining_warming_f for sample in samples]
    post_decision = [sample.post_decision_warming_f for sample in samples]
    return {
        "n": len(samples),
        "p50": percentile(remaining, 0.50),
        "p75": percentile(remaining, 0.75),
        "p90": percentile(remaining, 0.90),
        "p95": percentile(remaining, 0.95),
        "reversal_frequency": statistics.fmean(value > 1.0 for value in remaining),
        "post_decision_reversal_frequency": statistics.fmean(
            value > 1.0 for value in post_decision
        ),
        "decision_offset_p50_minutes": percentile(
            [sample.decision_offset_minutes for sample in samples], 0.50
        ),
    }


def high_time_summary(samples: Sequence[DailyReversal]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("high-time summary requires at least one daily sample")
    minutes = [
        sample.final_high_first_at.hour * 60 + sample.final_high_first_at.minute
        for sample in samples
    ]
    return {
        "n": len(samples),
        "p25_minutes": percentile(minutes, 0.25),
        "p50_minutes": percentile(minutes, 0.50),
        "p75_minutes": percentile(minutes, 0.75),
    }


def certainty_curve_points(
    observations: Iterable[TemperatureObservation],
    *,
    scan_times: Sequence[time] = CERTAINTY_SCAN_TIMES,
    unit: str = "fahrenheit",
    bucket_width_degrees: int = 2,
) -> dict[time, list[CertaintyPoint]]:
    """Replay observed daily highs at fixed local times without using future rows."""
    if any(
        right <= left for left, right in zip(scan_times, scan_times[1:], strict=False)
    ):
        raise ValueError("scan times must be strictly increasing")
    by_date: dict[date, list[TemperatureObservation]] = defaultdict(list)
    for observation in observations:
        by_date[observation.valid.date()].append(observation)

    points = {scan_time: [] for scan_time in scan_times}
    for target_date, records in sorted(by_date.items()):
        ordered = sorted(records, key=lambda item: item.valid)
        final_high_f = max(item.temperature_f for item in ordered)
        for scan_time in scan_times:
            scan_at = datetime.combine(target_date, scan_time)
            available = [item for item in ordered if item.valid <= scan_at]
            if not available:
                continue
            last_observation = available[-1]
            points[scan_time].append(
                CertaintyPoint(
                    station_id=last_observation.station_id,
                    target_date=target_date,
                    scan_at=scan_at,
                    last_observation_at=last_observation.valid,
                    observed_high_f=max(item.temperature_f for item in available),
                    final_high_f=final_high_f,
                    unit=unit,
                    bucket_width_degrees=bucket_width_degrees,
                )
            )
    return points


def certainty_summary(samples: Sequence[CertaintyPoint]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("certainty summary requires at least one sample")
    remaining = [sample.remaining_warming_f for sample in samples]
    lags = [sample.observation_lag_minutes for sample in samples]
    return {
        "n": len(samples),
        "hit_rate": statistics.fmean(sample.bucket_hit for sample in samples),
        "mean_remaining_warming_f": statistics.fmean(remaining),
        "p50_remaining_warming_f": percentile(remaining, 0.50),
        "p90_remaining_warming_f": percentile(remaining, 0.90),
        "locked_frequency": statistics.fmean(sample.locked for sample in samples),
        "observation_lag_p50_minutes": percentile(lags, 0.50),
        "observation_lag_p90_minutes": percentile(lags, 0.90),
    }
