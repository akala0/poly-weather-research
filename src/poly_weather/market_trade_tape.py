"""Read-only extraction of rich ``last_trade_price`` Market WS events.

The stream archive keeps the original websocket payload under ``raw``.  This
loader promotes only fields that are explicitly present there.  Missing side or
size is a hard data-quality failure: a price-only event cannot safely consume
queue volume and is therefore never converted into a shadow trade.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.archive_io import open_jsonl_text
from poly_weather.polymarket_status import (
    UpstreamQualityWindow,
    excluded_market_data_window_overlaps,
    market_record_is_analysis_eligible,
)
from poly_weather.shadow_orders import ShadowSide, TradeEvent
from poly_weather.trade_evidence import parse_trade_timestamp

WS_SIDE_SEMANTICS = (
    "Market WS last_trade_price.side is the documented BUY/SELL trade side; "
    "queue consumption treats it as the aggressor side only after cross-source "
    "validation against canonical taker rows."
)
_LAST_TRADE_EVENT_RE = re.compile(r'"event_type"\s*:\s*"last_trade_price"')


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp(value: Any) -> datetime | None:
    return parse_trade_timestamp(value)


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
    source_timestamp_text: str | None = None
    receipt_timestamp_text: str | None = None
    upstream_incident_id: str | None = None

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
            source_timestamp_text=self.source_timestamp_text,
            receipt_timestamp_text=self.receipt_timestamp_text,
        )


@dataclass(frozen=True, slots=True)
class MarketWsTradeLoadResult:
    trades: tuple[MarketWsTrade, ...]
    skipped: int
    skip_reasons: Mapping[str, int]
    source_file_count: int
    quality_excluded: int
    filtered_asset_count: int = 0
    filtered_time_count: int = 0
    filtered_identity_count: int = 0

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
            "filtered_asset_count": self.filtered_asset_count,
            "filtered_time_count": self.filtered_time_count,
            "filtered_identity_count": self.filtered_identity_count,
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
    if not price.is_finite() or not size.is_finite() or not ZERO < price <= ONE or size <= ZERO:
        raise ValueError("trade price and size must be positive")
    source_timestamp = _timestamp(raw.get("timestamp") or row.get("source_timestamp_ms"))
    received_value = row.get("received_at") or row.get("received_at_ns")
    received_at = (
        _timestamp(Decimal(str(received_value)) / Decimal(1_000_000_000))
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
        upstream_status=str(row.get("upstream_status") or "unknown"),
        upstream_incident_id=row.get("upstream_incident_id"),
        source_timestamp_text=str(raw.get("timestamp") or row.get("source_timestamp_ms")),
        receipt_timestamp_text=str(received_value),
    )


ZERO = Decimal("0")
ONE = Decimal("1")


def load_market_ws_trades(
    paths: Iterable[Path],
    *,
    quality_windows: Sequence[UpstreamQualityWindow] = (),
    exclude_degraded: bool = True,
    require_transaction_hash: bool = False,
    asset_ids: Iterable[str] | None = None,
    start_at: datetime | str | None = None,
    end_at: datetime | str | None = None,
    transaction_hashes: Iterable[str] | None = None,
) -> MarketWsTradeLoadResult:
    """Load rich WS trade events, fail-closed on price-only rows.

    ``asset_ids`` is an optional token-native scope filter.  It is applied
    before parsing so focused shadow replays do not materialise unrelated
    websocket trades from the full raw archive.  ``start_at`` and ``end_at``
    bound the source-time replay window; receipt time must also be no later
    than ``end_at`` so a later-arriving row cannot leak into a strict replay.
    ``transaction_hashes`` optionally keeps only rows with a canonical public
    tape identity; excluded rows are counted as unmatched.
    These filters never substitute one token for another or change the
    archive's ordering semantics.
    """
    trades: dict[tuple[Any, ...], MarketWsTrade] = {}
    skipped = 0
    quality_excluded = 0
    filtered_asset_count = 0
    filtered_time_count = 0
    filtered_identity_count = 0
    reasons: Counter[str] = Counter()
    file_count = 0
    selected_assets = frozenset(str(asset_id) for asset_id in asset_ids) if asset_ids else None
    selected_hashes = (
        frozenset(str(value) for value in transaction_hashes)
        if transaction_hashes is not None
        else None
    )
    start_time = _utc(start_at) if start_at is not None else None
    end_time = _utc(end_at) if end_at is not None else None
    if start_time is not None and end_time is not None and start_time > end_time:
        raise ValueError("start_at must be no later than end_at")
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
                # Raw CLOB archives are mostly book/price-change rows and can
                # be tens of GiB.  Avoid allocating a Python object for every
                # unrelated row: JSON parsing those rows made a focused
                # read-only replay retain a machine-threatening working set.
                # The regex is only a cheap prefilter; the parsed outer event
                # type below remains the authority.
                if not _LAST_TRADE_EVENT_RE.search(line):
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, Mapping) or row.get("event_type") != "last_trade_price":
                        continue
                    if selected_assets is not None:
                        raw = row.get("raw") if isinstance(row.get("raw"), Mapping) else row
                        asset_id = str(raw.get("asset_id") or row.get("asset_id") or "")
                        if asset_id not in selected_assets:
                            filtered_asset_count += 1
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
                if (
                    (start_time is not None and parsed.source_timestamp < start_time)
                    or (end_time is not None and parsed.source_timestamp > end_time)
                    or (end_time is not None and parsed.received_at > end_time)
                ):
                    filtered_time_count += 1
                    continue
                if selected_hashes is not None and (
                    parsed.transaction_hash is None
                    or parsed.transaction_hash not in selected_hashes
                ):
                    filtered_identity_count += 1
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
                        parsed.run_id,
                        parsed.sequence,
                        parsed.received_at,
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
    return MarketWsTradeLoadResult(
        ordered,
        skipped,
        dict(reasons),
        file_count,
        quality_excluded,
        filtered_asset_count,
        filtered_time_count,
        filtered_identity_count,
    )


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
    validation = validate_ws_side_semantics(ws_trades, public_trades)
    ws_hashes = {row.transaction_hash for row in ws_trades if row.transaction_hash}
    for row, result in zip(ws_trades, validation["matches"], strict=True):
        if result["allowed"]:
            output.append({"source": "market_ws", "trade": row, "side_validated": True,
                           "validated_at": result["validated_at"]})
    for row in sorted(public_trades, key=lambda item: (item.timestamp, item.transaction_hash)):
        if row.transaction_hash in ws_hashes:
            continue
        if row.available_at is None or row.available_at < row.timestamp:
            continue
        if (not row.price.is_finite() or not row.size.is_finite()
                or not ZERO < row.price <= ONE or row.size <= ZERO
                or row.side.upper() not in {"BUY", "SELL"}):
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


def match_ws_trade(
    trade: MarketWsTrade, public_trades: Sequence[PublicTrade], *,
    quality_windows: Sequence[UpstreamQualityWindow] = (),
) -> dict[str, Any]:
    """One receipt-safe complete match, never a hash-to-last-row dictionary.

    Transaction-wide conflicts are deliberately quarantined: the current wire
    models have no verified per-fill ID with which to disambiguate siblings.
    """
    candidates = [row for row in public_trades
                  if trade.transaction_hash and row.transaction_hash == trade.transaction_hash]
    base: dict[str, Any] = {"event_id": trade.event_id, "allowed": False,
                            "validated_at": None, "candidate_count": len(candidates)}
    if not candidates:
        return {**base, "reason": "UNKNOWN_NO_PUBLIC_MATCH"}
    if len(candidates) != 1:
        return {**base, "reason": "UNKNOWN_AMBIGUOUS_PUBLIC_SIBLINGS"}
    row = candidates[0]
    if any(not value.is_finite() for value in (trade.price, trade.size, row.price, row.size)):
        return {**base, "reason": "UNKNOWN_INVALID_DECIMAL"}
    if not (ZERO < row.price <= ONE and row.size > ZERO):
        return {**base, "reason": "UNKNOWN_INVALID_DECIMAL"}
    checks = (
        (trade.asset_id == row.asset_id, "TOKEN"),
        (trade.side.value == row.side.upper(), "SIDE"),
        (trade.price == row.price, "PRICE"),
        (trade.size == row.size, "QUANTITY"),
        (trade.source_timestamp == row.timestamp, "TIMESTAMP_OR_PRECISION"),
        (not row.condition_id or not trade.market_id or row.condition_id == trade.market_id, "MARKET"),
    )
    for valid, field in checks:
        if not valid:
            return {**base, "reason": f"UNKNOWN_{field}_CONFLICT"}
    if (row.available_at is None or row.available_at.tzinfo is None
            or trade.received_at.tzinfo is None
            or row.timestamp.tzinfo is None
            or row.available_at < row.timestamp
            or trade.received_at < trade.source_timestamp):
        return {**base, "reason": "UNKNOWN_RECEIPT_QUALITY"}
    if any(not market_record_is_analysis_eligible(
        {"upstream_status": trade.upstream_status}, point, tuple(quality_windows)
    ) for point in (trade.source_timestamp, trade.received_at, row.timestamp, row.available_at)) or excluded_market_data_window_overlaps(
        list(quality_windows), start_at=min(trade.source_timestamp, row.timestamp),
        end_at=max(trade.received_at, row.available_at),
    ) is not None:
        return {**base, "reason": "UNKNOWN_QUALITY_WINDOW"}
    return {**base, "allowed": True, "reason": "MATCHED_COMPLETE",
            "validated_at": max(trade.received_at, row.available_at).astimezone(UTC).isoformat()}


def validate_ws_side_semantics(
    ws_trades: Sequence[MarketWsTrade],
    public_trades: Sequence[PublicTrade],
    *, quality_windows: Sequence[UpstreamQualityWindow] = (),
) -> dict[str, Any]:
    """Cross-check wire ``side`` against canonical taker-only Data API rows.

    The official Market Channel documents the field and its BUY/SELL domain,
    while the public tape is explicitly taker-only in this project.  Only hash
    matches can establish the aggressor interpretation; missing matches are
    reported as unknown rather than treated as proof.
    """
    results = [match_ws_trade(trade, public_trades, quality_windows=quality_windows) for trade in ws_trades]
    # Repeated wire observations with different sequence/receipt are not proof
    # of independent fills. Preserve and reject ambiguity instead of overwriting.
    counts = Counter(row.transaction_hash for row in ws_trades if row.transaction_hash)
    for trade, result in zip(ws_trades, results, strict=True):
        if counts[trade.transaction_hash] > 1:
            result.update(allowed=False, reason="UNKNOWN_AMBIGUOUS_WS_SIBLINGS", validated_at=None)
    matched = sum(result["candidate_count"] > 0 for result in results)
    matching = sum(result["allowed"] for result in results)
    mismatched = [result["event_id"] for result in results
                  if result["candidate_count"] and not result["allowed"]]
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
        # Summary only. Consumers must use the per-observation result.
        "queue_use_allowed": bool(matching),
        "matches": results,
        "conflict_count": len(mismatched),
        "pending_count": len(results) - matching,
        "raw_observation_count": len(ws_trades) + len(public_trades),
        "ambiguous_sibling_observations": sum("SIBLINGS" in result["reason"] for result in results),
        "validated_ws_observations": matching,
    }


def build_shadow_trade_events(
    ws_trades: Sequence[MarketWsTrade],
    public_trades: Sequence[PublicTrade],
    *, quality_windows: Sequence[UpstreamQualityWindow] = (),
) -> tuple[tuple[TradeEvent, ...], dict[str, Any]]:
    """Build queue inputs with WS preference and a fail-closed side gate."""
    validation = validate_ws_side_semantics(ws_trades, public_trades, quality_windows=quality_windows)
    canonical_identities = {
        (trade.transaction_hash, trade.asset_id)
        for trade in public_trades
        if trade.transaction_hash
    }
    events: list[TradeEvent] = []
    for row, result in zip(ws_trades, validation["matches"], strict=True):
        # A documented side is not enough to infer queue aggressor semantics
        # for an unmatched archive row.  Require a canonical taker row with
        # the same transaction hash and token; unmatched WS rows are reported
        # but fail closed and the API row remains available as a supplement.
        event = row.to_trade_event(
            validated_aggressor_side=result["allowed"]
        )
        if event is not None:
            events.append(replace(event, available_at=_utc(result["validated_at"])))
    # A disputed transaction cannot bypass validation through API supplementation.
    observed_ws_hashes = {row.transaction_hash for row in ws_trades if row.transaction_hash}
    for row in public_trades:
        if row.transaction_hash in observed_ws_hashes:
            continue
        if row.available_at is None or row.available_at < row.timestamp:
            continue
        if any(not market_record_is_analysis_eligible(
            {"upstream_status": "normal"}, point, tuple(quality_windows)
        ) for point in (row.timestamp, row.available_at)) or excluded_market_data_window_overlaps(
            list(quality_windows), start_at=row.timestamp, end_at=row.available_at,
        ) is not None:
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
                    source_timestamp_text=row.source_timestamp_text,
                    receipt_timestamp_text=row.receipt_timestamp_text,
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
