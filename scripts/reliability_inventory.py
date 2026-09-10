"""Generate the sole current test inventory from real pytest collection.

Print JSON only; the caller reviews/persists it. No data/runtime operations.
Evidence levels are conservative labels, never execution results.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = [
    "tests/test_fees.py", "tests/test_market_supervisor.py", "tests/test_wrh_backfill.py",
    "tests/test_stream_daemons.py", "tests/test_polymarket_status.py",
]


def main() -> None:
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    nodes = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::test_" in line]
    if not nodes:
        raise RuntimeError("pytest returned no test nodes")
    sources = {}
    records = []
    for node in nodes:
        path, name = node.split("::", 1)
        name = name.split("[", 1)[0]
        if path not in sources:
            raw = (ROOT / path).read_bytes()
            source = raw.decode("utf-8")
            functions = {item.name: ast.get_source_segment(source, item) or ""
                         for item in ast.walk(ast.parse(source)) if isinstance(item, ast.FunctionDef)}
            sources[path] = (hashlib.sha256(raw).hexdigest(), functions)
        digest, functions = sources[path]
        body = functions.get(name, "")
        level, basis = "UNVERIFIED", "not individually qualified; collection is not PASS"
        if path in {"tests/test_evidence_closure.py", "tests/test_closure_crash_matrix.py",
                    "tests/test_archive_position_closure.py", "tests/test_signal_archive_closure.py",
                    "tests/test_weather_evidence_closure.py", "tests/test_weather_decision_prefix.py",
                    "tests/test_producer_progress_closure.py"}:
            level, basis = "PRODUCTION_INGRESS", "isolated actual reader/converter/publication contract; not deployed or public fills"
            if name == "test_ec_reader_cost_measurement":
                level, basis = "UNVERIFIED", "diagnostic read-cost measurement; not production performance qualification"
        elif path == "tests/test_follower_archive_closure.py":
            level, basis = "PRODUCTION_INGRESS", "actual temporary follower equivalence with MODEL_KERNEL prepared ledger; zero public queue consumption"
        elif path == "tests/test_business_readiness_closure.py":
            level, basis = "CONTAINMENT_ONLY", "readiness rejection and normalized status evidence; no resident certification"
            if name in {"test_he_rejected_entry_still_expires_and_releases",
                        "test_he_rejected_entry_preserves_native_risk_exit_gate"}:
                level, basis = "MODEL_KERNEL", "prepared inventory through actual lifecycle/snapshot gate; not public fills"
        elif "model_trade(" in body or "_process_ordered_model_trades(" in body:
            level, basis = "MODEL_KERNEL", "private economic kernel is invoked by this test"
        elif name.startswith(("test_f01_", "test_f02_")):
            level, basis = "CONTAINMENT_ONLY", "rejection/preservation, not positive fill capability"
        elif name.startswith("test_f03_"):
            level, basis = "PRODUCTION_INGRESS", "collector/receipt conversion assertion only; no group closure"
        elif name.startswith("test_receipt_"):
            level, basis = "PRODUCTION_INGRESS", "temporary collector journal/reconciliation, not group closure"
        elif name.startswith(("test_health_", "test_q01_", "test_q03_", "test_unsupported_", "test_no_production_")):
            level, basis = "CONTAINMENT_ONLY", "scoped health/evidence rejection; no production fills"
        elif name.startswith("test_q05_"):
            level, basis = "MODEL_KERNEL", "fixed-version Decimal fee math, no live fee claim"
        elif name in {
            "test_replayed_evidence_prefix_is_idempotent_after_cycle_failure",
            "test_evidence_append_oserror_halts_without_cursor_advance",
            "test_restart_replays_downtime_public_trade_once",
            "test_later_public_match_resolves_match_but_not_group_completeness",
            "test_real_follower_two_polls_then_restart",
        }:
            level, basis = "CONTAINMENT_ONLY", "native follower asserts zero economic consumption"
        records.append({"nodeid": node, "evidence_level": level, "basis": basis})
    payload = {
        "schema_version": 1, "as_of": datetime.now(UTC).isoformat(),
        "authority": "generated collection and evidence-layer index; no PASS inference",
        "formal_authority": "CURRENT_CONCLUSIONS.md#paper-v1-formal-status",
        "command": command, "exit_code": result.returncode,
        "collected_count": len(nodes), "diagnostic_excluded_files": EXCLUDED,
        "diagnostic_selected_count": sum(node.split("::", 1)[0] not in EXCLUDED for node in nodes),
        "source_sha256": {path: digest for path, (digest, _) in sources.items()},
        "collection_summary": result.stdout.splitlines()[-1], "tests": records,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
