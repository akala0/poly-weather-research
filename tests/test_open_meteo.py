from decimal import Decimal

import httpx
import pytest

from poly_weather.adapters.open_meteo import (
    OpenMeteoDeterministicClient,
    OpenMeteoEnsembleClient,
)


def test_open_meteo_adapter_parses_member_series() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["models"] == "gfs_seamless"
        assert request.url.params["temperature_unit"] == "fahrenheit"
        return httpx.Response(
            200,
            request=request,
            json={
                "latitude": 40.8,
                "longitude": -73.9,
                "timezone": "America/New_York",
                "hourly_units": {"temperature_2m": "°F"},
                "hourly": {
                    "time": ["2026-08-22T00:00", "2026-08-22T01:00"],
                    "temperature_2m": [70.1, 72.3],
                    "temperature_2m_member01": [69.8, 73.0],
                },
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoEnsembleClient(client=http_client) as client:
        _, forecast = client.temperature_forecast(
            latitude=40.8,
            longitude=-73.9,
            timezone="America/New_York",
            forecast_days=1,
        )

    assert len(forecast.members) == 2
    assert [str(value) for value in forecast.daily_highs(forecast.times[0].date())] == [
        "72.3",
        "73.0",
    ]


def test_open_meteo_ensemble_rejects_distant_grid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "latitude": 34.0,
                "longitude": -118.5,
                "hourly": {
                    "time": ["2026-08-22T00:00"],
                    "temperature_2m": [80.0],
                },
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoEnsembleClient(client=http_client) as client:
        with pytest.raises(ValueError, match=r"distance=12\.51 km exceeds 3\.00 km"):
            client.temperature_forecast(
                latitude=33.9382,
                longitude=-118.3866,
                timezone="America/Los_Angeles",
                forecast_days=1,
            )


def test_open_meteo_deterministic_parses_single_series() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/forecast"
        return httpx.Response(
            200,
            request=request,
            json={
                "latitude": 33.94541,
                "longitude": -118.40222,
                "timezone": "America/Los_Angeles",
                "hourly_units": {"temperature_2m": "°F"},
                "hourly": {
                    "time": ["2026-08-22T00:00", "2026-08-22T12:00"],
                    "temperature_2m": [70.0, 82.2],
                },
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoDeterministicClient(client=http_client) as client:
        _, forecast = client.temperature_forecast(
            latitude=33.9382,
            longitude=-118.3866,
            timezone="America/Los_Angeles",
            forecast_days=1,
        )

    assert forecast.daily_high(forecast.times[0].date()) == Decimal("82.2")


def test_open_meteo_multi_model_fetches_three_deterministic_series_once() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.params["models"] == (
            "gfs_seamless,icon_seamless,gem_seamless"
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "latitude": 33.94541,
                "longitude": -118.40222,
                "timezone": "America/Los_Angeles",
                "hourly": {
                    "time": ["2026-08-22T00:00", "2026-08-22T12:00"],
                    "temperature_2m_gfs_seamless": [70.0, 82.0],
                    "temperature_2m_icon_seamless": [71.0, 81.0],
                    "temperature_2m_gem_seamless": [69.0, 83.0],
                },
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoDeterministicClient(client=http_client) as client:
        _, forecasts = client.get_multi_model_ensemble(
            latitude=33.9382,
            longitude=-118.3866,
            timezone="America/Los_Angeles",
            forecast_days=1,
        )

    assert requests == 1
    assert set(forecasts) == {"gfs", "icon", "gem"}
    target_date = forecasts["gfs"].times[0].date()
    assert {
        model: forecast.daily_high(target_date) for model, forecast in forecasts.items()
    } == {"gfs": Decimal("82.0"), "icon": Decimal("81.0"), "gem": Decimal("83.0")}
