from datetime import UTC, datetime

from poly_weather.retention import (
    RetentionConfig,
    apply_market_retention,
    directory_storage_bytes,
    disk_capacity_status,
)


def test_retention_only_compresses_and_expires_market_raw_partitions(tmp_path) -> None:
    raw = tmp_path / "raw" / "polymarket_clob_websocket"
    old = raw / "2026-07-01"
    compress = raw / "2026-08-20"
    recent = raw / "2026-08-24"
    aggregate = tmp_path / "research.duckdb"
    for partition in (old, compress, recent):
        partition.mkdir(parents=True)
        (partition / "events.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    aggregate.write_bytes(b"aggregate")

    result = apply_market_retention(
        tmp_path,
        config=RetentionConfig(raw_retention_days=30, compress_after_days=2),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )

    assert old.exists() is False
    assert (compress / "events.jsonl").exists() is False
    assert (compress / "events.jsonl.gz").exists() is True
    assert (recent / "events.jsonl").exists() is True
    assert aggregate.read_bytes() == b"aggregate"
    assert result["aggregate_data_retained"] is True


def test_disk_capacity_reports_logical_and_physical_rates(tmp_path) -> None:
    partition = tmp_path / "raw" / "polymarket_clob_websocket" / "2026-08-24"
    partition.mkdir(parents=True)
    (partition / "events.jsonl").write_bytes(b"x" * 4096)
    logical, physical = directory_storage_bytes(partition)
    assert logical == 4096
    assert physical >= 0
    result = disk_capacity_status(tmp_path, projected_trimmed_gb_per_day=2.2)
    assert result["recent_market_archive_logical_bytes_per_day"] == 4096
    assert result["recent_market_archive_physical_bytes_per_day"] >= 0
    assert result["projected_trimmed_market_archive_logical_gb_per_day"] == 2.2
