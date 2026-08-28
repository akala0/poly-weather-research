import json
from datetime import UTC, date, datetime, timedelta

import duckdb

from poly_weather.bias_significance_audit import (
    audit_bias_significance,
    bias_standard_error,
    load_calibration_samples,
    render_bias_significance_audit,
    rolling_significance_gate_evaluate,
)
from poly_weather.domain import CalibrationSample


def _samples(errors: list[float]) -> list[CalibrationSample]:
    start = date(2026, 6, 1)
    return [
        CalibrationSample(
            station_id="KLAX",
            target_date=start + timedelta(days=index),
            lead_days=1,
            model="gfs_seamless",
            forecast_high_f=75.0 - error,
            observed_high_f=75.0,
            forecast_source="historical test fixture",
            truth_source="historical test fixture",
            truth_kind="test",
            ingested_at=datetime.now(UTC),
        )
        for index, error in enumerate(errors)
    ]


def test_bias_standard_error_reports_constant_nonzero_bias_as_infinite_z() -> None:
    standard_error, z_score = bias_standard_error(_samples([2.0] * 4))

    assert standard_error == 0.0
    assert z_score == float("inf")


def test_significance_walk_forward_uses_only_prior_dates() -> None:
    samples = _samples([1.0] * 20 + [-9.0] * 10)

    result = rolling_significance_gate_evaluate(samples, min_train_size=20, test_size=10)

    assert result is not None
    assert result["strict_no_lookahead"] is True
    assert result["folds"][0]["mean_bias_f"] == 1.0
    assert result["folds"][0]["significant_under_2se"] is True


def test_audit_identifies_existing_unconditional_zero_bias_as_2se_difference() -> None:
    result = audit_bias_significance(
        _samples([1.0, -1.0] * 20), min_train_size=20, test_size=10
    )

    assert result["execution_enabled"] is False
    assert result["strict_no_lookahead"] is True
    row = result["rows"][0]
    assert row["current_bias_applied"] is True
    assert row["significant_under_2se"] is False
    assert row["would_disable_current_bias_under_2se"] is True
    assert row["significance_gate_walk_forward"]["test_sample_count"] == 20


def test_load_calibration_samples_decodes_duckdb_json_object(tmp_path) -> None:
    database = tmp_path / "research.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            """
            CREATE TABLE calibration_samples (
                station_id VARCHAR,
                target_date DATE,
                lead_days INTEGER,
                model VARCHAR,
                forecast_high_f DOUBLE,
                observed_high_f DOUBLE,
                forecast_source VARCHAR,
                truth_source VARCHAR,
                truth_kind VARCHAR,
                ingested_at TIMESTAMP WITH TIME ZONE,
                forecast_high_f_by_model JSON
            )
            """
        )
        connection.execute(
            "INSERT INTO calibration_samples VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "KLAX",
                date(2026, 8, 1),
                1,
                "multi_model_blend",
                75.0,
                76.0,
                "test",
                "test",
                "test",
                datetime.now(UTC),
                json.dumps({"gfs": 75.0, "icon": 76.0}),
            ],
        )
    finally:
        connection.close()

    samples = load_calibration_samples(database)

    assert len(samples) == 1
    assert samples[0].forecast_high_f_by_model == {"gfs": 75.0, "icon": 76.0}


def test_rendered_audit_marks_itself_read_only(tmp_path) -> None:
    result = audit_bias_significance(_samples([1.0, -1.0] * 20), min_train_size=20)
    output = tmp_path / "bias_audit.md"

    render_bias_significance_audit(result, output)

    text = output.read_text(encoding="utf-8")
    assert "Audit only" in text
    assert "Walk-forward comparison" in text
    assert "execution_enabled=false" in text
