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
_LIVE_STATES = frozenset({"running", "connected", "healthy", "degraded", "stalled"})
_PID_REQUIRED_LIVE_STATES = frozenset({"running", "connected", "degraded", "stalled"})


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
            handle.write(payload)
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


def pid_is_alive(pid: int) -> bool:
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
            return ctypes.GetLastError() != 87
        except Exception:
            return True  # assume alive if we can't check
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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
) -> dict[str, Any]:
    """Read a daemon status safely and apply live PID/heartbeat semantics."""

    destination = Path(path)
    if not destination.exists():
        return {
            "state": "not_started",
            "status_integrity": "missing",
            "status_path": str(destination.resolve()),
            "status_last_good_path": str(last_good_path(destination).resolve()),
            "status_pid_alive": None,
        }
    try:
        value, integrity, source = read_json_with_fallback(destination)
    except StatusIntegrityError as exc:
        return {
            "state": "unreadable",
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
    pid_alive = pid_is_alive(pid) if pid is not None else None
    result["status_pid_alive"] = pid_alive

    heartbeat = next(
        (
            _parse_timestamp(result.get(key))
            for key in ("heartbeat", "updated_at", "checked_at", "last_evaluation_at")
            if _parse_timestamp(result.get(key)) is not None
        ),
        None,
    )
    age_seconds: float | None = None
    if heartbeat is not None:
        age_seconds = max(0.0, (datetime.now(UTC) - heartbeat).total_seconds())
    result["status_heartbeat_age_seconds"] = age_seconds

    original_state = str(result.get("state") or "")
    if pid_alive is False and original_state.casefold() in _LIVE_STATES:
        result["reported_state"] = original_state
        result["state"] = "stopped"
        result["status_state_reason"] = "pid_not_alive"
    elif pid_alive is None and original_state.casefold() in _PID_REQUIRED_LIVE_STATES:
        result["reported_state"] = original_state
        result["state"] = "stale"
        result["status_state_reason"] = "pid_missing"
    elif integrity != "verified" and original_state.casefold() in _LIVE_STATES:
        result["reported_state"] = original_state
        result["state"] = "stale"
        result["status_state_reason"] = f"status_integrity_{integrity}"
    elif (
        stale_after_seconds is not None
        and age_seconds is not None
        and age_seconds > stale_after_seconds
        and original_state.casefold() in _LIVE_STATES
    ):
        result["reported_state"] = original_state
        result["state"] = "stale"
        result["status_state_reason"] = "heartbeat_stale"
    return result


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
