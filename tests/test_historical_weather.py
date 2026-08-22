from datetime import date

import httpx

from poly_weather.adapters.historical_weather import (
    NceiDailySummariesClient,
    OpenMeteoPreviousRunsClient,
)


def test_previous_runs_groups_hourly_values_into_daily_highs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["hourly"] == "temperature_2m_previous_day2"
        return httpx.Response(
            200,
            request=request,
            json={
                "hourly": {
                    "time": [
                        "2026-07-01T00:00",
                        "2026-07-01T12:00",
                        "2026-07-02T00:00",
                        "2026-07-02T12:00",
                    ],
                    "temperature_2m_previous_day2": [70.0, 82.5, 71.0, 84.0],
                }
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoPreviousRunsClient(client=http_client) as client:
        _, series = client.daily_highs(
            station_id="KLGA",
            latitude=40.779,
            longitude=-73.88,
            timezone="America/New_York",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 2),
            lead_days=2,
        )

    assert [(point.local_date.isoformat(), point.value_f) for point in series.values] == [
        ("2026-07-01", 82.5),
        ("2026-07-02", 84.0),
    ]


def test_previous_runs_day_zero_uses_canonical_response_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["hourly"] == "temperature_2m_previous_day0"
        return httpx.Response(
            200,
            request=request,
            json={
                "hourly": {
                    "time": ["2026-07-01T00:00", "2026-07-01T12:00"],
                    "temperature_2m": [70.0, 83.0],
                }
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with OpenMeteoPreviousRunsClient(client=http_client) as client:
        _, series = client.daily_highs(
            station_id="KLAX",
            latitude=33.9382,
            longitude=-118.3866,
            timezone="America/Los_Angeles",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
            lead_days=0,
        )

    assert series.values[0].value_f == 83.0


def test_ncei_adapter_reads_tmax_and_ignores_other_stations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["dataTypes"] == "TMAX"
        return httpx.Response(
            200,
            request=request,
            json=[
                {"DATE": "2026-07-01", "STATION": "USW00014732", "TMAX": "93"},
                {"DATE": "2026-07-01", "STATION": "OTHER", "TMAX": "70"},
            ],
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with NceiDailySummariesClient(client=http_client) as client:
        _, series = client.daily_highs(
            station_id="USW00014732",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 1),
        )

    assert len(series.values) == 1
    assert series.values[0].value_f == 93.0
