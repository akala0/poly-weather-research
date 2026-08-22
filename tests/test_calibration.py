from datetime import UTC, date, datetime, timedelta

from poly_weather.calibration import rolling_origin_evaluate
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

