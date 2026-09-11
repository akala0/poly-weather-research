from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from websockets.asyncio.client import connect

from poly_weather.business_readiness import ArchiveCommitProgress, producer_progress_sample
from poly_weather.polymarket_status import (
    STATUS_POLL_SECONDS,
    PolymarketStatusClient,
    PolymarketStatusSnapshot,
    UpstreamQualityWindow,
    load_quality_overrides,
    load_quality_windows,
    merge_quality_windows,
    persist_quality_windows,
    quality_window_at,
)
from poly_weather.research_store import ResearchWarehouse
from poly_weather.retention import disk_capacity_status
from poly_weather.runtime_safety import atomic_json_write

DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024
RECONNECT_LOOP_WINDOW_SECONDS = 300.0
RECONNECT_LOOP_THRESHOLD = 5


@dataclass(slots=True)
class StreamRecord:
    run_id: str
    sequence: int
    received_at_ns: int
    source_timestamp_ms: int | None
    event_type: str
    asset_id: str | None
    market_id: str | None
    market_slug: str | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    last_trade_price: Decimal | None
    bids: tuple[dict[str, str], ...] | None
    asks: tuple[dict[str, str], ...] | None
    book_complete: bool
    upstream_status: str
    upstream_incident_id: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class BookState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    last_trade_price: Decimal | None = None
    complete: bool = False

    def replace(self, bids: list[dict[str, Any]], asks: list[dict[str, Any]]) -> None:
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)
        self.complete = True
        self._refresh_top()

    @staticmethod
    def _levels(rows: list[dict[str, Any]]) -> dict[Decimal, Decimal]:
        levels: dict[Decimal, Decimal] = {}
        for row in rows:
            price = Decimal(str(row["price"]))
            size = Decimal(str(row["size"]))
            if size > 0:
                levels[price] = size
        return levels

    def change(self, *, side: str, price: Decimal, size: Decimal) -> None:
        levels = self.bids if side.upper() == "BUY" else self.asks
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self._refresh_top()

    def _refresh_top(self) -> None:
        self.best_bid = max(self.bids, default=None)
        self.best_ask = min(self.asks, default=None)

    def depth(self) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...]]:
        bids = tuple(
            {"price": str(price), "size": str(self.bids[price])}
            for price in sorted(self.bids, reverse=True)
        )
        asks = tuple(
            {"price": str(price), "size": str(self.asks[price])} for price in sorted(self.asks)
        )
        return bids, asks


@dataclass(slots=True)
class StreamMetrics:
    run_id: str
    started_at: str
    state: str = "starting"
    connections: int = 0
    reconnects: int = 0
    messages: int = 0
    events: int = 0
    events_skipped_archive: int = 0
    hourly_audit_snapshots: int = 0
    rows_written: int = 0
    database_rows_written: int = 0
    pongs: int = 0
    parse_errors: int = 0
    queue_high_water: int = 0
    database_queue_high_water: int = 0
    last_event_at: str | None = None
    last_error: str | None = None
    last_error_fingerprint: str | None = None
    same_error_reconnects_5m: int = 0
    deterministic_reconnect_fault: str | None = None
    reconnect_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SubscriptionCommand:
    operation: str
    asset_ids: tuple[str, ...]
    sent: asyncio.Future[None]


@dataclass(frozen=True, slots=True)
class EventArchivePolicy:
    timezone: str
    target_date: date
    full_start: datetime_time = datetime_time(9, 0)
    full_end: datetime_time = datetime_time(20, 0)

    def full_capture(self, received_at: datetime) -> bool:
        local_time = received_at.astimezone(ZoneInfo(self.timezone)).time().replace(tzinfo=None)
        return self.full_start <= local_time < self.full_end


class MarketStreamSink:
    """Single-writer sink for append-only JSONL and batched DuckDB rows."""

    def __init__(self, *, data_dir: Path, run_id: str) -> None:
        self.data_dir = data_dir
        self.run_id = run_id
        # Each long-running process owns its DuckDB file. DuckDB is optimized for a
        # single writer, so this avoids cross-process write locking when both
        # daemons are running.
        self.warehouse = ResearchWarehouse(data_dir / "market_stream.duckdb")
        self.handles: dict[str, Any] = {}
        self.checkpoint_handles: dict[str, Any] = {}
        self.progress = ArchiveCommitProgress()

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        for handle in self.checkpoint_handles.values():
            handle.close()
        self.checkpoint_handles.clear()
        self.warehouse.close()

    def _handle(self, day: str) -> Any:
        handle = self.handles.get(day)
        if handle is None:
            path = self.data_dir / "raw" / "polymarket_clob_websocket" / day / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            self.handles[day] = handle
        return handle

    def _checkpoint_handle(self, day: str) -> Any:
        handle = self.checkpoint_handles.get(day)
        if handle is None:
            path = self.data_dir / "raw" / "polymarket_book_checkpoints" / day / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            self.checkpoint_handles[day] = handle
        return handle

    def write(self, records: list[StreamRecord]) -> None:
        self.write_raw(records)
        self.write_database(records)

    def write_raw(self, records: list[StreamRecord]) -> None:
        if not records:
            return
        touched = []
        for record in records:
            received = datetime.fromtimestamp(record.received_at_ns / 1_000_000_000, tz=UTC)
            envelope = {
                "run_id": record.run_id,
                "sequence": record.sequence,
                "received_at": received.isoformat(),
                "received_at_ns": record.received_at_ns,
                "source_timestamp_ms": record.source_timestamp_ms,
                "event_type": record.event_type,
                "asset_id": record.asset_id,
                "market_id": record.market_id,
                "market_slug": record.market_slug,
                "best_bid": str(record.best_bid) if record.best_bid is not None else None,
                "best_ask": str(record.best_ask) if record.best_ask is not None else None,
                "last_trade_price": (
                    str(record.last_trade_price) if record.last_trade_price is not None else None
                ),
                "bids": record.bids,
                "asks": record.asks,
                "book_complete": record.book_complete,
                "upstream_status": record.upstream_status,
                "upstream_incident_id": record.upstream_incident_id,
                "raw": record.raw,
            }
            handle = self._handle(received.date().isoformat())
            touched.append(handle)
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            handle.write(encoded)
            handle.write("\n")
            if record.bids is not None and record.asks is not None:
                checkpoint_handle = self._checkpoint_handle(received.date().isoformat())
                touched.append(checkpoint_handle)
                checkpoint_handle.write(encoded)
                checkpoint_handle.write("\n")
        self.progress.commit(touched)

    def write_database(self, records: list[StreamRecord]) -> None:
        if not records:
            return
        self.warehouse.append_market_stream_events(records)


class MarketWebSocketBot:
    """Public, unauthenticated Polymarket market-data daemon."""

    def __init__(
        self,
        *,
        asset_slugs: dict[str, str],
        data_dir: Path,
        asset_events: dict[str, str] | None = None,
        event_policies: dict[str, EventArchivePolicy] | None = None,
        websocket_url: str = DEFAULT_MARKET_WS_URL,
        heartbeat_seconds: float = 10,
        silence_timeout_seconds: float = 45,
        batch_size: int = 500,
        flush_interval_seconds: float = 0.25,
        queue_size: int = 50_000,
        max_message_size_bytes: int = DEFAULT_MAX_WS_MESSAGE_SIZE,
        status_poll_seconds: float = STATUS_POLL_SECONDS,
        status_client: PolymarketStatusClient | None = None,
        raw_collection_recovery: bool = False,
        startup_attempt_id: str | None = None,
    ) -> None:
        if not asset_slugs:
            raise ValueError("at least one asset id is required")
        self.asset_slugs = asset_slugs
        self.asset_events = asset_events or {}
        self.event_policies = event_policies or {}
        self.data_dir = data_dir
        self.websocket_url = websocket_url
        self.heartbeat_seconds = heartbeat_seconds
        self.silence_timeout_seconds = silence_timeout_seconds
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        if max_message_size_bytes < 1024 * 1024:
            raise ValueError("market websocket max message size cannot be below 1 MiB")
        self.max_message_size_bytes = max_message_size_bytes
        if status_poll_seconds < 60:
            raise ValueError("Polymarket status polling cannot be more frequent than 60 seconds")
        self.status_poll_seconds = status_poll_seconds
        self.queue: asyncio.Queue[StreamRecord] = asyncio.Queue(maxsize=queue_size)
        self.database_queue: asyncio.Queue[list[StreamRecord]] = asyncio.Queue(maxsize=200)
        self.subscription_queue: asyncio.Queue[SubscriptionCommand] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.run_id = str(uuid4())
        self.metrics = StreamMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.books = {asset_id: BookState() for asset_id in asset_slugs}
        self.book_snapshot_events = {asset_id: asyncio.Event() for asset_id in asset_slugs}
        self.last_depth_checkpoint_ns: dict[str, int] = {}
        self.last_hourly_archive: dict[tuple[str, str], str] = {}
        self.depth_checkpoint_interval_ns = 30_000_000_000
        self.event_counts = {
            event_slug: 0 for event_slug in sorted(set(self.asset_events.values()))
        }
        self.sequence = 0
        self.status_path = data_dir / "runtime" / "polymarket_ws_status.json"
        self.reconnect_log_path = data_dir / "runtime" / "polymarket_ws_reconnects.jsonl"
        self.status_history_path = data_dir / "runtime" / "polymarket_status_history.jsonl"
        self.quality_windows_path = data_dir / "runtime" / "polymarket_quality_windows.json"
        self.status_client = status_client or PolymarketStatusClient()
        self._owns_status_client = status_client is None
        self.raw_collection_recovery = raw_collection_recovery
        self.startup_attempt_id = startup_attempt_id
        self.quality_windows = merge_quality_windows(
            load_quality_windows(self.quality_windows_path),
            load_quality_overrides(),
        )
        self.upstream_snapshot: PolymarketStatusSnapshot | None = None
        self.upstream_status_error: str | None = None
        self._last_upstream_history_signature: str | None = None
        self._initialize_reconnect_log()
        self.sink = MarketStreamSink(data_dir=data_dir, run_id=self.run_id)
        self.last_database_maintenance = time.monotonic()
        self.last_disk_status_at = 0.0
        self.disk_status_cache: dict[str, Any] = {}
        self.reconnect_failures: deque[tuple[float, str]] = deque(maxlen=100)

    def _initialize_reconnect_log(self) -> None:
        """Make forensic logging visible and preserve unattributed legacy counts."""
        self.reconnect_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.reconnect_log_path.exists() and self.status_path.exists():
            try:
                previous = json.loads(self.status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
            reconnects = int(previous.get("reconnects") or 0)
            if reconnects > 0:
                legacy = {
                    "record_type": "legacy_unattributed_summary",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "previous_run_id": previous.get("run_id"),
                    "reconnect_count": reconnects,
                    "previous_started_at": previous.get("started_at"),
                    "previous_last_event_at": previous.get("last_event_at"),
                    "reason": (
                        "unrecoverable: previous runtime retained only transient "
                        "last_error and did not persist per-reconnect causes"
                    ),
                }
                with self.reconnect_log_path.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(
                        json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))
                    )
                    handle.write("\n")
        self.reconnect_log_path.touch(exist_ok=True)

    async def subscribe_assets(
        self,
        *,
        asset_slugs: dict[str, str],
        asset_events: dict[str, str],
        event_policies: dict[str, EventArchivePolicy] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Hot-subscribe and return only after every asset has a fresh full book."""
        new_ids = tuple(asset_id for asset_id in asset_slugs if asset_id not in self.asset_slugs)
        if not new_ids:
            await asyncio.wait_for(
                asyncio.gather(
                    *(
                        self.book_snapshot_events.setdefault(asset_id, asyncio.Event()).wait()
                        for asset_id in asset_slugs
                    )
                ),
                timeout=timeout_seconds,
            )
            return
        for asset_id in new_ids:
            self.asset_slugs[asset_id] = asset_slugs[asset_id]
            self.asset_events[asset_id] = asset_events[asset_id]
            self.books[asset_id] = BookState()
            self.book_snapshot_events[asset_id] = asyncio.Event()
            event_slug = asset_events[asset_id]
            self.event_counts.setdefault(event_slug, 0)
        if event_policies:
            self.event_policies.update(event_policies)
        loop = asyncio.get_running_loop()
        sent = loop.create_future()
        await self.subscription_queue.put(
            SubscriptionCommand(operation="subscribe", asset_ids=new_ids, sent=sent)
        )
        await asyncio.wait_for(sent, timeout=timeout_seconds)
        await asyncio.wait_for(
            asyncio.gather(*(self.book_snapshot_events[asset_id].wait() for asset_id in new_ids)),
            timeout=timeout_seconds,
        )
        self._write_status()

    async def unsubscribe_assets(
        self,
        asset_ids: list[str] | tuple[str, ...],
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Hot-unsubscribe assets after their replacement has been confirmed."""
        selected = tuple(asset_id for asset_id in asset_ids if asset_id in self.asset_slugs)
        if not selected:
            return
        loop = asyncio.get_running_loop()
        sent = loop.create_future()
        await self.subscription_queue.put(
            SubscriptionCommand(operation="unsubscribe", asset_ids=selected, sent=sent)
        )
        await asyncio.wait_for(sent, timeout=timeout_seconds)
        for asset_id in selected:
            self.asset_slugs.pop(asset_id, None)
            self.asset_events.pop(asset_id, None)
            self.books.pop(asset_id, None)
            self.book_snapshot_events.pop(asset_id, None)
            self.last_depth_checkpoint_ns.pop(asset_id, None)
        self._write_status()

    async def run(self, *, runtime_seconds: float = 0) -> StreamMetrics:
        # Fetch before writers start so the first archived frame is correctly
        # tagged and the quality dimension can be updated without sharing the
        # DuckDB connection across worker threads.
        await self._refresh_upstream_status(persist_database=True)
        self.sink.warehouse.start_market_stream_run(
            run_id=self.run_id,
            started_at=datetime.now(UTC),
            websocket_url=self.websocket_url,
            asset_slugs=self.asset_slugs,
        )
        writer = asyncio.create_task(self._writer(), name="market-stream-writer")
        database_writer = asyncio.create_task(
            self._database_writer(), name="market-stream-database-writer"
        )
        status_heartbeat = asyncio.create_task(
            self._status_heartbeat(), name="market-stream-status-heartbeat"
        )
        upstream_monitor = asyncio.create_task(
            self._upstream_status_monitor(), name="polymarket-upstream-status-monitor"
        )
        deadline = time.monotonic() + runtime_seconds if runtime_seconds > 0 else None
        backoff = 1.0
        try:
            while not self.stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    await self._connection(deadline=deadline)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # connection boundary: persist and retry all failures
                    self._record_connection_failure(f"{type(exc).__name__}: {exc}")
                    self._write_status()
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        break
                    delay = min(30.0, backoff) + random.random() * 0.25
                    if remaining is not None:
                        delay = min(delay, max(0.0, remaining))
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    backoff = min(30.0, backoff * 2)
        finally:
            self.stop_event.set()
            writer_results = await asyncio.gather(
                writer,
                database_writer,
                return_exceptions=True,
            )
            status_heartbeat.cancel()
            upstream_monitor.cancel()
            await asyncio.gather(status_heartbeat, upstream_monitor, return_exceptions=True)
            background_errors = [
                result for result in writer_results if isinstance(result, BaseException)
            ]
            self.metrics.state = "failed" if background_errors else "stopped"
            self._write_status()
            self.sink.warehouse.finish_market_stream_run(
                run_id=self.run_id,
                finished_at=datetime.now(UTC),
                metrics=asdict(self.metrics),
            )
            self.sink.close()
            if self._owns_status_client:
                await self.status_client.close()
            if background_errors:
                raise RuntimeError("market stream background writer failed") from background_errors[
                    0
                ]
        return self.metrics

    def _record_connection_failure(
        self,
        error: str,
        *,
        observed_at: float | None = None,
    ) -> None:
        """Classify repeated identical reconnects that cannot self-heal."""
        now = time.monotonic() if observed_at is None else observed_at
        self.metrics.last_error = error
        self.metrics.last_error_fingerprint = error
        self.metrics.reconnects += 1
        active_window = self._active_quality_window(datetime.now(UTC))
        reconnect_event = {
            "run_id": self.run_id,
            "observed_at": datetime.now(UTC).isoformat(),
            "error": error,
            "reconnect_number": self.metrics.reconnects,
            "successful_connections": self.metrics.connections,
            "asset_count": len(self.asset_slugs),
            "event_count": len(self.event_counts),
            "upstream_status": self._quality_status(active_window),
            "upstream_incident_id": active_window.incident_id if active_window else None,
        }
        self.metrics.reconnect_history.append(reconnect_event)
        self.metrics.reconnect_history[:] = self.metrics.reconnect_history[-100:]
        self.reconnect_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.reconnect_log_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(reconnect_event, ensure_ascii=False, separators=(",", ":"))
            )
            handle.write("\n")
        self.reconnect_failures.append((now, error))
        cutoff = now - RECONNECT_LOOP_WINDOW_SECONDS
        while self.reconnect_failures and self.reconnect_failures[0][0] < cutoff:
            self.reconnect_failures.popleft()
        same_error_count = sum(
            failure == error for _, failure in self.reconnect_failures
        )
        self.metrics.same_error_reconnects_5m = same_error_count
        if active_window is not None:
            # Repeated reconnects during an acknowledged upstream maintenance or
            # market-data incident are expected degradation, not a deterministic
            # local configuration loop.  Preserve every reconnect and keep trying.
            self.metrics.deterministic_reconnect_fault = None
            self.metrics.state = "upstream_maintenance_reconnecting"
        elif same_error_count >= RECONNECT_LOOP_THRESHOLD:
            if "message too big" in error.casefold() or "1009" in error:
                fault = "message_too_big"
                self.metrics.state = "faulted_message_too_big"
            else:
                fault = "repeated_identical_connection_failure"
                self.metrics.state = "faulted_reconnect_loop"
            self.metrics.deterministic_reconnect_fault = fault
        else:
            self.metrics.state = "reconnecting"

    def _mark_data_connection_healthy(self) -> None:
        """Clear a latched reconnect fault only after actual market data arrives."""
        self.reconnect_failures.clear()
        self.metrics.last_error = None
        self.metrics.last_error_fingerprint = None
        self.metrics.same_error_reconnects_5m = 0
        self.metrics.deterministic_reconnect_fault = None
        self.metrics.state = (
            "upstream_maintenance"
            if self._active_quality_window(datetime.now(UTC)) is not None
            else "connected"
        )

    def _websocket_connect_options(self) -> dict[str, Any]:
        return {
            "ping_interval": None,
            "close_timeout": 5,
            "max_queue": 4096,
            # A 572-token initial book frame was observed at 1,081,647 bytes,
            # already above websockets' 1 MiB default. Keep ample headroom for
            # daily overlap and planned city expansion; compression stays off.
            "max_size": self.max_message_size_bytes,
            "compression": None,
        }

    async def _connection(self, *, deadline: float | None) -> None:
        async with connect(
            self.websocket_url,
            **self._websocket_connect_options(),
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "assets_ids": list(self.asset_slugs),
                        "type": "market",
                        "custom_feature_enabled": True,
                    },
                    separators=(",", ":"),
                )
            )
            self.metrics.connections += 1
            if self.metrics.deterministic_reconnect_fault is None:
                self.metrics.state = "connected"
                self.metrics.last_error = None
            self._write_status()
            heartbeat = asyncio.create_task(
                self._heartbeat(websocket), name="market-stream-heartbeat"
            )
            last_data_at = time.monotonic()
            try:
                while not self.stop_event.is_set():
                    await self._send_subscription_updates(websocket)
                    if deadline is not None and time.monotonic() >= deadline:
                        return
                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    except TimeoutError:
                        if time.monotonic() - last_data_at > self.silence_timeout_seconds:
                            raise TimeoutError(
                                "market websocket data silence watchdog fired"
                            ) from None
                        continue
                    if isinstance(raw_message, bytes):
                        raw_message = raw_message.decode("utf-8")
                    if raw_message.upper() == "PONG":
                        self.metrics.pongs += 1
                        continue
                    last_data_at = time.monotonic()
                    self._mark_data_connection_healthy()
                    self.metrics.messages += 1
                    try:
                        decoded = json.loads(raw_message)
                    except json.JSONDecodeError:
                        self.metrics.parse_errors += 1
                        continue
                    messages = decoded if isinstance(decoded, list) else [decoded]
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        for record in self._records(message):
                            self.metrics.events += 1
                            event_slug = self.asset_events.get(record.asset_id or "")
                            if event_slug is not None:
                                self.event_counts[event_slug] = (
                                    self.event_counts.get(event_slug, 0) + 1
                                )
                            if self._prepare_for_archive(record):
                                await self.queue.put(record)
                                self.metrics.queue_high_water = max(
                                    self.metrics.queue_high_water, self.queue.qsize()
                                )
                            else:
                                self.metrics.events_skipped_archive += 1
                    self.metrics.last_event_at = datetime.now(UTC).isoformat()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    def _prepare_for_archive(self, record: StreamRecord) -> bool:
        """Apply per-event local-time persistence policy after in-memory book updates."""
        if record.event_type == "market_resolved":
            return True
        asset_id = record.asset_id
        event_slug = self.asset_events.get(asset_id or "")
        if event_slug is None and isinstance(record.raw.get("assets_ids"), list):
            for candidate in record.raw["assets_ids"]:
                event_slug = self.asset_events.get(str(candidate))
                if event_slug:
                    break
        policy = self.event_policies.get(event_slug or "")
        if policy is None:
            # Existing callers without a settlement policy retain legacy full capture.
            return True
        received = datetime.fromtimestamp(record.received_at_ns / 1_000_000_000, tz=UTC)
        if policy.full_capture(received):
            return True
        if asset_id is None:
            return False
        local = received.astimezone(ZoneInfo(policy.timezone))
        hour_key = local.strftime("%Y-%m-%dT%H")
        key = (event_slug or "", asset_id)
        if self.last_hourly_archive.get(key) == hour_key:
            return False
        state = self.books.get(asset_id)
        if state is None or not state.complete:
            return False
        record.bids, record.asks = state.depth()
        record.raw = {
            **record.raw,
            "archive_policy": "outside_full_window_hourly_snapshot",
            "archive_local_hour": hour_key,
            "archive_timezone": policy.timezone,
        }
        self.last_hourly_archive[key] = hour_key
        self.metrics.hourly_audit_snapshots += 1
        return True

    async def _send_subscription_updates(self, websocket: Any) -> None:
        while True:
            try:
                command = self.subscription_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "operation": command.operation,
                            "assets_ids": list(command.asset_ids),
                        },
                        separators=(",", ":"),
                    )
                )
                if not command.sent.done():
                    command.sent.set_result(None)
            except Exception as exc:
                if not command.sent.done():
                    command.sent.set_exception(exc)
                raise
            finally:
                self.subscription_queue.task_done()

    async def _heartbeat(self, websocket: Any) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            await websocket.send("PING")

    def _records(self, message: dict[str, Any]) -> list[StreamRecord]:
        event_type = str(message.get("event_type") or "unknown")
        source_timestamp = message.get("timestamp")
        source_timestamp_ms = int(source_timestamp) if source_timestamp is not None else None
        market_id = str(message.get("market")) if message.get("market") else None
        received_at_ns = time.time_ns()
        changes = message.get("price_changes") if event_type == "price_change" else None
        if isinstance(changes, list):
            records = []
            for change in changes:
                if not isinstance(change, dict):
                    continue
                asset_id = str(change.get("asset_id") or "") or None
                state = self.books.setdefault(asset_id or "", BookState())
                if change.get("price") is not None and change.get("size") is not None:
                    state.change(
                        side=str(change.get("side") or ""),
                        price=Decimal(str(change["price"])),
                        size=Decimal(str(change["size"])),
                    )
                if change.get("best_bid") is not None:
                    state.best_bid = Decimal(str(change["best_bid"]))
                if change.get("best_ask") is not None:
                    state.best_ask = Decimal(str(change["best_ask"]))
                records.append(
                    self._record(
                        received_at_ns=received_at_ns,
                        source_timestamp_ms=source_timestamp_ms,
                        event_type=event_type,
                        asset_id=asset_id,
                        market_id=market_id,
                        state=state,
                        raw={
                            **{
                                key: value
                                for key, value in message.items()
                                if key != "price_changes"
                            },
                            "price_changes": [change],
                        },
                    )
                )
            return records

        asset_id = str(message.get("asset_id") or "") or None
        state = self.books.setdefault(asset_id or "", BookState())
        raw = message
        if event_type == "book":
            bids = message.get("bids") if isinstance(message.get("bids"), list) else []
            asks = message.get("asks") if isinstance(message.get("asks"), list) else []
            state.replace(bids, asks)
            if asset_id is not None:
                self.book_snapshot_events.setdefault(asset_id, asyncio.Event()).set()
            raw = {
                **{key: value for key, value in message.items() if key not in {"bids", "asks"}},
                "book_depth_stored_top_level": True,
            }
        elif event_type == "best_bid_ask":
            state.best_bid = (
                Decimal(str(message["best_bid"])) if message.get("best_bid") is not None else None
            )
            state.best_ask = (
                Decimal(str(message["best_ask"])) if message.get("best_ask") is not None else None
            )
        elif event_type == "last_trade_price" and message.get("price") is not None:
            state.last_trade_price = Decimal(str(message["price"]))
        return [
            self._record(
                received_at_ns=received_at_ns,
                source_timestamp_ms=source_timestamp_ms,
                event_type=event_type,
                asset_id=asset_id,
                market_id=market_id,
                state=state,
                raw=raw,
            )
        ]

    def _record(
        self,
        *,
        received_at_ns: int,
        source_timestamp_ms: int | None,
        event_type: str,
        asset_id: str | None,
        market_id: str | None,
        state: BookState,
        raw: dict[str, Any],
    ) -> StreamRecord:
        self.sequence += 1
        # Persist a full book on authoritative snapshots and every subsequent
        # level delta in ``raw``. Repeating the entire book for each delta makes
        # an 8-city archive grow by tens of GB/day without adding information.
        include_depth = event_type == "book"
        if event_type == "price_change" and asset_id is not None:
            previous = self.last_depth_checkpoint_ns.get(asset_id)
            include_depth = previous is None or (
                received_at_ns - previous >= self.depth_checkpoint_interval_ns
            )
        if include_depth and asset_id is not None:
            self.last_depth_checkpoint_ns[asset_id] = received_at_ns
        bids, asks = state.depth() if include_depth else (None, None)
        return StreamRecord(
            run_id=self.run_id,
            sequence=self.sequence,
            received_at_ns=received_at_ns,
            source_timestamp_ms=source_timestamp_ms,
            event_type=event_type,
            asset_id=asset_id,
            market_id=market_id,
            market_slug=self.asset_slugs.get(asset_id or ""),
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            last_trade_price=state.last_trade_price,
            bids=bids,
            asks=asks,
            book_complete=state.complete,
            upstream_status=self._quality_status(
                self._active_quality_window(
                    datetime.fromtimestamp(received_at_ns / 1_000_000_000, tz=UTC)
                )
            ),
            upstream_incident_id=(
                window.incident_id
                if (
                    window := self._active_quality_window(
                        datetime.fromtimestamp(received_at_ns / 1_000_000_000, tz=UTC)
                    )
                )
                else None
            ),
            raw=raw,
        )

    @staticmethod
    def _quality_status(window: UpstreamQualityWindow | None) -> str:
        if window is None:
            return "normal"
        return (
            "upstream_maintenance"
            if "maintenance" in window.incident_type.casefold()
            else "upstream_incident"
        )

    def _active_quality_window(self, timestamp: datetime) -> UpstreamQualityWindow | None:
        return quality_window_at(self.quality_windows, timestamp)

    async def _upstream_status_monitor(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.status_poll_seconds
                )
            except TimeoutError:
                pass
            if self.stop_event.is_set():
                break
            await self._refresh_upstream_status(persist_database=False)
            self._write_status()

    async def _refresh_upstream_status(self, *, persist_database: bool) -> None:
        try:
            # Reload the deployment-local quality file so an operator can
            # close a recorded outage without restarting the irreplaceable
            # websocket collector.  The global config remains historical
            # overrides only; local outages live under data/runtime.
            self.quality_windows = merge_quality_windows(
                load_quality_windows(self.quality_windows_path),
                load_quality_overrides(),
            )
            snapshot = await self.status_client.fetch()
            self.quality_windows = merge_quality_windows(
                self.quality_windows, snapshot.windows
            )
            self.upstream_snapshot = replace(snapshot, windows=self.quality_windows)
            self.upstream_status_error = None
            persist_quality_windows(self.quality_windows_path, self.quality_windows)
            if persist_database:
                self.sink.warehouse.upsert_market_data_quality_windows(snapshot.windows)
            self._append_upstream_status_history(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Status telemetry must never stop the irreplaceable order-book
            # collector. Keep the last known windows fail-closed.
            self.upstream_status_error = f"{type(exc).__name__}: {exc}"

    def _append_upstream_status_history(self, snapshot: PolymarketStatusSnapshot) -> None:
        active = [window.as_json() for window in snapshot.active_market_data_windows]
        signature = json.dumps(
            {"page_status": snapshot.page_status, "active": active},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature == self._last_upstream_history_signature:
            return
        self._last_upstream_history_signature = signature
        self.status_history_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "checked_at": snapshot.checked_at.isoformat(),
            "page_status": snapshot.page_status,
            "page_message": snapshot.page_message,
            "upstream_maintenance": snapshot.upstream_maintenance,
            "active_market_data_windows": active,
        }
        with self.status_history_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    async def _writer(self) -> None:
        try:
            while not self.stop_event.is_set() or not self.queue.empty():
                batch: list[StreamRecord] = []
                try:
                    first = await asyncio.wait_for(
                        self.queue.get(), timeout=self.flush_interval_seconds
                    )
                    batch.append(first)
                except TimeoutError:
                    self._write_status()
                    continue
                while len(batch) < self.batch_size:
                    try:
                        batch.append(self.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                # Initial snapshots for hundreds of assets are large. Keep filesystem JSON
                # encoding and flushing off the receive loop so WebSocket heartbeats
                # and frames continue to be serviced during bursty writes.
                await asyncio.to_thread(self.sink.write_raw, batch)
                for _ in batch:
                    self.queue.task_done()
                self.metrics.rows_written += len(batch)
                database_batch = [
                    record
                    for record in batch
                    if record.bids is not None
                    or record.event_type in {"best_bid_ask", "last_trade_price"}
                ]
                if database_batch:
                    await self.database_queue.put(database_batch)
                    self.metrics.database_queue_high_water = max(
                        self.metrics.database_queue_high_water,
                        self.database_queue.qsize(),
                    )
                self._write_status()
        except Exception as exc:
            self.metrics.state = "failed"
            self.metrics.last_error = f"raw writer {type(exc).__name__}: {exc}"
            self.stop_event.set()
            self._write_status()
            raise

    async def _database_writer(self) -> None:
        try:
            while not self.stop_event.is_set() or not self.database_queue.empty():
                try:
                    batch = await asyncio.wait_for(
                        self.database_queue.get(), timeout=self.flush_interval_seconds
                    )
                except TimeoutError:
                    continue
                await asyncio.to_thread(self.sink.write_database, batch)
                self.database_queue.task_done()
                self.metrics.database_rows_written += len(batch)
                await self._maybe_maintain_database()
        except Exception as exc:
            self.metrics.state = "failed"
            self.metrics.last_error = f"database writer {type(exc).__name__}: {exc}"
            self.stop_event.set()
            self._write_status()
            raise

    def _maintain_database(self) -> None:
        self.sink.warehouse.connection.execute("CHECKPOINT")
        self.sink.warehouse.connection.execute("VACUUM")

    async def _maybe_maintain_database(self) -> bool:
        if self.raw_collection_recovery:
            return False
        if time.monotonic() - self.last_database_maintenance < 86_400:
            return False
        await asyncio.to_thread(self._maintain_database)
        self.last_database_maintenance = time.monotonic()
        return True

    async def _status_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            self._write_status()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _write_status(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        if time.monotonic() - self.last_disk_status_at >= 60:
            self.disk_status_cache = disk_capacity_status(self.data_dir)
            self.last_disk_status_at = time.monotonic()
        active_window = self._active_quality_window(datetime.now(UTC))
        payload = {
            **asdict(self.metrics),
            "pid": __import__("os").getpid(),
            "asset_count": len(self.asset_slugs),
            "event_count": len(self.event_counts) or 1,
            "events_by_event": self.event_counts,
            "queue_depth": self.queue.qsize(),
            "business_sample": producer_progress_sample(
                run_id=self.run_id, generation=self.run_id, positions=self.sink.progress.positions,
                as_of=datetime.now(UTC), pending_work=not self.queue.empty(),
                completed_checks=self.metrics.pongs, connection_verified=self.metrics.state == "connected"),
            "database_queue_depth": self.database_queue.qsize(),
            "subscription_queue_depth": self.subscription_queue.qsize(),
            "book_snapshot_count": sum(state.complete for state in self.books.values()),
            "archive_full_window_local": "09:00-20:00",
            "archive_policy_event_count": len(self.event_policies),
            "updated_at": datetime.now(UTC).isoformat(),
            "heartbeat": datetime.now(UTC).isoformat(),
            "websocket_url": self.websocket_url,
            "max_message_size_bytes": self.max_message_size_bytes,
            "reconnect_loop_window_seconds": RECONNECT_LOOP_WINDOW_SECONDS,
            "reconnect_loop_threshold": RECONNECT_LOOP_THRESHOLD,
            "reconnect_log_path": str(self.reconnect_log_path.resolve()),
            "upstream_maintenance": active_window is not None,
            "upstream_status": self._quality_status(active_window),
            "upstream_incident_id": active_window.incident_id if active_window else None,
            "upstream_incident_title": active_window.title if active_window else None,
            "upstream_affected_components": (
                list(active_window.affected_components) if active_window else []
            ),
            "upstream_status_checked_at": (
                self.upstream_snapshot.checked_at.isoformat()
                if self.upstream_snapshot is not None
                else None
            ),
            "upstream_status_error": self.upstream_status_error,
            "upstream_status_history_path": str(self.status_history_path.resolve()),
            "upstream_quality_windows_path": str(self.quality_windows_path.resolve()),
            "upstream_status_poll_seconds": self.status_poll_seconds,
            "collection_mode": (
                "raw_market_recovery" if self.raw_collection_recovery else "standard"
            ),
            "startup_attempt_id": self.startup_attempt_id,
            "downstream_start_blocked": self.raw_collection_recovery,
            "read_only": True,
            "database": self.sink.warehouse.database_status(),
            "retention": {
                "state": (
                    "disabled_raw_market_recovery"
                    if self.raw_collection_recovery
                    else "configured"
                ),
                "raw_market_days": None if self.raw_collection_recovery else 30,
                "aggregate_results": "permanent",
                "automatic_checkpoint_vacuum_enabled": not self.raw_collection_recovery,
            },
            **self.disk_status_cache,
        }
        payload["business_sample_previous"] = getattr(self, "_previous_business_sample", None)
        atomic_json_write(self.status_path, payload)
        self._previous_business_sample = payload["business_sample"]
