import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from poly_weather.adapters.wrh import WrhTimeseriesClient
from poly_weather.market_stream import (
    DEFAULT_MAX_WS_MESSAGE_SIZE,
    BookState,
    EventArchivePolicy,
    MarketWebSocketBot,
)
from poly_weather.weather_stream import (
    HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD,
    WeatherDaemon,
    WeatherStation,
    _http_pool_diagnostics,
)


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
        bot.sink.write_raw([book, changes, trade])
        bot.sink.write_database([book, changes, trade])
        stored_depth = bot.sink.warehouse.connection.execute(
            """
            SELECT bids_json, asks_json, book_complete
            FROM market_stream_events
            WHERE sequence = 2
            """
        ).fetchone()
    finally:
        bot.sink.close()

    assert book.best_bid == Decimal("0.40")
    assert book.book_complete is True
    assert book.bids == ({"price": "0.40", "size": "10"},)
    assert book.asks == ({"price": "0.44", "size": "10"},)
    assert changes.best_bid == Decimal("0.42")
    assert changes.book_complete is True
    assert changes.bids is None
    assert changes.asks is None
    assert changes.raw["price_changes"] == [
        {
            "asset_id": "yes-1",
            "price": "0.42",
            "size": "5",
            "side": "BUY",
            "best_bid": "0.42",
            "best_ask": "0.44",
        }
    ]
    assert changes.market_slug == "bucket-1:Yes"
    assert trade.last_trade_price == Decimal("0.43")
    assert trade.bids is None
    assert trade.asks is None
    assert stored_depth is not None
    assert stored_depth[0] is None
    assert stored_depth[1] is None
    assert stored_depth[2] is True
    assert [book.sequence, changes.sequence, trade.sequence] == [1, 2, 3]
    checkpoint_paths = list(
        (tmp_path / "raw" / "polymarket_book_checkpoints").glob("*/events.jsonl")
    )
    assert len(checkpoint_paths) == 1
    assert len(checkpoint_paths[0].read_text(encoding="utf-8").splitlines()) == 1


def test_market_archive_window_uses_each_events_local_timezone(tmp_path) -> None:
    bot = MarketWebSocketBot(
        asset_slugs={"la": "la:Yes", "ny": "ny:Yes"},
        asset_events={"la": "la-event", "ny": "ny-event"},
        event_policies={
            "la-event": EventArchivePolicy("America/Los_Angeles", date(2026, 8, 24)),
            "ny-event": EventArchivePolicy("America/New_York", date(2026, 8, 24)),
        },
        data_dir=tmp_path,
    )
    try:
        la = bot._records(
            {
                "event_type": "book",
                "asset_id": "la",
                "bids": [{"price": "0.4", "size": "1"}],
                "asks": [{"price": "0.5", "size": "1"}],
            }
        )[0]
        ny = bot._records(
            {
                "event_type": "book",
                "asset_id": "ny",
                "bids": [{"price": "0.4", "size": "1"}],
                "asks": [{"price": "0.5", "size": "1"}],
            }
        )[0]
        # 13:30 UTC is 06:30 Los Angeles (hourly audit) and 09:30 New York
        # (full capture), proving that one UTC window is not applied globally.
        received_ns = int(datetime(2026, 8, 24, 13, 30, tzinfo=UTC).timestamp() * 1e9)
        la.received_at_ns = received_ns
        ny.received_at_ns = received_ns
        assert bot._prepare_for_archive(la) is True
        assert la.raw["archive_policy"] == "outside_full_window_hourly_snapshot"
        assert bot._prepare_for_archive(la) is False
        assert bot._prepare_for_archive(ny) is True
    finally:
        bot.sink.close()


def test_market_resolved_is_always_archived(tmp_path) -> None:
    bot = MarketWebSocketBot(
        asset_slugs={"yes": "bucket:Yes"},
        asset_events={"yes": "event"},
        event_policies={
            "event": EventArchivePolicy("America/New_York", date(2026, 8, 24))
        },
        data_dir=tmp_path,
    )
    try:
        record = bot._records(
            {
                "event_type": "market_resolved",
                "assets_ids": ["yes"],
                "winning_asset_id": "yes",
            }
        )[0]
        record.received_at_ns = int(datetime(2026, 8, 25, 3, tzinfo=UTC).timestamp() * 1e9)
        assert bot._prepare_for_archive(record) is True
    finally:
        bot.sink.close()


def test_market_websocket_has_frame_headroom_without_compression(tmp_path) -> None:
    bot = MarketWebSocketBot(asset_slugs={"yes": "bucket:Yes"}, data_dir=tmp_path)
    try:
        options = bot._websocket_connect_options()
        assert options["max_size"] == DEFAULT_MAX_WS_MESSAGE_SIZE == 16 * 1024 * 1024
        assert options["compression"] is None
    finally:
        bot.sink.close()


def test_raw_market_recovery_disables_database_maintenance_and_marks_status(
    tmp_path, monkeypatch
) -> None:
    bot = MarketWebSocketBot(
        asset_slugs={"yes": "bucket:Yes"},
        data_dir=tmp_path,
        raw_collection_recovery=True,
        startup_attempt_id="attempt-test",
    )

    def forbidden_maintenance() -> None:
        raise AssertionError("raw market recovery invoked CHECKPOINT/VACUUM")

    monkeypatch.setattr(bot, "_maintain_database", forbidden_maintenance)
    bot.last_database_maintenance = 0
    try:
        assert asyncio.run(bot._maybe_maintain_database()) is False
        bot.metrics.state = "connected"
        bot._write_status()
        status = json.loads(bot.status_path.read_text(encoding="utf-8"))
        assert status["collection_mode"] == "raw_market_recovery"
        assert status["startup_attempt_id"] == "attempt-test"
        assert status["downstream_start_blocked"] is True
        assert status["retention"] == {
            "state": "disabled_raw_market_recovery",
            "raw_market_days": None,
            "aggregate_results": "permanent",
            "automatic_checkpoint_vacuum_enabled": False,
        }
    finally:
        bot.sink.close()


def test_repeated_message_too_big_failures_latch_explicit_fault(tmp_path) -> None:
    bot = MarketWebSocketBot(asset_slugs={"yes": "bucket:Yes"}, data_dir=tmp_path)
    error = (
        "ConnectionClosedError: sent 1009 (message too big) frame 1081647 bytes "
        "exceeds limit of 1048576 bytes"
    )
    try:
        for index in range(4):
            bot._record_connection_failure(error, observed_at=float(index * 30))
        assert bot.metrics.state == "reconnecting"
        bot._record_connection_failure(error, observed_at=120.0)
        assert bot.metrics.state == "faulted_message_too_big"
        assert bot.metrics.same_error_reconnects_5m == 5
        assert bot.metrics.deterministic_reconnect_fault == "message_too_big"
        assert [row["reconnect_number"] for row in bot.metrics.reconnect_history] == [
            1,
            2,
            3,
            4,
            5,
        ]
        persisted = [
            json.loads(line)
            for line in bot.reconnect_log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert persisted[-1]["error"] == error
        assert persisted[-1]["asset_count"] == 1

        # A TCP/WebSocket handshake alone must not clear a deterministic fault;
        # only a successfully received market-data frame does.
        bot._mark_data_connection_healthy()
        assert bot.metrics.state == "connected"
        assert bot.metrics.same_error_reconnects_5m == 0
        assert bot.metrics.deterministic_reconnect_fault is None
        # A healthy frame clears only the active fault, never the forensic log.
        assert len(bot.metrics.reconnect_history) == 5
    finally:
        bot.sink.close()


def test_reconnect_loop_window_excludes_old_identical_failures(tmp_path) -> None:
    bot = MarketWebSocketBot(asset_slugs={"yes": "bucket:Yes"}, data_dir=tmp_path)
    try:
        for observed_at in (0.0, 60.0, 120.0, 180.0, 601.0):
            bot._record_connection_failure("ConnectionClosedError: same", observed_at=observed_at)
        assert bot.metrics.state == "reconnecting"
        assert bot.metrics.same_error_reconnects_5m == 1
    finally:
        bot.sink.close()


def test_market_reconnect_log_preserves_unattributed_legacy_count(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "polymarket_ws_status.json").write_text(
        json.dumps(
            {
                "run_id": "old-run",
                "started_at": "2026-08-25T00:00:00+00:00",
                "last_event_at": "2026-08-25T01:00:00+00:00",
                "reconnects": 28,
            }
        ),
        encoding="utf-8",
    )
    bot = MarketWebSocketBot(asset_slugs={"yes": "bucket:Yes"}, data_dir=tmp_path)
    try:
        rows = [
            json.loads(line)
            for line in bot.reconnect_log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows == [
            {
                "record_type": "legacy_unattributed_summary",
                "observed_at": rows[0]["observed_at"],
                "previous_run_id": "old-run",
                "reconnect_count": 28,
                "previous_started_at": "2026-08-25T00:00:00+00:00",
                "previous_last_event_at": "2026-08-25T01:00:00+00:00",
                "reason": (
                    "unrecoverable: previous runtime retained only transient "
                    "last_error and did not persist per-reconnect causes"
                ),
            }
        ]
    finally:
        bot.sink.close()


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
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(
                200,
                request=request,
                json={
                    "latitude": 40.7769,
                    "longitude": -73.874,
                    "hourly": {
                        "time": ["2026-08-22T08:00"],
                        "temperature_2m_gfs_seamless": [73.4],
                        "temperature_2m_icon_seamless": [75.4],
                        "temperature_2m_gem_seamless": [77.4],
                    },
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
            model_weights_by_station={"KLGA": {"gfs": 0.5, "icon": 0.25, "gem": 0.25}},
        )
        transport = httpx.MockTransport(handler)
        try:
            async with httpx.AsyncClient(transport=transport) as client:
                nws = await daemon._fetch_nws(client)
                metar = await daemon._fetch_metar(client)
                taf = await daemon._fetch_taf(client)
                deterministic = await daemon._fetch_multi_model(client)
                daemon.sink.write([deterministic])
        finally:
            daemon.sink.close()
        assert metar is not None
        assert taf is not None
        assert nws.temperature_c == 22.0
        assert metar.temperature_c == 21.7
        assert set(deterministic.raw["models"]) == {"gfs", "icon", "gem"}
        assert deterministic.raw["blended"]["temperature_2m"] == [74.9]
        event_path = next((tmp_path / "raw" / "weather_daemon").glob("*/events.jsonl"))
        stored = json.loads(event_path.read_text(encoding="utf-8").splitlines()[-1])
        assert set(stored["models"]) == {"gfs", "icon", "gem"}
        assert stored["blended"]["temperature_2m"] == [74.9]
        return nws.product, metar.product, taf.product, deterministic.product

    assert asyncio.run(fetch()) == (
        "latest_observation",
        "metar",
        "taf",
        "multi_model_deterministic_forecast",
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


def test_wrh_client_and_weather_event_preserve_settlement_source_precision(tmp_path) -> None:
    token_requests = 0
    recent_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        if request.url.path.endswith("/apiKey.js"):
            token_requests += 1
            return httpx.Response(
                200,
                request=request,
                text="var mesoToken = 'abc123';",
            )
        assert request.url.host == "api.synopticdata.com"
        assert request.url.params["STID"] == "KLGA"
        recent_requests.append(request.url.params["recent"])
        return httpx.Response(
            200,
            request=request,
            json={
                "STATION": [
                    {
                        "STID": "KLGA",
                        "OBSERVATIONS": {
                            "date_time": [
                                "2026-08-24T16:00:00Z",
                                "2026-08-24T16:05:00Z",
                            ],
                            "air_temp_set_1": [77.36, 77.54],
                        },
                    }
                ]
            },
        )

    async def fetch() -> None:
        daemon = WeatherDaemon(station_id="KLGA", data_dir=tmp_path)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                wrh = WrhTimeseriesClient(http)
                first = await daemon._fetch_wrh(wrh)
                second = await daemon._fetch_wrh(wrh)
        finally:
            daemon.sink.close()
        assert first.product == "wrh_timeseries_observation"
        assert first.source_timestamp_ms == 1_787_587_500_000
        assert abs(first.temperature_c - 25.3) < 1e-9
        assert first.raw["temperature_f"] == "77.54"
        assert first.raw["settlement_source_exact"] is True
        assert second.raw["temperature_f"] == "77.54"

    asyncio.run(fetch())
    assert token_requests == 1
    assert recent_requests == ["1500", "180"]


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


def test_weather_daemon_fixed_intervals_match_measured_update_rates(tmp_path) -> None:
    stations = (
        WeatherStation("KLGA", 40.7769, -73.874, "America/New_York"),
        WeatherStation(
            "ZUCK",
            29.7192,
            106.6417,
            "Asia/Shanghai",
            nws_api_enabled=False,
        ),
    )
    daemon = WeatherDaemon(data_dir=tmp_path, stations=stations)
    try:
        us = daemon.intervals_for_station(stations[0])
        china = daemon.intervals_for_station(stations[1])
        assert us == {
            "wrh_timeseries_observation": 120,
            "nws": 120,
            "metar": 900,
            "taf": 3600,
            "multi_model_deterministic_forecast": 10_800,
        }
        assert china["wrh_timeseries_observation"] == 1800
        assert china["metar"] == 1800
        assert china["taf"] == 3600
    finally:
        daemon.sink.close()


def test_weather_status_reports_degraded_and_stalled(tmp_path) -> None:
    daemon = WeatherDaemon(station_id="KLGA", data_dir=tmp_path)
    try:
        daemon.metrics.state = "running"
        daemon.metrics.requests = 10
        daemon.metrics.last_successful_request_at = datetime.now(UTC).isoformat()
        daemon.metrics.last_new_observation_at = datetime.now(UTC).isoformat()
        daemon.request_outcomes.extend((0.0, False) for _ in range(10))
        # Keep synthetic outcomes inside the rolling window.
        now = __import__("time").monotonic()
        daemon.request_outcomes.clear()
        daemon.request_outcomes.extend((now, index >= 3) for index in range(10))
        daemon._write_status()
        status = json.loads(daemon.status_path.read_text(encoding="utf-8"))
        assert status["state"] == "degraded"
        assert status["recent_error_rate_5m"] == 0.3

        daemon.metrics.last_successful_request_at = datetime.now(UTC).isoformat()
        daemon.metrics.last_new_observation_at = "2026-08-24T00:00:00+00:00"
        daemon._write_status()
        status = json.loads(daemon.status_path.read_text(encoding="utf-8"))
        assert status["state"] == "stalled"
        assert status["base_state"] == "running"
    finally:
        daemon.sink.close()


def test_weather_http_pool_diagnostics_include_proxy_mounts() -> None:
    async def inspect_client() -> dict[str, object]:
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=80, max_keepalive_connections=40)
        ) as client:
            return _http_pool_diagnostics(client)

    diagnostics = asyncio.run(inspect_client())
    assert diagnostics["supported"] is True
    pools = diagnostics["pools"]
    assert isinstance(pools, list)
    assert pools
    assert all("requests_queued" in pool for pool in pools)
    assert all("connections_connecting" in pool for pool in pools)


def test_weather_pool_accounting_gap_marks_running_daemon_degraded(tmp_path) -> None:
    daemon = WeatherDaemon(station_id="KLGA", data_dir=tmp_path)
    try:
        now = datetime.now(UTC).isoformat()
        daemon.metrics.state = "running"
        daemon.metrics.requests = 10
        daemon.metrics.last_successful_request_at = now
        daemon.metrics.last_new_observation_at = now
        daemon.http_pool_health = {
            "sampled_at": now,
            "pool": {"connections_total": 12},
            "process_tcp": {"established": 2},
            "connection_accounting_gap": HTTP_POOL_ACCOUNTING_GAP_DEGRADED_THRESHOLD,
        }
        daemon._write_status()
        status = json.loads(daemon.status_path.read_text(encoding="utf-8"))
        assert status["state"] == "degraded"
        assert status["base_state"] == "running"
        assert status["http_pool_health"]["connection_accounting_gap"] == 10
    finally:
        daemon.sink.close()


def test_weather_http_client_uses_explicit_timeouts() -> None:
    client = WeatherDaemon._new_http_client()
    try:
        assert client.timeout.connect == 10
        assert client.timeout.read == 30
        assert client.timeout.write == 30
        assert client.timeout.pool == 30
    finally:
        asyncio.run(client.aclose())


def test_weather_pool_rebuild_swaps_client_after_requests_drain(
    tmp_path, monkeypatch
) -> None:
    async def rebuild() -> None:
        daemon = WeatherDaemon(station_id="KLGA", data_dir=tmp_path)
        old = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        new = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        daemon.http_client = old
        daemon.wrh_client = WrhTimeseriesClient(old)
        monkeypatch.setattr(daemon, "_new_http_client", lambda: new)
        try:
            await daemon._rebuild_http_client(reason="test accounting gap")
            assert old.is_closed
            assert daemon.http_client is new
            assert daemon.wrh_client is not None
            assert daemon.wrh_client.client is new
            assert daemon.http_pool_rebuild_count == 1
            assert daemon.last_http_pool_rebuild_reason == "test accounting gap"
        finally:
            await new.aclose()
            daemon.http_client = None
            daemon.wrh_client = None
            daemon.sink.close()

    asyncio.run(rebuild())
