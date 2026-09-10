"""Controlled closure of the local outage quality window."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poly_weather.polymarket_status import (
    load_quality_windows,
    persist_quality_windows,
)
from poly_weather.runtime_safety import atomic_json_write, read_chain_status

LOCAL_DAEMON_OUTAGE_ID = "local-daemon-outage-2026-08-29"


def _parse_recovery_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None
        else parsed.astimezone(UTC)
    )


def _status_gate(data_dir: Path) -> dict[str, Any]:
    chain = read_chain_status(data_dir)
    market, supervisor, weather, signal = (chain[key] for key in ("market", "supervisor", "weather", "signal"))
    checks = {
        "normalized_chain_health": all(row.get("health_ready") is True
                                       for row in (market, supervisor, weather, signal)),
        "market_pid_alive": market.get("status_pid_alive") is True,
        "market_has_complete_book": int(market.get("book_snapshot_count") or 0) > 0,
        "supervisor_pid_alive": supervisor.get("status_pid_alive") is True,
        "weather_pid_alive": weather.get("status_pid_alive") is True,
        "weather_running": weather.get("state") == "running",
        "signal_pid_alive": signal.get("status_pid_alive") is True,
        "signal_running": signal.get("state") == "running",
        "execution_disabled": all(
            payload.get("execution_enabled") is False
            for payload in (market, supervisor, weather, signal)
            if "execution_enabled" in payload
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "market": market,
        "supervisor": supervisor,
        "weather": weather,
        "signal": signal,
    }


def close_local_daemon_outage(
    data_dir: Path,
    *,
    recovered_at: str | None = None,
) -> dict[str, Any]:
    """Close the outage only after the live read-only chain passes its gate."""

    root = data_dir.resolve()
    recovery_at = _parse_recovery_time(recovered_at)
    runtime_path = root / "runtime" / "polymarket_quality_windows.json"
    windows = list(load_quality_windows(runtime_path))
    index = next(
        (position for position, window in enumerate(windows) if window.incident_id == LOCAL_DAEMON_OUTAGE_ID),
        None,
    )
    if index is None:
        raise RuntimeError(f"quality window {LOCAL_DAEMON_OUTAGE_ID!r} is not present")
    window = windows[index]
    if window.end_at is not None:
        raise RuntimeError(
            f"quality window is already closed at {window.end_at.isoformat()}"
        )
    if recovery_at <= window.start_at:
        raise ValueError("recovery time must be after the outage start")
    gate = _status_gate(root)
    if not gate["passed"]:
        failed = [name for name, passed in gate["checks"].items() if not passed]
        raise RuntimeError(f"recovery health gate failed: {', '.join(failed)}")
    closed = replace(
        window,
        end_at=recovery_at,
        status="completed",
        latest_update_at=recovery_at,
        latest_update_state="Verified local chain recovery",
        latest_update_message=(
            "Complete market book, live supervisor, weather commits, and signal "
            "engine were present at the operator-supplied recovery boundary. "
            "The outage interval remains excluded; only later records are eligible."
        ),
    )
    windows[index] = closed
    persist_quality_windows(runtime_path, tuple(windows))
    report = {
        "schema_version": 1,
        "incident_id": LOCAL_DAEMON_OUTAGE_ID,
        "recovered_at": recovery_at.isoformat(),
        "quality_window_start": window.start_at.isoformat(),
        "quality_window_end": recovery_at.isoformat(),
        "default_excluded_before_end": True,
        "status_gate": gate,
        "runtime_quality_windows_path": str(runtime_path),
        "execution_enabled": False,
    }
    atomic_json_write(root / "runtime" / "daemon_recovery_state.json", report)
    return report


__all__ = ["LOCAL_DAEMON_OUTAGE_ID", "close_local_daemon_outage"]
