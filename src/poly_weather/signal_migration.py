from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from poly_weather.signal_schema import SIGNAL_SCHEMA_VERSION, create_signal_indexes


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _scan_expression(paths: Iterable[Path]) -> str:
    resolved = [str(path.resolve()).replace("\\", "/") for path in paths]
    if not resolved:
        raise ValueError("at least one signal snapshot JSONL file is required")
    values = ",".join(_sql_string(path) for path in resolved)
    return (
        f"read_json_auto([{values}], format='newline_delimited', "
        "union_by_name=true, sample_size=-1, maximum_object_size=16777216, "
        "ignore_errors=true)"
    )


def _assert_candidate_path(target: Path, source_database: Path | None = None) -> None:
    resolved = target.resolve()
    if source_database is not None and resolved == source_database.resolve():
        raise ValueError("candidate path must differ from the online signal database")
    if resolved.name == "signal_stream.duckdb":
        raise ValueError("refusing to build directly over the online signal database")


def _create_source_views(
    connection: duckdb.DuckDBPyConnection,
    paths: Iterable[Path],
) -> None:
    scan = _scan_expression(paths)
    connection.execute(
        f"""
        CREATE TEMP TABLE source_snapshots AS
        SELECT
            CAST(run_id AS VARCHAR) AS run_id,
            CAST(sequence AS BIGINT) AS sequence,
            generated_at AT TIME ZONE 'UTC' AS generated_at,
            event_slug,
            station_id,
            status,
            payload
        FROM {scan}
        WHERE run_id IS NOT NULL AND sequence IS NOT NULL AND payload IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY CAST(run_id AS VARCHAR), CAST(sequence AS BIGINT)
            ORDER BY generated_at DESC
        ) = 1;

        CREATE TEMP VIEW source_buckets AS
        SELECT
            concat(s.run_id, ':', CAST(s.sequence AS VARCHAR)) AS snapshot_id,
            s.event_slug,
            s.station_id,
            s.generated_at,
            u.signal,
            to_json(u.signal) AS signal_json,
            CAST(u.bucket_ordinal AS SMALLINT) AS bucket_ordinal
        FROM source_snapshots AS s,
             UNNEST(s.payload.signals) WITH ORDINALITY AS u(signal, bucket_ordinal);

        CREATE TEMP VIEW parsed_buckets AS
        SELECT
            *,
            CASE
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c((or)?below|(or)?higher)?$')
                    THEN 'celsius'
                ELSE 'fahrenheit'
            END AS temperature_unit,
            CASE
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c((or)?below)$')
                    THEN NULL
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-(-?[0-9]+)c$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c((or)?higher)$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-(-?[0-9]+)c((or)?higher)$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+forbelow$')
                    THEN NULL
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+-[0-9]+f$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-([0-9]+)-[0-9]+f$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+forhigher$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-([0-9]+)forhigher$', 1) AS SMALLINT)
            END AS bucket_lower_value,
            CASE
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c((or)?below)$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-(-?[0-9]+)c((or)?below)$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-(-?[0-9]+)c$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-(-?[0-9]+)c((or)?higher)$')
                    THEN NULL
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+forbelow$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-([0-9]+)forbelow$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+-[0-9]+f$')
                    THEN TRY_CAST(regexp_extract(lower(signal.market_slug), '-[0-9]+-([0-9]+)f$', 1) AS SMALLINT)
                WHEN regexp_matches(lower(signal.market_slug), '-[0-9]+forhigher$')
                    THEN NULL
            END AS bucket_upper_value
        FROM source_buckets;
        """
    )


def _create_native_tables(connection: duckdb.DuckDBPyConnection) -> None:
    script = """
        CREATE TABLE signal_schema_metadata AS
        SELECT
            2::INTEGER AS schema_version,
            current_timestamp AS created_at,
            'normalized lossless signal snapshots'::VARCHAR AS description;

        CREATE TABLE calibration_dim AS
        SELECT
            md5(to_json(payload.calibration))::VARCHAR AS calibration_id,
            payload.calibration.station_id::VARCHAR AS station_id,
            payload.calibration.lead_days::SMALLINT AS lead_days,
            payload.calibration.sample_count::INTEGER AS sample_count,
            payload.calibration.sample_date_min::DATE AS sample_date_min,
            payload.calibration.sample_date_max::DATE AS sample_date_max,
            payload.calibration.bias_f::DECIMAL(7,4) AS bias_f,
            payload.calibration.residual_std_f::DECIMAL(6,1) AS residual_std_f,
            payload.calibration.raw_error_rms_f::DECIMAL(6,1) AS raw_error_rms_f,
            payload.calibration.validation_test_samples::INTEGER AS validation_test_samples,
            payload.calibration.mae_raw::DECIMAL(6,1) AS mae_raw,
            payload.calibration.mae_calibrated::DECIMAL(6,1) AS mae_calibrated,
            payload.calibration.rmse_raw::DECIMAL(6,1) AS rmse_raw,
            payload.calibration.rmse_calibrated::DECIMAL(6,1) AS rmse_calibrated,
            payload.calibration.brier_raw::DECIMAL(7,4) AS brier_raw,
            payload.calibration.brier_calibrated::DECIMAL(7,4) AS brier_calibrated,
            payload.calibration.log_loss_raw::DECIMAL(7,4) AS log_loss_raw,
            payload.calibration.log_loss_calibrated::DECIMAL(7,4) AS log_loss_calibrated,
            json_extract(to_json(payload.calibration), '$.model_weights.gfs')::DECIMAL(7,4)
                AS weight_gfs,
            json_extract(to_json(payload.calibration), '$.model_weights.icon')::DECIMAL(7,4)
                AS weight_icon,
            json_extract(to_json(payload.calibration), '$.model_weights.gem')::DECIMAL(7,4)
                AS weight_gem,
            payload.calibration.ready::BOOLEAN AS ready,
            payload.calibration.apply_bias::BOOLEAN AS apply_bias,
            payload.calibration.strategy::VARCHAR AS strategy,
            payload.calibration.reason::VARCHAR AS reason
        FROM source_snapshots
        WHERE payload.calibration IS NOT NULL
        QUALIFY row_number() OVER (
            PARTITION BY md5(to_json(payload.calibration)) ORDER BY generated_at
        ) = 1;

        CREATE TABLE bucket_dim AS
        SELECT
            signal.market_id::VARCHAR AS market_id,
            event_slug::VARCHAR AS event_slug,
            signal.market_slug::VARCHAR AS market_slug,
            signal.bucket::VARCHAR AS bucket_question,
            (CASE WHEN bucket_lower_value IS NULL THEN NULL
                  WHEN temperature_unit = 'celsius'
                      THEN bucket_lower_value * 9.0 / 5.0 + 32.0
                  ELSE bucket_lower_value END)::DECIMAL(6,1) AS bucket_lower_f,
            (CASE WHEN bucket_upper_value IS NULL THEN NULL
                  WHEN temperature_unit = 'celsius'
                      THEN bucket_upper_value * 9.0 / 5.0 + 32.0
                  ELSE bucket_upper_value END)::DECIMAL(6,1) AS bucket_upper_f,
            bucket_lower_value::SMALLINT AS bucket_lower_value,
            bucket_upper_value::SMALLINT AS bucket_upper_value,
            temperature_unit::VARCHAR AS temperature_unit,
            (CASE WHEN bucket_lower_value IS NOT NULL AND bucket_upper_value IS NOT NULL
                  THEN bucket_upper_value - bucket_lower_value + 1 END)::SMALLINT
                AS bucket_width_degrees,
            station_id::VARCHAR AS station_id
        FROM parsed_buckets
        QUALIFY row_number() OVER (
            PARTITION BY signal.market_id ORDER BY generated_at
        ) = 1;

        CREATE TABLE signal_snapshots AS
        SELECT
            concat(run_id, ':', CAST(sequence AS VARCHAR))::VARCHAR AS snapshot_id,
            run_id::VARCHAR AS run_id,
            sequence::BIGINT AS sequence,
            generated_at::TIMESTAMPTZ AS generated_at,
            payload.event_id::VARCHAR AS event_id,
            event_slug::VARCHAR AS event_slug,
            station_id::VARCHAR AS station_id,
            payload.target_date::DATE AS target_date,
            status::VARCHAR AS status,
            coalesce(payload.contract_verified, false)::BOOLEAN AS contract_verified,
            coalesce(payload.calibration_ready, false)::BOOLEAN AS calibration_ready,
            CASE WHEN payload.calibration IS NULL THEN NULL
                 ELSE md5(to_json(payload.calibration)) END::VARCHAR AS calibration_id,
            payload.current_observed_high_f::DECIMAL(6,1) AS current_observed_high_f,
            json_extract(to_json(payload), '$.warming_rate_f_per_hour')::DECIMAL(6,1)
                AS warming_rate_f_per_hour,
            json_extract(to_json(payload), '$.hours_to_typical_peak')::DECIMAL(6,1)
                AS hours_to_typical_peak,
            json_extract_string(to_json(payload), '$.typical_peak_local')::TIME
                AS typical_peak_local,
            payload.nws_temperature_f::DECIMAL(6,1) AS nws_temperature_f,
            payload.nws_age_minutes::DECIMAL(9,1) AS nws_age_minutes,
            json_extract(to_json(payload), '$.wrh_temperature_f')::DECIMAL(6,1)
                AS wrh_temperature_f,
            json_extract(to_json(payload), '$.wrh_age_minutes')::DECIMAL(9,1)
                AS wrh_age_minutes,
            payload.metar_temperature_f::DECIMAL(6,1) AS metar_temperature_f,
            payload.metar_age_minutes::DECIMAL(9,1) AS metar_age_minutes,
            payload.source_delta_f::DECIMAL(6,1) AS source_delta_f,
            json_extract(to_json(payload), '$.deterministic.raw.daily_high_f')::DECIMAL(6,1)
                AS raw_daily_high_f,
            json_extract(to_json(payload), '$.deterministic.selected.daily_high_f')::DECIMAL(6,1)
                AS selected_daily_high_f,
            json_extract(to_json(payload), '$.deterministic.selected.residual_std_f')::DECIMAL(6,1)
                AS residual_std_f,
            coalesce(
                json_extract(to_json(payload), '$.deterministic.selected.bias_applied')::BOOLEAN,
                false
            ) AS bias_applied,
            json_extract(to_json(payload), '$.ensemble.members')::SMALLINT AS ensemble_members,
            json_extract(to_json(payload), '$.ensemble.calibrated')::BOOLEAN
                AS ensemble_calibrated,
            json_extract(to_json(payload), '$.ensemble.minimum_f')::DECIMAL(6,1)
                AS ensemble_minimum_f,
            json_extract(to_json(payload), '$.ensemble.median_f')::DECIMAL(6,1)
                AS ensemble_median_f,
            json_extract(to_json(payload), '$.ensemble.maximum_f')::DECIMAL(6,1)
                AS ensemble_maximum_f,
            json_extract(to_json(payload), '$.ensemble.raw.minimum_f')::DECIMAL(6,1)
                AS ensemble_raw_minimum_f,
            json_extract(to_json(payload), '$.ensemble.raw.median_f')::DECIMAL(6,1)
                AS ensemble_raw_median_f,
            json_extract(to_json(payload), '$.ensemble.raw.maximum_f')::DECIMAL(6,1)
                AS ensemble_raw_maximum_f,
            json_extract(to_json(payload), '$.ensemble.selected.minimum_f')::DECIMAL(6,1)
                AS ensemble_selected_minimum_f,
            json_extract(to_json(payload), '$.ensemble.selected.median_f')::DECIMAL(6,1)
                AS ensemble_selected_median_f,
            json_extract(to_json(payload), '$.ensemble.selected.maximum_f')::DECIMAL(6,1)
                AS ensemble_selected_maximum_f,
            payload.cost_buffer::DECIMAL(7,4) AS cost_buffer,
            payload.min_net_edge::DECIMAL(7,4) AS min_net_edge,
            payload.top_research_candidate.market_id::VARCHAR AS top_market_id,
            coalesce(payload.paper_alert_eligible, false)::BOOLEAN AS paper_alert_eligible,
            coalesce(payload.execution_enabled, false)::BOOLEAN AS execution_enabled
        FROM source_snapshots;

        CREATE TABLE signal_snapshot_reasons AS
        SELECT
            concat(s.run_id, ':', CAST(s.sequence AS VARCHAR))::VARCHAR AS snapshot_id,
            r.reason_ordinal::SMALLINT AS reason_ordinal,
            r.reason::VARCHAR AS reason
        FROM source_snapshots AS s,
             UNNEST(s.payload.reasons) WITH ORDINALITY AS r(reason, reason_ordinal);

        CREATE TABLE signal_bucket_observations AS
        SELECT
            snapshot_id::VARCHAR AS snapshot_id,
            signal.market_id::VARCHAR AS market_id,
            bucket_ordinal::SMALLINT AS bucket_ordinal,
            json_extract(signal_json, '$.member_count')::SMALLINT AS member_count,
            signal.raw_model_probability::DECIMAL(7,4) AS raw_model_probability,
            signal.calibrated_model_probability::DECIMAL(7,4) AS calibrated_model_probability,
            signal.model_probability::DECIMAL(7,4) AS model_probability,
            signal.yes_best_bid::DECIMAL(5,3) AS yes_best_bid,
            signal.yes_best_ask::DECIMAL(5,3) AS yes_best_ask,
            signal.no_best_bid::DECIMAL(5,3) AS no_best_bid,
            signal.no_best_ask::DECIMAL(5,3) AS no_best_ask,
            json_extract(signal_json, '$.yes_taker_fee_per_share')::DECIMAL(7,5)
                AS yes_taker_fee_per_share,
            json_extract(signal_json, '$.no_taker_fee_per_share')::DECIMAL(7,5)
                AS no_taker_fee_per_share,
            coalesce(json_extract(signal_json, '$.no_book_complete')::BOOLEAN, false)
                AS no_book_complete,
            json_extract(signal_json, '$.no_book_age_minutes')::DECIMAL(9,1)
                AS no_book_age_minutes,
            json_extract(signal_json, '$.physical_margin_f')::DECIMAL(6,1)
                AS physical_margin_f,
            json_extract_string(signal_json, '$.margin_tier')::VARCHAR AS margin_tier,
            json_extract(signal_json, '$.eliminated')::BOOLEAN AS eliminated,
            coalesce(json_extract(signal_json, '$.warming_window_no_conditions_met')::BOOLEAN, false)
                AS warming_window_no_conditions_met,
            coalesce(json_extract(signal_json, '$.warming_window_no')::BOOLEAN, false)
                AS warming_window_no,
            signal.raw_research_candidate::VARCHAR AS raw_research_candidate,
            signal.raw_net_edge_after_buffer::DECIMAL(7,4) AS raw_net_edge_after_buffer,
            signal.research_candidate::VARCHAR AS research_candidate,
            signal.net_edge_after_buffer::DECIMAL(7,4) AS net_edge_after_buffer,
            json_extract(signal_json, '$.execution_estimates[0].estimated_fill_yes')::DECIMAL(5,3)
                AS fill_yes_50,
            json_extract(signal_json, '$.execution_estimates[0].estimated_fill_no')::DECIMAL(5,3)
                AS fill_no_50,
            json_extract(signal_json, '$.execution_estimates[0].slippage_bps_yes')::DECIMAL(12,1)
                AS slippage_bps_yes_50,
            json_extract(signal_json, '$.execution_estimates[0].slippage_bps_no')::DECIMAL(12,1)
                AS slippage_bps_no_50,
            json_extract(signal_json, '$.execution_estimates[0].taker_fee_per_share_yes')::DECIMAL(7,5)
                AS taker_fee_per_share_yes_50,
            json_extract(signal_json, '$.execution_estimates[0].taker_fee_per_share_no')::DECIMAL(7,5)
                AS taker_fee_per_share_no_50,
            json_extract(signal_json, '$.execution_estimates[0].filled_fraction_yes')::DECIMAL(5,4)
                AS filled_fraction_yes_50,
            json_extract(signal_json, '$.execution_estimates[0].filled_fraction_no')::DECIMAL(5,4)
                AS filled_fraction_no_50,
            json_extract_string(signal_json, '$.execution_estimates[0].executable_candidate')::VARCHAR
                AS executable_candidate_50,
            json_extract(signal_json, '$.execution_estimates[0].executable_net_edge_after_buffer')::DECIMAL(7,4)
                AS executable_net_edge_50,
            json_extract(signal_json, '$.execution_estimates[1].estimated_fill_yes')::DECIMAL(5,3)
                AS fill_yes_200,
            json_extract(signal_json, '$.execution_estimates[1].estimated_fill_no')::DECIMAL(5,3)
                AS fill_no_200,
            json_extract(signal_json, '$.execution_estimates[1].slippage_bps_yes')::DECIMAL(12,1)
                AS slippage_bps_yes_200,
            json_extract(signal_json, '$.execution_estimates[1].slippage_bps_no')::DECIMAL(12,1)
                AS slippage_bps_no_200,
            json_extract(signal_json, '$.execution_estimates[1].taker_fee_per_share_yes')::DECIMAL(7,5)
                AS taker_fee_per_share_yes_200,
            json_extract(signal_json, '$.execution_estimates[1].taker_fee_per_share_no')::DECIMAL(7,5)
                AS taker_fee_per_share_no_200,
            json_extract(signal_json, '$.execution_estimates[1].filled_fraction_yes')::DECIMAL(5,4)
                AS filled_fraction_yes_200,
            json_extract(signal_json, '$.execution_estimates[1].filled_fraction_no')::DECIMAL(5,4)
                AS filled_fraction_no_200,
            json_extract_string(signal_json, '$.execution_estimates[1].executable_candidate')::VARCHAR
                AS executable_candidate_200,
            json_extract(signal_json, '$.execution_estimates[1].executable_net_edge_after_buffer')::DECIMAL(7,4)
                AS executable_net_edge_200,
            json_extract(signal_json, '$.execution_estimates[2].estimated_fill_yes')::DECIMAL(5,3)
                AS fill_yes_1000,
            json_extract(signal_json, '$.execution_estimates[2].estimated_fill_no')::DECIMAL(5,3)
                AS fill_no_1000,
            json_extract(signal_json, '$.execution_estimates[2].slippage_bps_yes')::DECIMAL(12,1)
                AS slippage_bps_yes_1000,
            json_extract(signal_json, '$.execution_estimates[2].slippage_bps_no')::DECIMAL(12,1)
                AS slippage_bps_no_1000,
            json_extract(signal_json, '$.execution_estimates[2].taker_fee_per_share_yes')::DECIMAL(7,5)
                AS taker_fee_per_share_yes_1000,
            json_extract(signal_json, '$.execution_estimates[2].taker_fee_per_share_no')::DECIMAL(7,5)
                AS taker_fee_per_share_no_1000,
            json_extract(signal_json, '$.execution_estimates[2].filled_fraction_yes')::DECIMAL(5,4)
                AS filled_fraction_yes_1000,
            json_extract(signal_json, '$.execution_estimates[2].filled_fraction_no')::DECIMAL(5,4)
                AS filled_fraction_no_1000,
            json_extract_string(signal_json, '$.execution_estimates[2].executable_candidate')::VARCHAR
                AS executable_candidate_1000,
            json_extract(signal_json, '$.execution_estimates[2].executable_net_edge_after_buffer')::DECIMAL(7,4)
                AS executable_net_edge_1000,
            coalesce(signal.paper_alert_eligible, false)::BOOLEAN AS paper_alert_eligible,
            coalesce(json_extract(signal_json, '$.edge_gate_blocked')::BOOLEAN, false)
                AS edge_gate_blocked,
            coalesce(signal.action, 'skip')::VARCHAR AS action
        FROM parsed_buckets;

        CREATE TABLE signal_stream_runs AS
        SELECT
            run_id::VARCHAR AS run_id,
            min(generated_at)::TIMESTAMPTZ AS started_at,
            max(generated_at)::TIMESTAMPTZ AS finished_at,
            to_json(list_distinct(list(event_slug)))::VARCHAR AS event_slugs_json,
            'migrated'::VARCHAR AS status,
            NULL::VARCHAR AS metrics_json
        FROM source_snapshots
        GROUP BY run_id;
        """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _add_constraints_and_indexes(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        ALTER TABLE signal_schema_metadata ADD PRIMARY KEY (schema_version);
        ALTER TABLE calibration_dim ADD PRIMARY KEY (calibration_id);
        ALTER TABLE bucket_dim ADD PRIMARY KEY (market_id);
        ALTER TABLE signal_snapshots ADD PRIMARY KEY (snapshot_id);
        ALTER TABLE signal_snapshot_reasons ADD PRIMARY KEY (snapshot_id, reason_ordinal);
        ALTER TABLE signal_bucket_observations ADD PRIMARY KEY (snapshot_id, market_id);
        ALTER TABLE signal_stream_runs ADD PRIMARY KEY (run_id);
        CREATE UNIQUE INDEX signal_snapshot_run_sequence_idx
            ON signal_snapshots(run_id, sequence);
        """
    )
    create_signal_indexes(connection)


def hash_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.exists():
        return hashes
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        hashes[str(path.relative_to(root)).replace("\\", "/")] = digest.hexdigest()
    return hashes


def _edge_rows(path: Path, count: int) -> list[dict[str, Any]]:
    first: list[bytes] = []
    last: list[bytes] = []
    with path.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            if len(first) < count:
                first.append(line)
            last.append(line)
            if len(last) > count:
                last.pop(0)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line in first + last:
        try:
            row = json.loads(line)
            key = (str(row["run_id"]), int(row["sequence"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if key not in seen:
            rows.append(row)
            seen.add(key)
    return rows


def verify_sample_reconstruction(
    paths: Iterable[Path],
    *,
    candidate_path: Path,
    per_file: int = 3,
) -> dict[str, Any]:
    """Compare edge samples from every partition with normalized native columns."""
    rows = [row for path in paths for row in _edge_rows(path, per_file)]
    connection = duckdb.connect(str(candidate_path), read_only=True)
    mismatches: list[str] = []
    try:
        for row in rows:
            sid = f"{row['run_id']}:{row['sequence']}"
            payload = row["payload"]
            stored = connection.execute(
                """
                SELECT generated_at, event_slug, station_id, status, current_observed_high_f,
                       paper_alert_eligible, execution_enabled
                FROM signal_snapshots WHERE snapshot_id = ?
                """,
                [sid],
            ).fetchone()
            if stored is None:
                mismatches.append(f"{sid}: missing snapshot")
                continue
            source_generated_at = datetime.fromisoformat(str(row["generated_at"]))
            if stored[0].astimezone(UTC) != source_generated_at.astimezone(UTC):
                mismatches.append(f"{sid}: generated_at mismatch")
            if stored[1:4] != (row["event_slug"], row["station_id"], row["status"]):
                mismatches.append(f"{sid}: snapshot identity/status mismatch")
            observed = payload.get("current_observed_high_f")
            if observed is None:
                if stored[4] is not None:
                    mismatches.append(f"{sid}: unexpected observed high")
            elif abs(float(stored[4]) - float(observed)) > 0.051:
                mismatches.append(f"{sid}: observed high exceeds archive tolerance")
            if stored[5:] != (
                bool(payload.get("paper_alert_eligible")),
                bool(payload.get("execution_enabled")),
            ):
                mismatches.append(f"{sid}: safety flags mismatch")
            reasons = [
                value[0]
                for value in connection.execute(
                    """
                    SELECT reason FROM signal_snapshot_reasons
                    WHERE snapshot_id = ? ORDER BY reason_ordinal
                    """,
                    [sid],
                ).fetchall()
            ]
            if reasons != list(payload.get("reasons") or []):
                mismatches.append(f"{sid}: reasons mismatch")
            buckets = connection.execute(
                """
                SELECT b.market_id, d.market_slug, d.bucket_question,
                       b.model_probability, b.yes_best_ask, b.no_best_ask,
                       b.warming_window_no, b.action
                FROM signal_bucket_observations AS b
                JOIN bucket_dim AS d USING (market_id)
                WHERE b.snapshot_id = ? ORDER BY b.bucket_ordinal
                """,
                [sid],
            ).fetchall()
            source_signals = payload.get("signals") or []
            if len(buckets) != len(source_signals):
                mismatches.append(f"{sid}: bucket count mismatch")
                continue
            for index, (bucket, source) in enumerate(
                zip(buckets, source_signals, strict=True), start=1
            ):
                if bucket[:3] != (
                    str(source["market_id"]),
                    source["market_slug"],
                    source["bucket"],
                ):
                    mismatches.append(f"{sid}: bucket {index} static metadata mismatch")
                for stored_value, source_value, tolerance, label in (
                    (bucket[3], source.get("model_probability"), 0.000051, "probability"),
                    (bucket[4], source.get("yes_best_ask"), 0.000501, "yes ask"),
                    (bucket[5], source.get("no_best_ask"), 0.000501, "no ask"),
                ):
                    if source_value is None:
                        matched = stored_value is None
                    else:
                        matched = (
                            stored_value is not None
                            and abs(float(stored_value) - float(source_value)) <= tolerance
                        )
                    if not matched:
                        mismatches.append(f"{sid}: bucket {index} {label} mismatch")
                if bucket[6:] != (
                    bool(source.get("warming_window_no")),
                    str(source.get("action") or "skip"),
                ):
                    mismatches.append(f"{sid}: bucket {index} decision flags mismatch")
    finally:
        connection.close()
    return {
        "sample_count": len(rows),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:100],
        "values_reconstruct_within_archive_precision": not mismatches,
    }


def extract_signal_jsonl_after(
    paths: Iterable[Path],
    *,
    cutoff: datetime,
    output_path: Path,
) -> dict[str, Any]:
    """Copy complete JSONL records newer than cutoff without interpreting payloads."""
    cutoff_text = cutoff.astimezone(UTC).isoformat().encode("ascii")
    marker = b'"generated_at":"'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    count = 0
    bytes_written = 0
    with temporary.open("wb") as target:
        for path in paths:
            with path.open("rb") as source:
                for line in source:
                    start = line.find(marker)
                    if start < 0 or not line.endswith(b"\n"):
                        continue
                    start += len(marker)
                    end = line.find(b'"', start)
                    if end < 0 or line[start:end] <= cutoff_text:
                        continue
                    target.write(line)
                    count += 1
                    bytes_written += len(line)
    temporary.replace(output_path)
    return {
        "path": str(output_path.resolve()),
        "cutoff": cutoff.astimezone(UTC).isoformat(),
        "row_count": count,
        "bytes": bytes_written,
    }


def merge_normalized_signal_candidates(
    target_path: Path,
    delta_path: Path,
) -> dict[str, Any]:
    """Merge a verified normalized delta into a candidate in one transaction."""
    connection = duckdb.connect(str(target_path))
    before = _verification_metrics(connection)
    delta_alias = "signal_delta"
    delta_sql = _sql_string(str(delta_path.resolve()).replace("\\", "/"))
    try:
        connection.execute(f"ATTACH {delta_sql} AS {delta_alias} (READ_ONLY)")
        connection.execute("BEGIN TRANSACTION")
        try:
            for table in (
                "calibration_dim",
                "bucket_dim",
                "signal_snapshots",
                "signal_snapshot_reasons",
                "signal_bucket_observations",
                "signal_stream_runs",
            ):
                connection.execute(
                    f"INSERT OR IGNORE INTO {table} SELECT * FROM {delta_alias}.{table}"
                )
            connection.execute(
                """
                UPDATE signal_stream_runs AS r
                SET finished_at = x.finished_at
                FROM (
                    SELECT run_id, max(generated_at) AS finished_at
                    FROM signal_snapshots GROUP BY run_id
                ) AS x
                WHERE r.run_id = x.run_id
                """
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("DETACH signal_delta")
        connection.execute("CHECKPOINT")
        after = _verification_metrics(connection)
    finally:
        connection.close()
    return {
        "before": before,
        "after": after,
        "snapshots_added": after["snapshot_count"] - before["snapshot_count"],
        "bucket_observations_added": (
            after["bucket_observation_count"] - before["bucket_observation_count"]
        ),
        "warming_window_no_triggers_added": (
            after["warming_window_no_trigger_count"]
            - before["warming_window_no_trigger_count"]
        ),
    }


def benchmark_decimal_storage(
    source_path: Path,
    *,
    decimal_path: Path,
    double_path: Path,
) -> dict[str, Any]:
    """Measure archive DECIMAL savings against identical native DOUBLE tables."""
    tables = (
        "calibration_dim",
        "bucket_dim",
        "signal_snapshots",
        "signal_snapshot_reasons",
        "signal_bucket_observations",
        "signal_stream_runs",
    )
    sizes: dict[str, int] = {}
    for mode, target in (("decimal", decimal_path), ("double", double_path)):
        if target.exists():
            target.unlink()
        connection = duckdb.connect(str(target))
        source_sql = _sql_string(str(source_path.resolve()).replace("\\", "/"))
        try:
            connection.execute(f"ATTACH {source_sql} AS benchmark_source (READ_ONLY)")
            for table in tables:
                columns = connection.execute(
                    f"DESCRIBE SELECT * FROM benchmark_source.{table}"
                ).fetchall()
                expressions = []
                for name, type_name, *_ in columns:
                    quoted = '"' + str(name).replace('"', '""') + '"'
                    expression = (
                        f"CAST({quoted} AS DOUBLE) AS {quoted}"
                        if mode == "double" and str(type_name).startswith("DECIMAL")
                        else quoted
                    )
                    expressions.append(expression)
                connection.execute(
                    f"CREATE TABLE {table} AS SELECT {','.join(expressions)} "
                    f"FROM benchmark_source.{table}"
                )
            connection.execute("CHECKPOINT")
        finally:
            connection.close()
        sizes[mode] = target.stat().st_size
    return {
        "sample_source": str(source_path.resolve()),
        "decimal_bytes": sizes["decimal"],
        "double_bytes": sizes["double"],
        "decimal_savings_bytes": sizes["double"] - sizes["decimal"],
        "decimal_savings_percent": round(
            (sizes["double"] - sizes["decimal"]) / max(1, sizes["double"]) * 100,
            2,
        ),
    }


def _verification_metrics(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*), MIN(generated_at), MAX(generated_at),
               count_if(status IN ('stale', 'blocked', 'warning'))
        FROM signal_snapshots
        """
    ).fetchone()
    transitions = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT status, lag(status) OVER (
                PARTITION BY event_slug ORDER BY generated_at, snapshot_id
            ) AS previous_status
            FROM signal_snapshots
        )
        WHERE previous_status IS NOT NULL AND status <> previous_status
          AND (status IN ('stale', 'blocked', 'warning')
               OR previous_status IN ('stale', 'blocked', 'warning'))
        """
    ).fetchone()[0]
    return {
        "snapshot_count": int(row[0]),
        "time_min": row[1].isoformat() if row[1] else None,
        "time_max": row[2].isoformat() if row[2] else None,
        "protected_status_snapshot_count": int(row[3]),
        "protected_status_transition_count": int(transitions),
        "bucket_observation_count": int(
            connection.execute("SELECT COUNT(*) FROM signal_bucket_observations").fetchone()[0]
        ),
        "warming_window_no_trigger_count": int(
            connection.execute(
                "SELECT count_if(warming_window_no) FROM signal_bucket_observations"
            ).fetchone()[0]
        ),
        "reason_count": int(
            connection.execute("SELECT COUNT(*) FROM signal_snapshot_reasons").fetchone()[0]
        ),
        "bucket_dim_count": int(connection.execute("SELECT COUNT(*) FROM bucket_dim").fetchone()[0]),
        "calibration_dim_count": int(
            connection.execute("SELECT COUNT(*) FROM calibration_dim").fetchone()[0]
        ),
    }


def _source_metrics(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*), MIN(generated_at), MAX(generated_at),
               sum(list_count(payload.signals)), sum(list_count(payload.reasons)),
               sum(list_sum(list_transform(
                   payload.signals,
                   x -> CASE WHEN coalesce(
                       json_extract(to_json(x), '$.warming_window_no')::BOOLEAN,
                       false
                   ) THEN 1 ELSE 0 END
               )))
        FROM source_snapshots
        """
    ).fetchone()
    transitions = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT status, lag(status) OVER (
                PARTITION BY event_slug ORDER BY generated_at, run_id, sequence
            ) AS previous_status
            FROM source_snapshots
        )
        WHERE previous_status IS NOT NULL AND status <> previous_status
          AND (status IN ('stale', 'blocked', 'warning')
               OR previous_status IN ('stale', 'blocked', 'warning'))
        """
    ).fetchone()[0]
    return {
        "snapshot_count": int(row[0]),
        "time_min": row[1].isoformat() if row[1] else None,
        "time_max": row[2].isoformat() if row[2] else None,
        "bucket_observation_count": int(row[3] or 0),
        "reason_count": int(row[4] or 0),
        "warming_window_no_trigger_count": int(row[5] or 0),
        "protected_status_transition_count": int(transitions),
    }


def _static_metadata_bytes(connection: duckdb.DuckDBPyConnection) -> int:
    return int(
        connection.execute(
            """
            SELECT coalesce(sum(
                length(event_slug) + length(station_id)
                + list_sum(list_transform(payload.signals, x ->
                    coalesce(length(x.market_id), 0)
                    + coalesce(length(x.market_slug), 0)
                    + coalesce(length(x.bucket), 0)
                ))
                + coalesce(length(payload.top_research_candidate.market_id), 0)
                + coalesce(length(payload.top_research_candidate.market_slug), 0)
                + coalesce(length(payload.top_research_candidate.bucket), 0)
            ), 0)
            FROM source_snapshots
            """
        ).fetchone()[0]
    )


def migrate_signal_snapshots_from_jsonl(
    paths: Iterable[Path],
    *,
    target_path: Path,
    source_database: Path | None = None,
    replace: bool = False,
    protected_tree: Path | None = None,
) -> dict[str, Any]:
    """Build a candidate with vectorized CTAS; never writes the online database."""
    source_paths = tuple(path for path in paths if path.exists())
    _assert_candidate_path(target_path, source_database)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    building = target_path.with_name(f".{target_path.name}.building")
    for candidate in (building, Path(str(building) + ".wal")):
        if candidate.exists():
            candidate.unlink()
    if target_path.exists() and not replace:
        raise FileExistsError(f"candidate already exists: {target_path}")
    protected_before = hash_tree(protected_tree) if protected_tree is not None else {}
    started = datetime.now(UTC)
    connection = duckdb.connect(str(building))
    try:
        spill_directory = target_path.parent / ".duckdb_signal_migration_tmp"
        spill_directory.mkdir(parents=True, exist_ok=True)
        spill_path = str(spill_directory.resolve()).replace("\\", "/")
        connection.execute("SET memory_limit='16GB'")
        connection.execute("SET threads=2")
        connection.execute(f"SET temp_directory={_sql_string(spill_path)}")
        connection.execute("SET preserve_insertion_order=false")
        _create_source_views(connection, source_paths)
        source = _source_metrics(connection)
        repeated_static_bytes = _static_metadata_bytes(connection)
        _create_native_tables(connection)
        _add_constraints_and_indexes(connection)
        connection.execute("CHECKPOINT")
        candidate = _verification_metrics(connection)
        if any(
            source[key] != candidate[key]
            for key in (
                "snapshot_count",
                "time_min",
                "time_max",
                "bucket_observation_count",
                "reason_count",
                "warming_window_no_trigger_count",
                "protected_status_transition_count",
            )
        ):
            raise RuntimeError(
                f"candidate verification failed: source={source}, candidate={candidate}"
            )
    except Exception:
        connection.close()
        raise
    else:
        connection.close()
    protected_after = hash_tree(protected_tree) if protected_tree is not None else {}
    if protected_before != protected_after:
        raise RuntimeError("protected T7/settlement tree changed during migration")
    if target_path.exists():
        target_path.unlink()
    building.replace(target_path)
    size = target_path.stat().st_size
    report = {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "source_files": [str(path.resolve()) for path in source_paths],
        "source": source,
        "candidate": candidate,
        "repeated_static_metadata_bytes": repeated_static_bytes,
        "candidate_bytes": size,
        "candidate_average_bytes_per_snapshot": round(
            size / max(1, candidate["snapshot_count"]), 2
        ),
        "protected_tree_hashes_unchanged": protected_before == protected_after,
        "all_rows_preserved": True,
        "downsampling_applied": False,
        "execution_enabled": False,
    }
    report_path = target_path.with_suffix(target_path.suffix + ".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
