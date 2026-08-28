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
            "temperature_f": str(_temperature_f(report))
            if _temperature_f(report) is not None
            else None,
            "report_type": report.get("metarType") or report.get("reportType"),
            "provider_receipt_at": report.get("receiptTime"),
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
    available_at = _utc(row.get("received_at") or row.get("received_at_ns"))
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
                    metadata={
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
                "temperature_f": str(_temperature_f(raw)) if isinstance(raw, Mapping) else None,
                "product": product,
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
                    metadata={"payload": _stable(report)},
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
                    "temperature_f": str(_temperature_f(properties.get("temperature") or {})),
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
                metadata={"payload": _stable(payload)},
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
    predictable: bool = False
    receipt_verified: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_at", _utc(self.source_at))
        object.__setattr__(self, "available_at", _utc(self.available_at))
        object.__setattr__(self, "station_id", self.station_id.upper() if self.station_id else None)
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
        if row.get("product") is not None and row.get("received_at") is not None:
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
    """Incremental dedupe and change-flag state for one replay/live follower."""

    events: list[InformationEvent] = field(default_factory=list)
    invalid_events: list[dict[str, Any]] = field(default_factory=list)
    _seen_payloads: set[tuple[Any, ...]] = field(default_factory=set, init=False, repr=False)
    _known_high: dict[tuple[str, str], Decimal] = field(default_factory=dict, init=False, repr=False)
    _physical_margin: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)
    _forecast_hash: dict[tuple[str, str], str] = field(default_factory=dict, init=False, repr=False)

    def ingest(
        self,
        event: InformationEvent,
        *,
        decision_at: datetime | None = None,
    ) -> InformationEvent | None:
        if not event.phase_eligible:
            self.invalid_events.append(
                {
                    "event_id": event.event_id,
                    "reason": "missing_or_inconsistent_source_receipt_time",
                    "event": event.as_dict(include_metadata=False),
                }
            )
            return None
        if decision_at is not None and not event.available_by(decision_at):
            return None
        key = _dedupe_key(event)
        if key in self._seen_payloads:
            return None
        scope = event.scope
        metadata = event.metadata if isinstance(event.metadata, Mapping) else {}
        changed_high = event.changed_known_high
        temperature = _temperature_f(metadata)
        if changed_high is None and temperature is not None:
            prior = self._known_high.get(scope)
            changed_high = prior is None or temperature > prior
            self._known_high[scope] = max(prior or temperature, temperature)
        elif temperature is not None:
            self._known_high[scope] = max(self._known_high.get(scope, temperature), temperature)
        changed_margin = event.changed_physical_margin
        margin = metadata.get("physical_margin_f")
        if changed_margin is None and margin is not None:
            margin_value = str(margin)
            prior_margin = self._physical_margin.get(scope)
            changed_margin = prior_margin != margin_value
            self._physical_margin[scope] = margin_value
        changed_forecast = event.changed_forecast_distribution
        forecast_hash = str(metadata.get("forecast_distribution_hash") or "")
        if changed_forecast is None and forecast_hash:
            prior_hash = self._forecast_hash.get(scope)
            changed_forecast = prior_hash != forecast_hash
            self._forecast_hash[scope] = forecast_hash
        accepted = replace(
            event,
            changed_known_high=changed_high,
            changed_physical_margin=changed_margin,
            changed_forecast_distribution=changed_forecast,
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
        unusable = 0
        for event in self.events:
            by_kind[event.kind] += 1
            if not event.phase_eligible:
                unusable += 1
        return {
            "event_count": len(self.events),
            "invalid_event_count": len(self.invalid_events),
            "unusable_event_count": unusable,
            "events_by_kind": dict(sorted(by_kind.items())),
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
                        metadata={"payload": _stable(value)},
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
    "InformationClock",
    "InformationEvent",
    "deduplicate_information_events",
    "iter_information_events",
    "load_external_information_events",
    "payload_hash",
    "strict_information_cutoff",
]
