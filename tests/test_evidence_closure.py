"""E01 production loader counterexamples; all evidence is temporary."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_public_trade_collection import _trade
from test_reliability_storage import coverage_fixture

from poly_weather.public_trade_collection import (
    collect_depth_event_trades,
    verify_materialized_receipts,
)
from poly_weather.receipt_journal import ReceiptIntegrityError
from poly_weather.shadow_runtime import _public_trade_events_from_file
from poly_weather.trade_tape_analysis import load_event_trade_tapes


@pytest.mark.parametrize("damage", ["all", "partial", "duplicate", "provenance", "contract",
                                   "anchor", "scope", "count", "non_object", "invalid_json", "missing_scope"])
@pytest.mark.parametrize("reader", ["historical", "incremental"])
def test_ec_missing_or_contradictory_members_rejected(tmp_path, damage, reader):
    at, coverage = coverage_fixture(tmp_path)

    class Client:
        def market_trades(self, **kwargs):
            return [_trade("token", at, "tx-a"), _trade("token", at, "tx-b")]

    root = tmp_path / "tapes"
    collect_depth_event_trades(coverage, client=Client(), output_dir=root,
                              clock=lambda: at + timedelta(hours=1))
    path = root / "event-1.json"
    payload = json.loads(path.read_text())
    if damage == "all":
        payload["trades"] = []
        payload["trade_count"] = 0
    elif damage == "partial":
        payload["trades"].pop()
        payload["trade_count"] = 1
    elif damage == "duplicate":
        payload["trades"].append(payload["trades"][0])
        payload["trade_count"] = 3
    elif damage == "provenance":
        for row in payload["trades"]:
            row.pop("receipt_provenance")
    elif damage == "contract":
        payload.pop("receipt_contract")
    elif damage == "anchor":
        payload.pop("receipt_journal_anchor")
    elif damage == "scope":
        payload["event_slug"] = "wrong-event"
    elif damage == "count":
        payload["trade_count"] = 999
    elif damage == "non_object":
        payload = []
    elif damage == "missing_scope":
        payload.pop("event_slug")
    path.write_text(json.dumps(payload))
    if damage == "invalid_json":
        path.write_text('{')
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    with pytest.raises(ReceiptIntegrityError):
        if reader == "historical":
            load_event_trade_tapes(root)
        else:
            _public_trade_events_from_file(path)
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}


def test_ec_old_prefix_and_uncommitted_tail_remain_readable(tmp_path, monkeypatch):
    from test_public_trade_collection import _FakeClient

    from poly_weather import receipt_journal

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                              output_dir=root, clock=lambda: at + timedelta(hours=1))
    path = root / "event-1.json"
    old = json.loads(path.read_text())
    collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=2))],
                              client=_FakeClient(_trade("token", at, "new")),
                              output_dir=root, clock=lambda: at + timedelta(hours=2))
    verify_materialized_receipts(path, old)
    monkeypatch.setattr(receipt_journal.ReceiptJournal, "_witness",
                        lambda *args: (_ for _ in ()).throw(OSError("delayed witness")))
    with pytest.raises(OSError):
        collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=3))],
                                  client=_FakeClient(_trade("token", at, "later")),
                                  output_dir=root, clock=lambda: at + timedelta(hours=3))
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    verify_materialized_receipts(path, old)
    assert len(load_event_trade_tapes(root)["event-1"]) == 2
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}


@pytest.mark.parametrize("kind", ["zero", "failure", "legacy"])
def test_ec_empty_failure_legacy_and_missing_tape_reconstruction(tmp_path, kind):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    path = root / "event-1.json"
    legacy = _trade("token", at, "old").as_json()
    if kind == "legacy":
        path.write_text(json.dumps({"event_slug": "event-1", "trades": [legacy]}))

    class Client:
        def market_trades(self, **kwargs):
            if kind == "failure":
                raise OSError("fake failed page")
            return []

    collect_depth_event_trades(coverage, client=Client(), output_dir=root,
                              clock=lambda: at + timedelta(hours=1))
    payload = json.loads(path.read_text())
    verify_materialized_receipts(path, payload)
    assert payload["collection_status"] == ("collection_error" if kind == "failure" else "collected_zero")
    if kind == "legacy":
        assert payload["trades"] == [legacy]
        assert load_event_trade_tapes(root)["event-1"][0].available_at is None
        path.unlink()  # Explicit missing-derivative fixture, only tmp_path.
        collect_depth_event_trades(coverage, client=Client(), output_dir=root,
                                  clock=lambda: at + timedelta(hours=2))
        assert json.loads(path.read_text())["trades"] == [legacy]


@pytest.mark.parametrize("scale", [12, 24])
@pytest.mark.parametrize("reader", ["historical", "incremental"])
def test_ec_reader_cost_measurement(tmp_path, monkeypatch, scale, reader):
    import time
    import tracemalloc

    from poly_weather import receipt_journal

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"

    class Client:
        index = 0

        def market_trades(self, **kwargs):
            self.index += 1
            return [replace(_trade("token", at, f"tx-{self.index}"), event_slug=f"event-{self.index}")]

    collect_depth_event_trades([replace(coverage[0], event_slug=f"event-{n}") for n in range(1, scale + 1)],
                              client=Client(), output_dir=root, clock=lambda: at + timedelta(hours=1))
    counts = {"reads": 0, "bytes": 0}
    original = receipt_journal.Path.read_bytes

    def measured(path):
        raw = original(path)
        if ".receipt_journal" in str(path):
            counts["reads"] += 1
            counts["bytes"] += len(raw)
        return raw

    monkeypatch.setattr(receipt_journal.Path, "read_bytes", measured)
    tracemalloc.start()
    started = time.perf_counter()
    if reader == "historical":
        loaded = load_event_trade_tapes(root)
        assert sum(map(len, loaded.values())) == scale
    else:
        from poly_weather.shadow_runtime import _public_trade_events_from_files

        _public_trade_events_from_files(sorted(root.glob("event-*.json")))
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert counts["reads"] <= 6 * scale
    print(json.dumps({"reader": reader, "scale": scale, **counts, "seconds": elapsed, "peak_bytes": peak}))


@pytest.mark.parametrize("reader", ["historical", "incremental"])
def test_ec_cycle_recheck_rejects_mutation_even_with_original_mtime(tmp_path, monkeypatch, reader):
    import os

    from test_public_trade_collection import _FakeClient

    from poly_weather.receipt_journal import ReceiptJournal
    from poly_weather.shadow_runtime import _public_trade_events_from_files

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                              output_dir=root, clock=lambda: at + timedelta(hours=1))
    original = ReceiptJournal.assert_unchanged

    def tamper(journal):
        fact = next(journal.root.glob("*.fact.json"))
        stat = fact.stat()
        fact.write_bytes(fact.read_bytes().replace(b'"schema_version": 1', b'"schema_version": 9'))
        # Ensure mutation even if the writer uses compact serialization.
        fact.write_bytes(fact.read_bytes() + b'!')
        os.utime(fact, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        original(journal)

    monkeypatch.setattr(ReceiptJournal, "assert_unchanged", tamper)
    with pytest.raises(ReceiptIntegrityError):
        if reader == "historical":
            load_event_trade_tapes(root)
        else:
            _public_trade_events_from_files([root / "event-1.json"])


def test_ec_materialization_commit_failure_does_not_invalidate_old_tape(tmp_path, monkeypatch):
    from test_public_trade_collection import _FakeClient

    from poly_weather.receipt_journal import ReceiptJournal

    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                              output_dir=root, clock=lambda: at + timedelta(hours=1))
    path = root / "event-1.json"
    old = path.read_bytes()
    monkeypatch.setattr(ReceiptJournal, "commit_materialization",
                        lambda *args: (_ for _ in ()).throw(OSError("manifest crash")))
    with pytest.raises(OSError, match="manifest crash"):
        collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=2))],
                                  client=_FakeClient(_trade("token", at, "next")),
                                  output_dir=root, clock=lambda: at + timedelta(hours=2))
    assert path.read_bytes() == old
    assert len(load_event_trade_tapes(root)["event-1"]) == 1
