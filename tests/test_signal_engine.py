from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from poly_weather.domain import CalibrationSample
from poly_weather.signal_engine import build_live_calibration, gefs_daily_highs_f


def test_gefs_daily_highs_extracts_control_and_members_for_target_day() -> None:
    payload = {
        "hourly": {
            "time": [
                "2026-08-22T00:00",
                "2026-08-22T01:00",
                "2026-08-23T00:00",
            ],
            "temperature_2m": [70.0, 80.0, 75.0],
            "temperature_2m_member01": [69.0, 81.5, 76.0],
            "relative_humidity_2m": [50, 45, 55],
        }
    }

    assert gefs_daily_highs_f(payload, date(2026, 8, 22)) == (
        Decimal("80.0"),
        Decimal("81.5"),
    )


def test_gefs_daily_highs_returns_empty_for_missing_day() -> None:
    payload = {
        "hourly": {
            "time": ["2026-08-23T00:00"],
            "temperature_2m": [75.0],
        }
    }

    assert gefs_daily_highs_f(payload, date(2026, 8, 22)) == ()


def _calibration_samples(count: int, *, error_f: float) -> list[CalibrationSample]:
    first = date(2026, 1, 1)
    return [
        CalibrationSample(
            station_id="KTEST",
            target_date=first + timedelta(days=index),
            lead_days=0,
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
        lead_days=0,
        min_samples=30,
    )

    assert calibration is not None
    assert calibration.ready is False
    assert calibration.strategy == "insufficient_history"


def test_live_calibration_accepts_bias_only_after_walk_forward_improvement() -> None:
    calibration = build_live_calibration(
        _calibration_samples(80, error_f=2.0),
        station_id="KTEST",
        lead_days=0,
        min_samples=30,
    )

    assert calibration is not None
    assert calibration.ready is True
    assert calibration.apply_bias is True
    assert calibration.strategy == "bias_corrected"
    assert calibration.validation_test_samples == 50
    assert calibration.rmse_calibrated == 0.0
