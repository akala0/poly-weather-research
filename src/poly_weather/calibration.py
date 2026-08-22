"""No-lookahead bias calibration and forecast verification."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from statistics import NormalDist

from poly_weather.domain import BiasCalibration, CalibrationSample, RollingEvaluation

_EPSILON = 1e-12


def fit_bias_calibration(samples: Sequence[CalibrationSample]) -> BiasCalibration:
    if len(samples) < 2:
        raise ValueError("bias calibration requires at least two samples")
    errors = [sample.error_f for sample in samples]
    bias = statistics.fmean(errors)
    residuals = [error - bias for error in errors]
    residual_std = max(statistics.stdev(residuals), 0.75)
    raw_error_rms = math.sqrt(statistics.fmean(error * error for error in errors))
    return BiasCalibration(
        sample_count=len(samples),
        bias_f=bias,
        residual_std_f=residual_std,
        raw_error_rms_f=max(raw_error_rms, 0.75),
    )


def probability_at_or_above(*, mean_f: float, std_f: float, threshold_f: float) -> float:
    """Probability a whole-degree observation rounds to at least threshold_f."""
    sigma = max(std_f, 0.01)
    cutoff = threshold_f - 0.5
    return 1.0 - NormalDist(mu=mean_f, sigma=sigma).cdf(cutoff)


def brier_score(probability: float, outcome: bool) -> float:
    return (probability - float(outcome)) ** 2


def log_loss(probability: float, outcome: bool) -> float:
    clipped = min(max(probability, _EPSILON), 1.0 - _EPSILON)
    return -math.log(clipped if outcome else 1.0 - clipped)


def _mae(errors: Sequence[float]) -> float:
    return statistics.fmean(abs(value) for value in errors)


def _rmse(errors: Sequence[float]) -> float:
    return math.sqrt(statistics.fmean(value * value for value in errors))


def rolling_origin_evaluate(
    samples: Sequence[CalibrationSample],
    *,
    min_train_size: int,
    test_size: int,
) -> RollingEvaluation:
    """Fit only on earlier target dates and evaluate each following block."""
    if min_train_size < 2 or test_size < 1:
        raise ValueError("invalid rolling-origin sizes")
    ordered = sorted(samples, key=lambda sample: sample.target_date)
    if len(ordered) <= min_train_size:
        raise ValueError("not enough samples for a rolling-origin test fold")

    raw_errors: list[float] = []
    calibrated_errors: list[float] = []
    raw_brier: list[float] = []
    calibrated_brier: list[float] = []
    raw_log: list[float] = []
    calibrated_log: list[float] = []
    fold_count = 0
    for start in range(min_train_size, len(ordered), test_size):
        train = ordered[:start]
        test = ordered[start : start + test_size]
        if not test:
            break
        calibration = fit_bias_calibration(train)
        threshold = statistics.median(sample.observed_high_f for sample in train)
        fold_count += 1
        for sample in test:
            raw_error = sample.observed_high_f - sample.forecast_high_f
            calibrated_mean = sample.forecast_high_f + calibration.bias_f
            calibrated_error = sample.observed_high_f - calibrated_mean
            outcome = sample.observed_high_f >= threshold
            raw_probability = probability_at_or_above(
                mean_f=sample.forecast_high_f,
                std_f=calibration.raw_error_rms_f,
                threshold_f=threshold,
            )
            calibrated_probability = probability_at_or_above(
                mean_f=calibrated_mean,
                std_f=calibration.residual_std_f,
                threshold_f=threshold,
            )
            raw_errors.append(raw_error)
            calibrated_errors.append(calibrated_error)
            raw_brier.append(brier_score(raw_probability, outcome))
            calibrated_brier.append(brier_score(calibrated_probability, outcome))
            raw_log.append(log_loss(raw_probability, outcome))
            calibrated_log.append(log_loss(calibrated_probability, outcome))

    first = ordered[0]
    return RollingEvaluation(
        station_id=first.station_id,
        model=first.model,
        lead_days=first.lead_days,
        folds=fold_count,
        train_min_size=min_train_size,
        test_sample_count=len(raw_errors),
        mae_raw=_mae(raw_errors),
        mae_calibrated=_mae(calibrated_errors),
        rmse_raw=_rmse(raw_errors),
        rmse_calibrated=_rmse(calibrated_errors),
        brier_raw=statistics.fmean(raw_brier),
        brier_calibrated=statistics.fmean(calibrated_brier),
        log_loss_raw=statistics.fmean(raw_log),
        log_loss_calibrated=statistics.fmean(calibrated_log),
    )

