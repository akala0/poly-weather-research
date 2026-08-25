from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from poly_weather.domain import NwsObservation, ObservedDailyHigh, TruthKind
from poly_weather.temperature import celsius_to_fahrenheit


class NwsClient:
    """Read-only adapter for the US National Weather Service API."""

    def __init__(
        self,
        *,
        user_agent: str = "poly-weather/0.1 (research contact: local-user)",
        base_url: str = "https://api.weather.gov",
        timeout: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NwsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def latest_observation(self, station_id: str) -> NwsObservation:
        normalized = station_id.strip().upper()
        response = self._client.get(f"/stations/{normalized}/observations/latest")
        response.raise_for_status()
        payload = response.json()
        properties = payload.get("properties", {})
        temperature = properties.get("temperature", {}).get("value")
        temperature_decimal = None if temperature is None else Decimal(str(temperature))
        return NwsObservation(
            station_id=normalized,
            timestamp=properties["timestamp"],
            temperature_c=temperature_decimal,
            temperature_precision_degraded=(
                temperature_decimal is not None and temperature_decimal.as_tuple().exponent >= 0
            ),
            raw=payload,
        )

    def daily_high(
        self,
        *,
        station_id: str,
        target_date: date,
        timezone: str,
        as_of: datetime | None = None,
    ) -> tuple[str, ObservedDailyHigh]:
        """Compute a no-lookahead local-day high from same-station NWS observations."""
        normalized = station_id.strip().upper()
        local_zone = ZoneInfo(timezone)
        local_start = datetime.combine(target_date, time.min, tzinfo=local_zone)
        local_end = local_start + timedelta(days=1)
        effective_as_of = (as_of or datetime.now(UTC)).astimezone(UTC)
        request_end = min(local_end.astimezone(UTC) + timedelta(hours=6), effective_as_of)
        if request_end <= local_start.astimezone(UTC):
            raise ValueError("as_of must be later than the target local-day start")

        response = self._client.get(
            f"/stations/{normalized}/observations",
            params={
                "start": local_start.astimezone(UTC).isoformat(),
                "end": request_end.isoformat(),
            },
        )
        first_request_url = str(response.request.url)
        pages: list[dict[str, object]] = []
        observations: list[tuple[datetime, Decimal]] = []
        saw_next_day_observation = False
        for _ in range(20):
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("NWS observations response is not an object")
            pages.append(payload)
            features = payload.get("features") or []
            if not isinstance(features, list):
                raise ValueError("NWS observations features field is not a list")
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                properties = feature.get("properties") or {}
                if not isinstance(properties, dict) or not properties.get("timestamp"):
                    continue
                observed_at = datetime.fromisoformat(str(properties["timestamp"]).replace("Z", "+00:00"))
                observed_at = observed_at.astimezone(UTC)
                if observed_at > effective_as_of:
                    continue
                observed_local_date = observed_at.astimezone(local_zone).date()
                if observed_local_date > target_date:
                    saw_next_day_observation = True
                    continue
                if observed_local_date != target_date:
                    continue
                measure = properties.get("temperature") or {}
                if not isinstance(measure, dict) or measure.get("value") is None:
                    continue
                value = Decimal(str(measure["value"]))
                unit_code = str(measure.get("unitCode") or "")
                if unit_code.endswith("degC"):
                    value = celsius_to_fahrenheit(value)
                elif not unit_code.endswith("degF"):
                    raise ValueError(f"unsupported NWS temperature unit: {unit_code!r}")
                observations.append((observed_at, value))
            pagination = payload.get("pagination") or {}
            next_url = pagination.get("next") if isinstance(pagination, dict) else None
            if not next_url:
                break
            response = self._client.get(str(next_url))
        else:
            raise ValueError("NWS observation pagination exceeded 20 pages")

        if not observations:
            raise ValueError(
                f"NWS returned no usable {normalized} temperatures for {target_date.isoformat()}"
            )
        observations.sort(key=lambda item: item[0])
        return first_request_url, ObservedDailyHigh(
            source="NOAA/NWS station observations",
            truth_kind=TruthKind.NOAA_SAME_STATION_PROVISIONAL,
            station_id=normalized,
            local_date=target_date,
            timezone=timezone,
            value_f=max(value for _, value in observations),
            observation_count=len(observations),
            first_observation_at=observations[0][0],
            last_observation_at=observations[-1][0],
            finalized=saw_next_day_observation,
            fetched_at=datetime.now(UTC),
            raw={"pages": pages, "as_of": effective_as_of.isoformat()},
        )
