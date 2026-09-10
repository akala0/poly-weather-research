"""Weather collection provenance and strict no-lookahead guards."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from poly_weather.trade_evidence import parse_trade_timestamp

CollectionMode = Literal["realtime", "historical_backfill"]
REALTIME: CollectionMode = "realtime"
HISTORICAL_BACKFILL: CollectionMode = "historical_backfill"


def collection_mode(row: Mapping[str, Any]) -> CollectionMode:
    """Require explicit provenance; no verified legacy producer exception exists."""
    value = str(row.get("collection_mode") or "")
    if value not in {REALTIME, HISTORICAL_BACKFILL}:
        raise ValueError("UNKNOWN_WEATHER_COLLECTION_MODE")
    return value  # type: ignore[return-value]


def require_realtime_for_no_lookahead(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Reject post-hoc QC data in any strict no-lookahead analysis path."""
    selected = list(rows)
    rejected = [row for row in selected if collection_mode(row) != REALTIME]
    if rejected:
        raise ValueError(
            "strict no-lookahead analysis refuses historical_backfill weather data"
        )
    return selected


def weather_receipt_time(row: Mapping[str, Any]) -> datetime:
    """Conservative receipt bound; source validity is independently checked."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def integer(value: Any, reason: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(reason)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(reason) from exc
        if str(number) != str(value) or number < 0:
            raise ValueError(reason)
        return number

    try:
        rendered = row.get("received_at")
        iso = None
        if rendered is not None:
            iso = parse_trade_timestamp(rendered)
            if iso is None:
                raise ValueError("INVALID_OR_INEXACT_WEATHER_RECEIPT")
        ns = row.get("received_at_ns")
        exact_bound = (epoch + timedelta(microseconds=(integer(ns, "INVALID_WEATHER_RECEIPT_NS") + 999) // 1000)
                       if ns is not None else None)
    except (OverflowError, OSError) as exc:
        raise ValueError("WEATHER_CLOCK_OUT_OF_RANGE") from exc
    if iso is not None and exact_bound is not None and abs(iso - exact_bound) > timedelta(microseconds=1):
        raise ValueError("WEATHER_RECEIPT_CLOCK_CONFLICT")
    bounds = [value for value in (iso, exact_bound) if value is not None]
    if not bounds:
        raise ValueError("MISSING_WEATHER_RECEIPT")
    return max(bounds)


def weather_observation_times(row: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """Exact source ms and conservative receipt bound for the daemon envelope."""
    receipt = weather_receipt_time(row)
    value = row.get("source_timestamp_ms")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("INVALID_WEATHER_SOURCE_MS")
    try:
        number = int(value)
        if str(number) != str(value) or number < 0:
            raise ValueError("INVALID_WEATHER_SOURCE_MS")
        source = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=number)
    except OverflowError as exc:
        raise ValueError("WEATHER_CLOCK_OUT_OF_RANGE") from exc
    if source > receipt:
        raise ValueError("WEATHER_SOURCE_AFTER_RECEIPT")
    return source, receipt


def forecast_initialization(payload: Any) -> datetime | None:
    """Only explicit model-run fields count; neither process run_id nor hourly valid-time does."""
    names = {"model_run_initialization", "model_run_init", "run_initialization", "run_init",
             "initialization_time", "initialisation_time", "init_time", "reference_time"}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).casefold() in names:
                return parse_trade_timestamp(value)
        for value in payload.values():
            result = forecast_initialization(value)
            if result is not None:
                return result
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            result = forecast_initialization(value)
            if result is not None:
                return result
    return None


def forecast_vintage(row: Mapping[str, Any]) -> tuple[datetime | None, str]:
    """Validate fixed initialization per model; unsupported real-time forecasts stay diagnostic."""
    receipt = weather_receipt_time(row)

    def prohibited(value):
        if isinstance(value, Mapping):
            if "lead_days" in value:
                try:
                    lead = value["lead_days"]
                    if isinstance(lead, bool) or str(int(lead)) != str(lead) or int(lead) < 1:
                        return True
                except (TypeError, ValueError):
                    return True
            return any(prohibited(item) for item in value.values())
        return any(prohibited(item) for item in value) if isinstance(value, (list, tuple)) else False

    if prohibited(row):
        return None, "INVALID_FORECAST_LEAD_DAYS"
    raw = row.get("raw")
    models = raw.get("models") if isinstance(raw, Mapping) else None
    payloads = list(models.values()) if isinstance(models, Mapping) and models else [raw]
    initializations = [forecast_initialization(payload) for payload in payloads]
    if any(value is None for value in initializations):
        return None, "UNKNOWN_FIXED_FORECAST_VINTAGE"
    latest = max(initializations)
    if latest > receipt:
        return None, "FORECAST_INITIALIZATION_AFTER_RECEIPT"
    return latest, "VERIFIED_EXPLICIT_FORECAST_VINTAGE"
