import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.shadow_orders import BookSnapshot
from poly_weather.weather_market_join import (
    WeatherObservation,
    align_weather_to_snapshots,
    load_realtime_weather_observations,
)


def _snapshot(at: datetime, ask: str = "0.70") -> BookSnapshot:
    return BookSnapshot(
        timestamp=at,
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-08-27",
        season_version="heat-v1",
        bids=(("0.68", "100"),),
        asks=((ask, "100"),),
    )


def _observation(source: datetime, received: datetime, value: str, identity: str) -> WeatherObservation:
    return WeatherObservation(
        station_id="KLAX",
        product="wrh_timeseries_observation",
        source_timestamp=source,
        received_at=received,
        temperature_f=Decimal(value),
        observation_id=identity,
    )


def test_weather_join_requires_both_source_and_receipt_cutoffs() -> None:
    base = datetime(2026, 8, 27, 12, tzinfo=UTC)
    observations = (
        _observation(base - timedelta(minutes=2), base + timedelta(seconds=1), "80", "late"),
        _observation(base - timedelta(minutes=1), base - timedelta(seconds=1), "79", "ok"),
    )
    rows, reasons = align_weather_to_snapshots((_snapshot(base),), observations)
    assert rows[0]["metadata"]["weather_observation_id"] == "ok"
    assert reasons["aligned_new_observation"] == 1


def test_new_observation_is_marked_once_and_lag_uses_real_ask() -> None:
    base = datetime(2026, 8, 27, 12, tzinfo=UTC)
    observations = (
        _observation(base - timedelta(minutes=1), base - timedelta(minutes=1), "79", "one"),
        _observation(base + timedelta(minutes=1), base + timedelta(minutes=1), "80", "two"),
    )
    rows, reasons = align_weather_to_snapshots(
        (_snapshot(base, "0.70"), _snapshot(base + timedelta(minutes=2), "0.70")), observations
    )
    assert rows[0]["metadata"]["weather_observation_new"] is True
    assert rows[1]["metadata"]["weather_observation_new"] is True
    assert rows[1]["metadata"]["weather_market_lag"] is True
    assert reasons["aligned_new_observation"] == 2


def test_join_state_does_not_retrigger_same_observation_across_batches() -> None:
    base = datetime(2026, 8, 27, 12, tzinfo=UTC)
    observations = (
        _observation(base - timedelta(minutes=1), base - timedelta(minutes=1), "79", "one"),
        _observation(base + timedelta(minutes=1), base + timedelta(minutes=1), "80", "two"),
    )
    state: dict[str, object] = {}
    first, _ = align_weather_to_snapshots(
        (_snapshot(base + timedelta(minutes=2)),), observations, state=state
    )
    second, reasons = align_weather_to_snapshots(
        (_snapshot(base + timedelta(minutes=3)),), observations, state=state
    )
    assert first[0]["metadata"]["weather_observation_new"] is True
    assert second[0]["metadata"]["weather_observation_new"] is False
    assert second[0]["metadata"]["weather_market_lag"] is False
    assert reasons["aligned_repeated_observation"] == 1


def test_loader_skips_historical_backfill(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    row = {
        "station_id": "KLAX",
        "product": "wrh_timeseries_observation",
        "source_timestamp_ms": 1787820000000,
        "received_at": "2026-08-27T10:00:00+00:00",
        "temperature_c": 25,
        "collection_mode": "historical_backfill",
        "raw": {},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert load_realtime_weather_observations([path]) == ()


def test_loader_converts_normalized_celsius_and_nested_nws_temperature(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    rows = [
        {
            "station_id": "KLAX",
            "product": "metar",
            "collection_mode": "realtime",
            "source_timestamp_ms": 1787820000000,
            "received_at": "2026-08-27T10:00:00+00:00",
            "temperature_c": 25,
            "raw": [],
        },
        {
            "station_id": "KLAX",
            "product": "latest_observation",
            "collection_mode": "realtime",
            "source_timestamp_ms": 1787820060000,
            "received_at": "2026-08-27T10:01:00+00:00",
            "raw": {"id": "nws-1", "properties": {"temperature": {"value": 26}}},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    observations = load_realtime_weather_observations([path])
    assert [row.temperature_f for row in observations] == [Decimal("77"), Decimal("78.8")]
