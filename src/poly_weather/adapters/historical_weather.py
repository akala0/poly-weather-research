from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime

import httpx

from poly_weather.domain import DailyHighPoint, DailyHighSeries


class OpenMeteoPreviousRunsClient:
    """Fixed-lead historical forecasts from Open-Meteo Previous Runs."""

    def __init__(
        self,
        *,
        base_url: str = "https://previous-runs-api.open-meteo.com",
        timeout: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenMeteoPreviousRunsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def daily_highs(
        self,
        *,
        station_id: str,
        latitude: float,
        longitude: float,
        timezone: str,
        start_date: date,
        end_date: date,
        lead_days: int,
        model: str = "gfs_seamless",
    ) -> tuple[str, DailyHighSeries]:
        if not 0 <= lead_days <= 7:
            raise ValueError("Previous Runs lead_days must be between 0 and 7")
        variable = f"temperature_2m_previous_day{lead_days}"
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": variable,
                "models": model,
                "temperature_unit": "fahrenheit",
                "timezone": timezone,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hourly"), dict):
            raise ValueError("Open-Meteo Previous Runs response has no hourly data")
        hourly = payload["hourly"]
        times = hourly.get("time")
        # The API accepts temperature_2m_previous_day0 but returns the day-0
        # series under the canonical temperature_2m response key.
        response_variable = "temperature_2m" if lead_days == 0 else variable
        values = hourly.get(response_variable)
        if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
            raise ValueError("Open-Meteo Previous Runs time/value arrays are invalid")
        grouped: dict[date, list[float]] = defaultdict(list)
        for raw_time, raw_value in zip(times, values, strict=True):
            if raw_value is None:
                continue
            grouped[datetime.fromisoformat(str(raw_time)).date()].append(float(raw_value))
        points = tuple(
            DailyHighPoint(local_date=day, value_f=max(day_values))
            for day, day_values in sorted(grouped.items())
            if day_values
        )
        return str(response.request.url), DailyHighSeries(
            source="Open-Meteo Previous Runs",
            station_id=station_id,
            model=model,
            lead_days=lead_days,
            fetched_at=datetime.now(UTC),
            values=points,
            raw=payload,
        )


class NceiDailySummariesClient:
    """NOAA NCEI GHCN Daily Summaries adapter."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.ncei.noaa.gov",
        timeout: float = 45.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NceiDailySummariesClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def daily_highs(
        self,
        *,
        station_id: str,
        start_date: date,
        end_date: date,
    ) -> tuple[str, DailyHighSeries]:
        response = self._client.get(
            "/access/services/data/v1",
            params={
                "dataset": "daily-summaries",
                "stations": station_id,
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "format": "json",
                "units": "standard",
                "includeAttributes": "false",
                "dataTypes": "TMAX",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("NCEI Daily Summaries response is not a list")
        points = []
        for row in payload:
            if not isinstance(row, dict) or row.get("TMAX") in (None, ""):
                continue
            if row.get("STATION") and str(row["STATION"]) != station_id:
                continue
            points.append(
                DailyHighPoint(
                    local_date=date.fromisoformat(str(row["DATE"])[:10]),
                    value_f=float(row["TMAX"]),
                )
            )
        return str(response.request.url), DailyHighSeries(
            source="NOAA NCEI Daily Summaries",
            station_id=station_id,
            fetched_at=datetime.now(UTC),
            values=tuple(sorted(points, key=lambda item: item.local_date)),
            raw=payload,
        )
