import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from poly_weather.adapters.aviation_weather import AviationWeatherClient
from poly_weather.adapters.clob import ClobClient
from poly_weather.adapters.nws import NwsClient
from poly_weather.adapters.polymarket import GammaClient, is_weather_market


def test_gamma_client_fetches_and_filters_weather_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["closed"] == "false"
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "id": "weather-1",
                    "question": "Highest temperature in New York tomorrow?",
                    "slug": "highest-temperature-new-york",
                    "outcomes": '["80-81", "82-83"]',
                    "outcomePrices": '["0.4", "0.6"]',
                },
                {
                    "id": "politics-1",
                    "question": "Will a bill pass?",
                    "slug": "bill-pass",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.5", "0.5"]',
                },
            ],
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with GammaClient(client=http_client) as client:
        page = client.fetch_markets_page(limit=10)

    assert len(page.markets) == 2
    assert [market.market_id for market in page.markets if is_weather_market(market)] == [
        "weather-1"
    ]


def test_gamma_search_flattens_event_markets_and_inherits_resolution_source() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public-search"
        assert request.url.params["q"] == "highest temperature"
        return httpx.Response(
            200,
            request=request,
            json={
                "events": [
                    {
                        "title": "Highest temperature in NYC?",
                        "description": "Daily high temperature event",
                        "resolutionSource": "https://example.test/rules",
                        "markets": [
                            {
                                "id": "bucket-1",
                                "question": "Will NYC reach 80-81°F?",
                                "slug": "nyc-80-81",
                                "outcomes": '["Yes", "No"]',
                                "outcomePrices": '["0.4", "0.6"]',
                            }
                        ],
                    }
                ],
                "pagination": {"hasMore": False},
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with GammaClient(client=http_client) as client:
        page = client.search_markets_page(query="highest temperature", limit=20)

    assert page.has_more is False
    assert len(page.markets) == 1
    assert page.markets[0].resolution_source == "https://example.test/rules"
    assert is_weather_market(page.markets[0])


def test_nws_client_normalizes_latest_observation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stations/KNYC/observations/latest"
        return httpx.Response(
            200,
            request=request,
            json={
                "properties": {
                    "timestamp": "2026-08-22T12:00:00+00:00",
                    "temperature": {"value": 25.4, "unitCode": "wmoUnit:degC"},
                }
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with NwsClient(client=http_client) as client:
        observation = client.latest_observation("knyc")

    assert observation.station_id == "KNYC"
    assert str(observation.temperature_c) == "25.4"


def test_nws_daily_high_uses_local_day_and_marks_next_day_finalization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/stations/KLGA/observations"
        return httpx.Response(
            200,
            request=request,
            json={
                "features": [
                    {
                        "properties": {
                            "timestamp": "2026-08-22T03:50:00+00:00",
                            "temperature": {"value": 30, "unitCode": "wmoUnit:degC"},
                        }
                    },
                    {
                        "properties": {
                            "timestamp": "2026-08-22T05:00:00+00:00",
                            "temperature": {"value": 10, "unitCode": "wmoUnit:degC"},
                        }
                    },
                ],
                "pagination": {},
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with NwsClient(client=http_client) as client:
        _, observed = client.daily_high(
            station_id="klga",
            target_date=date(2026, 8, 21),
            timezone="America/New_York",
            as_of=datetime(2026, 8, 22, 6, tzinfo=UTC),
        )

    assert observed.observation_count == 1
    assert observed.value_f == 86
    assert observed.finalized is True


def test_clob_batch_price_history_parses_each_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/batch-prices-history"
        body = json.loads(request.content)
        assert body["markets"] == ["yes-1", "yes-2"]
        assert body["start_ts"] < body["end_ts"]
        assert body["fidelity"] == 60
        return httpx.Response(
            200,
            request=request,
            json={
                "history": {
                    "yes-1": [{"t": 1_787_313_600, "p": 0.42}],
                    "yes-2": [{"t": 1_787_313_600, "p": 0.18}],
                }
            },
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with ClobClient(client=http_client) as client:
        batch = client.batch_price_history(
            token_ids=("yes-1", "yes-2"),
            start=datetime(2026, 8, 21, tzinfo=UTC),
            end=datetime(2026, 8, 22, tzinfo=UTC),
        )

    assert [series.token_id for series in batch.series] == ["yes-1", "yes-2"]
    assert batch.series[0].points[0].price.as_tuple().digits == (4, 2)


def test_clob_batch_quotes_calculates_midpoint_and_spread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/prices"
        body = json.loads(request.content)
        assert body == [
            {"token_id": "yes-1", "side": "BUY"},
            {"token_id": "yes-1", "side": "SELL"},
        ]
        return httpx.Response(
            200,
            request=request,
            json={"yes-1": {"BUY": "0.42", "SELL": "0.46"}},
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with ClobClient(client=http_client) as client:
        batch = client.batch_quotes(token_ids=("yes-1",))

    assert batch.quotes[0].best_bid == Decimal("0.42")
    assert batch.quotes[0].best_ask == Decimal("0.46")
    assert batch.quotes[0].midpoint == Decimal("0.44")
    assert batch.quotes[0].spread == Decimal("0.04")


def test_aviation_weather_snapshot_parses_metar_and_taf() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["ids"] == "KLGA"
        if request.url.path.endswith("/metar"):
            return httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "icaoId": "KLGA",
                        "obsTime": 1_787_381_460,
                        "temp": 22.2,
                        "dewp": 16.7,
                        "rawOb": "METAR KLGA 220651Z 04006KT 10SM OVC180 22/17",
                        "fltCat": "VFR",
                    }
                ],
            )
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "icaoId": "KLGA",
                    "issueTime": "2026-08-22T05:40:00Z",
                    "validTimeFrom": 1_787_378_400,
                    "validTimeTo": 1_787_486_400,
                    "rawTAF": "TAF KLGA 220540Z 2206/2312 04004KT P6SM",
                    "fcsts": [{"timeFrom": 1_787_378_400, "wxString": "-RA"}],
                }
            ],
        )

    http_client = httpx.Client(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    with AviationWeatherClient(client=http_client) as client:
        _, _, snapshot = client.snapshot(station_id="klga", metar_hours=2)

    assert snapshot.station_id == "KLGA"
    assert snapshot.metars[0].temperature_c == Decimal("22.2")
    assert snapshot.tafs[0].periods[0]["wxString"] == "-RA"
