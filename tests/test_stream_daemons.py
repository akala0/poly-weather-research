import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from poly_weather.market_stream import BookState, MarketWebSocketBot
from poly_weather.weather_stream import WeatherDaemon, WeatherStation


def test_book_state_replaces_and_applies_level_changes() -> None:
    state = BookState()
    state.replace(
        bids=[{"price": "0.40", "size": "10"}, {"price": "0.42", "size": "3"}],
        asks=[{"price": "0.46", "size": "4"}, {"price": "0.48", "size": "8"}],
    )
    assert state.best_bid == Decimal("0.42")
    assert state.best_ask == Decimal("0.46")

    state.change(side="BUY", price=Decimal("0.42"), size=Decimal("0"))
    state.change(side="SELL", price=Decimal("0.44"), size=Decimal("2"))
    assert state.best_bid == Decimal("0.40")
    assert state.best_ask == Decimal("0.44")


def test_market_bot_normalizes_book_price_change_and_trade(tmp_path) -> None:
    bot = MarketWebSocketBot(
        asset_slugs={"yes-1": "bucket-1:Yes"},
        data_dir=tmp_path,
    )
    try:
        book = bot._records(
            {
                "event_type": "book",
                "asset_id": "yes-1",
                "market": "condition-1",
                "timestamp": "1787390000000",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.44", "size": "10"}],
            }
        )[0]
        changes = bot._records(
            {
                "event_type": "price_change",
                "market": "condition-1",
                "timestamp": "1787390000100",
                "price_changes": [
                    {
                        "asset_id": "yes-1",
                        "price": "0.42",
                        "size": "5",
                        "side": "BUY",
                        "best_bid": "0.42",
                        "best_ask": "0.44",
                    }
                ],
            }
        )[0]
        trade = bot._records(
            {
                "event_type": "last_trade_price",
                "asset_id": "yes-1",
                "market": "condition-1",
                "timestamp": "1787390000200",
                "price": "0.43",
            }
        )[0]
    finally:
        bot.sink.close()

    assert book.best_bid == Decimal("0.40")
    assert changes.best_bid == Decimal("0.42")
    assert changes.market_slug == "bucket-1:Yes"
    assert trade.last_trade_price == Decimal("0.43")
    assert [book.sequence, changes.sequence, trade.sequence] == [1, 2, 3]


def test_weather_daemon_fetches_normalized_noaa_and_aviation_events(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            return httpx.Response(
                200,
                request=request,
                json={
                    "properties": {
                        "timestamp": "2026-08-22T08:00:00Z",
                        "temperature": {"value": 22.0},
                    }
                },
            )
        if request.url.path.endswith("/metar"):
            return httpx.Response(
                200,
                request=request,
                json=[{"icaoId": "KLGA", "obsTime": 1_787_385_600, "temp": 21.7}],
            )
        if request.url.host == "ensemble-api.open-meteo.com":
            return httpx.Response(
                200,
                request=request,
                json={
                    "hourly": {
                        "time": ["2026-08-22T08:00"],
                        "temperature_2m_member01": [73.4],
                    }
                },
            )
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "icaoId": "KLGA",
                    "issueTime": "2026-08-22T07:40:00Z",
                    "validTimeFrom": 1_787_384_400,
                    "validTimeTo": 1_787_492_400,
                }
            ],
        )

    async def fetch() -> tuple[str, str, str, str]:
        daemon = WeatherDaemon(
            station_id="KLGA",
            data_dir=tmp_path,
            latitude=40.7769,
            longitude=-73.874,
            timezone="America/New_York",
        )
        transport = httpx.MockTransport(handler)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                nws = await daemon._fetch_nws(client)
                metar = await daemon._fetch_metar(client)
                taf = await daemon._fetch_taf(client)
                gefs = await daemon._fetch_gefs(client)
        finally:
            daemon.sink.close()
        assert metar is not None
        assert taf is not None
        assert nws.temperature_c == 22.0
        assert metar.temperature_c == 21.7
        return nws.product, metar.product, taf.product, gefs.product

    assert asyncio.run(fetch()) == (
        "latest_observation",
        "metar",
        "taf",
        "gefs_ensemble_forecast",
    )


def test_weather_event_keeps_nanosecond_receive_clock_and_latency(tmp_path) -> None:
    daemon = WeatherDaemon(station_id="KLGA", data_dir=tmp_path)
    try:
        event = daemon._event(
            provider="NOAA/NWS",
            product="latest_observation",
            source_time=datetime.now(UTC),
            temperature_c=20.0,
            raw={},
        )
    finally:
        daemon.sink.close()

    assert event.received_at_ns > 0
    assert event.source_timestamp_ms is not None
    assert event.latency_ms is not None
    assert event.latency_ms >= 0


def test_weather_daemon_accepts_multiple_unique_stations(tmp_path) -> None:
    daemon = WeatherDaemon(
        data_dir=tmp_path,
        stations=(
            WeatherStation("klga", 40.7769, -73.874, "America/New_York"),
            WeatherStation("klax", 33.9382, -118.3866, "America/Los_Angeles"),
        ),
    )
    try:
        assert [station.station_id for station in daemon.stations] == ["KLGA", "KLAX"]
        assert set(daemon.station_metrics) == {"KLGA", "KLAX"}
    finally:
        daemon.sink.close()
