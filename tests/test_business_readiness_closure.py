from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from runtime_health_support import seed_test_chain
from test_paper_recovery import BASE, fill_first, make_processor, snapshot, submit_first

from poly_weather.business_readiness import business_progress, paper_readiness
from poly_weather.runtime_safety import (
    atomic_json_write,
    read_chain_status,
    read_json_with_fallback,
)


@pytest.mark.parametrize("case,expected", [("advance", "advancing"), ("idle", "verified_idle"),
    ("stalled", "stalled"), ("heartbeat_only", "unknown"), ("first", "unknown"),
    ("reset", "reset"), ("rollback", "rollback"), ("gap", "gap"), ("clock", "unknown")])
def test_he_two_sample_business_progress(case, expected):
    first = {"sampled_at": BASE.isoformat(), "run_id": "r", "generation": "g",
             "committed_positions": {"archive": 1}, "continuity": "verified", "completed_checks": 1}
    last = {**first, "sampled_at": (BASE + timedelta(minutes=6)).isoformat(), "status_sequence": 999}
    if case == "advance":
        last["committed_positions"] = {"archive": 2}
    elif case == "idle":
        last.update(pending_work=False, connection_verified=True, completed_checks=2)
    elif case == "stalled":
        last["pending_work"] = True
    elif case == "first":
        first = None
    elif case == "reset":
        last["generation"] = "new"
    elif case == "rollback":
        last["committed_positions"] = {"archive": 0}
    elif case == "gap":
        last["continuity"] = "unknown"
    elif case == "clock":
        last["sampled_at"] = BASE.isoformat()
    result = business_progress(first, last, stalled_after_seconds=300)
    assert result["state"] == expected
    assert result["ready"] is (expected in {"advancing", "verified_idle"})


@pytest.mark.parametrize("missing", ["market", "supervisor", "weather", "progress", "active_set", "rules",
                                     "season", "quality", "archive_continuity", "account_ledger"])
def test_he_any_required_gate_unknown_blocks_readiness(missing):
    operational = dict.fromkeys(("market", "supervisor", "weather"), True)
    evidence = dict.fromkeys(("progress", "active_set", "weather", "rules", "season", "quality",
                              "archive_continuity", "account_ledger"), True)
    if missing in operational:
        operational[missing] = False
    else:
        evidence[missing] = False
    result = paper_readiness(as_of=BASE, scope="event/token/day", operational=operational,
                             evidence=evidence, group_completeness="unsupported")
    assert result["input_evidence_ready"] is False
    assert result["paper_score_eligible"] is False
    assert "UNSUPPORTED_GROUP_COMPLETENESS" in result["reasons"]


def test_he_input_ready_is_not_public_score_eligibility():
    operational = dict.fromkeys(("market", "supervisor", "weather"), True)
    evidence = dict.fromkeys(("progress", "active_set", "weather", "rules", "season", "quality",
                              "archive_continuity", "account_ledger"), True)
    ready = paper_readiness(as_of=BASE, scope="event/token/day", operational=operational,
                           evidence=evidence, group_completeness="unsupported")
    assert ready["operational_ready"] and ready["input_evidence_ready"]
    assert not ready["paper_score_eligible"]
    evidence["weather"] = False
    unknown = paper_readiness(as_of=BASE, scope="event/token/day", operational=operational,
                             evidence=evidence, group_completeness="unsupported")
    assert unknown["operational_ready"] and not unknown["input_evidence_ready"]


def test_he_rejected_entry_still_expires_and_releases(tmp_path):
    paper = make_processor(tmp_path)
    order = submit_first(paper)
    assert paper.account.buy_reserved_usd > 0
    paper.set_business_readiness(paper_readiness(as_of=BASE, scope="event/token/day",
                                               operational={}, evidence={}, group_completeness="unsupported"))
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=15))
    assert order.expired_at is not None
    assert paper.account.buy_reserved_usd == 0
    assert paper.process_snapshot(snapshot(at=BASE + timedelta(minutes=16))) is None
    assert paper.account.buy_reserved_usd == 0


@pytest.mark.parametrize("fresh", [False, True])
def test_he_rejected_entry_preserves_native_risk_exit_gate(tmp_path, fresh):
    paper = make_processor(tmp_path)
    fill_first(paper)  # MODEL_KERNEL preparation, never public ingress fill evidence.
    paper.set_business_readiness(paper_readiness(as_of=BASE, scope="event/token/day",
        operational={}, evidence={}, group_completeness="unsupported"))
    at = BASE + timedelta(hours=2, minutes=1)
    if fresh:
        paper.process_snapshot(snapshot(at=at, bid="0.80", ask="0.85"))
    paper.sweep_lifecycle(as_of=at)
    state = next(iter(paper.states.values()))
    assert state.stranded is (not fresh)
    assert paper.account.buy_reserved_usd == 0


def test_he_weather_raw_collection_has_no_market_dependency(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    seed_test_chain(tmp_path, monkeypatch, now=now, business_samples=False)
    (tmp_path / "runtime" / "polymarket_ws_status.json").write_bytes(b"broken")
    chain = read_chain_status(tmp_path, now=now)
    assert chain["market"]["health_ready"] is False
    assert chain["weather"]["health_ready"] is True
    assert chain["weather"]["business_ready"] is False
    assert chain["weather"]["business_progress"]["state"] == "unknown"
    runner = (Path(__file__).resolve().parents[1] / "scripts/windows/poly-weather-daemon-runner.ps1").read_text()
    branch = runner.split("function Test-Dependencies {", 1)[1].split("$status = Get-ChainStatus", 1)[0]
    assert '"weather-stream"' in branch and "return $true" in branch


def test_he_supervisor_persistence_failure_keeps_mutation_boundary(tmp_path, monkeypatch):
    paper = make_processor(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("supervisor evidence write failure")

    monkeypatch.setattr(paper, "_record_decision", fail)
    assert paper.set_supervisor_evidence(generation=None, active_event_ids=None,
                                         integrity="unknown", as_of=BASE) == ()
    assert paper.is_halted is True
    before = paper.business_readiness
    paper.set_business_readiness({"input_evidence_ready": True})
    assert paper.business_readiness == before


def test_he_short_polls_accumulate_pending_work_window(tmp_path, monkeypatch):
    seed_test_chain(tmp_path, monkeypatch, now=BASE)
    path = tmp_path / "runtime" / "weather_daemon_status.json"
    previous = None
    for seconds in (0, 60, 120, 180, 240, 300):
        payload = read_json_with_fallback(path)[0]
        at = BASE + timedelta(seconds=seconds)
        payload.update(heartbeat=at.isoformat(), business_sample={
            "sampled_at": at.isoformat(), "run_id": "r", "generation": "g",
            "committed_positions": {"archive": 1}, "continuity": "verified", "pending_work": True})
        atomic_json_write(path, payload)
        previous = read_chain_status(tmp_path, now=at, previous=previous)
    assert previous["weather"]["business_progress"]["state"] == "stalled"
    assert previous["weather"]["business_ready"] is False
    assert previous["weather"]["progress_baseline"]["sampled_at"] == BASE.isoformat()
