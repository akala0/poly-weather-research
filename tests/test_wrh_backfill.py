import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from poly_weather.adapters.wrh import WrhTimeseriesClient
from poly_weather.weather_provenance import require_realtime_for_no_lookahead
from poly_weather.wrh_backfill import (
    WrhBackfillRequest,
    cache_wrh_temperature_history,
    partition_wrh_backfill_range,
    realtime_wrh_points_many,
)


def test_wrh_backfill_rejects_more_than_30_calendar_days() -> None:
    allowed = WrhBackfillRequest(
        station_id="KLGA",
        timezone="America/New_York",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 30),
    )
    allowed.utc_interval()

    refused = WrhBackfillRequest(
        station_id="KLGA",
        timezone="America/New_York",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )
    with pytest.raises(ValueError, match="cannot exceed 30 calendar days"):
        refused.utc_interval()


def test_long_range_is_partitioned_below_public_limit() -> None:
    requests = partition_wrh_backfill_range(
        station_id="KLGA",
        timezone="America/New_York",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 8, 20),
    )
    assert len(requests) == 3
    assert all((row.end_date - row.start_date).days + 1 <= 29 for row in requests)


def test_temperature_only_cache_keeps_historical_provenance(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/apiKey.js"):
            return httpx.Response(200, request=request, text="var mesoToken = 'abc123';")
        return httpx.Response(
            200,
            request=request,
            json={
                "STATION": [
                    {
                        "OBSERVATIONS": {
                            "date_time": ["2026-08-24T12:00:00Z"],
                            "air_temp_set_1": [77.54],
                        }
                    }
                ]
            },
        )

    async def fetch() -> dict:
        original = httpx.AsyncClient

        class MockClient(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(handler)
                super().__init__(*args, **kwargs)

        httpx.AsyncClient = MockClient
        try:
            return await cache_wrh_temperature_history(
                [
                    WrhBackfillRequest(
                        station_id="KLGA",
                        timezone="America/New_York",
                        start_date=date(2026, 8, 24),
                        end_date=date(2026, 8, 24),
                    )
                ],
                data_dir=tmp_path,
            )
        finally:
            httpx.AsyncClient = original

    result = asyncio.run(fetch())
    row = json.loads(Path(result["results"][0]["path"]).read_text(encoding="utf-8"))
    assert row["collection_mode"] == "historical_backfill"
    assert row["temperature_only"] is True
    assert list(row["payload"]["STATION"][0]["OBSERVATIONS"]) == [
        "date_time",
        "air_temp_set_1",
    ]


def test_wrh_history_uses_explicit_range_instead_of_recent() -> None:
    observed_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_request
        if request.url.path.endswith("/apiKey.js"):
            return httpx.Response(200, request=request, text="var mesoToken = 'abc123';")
        observed_request = request
        return httpx.Response(
            200,
            request=request,
            json={
                "STATION": [
                    {
                        "OBSERVATIONS": {
                            "date_time": ["2026-08-24T12:00:00Z"],
                            "air_temp_set_1": [77.54],
                        }
                    }
                ]
            },
        )

    async def fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            history = await WrhTimeseriesClient(http).history(
                "KLGA",
                start=datetime(2026, 8, 24, tzinfo=UTC),
                end=datetime(2026, 8, 25, tzinfo=UTC),
            )
        assert len(history.observations) == 1

    asyncio.run(fetch())
    assert observed_request is not None
    assert "recent" not in observed_request.url.params
    assert observed_request.url.params["start"] == "202608240000"
    assert observed_request.url.params["end"] == "202608250000"


def test_realtime_reader_ignores_historical_archive_and_provenance(tmp_path) -> None:
    realtime_path = tmp_path / "raw" / "weather_daemon" / "2026-08-24" / "events.jsonl"
    historical_path = (
        tmp_path / "raw" / "wrh_historical_backfill" / "2026-08-24" / "events.jsonl"
    )
    realtime_path.parent.mkdir(parents=True)
    historical_path.parent.mkdir(parents=True)
    source_payload = {
        "STATION": [
            {
                "OBSERVATIONS": {
                    "date_time": ["2026-08-24T12:00:00Z"],
                    "air_temp_set_1": [77.5],
                }
            }
        ]
    }
    realtime_path.write_text(
        json.dumps(
            {
                "station_id": "KLGA",
                "product": "wrh_timeseries_observation",
                "collection_mode": "realtime",
                "raw": {"source_payload": source_payload},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    historical_path.write_text(
        json.dumps(
            {
                "station_id": "KLGA",
                "product": "wrh_timeseries_observation",
                "collection_mode": "historical_backfill",
                "raw": {
                    "source_payload": {
                        "STATION": [
                            {
                                "OBSERVATIONS": {
                                    "date_time": ["2026-08-24T13:00:00Z"],
                                    "air_temp_set_1": [90.0],
                                }
                            }
                        ]
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    points = realtime_wrh_points_many(
        tmp_path,
        intervals={
            "KLGA": (
                datetime(2026, 8, 24, tzinfo=UTC),
                datetime(2026, 8, 25, tzinfo=UTC),
            )
        },
    )
    assert list(points["KLGA"].values()) == [77.5]
    with pytest.raises(ValueError, match="refuses historical_backfill"):
        require_realtime_for_no_lookahead(
            ({"collection_mode": "historical_backfill"},)
        )
