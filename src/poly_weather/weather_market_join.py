"""Strict real-time weather-to-market timestamp alignment.

The join has two independent availability gates: an observation's source time
and the time our daemon received it must both be no later than the market
snapshot.  Historical backfills are intentionally ignored.  Missing metadata
is reported, never filled with a forecast or a later observation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.archive_io import open_jsonl_text
from poly_weather.shadow_orders import BookSnapshot


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _time(value: Any, *, milliseconds: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        number = float(value)
        if milliseconds or number > 10**11:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            return _utc(str(value))
        except (TypeError, ValueError):
            return None


def _temperature_f(value: Any, raw: Any = None, temperature_c: Any = None) -> Decimal | None:
    if value is not None:
        try:
            return Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            pass
    candidates: list[Any] = []
    if temperature_c is not None:
        candidates.append(("c", temperature_c))
    if isinstance(raw, Mapping):
        for key in ("temperature_f", "temp_f", "temperatureF"):
            if raw.get(key) is not None:
                candidates.append(("f", raw[key]))
        for key in ("temperature_c", "temp", "temperature"):
            if raw.get(key) is not None:
                candidates.append(("c", raw[key]))
        properties = raw.get("properties")
        if isinstance(properties, Mapping):
            nested = properties.get("temperature")
            if isinstance(nested, Mapping) and nested.get("value") is not None:
                candidates.append(("c", nested["value"]))
    elif isinstance(raw, list):
        # AWC METAR responses are commonly archived as a list of reports.
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            for key in ("temperature_f", "temp_f", "temperatureF"):
                if item.get(key) is not None:
                    candidates.append(("f", item[key]))
            for key in ("temperature_c", "temp", "temperature"):
                if item.get(key) is not None:
                    candidates.append(("c", item[key]))
            if candidates:
                break
    for unit, candidate in candidates:
        try:
            value = Decimal(str(candidate))
            return value if unit == "f" else value * Decimal("9") / Decimal("5") + Decimal("32")
        except (ArithmeticError, TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True, slots=True)
class WeatherObservation:
    station_id: str
    product: str
    source_timestamp: datetime
    received_at: datetime
    temperature_f: Decimal | None
    observation_id: str
    collection_mode: str = "realtime"

    def as_json(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "product": self.product,
            "source_timestamp": self.source_timestamp.isoformat(),
            "received_at": self.received_at.isoformat(),
            "temperature_f": str(self.temperature_f) if self.temperature_f is not None else None,
            "observation_id": self.observation_id,
            "collection_mode": self.collection_mode,
        }


def _raw_observation_identity(row: Mapping[str, Any]) -> str:
    raw = row.get("raw")
    if isinstance(raw, Mapping):
        payload = raw.get("source_payload") or raw
        if isinstance(payload, Mapping):
            for key in ("id", "@id", "timestamp", "obsTime", "reportTime"):
                if payload.get(key) is not None:
                    return str(payload[key])
    return ":".join(
        (
            str(row.get("station_id") or ""),
            str(row.get("product") or ""),
            str(row.get("source_timestamp_ms") or ""),
        )
    )


def parse_weather_observation(row: Mapping[str, Any]) -> WeatherObservation:
    product = str(row.get("product") or "")
    if product not in {"wrh_timeseries_observation", "latest_observation", "metar"}:
        raise ValueError("row is not a realtime observation")
    if str(row.get("collection_mode") or "realtime") != "realtime":
        raise ValueError("historical collection is not valid for no-lookahead join")
    station = str(row.get("station_id") or "").upper()
    source = _time(row.get("source_timestamp_ms"), milliseconds=True)
    received = _time(row.get("received_at") or row.get("received_at_ns"))
    if received is None and row.get("received_at_ns") is not None:
        received = _time(float(row["received_at_ns"]) / 1_000_000_000)
    if not station or source is None or received is None:
        raise ValueError("observation lacks station/source/receipt timestamp")
    raw = row.get("raw")
    temperature = _temperature_f(
        row.get("temperature_f"), raw, row.get("temperature_c")
    )
    return WeatherObservation(
        station_id=station,
        product=product,
        source_timestamp=source,
        received_at=received,
        temperature_f=temperature,
        observation_id=_raw_observation_identity(row),
        collection_mode="realtime",
    )


def load_realtime_weather_observations(paths: Iterable[Path]) -> tuple[WeatherObservation, ...]:
    """Read normalized weather-daemon rows and deduplicate source updates."""
    selected: dict[tuple[str, str, datetime, str], WeatherObservation] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            handle = open_jsonl_text(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if not isinstance(row, Mapping):
                        continue
                    observation = parse_weather_observation(row)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                key = (
                    observation.station_id,
                    observation.product,
                    observation.source_timestamp,
                    observation.observation_id,
                )
                selected.setdefault(key, observation)
    return tuple(
        sorted(
            selected.values(),
            key=lambda row: (row.station_id, row.source_timestamp, row.received_at, row.product),
        )
    )


def _snapshot_values(snapshot: BookSnapshot | Mapping[str, Any]) -> tuple[datetime, str, str, str, Decimal | None, Decimal | None, Decimal]:
    if isinstance(snapshot, BookSnapshot):
        return (
            snapshot.timestamp,
            snapshot.station_id or "",
            snapshot.event_id,
            snapshot.token_id,
            snapshot.best_bid,
            snapshot.best_ask,
            snapshot.tick_size,
        )
    no = snapshot.get("no") if isinstance(snapshot.get("no"), Mapping) else snapshot
    timestamp = _utc(str(snapshot.get("observed_at") or no.get("_timestamp") or snapshot["timestamp"]))
    station = str(snapshot.get("station_id") or "").upper()
    event_id = str(snapshot.get("event_id") or snapshot.get("event_slug") or "")
    token = str(no.get("asset_id") or snapshot.get("token_id") or "")
    def best(rows: Any, *, high: bool) -> Decimal | None:
        values: list[Decimal] = []
        for row in rows or ():
            if not isinstance(row, Mapping):
                continue
            try:
                values.append(Decimal(str(row["price"])))
            except (ArithmeticError, KeyError, TypeError, ValueError):
                continue
        return (max(values) if high else min(values)) if values else None
    bids = no.get("bids") if isinstance(no, Mapping) else ()
    asks = no.get("asks") if isinstance(no, Mapping) else ()
    return timestamp, station, event_id, token, best(bids, high=True), best(asks, high=False), Decimal(str(no.get("tick_size") or "0.01"))


def align_weather_to_snapshots(
    snapshots: Sequence[BookSnapshot | Mapping[str, Any]],
    observations: Sequence[WeatherObservation],
    *,
    market_lag_tolerance_ticks: int = 1,
    state: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Attach only observations available by each snapshot's receipt time.

    ``state`` is optional mutable join state for a continuous follower.  When
    supplied, observation identities and prior temperatures/asks survive the
    next polling cycle, so the same weather observation cannot retrigger a
    lag signal merely because the market archive was appended in batches.
    """
    by_station: dict[str, list[WeatherObservation]] = defaultdict(list)
    for observation in observations:
        by_station[observation.station_id].append(observation)
    for rows in by_station.values():
        rows.sort(key=lambda row: (row.source_timestamp, row.received_at, row.observation_id))
    previous_observation: dict[tuple[str, str], str] = (
        state.setdefault("previous_observation", {}) if state is not None else {}
    )
    previous_temperature: dict[tuple[str, str], Decimal] = (
        state.setdefault("previous_temperature", {}) if state is not None else {}
    )
    previous_ask: dict[tuple[str, str], Decimal] = (
        state.setdefault("previous_ask", {}) if state is not None else {}
    )
    output: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for original in sorted(snapshots, key=lambda value: _snapshot_values(value)[0]):
        timestamp, station, event_id, token, _bid, ask, tick = _snapshot_values(original)
        eligible = [
            row
            for row in by_station.get(station, ())
            if row.source_timestamp <= timestamp and row.received_at <= timestamp
        ]
        enriched = dict(original) if isinstance(original, Mapping) else {
            "observed_at": timestamp.isoformat(),
            "event_id": event_id,
            "station_id": station,
            "token_id": token,
        }
        metadata = dict(enriched.get("metadata") or {})
        if not eligible:
            metadata.update(
                {
                    "weather_join_status": "unavailable",
                    "weather_join_reason": "no_source_and_receipt_eligible_observation",
                    "weather_market_lag": False,
                }
            )
            reasons["no_source_and_receipt_eligible_observation"] += 1
            enriched["metadata"] = metadata
            output.append(enriched)
            continue
        observation = max(eligible, key=lambda row: (row.source_timestamp, row.received_at, row.observation_id))
        identity_key = (station, observation.product)
        prior_id = previous_observation.get(identity_key)
        is_new = prior_id != observation.observation_id
        prior_temperature = previous_temperature.get(identity_key)
        improving = (
            is_new
            and observation.temperature_f is not None
            and prior_temperature is not None
            and observation.temperature_f > prior_temperature
        )
        worsening = (
            is_new
            and observation.temperature_f is not None
            and prior_temperature is not None
            and observation.temperature_f < prior_temperature
        )
        prior_ask = previous_ask.get((event_id, token))
        lag = bool(
            is_new
            and improving
            and ask is not None
            and (prior_ask is None or abs(ask - prior_ask) <= tick * market_lag_tolerance_ticks)
        )
        metadata.update(
            {
                "weather_join_status": "aligned",
                "weather_observation_id": observation.observation_id,
                "weather_source_timestamp": observation.source_timestamp.isoformat(),
                "weather_received_at": observation.received_at.isoformat(),
                "weather_observation_new": is_new,
                "weather_improving": improving,
                "weather_worsening": worsening,
                "weather_unchanged": is_new and not improving and not worsening,
                "weather_market_lag": lag,
            }
        )
        enriched["metadata"] = metadata
        enriched["weather_join"] = {
            "status": "aligned",
            "observation": observation.as_json(),
            "new_observation": is_new,
            "market_lag": lag,
        }
        output.append(enriched)
        previous_observation[identity_key] = observation.observation_id
        if observation.temperature_f is not None:
            previous_temperature[identity_key] = observation.temperature_f
        if ask is not None:
            previous_ask[(event_id, token)] = ask
        reasons["aligned_new_observation" if is_new else "aligned_repeated_observation"] += 1
    return output, dict(reasons)


__all__ = [
    "WeatherObservation",
    "align_weather_to_snapshots",
    "load_realtime_weather_observations",
    "parse_weather_observation",
]
