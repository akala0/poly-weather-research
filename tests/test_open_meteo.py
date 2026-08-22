import httpx

from poly_weather.adapters.open_meteo import OpenMeteoEnsembleClient


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
