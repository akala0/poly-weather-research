import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from poly_weather.runtime_safety import (
    StatusIntegrityError,
    atomic_json_write,
    last_good_path,
    read_json_with_fallback,
    read_status,
)


def test_atomic_status_write_has_checksum_sequence_and_last_good(tmp_path) -> None:
    path = tmp_path / "runtime" / "status.json"
    first = atomic_json_write(path, {"state": "running", "pid": os.getpid()})
    second = atomic_json_write(path, {"state": "stopped", "pid": os.getpid()})

    assert first["status_sequence"] == 1
    assert second["status_sequence"] == 2
    assert path.exists()
    assert last_good_path(path).exists()
    loaded, integrity, source = read_json_with_fallback(path)
    assert loaded["state"] == "stopped"
    assert integrity == "verified"
    assert source == path


@pytest.mark.parametrize("raw", [b"\x00" * 40, b'{"state":"running"', b"not-json"])
def test_status_reader_falls_back_from_nul_or_torn_current(tmp_path, raw) -> None:
    path = tmp_path / "status.json"
    atomic_json_write(path, {"state": "running", "pid": os.getpid()})
    path.write_bytes(raw)

    payload = read_status(path, stale_after_seconds=300)

    assert payload["status_integrity"] == "degraded"
    assert payload["status_source_path"] == str(last_good_path(path).resolve())
    assert payload["state"] == "stale"
    assert payload["status_state_reason"] == "status_integrity_degraded"


def test_status_reader_does_not_trust_dead_pid_or_missing_pid(tmp_path) -> None:
    dead_path = tmp_path / "dead.json"
    atomic_json_write(dead_path, {"state": "running", "pid": 2_147_483_647})
    dead = read_status(dead_path)
    assert dead["state"] == "stopped"
    assert dead["status_state_reason"] == "pid_not_alive"

    missing_path = tmp_path / "missing-pid.json"
    atomic_json_write(missing_path, {"state": "running"})
    missing = read_status(missing_path)
    assert missing["state"] == "stale"
    assert missing["status_state_reason"] == "pid_missing"


def test_status_reader_marks_stale_heartbeat(tmp_path) -> None:
    path = tmp_path / "stale.json"
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    atomic_json_write(path, {"state": "running", "pid": os.getpid(), "heartbeat": old})

    payload = read_status(path, stale_after_seconds=30)

    assert payload["state"] == "stale"
    assert payload["status_state_reason"] == "heartbeat_stale"


def test_checksum_tamper_is_rejected_without_last_good(tmp_path) -> None:
    path = tmp_path / "tampered.json"
    atomic_json_write(path, {"state": "running", "pid": os.getpid()})
    last_good_path(path).unlink()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "healthy"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StatusIntegrityError):
        read_json_with_fallback(path)
