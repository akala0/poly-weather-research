from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect

from poly_weather.research_store import ResearchWarehouse

DEFAULT_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


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
    raw: dict[str, Any]


@dataclass(slots=True)
class BookState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    last_trade_price: Decimal | None = None

    def replace(self, bids: list[dict[str, Any]], asks: list[dict[str, Any]]) -> None:
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)
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


@dataclass(slots=True)
class StreamMetrics:
    run_id: str
    started_at: str
    state: str = "starting"
    connections: int = 0
    reconnects: int = 0
    messages: int = 0
    events: int = 0
    rows_written: int = 0
    database_rows_written: int = 0
    pongs: int = 0
    parse_errors: int = 0
    queue_high_water: int = 0
    database_queue_high_water: int = 0
    last_event_at: str | None = None
    last_error: str | None = None


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

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()
        self.handles.clear()
        self.warehouse.close()

    def _handle(self, day: str) -> Any:
        handle = self.handles.get(day)
        if handle is None:
            path = self.data_dir / "raw" / "polymarket_clob_websocket" / day / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8", newline="\n", buffering=1024 * 1024)
            self.handles[day] = handle
        return handle

    def write(self, records: list[StreamRecord]) -> None:
        self.write_raw(records)
        self.write_database(records)

    def write_raw(self, records: list[StreamRecord]) -> None:
        if not records:
            return
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
                "raw": record.raw,
            }
            handle = self._handle(received.date().isoformat())
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        for handle in self.handles.values():
            handle.flush()

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
        websocket_url: str = DEFAULT_MARKET_WS_URL,
        heartbeat_seconds: float = 10,
        silence_timeout_seconds: float = 45,
        batch_size: int = 500,
        flush_interval_seconds: float = 0.25,
        queue_size: int = 50_000,
    ) -> None:
        if not asset_slugs:
            raise ValueError("at least one asset id is required")
        self.asset_slugs = asset_slugs
        self.asset_events = asset_events or {}
        self.data_dir = data_dir
        self.websocket_url = websocket_url
        self.heartbeat_seconds = heartbeat_seconds
        self.silence_timeout_seconds = silence_timeout_seconds
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.queue: asyncio.Queue[StreamRecord] = asyncio.Queue(maxsize=queue_size)
        self.database_queue: asyncio.Queue[list[StreamRecord]] = asyncio.Queue(maxsize=200)
        self.stop_event = asyncio.Event()
        self.run_id = str(uuid4())
        self.metrics = StreamMetrics(run_id=self.run_id, started_at=datetime.now(UTC).isoformat())
        self.books = {asset_id: BookState() for asset_id in asset_slugs}
        self.event_counts = {
            event_slug: 0 for event_slug in sorted(set(self.asset_events.values()))
        }
        self.sequence = 0
        self.status_path = data_dir / "runtime" / "polymarket_ws_status.json"
        self.sink = MarketStreamSink(data_dir=data_dir, run_id=self.run_id)

    async def run(self, *, runtime_seconds: float = 0) -> StreamMetrics:
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
                    self.metrics.last_error = f"{type(exc).__name__}: {exc}"
                    self.metrics.reconnects += 1
                    self.metrics.state = "reconnecting"
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
            await writer
            await database_writer
            status_heartbeat.cancel()
            await asyncio.gather(status_heartbeat, return_exceptions=True)
            self.metrics.state = "stopped"
            self._write_status()
            self.sink.warehouse.finish_market_stream_run(
                run_id=self.run_id,
                finished_at=datetime.now(UTC),
                metrics=asdict(self.metrics),
            )
            self.sink.close()
        return self.metrics

    async def _connection(self, *, deadline: float | None) -> None:
        async with connect(
            self.websocket_url,
            ping_interval=None,
            close_timeout=5,
            max_queue=4096,
            compression=None,
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
            self.metrics.state = "connected"
            self.metrics.last_error = None
            self._write_status()
            heartbeat = asyncio.create_task(self._heartbeat(websocket), name="market-stream-heartbeat")
            last_data_at = time.monotonic()
            try:
                while not self.stop_event.is_set():
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
                            await self.queue.put(record)
                            self.metrics.events += 1
                            event_slug = self.asset_events.get(record.asset_id or "")
                            if event_slug is not None:
                                self.event_counts[event_slug] = self.event_counts.get(event_slug, 0) + 1
                            self.metrics.queue_high_water = max(
                                self.metrics.queue_high_water, self.queue.qsize()
                            )
                    self.metrics.last_event_at = datetime.now(UTC).isoformat()
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

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
                        raw=message,
                    )
                )
            return records

        asset_id = str(message.get("asset_id") or "") or None
        state = self.books.setdefault(asset_id or "", BookState())
        if event_type == "book":
            bids = message.get("bids") if isinstance(message.get("bids"), list) else []
            asks = message.get("asks") if isinstance(message.get("asks"), list) else []
            state.replace(bids, asks)
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
                raw=message,
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
            raw=raw,
        )

    async def _writer(self) -> None:
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
            self.sink.write_raw(batch)
            for _ in batch:
                self.queue.task_done()
            self.metrics.rows_written += len(batch)
            await self.database_queue.put(batch)
            self.metrics.database_queue_high_water = max(
                self.metrics.database_queue_high_water, self.database_queue.qsize()
            )
            self._write_status()

    async def _database_writer(self) -> None:
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

    async def _status_heartbeat(self) -> None:
        while not self.stop_event.is_set():
            self._write_status()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _write_status(self) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **asdict(self.metrics),
            "pid": __import__("os").getpid(),
            "asset_count": len(self.asset_slugs),
            "event_count": len(self.event_counts) or 1,
            "events_by_event": self.event_counts,
            "queue_depth": self.queue.qsize(),
            "database_queue_depth": self.database_queue.qsize(),
            "updated_at": datetime.now(UTC).isoformat(),
            "websocket_url": self.websocket_url,
            "read_only": True,
        }
        temporary = self.status_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.status_path)
