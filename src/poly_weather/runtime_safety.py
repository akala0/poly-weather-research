"""Durable runtime state helpers for the read-only daemon chain.

The resident processes write small JSON state documents frequently.  A status
file is operational evidence, not a best-effort cache: readers must be able to
distinguish a valid heartbeat from a torn write, and a dead PID must never be
reported as running.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not ship resource
    resource = None  # type: ignore[assignment]

RUNTIME_STATUS_SCHEMA_VERSION = 1
_LIVE_STATES = frozenset({"running", "connected", "healthy", "degraded", "stalled",
                         "reconnecting", "upstream_maintenance"})
FUTURE_SKEW_TOLERANCE_SECONDS = 5.0
STATUS_SPECS = {
    "market": ("polymarket_ws_status.json", 120, "market-supervisor"),
    "supervisor": ("market_supervisor_status.json", 360, "market-supervisor"),
    "weather": ("weather_daemon_status.json", 300, "weather-stream"),
    "signal": ("signal_engine_status.json", 120, "signal-engine"),
    "shadow": ("shadow_spread_status_v2_token_scoped.json", 120, "shadow-spread-engine"),
}


class StatusIntegrityError(ValueError):
    """Raised when a runtime JSON document is empty, torn, or tampered with."""


def last_good_path(path: Path | str) -> Path:
    """Return the sidecar path used for the last verified document."""

    destination = Path(path)
    if destination.suffix:
        return destination.with_name(f"{destination.stem}.last_good{destination.suffix}")
    return destination.with_name(destination.name + ".last_good")


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _checksum_payload(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("status_checksum_sha256", None)
    return hashlib.sha256(_canonical_json(body)).hexdigest()


def _fsync_parent(path: Path) -> None:
    """Fsync a containing directory where the platform exposes that handle."""

    if os.name == "nt":
        # Windows replaces the directory entry atomically with os.replace.  A
        # directory fsync is not available through the normal Python API.
        return
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return
    try:
        descriptor = os.open(str(path.parent), os.O_RDONLY | directory_flag)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise OSError("short durable state write")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                _fsync_parent(path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_raw_json(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise StatusIntegrityError(f"read failed: {type(exc).__name__}: {exc}") from exc
    if not raw:
        raise StatusIntegrityError("empty file")
    if all(value == 0 for value in raw):
        raise StatusIntegrityError("all-NUL file")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatusIntegrityError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StatusIntegrityError("JSON root is not an object")
    recorded = value.get("status_checksum_sha256")
    if recorded is None:
        return value, "legacy_unchecked"
    try:
        if int(value.get("status_sequence")) < 1:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise StatusIntegrityError("missing or invalid status sequence") from exc
    if str(recorded) != _checksum_payload(value):
        raise StatusIntegrityError("checksum mismatch")
    return value, "verified"


def read_json_with_fallback(
    path: Path | str,
    *,
    fallback: bool = True,
) -> tuple[dict[str, Any], str, Path]:
    """Read a JSON object and optionally fall back to its last-good sidecar."""

    destination = Path(path)
    try:
        value, integrity = _read_raw_json(destination)
        return value, integrity, destination
    except StatusIntegrityError as current_error:
        if not fallback:
            raise
        backup = last_good_path(destination)
        try:
            value, _backup_integrity = _read_raw_json(backup)
        except StatusIntegrityError as backup_error:
            raise StatusIntegrityError(
                f"current={current_error}; last_good={backup_error}"
            ) from current_error
        return value, "degraded", backup


def _next_sequence(path: Path) -> int:
    candidates = (path, last_good_path(path))
    current = 0
    for candidate in candidates:
        try:
            value, _integrity = _read_raw_json(candidate)
        except StatusIntegrityError:
            continue
        try:
            current = max(current, int(value.get("status_sequence") or 0))
        except (TypeError, ValueError):
            continue
    return current + 1


def atomic_json_write(
    path: Path | str,
    payload: Mapping[str, Any],
    *,
    integrity_metadata: bool = True,
    keep_last_good: bool = True,
) -> dict[str, Any]:
    """Write a JSON document with fsync, atomic replace, sequence and checksum."""

    destination = Path(path)
    body = dict(payload)
    if integrity_metadata:
        body["runtime_status_schema_version"] = RUNTIME_STATUS_SCHEMA_VERSION
        body["status_sequence"] = _next_sequence(destination)
        body.pop("status_checksum_sha256", None)
        body["status_checksum_sha256"] = _checksum_payload(body)
    encoded = _canonical_json(body)
    # Validate the exact bytes before replacing the previous good document.
    decoded = json.loads(encoded.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise StatusIntegrityError("serialized JSON root is not an object")
    if integrity_metadata and decoded.get("status_checksum_sha256") != _checksum_payload(decoded):
        raise StatusIntegrityError("serialized checksum self-check failed")
    _replace_bytes(destination, encoded)
    if keep_last_good:
        _replace_bytes(last_good_path(destination), encoded)
    return body


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def pid_is_alive(pid: int) -> bool | None:
    """Return whether an OS process currently owns ``pid``."""

    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows, os.kill(pid, 0) always raises OSError winerror=87
        # (ERROR_INVALID_PARAMETER) regardless of whether the PID exists.
        # Use ctypes to call OpenProcess which gives a reliable answer.
        try:
            import ctypes.wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # Error 87 = invalid parameter (PID doesn't exist)
            return False if ctypes.windll.kernel32.GetLastError() == 87 else None
        except Exception:
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_ownership(pid: int, expected_command: str | None) -> tuple[str, datetime | None]:
    """Read only one PID; never return its command line (which could hold secrets)."""
    allowed = {spec[2] for spec in STATUS_SPECS.values()} | {"paper-spread-engine"}
    if expected_command not in allowed:
        return "unknown", None
    try:
        if os.name == "nt":
            # Reuse the existing Windows health script's CIM ownership mechanism.
            script = (
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
                "if ($null -eq $p) { exit 2 }; "
                f"$ok = [string]$p.CommandLine -match '(?:^|[\\s\"]){expected_command}(?:$|[\\s\"])'; "
                "@{matches=$ok; created=$p.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=3, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            payload = json.loads(result.stdout)
            return ("matched" if payload["matches"] is True else "ownership_mismatch",
                    _parse_timestamp(payload.get("created")))
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return ("matched" if expected_command.encode() in arguments else "ownership_mismatch", None)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return "unknown", None


def normalize_health(value: Mapping[str, Any], *, integrity: str, pid_state: str,
                     now: datetime, stale_after_seconds: float = 300,
                     dependency_state: str = "unknown") -> dict[str, Any]:
    """Single truth table. Integrity is not health; a single snapshot is not progress."""
    result = dict(value)
    reported = str(value.get("state") or "unknown")
    live = reported.casefold() in _LIVE_STATES
    key = next((key for key in ("heartbeat", "updated_at", "checked_at", "last_evaluation_at")
                if key in value), None)
    raw = value.get(key) if key else None
    heartbeat = _parse_timestamp(raw)
    # A timezone-less timestamp is not evidence of a known local clock.
    if heartbeat is not None:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            heartbeat = None
    age = (now - heartbeat).total_seconds() if heartbeat else None
    heartbeat_state = ("missing" if raw is None else "invalid") if age is None else (
        "future" if age < -FUTURE_SKEW_TOLERANCE_SECONDS else
        "stale" if age > stale_after_seconds else "fresh")
    reasons = []
    effective = reported
    if pid_state == "dead":
        effective = "stopped"
        reasons.append("pid_not_alive")
    elif pid_state == "missing" and live:
        effective = "stale"
        reasons.append("pid_missing")
    elif pid_state in {"reused", "ownership_mismatch"}:
        effective = "ownership_mismatch"
        reasons.append(f"pid_{pid_state}")
    if integrity != "verified":
        reasons.append(f"status_integrity_{integrity}")
    if heartbeat_state != "fresh":
        reasons.append(f"heartbeat_{heartbeat_state}")
    if pid_state == "unknown":
        reasons.append("pid_ownership_unknown")
    if dependency_state not in {"healthy", "not_required"}:
        reasons.append(f"dependency_{dependency_state}")
    # No observed pair of cursor snapshots is available here.
    progress = "stalled" if reported.casefold() == "stalled" else "unknown"
    if progress == "stalled":
        reasons.append("progress_stalled")
    if live and effective == reported:
        if integrity != "verified":
            effective = "stale"
        elif heartbeat_state != "fresh":
            effective = "clock_skew" if heartbeat_state == "future" else "stale"
        elif pid_state == "unknown":
            effective = "unknown"
        elif dependency_state not in {"healthy", "not_required"}:
            effective = "degraded"
    ready = (effective.casefold() in {"running", "connected", "healthy"}
             and pid_state == "alive" and not reasons)
    result.update(
        integrity_state=integrity, reported_state=reported, pid_state=pid_state,
        heartbeat_state=heartbeat_state, dependency_state=dependency_state,
        progress_state=progress, effective_state=effective, reasons=reasons,
        state=effective, health_ready=ready, status_integrity=integrity,
        status_pid_alive=True if pid_state == "alive" else False if pid_state in {
            "dead", "reused", "ownership_mismatch"} else None,
        status_heartbeat_age_seconds=age,
        status_state_reason=reasons[0] if reasons else None,
    )
    return result


def process_memory_status() -> dict[str, Any]:
    """Return best-effort resident-memory telemetry without a new dependency."""

    if os.name == "nt":
        try:
            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            )
            if ok:
                return {
                    "supported": True,
                    "rss_bytes": int(counters.WorkingSetSize),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                    "pagefile_bytes": int(counters.PagefileUsage),
                }
        except (AttributeError, OSError, TypeError):
            pass
        return {"supported": False, "reason": "GetProcessMemoryInfo unavailable"}
    if resource is None:
        return {"supported": False, "reason": "resource usage unavailable"}
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        value = int(usage.ru_maxrss)
        if os.name != "nt":
            value *= 1024
        return {"supported": True, "peak_rss_bytes": value}
    except (AttributeError, OSError):
        return {"supported": False, "reason": "resource usage unavailable"}


def read_status(
    path: Path | str,
    *,
    stale_after_seconds: float | None = None,
    now: datetime | None = None,
    dependency_state: str = "unknown",
    expected_command: str | None = None,
) -> dict[str, Any]:
    """Read a daemon status safely and apply live PID/heartbeat semantics."""

    destination = Path(path)
    if not destination.exists():
        return {
            **normalize_health({}, integrity="missing", pid_state="missing",
                               now=now or datetime.now(UTC)),
            "state": "not_started",
            "effective_state": "not_started",
            "status_integrity": "missing",
            "status_path": str(destination.resolve()),
            "status_last_good_path": str(last_good_path(destination).resolve()),
            "status_pid_alive": None,
        }
    try:
        value, integrity, source = read_json_with_fallback(destination)
    except StatusIntegrityError as exc:
        return {
            **normalize_health({}, integrity="unreadable", pid_state="unknown",
                               now=now or datetime.now(UTC)),
            "state": "unreadable",
            "effective_state": "unreadable",
            "status_integrity": "degraded",
            "status_integrity_error": str(exc),
            "status_path": str(destination.resolve()),
            "status_last_good_path": str(last_good_path(destination).resolve()),
            "status_pid_alive": None,
        }
    result = dict(value)
    result["status_integrity"] = integrity
    result["status_path"] = str(destination.resolve())
    result["status_source_path"] = str(source.resolve())
    result["status_last_good_path"] = str(last_good_path(destination).resolve())
    if source != destination:
        result["status_integrity_error"] = "current status was unreadable; using last-good"

    raw_pid = result.get("pid")
    pid: int | None
    try:
        pid = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        pid = None
    pid_alive = pid_is_alive(pid) if pid is not None and pid > 0 else None
    pid_state = "missing" if pid is None or pid <= 0 else (
        "dead" if pid_alive is False else "unknown")
    if pid_alive is True:
        command = expected_command or next((spec[2] for spec in STATUS_SPECS.values()
                                            if spec[0] == destination.name), None)
        ownership, created = _process_ownership(pid, command)
        pid_state = "alive" if ownership == "matched" else ownership
        heartbeat = _parse_timestamp(result.get("heartbeat") or result.get("updated_at"))
        if created and heartbeat and created > heartbeat:
            pid_state = "reused"
    return normalize_health(result, integrity=integrity, pid_state=pid_state,
                            now=now or datetime.now(UTC),
                            stale_after_seconds=300 if stale_after_seconds is None else stale_after_seconds,
                            dependency_state=dependency_state)


def read_chain_status(data_dir: Path | str, *, now: datetime | None = None,
                      previous: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Shared CLI/runner/signal/shadow dependency ordering; no state writes."""
    statuses: dict[str, Any] = {}
    from poly_weather.business_readiness import business_progress
    dependencies = {"market": (), "supervisor": (), "weather": (),
                    "signal": ("market", "supervisor", "weather"),
                    "shadow": ("market", "supervisor", "weather", "signal")}
    for name, (filename, maximum_age, command) in STATUS_SPECS.items():
        required = dependencies[name]
        dependency = "not_required" if not required else (
            "healthy" if all(statuses[key].get("health_ready") is True for key in required)
            else "unhealthy")
        statuses[name] = read_status(Path(data_dir) / "runtime" / filename,
                                     stale_after_seconds=maximum_age, now=now,
                                     dependency_state=dependency, expected_command=command)
        prior = (previous or {}).get(name, {})
        current = statuses[name]
        baseline = prior.get("progress_baseline") or prior.get("business_sample") or current.get("business_sample_previous")
        if (prior.get("business_sample") is not None
                and prior.get("business_sample") == current.get("business_sample")
                and isinstance(prior.get("business_progress"), Mapping)):
            progress = dict(prior["business_progress"])
        else:
            progress = business_progress(baseline, current.get("business_sample"),
                                         stalled_after_seconds=maximum_age)
        sample = current.get("business_sample")
        sampled_at = _parse_timestamp(sample.get("sampled_at")) if isinstance(sample, Mapping) else None
        sample_age = ((now or datetime.now(UTC)) - sampled_at).total_seconds() if sampled_at else None
        if (sample_age is None or sample_age < -5 or sample_age > maximum_age
                or current.get("integrity_state") != "verified"):
            progress = {"state": "unknown", "reason": "UNVERIFIED_OR_STALE_PROGRESS_SAMPLE", "ready": False}
        current["business_progress"] = progress
        # Do not reset the no-progress clock at every short poll. The baseline
        # is an actual earlier producer sample, never a fabricated timestamp.
        current["progress_baseline"] = (
            current.get("business_sample")
            if baseline is None or progress["state"] in {"advancing", "verified_idle", "reset", "rollback", "gap"}
            else baseline)
        current["business_ready"] = current.get("health_ready") is True and progress["ready"]
    return statuses


__all__ = [
    "RUNTIME_STATUS_SCHEMA_VERSION",
    "StatusIntegrityError",
    "atomic_json_write",
    "last_good_path",
    "pid_is_alive",
    "process_memory_status",
    "read_json_with_fallback",
    "read_status",
]
