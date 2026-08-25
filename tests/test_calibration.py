from datetime import UTC, date, datetime, timedelta

from poly_weather.calibration import (
    evaluate_bucket_skill,
    learn_model_weights,
    rolling_origin_evaluate,
)
from poly_weather.domain import CalibrationSample


def _samples(count: int = 28) -> list[CalibrationSample]:
    start = date(2026, 6, 1)
    rows = []
    for index in range(count):
        observed = 70.0 + float(index % 10)
        rows.append(
            CalibrationSample(
                station_id="KLGA",
                target_date=start + timedelta(days=index),
                lead_days=1,
                model="gfs_seamless",
                forecast_high_f=observed - 2.0,
                observed_high_f=observed,
                forecast_source="Open-Meteo Previous Runs",
                truth_source="NOAA NCEI Daily Summaries",
                truth_kind="same_station_noaa_proxy_not_exact_wunderground",
                ingested_at=datetime.now(UTC),
            )
        )
    return rows


def test_rolling_origin_bias_correction_uses_only_prior_dates() -> None:
    result = rolling_origin_evaluate(_samples(), min_train_size=14, test_size=7)

    assert result.folds == 2
    assert result.test_sample_count == 14
    assert result.no_lookahead is True
    assert result.mae_raw == 2.0
    assert result.mae_calibrated == 0.0
    assert result.rmse_calibrated < result.rmse_raw
    assert result.brier_calibrated < result.brier_raw


def test_bucket_skill_scores_true_two_degree_bucket_walk_forward() -> None:
    result = evaluate_bucket_skill(_samples(35), min_train_size=30)

    assert result["test_sample_count"] == 5
    assert len(result["true_bucket_probs"]) == 5
    assert result["mean_true_bucket_prob"] > 0.70
    assert result["median_true_bucket_prob"] > 0.70
    assert result["hit_rate"] == 1.0


def test_bucket_skill_detects_four_degree_regime_shift() -> None:
    samples = _samples(35)
    shifted = [
        sample.model_copy(
            update={"forecast_high_f": sample.observed_high_f - 6.0}
        )
        if index >= 30
        else sample
        for index, sample in enumerate(samples)
    ]

    result = evaluate_bucket_skill(shifted, min_train_size=30)

    assert result["test_sample_count"] == 5
    assert result["mean_true_bucket_prob"] < 0.20
    assert result["hit_rate"] == 0.0


def test_model_weights_are_inverse_mae_and_normalized() -> None:
    samples = [
        sample.model_copy(
            update={
                "forecast_high_f_by_model": {
                    "gfs": sample.observed_high_f - 1.0,
                    "icon": sample.observed_high_f - 2.0,
                    "gem": sample.observed_high_f - 4.0,
                }
            }
        )
        for sample in _samples(10)
    ]

    weights = learn_model_weights(samples)

    assert abs(weights["gfs"] - 4 / 7) < 1e-12
    assert abs(weights["icon"] - 2 / 7) < 1e-12
    assert abs(weights["gem"] - 1 / 7) < 1e-12
    assert abs(sum(weights.values()) - 1.0) < 1e-12
