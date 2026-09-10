import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from poly_weather.domain import CalibrationSample, Market
from poly_weather.signal_engine import (
    LiveSignalConfig,
    LiveSignalEngine,
    build_live_calibration,
    deterministic_daily_high_f,
    physical_bucket_state,
    warming_rate_f_per_hour,
)


def test_deterministic_daily_high_extracts_target_day() -> None:
    payload = {
        "hourly": {
            "time": [
                "2026-08-22T00:00",
                "2026-08-22T01:00",
                "2026-08-23T00:00",
            ],
            "temperature_2m": [70.0, 80.0, 75.0],
            "relative_humidity_2m": [50, 45, 55],
        }
    }

    assert deterministic_daily_high_f(payload, date(2026, 8, 22)) == Decimal("80.0")


def test_deterministic_daily_high_returns_none_for_missing_day() -> None:
    payload = {
        "hourly": {
            "time": ["2026-08-23T00:00"],
            "temperature_2m": [75.0],
        }
    }

    assert deterministic_daily_high_f(payload, date(2026, 8, 22)) is None


def test_physical_margin_uses_final_settlement_rounding_and_is_monotonic() -> None:
    first = physical_bucket_state(Decimal("77.4"), upper=77, unit="fahrenheit")
    second = physical_bucket_state(Decimal("77.5"), upper=77, unit="fahrenheit")
    third = physical_bucket_state(Decimal("78.2"), upper=77, unit="fahrenheit")
    assert first == (0.0, "-1..0F", False)
    assert second == (1.0, "> 0F", True)
    assert third == (1.0, "> 0F", True)
    assert first[0] <= second[0] <= third[0]
    assert physical_bucket_state(Decimal("90"), upper=None, unit="fahrenheit") == (
        None,
        None,
        None,
    )


def test_warming_rate_uses_only_past_two_hour_observations() -> None:
    observations = [
        (datetime(2026, 8, 24, 9, tzinfo=UTC), Decimal("71")),
        (datetime(2026, 8, 24, 10, tzinfo=UTC), Decimal("72")),
        (datetime(2026, 8, 24, 11, tzinfo=UTC), Decimal("73")),
    ]
    assert warming_rate_f_per_hour(observations) == 1.0


def test_warming_rate_rejects_short_restart_fragment() -> None:
    observations = [
        (datetime(2026, 8, 24, 10, 55, tzinfo=UTC), Decimal("72")),
        (datetime(2026, 8, 24, 11, tzinfo=UTC), Decimal("73.8")),
    ]
    assert warming_rate_f_per_hour(observations) is None


def test_wrh_backfill_ingest_is_strictly_no_lookahead(tmp_path) -> None:
    market = Market.from_gamma(
        {
            "id": "m1",
            "question": "Will the high be 80-81F?",
            "slug": "event-80-81f",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["yes", "no"]',
        }
    )
    config = LiveSignalConfig(
        event_id="e1",
        event_slug="event",
        station_id="KTEST",
        timezone="UTC",
        target_date=date(2026, 8, 24),
        markets=(market,),
        contract_verified=True,
        contract_reason="verified",
    )
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    received = datetime(2026, 8, 24, 12, tzinfo=UTC)
    try:
        engine._ingest_weather(
            {
                "station_id": "KTEST",
                "collection_mode": "realtime",
                "product": "wrh_timeseries_observation",
                "received_at_ns": int(received.timestamp() * 1_000_000_000),
                "source_timestamp_ms": int(received.timestamp() * 1000),
                "temperature_c": 23.9,
                "raw": {
                    "source_payload": {
                        "STATION": [
                            {
                                "OBSERVATIONS": {
                                    "date_time": [
                                        "2026-08-24T10:00:00Z",
                                        "2026-08-24T11:00:00Z",
                                        "2026-08-24T12:05:00Z",
                                    ],
                                    "air_temp_set_1": [72.0, 75.0, 90.0],
                                }
                            }
                        ]
                    }
                },
            }
        )
        key = ("KTEST", date(2026, 8, 24))
        assert engine.observed_highs[key] == Decimal("75.0")
        assert len(engine.observation_history[key]) == 2
    finally:
        engine.sink.close()


def test_signal_engine_rejects_post_hoc_historical_weather(tmp_path) -> None:
    market = Market.from_gamma(
        {
            "id": "m1",
            "question": "Will the high be 80-81F?",
            "slug": "event-80-81f",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["yes", "no"]',
        }
    )
    config = LiveSignalConfig(
        event_id="e1",
        event_slug="event",
        station_id="KTEST",
        timezone="UTC",
        target_date=date(2026, 8, 24),
        markets=(market,),
        contract_verified=True,
        contract_reason="verified",
    )
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    try:
        with pytest.raises(ValueError, match="refuses historical_backfill"):
            engine._ingest_weather(
                {
                    "station_id": "KTEST",
                    "product": "wrh_timeseries_observation",
                    "collection_mode": "historical_backfill",
                }
            )
    finally:
        engine.sink.close()


def test_signal_book_delta_maintains_full_sorted_depth() -> None:
    book = {
        "bids": [
            {"price": "0.50", "size": "10"},
            {"price": "0.40", "size": "20"},
        ],
        "asks": [{"price": "0.60", "size": "30"}],
    }

    LiveSignalEngine._apply_book_delta(
        book,
        {"side": "BUY", "price": "0.50", "size": "0"},
    )
    LiveSignalEngine._apply_book_delta(
        book,
        {"side": "SELL", "price": "0.55", "size": "5"},
    )

    assert book["bids"] == [{"price": "0.40", "size": "20"}]
    assert book["asks"] == [
        {"price": "0.55", "size": "5"},
        {"price": "0.60", "size": "30"},
    ]


def test_signal_bootstraps_checkpoint_without_replaying_ticks(tmp_path) -> None:
    market = Market.from_gamma(
        {
            "id": "m1",
            "question": "Will the high be 80-81F?",
            "slug": "event-80-81f",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["yes", "no"]',
        }
    )
    config = LiveSignalConfig(
        event_id="e1",
        event_slug="event",
        station_id="KTEST",
        timezone="UTC",
        target_date=date(2026, 8, 24),
        markets=(market,),
        contract_verified=True,
        contract_reason="verified",
    )
    day = datetime.now(UTC).date().isoformat()
    ticks = tmp_path / "raw" / "polymarket_clob_websocket" / day / "events.jsonl"
    ticks.parent.mkdir(parents=True)
    ticks.write_text('{"large":"old tick that must not be replayed"}\n', encoding="utf-8")
    checkpoint = (
        tmp_path / "raw" / "polymarket_book_checkpoints" / day / "events.jsonl"
    )
    checkpoint.parent.mkdir(parents=True)
    row = {
        "event_type": "price_change",
        "asset_id": "no",
        "received_at_ns": 1,
        "best_bid": "0.90",
        "best_ask": "0.92",
        "bids": [{"price": "0.90", "size": "100"}],
        "asks": [{"price": "0.92", "size": "100"}],
        "book_complete": True,
        "raw": {"price_changes": []},
    }
    checkpoint.write_text(json.dumps(row) + "\n", encoding="utf-8")
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    try:
        assert engine.market_tail.offset == ticks.stat().st_size
        assert engine.books["no"]["best_ask"] == "0.92"
        assert engine.books["no"]["book_complete"] is False
        assert "no" in engine.awaiting_authoritative_books
        engine._ingest_market({**row, "received_at_ns": 2})
        assert engine.books["no"]["book_complete"] is True
        assert "no" not in engine.awaiting_authoritative_books
    finally:
        engine.sink.close()


def test_supervisor_generation_hot_reloads_signal_configs(tmp_path) -> None:
    market = Market.from_gamma(
        {
            "id": "market-1",
            "question": "Will the high be 80-81F?",
            "slug": "event-80-81f",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["yes-new", "no-new"]',
        }
    )
    old = LiveSignalConfig(
        event_id="old",
        event_slug="old-event",
        station_id="KTEST",
        timezone="UTC",
        target_date=date(2026, 8, 24),
        markets=(market,),
        contract_verified=True,
        contract_reason="verified",
    )
    new = replace(old, event_id="new", event_slug="new-event")
    update_path = tmp_path / "runtime" / "signal_config_update.json"
    update_path.parent.mkdir(parents=True)
    update_path.write_text(
        json.dumps(
            {
                "generation": 42,
                "events": [
                    {
                        "event_slug": "new-event",
                        "settlement_key": "test",
                        "evidence_sha256": "sha",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    engine = LiveSignalEngine(
        configs=(old,),
        data_dir=tmp_path,
        config_update_path=update_path,
        config_loader=lambda _events: (new,),
    )
    try:
        engine._reload_config_if_needed()
        assert engine.configs == (new,)
        assert engine.asset_ids == {"yes-new", "no-new"}
        assert engine.metrics.config_updates == 1
        assert engine.metrics.config_generation == 42
    finally:
        engine.sink.close()


def _calibration_samples(count: int, *, error_f: float) -> list[CalibrationSample]:
    first = date(2026, 1, 1)
    return [
        CalibrationSample(
            station_id="KTEST",
            target_date=first + timedelta(days=index),
            lead_days=1,
            model="gfs_seamless",
            forecast_high_f=70.0 + index % 12,
            observed_high_f=70.0 + index % 12 + error_f,
            forecast_source="test",
            truth_source="test",
            truth_kind="final",
            ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for index in range(count)
    ]


def test_live_calibration_requires_walk_forward_history() -> None:
    calibration = build_live_calibration(
        _calibration_samples(30, error_f=2.0),
        station_id="KTEST",
        lead_days=1,
        min_samples=30,
    )

    assert calibration is not None
    assert calibration.ready is False
    assert calibration.strategy == "insufficient_history"


def test_live_calibration_accepts_bias_only_after_walk_forward_improvement() -> None:
    calibration = build_live_calibration(
        _calibration_samples(80, error_f=2.0),
        station_id="KTEST",
        lead_days=1,
        min_samples=30,
    )

    assert calibration is not None
    assert calibration.ready is True
    assert calibration.apply_bias is True
    assert calibration.strategy == "bias_corrected"
    assert calibration.validation_test_samples == 50
    assert calibration.rmse_calibrated == 0.0


def test_live_calibration_learns_station_multi_model_weights() -> None:
    samples = [
        sample.model_copy(
            update={
                "model": "multi_model_blend",
                "forecast_high_f_by_model": {
                    "gfs": sample.observed_high_f - 1.0,
                    "icon": sample.observed_high_f - 2.0,
                    "gem": sample.observed_high_f - 4.0,
                },
            }
        )
        for sample in _calibration_samples(80, error_f=2.0)
    ]

    calibration = build_live_calibration(
        samples,
        station_id="KTEST",
        lead_days=1,
        min_samples=30,
    )

    assert calibration is not None
    assert calibration.model_weights is not None
    assert abs(calibration.model_weights["gfs"] - 4 / 7) < 1e-12


def test_live_calibration_rejects_lookahead_day_zero() -> None:
    with pytest.raises(ValueError, match="look-ahead"):
        build_live_calibration(
            _calibration_samples(30, error_f=2.0),
            station_id="KTEST",
            lead_days=0,
            min_samples=30,
        )


def test_implausible_edge_forces_skip_and_blocks_paper_alert(tmp_path, monkeypatch) -> None:
    from runtime_health_support import seed_test_chain

    def market(market_id: str, slug_suffix: str) -> Market:
        return Market.from_gamma(
            {
                "id": market_id,
                "question": slug_suffix,
                "slug": f"event-{slug_suffix}",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.01", "0.99"]',
                "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
            }
        )

    calibration = build_live_calibration(
        _calibration_samples(80, error_f=2.0),
        station_id="KTEST",
        lead_days=1,
        min_samples=30,
    )
    assert calibration is not None and calibration.ready
    calibration = replace(
        calibration,
        model_weights={"gfs": 0.5, "icon": 0.3, "gem": 0.2},
    )
    config = LiveSignalConfig(
        event_id="event-1",
        event_slug="event",
        station_id="KTEST",
        timezone="UTC",
        target_date=date(2026, 8, 22),
        markets=(
            market("a", "79forbelow"),
            market("b", "80-81f"),
            market("c", "82forhigher"),
        ),
        contract_verified=True,
        contract_reason="verified",
        calibration=calibration,
    )
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path)
    generated_at = datetime(2026, 8, 22, 12, tzinfo=UTC)
    source_timestamp_ms = int(generated_at.timestamp() * 1000)
    try:
        engine.weather[("KTEST", "latest_observation")] = {
            "temperature_c": 26.0,
            "source_timestamp_ms": source_timestamp_ms,
        }
        engine.weather[("KTEST", "metar")] = {
            "temperature_c": 26.0,
            "source_timestamp_ms": source_timestamp_ms,
        }
        engine.weather[("KTEST", "wrh_timeseries_observation")] = {
            "temperature_c": 26.0,
            "source_timestamp_ms": source_timestamp_ms,
        }
        engine.weather[("KTEST", "multi_model_deterministic_forecast")] = {
            "raw": {
                "run_initialization": "2026-08-21T12:00:00+00:00",
                "lead_days": 1,
                "blended": {
                    "time": ["2026-08-22T12:00"],
                    "temperature_2m": [80.0],
                    "weights": {"gfs": 0.5, "icon": 0.3, "gem": 0.2},
                }
            }
        }
        # Exercise actual envelope ingestion, not the unversioned diagnostic cache.
        envelopes = tuple(engine.weather.items())
        engine.weather.clear()
        for (station_id, product), payload in envelopes:
            engine._ingest_weather({**payload, "station_id": station_id, "product": product,
                                    "collection_mode": "realtime", "source_timestamp_ms": source_timestamp_ms,
                                    "received_at_ns": int(generated_at.timestamp()) * 10**9,
                                    "received_at": generated_at.isoformat()})
        for item in config.markets:
            yes_token, no_token = item.clob_token_ids
            engine.books[yes_token] = {
                "best_bid": "0.005",
                "best_ask": "0.01",
                "bids": [{"price": "0.005", "size": "200000"}],
                "asks": [{"price": "0.01", "size": "200000"}],
                "book_complete": True,
            }
            engine.books[no_token] = {
                "best_bid": "0.98",
                "best_ask": "0.99",
                "bids": [{"price": "0.98", "size": "2000"}],
                "asks": [{"price": "0.99", "size": "2000"}],
                "book_complete": True,
            }
        engine._atomic_json(
            tmp_path / "runtime" / "polymarket_ws_status.json",
            {"state": "connected", "updated_at": generated_at.isoformat()},
        )
        engine._atomic_json(
            tmp_path / "runtime" / "weather_daemon_status.json",
            {"state": "running", "updated_at": generated_at.isoformat()},
        )

        seed_test_chain(tmp_path, monkeypatch, now=generated_at)
        result = engine._event_signal(config, generated_at)
    finally:
        engine.sink.close()

    assert "implausible edge suggests model error" in result["reasons"]
    assert result["status"] == "warning"
    assert result["paper_alert_eligible"] is False
    assert result["top_research_candidate"]["edge_gate_blocked"] is True
    assert result["top_research_candidate"]["action"] == "skip"
    estimates = result["top_research_candidate"]["execution_estimates"]
    assert [estimate["size_usd"] for estimate in estimates] == [50.0, 200.0, 1000.0]
    assert all(estimate["estimated_fill_yes"] == 0.01 for estimate in estimates)
    assert all(estimate["filled_fraction_yes"] == 1.0 for estimate in estimates)
    assert result["execution_enabled"] is False
