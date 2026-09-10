"""P0: current authority cannot silently inherit historical model PASS."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs/reliability_test_inventory.json"
LEVELS = {"PRODUCTION_INGRESS", "MODEL_KERNEL", "CONTAINMENT_ONLY", "UNVERIFIED", "UNSUPPORTED"}


def test_d01_current_matrix_references_collected_tests():
    inventory = json.loads(INDEX.read_text(encoding="utf-8"))
    collected = {row["nodeid"].split("::", 1)[1].split("[", 1)[0] for row in inventory["tests"]}
    matrix = (ROOT / "docs/paper_v1_test_matrix.md").read_text(encoding="utf-8").split("<!-- SUPERSEDED_HISTORY -->")[0]
    referenced = set(re.findall(r"\btest_[A-Za-z0-9_]+", matrix))
    assert referenced
    assert referenced <= collected
    assert "test_replayed_successful_prefix_is_idempotent_after_cycle_failure" not in referenced
    assert "test_account_commit_oserror_halts_without_cursor_advance" not in referenced


def test_d02_inventory_layers_and_source_fingerprints():
    inventory = json.loads(INDEX.read_text(encoding="utf-8"))
    assert all(row["evidence_level"] in LEVELS for row in inventory["tests"])
    assert set(inventory["source_sha256"]) == {
        path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").glob("test_*.py")
    }
    for name, digest in inventory["source_sha256"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    for row in inventory["tests"]:
        if row["evidence_level"] == "MODEL_KERNEL":
            assert "production follower filled" not in row["basis"].lower()
            assert "生产 follower 已成交" not in row["basis"]


def test_d03_formal_authority_and_zero_consumption_are_explicit():
    for name in ["paper_v1_test_matrix.md", "paper_v1_seal_validation_status.md", "reliability_remediation_status.md"]:
        current = (ROOT / "docs" / name).read_text(encoding="utf-8").split("<!-- SUPERSEDED_HISTORY -->")[0]
        assert "CURRENT_CONCLUSIONS.md#paper-v1-formal-status" in current
        assert "accepted_queue_trade_rows=0" in current
        assert "reliability_test_inventory.json" in current
    authority = (ROOT / "CURRENT_CONCLUSIONS.md").read_text(encoding="utf-8")
    assert 'id="paper-v1-formal-status"' in authority
    assert "formal Paper N=0; PnL=N/A; DO NOT START PAPER; NOT SEALED" in authority


def test_d04_counts_are_derived_from_collection_not_hardcoded():
    inventory = json.loads(INDEX.read_text(encoding="utf-8"))
    nodes = [row["nodeid"] for row in inventory["tests"]]
    assert len(nodes) == len(set(nodes)) == inventory["collected_count"]
    assert inventory["exit_code"] == 0
    assert inventory["diagnostic_selected_count"] == sum(
        node.split("::", 1)[0] not in inventory["diagnostic_excluded_files"] for node in nodes
    )
    assert int(re.search(r"(\d+) tests collected", inventory["collection_summary"])[1]) == len(nodes)
