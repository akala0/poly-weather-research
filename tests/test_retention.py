from datetime import UTC, datetime

from poly_weather.retention import RetentionConfig, apply_market_retention


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

