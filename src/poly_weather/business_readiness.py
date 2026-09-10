"""Explicit business evidence; operational heartbeat never proves progress."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from poly_weather.trade_evidence import parse_trade_timestamp


class ArchiveCommitProgress:
    """Writer-owned fsync frontier; never inferred from status-write sequence."""

    def __init__(self) -> None:
        self.positions: dict[str, int] = {}

    def commit(self, handles) -> None:
        staged = dict(self.positions)
        for handle in set(handles):
            handle.flush()
            os.fsync(handle.fileno())
            staged[str(handle.name)] = int(handle.tell())
        self.positions = staged  # Only after all touched representations succeeded.


def producer_progress_sample(*, run_id: str, generation: str, positions: Mapping[str, int],
                             as_of: datetime, pending_work: bool, completed_checks: int,
                             connection_verified: bool) -> dict[str, Any]:
    return {"schema_version": 1, "producer_contract": "local_fsync_frontier_v1",
            "sampled_at": as_of.isoformat(), "run_id": run_id, "generation": generation,
            "committed_positions": dict(positions), "continuity": "verified",
            "pending_work": pending_work, "completed_checks": completed_checks,
            "connection_verified": connection_verified}


def business_progress(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None,
                      *, stalled_after_seconds: float) -> dict[str, Any]:
    """Compare producer-supplied committed positions, not status-write counters.

    Samples are normalized evidence, not newly assumed wire fields. Absence stays
    unknown; callers must not fabricate samples from their own observation times.
    """
    def result(state, reason):
        return {"state": state, "reason": reason, "ready": state in {"advancing", "verified_idle"}}

    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return result("unknown", "MISSING_PROGRESS_SAMPLE")
    for sample in (previous, current):
        if any(sample.get(key) is None for key in ("sampled_at", "run_id", "generation", "committed_positions")):
            return result("unknown", "INCOMPLETE_PROGRESS_SAMPLE")
        if not sample["run_id"] or not sample["generation"]:
            return result("unknown", "MISSING_RUN_IDENTITY")
        if not isinstance(sample["committed_positions"], Mapping) or not sample["committed_positions"]:
            return result("unknown", "MISSING_COMMITTED_POSITIONS")
        if any(type(value) is not int or value < 0 for value in sample["committed_positions"].values()):
            return result("unknown", "INVALID_COMMITTED_POSITION")
    if any(previous[key] != current[key] for key in ("run_id", "generation")):
        return result("reset", "RUN_OR_GENERATION_CHANGED")
    before = parse_trade_timestamp(previous["sampled_at"])
    after = parse_trade_timestamp(current["sampled_at"])
    if before is None or after is None or after <= before:
        return result("unknown", "INVALID_PROGRESS_SAMPLE_CLOCK")
    first, last = previous["committed_positions"], current["committed_positions"]
    if first.keys() != last.keys() or any(row.get("continuity") != "verified" for row in (previous, current)):
        return result("gap", "UNVERIFIED_PROGRESS_CONTINUITY")
    if any(last[key] < first[key] for key in first):
        return result("rollback", "COMMITTED_POSITION_REGRESSED")
    if any(last[key] > first[key] for key in first):
        return result("advancing", "COMMITTED_POSITION_ADVANCED")
    if (current.get("pending_work") is False and current.get("connection_verified") is True
            and type(current.get("completed_checks")) is int and type(previous.get("completed_checks")) is int
            and current["completed_checks"] > previous["completed_checks"]):
        return result("verified_idle", "VERIFIED_NO_WORK_WITH_COMPLETED_CHECK")
    if current.get("pending_work") is True and (after - before).total_seconds() >= stalled_after_seconds:
        return result("stalled", "PENDING_WORK_WITHOUT_COMMIT")
    return result("unknown", "NO_PROVEN_BUSINESS_PROGRESS")


def paper_readiness(*, as_of: datetime, scope: str, operational: Mapping[str, bool],
                    evidence: Mapping[str, bool], group_completeness: str) -> dict[str, Any]:
    required_operational = {"market", "supervisor", "weather"}
    required_evidence = {"progress", "active_set", "weather", "rules", "season", "quality",
                         "archive_continuity", "account_ledger"}
    operational_reasons = [f"OPERATIONAL_{key.upper()}" for key in sorted(required_operational)
                           if operational.get(key) is not True]
    evidence_reasons = [f"EVIDENCE_{key.upper()}" for key in sorted(required_evidence)
                        if evidence.get(key) is not True]
    ready = not operational_reasons and not evidence_reasons
    return {"as_of": as_of.isoformat(), "scope": scope, "operational_ready": not operational_reasons,
            "input_evidence_ready": ready,
            "paper_score_eligible": ready and group_completeness == "verified",
            "reasons": operational_reasons + evidence_reasons +
                       ([] if group_completeness == "verified" else ["UNSUPPORTED_GROUP_COMPLETENESS"]),
            "execution_enabled": False}
