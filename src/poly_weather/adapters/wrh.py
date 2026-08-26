"""Read-only adapter for the exact weather.gov WRH/Synoptic time-series feed."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

_TOKEN = re.compile(r"mesoToken\s*=\s*['\"](?P<token>[a-f0-9]+)['\"]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WrhObservation:
    station_id: str
    observed_at: datetime
    temperature_f: Decimal
    request_url: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WrhHistory:
    station_id: str
    start: datetime
    end: datetime
    observations: tuple[WrhObservation, ...]
    request_url: str
    raw: dict[str, Any]


class WrhTimeseriesClient:
    """Fetch the same ``air_temp_set_1`` array used by the WRH settlement page."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._token: str | None = None
        self._token_lock = asyncio.Lock()
        self._primed_stations: set[str] = set()

    async def _public_token(self) -> str:
        if self._token is not None:
            return self._token
        async with self._token_lock:
            if self._token is not None:
                return self._token
            response = await self.client.get("https://www.weather.gov/source/wrh/apiKey.js")
            try:
                response.raise_for_status()
                match = _TOKEN.search(response.text)
            finally:
                await response.aclose()
            if match is None:
                raise ValueError("weather.gov WRH public Synoptic token not found")
            self._token = match.group("token")
        return self._token

    @staticmethod
    def _headers(station_id: str) -> dict[str, str]:
        return {
            "Origin": "https://www.weather.gov",
            "Referer": (
                "https://www.weather.gov/wrh/timeseries?site="
                f"{station_id.casefold()}"
            ),
        }

    @staticmethod
    def _observations(
        payload: dict[str, Any],
        *,
        station_id: str,
        request_url: str,
    ) -> tuple[WrhObservation, ...]:
        stations = payload.get("STATION")
        if not isinstance(stations, list) or not stations or not isinstance(stations[0], dict):
            raise ValueError(f"WRH returned no station data for {station_id}")
        observations = stations[0].get("OBSERVATIONS")
        if not isinstance(observations, dict):
            raise ValueError(f"WRH returned no observations for {station_id}")
        timestamps = observations.get("date_time")
        temperatures = observations.get("air_temp_set_1")
        if not isinstance(timestamps, list) or not isinstance(temperatures, list):
            raise ValueError(f"WRH returned no air_temp_set_1 for {station_id}")
        usable = tuple(
            WrhObservation(
                station_id=station_id,
                observed_at=datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                ).astimezone(UTC),
                temperature_f=Decimal(str(value)),
                request_url=request_url,
                raw={},
            )
            for timestamp, value in zip(timestamps, temperatures, strict=False)
            if value is not None
        )
        if not usable:
            raise ValueError(f"WRH returned no usable temperature for {station_id}")
        return usable

    async def latest(
        self, station_id: str, *, recent_minutes: int | None = None
    ) -> WrhObservation:
        normalized_station = station_id.upper()
        requested_minutes = (
            recent_minutes
            if recent_minutes is not None
            else 1500
            if normalized_station not in self._primed_stations
            else 180
        )
        token = await self._public_token()
        response = await self.client.get(
            "https://api.synopticdata.com/v2/stations/timeseries",
            headers=self._headers(normalized_station),
            params={
                "STID": normalized_station,
                "showemptystations": 1,
                "units": "temp|F",
                "recent": requested_minutes,
                "complete": 1,
                "token": token,
                "obtimezone": "utc",
            },
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
        self._primed_stations.add(normalized_station)
        if not isinstance(payload, dict):
            raise ValueError(f"WRH returned a non-object payload for {station_id}")
        usable = self._observations(
            payload,
            station_id=normalized_station,
            request_url=str(response.request.url),
        )
        latest = max(usable, key=lambda row: row.observed_at)
        return WrhObservation(
            station_id=latest.station_id,
            observed_at=latest.observed_at,
            temperature_f=latest.temperature_f,
            request_url=latest.request_url,
            raw=payload,
        )

    async def history(
        self,
        station_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> WrhHistory:
        """Fetch an explicit UTC interval; WRH's public page limits this to 30 days."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("WRH history start and end must be timezone-aware")
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if end_utc <= start_utc:
            raise ValueError("WRH history end must be after start")
        if end_utc - start_utc > timedelta(days=30):
            raise ValueError("WRH history range cannot exceed 30 days")
        normalized_station = station_id.upper()
        token = await self._public_token()
        response = await self.client.get(
            "https://api.synopticdata.com/v2/stations/timeseries",
            headers=self._headers(normalized_station),
            params={
                "STID": normalized_station,
                "showemptystations": 1,
                "units": "temp|F",
                "start": start_utc.strftime("%Y%m%d%H%M"),
                "end": end_utc.strftime("%Y%m%d%H%M"),
                "complete": 1,
                "token": token,
                "obtimezone": "utc",
            },
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            await response.aclose()
        if not isinstance(payload, dict):
            raise ValueError(f"WRH returned a non-object payload for {station_id}")
        observations = self._observations(
            payload,
            station_id=normalized_station,
            request_url=str(response.request.url),
        )
        in_range = tuple(
            row for row in observations if start_utc <= row.observed_at <= end_utc
        )
        return WrhHistory(
            station_id=normalized_station,
            start=start_utc,
            end=end_utc,
            observations=in_range,
            request_url=str(response.request.url),
            raw=payload,
        )
