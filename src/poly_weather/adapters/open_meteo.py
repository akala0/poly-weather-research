from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from poly_weather.domain import DeterministicForecast, EnsembleForecast

MAX_GRID_DISTANCE_KM = 3.0
DEFAULT_MULTI_MODELS = {
    "gfs": "gfs_seamless",
    "icon": "icon_seamless",
    "gem": "gem_seamless",
}


def haversine_distance_km(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Great-circle distance between two WGS84 coordinates."""
    earth_radius_km = 6371.0088
    latitude_a_rad = math.radians(latitude_a)
    latitude_b_rad = math.radians(latitude_b)
    latitude_delta = math.radians(latitude_b - latitude_a)
    longitude_delta = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_a_rad)
        * math.cos(latitude_b_rad)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * earth_radius_km * math.asin(math.sqrt(haversine))


def validate_response_grid(
    payload: dict[str, object],
    *,
    requested_latitude: float,
    requested_longitude: float,
    max_distance_km: float = MAX_GRID_DISTANCE_KM,
) -> tuple[float, float]:
    try:
        returned_latitude = float(payload["latitude"])
        returned_longitude = float(payload["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Open-Meteo response is missing valid grid coordinates") from exc
    distance_km = haversine_distance_km(
        requested_latitude,
        requested_longitude,
        returned_latitude,
        returned_longitude,
    )
    if distance_km > max_distance_km:
        raise ValueError(
            "Open-Meteo grid mismatch: "
            f"requested=({requested_latitude:.6f}, {requested_longitude:.6f}), "
            f"returned=({returned_latitude:.6f}, {returned_longitude:.6f}), "
            f"distance={distance_km:.2f} km exceeds {max_distance_km:.2f} km"
        )
    return returned_latitude, returned_longitude


def parse_multi_model_forecasts(
    payload: dict[str, object],
    *,
    requested_latitude: float,
    requested_longitude: float,
    timezone: str,
    models: dict[str, str] | None = None,
    fetched_at: datetime | None = None,
) -> dict[str, DeterministicForecast]:
    """Parse and grid-check a deterministic multi-model API response."""
    selected_models = models or DEFAULT_MULTI_MODELS
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ValueError("Open-Meteo multi-model response has no hourly object")
    raw_times = hourly.get("time")
    if not isinstance(raw_times, list):
        raise ValueError("Open-Meteo multi-model response has no time axis")
    times = tuple(datetime.fromisoformat(str(value)) for value in raw_times)
    units = payload.get("hourly_units") or {}
    if not isinstance(units, dict):
        raise ValueError("Open-Meteo multi-model units are invalid")
    response_timezone = payload.get("timezone")
    parsed: dict[str, DeterministicForecast] = {}
    for short_name, model in selected_models.items():
        returned_latitude, returned_longitude = validate_response_grid(
            payload,
            requested_latitude=requested_latitude,
            requested_longitude=requested_longitude,
        )
        variable = f"temperature_2m_{model}"
        raw_values = hourly.get(variable)
        if not isinstance(raw_values, list) or len(raw_values) != len(raw_times):
            raise ValueError(f"Open-Meteo multi-model values are invalid for {model}")
        parsed[short_name] = DeterministicForecast(
            model=model,
            latitude=returned_latitude,
            longitude=returned_longitude,
            timezone=str(response_timezone or timezone),
            temperature_unit=str(units.get(variable) or "°F"),
            fetched_at=fetched_at or datetime.now(UTC),
            times=times,
            values=tuple(
                None if value is None else Decimal(str(value)) for value in raw_values
            ),
            raw=payload,
        )
    return parsed


class OpenMeteoDeterministicClient:
    """Read-only deterministic Open-Meteo forecast adapter."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.open-meteo.com",
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

    def __enter__(self) -> OpenMeteoDeterministicClient:
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
    ) -> tuple[str, DeterministicForecast]:
        response = self._client.get(
            "/v1/forecast",
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
            raise ValueError("Open-Meteo deterministic response is not an object")
        returned_latitude, returned_longitude = validate_response_grid(
            payload,
            requested_latitude=latitude,
            requested_longitude=longitude,
        )
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("Open-Meteo deterministic response has no hourly object")
        raw_times = hourly.get("time")
        raw_values = hourly.get("temperature_2m")
        if (
            not isinstance(raw_times, list)
            or not isinstance(raw_values, list)
            or len(raw_times) != len(raw_values)
        ):
            raise ValueError("Open-Meteo deterministic time/value arrays are invalid")
        forecast = DeterministicForecast(
            model=model,
            latitude=returned_latitude,
            longitude=returned_longitude,
            timezone=str(payload.get("timezone") or timezone),
            temperature_unit=str((payload.get("hourly_units") or {}).get("temperature_2m") or "°F"),
            fetched_at=datetime.now(UTC),
            times=tuple(datetime.fromisoformat(str(value)) for value in raw_times),
            values=tuple(
                None if value is None else Decimal(str(value)) for value in raw_values
            ),
            raw=payload,
        )
        return str(response.request.url), forecast

    def get_multi_model_ensemble(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone: str,
        forecast_days: int,
        models: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, DeterministicForecast]]:
        """Fetch multiple deterministic model families in one API request."""
        selected_models = models or DEFAULT_MULTI_MODELS
        if not selected_models:
            raise ValueError("at least one deterministic model is required")
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m",
                "models": ",".join(selected_models.values()),
                "temperature_unit": "fahrenheit",
                "timezone": timezone,
                "forecast_days": forecast_days,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Open-Meteo multi-model response is not an object")
        fetched_at = datetime.now(UTC)
        forecasts = parse_multi_model_forecasts(
            payload,
            requested_latitude=latitude,
            requested_longitude=longitude,
            timezone=timezone,
            models=selected_models,
            fetched_at=fetched_at,
        )
        return str(response.request.url), forecasts


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
        returned_latitude, returned_longitude = validate_response_grid(
            payload,
            requested_latitude=latitude,
            requested_longitude=longitude,
        )
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
            latitude=returned_latitude,
            longitude=returned_longitude,
            timezone=str(payload.get("timezone") or timezone),
            temperature_unit=unit,
            fetched_at=datetime.now(UTC),
            times=tuple(datetime.fromisoformat(str(value)) for value in raw_times),
            members=members,
            raw=payload,
        )
        return str(response.request.url), forecast
