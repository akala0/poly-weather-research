from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

SIGNAL_SCHEMA_VERSION = 2


class LegacySignalSchemaError(RuntimeError):
    """Raised when a writer is pointed at the pre-normalization signal schema."""


def _table_columns(connection: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    exists = connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0]
    if not exists:
        return set()
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info('{table}')").fetchall()
    }


def signal_schema_is_normalized(connection: duckdb.DuckDBPyConnection) -> bool:
    columns = _table_columns(connection, "signal_snapshots")
    return bool(columns) and "snapshot_id" in columns and "payload_json" not in columns


def create_normalized_signal_schema(
    connection: duckdb.DuckDBPyConnection,
    *,
    create_indexes: bool = True,
) -> None:
    """Create the lossless native-column signal schema on an empty/non-legacy DB."""
    columns = _table_columns(connection, "signal_snapshots")
    if columns and "payload_json" in columns:
        return
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_schema_metadata (
            schema_version INTEGER PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL,
            description VARCHAR NOT NULL
        );
        INSERT INTO signal_schema_metadata
        VALUES (2, current_timestamp, 'normalized lossless signal snapshots')
        ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS calibration_dim (
            calibration_id VARCHAR PRIMARY KEY,
            station_id VARCHAR,
            lead_days SMALLINT,
            sample_count INTEGER,
            sample_date_min DATE,
            sample_date_max DATE,
            bias_f DECIMAL(7, 4),
            residual_std_f DECIMAL(6, 1),
            raw_error_rms_f DECIMAL(6, 1),
            validation_test_samples INTEGER,
            mae_raw DECIMAL(6, 1),
            mae_calibrated DECIMAL(6, 1),
            rmse_raw DECIMAL(6, 1),
            rmse_calibrated DECIMAL(6, 1),
            brier_raw DECIMAL(7, 4),
            brier_calibrated DECIMAL(7, 4),
            log_loss_raw DECIMAL(7, 4),
            log_loss_calibrated DECIMAL(7, 4),
            weight_gfs DECIMAL(7, 4),
            weight_icon DECIMAL(7, 4),
            weight_gem DECIMAL(7, 4),
            ready BOOLEAN,
            apply_bias BOOLEAN,
            strategy VARCHAR,
            reason VARCHAR
        );

        CREATE TABLE IF NOT EXISTS bucket_dim (
            market_id VARCHAR PRIMARY KEY,
            event_slug VARCHAR NOT NULL,
            market_slug VARCHAR NOT NULL,
            bucket_question VARCHAR NOT NULL,
            bucket_lower_f DECIMAL(6, 1),
            bucket_upper_f DECIMAL(6, 1),
            bucket_lower_value SMALLINT,
            bucket_upper_value SMALLINT,
            temperature_unit VARCHAR NOT NULL,
            bucket_width_degrees SMALLINT,
            station_id VARCHAR NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signal_snapshots (
            snapshot_id VARCHAR PRIMARY KEY,
            run_id VARCHAR NOT NULL,
            sequence BIGINT NOT NULL,
            generated_at TIMESTAMPTZ NOT NULL,
            event_id VARCHAR,
            event_slug VARCHAR NOT NULL,
            station_id VARCHAR NOT NULL,
            target_date DATE,
            status VARCHAR NOT NULL,
            contract_verified BOOLEAN NOT NULL,
            calibration_ready BOOLEAN NOT NULL,
            calibration_id VARCHAR,
            current_observed_high_f DECIMAL(6, 1),
            warming_rate_f_per_hour DECIMAL(6, 1),
            hours_to_typical_peak DECIMAL(6, 1),
            typical_peak_local TIME,
            nws_temperature_f DECIMAL(6, 1),
            nws_age_minutes DECIMAL(9, 1),
            wrh_temperature_f DECIMAL(6, 1),
            wrh_age_minutes DECIMAL(9, 1),
            metar_temperature_f DECIMAL(6, 1),
            metar_age_minutes DECIMAL(9, 1),
            source_delta_f DECIMAL(6, 1),
            raw_daily_high_f DECIMAL(6, 1),
            selected_daily_high_f DECIMAL(6, 1),
            residual_std_f DECIMAL(6, 1),
            bias_applied BOOLEAN NOT NULL,
            ensemble_members SMALLINT,
            ensemble_calibrated BOOLEAN,
            ensemble_minimum_f DECIMAL(6, 1),
            ensemble_median_f DECIMAL(6, 1),
            ensemble_maximum_f DECIMAL(6, 1),
            ensemble_raw_minimum_f DECIMAL(6, 1),
            ensemble_raw_median_f DECIMAL(6, 1),
            ensemble_raw_maximum_f DECIMAL(6, 1),
            ensemble_selected_minimum_f DECIMAL(6, 1),
            ensemble_selected_median_f DECIMAL(6, 1),
            ensemble_selected_maximum_f DECIMAL(6, 1),
            cost_buffer DECIMAL(7, 4),
            min_net_edge DECIMAL(7, 4),
            top_market_id VARCHAR,
            paper_alert_eligible BOOLEAN NOT NULL,
            execution_enabled BOOLEAN NOT NULL,
            UNIQUE (run_id, sequence)
        );

        CREATE TABLE IF NOT EXISTS signal_snapshot_reasons (
            snapshot_id VARCHAR NOT NULL,
            reason_ordinal SMALLINT NOT NULL,
            reason VARCHAR NOT NULL,
            PRIMARY KEY (snapshot_id, reason_ordinal)
        );

        CREATE TABLE IF NOT EXISTS signal_bucket_observations (
            snapshot_id VARCHAR NOT NULL,
            market_id VARCHAR NOT NULL,
            bucket_ordinal SMALLINT NOT NULL,
            member_count SMALLINT,
            raw_model_probability DECIMAL(7, 4),
            calibrated_model_probability DECIMAL(7, 4),
            model_probability DECIMAL(7, 4),
            yes_best_bid DECIMAL(5, 3),
            yes_best_ask DECIMAL(5, 3),
            no_best_bid DECIMAL(5, 3),
            no_best_ask DECIMAL(5, 3),
            no_book_complete BOOLEAN NOT NULL,
            no_book_age_minutes DECIMAL(9, 1),
            physical_margin_f DECIMAL(6, 1),
            margin_tier VARCHAR,
            eliminated BOOLEAN,
            warming_window_no_conditions_met BOOLEAN NOT NULL,
            warming_window_no BOOLEAN NOT NULL,
            raw_research_candidate VARCHAR,
            raw_net_edge_after_buffer DECIMAL(7, 4),
            research_candidate VARCHAR,
            net_edge_after_buffer DECIMAL(7, 4),
            fill_yes_50 DECIMAL(5, 3),
            fill_no_50 DECIMAL(5, 3),
            slippage_bps_yes_50 DECIMAL(12, 1),
            slippage_bps_no_50 DECIMAL(12, 1),
            filled_fraction_yes_50 DECIMAL(5, 4),
            filled_fraction_no_50 DECIMAL(5, 4),
            executable_candidate_50 VARCHAR,
            executable_net_edge_50 DECIMAL(7, 4),
            fill_yes_200 DECIMAL(5, 3),
            fill_no_200 DECIMAL(5, 3),
            slippage_bps_yes_200 DECIMAL(12, 1),
            slippage_bps_no_200 DECIMAL(12, 1),
            filled_fraction_yes_200 DECIMAL(5, 4),
            filled_fraction_no_200 DECIMAL(5, 4),
            executable_candidate_200 VARCHAR,
            executable_net_edge_200 DECIMAL(7, 4),
            fill_yes_1000 DECIMAL(5, 3),
            fill_no_1000 DECIMAL(5, 3),
            slippage_bps_yes_1000 DECIMAL(12, 1),
            slippage_bps_no_1000 DECIMAL(12, 1),
            filled_fraction_yes_1000 DECIMAL(5, 4),
            filled_fraction_no_1000 DECIMAL(5, 4),
            executable_candidate_1000 VARCHAR,
            executable_net_edge_1000 DECIMAL(7, 4),
            paper_alert_eligible BOOLEAN NOT NULL,
            edge_gate_blocked BOOLEAN NOT NULL,
            action VARCHAR NOT NULL,
            PRIMARY KEY (snapshot_id, market_id)
        );
        """
    )
    if create_indexes:
        create_signal_indexes(connection)


def create_signal_indexes(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS signal_snapshot_event_time_idx
            ON signal_snapshots(event_slug, generated_at);
        CREATE INDEX IF NOT EXISTS signal_snapshot_status_time_idx
            ON signal_snapshots(status, generated_at);
        CREATE INDEX IF NOT EXISTS signal_bucket_market_time_idx
            ON signal_bucket_observations(market_id, snapshot_id);
        """
    )


def calibration_id(calibration: Mapping[str, Any] | None) -> str | None:
    if not calibration:
        return None
    canonical = json.dumps(calibration, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_id(run_id: str, sequence: int) -> str:
    return f"{run_id}:{sequence}"


def _estimate_by_size(signal: Mapping[str, Any], size: int) -> Mapping[str, Any]:
    estimates = signal.get("execution_estimates")
    if not isinstance(estimates, list):
        return {}
    for estimate in estimates:
        if isinstance(estimate, Mapping) and int(float(estimate.get("size_usd", -1))) == size:
            return estimate
    return {}


def _as_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def append_normalized_signal_snapshots(
    connection: duckdb.DuckDBPyConnection,
    snapshots: Iterable[dict[str, Any]],
) -> int:
    """Append every snapshot; precision reduction happens only at the archive boundary."""
    if not signal_schema_is_normalized(connection):
        raise LegacySignalSchemaError(
            "signal_stream.duckdb uses legacy payload_json schema; run migrate-signal-schema "
            "and switch to the verified candidate before restarting the signal engine"
        )
    rows = list(snapshots)
    if not rows:
        return 0
    calibrations: dict[str, tuple[Any, ...]] = {}
    dimensions: dict[str, tuple[Any, ...]] = {}
    snapshot_rows: list[tuple[Any, ...]] = []
    reason_rows: list[tuple[Any, ...]] = []
    bucket_rows: list[tuple[Any, ...]] = []
    for row in rows:
        payload = row["payload"]
        sid = snapshot_id(str(row["run_id"]), int(row["sequence"]))
        calibration = payload.get("calibration")
        cid = calibration_id(calibration)
        if cid is not None and isinstance(calibration, Mapping):
            weights = calibration.get("model_weights") or {}
            calibrations[cid] = (
                cid,
                calibration.get("station_id"),
                calibration.get("lead_days"),
                calibration.get("sample_count"),
                calibration.get("sample_date_min"),
                calibration.get("sample_date_max"),
                _as_decimal(calibration.get("bias_f")),
                _as_decimal(calibration.get("residual_std_f")),
                _as_decimal(calibration.get("raw_error_rms_f")),
                calibration.get("validation_test_samples"),
                _as_decimal(calibration.get("mae_raw")),
                _as_decimal(calibration.get("mae_calibrated")),
                _as_decimal(calibration.get("rmse_raw")),
                _as_decimal(calibration.get("rmse_calibrated")),
                _as_decimal(calibration.get("brier_raw")),
                _as_decimal(calibration.get("brier_calibrated")),
                _as_decimal(calibration.get("log_loss_raw")),
                _as_decimal(calibration.get("log_loss_calibrated")),
                _as_decimal(weights.get("gfs")),
                _as_decimal(weights.get("icon")),
                _as_decimal(weights.get("gem")),
                calibration.get("ready"),
                calibration.get("apply_bias"),
                calibration.get("strategy"),
                calibration.get("reason"),
            )
        deterministic = payload.get("deterministic") or {}
        raw_deterministic = deterministic.get("raw") or {}
        selected_deterministic = deterministic.get("selected") or {}
        ensemble = payload.get("ensemble") or {}
        ensemble_raw = ensemble.get("raw") or {}
        ensemble_selected = ensemble.get("selected") or {}
        top = payload.get("top_research_candidate") or {}
        snapshot_rows.append(
            (
                sid,
                row["run_id"],
                row["sequence"],
                row["generated_at"],
                payload.get("event_id"),
                row["event_slug"],
                row["station_id"],
                payload.get("target_date"),
                row["status"],
                bool(payload.get("contract_verified")),
                bool(payload.get("calibration_ready")),
                cid,
                _as_decimal(payload.get("current_observed_high_f")),
                _as_decimal(payload.get("warming_rate_f_per_hour")),
                _as_decimal(payload.get("hours_to_typical_peak")),
                payload.get("typical_peak_local"),
                _as_decimal(payload.get("nws_temperature_f")),
                _as_decimal(payload.get("nws_age_minutes")),
                _as_decimal(payload.get("wrh_temperature_f")),
                _as_decimal(payload.get("wrh_age_minutes")),
                _as_decimal(payload.get("metar_temperature_f")),
                _as_decimal(payload.get("metar_age_minutes")),
                _as_decimal(payload.get("source_delta_f")),
                _as_decimal(raw_deterministic.get("daily_high_f")),
                _as_decimal(selected_deterministic.get("daily_high_f")),
                _as_decimal(selected_deterministic.get("residual_std_f")),
                bool(selected_deterministic.get("bias_applied")),
                ensemble.get("members"),
                ensemble.get("calibrated"),
                _as_decimal(ensemble.get("minimum_f")),
                _as_decimal(ensemble.get("median_f")),
                _as_decimal(ensemble.get("maximum_f")),
                _as_decimal(ensemble_raw.get("minimum_f")),
                _as_decimal(ensemble_raw.get("median_f")),
                _as_decimal(ensemble_raw.get("maximum_f")),
                _as_decimal(ensemble_selected.get("minimum_f")),
                _as_decimal(ensemble_selected.get("median_f")),
                _as_decimal(ensemble_selected.get("maximum_f")),
                _as_decimal(payload.get("cost_buffer")),
                _as_decimal(payload.get("min_net_edge")),
                top.get("market_id"),
                bool(payload.get("paper_alert_eligible")),
                bool(payload.get("execution_enabled")),
            )
        )
        for ordinal, reason in enumerate(payload.get("reasons") or [], start=1):
            reason_rows.append((sid, ordinal, str(reason)))
        for ordinal, signal in enumerate(payload.get("signals") or [], start=1):
            market_id = str(signal["market_id"])
            lower_value = signal.get("bucket_lower_value")
            upper_value = signal.get("bucket_upper_value")
            unit = str(signal.get("temperature_unit") or "fahrenheit")
            lower_f = signal.get("bucket_lower_f")
            upper_f = signal.get("bucket_upper_f")
            dimensions[market_id] = (
                market_id,
                row["event_slug"],
                signal["market_slug"],
                signal["bucket"],
                _as_decimal(lower_f),
                _as_decimal(upper_f),
                lower_value,
                upper_value,
                unit,
                signal.get("bucket_width_degrees"),
                row["station_id"],
            )
            e50 = _estimate_by_size(signal, 50)
            e200 = _estimate_by_size(signal, 200)
            e1000 = _estimate_by_size(signal, 1000)
            bucket_rows.append(
                (
                    sid,
                    market_id,
                    ordinal,
                    signal.get("member_count"),
                    _as_decimal(signal.get("raw_model_probability")),
                    _as_decimal(signal.get("calibrated_model_probability")),
                    _as_decimal(signal.get("model_probability")),
                    _as_decimal(signal.get("yes_best_bid")),
                    _as_decimal(signal.get("yes_best_ask")),
                    _as_decimal(signal.get("no_best_bid")),
                    _as_decimal(signal.get("no_best_ask")),
                    bool(signal.get("no_book_complete")),
                    _as_decimal(signal.get("no_book_age_minutes")),
                    _as_decimal(signal.get("physical_margin_f")),
                    signal.get("margin_tier"),
                    signal.get("eliminated"),
                    bool(signal.get("warming_window_no_conditions_met")),
                    bool(signal.get("warming_window_no")),
                    signal.get("raw_research_candidate"),
                    _as_decimal(signal.get("raw_net_edge_after_buffer")),
                    signal.get("research_candidate"),
                    _as_decimal(signal.get("net_edge_after_buffer")),
                    *execution_estimate_columns(e50),
                    *execution_estimate_columns(e200),
                    *execution_estimate_columns(e1000),
                    bool(signal.get("paper_alert_eligible")),
                    bool(signal.get("edge_gate_blocked")),
                    str(signal.get("action") or "skip"),
                )
            )
    connection.execute("BEGIN TRANSACTION")
    try:
        if calibrations:
            connection.executemany(
                "INSERT INTO calibration_dim VALUES (" + ",".join("?" * 25) + ") "
                "ON CONFLICT DO NOTHING",
                calibrations.values(),
            )
        if dimensions:
            connection.executemany(
                "INSERT INTO bucket_dim VALUES (" + ",".join("?" * 11) + ") "
                "ON CONFLICT DO NOTHING",
                dimensions.values(),
            )
        connection.executemany(
            "INSERT INTO signal_snapshots VALUES (" + ",".join("?" * 43) + ") "
            "ON CONFLICT DO NOTHING",
            snapshot_rows,
        )
        if reason_rows:
            connection.executemany(
                "INSERT INTO signal_snapshot_reasons VALUES (?,?,?) ON CONFLICT DO NOTHING",
                reason_rows,
            )
        if bucket_rows:
            connection.executemany(
                "INSERT INTO signal_bucket_observations VALUES ("
                + ",".join("?" * 49)
                + ") ON CONFLICT DO NOTHING",
                bucket_rows,
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return len(rows)


def execution_estimate_columns(estimate: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _as_decimal(estimate.get("estimated_fill_yes")),
        _as_decimal(estimate.get("estimated_fill_no")),
        _as_decimal(estimate.get("slippage_bps_yes")),
        _as_decimal(estimate.get("slippage_bps_no")),
        _as_decimal(estimate.get("filled_fraction_yes")),
        _as_decimal(estimate.get("filled_fraction_no")),
        estimate.get("executable_candidate"),
        _as_decimal(estimate.get("executable_net_edge_after_buffer")),
    )


def normalized_signal_database_size(path: Path) -> dict[str, Any]:
    logical_bytes = path.stat().st_size
    return {"path": str(path.resolve()), "logical_bytes": logical_bytes}


def utc_now() -> datetime:
    return datetime.now(UTC)
