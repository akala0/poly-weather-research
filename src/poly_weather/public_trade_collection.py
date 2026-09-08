"""Incremental collection of the public Data API trade tape.

The depth archive is the source of truth for which events and condition/token
identities are in scope.  This module deliberately keeps collection metadata
next to the tape: an empty result after a successful request is different from
an event that was never requested.  Public executions are evidence for queue
consumption only; they are not historical bid/ask quotes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.archive_io import open_jsonl_text
from poly_weather.runtime_safety import atomic_json_write


class TradeClient(Protocol):
    def market_trades(
        self,
        *,
        market_ids: Sequence[str],
        start: datetime,
        end: datetime,
        taker_only: bool = True,
    ) -> list[PublicTrade]: ...


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_from_market_slug(value: Any) -> tuple[str, str, str] | None:
    """Return event, condition-market slug and outcome from an archive identity."""
    text = str(value or "")
    base, separator, outcome = text.rpartition(":")
    if not separator or outcome.casefold() not in {"yes", "no"}:
        return None
    event_slug = base.split("/", 1)[0]
    if not event_slug:
        return None
    return event_slug, base, outcome.casefold()


@dataclass(frozen=True, slots=True)
class DepthEventCoverage:
    event_slug: str
    condition_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    start_at: datetime
    end_at: datetime
    snapshot_count: int
    assets_by_condition: Mapping[str, tuple[str, ...]]
    market_slugs: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_at"] = self.start_at.isoformat()
        payload["end_at"] = self.end_at.isoformat()
        payload["condition_ids"] = list(self.condition_ids)
        payload["asset_ids"] = list(self.asset_ids)
        payload["market_slugs"] = list(self.market_slugs)
        payload["assets_by_condition"] = {
            str(key): list(value) for key, value in self.assets_by_condition.items()
        }
        return payload


def discover_depth_event_coverage(
    checkpoint_paths: Iterable[Path],
) -> tuple[DepthEventCoverage, ...]:
    """Discover event/condition/token coverage from plain or gzip JSONL.

    Only archive receipt time is used for the collection window.  A malformed
    row is ignored (fail closed) rather than inventing an event or timestamp.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for path in checkpoint_paths:
        if not path.exists():
            continue
        try:
            handle = open_jsonl_text(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    identity = _event_from_market_slug(row.get("market_slug"))
                    received = _utc(str(row["received_at"]))
                    condition_id = str(row.get("market_id") or "").strip()
                    asset_id = str(row.get("asset_id") or "").strip()
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if identity is None or not condition_id or not asset_id:
                    continue
                event_slug, market_slug, _outcome = identity
                value = grouped.setdefault(
                    event_slug,
                    {
                        "conditions": set(),
                        "assets": set(),
                        "assets_by_condition": {},
                        "market_slugs": set(),
                        "start": received,
                        "end": received,
                        "count": 0,
                    },
                )
                value["conditions"].add(condition_id)
                value["assets"].add(asset_id)
                value["assets_by_condition"].setdefault(condition_id, set()).add(asset_id)
                value["market_slugs"].add(market_slug)
                value["start"] = min(value["start"], received)
                value["end"] = max(value["end"], received)
                value["count"] += 1
    output: list[DepthEventCoverage] = []
    for event_slug, value in sorted(grouped.items()):
        output.append(
            DepthEventCoverage(
                event_slug=event_slug,
                condition_ids=tuple(sorted(value["conditions"])),
                asset_ids=tuple(sorted(value["assets"])),
                start_at=value["start"],
                end_at=value["end"],
                snapshot_count=int(value["count"]),
                assets_by_condition={
                    key: tuple(sorted(assets))
                    for key, assets in sorted(value["assets_by_condition"].items())
                },
                market_slugs=tuple(sorted(value["market_slugs"])),
            )
        )
    return tuple(output)


def public_trade_key(row: PublicTrade | Mapping[str, Any]) -> tuple[Any, ...]:
    """Strongest available identity for a canonical taker execution."""
    if isinstance(row, PublicTrade):
        return (
            row.transaction_hash,
            row.proxy_wallet,
            row.asset_id,
            row.condition_id,
            row.timestamp,
            row.side,
            row.size,
            row.price,
        )
    timestamp = row.get("timestamp")
    try:
        timestamp_value: Any = _utc(str(timestamp))
    except (TypeError, ValueError):
        timestamp_value = str(timestamp or "")
    # Decimal normalisation makes a JSON string such as ``"3.0"`` compare
    # equal to the PublicTrade value ``Decimal("3")``.
    def decimal_value(value: Any) -> Any:
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError, TypeError):
            return str(value or "")

    return (
        str(row.get("transaction_hash") or row.get("transactionHash") or ""),
        str(row.get("proxy_wallet") or row.get("proxyWallet") or ""),
        str(row.get("asset_id") or row.get("asset") or ""),
        str(row.get("condition_id") or row.get("conditionId") or ""),
        timestamp_value,
        str(row.get("side") or "").upper(),
        decimal_value(row.get("size")),
        decimal_value(row.get("price")),
    )


class TradeStorageError(ValueError):
    """An existing evidence object cannot safely be treated as empty."""

    def __init__(self, path: Path, state: str):
        self.path = path
        self.state = state
        super().__init__(f"{state}: {path}")


def _read_payload(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TradeStorageError(path, "unreadable") from exc
    if not raw.strip():
        raise TradeStorageError(path, "empty_file")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TradeStorageError(path, "corrupt_json") from exc
    if not isinstance(payload, dict):
        raise TradeStorageError(path, "invalid_schema")
    return payload


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_json_write(path, payload, integrity_metadata=False, keep_last_good=False)


@contextmanager
def _writer_lock(path: Path):
    """Cooperating collector ownership; OS releases the lock after a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_trade_cursor(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    payload = _read_payload(path)
    if payload is None:
        return {"schema_version": 1, "events": {}}
    events = payload.get("events")
    if not isinstance(events, dict) or any(not isinstance(row, dict) for row in events.values()):
        raise TradeStorageError(path, "invalid_cursor_schema")
    for row in events.values():
        if row.get("watermark_end"):
            try:
                _utc(str(row["watermark_end"]))
            except ValueError as exc:
                raise TradeStorageError(path, "invalid_cursor_watermark") from exc
    return payload


def _merge_trades(
    existing: Sequence[Mapping[str, Any]],
    fetched: Sequence[PublicTrade],
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in existing:
        if not isinstance(row, Mapping):
            continue
        merged.setdefault(public_trade_key(row), dict(row))
    for trade in fetched:
        merged.setdefault(public_trade_key(trade), trade.as_json())
    rows = list(merged.values())
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), public_trade_key(row)))
    return rows


def collect_depth_event_trades(
    coverages: Sequence[DepthEventCoverage],
    *,
    client: TradeClient,
    output_dir: Path | str,
    cursor_path: Path | str | None = None,
    overlap: timedelta = timedelta(seconds=2),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Incrementally fetch and merge tapes for every archived depth event.

    ``overlap`` catches a second-resolution boundary without re-fetching the
    complete history.  Exact identity de-duplication makes the overlap safe.
    """
    destination_root = Path(output_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    cursor_file = Path(cursor_path) if cursor_path else destination_root / ".depth_trade_cursor.json"
    locks = {
        destination_root.resolve() / ".trade_collection.lock",
        cursor_file.resolve().with_name(cursor_file.name + ".lock"),
    }
    with ExitStack() as stack:
        for lock in sorted(locks, key=lambda path: str(path).casefold()):
            stack.enter_context(_writer_lock(lock))
        return _collect_depth_event_trades_locked(
            coverages, client=client, destination_root=destination_root,
            cursor_file=cursor_file, overlap=overlap, now=now,
        )


def _collect_depth_event_trades_locked(
    coverages: Sequence[DepthEventCoverage], *, client: TradeClient,
    destination_root: Path, cursor_file: Path, overlap: timedelta,
    now: datetime | None,
) -> dict[str, Any]:
    cursor = load_trade_cursor(cursor_file)
    events_cursor = cursor.setdefault("events", {})
    fetched_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    summaries: list[dict[str, Any]] = []
    for coverage in coverages:
        event_slug = coverage.event_slug
        if (
            not event_slug or event_slug in {".", "..", "depth_trade_coverage"}
            or any(char in event_slug for char in '/\\:')
        ):
            raise TradeStorageError(destination_root, "invalid_event_path")
        path = destination_root / f"{event_slug}.json"
        if path.resolve().parent != destination_root.resolve():
            raise TradeStorageError(path, "event_path_escape")
        try:
            old_payload = _read_payload(path)
            if old_payload is not None and (
                not isinstance(old_payload.get("trades"), list)
                or any(not isinstance(row, dict) for row in old_payload["trades"])
                or old_payload.get("event_slug", event_slug) != event_slug
            ):
                raise TradeStorageError(path, "invalid_tape_schema")
        except TradeStorageError as exc:
            summaries.append({
                "event_slug": event_slug, "path": str(path.resolve()),
                "collection_status": "storage_quarantined", "storage_state": exc.state,
                "watermark_frozen": True, "trade_count": 0,
                "trade_count_known": False, "trade_asset_intersection_count": 0,
            })
            continue
        old_payload = old_payload or {}
        old_rows = old_payload.get("trades", [])
        state = events_cursor.setdefault(event_slug, {})
        previous_end = None
        if old_payload and state.get("watermark_end"):
            try:
                previous_end = _utc(str(state["watermark_end"]))
            except ValueError:
                previous_end = None
        # A coverage window that has not advanced is already represented by
        # the local tape.  No request is made and the status remains explicit.
        if previous_end is not None and coverage.end_at <= previous_end and old_payload:
            fetched: list[PublicTrade] = []
            status = str(old_payload.get("collection_status") or state.get("status") or "not_collected")
            request_start = previous_end
            request_end = previous_end
        else:
            request_start = coverage.start_at if previous_end is None else max(
                coverage.start_at, previous_end - overlap
            )
            request_end = coverage.end_at + timedelta(seconds=1)
            try:
                fetched = client.market_trades(
                    market_ids=coverage.condition_ids,
                    start=request_start,
                    end=request_end,
                    taker_only=True,
                )
            except Exception as exc:  # network/API failure is recorded, not mislabelled zero
                fetched = []
                status = "collection_error"
                state["last_error"] = f"{type(exc).__name__}: {exc}"
            else:
                status = "collected_nonzero" if fetched else "collected_zero"
        rows = _merge_trades(old_rows, fetched)
        trade_assets = {str(row.get("asset_id") or "") for row in rows if row.get("asset_id")}
        intersected = sorted(set(coverage.asset_ids) & trade_assets)
        unmatched = [
            {"asset_id": asset, "reason": "no_trade_for_snapshot_token"}
            for asset in coverage.asset_ids
            if asset not in trade_assets
        ]
        payload = {
            **old_payload,
            "schema_version": 2,
            "fetched_at": fetched_at,
            "source": "https://data-api.polymarket.com/trades",
            "event_slug": event_slug,
            "condition_ids": list(coverage.condition_ids),
            "snapshot_asset_ids": list(coverage.asset_ids),
            "snapshot_token_count": len(coverage.asset_ids),
            "depth_coverage": coverage.as_json(),
            "start": request_start.isoformat() if request_start <= request_end else None,
            "end": request_end.isoformat() if request_start <= request_end else None,
            "taker_only": True,
            "tape_semantics": (
                "canonical public taker executions; execution prices are not resting bid/ask quotes"
            ),
            "collection_status": status,
            "trade_count": len(rows),
            "fetched_trade_count": len(fetched),
            "trade_asset_intersection_count": len(intersected),
            "trade_asset_intersection": intersected,
            "unmatched_snapshot_assets": unmatched,
            "trades": rows,
            "fetched_ranges": [
                *(
                    old_payload.get("fetched_ranges")
                    if isinstance(old_payload.get("fetched_ranges"), list)
                    else []
                ),
                {
                    "start": request_start.isoformat(),
                    "end": request_end.isoformat(),
                    "trade_count": len(fetched),
                },
            ]
            if fetched
            else old_payload.get("fetched_ranges", []),
        }
        _atomic_json_write(path, payload)
        state.update(
            {
                "watermark_snapshot_count": coverage.snapshot_count,
                "status": status,
                "last_fetched_at": fetched_at,
                "last_fetched_trade_count": len(fetched),
            }
        )
        if status != "collection_error":
            state["watermark_end"] = coverage.end_at.isoformat()
        summaries.append(
            {
                "event_slug": event_slug,
                "path": str(path.resolve()),
                "collection_status": status,
                "snapshot_count": coverage.snapshot_count,
                "snapshot_token_count": len(coverage.asset_ids),
                "trade_count": len(rows),
                "fetched_trade_count": len(fetched),
                "trade_asset_intersection_count": len(intersected),
                "unmatched_snapshot_asset_count": len(unmatched),
            }
        )
    cursor["schema_version"] = 1
    cursor["updated_at"] = fetched_at
    _atomic_json_write(cursor_file, cursor)
    audit = {
        "schema_version": 1,
        "generated_at": fetched_at,
        "depth_event_count": len(summaries),
        "collected_zero_event_count": sum(row["collection_status"] == "collected_zero" for row in summaries),
        "not_collected_event_count": sum(
            row["collection_status"] in {"not_collected", "collection_error", "storage_quarantined"}
            for row in summaries
        ),
        "storage_quarantined_event_count": sum(
            row["collection_status"] == "storage_quarantined" for row in summaries
        ),
        "trade_count_complete": not any(
            row["collection_status"] == "storage_quarantined" for row in summaries
        ),
        "trade_count": sum(row["trade_count"] for row in summaries),
        "trade_asset_intersection_count": sum(
            row["trade_asset_intersection_count"] for row in summaries
        ),
        "events": summaries,
        "cursor_path": str(cursor_file.resolve()),
        "execution_enabled": False,
    }
    _atomic_json_write(destination_root / "depth_trade_coverage.json", audit)
    return audit


__all__ = [
    "DepthEventCoverage",
    "collect_depth_event_trades",
    "discover_depth_event_coverage",
    "load_trade_cursor",
    "public_trade_key",
]
