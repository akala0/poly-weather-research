"""Read-only extraction of rich ``last_trade_price`` Market WS events.

The stream archive keeps the original websocket payload under ``raw``.  This
loader promotes only fields that are explicitly present there.  Missing side or
size is a hard data-quality failure: a price-only event cannot safely consume
queue volume and is therefore never converted into a shadow trade.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.archive_io import open_jsonl_text
from poly_weather.polymarket_status import (
    UpstreamQualityWindow,
    market_record_is_analysis_eligible,
)
from poly_weather.shadow_orders import ShadowSide, TradeEvent

WS_SIDE_SEMANTICS = (
    "Market WS last_trade_price.side is the documented BUY/SELL trade side; "
    "queue consumption treats it as the aggressor side only after cross-source "
    "validation against canonical taker rows."
)


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        try:
            return _utc(str(value))
        except (TypeError, ValueError):
            return None
    if number > 10**11:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class MarketWsTrade:
    asset_id: str
    market_id: str
    market_slug: str | None
    price: Decimal
    size: Decimal
    side: ShadowSide
    source_timestamp: datetime
    received_at: datetime
    transaction_hash: str | None
    sequence: int | None
    run_id: str | None
    upstream_status: str
    source: str = "market_ws"

    @property
    def event_id(self) -> str:
        if self.transaction_hash:
            return self.transaction_hash
        return ":".join(
            (
                self.asset_id,
                self.market_id,
                self.source_timestamp.isoformat(),
                str(self.price),
                str(self.size),
                str(self.side),
                str(self.run_id or ""),
                str(self.sequence or ""),
            )
        )

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = str(self.side)
        payload["price"] = str(self.price)
        payload["size"] = str(self.size)
        payload["source_timestamp"] = self.source_timestamp.isoformat()
        payload["received_at"] = self.received_at.isoformat()
        payload["event_id"] = self.event_id
        payload["side_semantics"] = WS_SIDE_SEMANTICS
        return payload

    def to_trade_event(self, *, validated_aggressor_side: bool = False) -> TradeEvent | None:
        """Convert to the FSM input only when side semantics were validated."""
        if not validated_aggressor_side:
            return None
        return TradeEvent(
            timestamp=self.source_timestamp,
            available_at=self.received_at,
            asset_id=self.asset_id,
            side=self.side,
            price=self.price,
            size=self.size,
            event_id=self.event_id,
            source=self.source,
            sequence=self.sequence,
        )


@dataclass(frozen=True, slots=True)
class MarketWsTradeLoadResult:
    trades: tuple[MarketWsTrade, ...]
    skipped: int
    skip_reasons: Mapping[str, int]
    source_file_count: int
    quality_excluded: int

    @property
    def side_semantics(self) -> str:
        return WS_SIDE_SEMANTICS

    def as_json(self) -> dict[str, Any]:
        return {
            "trade_count": len(self.trades),
            "skipped_count": self.skipped,
            "skip_reasons": dict(self.skip_reasons),
            "source_file_count": self.source_file_count,
            "quality_excluded": self.quality_excluded,
            "side_semantics": self.side_semantics,
            "trades": [trade.as_json() for trade in self.trades],
        }


def parse_market_ws_trade(
    row: Mapping[str, Any],
    *,
    require_transaction_hash: bool = False,
) -> MarketWsTrade:
    """Parse one rich WS event, raising on any field needed for queue use."""
    if str(row.get("event_type") or "") != "last_trade_price":
        raise ValueError("not a last_trade_price event")
    raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else row
    asset_id = str(raw.get("asset_id") or row.get("asset_id") or "").strip()
    market_id = str(raw.get("market") or row.get("market_id") or "").strip()
    side_value = str(raw.get("side") or row.get("side") or "").upper().strip()
    if not asset_id or not market_id:
        raise ValueError("missing asset or market identity")
    if side_value not in {"BUY", "SELL"}:
        raise ValueError("missing or invalid trade side")
    try:
        price = Decimal(str(raw.get("price") if raw.get("price") is not None else row.get("last_trade_price")))
        size = Decimal(str(raw.get("size") if raw.get("size") is not None else row.get("size")))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("missing or invalid trade price/size") from exc
    if not ZERO < price <= ONE or size <= ZERO:
        raise ValueError("trade price and size must be positive")
    source_timestamp = _timestamp(raw.get("timestamp") or row.get("source_timestamp_ms"))
    received_value = row.get("received_at") or row.get("received_at_ns")
    received_at = (
        _timestamp(received_value / 1_000_000_000)
        if isinstance(received_value, (int, float)) and received_value > 10**14
        else _timestamp(received_value)
    )
    if source_timestamp is None or received_at is None:
        raise ValueError("missing source or receipt timestamp")
    transaction_hash = str(
        raw.get("transaction_hash")
        or raw.get("transactionHash")
        or raw.get("hash")
        or ""
    ).strip() or None
    if require_transaction_hash and transaction_hash is None:
        raise ValueError("missing transaction hash")
    return MarketWsTrade(
        asset_id=asset_id,
        market_id=market_id,
        market_slug=str(row.get("market_slug")) if row.get("market_slug") else None,
        price=price,
        size=size,
        side=ShadowSide(side_value),
        source_timestamp=source_timestamp,
        received_at=received_at,
        transaction_hash=transaction_hash,
        sequence=int(row["sequence"]) if row.get("sequence") is not None else None,
        run_id=str(row.get("run_id")) if row.get("run_id") else None,
        upstream_status=str(row.get("upstream_status") or "normal"),
    )


ZERO = Decimal("0")
ONE = Decimal("1")


def load_market_ws_trades(
    paths: Iterable[Path],
    *,
    quality_windows: Sequence[UpstreamQualityWindow] = (),
    exclude_degraded: bool = True,
    require_transaction_hash: bool = False,
) -> MarketWsTradeLoadResult:
    """Load rich WS trade events, fail-closed on price-only rows."""
    trades: dict[tuple[Any, ...], MarketWsTrade] = {}
    skipped = 0
    quality_excluded = 0
    reasons: Counter[str] = Counter()
    file_count = 0
    for path in paths:
        if not path.exists():
            continue
        file_count += 1
        try:
            handle = open_jsonl_text(path)
        except OSError:
            reasons["unreadable_file"] += 1
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if not isinstance(row, Mapping) or row.get("event_type") != "last_trade_price":
                        continue
                    parsed = parse_market_ws_trade(
                        row, require_transaction_hash=require_transaction_hash
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    skipped += 1
                    message = str(exc)
                    reasons[
                        "missing_side"
                        if "side" in message
                        else "missing_size"
                        if "size" in message
                        else "invalid_trade_event"
                    ] += 1
                    continue
                if exclude_degraded and not market_record_is_analysis_eligible(
                    row, parsed.received_at, tuple(quality_windows)
                ):
                    quality_excluded += 1
                    reasons["upstream_quality_window"] += 1
                    continue
                # A transaction hash plus token/tape fields is the stable
                # identity across reconnects.  A single blockchain
                # transaction may contain more than one token fill, so hash
                # alone would undercount queue consumption.  Without a hash,
                # retain run/sequence: same-second executions can share
                # price/size and cannot be collapsed safely.
                key = (
                    (
                        "tx",
                        parsed.transaction_hash,
                        parsed.asset_id,
                        parsed.source_timestamp,
                        parsed.price,
                        parsed.size,
                        parsed.side,
                    )
                    if parsed.transaction_hash
                    else (
                        "composite",
                        parsed.asset_id,
                        parsed.source_timestamp,
                        parsed.price,
                        parsed.size,
                        parsed.side,
                        parsed.run_id,
                        parsed.sequence,
                    )
                )
                trades.setdefault(key, parsed)
    ordered = tuple(
        sorted(
            trades.values(),
            key=lambda item: (item.source_timestamp, item.received_at, item.sequence or 0),
        )
    )
    return MarketWsTradeLoadResult(ordered, skipped, dict(reasons), file_count, quality_excluded)


def merge_trade_sources(
    ws_trades: Sequence[MarketWsTrade],
    public_trades: Sequence[PublicTrade],
) -> tuple[dict[str, Any], ...]:
    """Prefer WS rows and use Data API rows only where no strong identity exists.

    A transaction hash is the primary join.  Without it, a same-second Data API
    row is deliberately not collapsed into a WS event because its intra-second
    order is unknowable.
    """
    output: list[dict[str, Any]] = []
    ws_identities = {
        (row.transaction_hash, row.asset_id)
        for row in ws_trades
        if row.transaction_hash
    }
    ws_composites = {
        (row.asset_id, row.source_timestamp, row.price, row.size, row.side)
        for row in ws_trades
        if row.transaction_hash is None
    }
    for row in sorted(ws_trades, key=lambda item: (item.source_timestamp, item.sequence or 0)):
        output.append({"source": "market_ws", "trade": row, "side_validated": False})
    for row in sorted(public_trades, key=lambda item: (item.timestamp, item.transaction_hash)):
        if row.transaction_hash and (row.transaction_hash, row.asset_id) in ws_identities:
            continue
        composite = (row.asset_id, row.timestamp, row.price, row.size, ShadowSide(row.side.upper()))
        if composite in ws_composites:
            continue
        output.append({"source": "data_api", "trade": row, "side_validated": True})
    output.sort(
        key=lambda item: (
            item["trade"].source_timestamp
            if isinstance(item["trade"], MarketWsTrade)
            else item["trade"].timestamp,
            item["source"],
        )
    )
    return tuple(output)


def validate_ws_side_semantics(
    ws_trades: Sequence[MarketWsTrade],
    public_trades: Sequence[PublicTrade],
) -> dict[str, Any]:
    """Cross-check wire ``side`` against canonical taker-only Data API rows.

    The official Market Channel documents the field and its BUY/SELL domain,
    while the public tape is explicitly taker-only in this project.  Only hash
    matches can establish the aggressor interpretation; missing matches are
    reported as unknown rather than treated as proof.
    """
    by_identity = {
        (trade.transaction_hash, trade.asset_id): trade
        for trade in public_trades
        if trade.transaction_hash
    }
    matched = 0
    matching = 0
    mismatched: list[str] = []
    for trade in ws_trades:
        identity = (trade.transaction_hash, trade.asset_id)
        if not trade.transaction_hash or identity not in by_identity:
            continue
        matched += 1
        if trade.side.value == by_identity[identity].side.upper():
            matching += 1
        else:
            mismatched.append(trade.event_id)
    status = (
        "validated"
        if matched > 0 and not mismatched
        else "mismatch"
        if mismatched
        else "unknown_no_cross_source_hash_match"
    )
    return {
        "status": status,
        "side_semantics": WS_SIDE_SEMANTICS,
        "ws_trade_count": len(ws_trades),
        "public_trade_count": len(public_trades),
        "hash_match_count": matched,
        "matching_side_count": matching,
        "mismatched_side_count": len(mismatched),
        "mismatched_transaction_hashes": mismatched,
        "queue_use_allowed": status == "validated",
    }


def build_shadow_trade_events(
    ws_trades: Sequence[MarketWsTrade],
    public_trades: Sequence[PublicTrade],
) -> tuple[tuple[TradeEvent, ...], dict[str, Any]]:
    """Build queue inputs with WS preference and a fail-closed side gate."""
    validation = validate_ws_side_semantics(ws_trades, public_trades)
    ws_allowed = bool(validation["queue_use_allowed"])
    canonical_identities = {
        (trade.transaction_hash, trade.asset_id)
        for trade in public_trades
        if trade.transaction_hash
    }
    events: list[TradeEvent] = []
    for row in ws_trades:
        # A documented side is not enough to infer queue aggressor semantics
        # for an unmatched archive row.  Require a canonical taker row with
        # the same transaction hash and token; unmatched WS rows are reported
        # but fail closed and the API row remains available as a supplement.
        event = row.to_trade_event(
            validated_aggressor_side=ws_allowed
            and row.transaction_hash is not None
            and (row.transaction_hash, row.asset_id) in canonical_identities
        )
        if event is not None:
            events.append(event)
    accepted_ws_identities = {
        (row.transaction_hash, row.asset_id)
        for row in ws_trades
        if row.transaction_hash
        and (row.transaction_hash, row.asset_id) in canonical_identities
        and ws_allowed
    }
    for row in public_trades:
        if row.transaction_hash and (row.transaction_hash, row.asset_id) in accepted_ws_identities:
            continue
        try:
            events.append(
                TradeEvent(
                    timestamp=row.timestamp,
                    asset_id=row.asset_id,
                    side=row.side,
                    price=row.price,
                    size=row.size,
                    event_id=row.transaction_hash or None,
                    source="data_api",
                    available_at=row.available_at,
                )
            )
        except (TypeError, ValueError):
            continue
    events.sort(key=lambda row: (row.timestamp, row.sequence is None, row.sequence or 0))
    validation["shadow_trade_event_count"] = len(events)
    validation["ws_shadow_trade_event_count"] = sum(row.source == "market_ws" for row in events)
    validation["data_api_shadow_trade_event_count"] = sum(row.source == "data_api" for row in events)
    validation["unmatched_ws_trade_count"] = sum(
        row.transaction_hash is None
        or (row.transaction_hash, row.asset_id) not in canonical_identities
        for row in ws_trades
    )
    return tuple(events), validation


__all__ = [
    "MarketWsTrade",
    "MarketWsTradeLoadResult",
    "WS_SIDE_SEMANTICS",
    "load_market_ws_trades",
    "merge_trade_sources",
    "parse_market_ws_trade",
    "build_shadow_trade_events",
    "validate_ws_side_semantics",
]
