"""Read-only audit of the proposed statistical-significance bias gate.

This module deliberately does not alter live calibration behaviour.  It makes
the current sample-count plus walk-forward gate explicit and compares it with
the additional ``abs(mean_bias) / SE > 2`` rule using only prior dates in each
walk-forward fold.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from poly_weather.calibration import (
    brier_score,
    fit_bias_calibration,
    log_loss,
    probability_at_or_above,
    rolling_origin_evaluate,
)
from poly_weather.domain import CalibrationSample
from poly_weather.signal_engine import build_live_calibration


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _rmse(values: Sequence[float]) -> float | None:
    return math.sqrt(statistics.fmean(value * value for value in values)) if values else None


def _mae(values: Sequence[float]) -> float | None:
    return statistics.fmean(abs(value) for value in values) if values else None


def bias_standard_error(samples: Sequence[CalibrationSample]) -> tuple[float | None, float | None]:
    """Return standard error and absolute z-score for the mean forecast error."""
    errors = [sample.error_f for sample in samples]
    if len(errors) < 2:
        return None, None
    sample_std = statistics.stdev(errors)
    standard_error = sample_std / math.sqrt(len(errors))
    mean_bias = statistics.fmean(errors)
    if standard_error == 0:
        return standard_error, math.inf if mean_bias != 0 else 0.0
    return standard_error, abs(mean_bias) / standard_error


def _metric_summary(
    errors: Sequence[float], briers: Sequence[float], logs: Sequence[float]
) -> dict[str, float | None]:
    return {
        "mae": _mae(errors),
        "rmse": _rmse(errors),
        "brier": _mean(briers),
        "log_loss": _mean(logs),
    }


def rolling_significance_gate_evaluate(
    samples: Sequence[CalibrationSample],
    *,
    min_train_size: int = 30,
    test_size: int = 10,
    significance_threshold: float = 2.0,
) -> dict[str, Any] | None:
    """Compare unconditional versus significance-gated correction OOS.

    Every fold estimates its bias and SE from earlier target dates only.  The
    current replay mirrors the existing rolling evaluator's unconditional bias
    estimate; the proposed replay leaves a fold raw if its prior bias is not
    significant.  Neither path changes live configuration.
    """
    ordered = sorted(samples, key=lambda sample: sample.target_date)
    if len(ordered) <= min_train_size:
        return None
    raw_errors: list[float] = []
    current_errors: list[float] = []
    gated_errors: list[float] = []
    raw_brier: list[float] = []
    current_brier: list[float] = []
    gated_brier: list[float] = []
    raw_log: list[float] = []
    current_log: list[float] = []
    gated_log: list[float] = []
    insignificant_test_count = 0
    insignificant_bias_mae_worse = 0
    insignificant_bias_rmse_worse = 0
    folds: list[dict[str, Any]] = []
    for start in range(min_train_size, len(ordered), test_size):
        train = ordered[:start]
        test = ordered[start : start + test_size]
        if not test:
            continue
        calibration = fit_bias_calibration(train)
        standard_error, z_score = bias_standard_error(train)
        significant = z_score is not None and z_score > significance_threshold
        threshold = statistics.median(sample.observed_high_f for sample in train)
        fold_raw_errors: list[float] = []
        fold_current_errors: list[float] = []
        for sample in test:
            raw_error = sample.observed_high_f - sample.forecast_high_f
            corrected_error = raw_error - calibration.bias_f
            gated_error = corrected_error if significant else raw_error
            raw_errors.append(raw_error)
            current_errors.append(corrected_error)
            gated_errors.append(gated_error)
            fold_raw_errors.append(raw_error)
            fold_current_errors.append(corrected_error)
            outcome = sample.observed_high_f >= threshold
            raw_probability = probability_at_or_above(
                mean_f=sample.forecast_high_f,
                std_f=calibration.raw_error_rms_f,
                threshold_f=threshold,
            )
            current_probability = probability_at_or_above(
                mean_f=sample.forecast_high_f + calibration.bias_f,
                std_f=calibration.residual_std_f,
                threshold_f=threshold,
            )
            gated_probability = current_probability if significant else raw_probability
            raw_brier.append(brier_score(raw_probability, outcome))
            current_brier.append(brier_score(current_probability, outcome))
            gated_brier.append(brier_score(gated_probability, outcome))
            raw_log.append(log_loss(raw_probability, outcome))
            current_log.append(log_loss(current_probability, outcome))
            gated_log.append(log_loss(gated_probability, outcome))
        if not significant:
            insignificant_test_count += len(test)
            if _mae(fold_current_errors) > _mae(fold_raw_errors):
                insignificant_bias_mae_worse += len(test)
            if _rmse(fold_current_errors) > _rmse(fold_raw_errors):
                insignificant_bias_rmse_worse += len(test)
        folds.append(
            {
                "train_end_date": train[-1].target_date.isoformat(),
                "test_start_date": test[0].target_date.isoformat(),
                "test_end_date": test[-1].target_date.isoformat(),
                "train_sample_count": len(train),
                "test_sample_count": len(test),
                "mean_bias_f": calibration.bias_f,
                "bias_standard_error_f": standard_error,
                "abs_bias_over_se": z_score,
                "significant_under_2se": significant,
            }
        )
    return {
        "test_sample_count": len(raw_errors),
        "fold_count": len(folds),
        "raw": _metric_summary(raw_errors, raw_brier, raw_log),
        "current_unconditional_bias": _metric_summary(
            current_errors, current_brier, current_log
        ),
        "significance_gated_bias": _metric_summary(gated_errors, gated_brier, gated_log),
        "insignificant_fold_test_count": insignificant_test_count,
        "insignificant_fold_bias_mae_worse_count": insignificant_bias_mae_worse,
        "insignificant_fold_bias_rmse_worse_count": insignificant_bias_rmse_worse,
        "folds": folds,
        "strict_no_lookahead": True,
    }


def audit_bias_significance(
    samples: Sequence[CalibrationSample],
    *,
    min_train_size: int = 30,
    test_size: int = 10,
    significance_threshold: float = 2.0,
) -> dict[str, Any]:
    """Audit every station/model/lead group without changing calibration."""
    groups: dict[tuple[str, str, int], list[CalibrationSample]] = defaultdict(list)
    for sample in samples:
        if sample.lead_days < 1:
            continue
        groups[(sample.station_id, sample.model, sample.lead_days)].append(sample)
    rows: list[dict[str, Any]] = []
    for (station_id, model, lead_days), values in sorted(groups.items()):
        ordered = sorted(values, key=lambda sample: sample.target_date)
        errors = [sample.error_f for sample in ordered]
        standard_error, z_score = bias_standard_error(ordered)
        significant = z_score is not None and z_score > significance_threshold
        current = build_live_calibration(
            ordered,
            station_id=station_id,
            lead_days=lead_days,
            min_samples=min_train_size,
        )
        current_applied = bool(current and current.apply_bias)
        current_ready = bool(current and current.ready)
        rolling_current = None
        if len(ordered) > min_train_size:
            current_evaluation = rolling_origin_evaluate(
                ordered, min_train_size=min_train_size, test_size=test_size
            )
            rolling_current = {
                "test_sample_count": current_evaluation.test_sample_count,
                "mae": current_evaluation.mae_calibrated,
                "rmse": current_evaluation.rmse_calibrated,
                "brier": current_evaluation.brier_calibrated,
                "log_loss": current_evaluation.log_loss_calibrated,
                "strict_no_lookahead": current_evaluation.no_lookahead,
            }
        gate_evaluation = rolling_significance_gate_evaluate(
            ordered,
            min_train_size=min_train_size,
            test_size=test_size,
            significance_threshold=significance_threshold,
        )
        rows.append(
            {
                "station_id": station_id,
                "model": model,
                "lead_days": lead_days,
                "sample_count": len(ordered),
                "sample_date_min": ordered[0].target_date.isoformat() if ordered else None,
                "sample_date_max": ordered[-1].target_date.isoformat() if ordered else None,
                "mean_bias_f": _mean(errors),
                "bias_standard_error_f": standard_error,
                "abs_bias_over_se": z_score,
                "significant_under_2se": significant,
                "current_ready": current_ready,
                "current_bias_applied": current_applied,
                "current_strategy": current.strategy if current else "no_calibration",
                "current_reason": current.reason if current else "fewer than two samples",
                "would_disable_current_bias_under_2se": current_applied and not significant,
                "would_enable_bias_under_2se": False,
                "current_walk_forward": rolling_current,
                "significance_gate_walk_forward": gate_evaluation,
                "statistically_unreliable": len(ordered) < 30,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "audit only; no calibration or strategy parameter was changed",
        "significance_threshold": significance_threshold,
        "min_train_size": min_train_size,
        "test_size": test_size,
        "group_count": len(rows),
        "current_gate_description": (
            "current live calibration requires sample history plus walk-forward "
            "RMSE/Brier/LogLoss gates; it has no explicit abs(mean_bias)/SE gate"
        ),
        "would_disable_count": sum(
            bool(row["would_disable_current_bias_under_2se"]) for row in rows
        ),
        "rows": rows,
        "execution_enabled": False,
        "strict_no_lookahead": True,
    }


def load_calibration_samples(path: Path | str) -> list[CalibrationSample]:
    """Read calibration inputs from the local research DB without writing it."""
    database = Path(path)
    if not database.exists():
        return []
    connection = duckdb.connect(str(database), read_only=True)
    try:
        try:
            values = connection.execute(
                """
                SELECT station_id, target_date, lead_days, model, forecast_high_f,
                       observed_high_f, forecast_source, truth_source, truth_kind,
                       ingested_at, forecast_high_f_by_model
                FROM calibration_samples
                WHERE lead_days >= 1
                ORDER BY station_id, model, lead_days, target_date
                """
            ).fetchall()
        except duckdb.CatalogException:
            return []
    finally:
        connection.close()
    samples: list[CalibrationSample] = []
    for row in values:
        model_values = row[10]
        if isinstance(model_values, str):
            model_values = json.loads(model_values)
        if model_values is not None and not isinstance(model_values, Mapping):
            raise ValueError("forecast_high_f_by_model must be a JSON object when present")
        samples.append(
            CalibrationSample(
                station_id=row[0],
                target_date=row[1],
                lead_days=row[2],
                model=row[3],
                forecast_high_f=row[4],
                observed_high_f=row[5],
                forecast_source=row[6],
                truth_source=row[7],
                truth_kind=row[8],
                ingested_at=row[9],
                forecast_high_f_by_model=dict(model_values) if model_values is not None else None,
            )
        )
    return samples


def render_bias_significance_audit(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Bias significance gate audit",
        "",
        f"Generated at (UTC): {result.get('generated_at', 'N/A')}",
        "",
        "> Audit only. This report does not alter calibration, signal thresholds, or execution state. Every walk-forward fold uses prior target dates only.",
        "",
        str(result.get("current_gate_description", "")),
        "",
        "| Station | Model | Lead | n | Mean bias F | SE F | |bias|/SE | Current bias applied | Would 2SE disable | OOS gate RMSE |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---:|",
    ]
    for row in result.get("rows") or ():
        gate = row.get("significance_gate_walk_forward") or {}
        metrics = gate.get("significance_gated_bias") or {}
        n_label = str(row.get("sample_count", 0))
        if row.get("statistically_unreliable"):
            n_label += " (n<30)"
        lines.append(
            "| "
            + " | ".join(
                (
                    str(row.get("station_id")),
                    str(row.get("model")),
                    str(row.get("lead_days")),
                    n_label,
                    _format_number(row.get("mean_bias_f")),
                    _format_number(row.get("bias_standard_error_f")),
                    _format_number(row.get("abs_bias_over_se")),
                    "yes" if row.get("current_bias_applied") else "no",
                    "yes" if row.get("would_disable_current_bias_under_2se") else "no",
                    _format_number(metrics.get("rmse")),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Walk-forward comparison",
            "",
            "| Station | Model | Lead | OOS n | Raw RMSE | Current unconditional RMSE | 2SE-gated RMSE | Raw Brier | Current Brier | 2SE-gated Brier | Insignificant-fold test n | Unconditional MAE/RMSE worse test n |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result.get("rows") or ():
        gate = row.get("significance_gate_walk_forward") or {}
        raw = gate.get("raw") or {}
        current = gate.get("current_unconditional_bias") or {}
        gated = gate.get("significance_gated_bias") or {}
        lines.append(
            "| "
            + " | ".join(
                (
                    str(row.get("station_id")),
                    str(row.get("model")),
                    str(row.get("lead_days")),
                    str(gate.get("test_sample_count", 0)),
                    _format_number(raw.get("rmse")),
                    _format_number(current.get("rmse")),
                    _format_number(gated.get("rmse")),
                    _format_number(raw.get("brier")),
                    _format_number(current.get("brier")),
                    _format_number(gated.get("brier")),
                    str(gate.get("insignificant_fold_test_count", 0)),
                    f"{gate.get('insignificant_fold_bias_mae_worse_count', 0)}/{gate.get('insignificant_fold_bias_rmse_worse_count', 0)}",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- A significant full-sample bias does not prove a live edge; it only permits considering a correction after the existing walk-forward gates.",
            "- `would_disable_current_bias_under_2se=yes` is a proposed safety gate difference, not an automatic configuration change.",
            "- The final column reports test observations in folds where unconditional correction worsened MAE/RMSE while the prior bias was not significant. It is diagnostic, not a new selection rule.",
            "- No ratio is used for trading. `execution_enabled=false`.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_number(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return f"{float(value):.3f}"
