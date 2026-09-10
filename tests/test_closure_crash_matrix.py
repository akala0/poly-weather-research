"""Temporary real-collector recovery at both sides of each publication."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_public_trade_collection import _FakeClient, _trade
from test_reliability_storage import coverage_fixture

from poly_weather import public_trade_collection as collection
from poly_weather import receipt_journal as receipt
from poly_weather.trade_tape_analysis import load_event_trade_tapes


@pytest.mark.parametrize("stage", ["fact", "witness", "manifest", "tape", "cursor", "audit"])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("existing", [False, True])
def test_ec_cross_file_commit_recovery(tmp_path, monkeypatch, stage, when, existing):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    if existing:
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "old")),
                                             output_dir=root, clock=lambda: at + timedelta(hours=1))
        coverage = [replace(coverage[0], end_at=at + timedelta(minutes=2))]
    original_journal = receipt.atomic_json_write
    original_tape = collection._atomic_json_write

    def classify(path):
        if path.name.endswith(".fact.json"):
            return "fact"
        if path.name.endswith(".witness.json"):
            return "witness"
        if "materializations" in path.parts:
            return "manifest"
        return {"event-1.json": "tape", ".depth_trade_cursor.json": "cursor",
                "depth_trade_coverage.json": "audit"}.get(path.name)

    def wrap(original):
        def write(path, payload, **kwargs):
            target = classify(path) == stage
            if target and when == "before":
                raise OSError("injected publication crash")
            result = original(path, payload, **kwargs)
            if target and when == "after":
                raise OSError("injected publication crash")
            return result
        return write

    with monkeypatch.context() as patch:
        patch.setattr(receipt, "atomic_json_write", wrap(original_journal))
        patch.setattr(collection, "_atomic_json_write", wrap(original_tape))
        with pytest.raises(OSError, match="publication crash"):
            collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "new")),
                                                 output_dir=root, clock=lambda: at + timedelta(hours=2))
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    load_event_trade_tapes(root)
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}
    client = _FakeClient(_trade("token", at, "new"))
    immutable = None
    for _ in range(2):
        collection.collect_depth_event_trades(coverage, client=client, output_dir=root,
                                             clock=lambda: at + timedelta(hours=3))
        payload = json.loads((root / "event-1.json").read_text())
        collection.verify_materialized_receipts(root / "event-1.json", payload)
        assert len(payload["trades"]) == 1 + int(existing)
        facts = {p: p.read_bytes() for p in (root / ".receipt_journal").glob("*.json")}
        if immutable is not None:
            assert facts == immutable
        immutable = facts
    assert len(client.calls) == int(stage == "fact" and when == "before")


def test_ec_preflight_corrupt_tape_cannot_publish_recovery_witness(tmp_path, monkeypatch):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "old")),
                                         output_dir=root, clock=lambda: at + timedelta(hours=1))
    with monkeypatch.context() as patch:
        patch.setattr(receipt.ReceiptJournal, "_witness",
                      lambda *args: (_ for _ in ()).throw(OSError("witness crash")))
        with pytest.raises(OSError):
            collection.collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=2))],
                                                 client=_FakeClient(_trade("token", at, "new")),
                                                 output_dir=root, clock=lambda: at + timedelta(hours=2))
    path = root / "event-1.json"
    path.write_text('{"trades":[]}')
    before = {p: p.read_bytes() for p in (root / ".receipt_journal").glob("*.witness.json")}
    with pytest.raises(receipt.ReceiptIntegrityError):
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "old")), output_dir=root)
    assert path.read_text() == '{"trades":[]}'
    assert before == {p: p.read_bytes() for p in (root / ".receipt_journal").glob("*.witness.json")}


def test_ec_readers_ignore_corrupt_unclaimed_tail_but_writer_blocks(tmp_path, monkeypatch):
    from poly_weather.shadow_runtime import (
        _public_trade_events_from_file,
        _public_trade_events_from_files,
    )

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "old")),
                                         output_dir=root, clock=lambda: at + timedelta(hours=1))
    with monkeypatch.context() as patch:
        patch.setattr(receipt.ReceiptJournal, "_witness",
                      lambda *args: (_ for _ in ()).throw(OSError("witness crash")))
        with pytest.raises(OSError):
            collection.collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=2))],
                                                 client=_FakeClient(_trade("token", at, "new")),
                                                 output_dir=root, clock=lambda: at + timedelta(hours=2))
    tail = sorted((root / ".receipt_journal").glob("*.fact.json"))[-1]
    tail.write_bytes(b'{"torn":')
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    assert len(load_event_trade_tapes(root)["event-1"]) == 1
    _public_trade_events_from_file(root / "event-1.json")
    _public_trade_events_from_files([root / "event-1.json"])
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}
    with pytest.raises(receipt.ReceiptIntegrityError):
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "old")), output_dir=root)
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}
