from datetime import datetime, time

import pytest

from poly_weather.intraday_reversal import (
    TemperatureObservation,
    certainty_curve_points,
    certainty_summary,
    daily_reversals,
    load_iem_asos_csv,
    percentile,
    reversal_summary,
)


def _observation(clock: str, temperature_f: float) -> TemperatureObservation:
    return TemperatureObservation(
        station_id="KLAX",
        valid=datetime.fromisoformat(f"2026-08-20T{clock}"),
        temperature_f=temperature_f,
    )


def test_daily_reversal_uses_nearest_observation_and_first_high_time() -> None:
    result = daily_reversals(
        [
            _observation("14:00", 82.0),
            _observation("16:10", 77.0),
            _observation("16:48", 78.0),
            _observation("17:20", 80.0),
            _observation("17:50", 82.0),
        ]
    )[0]

    assert result.decision_observed_at.hour == 16
    assert result.decision_observed_at.minute == 48
    assert result.remaining_warming_f == 4.0
    assert result.post_decision_warming_f == 4.0
    assert result.final_high_first_at.hour == 14


def test_requested_metric_does_not_prove_post_decision_warming() -> None:
    result = daily_reversals(
        [
            _observation("13:00", 85.0),
            _observation("16:31", 80.0),
            _observation("17:00", 80.5),
            _observation("23:00", 75.0),
        ]
    )[0]
    summary = reversal_summary([result])

    assert result.remaining_warming_f == 5.0
    assert result.post_decision_warming_f == 0.5
    assert summary["reversal_frequency"] == 1.0
    assert summary["post_decision_reversal_frequency"] == 0.0


def test_percentile_uses_linear_interpolation() -> None:
    assert percentile([0.0, 10.0, 20.0, 30.0], 0.75) == pytest.approx(22.5)


def test_certainty_curve_is_strictly_no_lookahead_and_uses_running_high() -> None:
    points = certainty_curve_points(
        [
            _observation("09:00", 76.0),
            _observation("09:55", 74.0),
            _observation("10:01", 78.0),
            _observation("12:00", 78.0),
        ],
        scan_times=(time(10, 0), time(10, 30)),
    )

    at_1000 = points[time(10, 0)][0]
    at_1030 = points[time(10, 30)][0]
    assert at_1000.last_observation_at.minute == 55
    assert at_1000.observed_high_f == 76.0
    assert not at_1000.bucket_hit
    assert at_1030.last_observation_at.minute == 1
    assert at_1030.observed_high_f == 78.0
    assert at_1030.bucket_hit
    assert certainty_summary([at_1000])["observation_lag_p50_minutes"] == 5.0


def test_certainty_curve_skips_day_without_observation_by_scan_time() -> None:
    points = certainty_curve_points(
        [_observation("10:01", 78.0)],
        scan_times=(time(10, 0),),
    )

    assert points[time(10, 0)] == []


def test_certainty_curve_supports_one_degree_celsius_buckets() -> None:
    points = certainty_curve_points(
        [
            _observation("09:00", 87.8),  # 31C
            _observation("10:01", 89.6),  # 32C
        ],
        scan_times=(time(10, 0), time(10, 30)),
        unit="celsius",
        bucket_width_degrees=1,
    )
    assert points[time(10, 0)][0].bucket_hit is False
    assert points[time(10, 30)][0].bucket_hit is True


def test_iem_loader_skips_null_and_empty_temperature_rows(tmp_path) -> None:
    csv_path = tmp_path / "observations.csv"
    csv_path.write_text(
        "station,valid,tmpf\n"
        "LAX,2026-08-20 09:00,null\n"
        "LAX,2026-08-20 09:05,\n"
        "LAX,2026-08-20 09:53,76.0\n",
        encoding="utf-8",
    )

    observations = load_iem_asos_csv(csv_path, station_id="KLAX")

    assert len(observations) == 1
    assert observations[0].temperature_f == 76.0
