"""Weather qualification through the actual daemon observation converter."""

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from poly_weather.weather_market_join import parse_weather_observation
from poly_weather.weather_stream import WeatherEvent


def producer_row():
    at = datetime(2026, 9, 2, 12, tzinfo=UTC)
    event = WeatherEvent(run_id="test-run", sequence=1, received_at_ns=int(at.timestamp()) * 10**9,
                         source_timestamp_ms=int(at.timestamp()) * 1000 - 1000,
                         provider="nws", product="latest_observation", station_id="KLAX",
                         temperature_c=25.1, latency_ms=1000, raw={"id": "observation-1"})
    return {**asdict(event), "received_at": at.isoformat()}


@pytest.mark.parametrize("damage", ["mode_missing", "backfill", "receipt_missing", "naive",
                                   "source_after_receipt", "nonfinite", "clock_conflict", "inexact_iso", "invalid_temperature"])
@pytest.mark.parametrize("consumer", ["join", "information", "signal"])
def test_we_invalid_weather_cannot_become_realtime(tmp_path, damage, consumer):
    row = producer_row()
    if damage == "mode_missing":
        row.pop("collection_mode")
    elif damage == "backfill":
        row["collection_mode"] = "historical_backfill"
    elif damage == "receipt_missing":
        row.pop("received_at")
        row.pop("received_at_ns")
    elif damage == "naive":
        row["received_at"] = "2026-09-02T12:00:00"
    elif damage == "source_after_receipt":
        row["source_timestamp_ms"] += 2000
    elif damage == "nonfinite":
        row["temperature_f"] = "NaN"
    elif damage == "inexact_iso":
        row["received_at"] = "2026-09-02T12:00:00.000000001Z"
    elif damage == "invalid_temperature":
        row["temperature_f"] = "corrupt"
    else:
        row["received_at_ns"] += 10**9
    if consumer == "join":
        with pytest.raises(ValueError):
            parse_weather_observation(row)
    elif consumer == "information":
        from poly_weather.information_clock import InformationClock, iter_information_events

        events = tuple(iter_information_events([row]))
        assert len(events) == 1
        assert events[0].receipt_verified is False
        assert events[0].metadata["qualification_reason"]
        clock = InformationClock()
        assert not clock.ingest_many(events)
        assert clock.events == []
    else:
        from poly_weather.signal_engine import LiveSignalConfig, LiveSignalEngine

        config = LiveSignalConfig(event_id="e", event_slug="event", station_id="KLAX",
                                  timezone="UTC", target_date=datetime(2026, 9, 2).date(),
                                  markets=(), contract_verified=True, contract_reason="test")
        engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
        try:
            with pytest.raises(ValueError):
                engine._ingest_weather(row)
            assert engine.weather == {}
            assert engine.observed_highs == {}
        finally:
            engine.sink.close()


def test_we_real_producer_nanoseconds_never_round_receipt_early():
    row = producer_row()
    row["received_at_ns"] += 1
    observation = parse_weather_observation(row)
    assert observation.received_at.microsecond == 1
