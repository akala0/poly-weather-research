from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import duckdb
import pytest

from poly_weather.research_store import ResearchWarehouse
from poly_weather.retention import maintain_signal_database
from poly_weather.signal_migration import (
    migrate_signal_snapshots_from_jsonl,
    verify_sample_reconstruction,
)
from poly_weather.signal_schema import (
    LegacySignalSchemaError,
    create_normalized_signal_schema,
)
from poly_weather.temperature import celsius_to_fahrenheit, round_whole_degree


def _snapshot(*, sequence: int = 1, status: str = "warning") -> dict:
    generated_at = datetime(2026, 8, 25, 1, 2, 3, tzinfo=UTC)
    calibration = {
        "station_id": "KLGA",
        "lead_days": 1,
        "sample_count": 81,
        "sample_date_min": "2026-06-01",
        "sample_date_max": "2026-08-20",
        "bias_f": 0.4555083,
        "residual_std_f": 2.2183142,
        "raw_error_rms_f": 2.2511449,
        "validation_test_samples": 51,
        "mae_raw": 1.749,
        "mae_calibrated": 1.793,
        "rmse_raw": 2.324,
        "rmse_calibrated": 2.375,
        "brier_raw": 0.087408,
        "brier_calibrated": 0.082895,
        "log_loss_raw": 0.282486,
        "log_loss_calibrated": 0.280795,
        "model_weights": {"gfs": 0.321166, "icon": 0.351763, "gem": 0.327071},
        "ready": True,
        "apply_bias": True,
        "strategy": "bias_corrected",
        "reason": "passed",
    }
    signal = {
        "market_id": "m1",
        "market_slug": "highest-temperature-in-nyc-on-august-25-2026-82-83f",
        "bucket": "Will the highest temperature be between 82-83F?",
        "bucket_lower_f": 82.0,
        "bucket_upper_f": 83.0,
        "bucket_lower_value": 82,
        "bucket_upper_value": 83,
        "temperature_unit": "fahrenheit",
        "bucket_width_degrees": 2,
        "raw_model_probability": 0.12345678,
        "calibrated_model_probability": 0.23456789,
        "model_probability": 0.23456789,
        "yes_best_bid": 0.12349,
        "yes_best_ask": 0.13549,
        "no_best_bid": 0.86451,
        "no_best_ask": 0.87651,
        "yes_taker_fee_per_share": 0.005857,
        "no_taker_fee_per_share": 0.005412,
        "no_book_complete": True,
        "no_book_age_minutes": 0.236,
        "physical_margin_f": -2.04,
        "margin_tier": "-3..-2F",
        "eliminated": False,
        "warming_window_no_conditions_met": True,
        "warming_window_no": True,
        "raw_research_candidate": "buy_yes",
        "raw_net_edge_after_buffer": 0.111119,
        "research_candidate": "buy_yes",
        "net_edge_after_buffer": 0.099999,
        "execution_estimates": [
            {
                "size_usd": size,
                "estimated_fill_yes": 0.14549,
                "estimated_fill_no": 0.88749,
                "slippage_bps_yes": 1234.56,
                "slippage_bps_no": 2345.67,
                "taker_fee_per_share_yes": 0.006214,
                "taker_fee_per_share_no": 0.004993,
                "filled_fraction_yes": 0.99999,
                "filled_fraction_no": 1.0,
                "executable_candidate": "buy_yes",
                "executable_net_edge_after_buffer": 0.079999,
            }
            for size in (50.0, 200.0, 1000.0)
        ],
        "paper_alert_eligible": False,
        "edge_gate_blocked": False,
        "action": "skip",
    }
    payload = {
        "generated_at": generated_at.isoformat(),
        "event_id": "e1",
        "event_slug": "highest-temperature-in-nyc-on-august-25-2026",
        "station_id": "KLGA",
        "target_date": "2026-08-25",
        "status": status,
        "contract_verified": True,
        "calibration_ready": True,
        "calibration": calibration,
        "reasons": ["test warning"],
        "current_observed_high_f": 81.55,
        "warming_rate_f_per_hour": 1.24,
        "hours_to_typical_peak": 2.26,
        "typical_peak_local": "14:51",
        "nws_temperature_f": 80.55,
        "nws_age_minutes": 1.24,
        "wrh_temperature_f": 80.45,
        "wrh_age_minutes": 2.26,
        "metar_temperature_f": 80.45,
        "metar_age_minutes": 2.26,
        "source_delta_f": 0.1,
        "deterministic": {
            "raw": {"daily_high_f": 82.44},
            "selected": {
                "daily_high_f": 82.8555,
                "residual_std_f": 2.2183,
                "bias_applied": True,
            },
        },
        "cost_buffer": 0.01,
        "min_net_edge": 0.03,
        "top_research_candidate": signal,
        "paper_alert_eligible": False,
        "signals": [signal],
        "execution_enabled": False,
    }
    return {
        "run_id": "run-1",
        "sequence": sequence,
        "generated_at": generated_at,
        "event_slug": payload["event_slug"],
        "station_id": "KLGA",
        "status": status,
        "payload": payload,
    }


def test_normalized_writer_uses_native_columns_and_archive_precision(tmp_path) -> None:
    row = _snapshot()
    original = deepcopy(row)
    with ResearchWarehouse(tmp_path / "signals.duckdb") as warehouse:
        assert warehouse.append_signal_snapshots([row]) == 1
        columns = {
            value[1]
            for value in warehouse.connection.execute(
                "PRAGMA table_info('signal_snapshots')"
            ).fetchall()
        }
        assert "payload_json" not in columns
        assert "snapshot_id" in columns
        bucket = warehouse.connection.execute(
            """
            SELECT model_probability, yes_best_bid, physical_margin_f,
                   fill_yes_50, filled_fraction_yes_50,
                   yes_taker_fee_per_share, taker_fee_per_share_no_50
            FROM signal_bucket_observations
            """
        ).fetchone()
        assert bucket == (
            Decimal("0.2346"),
            Decimal("0.123"),
            Decimal("-2.0"),
            Decimal("0.145"),
            Decimal("1.0000"),
            Decimal("0.00586"),
            Decimal("0.00499"),
        )
        dimension = warehouse.connection.execute(
            "SELECT event_slug, bucket_question, bucket_lower_f, bucket_upper_f FROM bucket_dim"
        ).fetchone()
        assert dimension[0] == row["event_slug"]
        assert dimension[1] == row["payload"]["signals"][0]["bucket"]
        assert dimension[2:] == (Decimal("82.0"), Decimal("83.0"))
    assert row == original


def test_writer_uses_column_names_after_v2_fee_column_migration(tmp_path) -> None:
    """ALTER-added fee columns may be physically last in an existing v2 DB."""
    with ResearchWarehouse(tmp_path / "signals.duckdb") as warehouse:
        connection = warehouse.connection
        connection.execute("DROP INDEX signal_bucket_market_time_idx")
        columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('signal_bucket_observations')"
            ).fetchall()
            if "taker_fee" not in row[1]
        ]
        connection.execute(
            "CREATE TABLE signal_bucket_v2 AS SELECT "
            + ",".join(columns)
            + " FROM signal_bucket_observations"
        )
        connection.execute("DROP TABLE signal_bucket_observations")
        connection.execute(
            "ALTER TABLE signal_bucket_v2 RENAME TO signal_bucket_observations"
        )
        connection.execute(
            "CREATE UNIQUE INDEX signal_bucket_pk_simulation "
            "ON signal_bucket_observations(snapshot_id, market_id)"
        )
        create_normalized_signal_schema(connection)

        assert warehouse.append_signal_snapshots([_snapshot()]) == 1
        stored = connection.execute(
            """
            SELECT margin_tier, eliminated, yes_taker_fee_per_share,
                   taker_fee_per_share_no_50
            FROM signal_bucket_observations
            """
        ).fetchone()
        assert stored == (
            "-3..-2F",
            False,
            Decimal("0.00586"),
            Decimal("0.00499"),
        )
def test_archive_quantization_does_not_change_settlement_rounding_path(tmp_path) -> None:
    row = _snapshot()
    source_c = Decimal("25.55")
    exact_settlement_f = round_whole_degree(celsius_to_fahrenheit(source_c))
    with ResearchWarehouse(tmp_path / "signals.duckdb") as warehouse:
        warehouse.append_signal_snapshots([row])
        stored = warehouse.connection.execute(
            "SELECT current_observed_high_f FROM signal_snapshots"
        ).fetchone()[0]
    assert stored == Decimal("81.6")
    assert exact_settlement_f == Decimal("78")
    assert round_whole_degree(celsius_to_fahrenheit(source_c)) == exact_settlement_f


def test_legacy_payload_schema_fails_closed_for_new_writes(tmp_path) -> None:
    path = tmp_path / "legacy.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE signal_snapshots(run_id VARCHAR, sequence BIGINT, payload_json VARCHAR)"
    )
    connection.close()
    with ResearchWarehouse(path) as warehouse, pytest.raises(LegacySignalSchemaError):
        warehouse.append_signal_snapshots([_snapshot()])


def test_vectorized_candidate_migration_is_lossless_and_idempotent(tmp_path) -> None:
    rows = [_snapshot(sequence=1), _snapshot(sequence=2, status="stale")]
    source = tmp_path / "events.jsonl"
    with source.open("w", encoding="utf-8") as handle:
        for row in rows:
            serializable = {**row, "generated_at": row["generated_at"].isoformat()}
            handle.write(json.dumps(serializable, separators=(",", ":")) + "\n")
    protected = tmp_path / "no_forward_validation"
    protected.mkdir()
    (protected / "events.jsonl").write_text('{"critical":true}\n', encoding="utf-8")
    target = tmp_path / "signal_stream.candidate.duckdb"
    first = migrate_signal_snapshots_from_jsonl(
        [source], target_path=target, protected_tree=protected
    )
    assert first["source"]["snapshot_count"] == 2
    assert first["candidate"]["snapshot_count"] == 2
    assert first["candidate"]["warming_window_no_trigger_count"] == 2
    assert first["protected_tree_hashes_unchanged"] is True
    assert first["downsampling_applied"] is False
    sample = verify_sample_reconstruction(
        [source], candidate_path=target, per_file=2
    )
    assert sample["mismatch_count"] == 0
    second = migrate_signal_snapshots_from_jsonl(
        [source], target_path=target, protected_tree=protected, replace=True
    )
    assert second["candidate"] == first["candidate"]
    connection = duckdb.connect(str(target), read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM bucket_dim").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM signal_bucket_observations"
        ).fetchone()[0] == 2
    finally:
        connection.close()


def test_migration_refuses_online_database_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="online signal database"):
        migrate_signal_snapshots_from_jsonl(
            [tmp_path / "missing.jsonl"],
            target_path=tmp_path / "signal_stream.duckdb",
        )


def test_signal_retention_is_disabled_by_default_and_preserves_critical_rows(tmp_path) -> None:
    path = tmp_path / "signals.duckdb"
    healthy = _snapshot(sequence=1, status="healthy")
    healthy["payload"]["status"] = "healthy"
    healthy["payload"]["signals"][0]["warming_window_no"] = False
    critical = _snapshot(sequence=2, status="warning")
    with ResearchWarehouse(path) as warehouse:
        warehouse.append_signal_snapshots([healthy, critical])
    default_result = maintain_signal_database(path)
    assert default_result["deleted"] == 0
    result = maintain_signal_database(
        path,
        retention_days=1,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )
    assert result["deleted"] == 1
    connection = duckdb.connect(str(path), read_only=True)
    try:
        assert connection.execute(
            "SELECT status FROM signal_snapshots"
        ).fetchall() == [("warning",)]
        assert connection.execute(
            "SELECT count_if(warming_window_no) FROM signal_bucket_observations"
        ).fetchone()[0] == 1
    finally:
        connection.close()
