"""Strict, receipt-aware external-information clock for weather markets.

The clock is deliberately independent from the maker strategy.  It turns the
different public weather/status archives into a common event stream, keeps
source time separate from local availability time, and refuses to use an event
for phase classification when either timestamp is missing.  A repeated payload
is not a new information event; a changed payload at the same source timestamp
is a revision and therefore is a new event.

This module never calls a trading endpoint and does not infer a model release
time from a forecast horizon.  Open-Meteo responses without an explicit run
initialisation timestamp remain visible as unusable diagnostics, but are not
allowed to reset a live/replayed regime.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from poly_weather.archive_io import jsonl_archive_paths, open_jsonl_text

UTC_MAX = datetime.max.replace(tzinfo=UTC)
_MISSING = object()
_VOLATILE_KEYS = frozenset(
    {
        "run_id",
        "sequence",
        "received_at",
        "received_at_ns",
        "fetched_at",
        "latency_ms",
        "request_url",
        "generationtime_ms",
        "metadata_query_time",
        "metadata_parse_time",
        "data_query_time",
        "data_parse_time",
        "total_metadata_time",
        "total_data_time",
        "total_time",
    }
)
_SEMANTIC_VOLATILE_KEYS = _VOLATILE_KEYS | frozenset(
    {
        "timestamp",
        "obstime",
        "observationtime",
        "reporttime",
        "receipttime",
        "issuetime",
        "validtime",
        "time",
        "source_timestamp_ms",
    }
)
_GLOBAL_SCOPE_KINDS = frozenset({"official_status", "market_health_status"})


class ImpactClass(StrEnum):
    """Declared information importance; it is never inferred from later PnL."""

    HARD_RESET = "HARD_RESET"
    SOFT_UPDATE = "SOFT_UPDATE"
    NO_OP = "NO_OP"
    INVALID = "INVALID"


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        else:
            if abs(number) > 10**14:
                number /= 1_000_000_000
            elif abs(number) > 10**11:
                number /= 1_000
            try:
                parsed = datetime.fromtimestamp(number, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _stable(value: Any) -> Any:
    """Remove transport-only fields before hashing an information payload."""
    if isinstance(value, Mapping):
        return {
            str(key): _stable(item)
            for key, item in value.items()
            if str(key).casefold() not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    return _jsonable(value)


def _semantic_stable(value: Any) -> Any:
    """Normalize an observation without treating transport/report time as news."""
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_stable(item)
            for key, item in value.items()
            if str(key).casefold() not in _SEMANTIC_VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_semantic_stable(item) for item in value]
    return _jsonable(value)


def _semantic_hash(value: Any) -> str:
    encoded = json.dumps(
        _semantic_stable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def payload_hash(value: Any) -> str:
    encoded = json.dumps(
        _stable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return bool(value)


def _temperature_f(row: Mapping[str, Any]) -> Decimal | None:
    if row.get("value") is not None:
        try:
            value = Decimal(str(row["value"]))
            unit = str(row.get("unitCode") or row.get("unit") or "").casefold()
            return (
                value * Decimal("9") / Decimal("5") + Decimal("32")
                if "celsius" in unit or unit.endswith(":c") or unit in {"c", "degc"}
                else value
            )
        except (ArithmeticError, TypeError, ValueError):
            pass
    for key in ("temperature_f", "temp_f", "temperatureF"):
        if row.get(key) is not None:
            try:
                return Decimal(str(row[key]))
            except (ArithmeticError, TypeError, ValueError):
                pass
    for key in ("temperature_c", "temp_c", "temperature", "temp"):
        if row.get(key) is not None:
            try:
                return Decimal(str(row[key])) * Decimal("9") / Decimal("5") + Decimal("32")
            except (ArithmeticError, TypeError, ValueError):
                pass
    properties = row.get("properties")
    if isinstance(properties, Mapping):
        nested = properties.get("temperature")
        if isinstance(nested, Mapping):
            return _temperature_f(nested)
    return None


def _report_kind(report: Mapping[str, Any]) -> str:
    report_type = str(report.get("metarType") or report.get("reportType") or "").upper()
    raw_text = str(report.get("rawOb") or report.get("raw_text") or "").lstrip().upper()
    return "speci" if "SPECI" in report_type or raw_text.startswith("SPECI") else "metar"


def _candidate_mappings(payload: Any) -> Iterator[Mapping[str, Any]]:
    """Yield the small set of weather payload layers that carry semantic fields."""
    if not isinstance(payload, Mapping):
        return
    queue: list[tuple[Mapping[str, Any], int]] = [(payload, 0)]
    seen: set[int] = set()
    while queue:
        value, depth = queue.pop(0)
        if id(value) in seen:
            continue
        seen.add(id(value))
        yield value
        if depth >= 3:
            continue
        for key in (
            "properties",
            "source_payload",
            "report",
            "observation",
            "latest_observation",
            "conditions",
            "raw",
        ):
            nested = value.get(key)
            if isinstance(nested, Mapping):
                queue.append((nested, depth + 1))
        for key in ("reports", "observations", "cloudLayers", "clouds"):
            nested = value.get(key)
            if isinstance(nested, list):
                queue.extend(
                    (item, depth + 1)
                    for item in nested[:8]
                    if isinstance(item, Mapping)
                )


def _field_value(payload: Any, *names: str) -> Any:
    targets = {name.casefold() for name in names}
    for row in _candidate_mappings(payload):
        for key, value in row.items():
            if str(key).casefold() in targets and value not in (None, ""):
                return value
    return None


def _field_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        if value.get("value") not in (None, ""):
            unit = value.get("unitCode") or value.get("unit")
            return f"{value.get('value')}:{unit}" if unit else str(value.get("value"))
        return json.dumps(_semantic_stable(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return json.dumps(_semantic_stable(value), ensure_ascii=False, sort_keys=True)
    return str(value).strip().casefold() or None


def _weather_risk_fields(payload: Any) -> dict[str, str]:
    """Extract only presently visible weather-regime fields from a report.

    Report and receipt timestamps intentionally do not participate.  That is
    what lets repeated 5-minute polling of an unchanged WRH observation remain
    a NO_OP while preserving a changed wind/cloud/phenomena report as a risk
    update.
    """
    temperature = next(
        (
            value
            for row in _candidate_mappings(payload)
            if (value := _temperature_f(row)) is not None
        ),
        None,
    )
    values = {
        "temperature_f": str(temperature) if temperature is not None else None,
        "wind_direction": _field_text(
            _field_value(payload, "windDirection", "wind_dir", "wdir", "wind_direction")
        ),
        "wind_speed": _field_text(
            _field_value(payload, "windSpeed", "wind_speed", "wspd")
        ),
        "cloud": _field_text(
            _field_value(payload, "cloudLayers", "clouds", "cloud", "cover", "skyCover")
        ),
        "weather": _field_text(
            _field_value(
                payload,
                "textDescription",
                "wxString",
                "weather",
                "presentWeather",
                "conditions",
            )
        ),
        "dewpoint": _field_text(
            _field_value(payload, "dewpoint", "dew_point", "dewp")
        ),
    }
    output = {key: value for key, value in values.items() if value is not None}
    # Some provider products do not expose a structured weather report.  A
    # changed content body is still a visible soft update, but timestamps alone
    # have been removed from this semantic digest.
    if not output and payload is not None:
        output["semantic_hash"] = _semantic_hash(payload)
    return output


def _weather_regime_changed(
    prior: Mapping[str, str] | None, current: Mapping[str, str]
) -> bool:
    if not prior:
        return False
    # A normal cloud-layer wording or wind/dew revision is a SOFT_UPDATE.  A
    # HARD weather-regime transition is intentionally narrower: visible severe
    # phenomena (for example TS/SQ/FZRA/GR) can change the temperature-path
    # distribution, while ordinary ``partly cloudy -> mostly cloudy`` text
    # must not make a five-minute polling cadence permanently EVENT.
    before = str(prior.get("weather") or "").casefold()
    after = str(current.get("weather") or "").casefold()
    if before == after:
        return False
    severe_markers = (
        "thunder",
        " ts",
        "ts ",
        "tsra",
        "squall",
        " fzra",
        "freezing rain",
        "hail",
        "gr",
        "funnel",
        "tornado",
    )
    return any(marker in f" {before} " or marker in f" {after} " for marker in severe_markers)


def _numeric_with_unit(value: Any) -> tuple[Decimal, str] | None:
    """Parse the compact ``value:unit`` strings retained in risk fields."""
    if value in (None, ""):
        return None
    match = re.match(r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))(?:\s*:\s*(.*))?$", str(value))
    if match is None:
        return None
    try:
        number = Decimal(match.group(1))
    except (ArithmeticError, ValueError):
        return None
    return number, (match.group(2) or "").casefold()


def _wind_mph(value: Any) -> Decimal | None:
    parsed = _numeric_with_unit(value)
    if parsed is None:
        return None
    number, unit = parsed
    if "km" in unit:
        return number * Decimal("0.621371")
    if "m_s" in unit or "m/s" in unit or "metre_per_second" in unit:
        return number * Decimal("2.236936")
    if "knot" in unit or unit.endswith("kt"):
        return number * Decimal("1.150779")
    # Fixtures and several METAR-derived records omit a unit.  Treat those
    # values as mph rather than fabricating a conversion from an unknown unit.
    return number


def _fahrenheit(value: Any) -> Decimal | None:
    parsed = _numeric_with_unit(value)
    if parsed is None:
        return None
    number, unit = parsed
    if "degc" in unit or "celsius" in unit:
        return number * Decimal("1.8") + Decimal("32")
    return number


def _wind_signature(fields: Mapping[str, str]) -> tuple[str, str | None] | None:
    """Return fixed, present-time wind-risk bands rather than decimal jitter.

    Direction is only meaningful once wind is at least breezy.  The bands are
    intentionally coarse: routine five-minute variations inside one observed
    regime are receipt diagnostics, while a transition between light, breezy,
    and strong wind remains a visible SOFT update.
    """
    speed = _wind_mph(fields.get("wind_speed"))
    if speed is None:
        return None
    if speed < Decimal("10"):
        return "light", None
    if speed < Decimal("20"):
        speed_band = "breezy"
    else:
        speed_band = "strong"
    direction = _numeric_with_unit(fields.get("wind_direction"))
    if direction is None:
        return speed_band, None
    bearing = direction[0] % Decimal("360")
    sector = ("north", "east", "south", "west")[
        int(((bearing + Decimal("45")) % Decimal("360")) // Decimal("90"))
    ]
    return speed_band, sector


def _cloud_signature(value: Any) -> str | None:
    text = str(value or "").casefold()
    if not text or text in {"[]", "{}", "none", "null"}:
        return None
    if any(marker in text for marker in ("ovc", "overcast", "cloudy")):
        return "overcast"
    if any(marker in text for marker in ("bkn", "broken", "mostly cloudy")):
        return "broken"
    if any(marker in text for marker in ("sct", "scattered", "partly cloudy")):
        return "scattered"
    if any(marker in text for marker in ("few", "mostly clear")):
        return "few"
    if any(marker in text for marker in ("clr", "skc", "clear", "sunny")):
        return "clear"
    return None


def _phenomena_signature(value: Any) -> str | None:
    text = str(value or "").casefold()
    if not text or text in {"[]", "{}", "none", "null"}:
        return None
    if any(marker in text for marker in ("rain", "shower", "snow", "drizzle", "ice")):
        return "precipitation"
    if any(marker in text for marker in ("fog", "mist", "haze", "smoke", "dust")):
        return "visibility_reduction"
    # Ordinary cloud wording is represented by the cloud signature above;
    # treating ``clear -> cloudy`` text and raw cloud-layer shape as two
    # independent events would turn feed-format variation into information.
    return None


def _weather_risk_signature(fields: Mapping[str, str]) -> dict[str, str]:
    """Coarsen visible fields into fixed material-risk categories.

    The raw fields remain in the event record for audit.  Only this signature
    controls SOFT classification for non-SPECI observations, so a 0.1°C dew
    point or 10° light-wind change is not misrepresented as fresh external
    information.
    """
    output: dict[str, str] = {}
    wind = _wind_signature(fields)
    if wind is not None:
        output["wind_speed_band"] = wind[0]
        if wind[1] is not None:
            output["wind_sector"] = wind[1]
    cloud = _cloud_signature(fields.get("cloud")) or _cloud_signature(fields.get("weather"))
    if cloud is not None:
        output["cloud_regime"] = cloud
    phenomena = _phenomena_signature(fields.get("weather"))
    if phenomena is not None:
        output["phenomena"] = phenomena
    dewpoint = _fahrenheit(fields.get("dewpoint"))
    if dewpoint is not None:
        lower = (dewpoint // Decimal("5")) * Decimal("5")
        output["dewpoint_band_f"] = f"{lower}-{lower + Decimal('5')}"
    # A provider with no structured weather fields has no safer materiality
    # proxy than its timestamp-free semantic payload digest.
    if fields.get("semantic_hash"):
        output["semantic_hash"] = str(fields["semantic_hash"])
    return output


def _material_weather_changed_keys(
    prior: Mapping[str, str] | None, current: Mapping[str, str]
) -> tuple[str, ...]:
    if prior is None:
        return ()
    before = _weather_risk_signature(prior)
    after = _weather_risk_signature(current)
    # Missing source fields are not evidence of a weather transition.  Compare
    # only categories both reports actually exposed, otherwise transport/schema
    # variation would incorrectly keep the clock in DIGESTION.
    return tuple(
        key
        for key in sorted(set(before) & set(after))
        if before.get(key) != after.get(key)
    )


def _payload_context(
    payload: Any,
    *,
    station_id: str | None,
    market_day: str | None,
) -> tuple[str | None, str | None]:
    """Recover explicit station/day context from archived event payloads."""
    station = station_id.upper() if station_id else None
    day = market_day
    mappings: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        mappings.append(payload)
        for key in ("evidence", "event", "metadata"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                mappings.append(nested)
    for row in mappings:
        if station is None:
            value = row.get("station_id") or row.get("stationId") or row.get("icaoId")
            if value:
                station = str(value).upper()
        if day is None:
            value = (
                row.get("target_date")
                or row.get("targetDate")
                or row.get("market_day")
                or row.get("eventDate")
                or row.get("event_date")
                or row.get("endDateIso")
            )
            if value:
                day = str(value)[:10]
        if station is not None and day is not None:
            break
    if station is None and isinstance(payload, Mapping):
        for key in ("resolutionSource", "resolution_source", "description"):
            text = str(payload.get(key) or "")
            match = re.search(r"/(K[A-Z0-9]{3}|Z[A-Z0-9]{3})(?:[/?#]|$)", text.upper())
            if match:
                station = match.group(1)
                break
    return station, day


def _report_event(
    report: Mapping[str, Any],
    *,
    source: str,
    available_at: datetime | None,
    station_id: str | None,
    market_day: str | None,
    outer_payload: Any,
) -> InformationEvent:
    source_at = _utc(
        report.get("obsTime")
        or report.get("observationTime")
        or report.get("timestamp")
        or report.get("reportTime")
    )
    station = str(report.get("icaoId") or station_id or "").upper() or None
    kind = _report_kind(report)
    stable = {
        "report": report,
        "source": source,
        "station_id": station,
        "kind": kind,
        "source_at": source_at,
    }
    digest = payload_hash(stable)
    risk_fields = _weather_risk_fields(report)
    return InformationEvent(
        event_id=f"{source}:{kind}:{station or 'unknown'}:{source_at or 'unknown'}:{digest[:16]}",
        source=source,
        kind=kind,
        source_at=source_at,
        available_at=available_at,
        station_id=station,
        market_day=market_day,
        payload_hash=digest,
        metadata={
            "temperature_f": risk_fields.get("temperature_f"),
            "report_type": report.get("metarType") or report.get("reportType"),
            "provider_receipt_at": report.get("receiptTime"),
            "risk_fields": risk_fields,
            "payload": _stable(outer_payload),
        },
    )


def _forecast_initialisation(payload: Any) -> datetime | None:
    """Find only explicitly named model-run initialisation fields."""
    names = {
        "model_run_initialization",
        "model_run_init",
        "run_initialization",
        "run_init",
        "initialization_time",
        "initialisation_time",
        "init_time",
        "reference_time",
    }
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).casefold() in names:
                parsed = _utc(value)
                if parsed is not None:
                    return parsed
        for value in payload.values():
            parsed = _forecast_initialisation(value)
            if parsed is not None:
                return parsed
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            parsed = _forecast_initialisation(value)
            if parsed is not None:
                return parsed
    return None


def _weather_daemon_events(
    row: Mapping[str, Any], *, market_day: str | None
) -> tuple[InformationEvent, ...]:
    from poly_weather.weather_market_join import parse_weather_observation
    from poly_weather.weather_provenance import require_realtime_for_no_lookahead

    qualified_observation = None
    try:
        require_realtime_for_no_lookahead((row,))
        if row.get("product") in {"wrh_timeseries_observation", "latest_observation", "metar"}:
            qualified_observation = parse_weather_observation(row)
    except (TypeError, ValueError) as exc:
        return (InformationEvent(
            event_id=f"invalid_weather:{payload_hash(row)}", source="weather_daemon",
            kind=str(row.get("product") or "weather"), source_at=None, available_at=None,
            station_id=str(row.get("station_id") or "").upper() or None, market_day=market_day,
            payload_hash=payload_hash(row), receipt_verified=False,
            metadata={"qualification_reason": str(exc)},
        ),)
    available_at = (qualified_observation.received_at if qualified_observation is not None
                    else _utc(row.get("received_at") or row.get("received_at_ns")))
    station = str(row.get("station_id") or "").upper() or None
    product = str(row.get("product") or "")
    source = str(row.get("provider") or "weather_daemon")
    raw = row.get("raw")
    if product == "metar":
        reports = raw.get("reports") if isinstance(raw, Mapping) else None
        if isinstance(reports, list):
            return tuple(
                _report_event(
                    report,
                    source=source,
                    available_at=available_at,
                    station_id=station,
                    market_day=market_day,
                    outer_payload=raw,
                )
                for report in reports
                if isinstance(report, Mapping)
            )
    source_at = _utc(row.get("source_timestamp_ms"))
    if product == "multi_model_deterministic_forecast":
        from poly_weather.weather_provenance import forecast_vintage

        _, vintage_reason = forecast_vintage(row)
        models = raw.get("models") if isinstance(raw, Mapping) else None
        model_rows = models.items() if isinstance(models, Mapping) else (("blend", raw),)
        output: list[InformationEvent] = []
        for model_name, model_payload in model_rows:
            init = _forecast_initialisation(model_payload)
            digest = payload_hash({"model": model_name, "payload": model_payload})
            output.append(
                InformationEvent(
                    event_id=f"open_meteo:{station or 'unknown'}:{model_name}:{init or 'unknown'}:{digest[:16]}",
                    source=f"{source}:{model_name}",
                    kind="model_run",
                    source_at=init,
                    available_at=available_at,
                    station_id=station,
                    market_day=market_day,
                    payload_hash=digest,
                    predictable=False,
                    receipt_verified=vintage_reason == "VERIFIED_EXPLICIT_FORECAST_VINTAGE",
                    metadata={
                        "vintage_qualification": vintage_reason,
                        "model": str(model_name),
                        "missing_run_initialization": init is None,
                        "forecast_distribution_hash": digest,
                    },
                )
            )
        return tuple(output)
    digest = payload_hash(
        {
            "station_id": station,
            "product": product,
            "source_at": source_at,
            "temperature_c": row.get("temperature_c"),
            "temperature_f": raw.get("temperature_f")
            if isinstance(raw, Mapping)
            else None,
            "raw": raw,
        }
    )
    kind = {
        "wrh_timeseries_observation": "wrh_observation",
        "latest_observation": "nws_observation",
        "taf": "taf",
    }.get(product, product or "weather")
    risk_fields = _weather_risk_fields(raw)
    return (
        InformationEvent(
            event_id=f"{source}:{kind}:{station or 'unknown'}:{source_at or 'unknown'}:{digest[:16]}",
            source=source,
            kind=kind,
            source_at=source_at,
            available_at=available_at,
            station_id=station,
            market_day=market_day,
            payload_hash=digest,
            metadata={
                "temperature_f": risk_fields.get("temperature_f"),
                "product": product,
                "risk_fields": risk_fields,
                "payload": _stable(raw),
            },
        ),
    )


def _archive_events(
    row: Mapping[str, Any], *, market_day: str | None
) -> tuple[InformationEvent, ...]:
    source = str(row.get("source") or "archive")
    available_at = _utc(row.get("fetched_at") or row.get("available_at"))
    payload = row.get("payload")
    station, market_day = _payload_context(
        payload,
        station_id=str(row.get("station_id") or "").upper() or None,
        market_day=market_day,
    )
    if source == "noaa_aviation_metar":
        reports = payload if isinstance(payload, list) else ()
        return tuple(
            _report_event(
                report,
                source="NOAA Aviation Weather Center",
                available_at=available_at,
                station_id=station,
                market_day=market_day,
                outer_payload=payload,
            )
            for report in reports
            if isinstance(report, Mapping)
        )
    if source == "noaa_aviation_taf":
        reports = payload if isinstance(payload, list) else ()
        output: list[InformationEvent] = []
        for report in reports:
            if not isinstance(report, Mapping):
                continue
            source_at = _utc(report.get("issueTime"))
            station_value = str(report.get("icaoId") or station or "").upper() or None
            digest = payload_hash(report)
            output.append(
                InformationEvent(
                    event_id=f"NOAA Aviation Weather Center:taf:{station_value or 'unknown'}:{source_at or 'unknown'}:{digest[:16]}",
                    source="NOAA Aviation Weather Center",
                    kind="taf",
                    source_at=source_at,
                    available_at=available_at,
                    station_id=station_value,
                    market_day=market_day,
                    payload_hash=digest,
                    metadata={
                        "risk_fields": _weather_risk_fields(report),
                        "payload": _stable(report),
                    },
                )
            )
        return tuple(output)
    if source in {
        "open_meteo_gefs",
        "open_meteo_deterministic",
        "open_meteo_previous_runs",
    }:
        init = _forecast_initialisation(payload)
        digest = payload_hash(payload)
        return (
            InformationEvent(
                event_id=f"{source}:{station or 'unknown'}:{init or 'unknown'}:{digest[:16]}",
                source=source,
                kind="model_run",
                source_at=init,
                available_at=available_at,
                station_id=station,
                market_day=market_day,
                payload_hash=digest,
                metadata={
                    "missing_run_initialization": init is None,
                    "forecast_distribution_hash": digest,
                    "payload": _stable(payload),
                },
            ),
        )
    if source == "nws_latest_observation":
        properties = payload.get("properties") if isinstance(payload, Mapping) else {}
        properties = properties if isinstance(properties, Mapping) else {}
        source_at = _utc(properties.get("timestamp"))
        station_value = str(
            properties.get("stationIdentifier") or station or ""
        ).upper() or None
        digest = payload_hash(payload)
        risk_fields = _weather_risk_fields(properties)
        return (
            InformationEvent(
                event_id=f"NOAA/NWS:nws_observation:{station_value or 'unknown'}:{source_at or 'unknown'}:{digest[:16]}",
                source="NOAA/NWS",
                kind="nws_observation",
                source_at=source_at,
                available_at=available_at,
                station_id=station_value,
                market_day=market_day,
                payload_hash=digest,
            metadata={
                    "temperature_f": risk_fields.get("temperature_f"),
                    "risk_fields": risk_fields,
                    "payload": _stable(payload),
                },
            ),
        )
    # Status, settlement and supervisor records use their observed receipt as
    # source time.  This is a local observation of an external state change,
    # not an invented remote timestamp.
    if source in {
        "settlement_evidence",
        "polymarket_gamma_event",
        "polymarket_status",
        "polymarket_supervisor",
    }:
        digest = payload_hash(payload)
        observed_at = available_at
        kind = (
            "settlement_rule"
            if source in {"settlement_evidence", "polymarket_gamma_event"}
            else "market_health_status"
        )
        return (
            InformationEvent(
                event_id=f"{source}:{digest}",
                source=source,
                kind=kind,
                source_at=observed_at,
                available_at=available_at,
                station_id=station,
                market_day=market_day,
                payload_hash=digest,
                metadata={
                    "state_hash": _semantic_hash(payload),
                    "payload": _stable(payload),
                },
            ),
        )
    return ()


@dataclass(frozen=True, slots=True)
class InformationEvent:
    """One external information change with two independent time gates."""

    event_id: str
    source: str
    kind: str
    source_at: datetime | None
    available_at: datetime | None
    station_id: str | None = None
    market_day: str | None = None
    payload_hash: str = ""
    changed_known_high: bool | None = None
    changed_physical_margin: bool | None = None
    changed_forecast_distribution: bool | None = None
    impact_class: ImpactClass | str | None = None
    impact_reason: str | None = None
    predictable: bool = False
    receipt_verified: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_at", _utc(self.source_at))
        object.__setattr__(self, "available_at", _utc(self.available_at))
        object.__setattr__(self, "station_id", self.station_id.upper() if self.station_id else None)
        if self.impact_class is not None:
            object.__setattr__(self, "impact_class", ImpactClass(self.impact_class))
        if not self.payload_hash:
            object.__setattr__(self, "payload_hash", payload_hash(self.metadata))
        if self.source_at is not None and self.available_at is not None:
            if self.source_at > self.available_at:
                object.__setattr__(self, "receipt_verified", False)
        if self.available_at is None:
            object.__setattr__(self, "receipt_verified", False)

    @property
    def scope(self) -> tuple[str, str]:
        return (
            self.station_id or "unknown",
            self.market_day or (self.source_at.date().isoformat() if self.source_at else "unknown"),
        )

    @property
    def phase_eligible(self) -> bool:
        return bool(
            self.receipt_verified
            and self.source_at is not None
            and self.available_at is not None
        )

    def available_by(self, decision_at: datetime) -> bool:
        point = _utc(decision_at)
        return bool(
            point is not None
            and self.phase_eligible
            and self.source_at <= point
            and self.available_at <= point
        )

    def as_dict(self, *, include_metadata: bool = True) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "kind": self.kind,
            "source_at": self.source_at.isoformat() if self.source_at else None,
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "station_id": self.station_id,
            "market_day": self.market_day,
            "scope": {"station_id": self.scope[0], "market_day": self.scope[1]},
            "payload_hash": self.payload_hash,
            "changed_known_high": self.changed_known_high,
            "changed_physical_margin": self.changed_physical_margin,
            "changed_forecast_distribution": self.changed_forecast_distribution,
            "impact_class": self.impact_class.value if self.impact_class else None,
            "impact_reason": self.impact_reason,
            "predictable": self.predictable,
            "receipt_verified": self.receipt_verified,
            "phase_eligible": self.phase_eligible,
            "metadata": _jsonable(self.metadata) if include_metadata else {},
        }


def _event_from_row(row: Mapping[str, Any]) -> InformationEvent | None:
    source_at = _utc(
        row.get("source_at")
        or row.get("source_timestamp")
        or row.get("source_timestamp_ms")
        or row.get("timestamp")
    )
    available_at = _utc(
        row.get("available_at")
        or row.get("received_at")
        or row.get("received_at_ns")
        or row.get("fetched_at")
    )
    source = str(row.get("source") or "external")
    kind = str(row.get("kind") or row.get("product") or "external")
    digest = str(row.get("payload_hash") or payload_hash(row.get("payload") or row))
    event_id = str(row.get("event_id") or f"{source}:{kind}:{digest}")
    return InformationEvent(
        event_id=event_id,
        source=source,
        kind=kind,
        source_at=source_at,
        available_at=available_at,
        station_id=str(row.get("station_id") or "").upper() or None,
        market_day=str(row.get("market_day") or "") or None,
        payload_hash=digest,
        changed_known_high=_bool_or_none(row.get("changed_known_high")),
        changed_physical_margin=_bool_or_none(row.get("changed_physical_margin")),
        changed_forecast_distribution=_bool_or_none(
            row.get("changed_forecast_distribution")
        ),
        impact_class=(
            str(row["impact_class"]) if row.get("impact_class") is not None else None
        ),
        impact_reason=(str(row["impact_reason"]) if row.get("impact_reason") else None),
        predictable=bool(row.get("predictable", False)),
        receipt_verified=bool(row.get("receipt_verified", True)),
        metadata=row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {},
    )


def iter_information_events(
    rows: Iterable[Mapping[str, Any]],
    *,
    market_day_by_station: Mapping[str, str] | None = None,
) -> Iterator[InformationEvent]:
    """Normalize mixed realtime and RawEventArchive rows into information events."""
    market_day_by_station = market_day_by_station or {}
    for row in rows:
        station = str(row.get("station_id") or "").upper()
        market_day = str(
            row.get("market_day") or market_day_by_station.get(station) or ""
        ) or None
        if row.get("product") is not None:
            events = _weather_daemon_events(row, market_day=market_day)
        elif row.get("source") is not None and row.get("fetched_at") is not None:
            events = _archive_events(row, market_day=market_day)
        else:
            event = _event_from_row(row)
            events = (event,) if event is not None else ()
        for event in events:
            if event.station_id and event.market_day is None:
                derived_day = market_day_by_station.get(event.station_id)
                if derived_day:
                    event = replace(event, market_day=derived_day)
            yield event


def _dedupe_key(event: InformationEvent) -> tuple[Any, ...]:
    return (*event.scope, event.source, event.kind, event.payload_hash)


def deduplicate_information_events(
    events: Iterable[InformationEvent],
) -> tuple[InformationEvent, ...]:
    """Drop repeated payloads while retaining same-time revisions."""
    output: list[InformationEvent] = []
    seen: set[tuple[Any, ...]] = set()
    for event in sorted(
        events,
        key=lambda item: (
            item.available_at or UTC_MAX,
            item.source_at or UTC_MAX,
            item.event_id,
        ),
    ):
        key = _dedupe_key(event)
        if key in seen:
            continue
        seen.add(key)
        output.append(event)
    return tuple(output)


def strict_information_cutoff(
    events: Iterable[InformationEvent], decision_at: datetime
) -> tuple[InformationEvent, ...]:
    """Apply both source-time and receipt-time no-lookahead gates."""
    point = _utc(decision_at)
    if point is None:
        return ()
    return tuple(event for event in events if event.available_by(point))


@dataclass(slots=True)
class InformationClock:
    """Incremental receipt-gated clock with present-time impact semantics."""

    events: list[InformationEvent] = field(default_factory=list)
    invalid_events: list[dict[str, Any]] = field(default_factory=list)
    _seen_payloads: set[tuple[Any, ...]] = field(default_factory=set, init=False, repr=False)
    _known_high: dict[tuple[str, str], Decimal] = field(default_factory=dict, init=False, repr=False)
    _latest_temperature: dict[tuple[str, str], Decimal] = field(default_factory=dict, init=False, repr=False)
    _physical_margin: dict[tuple[tuple[str, str], str], tuple[str, str, str]] = field(default_factory=dict, init=False, repr=False)
    _forecast_hash: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _settlement_hash: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _status_hash: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _weather_fields: dict[tuple[tuple[str, str], str, str], dict[str, str]] = field(
        default_factory=dict, init=False, repr=False
    )

    def _reject(self, event: InformationEvent, reason: str) -> None:
        self.invalid_events.append(
            {
                "event_id": event.event_id,
                "reason": reason,
                "impact_class": ImpactClass.INVALID.value,
                "event": replace(
                    event,
                    impact_class=ImpactClass.INVALID,
                    impact_reason=reason,
                ).as_dict(include_metadata=False),
            }
        )

    @staticmethod
    def _physical_key(event: InformationEvent, metadata: Mapping[str, Any]) -> str:
        return str(
            metadata.get("token_id")
            or metadata.get("market_id")
            or metadata.get("market_slug")
            or "scope"
        )

    def _classify(
        self,
        event: InformationEvent,
        *,
        changed_high: bool | None,
        changed_margin: bool | None,
        changed_forecast: bool | None,
        risk_fields: Mapping[str, str],
    ) -> tuple[ImpactClass, str]:
        """Classify using only state visible at this receipt, never markout."""
        if event.impact_class is not None:
            return ImpactClass(event.impact_class), event.impact_reason or "declared_impact_class"
        scope = event.scope
        metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
        kind = event.kind.casefold()
        if changed_margin:
            return ImpactClass.HARD_RESET, "physical_margin_tier_or_elimination_changed"
        if changed_forecast:
            return ImpactClass.HARD_RESET, "forecast_distribution_changed"
        if kind == "model_run":
            return ImpactClass.NO_OP, "forecast_distribution_unchanged"
        if kind == "settlement_rule":
            version = str(
                metadata.get("settlement_rule_hash")
                or metadata.get("state_hash")
                or event.payload_hash
            )
            prior = self._settlement_hash.get(scope)
            self._settlement_hash[scope] = version
            if prior is not None and prior != version:
                return ImpactClass.HARD_RESET, "settlement_rule_hash_changed"
            return ImpactClass.NO_OP, "settlement_rule_initial_or_unchanged"
        if kind in {"official_status", "market_health_status"}:
            version = str(metadata.get("state_hash") or event.payload_hash)
            prior = self._status_hash.get(scope)
            self._status_hash[scope] = version
            if prior is not None and prior != version:
                return ImpactClass.HARD_RESET, "official_market_status_changed"
            return ImpactClass.NO_OP, "official_market_status_initial_or_unchanged"
        if kind == "taf":
            signature_key = (scope, event.source, kind)
            prior = self._weather_fields.get(signature_key)
            self._weather_fields[signature_key] = dict(risk_fields)
            if prior is not None and prior != dict(risk_fields):
                return ImpactClass.SOFT_UPDATE, "taf_visible_fields_changed"
            return ImpactClass.NO_OP, "taf_initial_or_unchanged"
        if kind in {"metar", "speci", "wrh_observation", "nws_observation", "weather"}:
            signature_key = (scope, event.source, kind)
            prior = self._weather_fields.get(signature_key)
            current = dict(risk_fields)
            self._weather_fields[signature_key] = current
            if changed_high:
                return ImpactClass.HARD_RESET, "observed_daily_high_increased"
            if kind == "speci" and prior is not None:
                critical = ("temperature_f", "wind_direction", "wind_speed", "cloud", "weather")
                if any(prior.get(key) != current.get(key) for key in critical):
                    return ImpactClass.HARD_RESET, "speci_critical_weather_field_changed"
            if _weather_regime_changed(prior, current):
                return ImpactClass.HARD_RESET, "key_weather_regime_changed"
            changed_keys = _material_weather_changed_keys(prior, current)
            if changed_keys and set(changed_keys) <= {"temperature_f"}:
                return ImpactClass.NO_OP, "temperature_not_new_daily_high"
            if changed_keys:
                return ImpactClass.SOFT_UPDATE, "material_weather_risk_regime_changed"
            return ImpactClass.NO_OP, "weather_fields_initial_or_unchanged"
        if changed_high:
            return ImpactClass.HARD_RESET, "observed_daily_high_increased"
        if risk_fields:
            signature_key = (scope, event.source, kind)
            prior = self._weather_fields.get(signature_key)
            self._weather_fields[signature_key] = dict(risk_fields)
            if prior is not None and prior != dict(risk_fields):
                return ImpactClass.SOFT_UPDATE, "visible_external_fields_changed"
        return ImpactClass.NO_OP, "no_visible_state_change"

    def ingest(
        self,
        event: InformationEvent,
        *,
        decision_at: datetime | None = None,
    ) -> InformationEvent | None:
        if not event.phase_eligible:
            self._reject(event, "missing_or_inconsistent_source_receipt_time")
            return None
        if event.impact_class is ImpactClass.INVALID:
            self._reject(event, event.impact_reason or "declared_invalid_event")
            return None
        if event.station_id is None and event.kind not in _GLOBAL_SCOPE_KINDS:
            self._reject(event, "information_scope_unknown")
            return None
        if decision_at is not None and not event.available_by(decision_at):
            return None
        key = _dedupe_key(event)
        if key in self._seen_payloads:
            return None
        scope = event.scope
        metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
        changed_high = event.changed_known_high
        risk_payload = metadata.get("risk_fields")
        risk_fields = (
            {str(key): str(value) for key, value in risk_payload.items() if value is not None}
            if isinstance(risk_payload, Mapping)
            else _weather_risk_fields(metadata.get("payload"))
            if metadata.get("payload") is not None
            else {}
        )
        temperature = _temperature_f(metadata)
        if temperature is None:
            raw_temperature = risk_fields.get("temperature_f")
            try:
                temperature = Decimal(raw_temperature) if raw_temperature is not None else None
            except (ArithmeticError, TypeError, ValueError):
                temperature = None
        if changed_high is None and temperature is not None:
            prior = self._known_high.get(scope)
            changed_high = prior is None or temperature > prior
            self._known_high[scope] = max(prior or temperature, temperature)
        elif temperature is not None:
            self._known_high[scope] = max(self._known_high.get(scope, temperature), temperature)
        if temperature is not None:
            self._latest_temperature[scope] = temperature
        changed_margin = event.changed_physical_margin
        margin = metadata.get("physical_margin_f")
        margin_tier = metadata.get("physical_margin_tier")
        eliminated = metadata.get("physical_eliminated")
        if changed_margin is None and any(value is not None for value in (margin, margin_tier, eliminated)):
            physical_key = (scope, self._physical_key(event, metadata))
            value = (str(margin), str(margin_tier), str(eliminated))
            prior_margin = self._physical_margin.get(physical_key)
            changed_margin = prior_margin is not None and prior_margin != value
            self._physical_margin[physical_key] = value
        changed_forecast = event.changed_forecast_distribution
        forecast_hash = str(metadata.get("forecast_distribution_hash") or "")
        if changed_forecast is None and forecast_hash:
            prior_hash = self._forecast_hash.get(scope)
            changed_forecast = prior_hash is None or prior_hash != forecast_hash
            self._forecast_hash[scope] = forecast_hash
        impact_class, impact_reason = self._classify(
            event,
            changed_high=changed_high,
            changed_margin=changed_margin,
            changed_forecast=changed_forecast,
            risk_fields=risk_fields,
        )
        accepted = replace(
            event,
            changed_known_high=changed_high,
            changed_physical_margin=changed_margin,
            changed_forecast_distribution=changed_forecast,
            impact_class=impact_class,
            impact_reason=impact_reason,
        )
        self._seen_payloads.add(key)
        self.events.append(accepted)
        return accepted

    def ingest_many(
        self,
        events: Iterable[InformationEvent],
        *,
        decision_at: datetime | None = None,
    ) -> tuple[InformationEvent, ...]:
        accepted: list[InformationEvent] = []
        for event in deduplicate_information_events(events):
            value = self.ingest(event, decision_at=decision_at)
            if value is not None:
                accepted.append(value)
        return tuple(accepted)

    def new_events_for(
        self, scope: tuple[str, str], decision_at: datetime
    ) -> tuple[InformationEvent, ...]:
        point = _utc(decision_at)
        if point is None:
            return ()
        return tuple(
            event
            for event in self.events
            if event.scope == scope and event.available_by(point)
        )

    def as_dict(self, *, include_metadata: bool = False) -> dict[str, Any]:
        by_kind: defaultdict[str, int] = defaultdict(int)
        by_impact: defaultdict[str, int] = defaultdict(int)
        by_kind_impact: defaultdict[str, defaultdict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        unusable = 0
        for event in self.events:
            by_kind[event.kind] += 1
            impact = event.impact_class.value if event.impact_class else "UNCLASSIFIED"
            by_impact[impact] += 1
            by_kind_impact[event.kind][impact] += 1
            if not event.phase_eligible:
                unusable += 1
        state_by_scope: dict[str, dict[str, str | None]] = {}
        for scope in sorted(set(self._known_high) | set(self._latest_temperature)):
            high = self._known_high.get(scope)
            latest = self._latest_temperature.get(scope)
            state_by_scope[f"{scope[0]}:{scope[1]}"] = {
                "running_high_f": str(high) if high is not None else None,
                "latest_temperature_f": str(latest) if latest is not None else None,
                "latest_minus_running_high_f": (
                    str(latest - high)
                    if latest is not None and high is not None
                    else None
                ),
                "forecast_distribution_hash": self._forecast_hash.get(scope),
                "settlement_rule_hash": self._settlement_hash.get(scope),
                "official_status_hash": self._status_hash.get(scope),
            }
        return {
            "event_count": len(self.events),
            "invalid_event_count": len(self.invalid_events),
            "unusable_event_count": unusable,
            "events_by_kind": dict(sorted(by_kind.items())),
            "events_by_impact_class": dict(sorted(by_impact.items())),
            "events_by_kind_and_impact_class": {
                kind: dict(sorted(counts.items()))
                for kind, counts in sorted(by_kind_impact.items())
            },
            "station_day_state": state_by_scope,
            "invalid_events": list(self.invalid_events),
            "events": [
                event.as_dict(include_metadata=include_metadata) for event in self.events
            ],
        }


def load_external_information_events(
    data_dir: Path | str,
    *,
    market_day_by_station: Mapping[str, str] | None = None,
    include_sources: Sequence[str] | None = None,
    include_payload: bool = False,
) -> tuple[InformationEvent, ...]:
    """Load all available external-information archives without using future data."""
    root = Path(data_dir)
    source_names = tuple(
        include_sources
        or (
            "weather_daemon",
            "noaa_aviation_metar",
            "noaa_aviation_taf",
            "nws_latest_observation",
            "open_meteo_gefs",
            "open_meteo_deterministic",
            "open_meteo_previous_runs",
            "settlement_evidence",
            "polymarket_gamma_event",
            "polymarket_status",
            "polymarket_supervisor",
        )
    )
    rows: list[Mapping[str, Any]] = []
    for source in source_names:
        for path in jsonl_archive_paths(root / "raw" / source):
            if not path.exists():
                continue
            with open_jsonl_text(path) as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, Mapping):
                        rows.append(value)
    events = list(
        iter_information_events(rows, market_day_by_station=market_day_by_station)
    )
    status_path = root / "runtime" / "polymarket_status_history.jsonl"
    if status_path.exists():
        with status_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, Mapping):
                    continue
                checked_at = _utc(value.get("checked_at"))
                digest = payload_hash(
                    {
                        "page_status": value.get("page_status"),
                        "active_market_data_windows": value.get("active_market_data_windows"),
                    }
                )
                events.append(
                    InformationEvent(
                        event_id=f"polymarket_status:{digest}",
                        source="polymarket_status",
                        kind="official_status",
                        source_at=checked_at,
                        available_at=checked_at,
                        payload_hash=digest,
                        metadata={
                            "state_hash": digest,
                            "payload": _stable(value),
                        },
                    )
                )
    deduplicated = deduplicate_information_events(events)
    if include_payload:
        return deduplicated
    return tuple(
        replace(
            event,
            metadata={
                key: value
                for key, value in event.metadata.items()
                if str(key).casefold() != "payload"
            },
        )
        for event in deduplicated
    )


__all__ = [
    "ImpactClass",
    "InformationClock",
    "InformationEvent",
    "deduplicate_information_events",
    "iter_information_events",
    "load_external_information_events",
    "payload_hash",
    "strict_information_cutoff",
]
