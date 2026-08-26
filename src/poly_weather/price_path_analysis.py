"""Price-path reachability from archived NO ask/bid order books.

This module answers a different question from settlement accuracy or quote
accessibility: after a fully executable $200 NO entry, did the *same NO
token's* resting best bid (and, separately, a full $200 exit walk) reach a
specified price increase before the local market day ended?

The implementation is deliberately fail-closed about inputs.  An entry uses
only the NO ask visible at that timestamp.  A path uses only strictly later
NO book timestamps.  Public trades, prices-history points, midpoint values and
the YES complement are never used as quotes.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.archive_io import open_jsonl_text
from poly_weather.entry_accessibility import _top
from poly_weather.execution_cost import (
    ExecutionCostEstimate,
    estimate_execution_by_shares,
    estimate_execution_cost,
)
from poly_weather.no_forward import wilson_interval
from poly_weather.price_band_accessibility import price_band_for_ask
from poly_weather.real_no_books import _bucket_upper_and_unit, _levels
from poly_weather.signal_engine import physical_bucket_state
from poly_weather.temperature import celsius_to_fahrenheit, fahrenheit_to_celsius

TARGET_RISES: tuple[Decimal, ...] = (
    Decimal("0.05"),
    Decimal("0.10"),
    Decimal("0.13"),
    Decimal("0.20"),
)
ENTRY_SIZE_USD = Decimal("200")
PRIMARY_BANDS: dict[str, str] = {
    "KLAX": "0.70–0.85",
    "KLGA": "0.50–0.70",
}
CONTROL_BANDS: tuple[str, ...] = ("0.50–0.70", "0.70–0.85")


@dataclass(frozen=True, slots=True)
class ArchivedWeatherObservation:
    """One raw WRH observation and when the daemon made it available."""

    station_id: str
    observed_at: datetime
    available_at: datetime
    temperature_f: Decimal
    product: str = "wrh_timeseries_observation"


def compact_no_book_pairs(
    pairs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Drop YES/raw envelope fields before the path pass.

    Path analysis only needs the event identity, NO quote timestamp and NO
    bid/ask ladders.  Keeping the original WebSocket raw payload and YES book
    can multiply memory use on multi-day archives without changing a result.
    The bid/ask lists themselves are intentionally retained unchanged.
    """

    output: list[dict[str, Any]] = []
    for pair in pairs:
        no = pair.get("no")
        if not isinstance(no, Mapping):
            continue
        output.append(
            {
                "observed_at": pair.get("observed_at"),
                "event_slug": pair.get("event_slug"),
                "market_slug": pair.get("market_slug"),
                "no": {
                    "_timestamp": no.get("_timestamp"),
                    "asset_id": no.get("asset_id"),
                    "bids": no.get("bids"),
                    "asks": no.get("asks"),
                },
            }
        )
    return output


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decimal(value: Any) -> Decimal | None:
    try:
        if value is None:
            return None
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def load_archived_weather_observations(
    paths: Sequence[Path],
    *,
    station_ids: Sequence[str] | None = None,
) -> dict[str, list[ArchivedWeatherObservation]]:
    """Load raw WRH observations without using a historical backfill.

    ``wrh_timeseries_observation`` is the configured settlement-source product.
    The source timestamp and daemon receipt timestamp are retained separately;
    entry-time features require both to be no later than the book timestamp.
    """

    wanted = {str(value).upper() for value in station_ids} if station_ids else None
    by_station: dict[str, list[ArchivedWeatherObservation]] = defaultdict(list)
    for path in paths:
        if not path.exists():
            continue
        with open_jsonl_text(path) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("product") != "wrh_timeseries_observation":
                    continue
                station = str(row.get("station_id") or "").upper()
                if not station or (wanted is not None and station not in wanted):
                    continue
                source_ms = row.get("source_timestamp_ms")
                try:
                    observed_at = datetime.fromtimestamp(int(source_ms) / 1000, tz=UTC)
                except (TypeError, ValueError, OSError, OverflowError):
                    continue
                available_at = _aware(row.get("received_at"))
                if available_at is None:
                    continue
                raw = row.get("raw")
                raw_temperature = raw.get("temperature_f") if isinstance(raw, dict) else None
                temperature_f = _decimal(raw_temperature)
                if temperature_f is None:
                    temperature_c = _decimal(row.get("temperature_c"))
                    if temperature_c is None:
                        continue
                    temperature_f = celsius_to_fahrenheit(temperature_c)
                by_station[station].append(
                    ArchivedWeatherObservation(
                        station_id=station,
                        observed_at=observed_at,
                        available_at=available_at,
                        temperature_f=temperature_f,
                    )
                )
    for station, observations in by_station.items():
        unique: dict[tuple[datetime, Decimal], ArchivedWeatherObservation] = {}
        for observation in observations:
            key = (observation.observed_at, observation.temperature_f)
            previous = unique.get(key)
            if previous is None or observation.available_at < previous.available_at:
                unique[key] = observation
        by_station[station] = sorted(
            unique.values(), key=lambda item: (item.observed_at, item.available_at)
        )
    return dict(by_station)


def _levels_decimal(value: Any, *, reverse: bool = False) -> tuple[tuple[Decimal, Decimal], ...]:
    parsed = [
        (Decimal(price), Decimal(size))
        for price, size in _levels(value)
        if Decimal(price) > 0 and Decimal(size) > 0
    ]
    return tuple(sorted(parsed, key=lambda row: row[0], reverse=reverse))


def _quote_at(pair: Mapping[str, Any]) -> datetime | None:
    no = pair.get("no")
    if isinstance(no, Mapping):
        timestamp = _aware(no.get("_timestamp"))
        if timestamp is not None:
            return timestamp
    return _aware(pair.get("observed_at"))


def _size_key(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _number_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p10": None, "p50": None, "p90": None}
    ordered = sorted(float(value) for value in values)

    def percentile(probability: float) -> float:
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        return ordered[lower] * (1 - fraction) + ordered[upper] * fraction

    return {
        "count": len(ordered),
        "mean": fmean(ordered),
        "p10": percentile(0.10),
        "p50": percentile(0.50),
        "p90": percentile(0.90),
    }


def _rate(successes: int, sample_count: int) -> dict[str, Any]:
    interval = wilson_interval(successes, sample_count)
    return {
        "count": successes,
        "sample_count": sample_count,
        "rate": successes / sample_count if sample_count else None,
        "wilson_low": interval[0] if interval else None,
        "wilson_high": interval[1] if interval else None,
        "statistically_unreliable": sample_count < 30,
    }


def _rate_text(value: Mapping[str, Any]) -> str:
    if value.get("rate") is None:
        return "N=0"
    suffix = "；n<30，统计不可靠" if value.get("statistically_unreliable") else ""
    return (
        f"{int(value['count'])}/{int(value['sample_count'])} "
        f"({float(value['rate']):.1%}; Wilson 95% "
        f"{float(value['wilson_low']):.1%}–{float(value['wilson_high']):.1%}){suffix}"
    )


def _peak_phase(hours_to_peak: float | None) -> str:
    if hours_to_peak is None:
        return "typical_peak_unknown"
    if hours_to_peak < 0:
        return "after_typical_peak"
    if hours_to_peak <= 1:
        return "pre_peak_0-1h"
    if hours_to_peak <= 2:
        return "pre_peak_1-2h"
    if hours_to_peak <= 4:
        return "pre_peak_2-4h"
    return "pre_peak_>4h"


def _margin_band(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < -6:
        return "<−6°F"
    if value < -4:
        return "−6..−4°F"
    if value < -2:
        return "−4..−2°F"
    if value <= 0:
        return "−2..0°F"
    return ">0°F（已出局）"


def _coerce_observation(value: Any) -> ArchivedWeatherObservation | None:
    if isinstance(value, ArchivedWeatherObservation):
        return value
    if isinstance(value, Mapping):
        station = str(value.get("station_id") or "").upper()
        observed_at = _aware(value.get("observed_at") or value.get("valid"))
        available_at = _aware(value.get("available_at") or value.get("received_at"))
        temperature = _decimal(value.get("temperature_f"))
        if station and observed_at and available_at and temperature is not None:
            return ArchivedWeatherObservation(station, observed_at, available_at, temperature)
        return None
    station = str(getattr(value, "station_id", "")).upper()
    observed_at = _aware(getattr(value, "observed_at", None) or getattr(value, "valid", None))
    available_at = _aware(getattr(value, "available_at", None) or observed_at)
    temperature = _decimal(getattr(value, "temperature_f", None))
    if station and observed_at and available_at and temperature is not None:
        return ArchivedWeatherObservation(station, observed_at, available_at, temperature)
    return None


def _coerced_observations(
    observations_by_station: Mapping[str, Sequence[Any]],
) -> dict[str, tuple[ArchivedWeatherObservation, ...]]:
    output: dict[str, tuple[ArchivedWeatherObservation, ...]] = {}
    for station, rows in observations_by_station.items():
        values = [row for row in (_coerce_observation(item) for item in rows) if row is not None]
        output[str(station).upper()] = tuple(
            sorted(values, key=lambda item: (item.observed_at, item.available_at))
        )
    return output


def _observations_for_target(
    observations: Sequence[ArchivedWeatherObservation],
    *,
    target_date: date,
    timezone: ZoneInfo,
) -> list[ArchivedWeatherObservation]:
    return [
        row
        for row in observations
        if row.observed_at.astimezone(timezone).date() == target_date
    ]


def _physical_elimination(
    observations: Sequence[ArchivedWeatherObservation],
    *,
    target_date: date,
    timezone: ZoneInfo,
    upper: int | None,
    unit: str,
) -> datetime | None:
    if upper is None:
        return None
    running_high: Decimal | None = None
    for row in _observations_for_target(observations, target_date=target_date, timezone=timezone):
        temperature = (
            row.temperature_f
            if unit == "fahrenheit"
            else fahrenheit_to_celsius(row.temperature_f)
        )
        running_high = temperature if running_high is None else max(running_high, temperature)
        _margin, _tier, eliminated = physical_bucket_state(
            running_high, upper=upper, unit=unit
        )
        if eliminated:
            return row.observed_at
    return None


def _physical_state_at(
    observations: Sequence[ArchivedWeatherObservation],
    *,
    entry_at: datetime,
    target_date: date,
    timezone: ZoneInfo,
    upper: int | None,
    unit: str,
) -> tuple[float | None, str | None, bool | None]:
    if upper is None:
        return None, None, None
    available = [
        row
        for row in _observations_for_target(observations, target_date=target_date, timezone=timezone)
        if row.observed_at <= entry_at and row.available_at <= entry_at
    ]
    if not available:
        return None, None, None
    observed_high = max(
        row.temperature_f
        if unit == "fahrenheit"
        else fahrenheit_to_celsius(row.temperature_f)
        for row in available
    )
    return physical_bucket_state(observed_high, upper=upper, unit=unit)


def _day_end(target_date: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(
        target_date + timedelta(days=1), time.min, tzinfo=timezone
    ).astimezone(UTC)


def _estimate_payload(estimate: ExecutionCostEstimate | None) -> dict[str, Any]:
    if estimate is None:
        return {
            "complete": False,
            "average": None,
            "slippage": None,
            "fee_per_share": None,
            "filled_shares": None,
        }
    return {
        "complete": estimate.filled_fraction >= 1,
        "average": float(estimate.average_fill_price),
        "slippage": float(estimate.slippage_vs_top),
        "fee_per_share": float(estimate.fee_per_share),
        "filled_shares": float(estimate.filled_shares),
    }


def _cost_per_share(
    entry: ExecutionCostEstimate | None,
    exit_at_entry: ExecutionCostEstimate | None,
) -> Decimal | None:
    if (
        entry is None
        or exit_at_entry is None
        or entry.filled_fraction < 1
        or exit_at_entry.filled_fraction < 1
    ):
        return None
    return (
        entry.slippage_vs_top
        + entry.fee_per_share
        + exit_at_entry.slippage_vs_top
        + exit_at_entry.fee_per_share
    )


def _future_rows(
    timeline: Sequence[Mapping[str, Any]],
    *,
    entry_at: datetime,
    horizon_end: datetime,
) -> list[tuple[datetime, Mapping[str, Any], Decimal | None]]:
    output: list[tuple[datetime, Mapping[str, Any], Decimal | None]] = []
    for pair in timeline:
        quote_at = _quote_at(pair)
        if quote_at is None or quote_at <= entry_at or quote_at >= horizon_end:
            continue
        no = pair.get("no")
        if not isinstance(no, Mapping):
            continue
        bids = _levels_decimal(no.get("bids"), reverse=True)
        output.append((quote_at, pair, _top(bids, bids=True)))
    return output


def _path_outcomes(
    *,
    entry_at: datetime,
    entry_average: Decimal,
    entry_shares: Decimal,
    future: Sequence[tuple[datetime, Mapping[str, Any], Decimal | None]],
    rises: Sequence[Decimal],
    elimination_at: datetime | None,
    horizon_end: datetime,
    horizon_complete: bool,
) -> dict[str, dict[str, Any]]:
    thresholds = {rise: entry_average + rise for rise in rises}
    top_target_at: dict[Decimal, datetime | None] = {rise: None for rise in rises}
    depth_target_at: dict[Decimal, datetime | None] = {rise: None for rise in rises}
    minimum_threshold = min(thresholds.values())
    for quote_at, pair, top_bid in future:
        if top_bid is None:
            continue
        for rise, threshold in thresholds.items():
            if top_target_at[rise] is None and top_bid >= threshold:
                top_target_at[rise] = quote_at
        if top_bid < minimum_threshold or all(
            depth_target_at[rise] is not None for rise in rises
        ):
            continue
        no = pair.get("no")
        bids = _levels_decimal(no.get("bids"), reverse=True) if isinstance(no, Mapping) else ()
        exit_fill = estimate_execution_by_shares(bids, entry_shares, "sell") if bids else None
        if exit_fill is not None and exit_fill.filled_fraction >= 1:
            for rise, threshold in thresholds.items():
                if (
                    depth_target_at[rise] is None
                    and exit_fill.average_fill_price >= threshold
                ):
                    depth_target_at[rise] = quote_at

    collapse_rows: list[tuple[datetime, Decimal]] = []
    if elimination_at is not None and elimination_at <= horizon_end:
        for quote_at, _pair, top_bid in future:
            if quote_at <= elimination_at and top_bid is not None:
                collapse_rows.append((quote_at, top_bid))
        after = next(
            ((quote_at, top_bid) for quote_at, _pair, top_bid in future if quote_at > elimination_at and top_bid is not None),
            None,
        )
        if after is not None:
            collapse_rows.append(after)
    minimum_bid = min((bid for _at, bid in collapse_rows), default=None)
    intermediate = any(Decimal(0) < bid < entry_average for _at, bid in collapse_rows)
    stop_band = any(
        entry_average * Decimal("0.95") <= bid < entry_average
        for _at, bid in collapse_rows
    )
    outcomes: dict[str, dict[str, Any]] = {}
    for rise, threshold in thresholds.items():
        reached_at = top_target_at[rise]
        target_before_elimination = (
            reached_at is not None
            and (elimination_at is None or reached_at < elimination_at)
        )
        if target_before_elimination:
            outcome = "target_reached"
        elif elimination_at is not None and elimination_at <= horizon_end:
            outcome = "zero_gap"
        elif horizon_complete:
            outcome = "not_reached_nonzero"
        else:
            outcome = "censored"
        outcomes[f"{rise:.2f}"] = {
            "outcome": outcome,
            "target_price": float(threshold),
            "top_bid_target_at": reached_at.isoformat() if reached_at else None,
            "depth_200_target_at": (
                depth_target_at[rise].isoformat() if depth_target_at[rise] else None
            ),
            "target_top_bid_reached": target_before_elimination,
            "target_200_depth_reached": (
                depth_target_at[rise] is not None
                and (elimination_at is None or depth_target_at[rise] < elimination_at)
            ),
            "duration_minutes": (
                (reached_at - entry_at).total_seconds() / 60
                if target_before_elimination and reached_at is not None
                else None
            ),
            "minimum_bid_before_collapse": (
                float(minimum_bid) if minimum_bid is not None else None
            ),
            "collapse_bid_observation_count": len(collapse_rows),
            "has_intermediate_nonzero_bid": intermediate,
            "has_observed_bid_in_minus_5pct_band": stop_band,
            "elimination_at": elimination_at.isoformat() if elimination_at else None,
        }
    return outcomes


def _summarize_records(
    records: Sequence[Mapping[str, Any]],
    *,
    target_rises: Sequence[Decimal],
    candidate_count: int,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "candidate_count": candidate_count,
        "entry_count": len(records),
        "statistically_unreliable": len(records) < 30,
        "entry_average": _number_summary([float(row["entry_average"]) for row in records]),
        "round_trip_cost": _number_summary(
            [float(row["round_trip_cost"]) for row in records if row.get("round_trip_cost") is not None]
        ),
        "round_trip_cost_observed_count": sum(row.get("round_trip_cost") is not None for row in records),
        "physical_margin_f": _number_summary(
            [float(row["physical_margin_f"]) for row in records if row.get("physical_margin_f") is not None]
        ),
        "targets": {},
    }
    for rise in target_rises:
        key = f"{rise:.2f}"
        rows = [row["outcomes"][key] for row in records]
        definitive = [row for row in rows if row["outcome"] != "censored"]
        reached = sum(row["outcome"] == "target_reached" for row in definitive)
        zero = sum(row["outcome"] == "zero_gap" for row in definitive)
        nonzero = sum(row["outcome"] == "not_reached_nonzero" for row in definitive)
        censored = sum(row["outcome"] == "censored" for row in rows)
        zero_rows = [row for row in definitive if row["outcome"] == "zero_gap"]
        durations = [float(row["duration_minutes"]) for row in rows if row.get("duration_minutes") is not None]
        p50_cost = output["round_trip_cost"].get("p50")
        p90_cost = output["round_trip_cost"].get("p90")
        rise_float = float(rise)
        target_rate = _rate(reached, len(definitive))
        zero_rate = _rate(zero, len(definitive))
        nonzero_rate = _rate(nonzero, len(definitive))
        # This is an explicitly conservative screen, not a realized PnL
        # estimator: non-target/non-zero is assigned zero, a physical collapse
        # loses at most the full $1/share, and the cost is the p90 same-book
        # hurdle.  Censored paths are excluded from the denominator.
        net_p50 = rise_float - p50_cost if p50_cost is not None else None
        net_p90 = rise_float - p90_cost if p90_cost is not None else None
        profit = net_p90
        if (
            profit is None
            or target_rate["wilson_low"] is None
            or target_rate["wilson_high"] is None
            or zero_rate["wilson_high"] is None
        ):
            conservative = None
        else:
            success_bound = target_rate["wilson_high"] if profit < 0 else target_rate["wilson_low"]
            conservative = success_bound * profit - zero_rate["wilson_high"]
        output["targets"][key] = {
            "rise": rise_float,
            "target_rate": target_rate,
            "zero_gap_rate": zero_rate,
            "not_reached_nonzero_rate": nonzero_rate,
            "censored_rate": _rate(censored, len(rows)),
            "definitive_count": len(definitive),
            "duration_minutes": _number_summary(durations),
            "minimum_bid_before_collapse": _number_summary(
                [float(row["minimum_bid_before_collapse"]) for row in zero_rows if row.get("minimum_bid_before_collapse") is not None]
            ),
            "intermediate_bid_rate": _rate(
                sum(row["has_intermediate_nonzero_bid"] for row in zero_rows), len(zero_rows)
            ),
            "minus_5pct_stop_band_rate": _rate(
                sum(row["has_observed_bid_in_minus_5pct_band"] for row in zero_rows), len(zero_rows)
            ),
            "target_200_depth_rate": _rate(
                sum(row["target_200_depth_reached"] for row in definitive), len(definitive)
            ),
            "net_after_round_trip_cost_p50": net_p50,
            "net_after_round_trip_cost_p90": net_p90,
            "conservative_lower_bound_per_share_p90": conservative,
        }
    return output


def _group_summaries(
    records: Sequence[Mapping[str, Any]],
    *,
    target_rises: Sequence[Decimal],
    candidate_counts: Mapping[tuple[str, str], int],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[(str(row["station_id"]), str(row["entry_band"]))].append(row)
    return [
        {
            "station_id": station,
            "entry_band": band,
            **_summarize_records(
                rows,
                target_rises=target_rises,
                candidate_count=candidate_counts.get((station, band), 0),
            ),
        }
        for (station, band), rows in sorted(groups.items())
    ]


def _margin_summaries(
    records: Sequence[Mapping[str, Any]],
    *,
    target_rises: Sequence[Decimal],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        margin_band = str(row.get("margin_band") or "unknown")
        if margin_band == "unknown":
            continue
        groups[(str(row["station_id"]), str(row["entry_band"]), margin_band)].append(row)
    return [
        {
            "station_id": station,
            "entry_band": band,
            "physical_margin_band": margin,
            **_summarize_records(rows, target_rises=target_rises, candidate_count=len(rows)),
        }
        for (station, band, margin), rows in sorted(groups.items())
    ]


def _joint_margin_peak_summaries(
    records: Sequence[Mapping[str, Any]],
    *,
    target_rises: Sequence[Decimal],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        margin = str(row.get("margin_band") or "unknown")
        phase = str(row.get("peak_phase") or "typical_peak_unknown")
        if margin == "unknown":
            continue
        groups[(str(row["station_id"]), str(row["entry_band"]), margin, phase)].append(row)
    return [
        {
            "station_id": station,
            "entry_band": band,
            "physical_margin_band": margin,
            "peak_phase": phase,
            **_summarize_records(rows, target_rises=target_rises, candidate_count=len(rows)),
        }
        for (station, band, margin, phase), rows in sorted(groups.items())
    ]


def analyze_price_paths(
    pairs: Sequence[Mapping[str, Any]],
    *,
    event_metadata: Mapping[str, Mapping[str, Any]],
    observations_by_station: Mapping[str, Sequence[Any]],
    typical_peak_minutes_by_station: Mapping[str, int],
    settled_event_slugs: Sequence[str] = (),
    settlement_end_by_event: Mapping[str, datetime] | None = None,
    entry_bands_by_station: Mapping[str, Sequence[str]] | None = None,
    entry_size_usd: Decimal = ENTRY_SIZE_USD,
    target_rises: Sequence[Decimal] = TARGET_RISES,
) -> dict[str, Any]:
    """Measure target reachability from fully executable NO entries.

    The primary target rate is a *best-bid touch* rate, because that is the
    requested price-path observable.  ``target_200_depth_rate`` is reported in
    parallel and is the stricter rate requiring the same number of shares to
    walk the future NO bids at an average price at or above the target.
    """

    if entry_size_usd <= 0:
        raise ValueError("entry_size_usd must be positive")
    target_rises = tuple(Decimal(str(value)) for value in target_rises)
    if any(value <= 0 for value in target_rises):
        raise ValueError("target_rises must be positive")
    metadata_by_event = {str(key): value for key, value in event_metadata.items()}
    allowed_bands = (
        {
            str(station).upper(): {str(band) for band in bands}
            for station, bands in entry_bands_by_station.items()
        }
        if entry_bands_by_station is not None
        else None
    )
    weather = _coerced_observations(observations_by_station)
    settlement_ends = {
        str(event): _aware(value)
        for event, value in (settlement_end_by_event or {}).items()
        if _aware(value) is not None
    }
    settled = {str(value) for value in settled_event_slugs}
    timelines: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    data_cutoff: datetime | None = None
    metadata_missing = 0
    for pair in pairs:
        event_slug = str(pair.get("event_slug") or "")
        market_slug = str(pair.get("market_slug") or "")
        quote_at = _quote_at(pair)
        if not event_slug or not market_slug or quote_at is None:
            continue
        if event_slug not in metadata_by_event:
            metadata_missing += 1
            continue
        timelines[(event_slug, market_slug)].append(pair)
        data_cutoff = max(data_cutoff, quote_at) if data_cutoff is not None else quote_at
    for rows in timelines.values():
        rows.sort(key=lambda row: _quote_at(row) or datetime.min.replace(tzinfo=UTC))

    candidate_counts: dict[tuple[str, str], int] = defaultdict(int)
    records: list[dict[str, Any]] = []
    excluded_incomplete = 0
    excluded_non_target_day = 0
    for (event_slug, market_slug), timeline in sorted(timelines.items()):
        metadata = metadata_by_event[event_slug]
        try:
            station = str(metadata["station_id"]).upper()
            timezone = ZoneInfo(str(metadata["timezone"]))
            target_date = date.fromisoformat(str(metadata["target_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        observations = weather.get(station, ())
        parsed_bucket = _bucket_upper_and_unit(market_slug)
        upper, unit = parsed_bucket if parsed_bucket is not None else (None, "fahrenheit")
        elimination_at = _physical_elimination(
            observations,
            target_date=target_date,
            timezone=timezone,
            upper=upper,
            unit=unit,
        )
        day_end = _day_end(target_date, timezone)
        horizon_end = min(day_end, settlement_ends.get(event_slug, day_end))
        last_quote = max((_quote_at(row) for row in timeline if _quote_at(row) is not None), default=None)
        horizon_complete = data_cutoff is not None and data_cutoff >= horizon_end and last_quote is not None and last_quote >= horizon_end
        for pair in timeline:
            entry_at = _quote_at(pair)
            no = pair.get("no")
            if entry_at is None or not isinstance(no, Mapping):
                continue
            local_entry = entry_at.astimezone(timezone)
            if local_entry.date() != target_date:
                excluded_non_target_day += 1
                continue
            asks = _levels_decimal(no.get("asks"))
            bids = _levels_decimal(no.get("bids"), reverse=True)
            no_ask = _top(asks, bids=False)
            if no_ask is None:
                continue
            band = price_band_for_ask(no_ask)
            if band is None:
                continue
            if allowed_bands is not None and band not in allowed_bands.get(station, set()):
                continue
            candidate_counts[(station, band)] += 1
            entry = estimate_execution_cost(asks, entry_size_usd, "buy")
            if entry is None or entry.filled_fraction < 1:
                excluded_incomplete += 1
                continue
            exit_at_entry = estimate_execution_by_shares(bids, entry.filled_shares, "sell") if bids else None
            margin, _tier, eliminated = _physical_state_at(
                observations,
                entry_at=entry_at,
                target_date=target_date,
                timezone=timezone,
                upper=upper,
                unit=unit,
            )
            peak_minutes = typical_peak_minutes_by_station.get(station)
            peak_at = (
                datetime.combine(target_date, time.min, tzinfo=timezone)
                + timedelta(minutes=int(peak_minutes))
                if peak_minutes is not None
                else None
            )
            hours_to_peak = (
                (peak_at - local_entry).total_seconds() / 3600
                if peak_at is not None
                else None
            )
            future = _future_rows(timeline, entry_at=entry_at, horizon_end=horizon_end)
            outcomes = _path_outcomes(
                entry_at=entry_at,
                entry_average=entry.average_fill_price,
                entry_shares=entry.filled_shares,
                future=future,
                rises=target_rises,
                elimination_at=elimination_at,
                horizon_end=horizon_end,
                horizon_complete=horizon_complete,
            )
            records.append(
                {
                    "entry_at": entry_at.isoformat(),
                    "event_slug": event_slug,
                    "market_slug": market_slug,
                    "station_id": station,
                    "target_date": target_date.isoformat(),
                    "entry_band": band,
                    "entry_average": float(entry.average_fill_price),
                    "entry_top_ask": float(no_ask),
                    "entry_slippage": float(entry.slippage_vs_top),
                    "entry_fee_per_share": float(entry.fee_per_share),
                    "entry_shares": float(entry.filled_shares),
                    "exit_at_entry": _estimate_payload(exit_at_entry),
                    "round_trip_cost": (
                        float(_cost_per_share(entry, exit_at_entry))
                        if _cost_per_share(entry, exit_at_entry) is not None
                        else None
                    ),
                    "physical_margin_f": margin,
                    "margin_band": _margin_band(margin),
                    "physical_eliminated_at_entry": eliminated,
                    "physical_elimination_at": elimination_at.isoformat() if elimination_at else None,
                    "hours_to_typical_peak": hours_to_peak,
                    "peak_phase": _peak_phase(hours_to_peak),
                    "horizon_end": horizon_end.isoformat(),
                    "horizon_complete": horizon_complete,
                    "future_quote_count": len(future),
                    "event_settled": event_slug in settled,
                    "outcomes": outcomes,
                }
            )

    station_bands = _group_summaries(
        records, target_rises=target_rises, candidate_counts=candidate_counts
    )
    margin = _margin_summaries(records, target_rises=target_rises)
    joint = _joint_margin_peak_summaries(records, target_rises=target_rises)
    event_slugs = {str(key[0]) for key in timelines}
    settled_depth_events = event_slugs & settled
    settled_records = [row for row in records if row["event_settled"]]
    unsettled_records = [row for row in records if not row["event_settled"]]
    margin_known_entry_count = sum(
        row.get("margin_band") not in (None, "unknown") for row in records
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": data_cutoff.isoformat() if data_cutoff else None,
        "paired_snapshot_count": len(pairs),
        "market_timeline_count": len(timelines),
        "event_count": len(event_slugs),
        "metadata_missing_pair_count": metadata_missing,
        "entry_size_usd": float(entry_size_usd),
        "target_rises": [float(value) for value in target_rises],
        "candidate_entry_count": sum(candidate_counts.values()),
        "usable_entry_count": len(records),
        "excluded_incomplete_entry_count": excluded_incomplete,
        "excluded_non_target_day_pair_count": excluded_non_target_day,
        "weather_observation_count": sum(len(rows) for rows in weather.values()),
        "weather_station_count": len(weather),
        "physical_margin_known_entry_count": margin_known_entry_count,
        "physical_margin_unknown_entry_count": len(records) - margin_known_entry_count,
        "settlement_overlap": {
            "settled_catalog_event_count": len(settled),
            "depth_event_count": len(event_slugs),
            "settled_depth_event_count": len(settled_depth_events),
            "settled_depth_pair_count": sum(1 for pair in pairs if str(pair.get("event_slug") or "") in settled),
            "settled_usable_entry_count": len(settled_records),
            "unsettled_usable_entry_count": len(unsettled_records),
            "settled_path_pnl_status": (
                "N/A: no settled/depth overlap"
                if not settled_depth_events
                else "available only as a path sample; realized settlement PnL still separate"
            ),
        },
        "primary_bands": PRIMARY_BANDS,
        "entry_bands_by_station": {
            station: sorted(bands)
            for station, bands in (allowed_bands or {}).items()
        },
        "station_bands": station_bands,
        "physical_margin": margin,
        "joint_margin_peak": joint,
        "records": records,
        "semantics": {
            "entry_quote": "真实 NO ask 的 $200 深度均价；非顶层 ask、非 midpoint、非 p、非 1−YES",
            "path_quote": "严格晚于入场时刻的同一 NO token 真实 best bid；不使用成交价作为盘口",
            "target_rate": "best bid 触达目标价；同时报告要求同样 $200 shares 的未来深度均价触达率",
            "zero_gap": "物理出局发生在目标触达之前；记录出局前到首个出局后报价的最低真实 bid",
            "cost": "入场 ask 与同一快照 NO bid 的 $200 同簿滑点+两笔 taker 手续费；p50/p90 是成本门槛，不是假定未来盘口",
            "censoring": "数据截止早于本地日终/结算，且未先触达/出局的路径记为 censored，不当作失败",
            "trades_boundary": "成交价不是可成交 ask/bid，只能作为成交事件信息，未用于本分析",
            "independence": "每个 5 分钟配对快照是一个入场点；同一市场/日期的点相关，Wilson 为名义区间，不替代聚类推断",
        },
        "execution_enabled": False,
    }


def _fmt_number(value: Any, digits: int = 3) -> str:
    return "N/A" if value is None else f"{float(value):.{digits}f}"


def _fmt_cents(value: Any, digits: int = 2) -> str:
    """Format a per-share USDC value as cents for the human report."""
    return "N/A" if value is None else f"{float(value) * 100:.{digits}f}¢"


def _fmt_pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.1%}"


def render_price_path_report(result: Mapping[str, Any], output_path: Path) -> None:
    lines = [
        "# 中间价位 NO 价格路径成功率",
        "",
        f"生成时间：{result.get('generated_at')}；订单簿数据截止：{result.get('data_cutoff')}。",
        "",
        "本报告回答的是价差路径，不是最终结算胜负。入场必须是目标日、真实 NO ask 的 $200 深度完整成交；",
        "之后只看同一 NO token 严格晚于入场时刻的真实 bid。成交价不是可成交 ask/bid，",
        "`/trades`、`prices-history.p`、midpoint、`1−YES` 均没有用于构造盘口。",
        "",
        f"- 配对快照：{result.get('paired_snapshot_count')}；市场时间线：{result.get('market_timeline_count')}；",
        f"可用入场点：{result.get('usable_entry_count')}（候选真实 ask 点 {result.get('candidate_entry_count')}，"
        f"因 $200 深度不完整排除 {result.get('excluded_incomplete_entry_count')}）。",
        f"- 余量可知入场点：{result.get('physical_margin_known_entry_count', 'N/A')}；"
        f"因入场前没有同时满足 source/receipt 截止条件而无法分层：{result.get('physical_margin_unknown_entry_count', 'N/A')}。",
        f"- 目标涨幅：{', '.join(f'+{float(value) * 100:.0f}¢' for value in result.get('target_rises', ())) }。",
        "- `target_reached` 是 best bid 触达；另列 `target_200_depth_rate`，只有未来 NO bid 深度能完整承接入场 shares 且深度均价达到目标才算。",
        "- 数据未覆盖本地日终/结算的路径记为 censored，不算未达标；同一市场多个 5 分钟入场点相关，Wilson 区间是名义区间。",
        "",
        "## 已结算重叠与前向路径",
        "",
    ]
    overlap = result["settlement_overlap"]
    lines.extend(
        [
            f"- 已结算目录事件：{overlap['settled_catalog_event_count']}；深度事件：{overlap['depth_event_count']}；"
            f"已结算×深度事件：{overlap['settled_depth_event_count']}；",
            f"已结算×深度配对：{overlap['settled_depth_pair_count']}；可用已结算入场点：{overlap['settled_usable_entry_count']}；"
            f"未结算前向入场点：{overlap['unsettled_usable_entry_count']}。",
            f"结论：{overlap['settled_path_pnl_status']}。",
            "",
        ]
    )
    lines.extend(
        [
            "## Q1 入场 ask 区间 × 目标涨幅",
            "",
            "`target_rate` 的分母是非 censored 的 definitive 路径；每格显示达标、跳空归零、完整窗口未达标和 censored。",
            "价格均价、成本和净价差均按每份 USDC 计；表中成本/净价差换算为¢。净价差=目标涨幅−同簿往返成本门槛；p90 是保守成本口径，不是未来退出价的保证。",
            "",
            "|站点|入场 ask 区间|入场点|目标|达标率 (Wilson)|$200 深度达标|跳空归零|未达标非零|censored|达标时长 p50/p90 分钟|成本¢/份 p50/p90|净价差¢/份 p50/p90|",
            "|---|---:|---:|---:|---|---|---|---|---|---:|---:|---:|",
        ]
    )
    for group in result.get("station_bands", ()):
        for _rise, target in group["targets"].items():
            lines.append(
                f"| {group['station_id']} | {group['entry_band']} | {group['entry_count']} | +{float(target['rise']) * 100:.0f}¢ | "
                f"{_rate_text(target['target_rate'])} | {_rate_text(target['target_200_depth_rate'])} | "
                f"{_rate_text(target['zero_gap_rate'])} | {_rate_text(target['not_reached_nonzero_rate'])} | "
                f"{_rate_text(target['censored_rate'])} | {_fmt_number(target['duration_minutes'].get('p50'))}/{_fmt_number(target['duration_minutes'].get('p90'))} | "
                f"{_fmt_cents(group['round_trip_cost'].get('p50'))}/{_fmt_cents(group['round_trip_cost'].get('p90'))} | "
                f"{_fmt_cents(target.get('net_after_round_trip_cost_p50'))}/{_fmt_cents(target.get('net_after_round_trip_cost_p90'))} |"
            )
    lines.extend(
        [
            "",
            "说明：正的 p90 净价差只是成本门槛上仍有空间，不等于正期望；还必须看达标率、跳空损失和失败路径的实际退出价。",
            "目标达标率若 n<30，表中已附‘统计不可靠’；N=0 直接保留 N=0。",
            "",
            "## 跳空归零与止损可行性",
            "",
            "最低 bid 是从入场后到物理出局时（含首个出局后真实报价）观察到的最低可成交 best bid；",
            "`−5% 止损带`表示至少有一个观察到的 bid 落在入场价的 95%–100% 区间，不能把它等同于实际止损订单成交。",
            "",
            "|站点|入场区间|目标|归零样本|最低 bid p10/p50/p90|有中间非零 bid|观察到 −5% 止损带|",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for group in result.get("station_bands", ()):
        for target in group["targets"].values():
            minimum = target["minimum_bid_before_collapse"]
            lines.append(
                f"| {group['station_id']} | {group['entry_band']} | +{float(target['rise']) * 100:.0f}¢ | "
                f"{target['zero_gap_rate']['count']} | "
                f"{_fmt_number(minimum.get('p10'))}/{_fmt_number(minimum.get('p50'))}/{_fmt_number(minimum.get('p90'))} | "
                f"{_rate_text(target['intermediate_bid_rate'])} | {_rate_text(target['minus_5pct_stop_band_rate'])} |"
            )
    lines.extend(
        [
            "",
            "## Q2 物理余量分层",
            "",
            "余量=入场时已知、且 source 与 receipt 均不晚于入场的当日观测最高温（结算舍入后）−桶上界。",
            "余量未知的入场点不进入下表；未知数量见上方，不能把它们当作任一余量层。",
            "",
            "|站点|入场区间|余量层|入场点|+5¢ 达标|+10¢ 达标|+13¢ 达标|+20¢ 达标|",
            "|---|---:|---:|---:|---|---|---|---|",
        ]
    )
    for group in result.get("physical_margin", ()):
        targets = group["targets"]
        lines.append(
            f"| {group['station_id']} | {group['entry_band']} | {group['physical_margin_band']} | {group['entry_count']} | "
            + " | ".join(_rate_text(targets.get(f"{rise:.2f}", {}).get("target_rate", {"rate": None})) for rise in TARGET_RISES)
            + " |"
        )
    lines.extend(
        [
            "",
            "### 余量层 × 距典型高点联合结果",
            "",
            "下表同时列出达标/跳空归零；每个单元格分母都是该目标的 definitive 路径，均附 Wilson 95%。",
            "只有 n≥30 的格子可作方向性参考；n<30 已显式标注统计不可靠。完整十城结果也保留在 JSON `joint_margin_peak`。",
            "",
            "|站点|入场区间|余量层|距典型高点|n|+5¢ 达标/归零|+10¢ 达标/归零|+13¢ 达标/归零|+20¢ 达标/归零|",
            "|---|---|---|---|---:|---|---|---|---|",
        ]
    )
    primary_stations = set(PRIMARY_BANDS)
    joint_rows = sorted(
        (
            row
            for row in result.get("joint_margin_peak", ())
            if row.get("station_id") in primary_stations
        ),
        key=lambda row: (
            str(row.get("station_id")),
            str(row.get("entry_band")),
            str(row.get("physical_margin_band")),
            str(row.get("peak_phase")),
        ),
    )
    for group in joint_rows:
        cells = []
        for rise in TARGET_RISES:
            target = group["targets"].get(f"{rise:.2f}", {})
            cells.append(
                f"达标 {_rate_text(target.get('target_rate', {'rate': None}))}<br>归零 {_rate_text(target.get('zero_gap_rate', {'rate': None}))}"
            )
        lines.append(
            f"| {group['station_id']} | {group['entry_band']} | {group['physical_margin_band']} | {group['peak_phase']} | {group['entry_count']} | "
            + " | ".join(cells)
            + " |"
        )
    lines.extend(
        [
            "",
            "## 当前判断",
            "",
            "- 尾桶的结构性无 ask 结论不因本报告改变；本报告只评价有真实 ask 且 $200 可完整买入的路径。",
            "- 进入与赚钱是两件事：p90 往返成本若高于目标涨幅，保守净价差为负；即便成本为正，若达标率/跳空风险证据不足，也不能宣布正期望。",
            "- 选定两站的部分 p50 净价差为正（KLAX +10/+13/+20¢、KLGA +20¢），但对应 p90 净价差全部为负，且按 Wilson 下界/上界与跳空损失构造的保守 p90 屏幕也全部为负。",
            "- 任何已结算×真实深度重叠不足 30 的 realized PnL 结论必须写 N/A；前向路径可以报告，但不能冒充已结算样本。",
            "",
            "## 机器可读语义",
            "",
            f"- execution_enabled={result.get('execution_enabled')}；天气原始 WRH 观测数={result.get('weather_observation_count')}。",
            "- 所有输入默认排除了上游维护/恢复窗口（由 paired_book_snapshots 在 CLI 层完成）。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
