"""Append-only, forward-only QUIET state evidence.

This module is deliberately a passive sink.  It does not subscribe to a
network feed, submit an order, or alter a strategy state machine.  A caller
which already derives QUIET transitions from the local public archives can
pass those observations here.  The first run records only a tail bootstrap;
later runs append records strictly after that local watermark.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.market_regime import MarketRegimeStateMachine, RegimeTransition, ThresholdTier
from poly_weather.shadow_orders import BookSnapshot

QUIET_STATE_LOG_VERSION = "quiet-window-forward-state-log-v1"
DEFAULT_QUIET_STATE_LOG_PATH = Path("data/raw/quiet_window_state/quiet_state_v1.jsonl")
DEFAULT_QUIET_STATE_CURSOR_PATH = Path("data/runtime/quiet_state_v1_cursor.json")


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _record_id(record: Mapping[str, Any]) -> str:
    canonical = json.dumps(_json_value(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quiet_transition_record(
    tier: ThresholdTier,
    snapshot: BookSnapshot,
    _machine: MarketRegimeStateMachine,
    transition: RegimeTransition,
) -> dict[str, Any]:
    """Create one receipt-time state record without causing an action."""
    metrics = transition.metrics
    previous = transition.previous_state.value
    current = transition.state.value
    if previous != "QUIET" and current == "QUIET":
        quiet_lifecycle = "quiet_start"
    elif previous == "QUIET" and current != "QUIET":
        quiet_lifecycle = "quiet_end"
    else:
        quiet_lifecycle = None
    record: dict[str, Any] = {
        "schema_version": QUIET_STATE_LOG_VERSION,
        "record_type": "regime_state_observation",
        "observed_at": transition.timestamp,
        "threshold_tier": tier.value,
        "scope": {
            "event_id": snapshot.event_id,
            "market_id": snapshot.market_id,
            "token_id": snapshot.token_id,
            "station_id": snapshot.station_id,
            "market_day": snapshot.market_day,
        },
        "state": current,
        "previous_state": previous,
        "reason": transition.reason,
        "quiet_lifecycle": quiet_lifecycle,
        "cancel_required": transition.cancel_required,
        "reduce_only": transition.reduce_only,
        "allow_new_orders": transition.allow_new_orders,
        "coverage_reasons": list(transition.coverage_reasons),
        "true_violations": list(transition.true_violations),
        "violations": list(transition.violations),
        "book": {
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "book_complete": snapshot.book_complete,
        },
        "mandatory_metrics": {
            "metric_status": dict(metrics.metric_status) if metrics is not None else {},
            "trade_count_since_previous": metrics.trade_count if metrics is not None else None,
            "trade_volume_since_previous": metrics.trade_volume if metrics is not None else None,
            "spread": metrics.spread if metrics is not None else None,
            "cross_bucket_mass_error": (
                metrics.cross_bucket_mass_error if metrics is not None else None
            ),
        },
        # The strategy supplies this only from its existing token-native
        # shadow queue. An empty list means no active order at this receipt;
        # the state log never derives queue progress from L2 removals.
        "order_queue": list(snapshot.metadata.get("quiet_state_order_queue") or ()),
        "execution_enabled": False,
    }
    record["record_id"] = _record_id(record)
    return record


class QuietForwardStateCollector:
    """Collect state observations from an existing replay/follower callback."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def observe(
        self,
        tier: ThresholdTier,
        snapshot: BookSnapshot,
        machine: MarketRegimeStateMachine,
        transition: RegimeTransition,
    ) -> None:
        self.records.append(quiet_transition_record(tier, snapshot, machine, transition))


def _read_cursor(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_json_value(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def append_forward_quiet_state_log(
    records: Sequence[Mapping[str, Any]],
    *,
    ledger_path: Path | str = DEFAULT_QUIET_STATE_LOG_PATH,
    cursor_path: Path | str = DEFAULT_QUIET_STATE_CURSOR_PATH,
    bootstrap_at_tail: bool = True,
) -> dict[str, Any]:
    """Append only new receipt-time state records to an independent ledger.

    The initial tail bootstrap intentionally does not write historical state
    observations.  It makes a forward evidence boundary explicit and avoids
    letting an old replay pose as a live record.
    """
    ledger = Path(ledger_path)
    cursor_file = Path(cursor_path)
    normalised: list[dict[str, Any]] = []
    for value in records:
        if not isinstance(value, Mapping) or value.get("observed_at") is None:
            continue
        record = dict(value)
        record["observed_at"] = _utc(record["observed_at"])
        record.setdefault("schema_version", QUIET_STATE_LOG_VERSION)
        record.setdefault("execution_enabled", False)
        if record["execution_enabled"] is not False:
            raise ValueError("QUIET state log is read-only and requires execution_enabled=false")
        record["record_id"] = str(record.get("record_id") or _record_id(record))
        normalised.append(record)
    normalised.sort(key=lambda row: (_utc(row["observed_at"]), str(row["record_id"])))
    cursor = _read_cursor(cursor_file)
    if cursor is None and bootstrap_at_tail:
        tail = _utc(normalised[-1]["observed_at"]) if normalised else None
        tail_ids = [
            str(row["record_id"])
            for row in normalised
            if tail is not None and _utc(row["observed_at"]) == tail
        ]
        _atomic_json(
            cursor_file,
            {
                "schema_version": QUIET_STATE_LOG_VERSION,
                "bootstrap_mode": "tail_only_no_historical_state_rows",
                "last_observed_at": tail,
                "seen_record_ids_at_last_timestamp": tail_ids,
                "execution_enabled": False,
            },
        )
        return {
            "schema_version": QUIET_STATE_LOG_VERSION,
            "bootstrap": True,
            "appended_state_record_count": 0,
            "suppressed_historical_record_count": len(normalised),
            "last_observed_at": tail,
            "execution_enabled": False,
        }

    last_at = _utc(cursor["last_observed_at"]) if cursor and cursor.get("last_observed_at") else None
    seen_at_last = {
        str(value) for value in (cursor or {}).get("seen_record_ids_at_last_timestamp", ())
    }
    appended: list[dict[str, Any]] = []
    late_ignored = 0
    for record in normalised:
        observed_at = _utc(record["observed_at"])
        identifier = str(record["record_id"])
        if last_at is not None and observed_at < last_at:
            late_ignored += 1
            continue
        if observed_at == last_at and identifier in seen_at_last:
            continue
        appended.append(record)
    if appended:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            for record in appended:
                handle.write(json.dumps(_json_value(record), ensure_ascii=False) + "\n")
        new_tail = _utc(appended[-1]["observed_at"])
        new_seen = {
            str(record["record_id"])
            for record in appended
            if _utc(record["observed_at"]) == new_tail
        }
        if new_tail == last_at:
            new_seen.update(seen_at_last)
    else:
        new_tail = last_at
        new_seen = seen_at_last
    _atomic_json(
        cursor_file,
        {
            "schema_version": QUIET_STATE_LOG_VERSION,
            "bootstrap_mode": "tail_only_no_historical_state_rows",
            "last_observed_at": new_tail,
            "seen_record_ids_at_last_timestamp": sorted(new_seen),
            "execution_enabled": False,
        },
    )
    return {
        "schema_version": QUIET_STATE_LOG_VERSION,
        "bootstrap": False,
        "appended_state_record_count": len(appended),
        "late_record_ignored_count": late_ignored,
        "last_observed_at": new_tail,
        "execution_enabled": False,
    }


__all__ = [
    "DEFAULT_QUIET_STATE_CURSOR_PATH",
    "DEFAULT_QUIET_STATE_LOG_PATH",
    "QUIET_STATE_LOG_VERSION",
    "QuietForwardStateCollector",
    "append_forward_quiet_state_log",
    "quiet_transition_record",
]
