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
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.archive_io import open_jsonl_text
from poly_weather.receipt_journal import ReceiptIntegrityError, ReceiptJournal, _read, digest
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
    *,
    request_started_at: datetime | None = None,
    response_received_at: datetime | None = None,
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in existing:
        if not isinstance(row, Mapping):
            continue
        merged.setdefault(public_trade_key(row), dict(row))
    for trade in fetched:
        row = trade.as_json()
        if response_received_at is not None:
            receipt = response_received_at.isoformat()
            row.update(
                available_at=receipt, first_seen_at=receipt,
                receipt_timestamp_text=receipt,
                request_started_at=request_started_at.isoformat() if request_started_at else None,
                response_received_at=receipt,
                receipt_provenance="collector_market_trades_return_v1",
            )
        # Existing rows, including legacy rows with unknown receipt, are facts.
        # A later response never upgrades or rewrites their first visibility.
        merged.setdefault(public_trade_key(trade), row)
    rows = list(merged.values())
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), public_trade_key(row)))
    return rows


def _member_identity(row: Mapping[str, Any]) -> str:
    values = []
    for value in public_trade_key(row):
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise ValueError("nonfinite trade identity")
            text = format(value, "f")
            values.append(text.rstrip("0").rstrip(".") if "." in text else text)
        elif isinstance(value, datetime):
            values.append(value.isoformat())
        else:
            values.append(str(value))
    return digest(values)


def _receipt_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    fact, witness = record["fact"], record["witness"]
    rows = []
    for member in fact["members"]:
        row = dict(member)
        visible = max(_utc(row["timestamp"]), _utc(fact["response_received_at"]),
                      _utc(witness["receipt_committed_at"])).isoformat()
        coherent = _utc(row["timestamp"]) <= _utc(fact["response_received_at"])
        if not coherent:
            visible = None
        row.update(
            source_timestamp=row["timestamp"],
            request_started_at=fact["request_started_at"],
            response_received_at=fact["response_received_at"],
            first_seen_at=fact["response_received_at"],
            receipt_committed_at=witness["receipt_committed_at"],
            decision_visible_at=visible, available_at=visible,
            receipt_validation_state="verified" if coherent else "UNKNOWN_FUTURE_SOURCE",
            receipt_timestamp_text=fact["response_received_at"],
            receipt_provenance="durable_public_receipt_v1",
            receipt_journal_sequence=fact["sequence"],
            receipt_fact_digest=fact["checksum"],
            receipt_member_id=_member_identity(row),
        )
        rows.append(row)
    return rows


def _reconcile_receipts(old_rows, records, journal, event_slug):
    baseline = records[0]["fact"].get("legacy_baseline", []) if records else []
    merged = {public_trade_key(row): dict(row) for row in [*baseline, *old_rows]}
    expected = {}
    for record in records:
        for row in _receipt_rows(record):
            expected.setdefault(public_trade_key(row), row)
    for key, row in expected.items():
        prior = merged.get(key)
        if prior is not None and prior.get("receipt_provenance") == "durable_public_receipt_v1":
            if prior != row:
                journal.discrepancy({"event_slug": event_slug, "member_id": row["receipt_member_id"],
                                     "reason": "existing_receipt_conflict"})
                raise ReceiptIntegrityError("journal/tape receipt conflict")
        # Existing legacy or pre-journal rows are never promoted by a retry.
        merged.setdefault(key, row)
    for key, row in merged.items():
        if row.get("receipt_provenance") == "durable_public_receipt_v1" and key not in expected:
            journal.discrepancy({"event_slug": event_slug, "reason": "orphan_tape_member"})
            raise ReceiptIntegrityError("journal/tape orphan member")
    return sorted(merged.values(), key=lambda row: (str(row.get("timestamp")), public_trade_key(row)))


def verify_materialized_receipts(path: Path, payload: Mapping[str, Any], *,
                                 journal: ReceiptJournal | None = None) -> None:
    """Read-only consumer verification; legacy tapes are not promoted to journal facts."""
    rows = payload.get("trades") or []
    journal_rows = [row for row in rows if isinstance(row, Mapping) and row.get("receipt_provenance") == "durable_public_receipt_v1"]
    root = path.parent / ".receipt_journal"
    directory = root / "materializations" / digest(path.stem)
    if payload.get("receipt_contract") != "durable_public_receipt_v1" and not journal_rows and not directory.exists():
        return
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ReceiptIntegrityError("invalid materialized members schema")
    anchor = payload.get("receipt_journal_anchor")
    if not isinstance(anchor, Mapping):
        raise ReceiptIntegrityError("materialized receipt anchor missing")
    journal = journal or ReceiptJournal(root, lambda: datetime.now(UTC), read_only=True,
                                        prefix=anchor.get("sequence"))
    if journal.root.resolve() != root.resolve():
        raise ReceiptIntegrityError("receipt journal scope mismatch")
    journal.verify_anchor(anchor)
    event = str(payload.get("event_slug") or "")
    if event != path.stem:
        raise ReceiptIntegrityError("materialized event scope conflict")
    identity = payload.get("materialization_id")
    if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ReceiptIntegrityError("materialization identity missing or invalid")
    manifest = _read(directory / f"{identity}.json")
    manifest_path = directory / f"{identity}.json"
    stat = manifest_path.stat()
    journal.verified_objects[manifest_path] = (manifest["checksum"], stat.st_dev, stat.st_ino)
    body = {key: value for key, value in payload.items() if key != "materialization_id"}
    if (manifest.get("schema_version") != 1 or manifest.get("execution_enabled") is not False
            or manifest.get("event_slug") != event or manifest.get("payload") != body
            or digest(body) != identity or body.get("receipt_contract") != "durable_public_receipt_v1"):
        raise ReceiptIntegrityError("materialization payload conflict")
    expected = {}
    event_records = [record for record in journal.records if record["fact"]["event_slug"] == event
                     and record["fact"]["sequence"] <= anchor["sequence"]]
    if not event_records or event_records[-1]["fact"]["sequence"] != anchor["sequence"]:
        raise ReceiptIntegrityError("materialization anchor scope conflict")
    for row in event_records[0]["fact"].get("legacy_baseline", []):
        expected.setdefault(public_trade_key(row), row)
    for record in event_records:
        for row in _receipt_rows(record):
            expected.setdefault(public_trade_key(row), row)
    if len(rows) != len(expected) or payload.get("trade_count") != len(expected):
        raise ReceiptIntegrityError("materialized receipt member set incomplete or duplicated")
    if len({public_trade_key(row) for row in rows}) != len(rows):
        raise ReceiptIntegrityError("duplicate materialized receipt member")
    for row in rows:
        if expected.get(public_trade_key(row)) != row:
            raise ReceiptIntegrityError("materialized receipt member mismatch")


def collect_depth_event_trades(
    coverages: Sequence[DepthEventCoverage],
    *,
    client: TradeClient,
    output_dir: Path | str,
    cursor_path: Path | str | None = None,
    overlap: timedelta = timedelta(seconds=2),
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Incrementally fetch and merge tapes for every archived depth event.

    ``overlap`` catches a second-resolution boundary without re-fetching the
    complete history.  Exact identity de-duplication makes the overlap safe.
    ``now`` controls report generation only (legacy call compatibility).
    ``clock`` supplies actual request/response/persistence clocks; production
    uses UTC wall time. Receipt is the fully materialized client-return bound,
    not a claim about an earlier HTTP page or socket arrival.
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
            clock=clock or (lambda: datetime.now(UTC)),
        )


def _collect_depth_event_trades_locked(
    coverages: Sequence[DepthEventCoverage], *, client: TradeClient,
    destination_root: Path, cursor_file: Path, overlap: timedelta,
    now: datetime | None,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    previous_clock: datetime | None = None

    def read_clock() -> datetime:
        nonlocal previous_clock
        value = clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collector clock must be timezone-aware")
        value = value.astimezone(UTC)
        if previous_clock is not None and value < previous_clock:
            raise ValueError("collector clock moved backwards")
        previous_clock = value
        return value

    cursor = load_trade_cursor(cursor_file)
    journal = ReceiptJournal(destination_root / ".receipt_journal", read_clock,
                             read_only=True, allow_pending_tail=True)
    journal.verify_anchor(cursor.get("receipt_journal_anchor"))
    # No recovery witness or tape overwrite before existing evidence is validated.
    for existing in destination_root.glob("*.json"):
        if existing.name == "depth_trade_coverage.json":
            continue
        bound_events = {record["fact"]["event_slug"] for record in journal.records}
        if journal.pending_fact:
            bound_events.add(journal.pending_fact["event_slug"])
        if existing.stem not in bound_events:
            continue  # Existing legacy corrupt-file quarantine remains per event.
        try:
            prior = _read_payload(existing)
            if prior is not None:
                verify_materialized_receipts(existing, prior)
        except (TradeStorageError, ReceiptIntegrityError) as exc:
            journal.discrepancy({"event_slug": existing.stem, "reason": "materialization_preflight_conflict"})
            raise ReceiptIntegrityError("journal/tape preflight conflict") from exc
    journal.recover_pending()
    collector_run_id = uuid4().hex
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
        journal.verify_anchor(old_payload.get("receipt_journal_anchor"))
        old_rows = old_payload.get("trades", [])
        state = events_cursor.setdefault(event_slug, {})
        records = [record for record in journal.records
                   if record["fact"]["event_slug"] == event_slug]
        old_rows = _reconcile_receipts(old_rows, records, journal, event_slug)
        if records:
            latest = records[-1]["fact"]
            old_payload = {**old_payload, "collection_status": latest["collection_status"]}
            for record in records:
                if record["fact"]["collection_status"] != "collection_error":
                    state["watermark_end"] = record["fact"]["coverage_end"]
        previous_end = None
        request_started_at = None
        response_received_at = None
        failure_observed_at = None
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
            request_started_at = read_clock()
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
                # Never persist exception bodies that may contain request secrets.
                state["last_error"] = type(exc).__name__
                failure_observed_at = read_clock()
            else:
                response_received_at = read_clock()
                if response_received_at < request_started_at:
                    raise ValueError("collector clock moved backwards during request")
                status = "collected_nonzero" if fetched else "collected_zero"
            members = [trade.as_json() for trade in fetched]
            # Response -> fact fsync -> post-fsync witness -> tape -> cursor -> audit.
            record = journal.append({
                "source": "public_data_api_trades", "collector_run_id": collector_run_id,
                "event_slug": event_slug, "condition_ids": list(coverage.condition_ids),
                "asset_ids": list(coverage.asset_ids), "coverage_end": coverage.end_at.isoformat(),
                "request_started_at": request_started_at.isoformat(),
                "response_received_at": response_received_at.isoformat() if response_received_at else None,
                "response_complete": status != "collection_error",
                "failure_observed_at": failure_observed_at.isoformat() if failure_observed_at else None,
                "query": {"start": request_start.isoformat(), "end": request_end.isoformat(),
                          "taker_only": True},
                "page_coverage": "UNKNOWN_ADAPTER_DOES_NOT_EXPOSE_PAGE_RECEIPTS",
                "upstream_quality": "unknown", "incident": None, "gap": "unknown",
                "collection_status": status,
                "error_type": state.get("last_error") if status == "collection_error" else None,
                "producer_version": "durable_public_receipt_v1",
                "config_hash": digest({"taker_only": True, "overlap": str(overlap)}),
                "members": members, "member_ids": [_member_identity(row) for row in members],
                "member_count": len(members),
                "legacy_baseline": old_rows if not records else [],
            })
            records.append(record)
        rows = _reconcile_receipts(old_rows, records, journal, event_slug)
        trade_assets = {str(row.get("asset_id") or "") for row in rows if row.get("asset_id")}
        intersected = sorted(set(coverage.asset_ids) & trade_assets)
        unmatched = [
            {"asset_id": asset, "reason": "no_trade_for_snapshot_token"}
            for asset in coverage.asset_ids
            if asset not in trade_assets
        ]
        payload = {
            **old_payload,
            "schema_version": 3,
            "fetched_at": (response_received_at.isoformat() if response_received_at
                           else old_payload.get("fetched_at")),
            "file_written_at": read_clock().isoformat(),
            "receipt_contract": "durable_public_receipt_v1",
            "receipt_journal_anchor": ({"sequence": records[-1]["fact"]["sequence"],
                                        "digest": records[-1]["fact"]["checksum"]}
                                       if records else old_payload.get("receipt_journal_anchor")),
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
        payload["materialization_id"] = journal.commit_materialization(event_slug, {
            key: value for key, value in payload.items() if key != "materialization_id"
        })
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
    if journal.records:
        cursor["receipt_journal_anchor"] = {
            "sequence": journal.records[-1]["fact"]["sequence"],
            "digest": journal.records[-1]["fact"]["checksum"],
        }
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
