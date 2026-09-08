"""Reliability audit counterexamples; every mutation is under tmp_path."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_public_trade_collection import _checkpoint, _FakeClient, _trade

from poly_weather.public_trade_collection import (
    collect_depth_event_trades,
    discover_depth_event_coverage,
)
from poly_weather.retention import apply_market_retention
from poly_weather.trade_tape_analysis import load_event_trade_tapes


def coverage_fixture(tmp_path):
    at = datetime(2026, 8, 27, tzinfo=UTC)
    path = tmp_path / "checkpoint.jsonl"
    path.write_text(json.dumps(_checkpoint(at, asset="token")) + "\n", encoding="utf-8")
    return at, discover_depth_event_coverage([path])


def test_f04_corrupt_tape_bytes_are_not_overwritten(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    tape = root / "event-1.json"
    original = b"{broken-old-evidence"
    tape.write_bytes(original)
    try:
        collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                                  output_dir=root, now=at + timedelta(hours=1))
    except ValueError:
        pass
    assert tape.read_bytes() == original


def test_f05_late_append_is_not_deleted(tmp_path, monkeypatch):
    from poly_weather import retention

    source = tmp_path / "raw" / "polymarket_clob_websocket" / "2026-08-20" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"old\n")
    copy = retention.shutil.copyfileobj

    def append_after_copy(reader, writer, length):
        copy(reader, writer, length)
        with source.open("ab") as handle:
            handle.write(b"late\n")

    monkeypatch.setattr(retention.shutil, "copyfileobj", append_after_copy)
    apply_market_retention(tmp_path, now=datetime(2026, 8, 27, tzinfo=UTC),
                           maintain_signal_database_online=False)
    assert source.exists(), "unsealed source is not safe to unlink"
    assert source.read_bytes().startswith(b"old\n")


def test_f03_no_request_refresh_keeps_first_receipt(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    client = _FakeClient(_trade("token", at, "tx"))
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at + timedelta(hours=1))
    first = load_event_trade_tapes(root)["event-1"][0].available_at
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at + timedelta(hours=2))
    second = load_event_trade_tapes(root)["event-1"][0].available_at
    assert len(client.calls) == 1
    assert second == first


@pytest.mark.parametrize("raw", [b"", b" ", b"{", b"[]", b"{}", b'{"trades": [null]}', b'\xff'])
def test_f04_bad_tape_is_quarantined_and_other_event_collects(tmp_path, raw):
    from dataclasses import replace

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    tape = root / "event-1.json"
    tape.write_bytes(raw)
    client = _FakeClient(_trade("token", at, "tx"))
    result = collect_depth_event_trades(
        [*coverage, replace(coverage[0], event_slug="healthy-event")],
        client=client, output_dir=root, now=at,
    )
    assert tape.read_bytes() == raw
    assert result["storage_quarantined_event_count"] == 1
    assert result["trade_count_complete"] is False
    assert len(client.calls) == 1
    cursor = json.loads((root / ".depth_trade_cursor.json").read_text())
    assert "event-1" not in cursor["events"]
    assert cursor["events"]["healthy-event"]["watermark_end"] == at.isoformat()


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"{}", b'{"events": []}',
                                  b'{"events": {"event-1": null}}',
                                  b'{"events": {"event-1": {"watermark_end": "bad"}}}'])
def test_f04_bad_cursor_stops_before_request(tmp_path, raw):
    from poly_weather.public_trade_collection import TradeStorageError

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    cursor = root / ".depth_trade_cursor.json"
    cursor.write_bytes(raw)
    client = _FakeClient(_trade("token", at, "tx"))
    with pytest.raises(TradeStorageError):
        collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    assert cursor.read_bytes() == raw
    assert not client.calls
    assert not (root / "event-1.json").exists()


@pytest.mark.parametrize("object_name", ["event-1.json", ".depth_trade_cursor.json"])
def test_f04_unreadable_object_is_not_overwritten(tmp_path, monkeypatch, object_name):
    from pathlib import Path

    from poly_weather.public_trade_collection import TradeStorageError

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    target = root / object_name
    target.write_bytes(b"preserved")
    read = Path.read_bytes

    def denied(path):
        if path == target:
            raise PermissionError("injected read denial")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    client = _FakeClient(_trade("token", at, "tx"))
    if object_name.startswith("."):
        with pytest.raises(TradeStorageError, match="unreadable"):
            collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    else:
        result = collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
        assert result["events"][0]["storage_state"] == "unreadable"
    assert read(target) == b"preserved"
    assert not client.calls


@pytest.mark.parametrize("boundary", ["event-1.json", ".depth_trade_cursor.json", "depth_trade_coverage.json"])
@pytest.mark.parametrize("after", [False, True])
def test_f04_commit_failure_retry_is_idempotent(tmp_path, monkeypatch, boundary, after):
    from poly_weather import public_trade_collection as collector

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    client = _FakeClient(_trade("token", at, "tx"))
    write = collector._atomic_json_write

    def interrupted(path, payload):
        if path.name == boundary:
            if after:
                write(path, payload)
            raise OSError("injected commit boundary")
        write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(collector, "_atomic_json_write", interrupted)
        with pytest.raises(OSError, match="commit boundary"):
            collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    cursor = root / ".depth_trade_cursor.json"
    if cursor.exists():
        assert (root / "event-1.json").exists()
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    tape = json.loads((root / "event-1.json").read_text())
    assert tape["trade_count"] == 1
    assert len(tape["trades"]) == 1
    assert json.loads(cursor.read_text())["events"]["event-1"]["watermark_end"] == at.isoformat()


def test_f04_collector_lock_rejects_concurrent_writer(tmp_path):
    from poly_weather.public_trade_collection import _writer_lock

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    client = _FakeClient(_trade("token", at, "tx"))
    with _writer_lock(root / ".trade_collection.lock"):
        with pytest.raises(OSError):
            collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    assert not client.calls
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    assert len(client.calls) == 1


def test_f04_verified_empty_tape_is_distinct_from_empty_file(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"

    class EmptyClient:
        def market_trades(self, **kwargs):
            return []

    result = collect_depth_event_trades(coverage, client=EmptyClient(), output_dir=root, now=at)
    assert result["collected_zero_event_count"] == 1
    assert result["trade_count_complete"] is True
    assert json.loads((root / "event-1.json").read_text())["trades"] == []


def test_f04_missing_tape_does_not_reuse_advanced_cursor(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    (root / ".depth_trade_cursor.json").write_text(json.dumps({
        "schema_version": 1, "events": {"event-1": {
            "watermark_end": (at + timedelta(hours=1)).isoformat(),
        }},
    }))
    client = _FakeClient(_trade("token", at, "tx"))
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    assert client.calls[0][1] == at


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("after", [False, True])
def test_f04_durable_writer_failure_keeps_recoverable_tape(tmp_path, monkeypatch, operation, after):
    from dataclasses import replace

    from poly_weather import runtime_safety

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    client = _FakeClient(_trade("token", at, "tx"))
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at)
    tape = root / "event-1.json"
    before = json.loads(tape.read_text())
    cursor = root / ".depth_trade_cursor.json"
    cursor_bytes = cursor.read_bytes()
    original = getattr(runtime_safety.os, operation)

    def fault(*args, **kwargs):
        if after:
            original(*args, **kwargs)
        raise OSError("injected durable write failure")

    advanced = [replace(coverage[0], end_at=at + timedelta(minutes=1))]
    with monkeypatch.context() as patch:
        patch.setattr(runtime_safety.os, operation, fault)
        with pytest.raises(OSError, match="durable write failure"):
            collect_depth_event_trades(advanced, client=client, output_dir=root, now=at)
    assert json.loads(tape.read_text())["trades"] == before["trades"]
    assert cursor.read_bytes() == cursor_bytes
    collect_depth_event_trades(advanced, client=client, output_dir=root, now=at)
    assert json.loads(tape.read_text())["trade_count"] == 1


@pytest.mark.parametrize("archive_bytes", [b"bad-gzip", None])
def test_f05_unsealed_retention_never_opens_mutation_handles(tmp_path, monkeypatch, archive_bytes):
    from pathlib import Path

    source = tmp_path / "raw" / "signal_snapshot" / "2026-08-20" / "events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original\n")
    archive = source.with_suffix(".jsonl.gz")
    if archive_bytes is not None:
        archive.write_bytes(archive_bytes)

    def forbidden(*args, **kwargs):
        raise AssertionError("unsealed retention attempted mutation")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", forbidden)
        patch.setattr(Path, "replace", forbidden)
        patch.setattr(Path, "open", forbidden)
        result = apply_market_retention(tmp_path, now=datetime(2026, 8, 27, tzinfo=UTC),
                                        maintain_signal_database_online=False)
    assert result["deferred_raw_partitions"][0]["reason"] == "unproven_seal_and_writer_exclusion"
    assert source.read_bytes() == b"original\n"
    if archive_bytes is not None:
        assert archive.read_bytes() == archive_bytes
