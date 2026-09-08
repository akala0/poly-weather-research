import gzip
from datetime import UTC, datetime

import pytest

import poly_weather.retention as retention
from poly_weather.retention import (
    RetentionConfig,
    apply_market_retention,
    directory_storage_bytes,
    disk_capacity_status,
)


def test_retention_defers_unsealed_compression_and_expiry_for_all_sources(tmp_path) -> None:
    aggregate = tmp_path / "research.duckdb"
    protected = tmp_path / "raw" / "no_forward_validation" / "2026-07-01"
    protected.mkdir(parents=True)
    (protected / "events.jsonl").write_text('{"critical":true}\n', encoding="utf-8")
    partitions = {}
    for source in retention.RAW_RETENTION_SOURCES:
        root = tmp_path / "raw" / source
        partitions[source] = (
            root / "2026-07-01",
            root / "2026-08-20",
            root / "2026-08-24",
        )
        for partition in partitions[source]:
            partition.mkdir(parents=True)
            (partition / "events.jsonl").write_bytes(b'{"x":1}\n')
    aggregate.write_bytes(b"aggregate")

    result = apply_market_retention(
        tmp_path,
        config=RetentionConfig(raw_retention_days=30, compress_after_days=2),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    for old, compress, recent in partitions.values():
        assert (old / "events.jsonl").read_bytes() == b'{"x":1}\n'
        assert (compress / "events.jsonl").read_bytes() == b'{"x":1}\n'
        assert (compress / "events.jsonl.gz").exists() is False
        assert (recent / "events.jsonl").exists() is True
    assert (protected / "events.jsonl").read_text(encoding="utf-8") == '{"critical":true}\n'
    assert aggregate.read_bytes() == b"aggregate"
    assert result["aggregate_data_retained"] is True
    assert result["raw_retention_sources"] == list(retention.RAW_RETENTION_SOURCES)
    assert len(result["deferred_raw_partitions"]) == 6
    assert result["compressed"] == result["deleted"] == []
    assert result["raw_retention_state"] == "deferred_unproven_writer_exclusion"


def test_signal_snapshot_retention_root_rejects_path_escape(tmp_path) -> None:
    root = tmp_path / "raw" / "signal_snapshot"
    root.mkdir(parents=True)

    with pytest.raises(ValueError, match="escapes configured raw root"):
        retention._assert_within(tmp_path / "raw" / "outside", root)


def test_retention_preserves_matching_gzip_without_writer_exclusion(tmp_path) -> None:
    partition = tmp_path / "raw" / "polymarket_clob_websocket" / "2026-08-20"
    partition.mkdir(parents=True)
    jsonl = partition / "events.jsonl"
    archive = partition / "events.jsonl.gz"
    payload = b'{"complete_book":true}\n'
    jsonl.write_bytes(payload)
    with gzip.open(archive, "wb") as handle:
        handle.write(payload)

    result = apply_market_retention(
        tmp_path,
        config=RetentionConfig(raw_retention_days=30, compress_after_days=2),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert jsonl.read_bytes() == payload
    assert archive.exists() is True
    assert result["reconciled_existing_archives"] == []
    assert result["unresolved_duplicate_archives"] == [str(jsonl)]


def test_retention_preserves_a_mismatched_existing_gzip_archive(tmp_path) -> None:
    partition = tmp_path / "raw" / "polymarket_clob_websocket" / "2026-08-20"
    partition.mkdir(parents=True)
    jsonl = partition / "events.jsonl"
    archive = partition / "events.jsonl.gz"
    jsonl.write_bytes(b'{"complete_book":true}\n')
    with gzip.open(archive, "wb") as handle:
        handle.write(b'{"complete_book":false}\n')

    result = apply_market_retention(
        tmp_path,
        config=RetentionConfig(raw_retention_days=30, compress_after_days=2),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert jsonl.exists() is True
    assert archive.exists() is True
    assert result["reconciled_existing_archives"] == []
    assert result["unresolved_duplicate_archives"] == [str(jsonl)]


def test_disk_capacity_reports_logical_and_physical_rates(tmp_path) -> None:
    partition = tmp_path / "raw" / "polymarket_clob_websocket" / "2026-08-24"
    partition.mkdir(parents=True)
    (partition / "events.jsonl").write_bytes(b"x" * 4096)
    logical, physical = directory_storage_bytes(partition)
    assert logical == 4096
    assert physical >= 0
    result = disk_capacity_status(
        tmp_path,
        projected_trimmed_gb_per_day=2.2,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert result["recent_market_archive_logical_bytes_per_day"] == 4096
    assert result["recent_market_archive_physical_bytes_per_day"] >= 0
    assert result["projected_trimmed_market_archive_logical_gb_per_day"] == 2.2
