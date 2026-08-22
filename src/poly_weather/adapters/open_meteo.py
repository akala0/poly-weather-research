from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from poly_weather.domain import EnsembleForecast


class OpenMeteoEnsembleClient:
    """Read-only Open-Meteo ensemble adapter, configured for GEFS by default."""

    def __init__(
        self,
        *,
        base_url: str = "https://ensemble-api.open-meteo.com",
        timeout: float = 30.0,
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

    def __enter__(self) -> OpenMeteoEnsembleClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def temperature_forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone: str,
        forecast_days: int,
        model: str = "gfs_seamless",
    ) -> tuple[str, EnsembleForecast]:
        response = self._client.get(
            "/v1/ensemble",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m",
                "models": model,
                "temperature_unit": "fahrenheit",
                "timezone": timezone,
                "forecast_days": forecast_days,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Open-Meteo ensemble response is not an object")
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("Open-Meteo response has no hourly object")
        raw_times = hourly.get("time")
        if not isinstance(raw_times, list):
            raise ValueError("Open-Meteo response has no hourly time axis")

        members: dict[str, tuple[Decimal | None, ...]] = {}
        for name, raw_values in hourly.items():
            if not name.startswith("temperature_2m") or not isinstance(raw_values, list):
                continue
            members[name] = tuple(
                None if value is None else Decimal(str(value)) for value in raw_values
            )
        unit = str((payload.get("hourly_units") or {}).get("temperature_2m") or "°F")
        forecast = EnsembleForecast(
            model=model,
            latitude=float(payload.get("latitude", latitude)),
            longitude=float(payload.get("longitude", longitude)),
            timezone=str(payload.get("timezone") or timezone),
            temperature_unit=unit,
            fetched_at=datetime.now(UTC),
            times=tuple(datetime.fromisoformat(str(value)) for value in raw_times),
            members=members,
            raw=payload,
        )
        return str(response.request.url), forecast

