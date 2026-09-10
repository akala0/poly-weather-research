"""Receipt journal crash boundaries through the real collector; temporary files only."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_public_trade_collection import _FakeClient, _trade
from test_reliability_storage import coverage_fixture

from poly_weather import public_trade_collection as collection
from poly_weather import receipt_journal as receipt


@pytest.mark.parametrize("boundary", ["before_fact", "before_witness", "before_tape",
                                      "before_cursor", "before_audit"])
def test_receipt_crash_reconciliation(tmp_path, monkeypatch, boundary):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    original = receipt.atomic_json_write
    original_tape = collection._atomic_json_write

    def journal_write(path, body, **kwargs):
        if ((boundary == "before_fact" and path.name.endswith(".fact.json"))
                or (boundary == "before_witness" and path.name.endswith(".witness.json"))):
            raise OSError("injected process boundary")
        return original(path, body, **kwargs)

    def tape_write(path, body):
        targets = {"before_tape": "event-1.json", "before_cursor": ".depth_trade_cursor.json",
                   "before_audit": "depth_trade_coverage.json"}
        if path.name == targets.get(boundary):
            raise OSError("injected process boundary")
        return original_tape(path, body)

    monkeypatch.setattr(receipt, "atomic_json_write", journal_write)
    monkeypatch.setattr(collection, "_atomic_json_write", tape_write)
    with pytest.raises(OSError, match="injected"):
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                                             output_dir=root, clock=lambda: at + timedelta(hours=1))
    monkeypatch.setattr(receipt, "atomic_json_write", original)
    monkeypatch.setattr(collection, "_atomic_json_write", original_tape)
    client = _FakeClient(_trade("token", at, "tx"))
    for _ in range(2):
        collection.collect_depth_event_trades(coverage, client=client, output_dir=root,
                                             clock=lambda: at + timedelta(hours=2))
        rows = json.loads((root / "event-1.json").read_text())["trades"]
        assert len(rows) == 1
        expected_response = at + timedelta(hours=2 if boundary == "before_fact" else 1)
        expected_commit = at + timedelta(hours=2 if boundary in {"before_fact", "before_witness"} else 1)
        assert rows[0]["first_seen_at"] == expected_response.isoformat()
        assert rows[0]["receipt_committed_at"] == expected_commit.isoformat()
    assert len(client.calls) == (1 if boundary == "before_fact" else 0)
    assert json.loads((root / "depth_trade_coverage.json").read_text())["trade_count"] == 1


@pytest.mark.parametrize("damage", ["tail", "interior", "checksum", "rollback", "witness"])
def test_receipt_corruption_preserves_bytes_and_blocks(tmp_path, damage):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    for n in range(2):
        collection.collect_depth_event_trades(
            [replace(coverage[0], end_at=at + timedelta(minutes=n))],
            client=_FakeClient(_trade("token", at, "tx")), output_dir=root,
            clock=lambda: at + timedelta(hours=1))
    facts = sorted((root / ".receipt_journal").glob("*.fact.json"))
    if damage == "rollback":
        facts[-1].unlink()  # test-owned fixture only
    else:
        target = facts[0] if damage == "interior" else facts[-1]
        if damage == "witness":
            target = target.with_name(target.name.replace("fact", "witness"))
        target.write_bytes(b'{"torn":' if damage != "checksum" else
                           target.read_bytes().replace(b'"member_count": 1', b'"member_count": 9'))
    tape_bytes = (root / "event-1.json").read_bytes()
    before = {p.name: p.read_bytes() for p in (root / ".receipt_journal").glob("*.json")}
    with pytest.raises(receipt.ReceiptIntegrityError):
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")), output_dir=root)
    assert (root / "event-1.json").read_bytes() == tape_bytes
    assert {p.name: p.read_bytes() for p in (root / ".receipt_journal").glob("*.json")} == before


def test_receipt_tape_conflict_is_quarantined_without_overwrite(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                                         output_dir=root, clock=lambda: at + timedelta(hours=1))
    path = root / "event-1.json"
    payload = json.loads(path.read_text())
    payload["trades"][0]["first_seen_at"] = at.isoformat()
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(receipt.ReceiptIntegrityError, match="conflict"):
        collection.collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")), output_dir=root)
    assert path.read_bytes() == before
    assert list((root / ".receipt_journal").glob("discrepancy-*.json"))


def test_receipt_decimal_and_local_sequence_do_not_create_economic_identity(tmp_path):
    at, _ = coverage_fixture(tmp_path)
    first = _trade("token", at, "tx").as_json()
    second = {**first, "price": str(Decimal(first["price"])) + "0", "sequence": 999}
    assert collection._member_identity(first) == collection._member_identity(second)
    first["size"] = "123456789012345678901234567890.0000000000000000001"
    second = {**first, "size": "123456789012345678901234567890.0000000000000000002"}
    assert collection._member_identity(first) != collection._member_identity(second)


def test_receipt_failed_request_is_error_not_successful_empty_page(tmp_path):
    at, coverage = coverage_fixture(tmp_path)

    class Client:
        def market_trades(self, **kwargs):
            raise OSError("page failed; secret-like URL must not be persisted")

    root = tmp_path / "tapes"
    collection.collect_depth_event_trades(coverage, client=Client(), output_dir=root,
                                         clock=lambda: at + timedelta(hours=1))
    fact = json.loads(next((root / ".receipt_journal").glob("*.fact.json")).read_text())
    assert fact["collection_status"] == "collection_error"
    assert fact["response_complete"] is False
    assert fact["response_received_at"] is None
    assert fact["failure_observed_at"] == (at + timedelta(hours=1)).isoformat()
    assert fact["member_count"] == 0
    assert fact["page_coverage"].startswith("UNKNOWN")
    assert "secret-like" not in json.dumps(fact)
    assert "watermark_end" not in json.loads((root / ".depth_trade_cursor.json").read_text())["events"]["event-1"]
