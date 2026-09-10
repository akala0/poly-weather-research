"""Actual synchronous producer paths, isolated disks; no daemon/event loop starts."""
from datetime import UTC, datetime

import pytest
from runtime_health_support import seed_test_chain

from poly_weather.business_readiness import ArchiveCommitProgress, business_progress
from poly_weather.market_stream import MarketWebSocketBot
from poly_weather.market_supervisor import MarketEventSupervisor
from poly_weather.runtime_safety import read_json_with_fallback
from poly_weather.signal_engine import JsonlTail, LiveSignalConfig, LiveSignalEngine
from poly_weather.weather_stream import WeatherDaemon


@pytest.mark.parametrize("kind", ["market", "weather", "supervisor"])
def test_actual_producer_commits_two_durable_samples(tmp_path, kind):
    bot = MarketWebSocketBot(asset_slugs={"yes-1": "bucket-1:Yes"}, data_dir=tmp_path / "market")
    weather = WeatherDaemon(station_id="KLGA", data_dir=tmp_path / "weather")
    supervisor = MarketEventSupervisor(specs=(), bot=bot, data_dir=tmp_path / "supervisor")
    producer = {"market": bot, "weather": weather, "supervisor": supervisor}[kind]
    try:
        for index in range(2):
            if kind == "market":
                records = bot._records({"event_type": "book", "asset_id": "yes-1", "market": "condition-1",
                    "timestamp": "1787390000000", "bids": [{"price": ".4", "size": "10"}],
                    "asks": [{"price": ".44", "size": "10"}]})
                bot.sink.write_raw(records)
            elif kind == "weather":
                weather.sink.write([weather._event(provider="wrh", product="latest_observation",
                    source_time=datetime.now(UTC), temperature_c=25, raw={"test": index})])
            else:
                supervisor._publish_signal_update()
            producer._write_status()
        payload = read_json_with_fallback(producer.status_path)[0]
        first, last = payload["business_sample_previous"], payload["business_sample"]
        assert first["committed_positions"]
        assert business_progress(first, last, stalled_after_seconds=300)["state"] == "advancing"
        producer._write_status()  # A heartbeat alone cannot manufacture progress.
        payload = read_json_with_fallback(producer.status_path)[0]
        assert not business_progress(payload["business_sample_previous"], payload["business_sample"],
                                     stalled_after_seconds=300)["ready"]
    finally:
        bot.sink.close()
        weather.sink.close()


def test_cross_file_fsync_failure_does_not_publish_partial_frontier(tmp_path, monkeypatch):
    progress = ArchiveCommitProgress()
    calls = 0

    def fail_second(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second file durability failure")

    with (tmp_path / "first").open("wb") as first, (tmp_path / "second").open("wb") as second:
        first.write(b"one\n")
        second.write(b"two\n")
        monkeypatch.setattr("poly_weather.business_readiness.os.fsync", fail_second)
        with pytest.raises(OSError, match="second file"):
            progress.commit([first, second])
        assert progress.positions == {}


def test_failed_active_set_publish_does_not_advance(tmp_path, monkeypatch):
    supervisor = MarketEventSupervisor(specs=(), bot=None, data_dir=tmp_path)
    def fail(*args, **kwargs):
        raise OSError("injected publication failure")
    monkeypatch.setattr("poly_weather.market_supervisor.atomic_json_write", fail)
    with pytest.raises(OSError):
        supervisor._publish_signal_update()
    assert supervisor.published_count == 0
    assert supervisor.published_generation is None


def test_signal_progress_requires_successful_state_commit(tmp_path, monkeypatch):
    seed_test_chain(tmp_path, monkeypatch)
    config = LiveSignalConfig(event_id="event", event_slug="event", station_id="KLAX",
        timezone="UTC", target_date=datetime.now(UTC).date(), markets=(),
        contract_verified=True, contract_reason="fixture")
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    path = tmp_path / "fixture-input.jsonl"
    engine.market_tail = JsonlTail(lambda: path)
    try:
        path.write_bytes(b'{"id":1}\n')
        engine.market_tail.poll()
        engine._evaluate()
        engine._write_status()
        first = read_json_with_fallback(engine.status_path)[0]["business_sample"]
        path.write_bytes(b'{"id":1}\n{"id":2}\n')
        engine.market_tail.poll()
        def fail(*args, **kwargs):
            raise OSError("injected signal state commit failure")
        with monkeypatch.context() as patch:
            patch.setattr(engine, "_atomic_json", fail)
            with pytest.raises(OSError):
                engine._evaluate()
        engine._write_status()
        failed = read_json_with_fallback(engine.status_path)[0]["business_sample"]
        assert failed["committed_positions"] == first["committed_positions"]
        assert failed["pending_work"] is True
        engine._evaluate()
        engine._write_status()
        last = read_json_with_fallback(engine.status_path)[0]["business_sample"]
        assert business_progress(first, last, stalled_after_seconds=300)["state"] == "advancing"
    finally:
        engine.sink.close()
