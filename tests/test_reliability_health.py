"""P3 truth table with fake PID/ownership providers; never probes daemon processes."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poly_weather import runtime_safety as safety

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.mark.parametrize("reported", ["running", "connected", "healthy", "reconnecting", "stopped"])
@pytest.mark.parametrize("pid_state,expected", [("dead", "stopped"), ("reused", "ownership_mismatch"),
                                               ("ownership_mismatch", "ownership_mismatch")])
def test_health_pid_failure_overrides_every_reported_state(reported, pid_state, expected):
    result = safety.normalize_health({"state": reported, "heartbeat": NOW.isoformat()},
                                     integrity="verified", pid_state=pid_state, now=NOW,
                                     dependency_state="not_required")
    assert result["effective_state"] == expected
    assert result["reported_state"] == reported
    assert result["integrity_state"] == "verified"
    assert not result["health_ready"]


@pytest.mark.parametrize("heartbeat,state", [(None, "missing"), ("bad", "invalid"),
    ("2026-09-08T12:00:00", "invalid"), (NOW + timedelta(seconds=5), "fresh"),
    (NOW + timedelta(seconds=5, microseconds=1), "future"),
    (NOW - timedelta(seconds=300), "fresh"),
    (NOW - timedelta(seconds=300, microseconds=1), "stale")])
def test_health_heartbeat_fixed_boundaries_and_no_fallback(heartbeat, state):
    value = heartbeat.isoformat() if isinstance(heartbeat, datetime) else heartbeat
    result = safety.normalize_health({"state": "healthy", "heartbeat": value,
                                      "updated_at": NOW.isoformat()}, integrity="verified",
                                     pid_state="alive", now=NOW, dependency_state="not_required")
    assert result["heartbeat_state"] == state
    assert result["health_ready"] is (state == "fresh")
    if state == "future":
        assert result["status_heartbeat_age_seconds"] < -5


@pytest.mark.parametrize("pid_state", ["missing", "unknown"])
def test_health_missing_or_unknown_pid_never_ready(pid_state):
    result = safety.normalize_health({"state": "running", "heartbeat": NOW.isoformat()},
                                     integrity="verified", pid_state=pid_state, now=NOW,
                                     dependency_state="not_required")
    assert result["health_ready"] is False


def test_health_read_status_cli_and_downstream_share_truth(tmp_path, monkeypatch):
    from poly_weather import cli

    monkeypatch.setattr(safety, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(safety, "_process_ownership", lambda pid, command: ("matched", NOW - timedelta(hours=1)))
    monkeypatch.setattr(cli, "_emit", lambda payload: captured.append(payload))
    for filename, _, _ in safety.STATUS_SPECS.values():
        safety.atomic_json_write(tmp_path / "runtime" / filename,
                                 {"state": "running", "heartbeat": datetime.now(UTC).isoformat(),
                                  "pid": os.getpid(), "execution_enabled": False})
    captured = []
    cli.stream_status(tmp_path)
    assert all(row["health_ready"] for row in captured[0].values())
    market = tmp_path / "runtime" / safety.STATUS_SPECS["market"][0]
    safety.atomic_json_write(market, {"state": "connected", "pid": os.getpid()})
    captured.clear()
    cli.stream_status(tmp_path)
    assert captured[0]["market"]["heartbeat_state"] == "missing"
    assert captured[0]["signal"]["dependency_state"] == "unhealthy"
    assert captured[0]["shadow"]["health_ready"] is False


def test_health_process_creation_after_heartbeat_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(safety, "_process_ownership", lambda pid, command: ("matched", NOW + timedelta(seconds=1)))
    path = tmp_path / "status.json"
    safety.atomic_json_write(path, {"state": "reconnecting", "pid": os.getpid(), "heartbeat": NOW.isoformat()})
    result = safety.read_status(path, now=NOW, dependency_state="not_required")
    assert result["pid_state"] == "reused"
    assert result["status_pid_alive"] is False


@pytest.mark.parametrize("kind", ["missing", "corrupt", "legacy", "fallback"])
def test_health_integrity_is_distinct_from_liveness(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(safety, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(safety, "_process_ownership", lambda pid, command: ("matched", None))
    path = tmp_path / "status.json"
    if kind == "legacy":
        path.write_text(json.dumps({"state": "running", "pid": os.getpid(), "heartbeat": NOW.isoformat()}))
    elif kind == "fallback":
        safety.atomic_json_write(path, {"state": "running", "pid": os.getpid(), "heartbeat": NOW.isoformat()})
        path.write_bytes(b"{")
    elif kind == "corrupt":
        path.write_bytes(b"{")
    result = safety.read_status(path, now=NOW, dependency_state="not_required")
    assert result["health_ready"] is False
    assert set(("integrity_state", "reported_state", "pid_state", "heartbeat_state",
                "dependency_state", "progress_state", "effective_state", "reasons")) <= result.keys()


def test_health_runner_and_consumers_use_normalized_gate_static():
    root = Path(__file__).resolve().parents[1]
    runner = (root / "scripts/windows/poly-weather-daemon-runner.ps1").read_text()
    function = runner.split("function Test-VerifiedStatus", 1)[1].split("function Test-MarketReady", 1)[0]
    assert '"health_ready"' in function and "legacy_unchecked" not in function
    for name in ("signal_engine.py", "shadow_runtime.py", "paper_spread_runtime.py"):
        source = (root / "src/poly_weather" / name).read_text(encoding="utf-8")
        assert 'get("health_ready") is not True' in source or 'get("health_ready") is True' in source


def test_health_short_durable_write_preserves_previous_object(tmp_path, monkeypatch):
    from contextlib import contextmanager

    path = tmp_path / "status.json"
    safety.atomic_json_write(path, {"state": "stopped"})
    before = path.read_bytes()
    original = Path.open

    @contextmanager
    def short_open(target, mode="r", *args, **kwargs):
        with original(target, mode, *args, **kwargs) as handle:
            if mode == "xb":
                class ShortWriter:
                    def write(self, payload):
                        return handle.write(payload[:3])

                yield ShortWriter()
            else:
                yield handle

    monkeypatch.setattr(Path, "open", short_open)
    with pytest.raises(OSError, match="short durable state write"):
        safety.atomic_json_write(path, {"state": "running"})
    assert path.read_bytes() == before
