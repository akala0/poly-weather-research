"""Production converters must not retroactively consume a late revision."""

from datetime import UTC, datetime, timedelta

import pytest
from runtime_health_support import seed_test_chain
from test_weather_evidence_closure import producer_row

from poly_weather.information_clock import iter_information_events, strict_information_cutoff
from poly_weather.signal_engine import LiveSignalConfig, LiveSignalEngine
from poly_weather.weather_market_join import align_weather_to_snapshots, parse_weather_observation
from poly_weather.weather_provenance import forecast_vintage


@pytest.mark.parametrize("damage,reason", [(None, "VERIFIED_EXPLICIT_FORECAST_VINTAGE"),
    ("missing", "UNKNOWN_FIXED_FORECAST_VINTAGE"), ("second_model", "UNKNOWN_FIXED_FORECAST_VINTAGE"),
    ("future", "FORECAST_INITIALIZATION_AFTER_RECEIPT"), ("naive", "UNKNOWN_FIXED_FORECAST_VINTAGE"),
    (0, "INVALID_FORECAST_LEAD_DAYS"), (1.5, "INVALID_FORECAST_LEAD_DAYS"),
    (True, "INVALID_FORECAST_LEAD_DAYS")])
def test_fixed_forecast_requires_every_model_initialization(damage, reason):
    model = {"initialization_time": "2026-09-01T00:00:00Z", "lead_days": 1}
    row = {**producer_row(), "product": "multi_model_deterministic_forecast",
           "source_timestamp_ms": None, "raw": {"models": {"gfs": model}}}
    if damage == "missing":
        model.pop("initialization_time")
    elif damage == "second_model":
        row["raw"]["models"]["ecmwf"] = {"hourly": {"time": ["2026-09-01T00:00:00Z"]}}
    elif damage == "future":
        model["initialization_time"] = "2026-09-03T00:00:00Z"
    elif damage == "naive":
        model["initialization_time"] = "2026-09-01T00:00:00"
    elif damage is not None:
        model["lead_days"] = damage
    assert forecast_vintage(row)[1] == reason


def test_we_signal_information_join_prefix_survives_late_high(tmp_path, monkeypatch):
    cutoff = datetime(2026, 9, 2, 12, 5, tzinfo=UTC)
    seed_test_chain(tmp_path, monkeypatch, now=cutoff)
    config = LiveSignalConfig(event_id="event", event_slug="event", station_id="KLAX",
                              timezone="UTC", target_date=cutoff.date(), markets=(),
                              contract_verified=True, contract_reason="isolated rule evidence")
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    first = {**producer_row(), "product": "wrh_timeseries_observation"}
    late = {**first, "temperature_c": 45, "received_at": (cutoff + timedelta(hours=1)).isoformat(),
            "received_at_ns": int((cutoff + timedelta(hours=1)).timestamp()) * 10**9,
            "raw": {"id": "late-qc-revision"}}
    snapshot = {"observed_at": cutoff.isoformat(), "station_id": "KLAX", "event_id": "event",
                "token_id": "token", "bids": [{"price": ".7", "size": "100"}],
                "asks": [{"price": ".8", "size": "100"}]}
    try:
        engine._ingest_weather(first)
        original_signal = engine._event_signal(config, cutoff)
        original_join = align_weather_to_snapshots([snapshot], [parse_weather_observation(first)])
        original_information = strict_information_cutoff(iter_information_events([first]), cutoff)
        engine._ingest_weather(late)
        assert align_weather_to_snapshots([snapshot], [parse_weather_observation(first), parse_weather_observation(late)]) == original_join
        assert strict_information_cutoff(iter_information_events([first, late]), cutoff) == original_information
        assert engine._event_signal(config, cutoff) == original_signal
    finally:
        engine.sink.close()


def test_forecast_append_preserves_actual_signal_and_information_prefix(tmp_path, monkeypatch):
    cutoff = datetime(2026, 9, 2, 12, 5, tzinfo=UTC)
    seed_test_chain(tmp_path, monkeypatch, now=cutoff)
    config = LiveSignalConfig(event_id="event", event_slug="event", station_id="KLAX",
        timezone="UTC", target_date=cutoff.date(), markets=(), contract_verified=True, contract_reason="fixture")
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    first = {**producer_row(), "product": "multi_model_deterministic_forecast", "source_timestamp_ms": None,
        "raw": {"initialization_time": "2026-09-01T00:00:00Z", "lead_days": 1,
                "blended": {"time": ["2026-09-02T12:00"], "temperature_2m": [80]}}}
    late = {**first, "received_at": (cutoff + timedelta(hours=1)).isoformat(),
        "received_at_ns": int((cutoff + timedelta(hours=1)).timestamp()) * 10**9,
        "raw": {**first["raw"], "blended": {"time": ["2026-09-02T12:00"], "temperature_2m": [110]}}}
    try:
        engine._ingest_weather(first)
        before = engine._event_signal(config, cutoff)
        information = strict_information_cutoff(iter_information_events([first]), cutoff)
        assert information  # A valid fixed-vintage fixture, not an empty-prefix proof.
        engine._ingest_weather(late)
        assert engine._event_signal(config, cutoff) == before
        assert strict_information_cutoff(iter_information_events([first, late]), cutoff) == information
    finally:
        engine.sink.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_nested_wrh_invalid_payload_cannot_partially_mutate_signal(tmp_path, value):
    config = LiveSignalConfig(event_id="event", event_slug="event", station_id="KLAX",
        timezone="UTC", target_date=datetime(2026, 9, 2).date(), markets=(),
        contract_verified=True, contract_reason="fixture")
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    row = {**producer_row(), "product": "wrh_timeseries_observation", "raw": {"source_payload": {
        "STATION": [{"OBSERVATIONS": {"date_time": ["2026-09-02T11:59:00Z"], "air_temp_set_1": [value]}}]}}}
    try:
        with pytest.raises(ValueError, match="INVALID_WRH"):
            engine._ingest_weather(row)
        assert not engine.weather and not engine.weather_versions and not engine.observed_highs
    finally:
        engine.sink.close()
