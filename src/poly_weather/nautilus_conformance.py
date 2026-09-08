"""Isolated, offline NautilusTrader conformance challenger.

This module is deliberately not imported by any resident runtime.  It accepts
only caller-supplied, token-native in-memory fixtures and runs an ephemeral
Nautilus Sandbox for comparison.  It is not a Paper V1 score source, does not
read credentials, and never constructs a live venue client.

The configured ``DefaultFillModel(0, 0)`` is intentional: a book touch must
not become a fill.  On the pinned Nautilus release that conservative setting
also prevents a public-tape queue fill, so those comparisons are reported as
``NAUTILUS_LIMITATION`` rather than treated as a passing score.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from poly_weather.fees import LiquidityRole, trading_fee_usdc
from poly_weather.runtime_safety import atomic_json_write
from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    ShadowOrderEngine,
    ShadowOrderRejected,
    ShadowSide,
    TradeEvent,
)

NAUTILUS_VERSION = "2.0.0rc4"
INITIAL_CASH_USDC = Decimal("200")
LOCAL_ENGINE_VERSION = "shadow-order-engine-queue-aware-v2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_EXECUTION_MODULES = frozenset(
    {"web3", "eth_account", "py_clob_client", "relayer", "ccxt"}
)


class NautilusConformanceError(RuntimeError):
    """A fail-closed conformance boundary violation."""


class NautilusUnavailableError(NautilusConformanceError):
    """The deliberately optional challenger dependency is unavailable."""


class NautilusFixtureRejected(NautilusConformanceError):
    """A fixture cannot be represented without weakening local evidence rules."""


class ConformanceClassification(StrEnum):
    """The required explicit result taxonomy; this module has no pass rate."""

    MATCH = "MATCH"
    EXPECTED_DIFFERENCE = "EXPECTED_DIFFERENCE"
    LOCAL_BUG = "LOCAL_BUG"
    NAUTILUS_LIMITATION = "NAUTILUS_LIMITATION"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerifiedTrade:
    """A public trade that has already passed the local queue-evidence gates."""

    trade: TradeEvent
    receipt_verified: bool
    public_tape_verified: bool
    gap_free: bool


@dataclass(frozen=True, slots=True)
class CanonicalTimelineRow:
    """One local-information event shared by the challenger implementations."""

    kind: str
    event_timestamp: datetime
    available_at: datetime
    sequence: int | None
    identity: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "event_timestamp": self.event_timestamp.astimezone(UTC).isoformat(),
            "available_at": self.available_at.astimezone(UTC).isoformat(),
            "sequence": self.sequence,
            "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class NautilusFixture:
    """Native objects built only after strict local validation succeeds."""

    snapshot: BookSnapshot
    verified_trades: tuple[VerifiedTrade, ...]
    instrument: Any
    book_deltas: tuple[Any, ...]
    trade_ticks: tuple[Any, ...]
    sandbox_config: Any
    fixture_hash: str
    sandbox_config_hash: str
    sandbox_config_payload: Mapping[str, Any]
    timeline: tuple[CanonicalTimelineRow, ...]
    book_frames: tuple[BookSnapshot, ...]
    book_deltas_by_identity: Mapping[str, tuple[Any, ...]]


def forbidden_execution_dependency_scan() -> dict[str, Any]:
    """Assert the challenger did not pull a project-forbidden execution module."""

    loaded = sorted(
        name
        for name in sys.modules
        if name in FORBIDDEN_EXECUTION_MODULES
        or any(name.startswith(f"{prefix}.") for prefix in FORBIDDEN_EXECUTION_MODULES)
    )
    return {
        "status": "clear" if not loaded else "forbidden_modules_loaded",
        "forbidden_modules": loaded,
        "execution_enabled": False,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise NautilusFixtureRejected("fixture time must carry an explicit timezone")
    return value.astimezone(UTC)


def _nanoseconds(value: datetime) -> int:
    """Convert a Python datetime without a float timestamp round-trip."""

    instant = _utc(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _decimal_places(value: Decimal) -> int:
    normalized = value.normalize()
    return max(0, -normalized.as_tuple().exponent)


def _native_float(value: Decimal) -> float:
    if not value.is_finite():
        raise NautilusFixtureRejected("non-finite Decimal cannot enter a native fixture")
    return float(value)


def _snapshot_receipt(snapshot: BookSnapshot) -> datetime:
    """Use an explicit archived receipt when present, otherwise its availability time.

    ``BookSnapshot`` deliberately has one required event/availability clock.
    Calling code may preserve a more specific archive receipt as
    ``metadata.book_received_at``; it cannot be later than the decision
    snapshot and is never invented from a wall clock.
    """

    raw = snapshot.metadata.get("book_received_at")
    if raw is None:
        return snapshot.timestamp
    receipt = _utc(raw)
    if receipt > snapshot.timestamp:
        raise NautilusFixtureRejected("book receipt is later than decision snapshot")
    return receipt


def _validate_snapshot(snapshot: BookSnapshot) -> None:
    if not snapshot.event_id or not snapshot.market_id or not snapshot.token_id:
        raise NautilusFixtureRejected("snapshot must contain event, market, and token identity")
    if not snapshot.health_ok:
        raise NautilusFixtureRejected("unhealthy snapshot cannot be converted to a clean fixture")
    if not snapshot.bids or not snapshot.asks:
        raise NautilusFixtureRejected("complete two-sided native L2 levels are required")
    if snapshot.tick_size <= 0 or snapshot.min_order_size < 0:
        raise NautilusFixtureRejected("invalid tick or minimum size")
    _snapshot_receipt(snapshot)


def validate_verified_trade(snapshot: BookSnapshot, evidence: VerifiedTrade) -> None:
    """Reject missing queue evidence before any Nautilus object is constructed."""

    trade = evidence.trade
    if not evidence.receipt_verified:
        raise NautilusFixtureRejected("trade receipt verification is required")
    if not evidence.public_tape_verified:
        raise NautilusFixtureRejected("public-tape verification is required")
    if not evidence.gap_free:
        raise NautilusFixtureRejected("trade crosses a feed gap")
    if trade.asset_id != snapshot.token_id:
        raise NautilusFixtureRejected("trade token does not match the native snapshot token")
    if not trade.event_id:
        raise NautilusFixtureRejected("trade needs a stable public identity")
    if trade.sequence is None:
        raise NautilusFixtureRejected("unsequenced trade cannot be converted into a clean TradeTick")
    if trade.available_at is None:
        raise NautilusFixtureRejected("trade receipt timestamp is required")
    if trade.available_at < trade.timestamp:
        raise NautilusFixtureRejected("trade receipt predates its exchange event")
    if trade.side not in {ShadowSide.BUY, ShadowSide.SELL}:
        raise NautilusFixtureRejected("trade has no verified aggressor side")


def _verified_trade_identity(trade: TradeEvent) -> str:
    """Keep sibling transaction rows distinct in every challenger trace."""

    return _digest(
        {
            "event_id": trade.event_id,
            "asset_id": trade.asset_id,
            "timestamp": trade.timestamp.isoformat(),
            "available_at": trade.available_at.isoformat() if trade.available_at else None,
            "side": trade.side.value,
            "price": str(trade.price),
            "size": str(trade.size),
            "sequence": trade.sequence,
        }
    )


def _book_identity(snapshot: BookSnapshot) -> str:
    """Return a stable identity for one token-native L2 frame."""

    return _digest(
        {
            "kind": "book",
            "event_timestamp": snapshot.timestamp.isoformat(),
            "available_at": _snapshot_receipt(snapshot).isoformat(),
            "event_id": snapshot.event_id,
            "market_id": snapshot.market_id,
            "token_id": snapshot.token_id,
            "bids": [[str(price), str(size)] for price, size in snapshot.bids],
            "asks": [[str(price), str(size)] for price, size in snapshot.asks],
        }
    )


def _book_frames(
    snapshot: BookSnapshot,
    book_updates: Sequence[BookSnapshot],
) -> tuple[BookSnapshot, ...]:
    """Validate an availability-ordered stream of frames for one token.

    The challenger deliberately receives each actual L2 change as a separate
    frame.  A later book is not inferred from a trade or a complement token.
    """

    _validate_snapshot(snapshot)
    frames = (snapshot, *tuple(book_updates))
    prior_receipt: datetime | None = None
    for frame in frames:
        _validate_snapshot(frame)
        if (
            frame.event_id != snapshot.event_id
            or frame.market_id != snapshot.market_id
            or frame.token_id != snapshot.token_id
            or frame.tick_size != snapshot.tick_size
            or frame.min_order_size != snapshot.min_order_size
        ):
            raise NautilusFixtureRejected("book update crosses the token-native fixture scope")
        receipt = _snapshot_receipt(frame)
        if prior_receipt is not None and receipt < prior_receipt:
            raise NautilusFixtureRejected("book updates must be receipt-ordered")
        prior_receipt = receipt
    return frames


def canonical_timeline(
    snapshot: BookSnapshot,
    verified_trades: Sequence[VerifiedTrade],
    *,
    book_updates: Sequence[BookSnapshot] = (),
) -> tuple[CanonicalTimelineRow, ...]:
    """Build the receipt-ordered information clock used by both challengers."""

    frames = _book_frames(snapshot, book_updates)
    rows = [
        CanonicalTimelineRow(
            kind="book",
            event_timestamp=frame.timestamp,
            available_at=_snapshot_receipt(frame),
            sequence=None,
            identity=_book_identity(frame),
        )
        for frame in frames
    ]
    for evidence in verified_trades:
        validate_verified_trade(snapshot, evidence)
        trade = evidence.trade
        assert trade.available_at is not None  # validated above
        rows.append(
            CanonicalTimelineRow(
                kind="trade",
                event_timestamp=trade.timestamp,
                available_at=trade.available_at,
                sequence=trade.sequence,
                identity=_verified_trade_identity(trade),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.available_at,
                0 if row.kind == "book" else 1,
                row.event_timestamp,
                row.sequence is None,
                row.sequence if row.sequence is not None else 0,
                row.identity,
            ),
        )
    )


def fixture_manifest(
    snapshot: BookSnapshot,
    verified_trades: Sequence[VerifiedTrade],
    *,
    book_updates: Sequence[BookSnapshot] = (),
) -> dict[str, Any]:
    """Return a deterministic, dependency-free manifest for an input fixture."""

    frames = _book_frames(snapshot, book_updates)
    timeline = canonical_timeline(snapshot, verified_trades, book_updates=book_updates)
    normalized_trades: list[dict[str, Any]] = []
    for evidence in verified_trades:
        validate_verified_trade(snapshot, evidence)
        trade = evidence.trade
        normalized_trades.append(
            {
                "event_id": trade.event_id,
                "asset_id": trade.asset_id,
                "side": trade.side.value,
                "price": str(trade.price),
                "size": str(trade.size),
                "timestamp": trade.timestamp.isoformat(),
                "available_at": trade.available_at.isoformat() if trade.available_at else None,
                "sequence": trade.sequence,
                "source": trade.source,
                "receipt_verified": evidence.receipt_verified,
                "public_tape_verified": evidence.public_tape_verified,
                "gap_free": evidence.gap_free,
            }
        )
    normalized_trades.sort(
        key=lambda row: (
            row["available_at"],
            row["timestamp"],
            row["sequence"] is None,
            row["sequence"] if row["sequence"] is not None else 0,
            row["event_id"],
            row["asset_id"],
            row["price"],
            row["size"],
        )
    )
    payload = {
        "schema_version": 1,
        "token_native_only": True,
        "snapshot": {
            "timestamp": snapshot.timestamp.isoformat(),
            "book_received_at": _snapshot_receipt(snapshot).isoformat(),
            "event_id": snapshot.event_id,
            "market_id": snapshot.market_id,
            "token_id": snapshot.token_id,
            "bids": [[str(price), str(size)] for price, size in snapshot.bids],
            "asks": [[str(price), str(size)] for price, size in snapshot.asks],
            "tick_size": str(snapshot.tick_size),
            "min_order_size": str(snapshot.min_order_size),
            "health": {
                "book_complete": snapshot.book_complete,
                "market_stale": snapshot.market_stale,
                "weather_stale": snapshot.weather_stale,
                "settlement_verified": snapshot.settlement_verified,
                "in_season": snapshot.in_season,
                "season_version": snapshot.season_version,
                "warming_valid": snapshot.warming_valid,
                "upstream_status": snapshot.upstream_status,
                "quality_excluded": snapshot.quality_excluded,
            },
        },
        "book_updates": [
            {
                "timestamp": frame.timestamp.isoformat(),
                "book_received_at": _snapshot_receipt(frame).isoformat(),
                "bids": [[str(price), str(size)] for price, size in frame.bids],
                "asks": [[str(price), str(size)] for price, size in frame.asks],
            }
            for frame in frames[1:]
        ],
        "verified_trades": normalized_trades,
        "timeline": [row.as_dict() for row in timeline],
        "execution_enabled": False,
    }
    return {**payload, "fixture_hash": _digest(payload)}


def _sandbox_config_payload() -> dict[str, Any]:
    return {
        "venue": "POLYMARKET",
        "starting_balance_usdc": str(INITIAL_CASH_USDC),
        "account_type": "CASH",
        "oms_type": "NETTING",
        "book_type": "L2_MBP",
        "trade_execution": True,
        "queue_position": True,
        "liquidity_consumption": True,
        "bar_execution": False,
        "fee_model": "PolymarketFeeModel",
        "fill_model": {
            "name": "DefaultFillModel",
            "prob_fill_on_limit": 0.0,
            "prob_slippage": 0.0,
            "random_seed": 0,
            "policy": "never score a book touch as a fill",
        },
        "execution_enabled": False,
    }


def sandbox_config_hash(payload: Mapping[str, Any]) -> str:
    """Hash the normalized venue semantics actually supplied to the engine."""

    return _digest(dict(payload))


def _active_venue_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize exactly the arguments shared by both adapters."""

    required = {
        "venue",
        "starting_balance_usdc",
        "account_type",
        "oms_type",
        "book_type",
        "trade_execution",
        "queue_position",
        "liquidity_consumption",
        "bar_execution",
        "fee_model",
        "fill_model",
        "execution_enabled",
    }
    if set(payload) != required:
        raise NautilusFixtureRejected("sandbox payload must contain exactly the active venue semantics")
    normalized = dict(payload)
    if (
        normalized["venue"] != "POLYMARKET"
        or normalized["starting_balance_usdc"] != str(INITIAL_CASH_USDC)
        or normalized["account_type"] != "CASH"
        or normalized["oms_type"] != "NETTING"
        or normalized["book_type"] != "L2_MBP"
        or normalized["fee_model"] != "PolymarketFeeModel"
        or normalized["execution_enabled"] is not False
    ):
        raise NautilusFixtureRejected("invalid isolated Nautilus sandbox semantics")
    if not all(
        isinstance(normalized[name], bool)
        for name in ("trade_execution", "queue_position", "liquidity_consumption", "bar_execution")
    ):
        raise NautilusFixtureRejected("sandbox behavior flags must be booleans")
    fill_model = normalized["fill_model"]
    if not isinstance(fill_model, Mapping) or set(fill_model) != {
        "name",
        "prob_fill_on_limit",
        "prob_slippage",
        "random_seed",
        "policy",
    }:
        raise NautilusFixtureRejected("sandbox fill model semantics are incomplete")
    if (
        fill_model["name"] != "DefaultFillModel"
        or Decimal(str(fill_model["prob_fill_on_limit"])) != Decimal("0")
        or Decimal(str(fill_model["prob_slippage"])) != Decimal("0")
        or int(fill_model["random_seed"]) != 0
    ):
        raise NautilusFixtureRejected("sandbox touch-fill policy drifted")
    return normalized


def _sandbox_execution_config(native: Mapping[str, Any], payload: Mapping[str, Any]) -> Any:
    """Construct the displayable Sandbox config from canonical semantics."""

    active = _active_venue_config(payload)
    usdc = native["Currency"].from_str("USDC")
    return native["SandboxExecutionClientConfig"](
        venue=native["POLYMARKET_VENUE"],
        starting_balances=[
            native["Money"](_native_float(Decimal(str(active["starting_balance_usdc"]))), usdc)
        ],
        oms_type=native["OmsType"].NETTING,
        account_type=native["AccountType"].CASH,
        book_type=native["BookType"].L2_MBP,
        bar_execution=active["bar_execution"],
        trade_execution=active["trade_execution"],
        queue_position=active["queue_position"],
        liquidity_consumption=active["liquidity_consumption"],
        fee_model=native["PolymarketFeeModel"](),
        fill_model=native["DefaultFillModel"](
            float(active["fill_model"]["prob_fill_on_limit"]),
            float(active["fill_model"]["prob_slippage"]),
            random_seed=int(active["fill_model"]["random_seed"]),
        ),
    )


def _add_sandbox_venue(
    native: Mapping[str, Any], engine: Any, payload: Mapping[str, Any]
) -> None:
    """Use the same canonical payload for the *actual* BacktestEngine call."""

    active = _active_venue_config(payload)
    usdc = native["Currency"].from_str("USDC")
    engine.add_venue(
        native["POLYMARKET_VENUE"],
        native["OmsType"].NETTING,
        native["AccountType"].CASH,
        [native["Money"](_native_float(Decimal(str(active["starting_balance_usdc"]))), usdc)],
        book_type=native["BookType"].L2_MBP,
        bar_execution=active["bar_execution"],
        trade_execution=active["trade_execution"],
        queue_position=active["queue_position"],
        liquidity_consumption=active["liquidity_consumption"],
        fee_model=native["PolymarketFeeModel"](),
        fill_model=native["DefaultFillModel"](
            float(active["fill_model"]["prob_fill_on_limit"]),
            float(active["fill_model"]["prob_slippage"]),
            random_seed=int(active["fill_model"]["random_seed"]),
        ),
    )


def _lazy_nautilus() -> dict[str, Any]:
    """Import only offline Sandbox components, on explicit challenger use."""

    try:
        import nautilus_trader
        from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE, PolymarketFeeModel
        from nautilus_trader.adapters.sandbox import SandboxExecutionClientConfig
        from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.execution import DefaultFillModel
        from nautilus_trader.model import (
            AccountType,
            AggressorSide,
            AssetClass,
            BinaryOption,
            BookAction,
            BookOrder,
            BookType,
            Currency,
            InstrumentId,
            Money,
            OmsType,
            OrderBookDelta,
            OrderSide,
            Price,
            Quantity,
            Symbol,
            TimeInForce,
            TradeId,
            TradeTick,
        )
        from nautilus_trader.trading import Strategy
    except ModuleNotFoundError as exc:
        raise NautilusUnavailableError(
            "Nautilus challenger is optional; install the project with the `nautilus-eval` extra."
        ) from exc
    if getattr(nautilus_trader, "__version__", None) != NAUTILUS_VERSION:
        raise NautilusConformanceError(
            f"expected nautilus-trader=={NAUTILUS_VERSION}, got "
            f"{getattr(nautilus_trader, '__version__', 'unknown')}"
        )
    return {
        "module": nautilus_trader,
        "POLYMARKET_VENUE": POLYMARKET_VENUE,
        "PolymarketFeeModel": PolymarketFeeModel,
        "SandboxExecutionClientConfig": SandboxExecutionClientConfig,
        "BacktestEngine": BacktestEngine,
        "BacktestEngineConfig": BacktestEngineConfig,
        "DefaultFillModel": DefaultFillModel,
        "AccountType": AccountType,
        "AggressorSide": AggressorSide,
        "AssetClass": AssetClass,
        "BinaryOption": BinaryOption,
        "BookAction": BookAction,
        "BookOrder": BookOrder,
        "BookType": BookType,
        "Currency": Currency,
        "InstrumentId": InstrumentId,
        "Money": Money,
        "OmsType": OmsType,
        "OrderBookDelta": OrderBookDelta,
        "OrderSide": OrderSide,
        "Price": Price,
        "Quantity": Quantity,
        "Symbol": Symbol,
        "TimeInForce": TimeInForce,
        "TradeId": TradeId,
        "TradeTick": TradeTick,
        "Strategy": Strategy,
    }


def _book_deltas_for_frames(
    native: Mapping[str, Any],
    instrument_id: Any,
    frames: Sequence[BookSnapshot],
    *,
    price_precision: int,
    size_precision: int,
) -> dict[str, tuple[Any, ...]]:
    """Encode each full L2 frame as native ADD/UPDATE/DELETE evidence.

    A frame after the initial book is a genuine later order-book update.  Its
    levels are not fabricated from a price tick: changed same-level entries
    use UPDATE, new levels ADD, and removed levels DELETE.
    """

    output: dict[str, tuple[Any, ...]] = {}
    previous: dict[str, tuple[tuple[Decimal, Decimal], ...]] = {"bids": (), "asks": ()}
    delta_sequence = 1
    for frame_index, frame in enumerate(frames):
        frame_deltas: list[Any] = []
        event_ns = _nanoseconds(frame.timestamp)
        receipt_ns = _nanoseconds(_snapshot_receipt(frame))
        current = {"bids": frame.bids, "asks": frame.asks}
        for side_name, native_side in (
            ("bids", native["OrderSide"].BUY),
            ("asks", native["OrderSide"].SELL),
        ):
            prior_levels = previous[side_name]
            current_levels = current[side_name]
            # Explicitly remove levels which disappeared before adding a
            # changed book.  This is particularly important for a later ask
            # moving down to a resting maker price in the touch case.
            for level_index in range(len(current_levels), len(prior_levels)):
                price, size = prior_levels[level_index]
                frame_deltas.append(
                    native["OrderBookDelta"](
                        instrument_id,
                        native["BookAction"].DELETE,
                        native["BookOrder"](
                            native_side,
                            native["Price"](_native_float(price), price_precision),
                            native["Quantity"](_native_float(size), size_precision),
                            (1 if side_name == "bids" else 10_000) + level_index,
                        ),
                        0,
                        delta_sequence,
                        event_ns,
                        receipt_ns,
                    )
                )
                delta_sequence += 1
            for level_index, (price, size) in enumerate(current_levels):
                old = prior_levels[level_index] if level_index < len(prior_levels) else None
                if old == (price, size):
                    continue
                action = (
                    native["BookAction"].ADD
                    if frame_index == 0 or old is None
                    else native["BookAction"].UPDATE
                )
                frame_deltas.append(
                    native["OrderBookDelta"](
                        instrument_id,
                        action,
                        native["BookOrder"](
                            native_side,
                            native["Price"](_native_float(price), price_precision),
                            native["Quantity"](_native_float(size), size_precision),
                            (1 if side_name == "bids" else 10_000) + level_index,
                        ),
                        0,
                        delta_sequence,
                        event_ns,
                        receipt_ns,
                    )
                )
                delta_sequence += 1
        output[_book_identity(frame)] = tuple(frame_deltas)
        previous = current
    return output


def adapt_nautilus_fixture(
    snapshot: BookSnapshot,
    verified_trades: Sequence[VerifiedTrade],
    *,
    book_updates: Sequence[BookSnapshot] = (),
) -> NautilusFixture:
    """Build a strictly token-native in-memory Nautilus fixture.

    This is the sole conversion boundary.  It neither discovers markets nor
    fetches data; callers must provide verified local evidence.
    """

    frames = _book_frames(snapshot, book_updates)
    manifest = fixture_manifest(snapshot, verified_trades, book_updates=book_updates)
    timeline = canonical_timeline(snapshot, verified_trades, book_updates=book_updates)
    native = _lazy_nautilus()
    scan = forbidden_execution_dependency_scan()
    if scan["status"] != "clear":
        raise NautilusConformanceError("forbidden execution dependency was already loaded")

    max_size_precision = max(
        [_decimal_places(snapshot.min_order_size)]
        + [
            _decimal_places(size)
            for frame in frames
            for _price, size in (*frame.bids, *frame.asks)
        ]
        + [_decimal_places(evidence.trade.size) for evidence in verified_trades]
    )
    price_precision = _decimal_places(snapshot.tick_size)
    if price_precision > 16 or max_size_precision > 16:
        raise NautilusFixtureRejected("native fixture precision exceeds the configured fail-closed limit")

    Symbol = native["Symbol"]
    instrument_id = native["InstrumentId"](
        Symbol(snapshot.token_id), native["POLYMARKET_VENUE"]
    )
    snapshot_event_ns = _nanoseconds(snapshot.timestamp)
    snapshot_receipt_ns = _nanoseconds(_snapshot_receipt(snapshot))
    trade_event_times = [_nanoseconds(row.trade.timestamp) for row in verified_trades]
    activation_ns = max(0, min([snapshot_event_ns, *trade_event_times]) - 1)
    expiration_ns = max([snapshot_event_ns, *trade_event_times]) + int(
        timedelta(days=1).total_seconds() * 1_000_000_000
    )
    min_size = snapshot.min_order_size if snapshot.min_order_size > 0 else Decimal("1")
    instrument = native["BinaryOption"](
        instrument_id,
        Symbol(snapshot.token_id),
        native["AssetClass"].ALTERNATIVE,
        native["Currency"].from_str("USDC"),
        activation_ns,
        expiration_ns,
        price_precision,
        max_size_precision,
        native["Price"](_native_float(snapshot.tick_size), price_precision),
        native["Quantity"](_native_float(min_size), max_size_precision),
        snapshot_event_ns,
        snapshot_receipt_ns,
        outcome=str(snapshot.metadata.get("outcome") or "TOKEN_NATIVE"),
        maker_fee=0.0,
        taker_fee=0.05,
    )

    deltas_by_identity = _book_deltas_for_frames(
        native,
        instrument_id,
        frames,
        price_precision=price_precision,
        size_precision=max_size_precision,
    )
    deltas = [
        delta
        for frame in frames
        for delta in deltas_by_identity[_book_identity(frame)]
    ]

    ticks: list[Any] = []
    for evidence in sorted(
        verified_trades,
        key=lambda row: (
            row.trade.available_at,
            row.trade.timestamp,
            row.trade.sequence is None,
            row.trade.sequence if row.trade.sequence is not None else 0,
            _verified_trade_identity(row.trade),
        ),
    ):
        trade = evidence.trade
        trade_key = _verified_trade_identity(trade)[:24]
        ticks.append(
            native["TradeTick"](
                instrument_id,
                native["Price"](_native_float(trade.price), price_precision),
                native["Quantity"](_native_float(trade.size), max_size_precision),
                (
                    native["AggressorSide"].BUY
                    if trade.side is ShadowSide.BUY
                    else native["AggressorSide"].SELL
                ),
                native["TradeId"](f"T-{trade_key}"),
                _nanoseconds(trade.timestamp),
                _nanoseconds(trade.available_at),
            )
        )

    sandbox_payload = _sandbox_config_payload()
    sandbox_config = _sandbox_execution_config(native, sandbox_payload)
    return NautilusFixture(
        snapshot=snapshot,
        verified_trades=tuple(verified_trades),
        instrument=instrument,
        book_deltas=tuple(deltas),
        trade_ticks=tuple(ticks),
        sandbox_config=sandbox_config,
        fixture_hash=str(manifest["fixture_hash"]),
        sandbox_config_hash=sandbox_config_hash(sandbox_payload),
        sandbox_config_payload=sandbox_payload,
        timeline=timeline,
        book_frames=frames,
        book_deltas_by_identity=deltas_by_identity,
    )


def _decimal_text(value: Any) -> str:
    return str(Decimal(str(value)))


def _run_local_shadow(
    snapshot: BookSnapshot,
    verified_trades: Sequence[VerifiedTrade],
    *,
    shares: Decimal,
    book_updates: Sequence[BookSnapshot] = (),
    fill_model: FillModel = FillModel.QUEUE_AWARE,
) -> dict[str, Any]:
    """Replay a single strictly token-scoped maker order in the local engine."""

    frames = _book_frames(snapshot, book_updates)
    timeline = canonical_timeline(snapshot, verified_trades, book_updates=book_updates)
    by_identity = {
        _verified_trade_identity(evidence.trade): evidence.trade
        for evidence in verified_trades
    }
    frames_by_identity = {_book_identity(frame): frame for frame in frames}
    engine = ShadowOrderEngine(
        budget_usd=INITIAL_CASH_USDC,
        max_active_orders=1,
        fill_model=fill_model,
        order_timeout=timedelta(minutes=15),
        execution_enabled=False,
    )
    fills: list[Any] = []
    observed_trace: list[dict[str, Any]] = []
    order = None
    for row in timeline:
        if row.kind == "book":
            frame = frames_by_identity[row.identity]
            try:
                if order is None:
                    order = engine.submit_limit(
                        frame,
                        side=ShadowSide.BUY,
                        limit_price=frame.best_bid or Decimal("0"),
                        shares=shares,
                        idempotency_key="nautilus-conformance:single-maker-buy",
                        strategy_version="nautilus-conformance-only",
                        season_version=frame.season_version,
                        trigger_reason="offline_conformance",
                        maker_assumption=True,
                    )
                    observed_trace.append(
                        {
                            "kind": "book",
                            "identity": row.identity,
                            "event_timestamp": row.event_timestamp.isoformat(),
                            "available_at": row.available_at.isoformat(),
                            "action": "maker_order_submitted",
                        }
                    )
                else:
                    snapshot_fills = engine.process_snapshot(frame)
                    fills.extend(snapshot_fills)
                    observed_trace.append(
                        {
                            "kind": "book",
                            "identity": row.identity,
                            "event_timestamp": row.event_timestamp.isoformat(),
                            "available_at": row.available_at.isoformat(),
                            "action": "book_update_processed",
                            "fills": len(snapshot_fills),
                        }
                    )
            except ShadowOrderRejected as exc:
                return {
                    "order_state": "rejected",
                    "rejection_reason": exc.reason.value,
                    "remaining_shares": "N/A",
                    "queue_ahead": "N/A",
                    "fills": [],
                    "fees_usdc": "0",
                    "cash_usdc": "N/A (ShadowOrderEngine has no account layer)",
                    "reserved_cash_usdc": "0",
                    "positions": "0",
                    "realized_pnl_usdc": "0",
                    "terminal_reason": exc.reason.value,
                    "event_timestamp": row.event_timestamp.isoformat(),
                    "decision_available_at": row.available_at.isoformat(),
                    "timeline": [timeline_row.as_dict() for timeline_row in timeline],
                    "observed_trace": observed_trace,
                }
            continue
        # Receipt ordering governs when this local challenger receives a
        # verified tape event; the engine itself still refuses a trade whose
        # exchange timestamp predates the resting order.
        if order is None:
            observed_trace.append(
                {
                    "kind": "trade",
                    "identity": row.identity,
                    "event_timestamp": row.event_timestamp.isoformat(),
                    "available_at": row.available_at.isoformat(),
                    "action": "ignored_before_resting_book",
                }
            )
            continue
        trade_fills = engine.process_trade(by_identity[row.identity])
        fills.extend(trade_fills)
        observed_trace.append(
            {
                "kind": "trade",
                "identity": row.identity,
                "event_timestamp": row.event_timestamp.isoformat(),
                "available_at": row.available_at.isoformat(),
                "action": "verified_trade_processed",
                "fills": len(trade_fills),
            }
        )
    if order is None:  # pragma: no cover - canonical fixtures always include a book
        raise NautilusFixtureRejected("canonical fixture contains no book frame")
    return {
        "order_state": order.state.value,
        "rejection_reason": None,
        "remaining_shares": str(order.remaining_shares),
        "queue_ahead": str(order.better_level_shares + order.volume_ahead),
        "fills": [
            {
                "shares": str(fill.shares),
                "price": str(fill.price),
                "fee_usdc": str(fill.fee_usd),
                "timestamp": fill.timestamp.isoformat(),
                "source": fill.source,
            }
            for fill in fills
        ],
        "fees_usdc": str(engine.fees_usd),
        "cash_usdc": "N/A (ShadowOrderEngine has no account layer)",
        "reserved_cash_usdc": str(engine.active_reserved_usd),
        "positions": str(engine.inventory_shares),
        "realized_pnl_usdc": str(engine.realized_pnl_usd),
        "terminal_reason": order.cancel_reason,
        "event_timestamp": (
            fills[-1].timestamp.isoformat() if fills else snapshot.timestamp.isoformat()
        ),
        "decision_available_at": (
            timeline[-1].available_at.isoformat() if timeline else _snapshot_receipt(snapshot).isoformat()
        ),
        "timeline": [row.as_dict() for row in timeline],
        "observed_trace": observed_trace,
        "fill_model": fill_model.value,
    }


def _run_nautilus_sandbox(fixture: NautilusFixture, *, shares: Decimal) -> dict[str, Any]:
    """Run a one-order, in-memory-only Sandbox probe with touch fills disabled."""

    native = _lazy_nautilus()
    if shares <= 0:
        raise ValueError("shares must be positive")
    precision = max(
        _decimal_places(fixture.snapshot.min_order_size),
        *[_decimal_places(size) for _price, size in fixture.snapshot.bids],
        *[_decimal_places(size) for _price, size in fixture.snapshot.asks],
    )
    price_precision = _decimal_places(fixture.snapshot.tick_size)

    class _ConformanceProbe(native["Strategy"]):
        def __init__(self) -> None:
            super().__init__()
            self.submitted = False
            self.accepted = False
            self.denied = False
            self.event_types: list[str] = []
            self.fills: list[dict[str, Any]] = []
            self.observed_trace: list[dict[str, Any]] = []

        def on_start(self) -> None:
            self.subscribe_book_deltas(fixture.instrument.id, native["BookType"].L2_MBP)

        def on_book_deltas(self, deltas: Any) -> None:
            self.observed_trace.append(
                {
                    "kind": "book_callback",
                    "event_timestamp_ns": int(getattr(deltas, "ts_event", 0)),
                    "available_at_ns": int(getattr(deltas, "ts_init", 0)),
                    "callback_type": type(deltas).__name__,
                }
            )
            if self.submitted:
                return
            self.submitted = True
            order = self.order_factory.limit(
                fixture.instrument.id,
                native["OrderSide"].BUY,
                native["Quantity"](_native_float(shares), precision),
                native["Price"](
                    _native_float(fixture.snapshot.best_bid or Decimal("0")), price_precision
                ),
                time_in_force=native["TimeInForce"].GTC,
                post_only=True,
            )
            self.submit_order(order)

        def on_order_event(self, event: Any) -> None:
            self.event_types.append(type(event).__name__)

        def on_order_accepted(self, _event: Any) -> None:
            self.accepted = True
            self.observed_trace.append(
                {
                    "kind": "order_accepted",
                    "event_timestamp_ns": int(getattr(_event, "ts_event", 0)),
                    "available_at_ns": int(getattr(_event, "ts_init", 0)),
                }
            )

        def on_order_denied(self, _event: Any) -> None:
            self.denied = True

        def on_order_rejected(self, _event: Any) -> None:
            self.denied = True

        def on_order_filled(self, event: Any) -> None:
            self.fills.append(
                {
                    "shares": _decimal_text(event.last_qty),
                    "price": _decimal_text(event.last_px),
                    "fee_usdc": str(event.commission),
                    "timestamp_ns": int(event.ts_event),
                    "liquidity_side": str(event.liquidity_side),
                }
            )

        def on_trade_tick(self, tick: Any) -> None:
            self.observed_trace.append(
                {
                    "kind": "trade_tick",
                    "event_timestamp_ns": int(getattr(tick, "ts_event", 0)),
                    "available_at_ns": int(getattr(tick, "ts_init", 0)),
                    "trade_id": str(getattr(tick, "trade_id", "")),
                }
            )

    engine = native["BacktestEngine"](native["BacktestEngineConfig"](bypass_logging=True))
    probe = _ConformanceProbe()
    try:
        _add_sandbox_venue(native, engine, fixture.sandbox_config_payload)
        engine.add_instrument(fixture.instrument)
        engine.add_strategy(probe)
        ordered_evidence = sorted(
            fixture.verified_trades,
            key=lambda row: (
                row.trade.available_at,
                row.trade.timestamp,
                row.trade.sequence is None,
                row.trade.sequence if row.trade.sequence is not None else 0,
                _verified_trade_identity(row.trade),
            ),
        )
        ticks_by_identity = {
            _verified_trade_identity(evidence.trade): tick
            for evidence, tick in zip(ordered_evidence, fixture.trade_ticks, strict=True)
        }
        timeline_data: list[Any] = []
        for row in fixture.timeline:
            if row.kind == "book":
                timeline_data.extend(fixture.book_deltas_by_identity[row.identity])
            else:
                timeline_data.append(ticks_by_identity[row.identity])
        engine.add_data(timeline_data)
        engine.run()
        result = engine.get_result()
    finally:
        engine.dispose()

    filled = sum((Decimal(row["shares"]) for row in probe.fills), start=Decimal("0"))
    if probe.denied:
        order_state = "rejected"
        remaining = "N/A"
    elif filled >= shares:
        order_state = "filled"
        remaining = "0"
    elif filled > 0:
        order_state = "partially_filled"
        remaining = str(shares - filled)
    elif probe.accepted:
        order_state = "resting"
        remaining = str(shares)
    else:
        order_state = "unknown"
        remaining = "N/A"
    return {
        "order_state": order_state,
        "remaining_shares": remaining,
        "queue_ahead": "N/A (Sandbox callback does not expose queue-ahead evidence)",
        "fills": probe.fills,
        "fees_usdc": "N/A (only callback fill commissions are available without report extras)",
        "cash_usdc": "N/A (not inferred from logs or a report dependency)",
        "reserved_cash_usdc": "N/A (not inferred from logs or a report dependency)",
        "positions": str(result.total_positions),
        "position_shares": str(filled),
        "realized_pnl_usdc": "N/A",
        "terminal_reason": "N/A",
        "event_timestamp": (
            str(probe.fills[-1]["timestamp_ns"])
            if probe.fills
            else str(_nanoseconds(fixture.snapshot.timestamp))
        ),
        "event_types": tuple(probe.event_types),
        "iterations": int(result.iterations),
        "orders": int(result.total_orders),
        "touch_fill_policy": "DefaultFillModel(0, 0); book touch is never a scored fill",
        "timeline": [row.as_dict() for row in fixture.timeline],
        "observed_trace": probe.observed_trace,
        "contains_later_book_update": len(fixture.book_frames) > 1,
        "active_venue_config": dict(fixture.sandbox_config_payload),
        "active_venue_config_hash": sandbox_config_hash(fixture.sandbox_config_payload),
        "execution_enabled": False,
    }


def _matrix_row(
    case: str,
    classification: ConformanceClassification,
    reason: str,
    *,
    local: Mapping[str, Any] | str = "N/A",
    nautilus: Mapping[str, Any] | str = "N/A",
    inputs: Mapping[str, Any] | str = "N/A",
    compared_fields: Sequence[str] = (),
    assertions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case": case,
        "inputs": inputs,
        "local_trace": local,
        "native_trace": nautilus,
        "compared_fields": list(compared_fields),
        "classification": classification.value,
        "reason": reason,
        "assertions": dict(assertions or {}),
        # Retain these aliases for existing readers, but reviewers should use
        # the explicitly named trace fields above.
        "rationale": reason,
        "local": local,
        "nautilus": nautilus,
    }


def _unsupported_without_native_trace(native_trace: Mapping[str, Any]) -> ConformanceClassification:
    """Classify only an explicitly recorded unavailable native capability.

    A case name is never enough to call something unsupported. The caller has
    to include a normalized capability trace showing that this narrow offline
    Sandbox probe did not construct that lifecycle.
    """

    return (
        ConformanceClassification.UNSUPPORTED
        if native_trace.get("native_trace_available") is False
        else ConformanceClassification.UNKNOWN
    )


def _unsequenced_evidence(snapshot: BookSnapshot, base: TradeEvent) -> dict[str, Any]:
    first = replace(base, event_id="unsequenced-a", sequence=None)
    second = replace(base, event_id="unsequenced-b", sequence=None)
    evidence = (
        VerifiedTrade(first, True, True, True),
        VerifiedTrade(second, True, True, True),
    )
    try:
        local = _run_local_shadow(snapshot, evidence, shares=Decimal("10"))
    except NautilusFixtureRejected as exc:
        local: Mapping[str, Any] = {
            "input": "rejected",
            "reason": str(exc),
            "evidence_state": "UNKNOWN_TRADE_SEQUENCE",
        }
    try:
        adapt_nautilus_fixture(snapshot, evidence)
    except NautilusFixtureRejected as exc:
        native: Mapping[str, Any] = {"input": "rejected", "reason": str(exc)}
    else:  # pragma: no cover - the fail-closed validation above is intentional
        native = {"input": "unexpectedly accepted"}
    return {
        "local": {**local, "evidence_state": "UNKNOWN_TRADE_SEQUENCE"},
        "nautilus": native,
    }


def _touch_book_update(snapshot: BookSnapshot) -> BookSnapshot:
    """Construct one later native ask touch without inventing a trade."""

    if snapshot.best_bid is None or not snapshot.asks:
        raise NautilusFixtureRejected("touch case requires an initial native bid and ask")
    received_at = _snapshot_receipt(snapshot) + timedelta(seconds=2)
    event_at = max(snapshot.timestamp + timedelta(seconds=2), received_at)
    metadata = dict(snapshot.metadata)
    metadata["book_received_at"] = received_at.isoformat()
    metadata["conformance_touch"] = True
    return replace(
        snapshot,
        timestamp=event_at,
        asks=((snapshot.best_bid, snapshot.asks[0][1]),),
        metadata=metadata,
    )


def _touch_case(
    snapshot: BookSnapshot, *, shares: Decimal
) -> dict[str, Any]:
    """Run the no-trade book-touch fixture through both concrete paths."""

    later_book = _touch_book_update(snapshot)
    local = _run_local_shadow(
        snapshot, (), shares=shares, book_updates=(later_book,)
    )
    touch_counterfactual = _run_local_shadow(
        snapshot,
        (),
        shares=shares,
        book_updates=(later_book,),
        fill_model=FillModel.TOUCH,
    )
    fixture = adapt_nautilus_fixture(snapshot, (), book_updates=(later_book,))
    native = _run_nautilus_sandbox(fixture, shares=shares)
    return {
        "input": {
            "initial_best_ask": str(snapshot.best_ask),
            "maker_limit": str(snapshot.best_bid),
            "later_best_ask": str(later_book.best_ask),
            "public_trade_count": 0,
            "timeline": [row.as_dict() for row in fixture.timeline],
        },
        "local": local,
        "local_touch_counterfactual": touch_counterfactual,
        "nautilus": native,
    }


def _reversed_same_second_evidence(
    snapshot: BookSnapshot, base: TradeEvent, *, shares: Decimal
) -> dict[str, Any]:
    """Make receipt order deliberately disagree with exchange sequence.

    rc4 `TradeTick` retains event and receipt timestamps but has no exchange
    sequence field.  The result is therefore a limitation evidence record,
    not a manufactured MATCH based on Python list ordering.
    """

    if base.available_at is None:
        raise NautilusFixtureRejected("sequence limitation fixture needs receipt time")
    first = replace(
        base,
        event_id=f"{base.event_id}-sequence-2-received-first",
        sequence=2,
        available_at=base.available_at,
    )
    second = replace(
        base,
        event_id=f"{base.event_id}-sequence-1-received-second",
        sequence=1,
        available_at=base.available_at + timedelta(seconds=1),
    )
    evidence = (
        VerifiedTrade(first, True, True, True),
        VerifiedTrade(second, True, True, True),
    )
    fixture = adapt_nautilus_fixture(snapshot, evidence)
    local = _run_local_shadow(snapshot, evidence, shares=shares)
    native = _run_nautilus_sandbox(fixture, shares=shares)
    expected_sequences = [
        row.sequence for row in fixture.timeline if row.kind == "trade"
    ]
    native_has_sequence = any(hasattr(tick, "sequence") for tick in fixture.trade_ticks)
    return {
        "input": {
            "canonical_receipt_order_sequences": expected_sequences,
            "exchange_sequence_order": [1, 2],
            "timeline": [row.as_dict() for row in fixture.timeline],
        },
        "local": local,
        "nautilus": native,
        "native_tick_has_sequence": native_has_sequence,
    }


def _native_trade_timeline_matches(
    fixture: NautilusFixture, native_trace: Mapping[str, Any]
) -> bool:
    """Compare native callback timing to the canonical receipt timeline."""

    expected = [
        (_nanoseconds(row.event_timestamp), _nanoseconds(row.available_at))
        for row in fixture.timeline
        if row.kind == "trade"
    ]
    observed = [
        (int(row["event_timestamp_ns"]), int(row["available_at_ns"]))
        for row in native_trace.get("observed_trace", ())
        if isinstance(row, Mapping) and row.get("kind") == "trade_tick"
    ]
    return expected == observed


def run_offline_conformance(
    snapshot: BookSnapshot,
    verified_trades: Sequence[VerifiedTrade],
    *,
    config_hash: str | None = None,
    order_shares: Decimal | str = Decimal("10"),
) -> dict[str, Any]:
    """Run the isolated challenger and return an explicit 18-case matrix.

    The output intentionally has no score, win rate, or promotion path.  It is
    valid only for the supplied fixture and remains outside ``data/`` unless a
    caller explicitly writes it through :func:`write_conformance_result`.
    """

    requested_shares = Decimal(str(order_shares))
    fixture = adapt_nautilus_fixture(snapshot, verified_trades)
    local = _run_local_shadow(snapshot, verified_trades, shares=requested_shares)
    native = _run_nautilus_sandbox(fixture, shares=requested_shares)
    timeline_matches = _native_trade_timeline_matches(fixture, native)
    touch = _touch_case(snapshot, shares=requested_shares)
    over_budget_local = _run_local_shadow(snapshot, verified_trades, shares=Decimal("300"))
    over_budget_native = _run_nautilus_sandbox(fixture, shares=Decimal("300"))
    repeated_native = _run_nautilus_sandbox(fixture, shares=requested_shares)

    first_trade = verified_trades[0].trade if verified_trades else None
    wrong_side: Mapping[str, Any] | str = "N/A (fixture contains no verified trade)"
    wrong_side_native: Mapping[str, Any] | str = "N/A (fixture contains no verified trade)"
    if first_trade is not None:
        opposite = replace(
            first_trade,
            event_id=f"{first_trade.event_id}-wrong-side",
            side=(ShadowSide.BUY if first_trade.side is ShadowSide.SELL else ShadowSide.SELL),
        )
        wrong_side = _run_local_shadow(
            snapshot, (VerifiedTrade(opposite, True, True, True),), shares=requested_shares
        )
        wrong_side_native = _run_nautilus_sandbox(
            adapt_nautilus_fixture(snapshot, (VerifiedTrade(opposite, True, True, True),)),
            shares=requested_shares,
        )

    wrong_token_result: Mapping[str, Any] | str = "N/A (fixture contains no verified trade)"
    if first_trade is not None:
        wrong_token = replace(first_trade, asset_id=f"not-{snapshot.token_id}")
        try:
            validate_verified_trade(snapshot, VerifiedTrade(wrong_token, True, True, True))
        except NautilusFixtureRejected as exc:
            wrong_token_result = {"input": "rejected", "reason": str(exc)}

    stale_result: Mapping[str, Any]
    try:
        fixture_manifest(replace(snapshot, market_stale=True), verified_trades)
    except NautilusFixtureRejected as exc:
        stale_result = {"input": "rejected", "reason": str(exc)}
    else:  # pragma: no cover - fixture validation intentionally rejects stale books
        stale_result = {"input": "unexpectedly accepted"}

    out_of_order_result: Mapping[str, Any] | str = "N/A (fixture contains no verified trade)"
    if first_trade is not None:
        invalid_receipt = replace(
            first_trade,
            event_id=f"{first_trade.event_id}-receipt-before-event",
            available_at=first_trade.timestamp - timedelta(seconds=1),
        )
        try:
            adapt_nautilus_fixture(
                snapshot, (VerifiedTrade(invalid_receipt, True, True, True),)
            )
        except NautilusFixtureRejected as exc:
            out_of_order_result = {"input": "rejected", "reason": str(exc)}
        else:  # pragma: no cover - adapter must reject the actual disorder
            out_of_order_result = {"input": "unexpectedly accepted"}

    unsequenced = (
        _unsequenced_evidence(snapshot, first_trade)
        if first_trade is not None
        else {"local": "N/A", "nautilus": "N/A"}
    )
    sequence_conflict = (
        _reversed_same_second_evidence(snapshot, first_trade, shares=requested_shares)
        if first_trade is not None
        else {"input": "N/A", "local": "N/A", "nautilus": "N/A", "native_tick_has_sequence": False}
    )
    local_taker_fee = trading_fee_usdc(
        requested_shares,
        snapshot.best_bid or Decimal("0"),
        liquidity_role=LiquidityRole.TAKER,
        market_category="weather",
    )
    local_maker_fee = trading_fee_usdc(
        requested_shares,
        snapshot.best_bid or Decimal("0"),
        liquidity_role=LiquidityRole.MAKER,
        market_category="weather",
    )
    post_only_classification = (
        ConformanceClassification.MATCH
        if local["order_state"] != "rejected"
        and native["order_state"] in {"resting", "partially_filled", "filled"}
        and timeline_matches
        else ConformanceClassification.LOCAL_BUG
        if local["order_state"] == "rejected"
        else ConformanceClassification.NAUTILUS_LIMITATION
    )
    touch_native_trace = touch["nautilus"].get("observed_trace", ())
    touch_event_ns = _nanoseconds(_touch_book_update(snapshot).timestamp)
    touch_was_observed = any(
        isinstance(row, Mapping)
        and row.get("kind") == "book_callback"
        and int(row.get("event_timestamp_ns", -1)) == touch_event_ns
        for row in touch_native_trace
    )
    touch_classification = (
        ConformanceClassification.MATCH
        if not touch["local"]["fills"]
        and not touch["nautilus"]["fills"]
        and touch_was_observed
        and bool(touch["nautilus"].get("contains_later_book_update"))
        else ConformanceClassification.LOCAL_BUG
        if touch["local"]["fills"]
        else ConformanceClassification.NAUTILUS_LIMITATION
    )
    wrong_side_classification = (
        ConformanceClassification.MATCH
        if isinstance(wrong_side, Mapping)
        and isinstance(wrong_side_native, Mapping)
        and not wrong_side["fills"]
        and not wrong_side_native["fills"]
        else ConformanceClassification.UNKNOWN
    )
    cash_classification = (
        ConformanceClassification.MATCH
        if over_budget_local["order_state"] == "rejected"
        and over_budget_native["order_state"] == "rejected"
        else ConformanceClassification.LOCAL_BUG
        if over_budget_local["order_state"] != "rejected"
        else ConformanceClassification.NAUTILUS_LIMITATION
    )
    partial_fill_classification = (
        ConformanceClassification.NAUTILUS_LIMITATION
        if bool(local["fills"]) and not native["fills"]
        else ConformanceClassification.MATCH
        if local["fills"] == native["fills"]
        else ConformanceClassification.UNKNOWN
    )
    wrong_token_classification = (
        ConformanceClassification.MATCH
        if isinstance(wrong_token_result, Mapping)
        and wrong_token_result.get("input") == "rejected"
        else ConformanceClassification.UNKNOWN
    )
    unsequenced_classification = (
        ConformanceClassification.MATCH
        if isinstance(unsequenced["local"], Mapping)
        and isinstance(unsequenced["nautilus"], Mapping)
        and unsequenced["local"].get("input") == "rejected"
        and unsequenced["nautilus"].get("input") == "rejected"
        else ConformanceClassification.UNKNOWN
    )
    sequence_classification = (
        ConformanceClassification.NAUTILUS_LIMITATION
        if isinstance(sequence_conflict, Mapping)
        and sequence_conflict.get("native_tick_has_sequence") is False
        and sequence_conflict.get("input", {}).get("canonical_receipt_order_sequences") == [2, 1]
        else ConformanceClassification.UNKNOWN
    )
    stale_classification = (
        ConformanceClassification.MATCH
        if stale_result.get("input") == "rejected"
        and isinstance(out_of_order_result, Mapping)
        and out_of_order_result.get("input") == "rejected"
        else ConformanceClassification.UNKNOWN
    )

    l2_update_classification = (
        ConformanceClassification.NAUTILUS_LIMITATION
        if touch["nautilus"].get("contains_later_book_update")
        and str(touch["nautilus"].get("queue_ahead", "")).startswith("N/A")
        else ConformanceClassification.UNKNOWN
    )
    deterministic_classification = (
        ConformanceClassification.MATCH
        if native == repeated_native
        else ConformanceClassification.UNKNOWN
    )
    displayed_trade_local = {
        "canonical_trade_identities": [
            row.identity for row in fixture.timeline if row.kind == "trade"
        ]
    }
    displayed_trade_native = {
        "queue_consumption": native["queue_ahead"],
        "observed_trace": native["observed_trace"],
        "native_trace_available": bool(native["observed_trace"]),
    }
    displayed_trade_assertions = {
        "local_canonical_identity_count": len(displayed_trade_local["canonical_trade_identities"]),
        "native_consumed_volume_available": not str(
            displayed_trade_native["queue_consumption"]
        ).startswith("N/A"),
        "native_observed_trace_count": len(displayed_trade_native["observed_trace"]),
    }
    displayed_trade_classification = (
        ConformanceClassification.NAUTILUS_LIMITATION
        if displayed_trade_assertions["local_canonical_identity_count"] > 0
        and displayed_trade_assertions["native_observed_trace_count"] > 0
        and not displayed_trade_assertions["native_consumed_volume_available"]
        else ConformanceClassification.UNKNOWN
    )
    fee_native = {
        "active_venue_config": native["active_venue_config"],
        "fills": native["fills"],
        "native_trace_available": bool(native["fills"]),
    }
    fee_assertions = {
        "active_fee_model": fee_native["active_venue_config"].get("fee_model"),
        "native_fill_count": len(fee_native["fills"]),
        "native_commission_trace_available": fee_native["native_trace_available"],
    }
    fee_classification = (
        ConformanceClassification.UNSUPPORTED
        if fee_assertions["active_fee_model"] == "PolymarketFeeModel"
        and not fee_assertions["native_commission_trace_available"]
        else ConformanceClassification.UNKNOWN
    )
    sell_native = {
        "sell_probe": "not implemented",
        "native_trace_available": False,
        "reason": "one-order BUY probe has no inventory-restoring SELL lifecycle",
    }
    timeout_native = {
        "timeout": "not mapped",
        "native_trace_available": False,
        "reason": "Sandbox timeout callback is not mapped to Paper's durable lifecycle ledger",
    }
    close_native = {
        "close_adapter": "not implemented",
        "native_trace_available": False,
        "reason": "no verified supervisor/native-bid close event is synthesized",
    }
    restart_native = {
        "recovery": "ephemeral challenger only",
        "native_trace_available": False,
        "reason": "Sandbox fixture has no durable cursor or transition ledger",
    }
    matrix = [
        _matrix_row(
            "post_only_maker_submission",
            post_only_classification,
            "MATCH requires acceptance and the observed native trade callbacks to preserve the canonical receipt timeline; it is never a fill claim.",
            inputs=fixture_manifest(snapshot, verified_trades),
            local=local,
            nautilus=native,
            compared_fields=("order_state", "timeline", "observed_trace"),
            assertions={"timeline_matches": timeline_matches},
        ),
        _matrix_row(
            "touch_only_does_not_fill",
            touch_classification,
            "A later native ask update reaches the maker limit with zero public trades. Queue-aware local behavior and the zero-touch Sandbox must remain unfilled; the local TOUCH counterfactual proves the fixture is a real touch.",
            inputs=touch["input"],
            local={"queue_aware": touch["local"], "touch_counterfactual": touch["local_touch_counterfactual"]},
            nautilus=touch["nautilus"],
            compared_fields=("later_best_ask", "fills", "observed_trace"),
            assertions={
                "native_later_book_callback_observed": touch_was_observed,
                "queue_aware_fill_count": len(touch["local"]["fills"]),
                "touch_counterfactual_fill_count": len(touch["local_touch_counterfactual"]["fills"]),
                "native_fill_count": len(touch["nautilus"]["fills"]),
            },
        ),
        _matrix_row(
            "same_token_opposite_taker_partial_fill",
            partial_fill_classification,
            "The pinned zero-touch Sandbox may not establish verified-tape queue-fill equivalence. Its limitation is inferred from actual local/native fill traces, not the case name.",
            inputs=fixture_manifest(snapshot, verified_trades),
            local=local,
            nautilus=native,
            compared_fields=("fills", "remaining_shares", "queue_ahead"),
            assertions={
                "local_fill_count": len(local["fills"]),
                "native_fill_count": len(native["fills"]),
                "native_queue_ahead_available": not str(native["queue_ahead"]).startswith("N/A"),
            },
        ),
        _matrix_row(
            "wrong_side_trade_no_fill",
            wrong_side_classification,
            "The opposite aggressor is exercised through both traces and cannot advance the local BUY queue.",
            inputs={"side": "opposite verified trade"},
            local=wrong_side,
            nautilus=wrong_side_native,
            compared_fields=("fills", "order_state"),
            assertions={
                "local_no_fill": isinstance(wrong_side, Mapping) and not wrong_side["fills"],
                "native_no_fill": isinstance(wrong_side_native, Mapping) and not wrong_side_native["fills"],
            },
        ),
        _matrix_row(
            "wrong_token_trade_no_fill",
            wrong_token_classification,
            "A different token is rejected at the shared adapter boundary; no complement or cross-token proxy is constructed.",
            inputs={"asset_id": f"not-{snapshot.token_id}"},
            local=wrong_token_result,
            nautilus=wrong_token_result,
            compared_fields=("input", "reason"),
            assertions={"adapter_rejected": wrong_token_classification is ConformanceClassification.MATCH},
        ),
        _matrix_row(
            "displayed_trade_not_double_consumed",
            displayed_trade_classification,
            "The local trace contains canonical one-use trade identities, while the actual Sandbox trace has no per-order consumed-volume field. It cannot prove this equivalence.",
            inputs=fixture_manifest(snapshot, verified_trades),
            local=displayed_trade_local,
            nautilus=displayed_trade_native,
            compared_fields=("trade_identity", "queue_consumption"),
            assertions=displayed_trade_assertions,
        ),
        _matrix_row(
            "partial_fill_residual",
            partial_fill_classification,
            "Residual comparison is derived from the actual fill and remaining-share trace; no native queue result is inferred when it is unavailable.",
            inputs=fixture_manifest(snapshot, verified_trades),
            local=local,
            nautilus=native,
            compared_fields=("remaining_shares", "fills"),
            assertions={
                "local_remaining": local["remaining_shares"],
                "native_remaining": native["remaining_shares"],
                "native_queue_ahead_available": not str(native["queue_ahead"]).startswith("N/A"),
            },
        ),
        _matrix_row(
            "availability_timeline_and_sequence_limitation",
            sequence_classification,
            "The fixture deliberately receives sequence 2 before sequence 1 at one event timestamp. rc4 TradeTick has no sequence field, so this is a limitation, never MATCH.",
            inputs=sequence_conflict["input"],
            local=sequence_conflict["local"],
            nautilus=sequence_conflict["nautilus"],
            compared_fields=("event_timestamp", "available_at", "sequence"),
            assertions={
                "native_tick_has_sequence": sequence_conflict["native_tick_has_sequence"],
                "base_fixture_timeline_matches": timeline_matches,
            },
        ),
        _matrix_row(
            "unsequenced_same_second_is_unknown",
            unsequenced_classification,
            "Both paths reject unsequenced same-second evidence rather than applying source-file order.",
            inputs={"same_second": True, "sequence": None},
            local=unsequenced["local"],
            nautilus=unsequenced["nautilus"],
            compared_fields=("input", "reason"),
            assertions={"local_unknown": isinstance(unsequenced["local"], Mapping)},
        ),
        _matrix_row(
            "l2_delete_update_queue_advance",
            l2_update_classification,
            "The real later L2 update is supplied, but native callbacks do not expose the queue-ahead/consumed-volume evidence necessary to claim equivalence.",
            inputs=touch["input"],
            local=touch["local"],
            nautilus=touch["nautilus"],
            compared_fields=("observed_trace", "queue_ahead", "fills"),
            assertions={
                "later_l2_update_supplied": touch["nautilus"].get("contains_later_book_update"),
                "native_queue_ahead_available": not str(
                    touch["nautilus"].get("queue_ahead", "N/A")
                ).startswith("N/A"),
            },
        ),
        _matrix_row(
            "polymarket_fee_model",
            fee_classification,
            "The active venue config uses PolymarketFeeModel, but the observed zero-touch run has no native fill commission to compare. No fee equivalence is claimed.",
            inputs=fixture.sandbox_config_payload,
            local={"maker_fee_usdc": str(local_maker_fee), "taker_fee_usdc": str(local_taker_fee)},
            nautilus=fee_native,
            compared_fields=("fee_model", "fills", "commission"),
            assertions=fee_assertions,
        ),
        _matrix_row(
            "cash_account_does_not_overdraft",
            cash_classification,
            "A 300-share BUY is actually submitted to both isolated paths against 200 USDC and both outcome traces are retained.",
            inputs={"order_shares": "300", "starting_balance_usdc": "200"},
            local=over_budget_local,
            nautilus=over_budget_native,
            compared_fields=("order_state", "rejection_reason"),
            assertions={"local_state": over_budget_local["order_state"], "native_state": over_budget_native["order_state"]},
        ),
        _matrix_row(
            "sell_does_not_exceed_inventory",
            _unsupported_without_native_trace(sell_native),
            "The isolated one-order probe has no inventory-restoring SELL lifecycle; this boundary remains Paper-account authority.",
            inputs={"sell_probe": "not constructed"},
            local={"policy": "same-token inventory enforced"},
            nautilus=sell_native,
            compared_fields=("inventory", "sell_submission"),
            assertions={"native_trace_available": sell_native["native_trace_available"]},
        ),
        _matrix_row(
            "gtd_external_lifecycle_timeout",
            _unsupported_without_native_trace(timeout_native),
            "Nautilus Sandbox timeout callbacks are not mapped to the local durable lifecycle/transition ledger.",
            inputs={"timeout": "event-time lifecycle"},
            local={"timeout": "local explicit event-time lifecycle"},
            nautilus=timeout_native,
            compared_fields=("timeout", "durable_transition"),
            assertions={"native_trace_available": timeout_native["native_trace_available"]},
        ),
        _matrix_row(
            "stale_or_out_of_order_market_data",
            stale_classification,
            "A real stale-book input and a real receipt-before-event trade are separately rejected before either can reach native matching.",
            inputs={"stale_book": True, "receipt_before_event": True},
            local={"stale": stale_result, "out_of_order": out_of_order_result},
            nautilus={"stale": stale_result, "out_of_order": out_of_order_result},
            compared_fields=("input", "reason"),
            assertions={"stale_rejected": stale_result.get("input") == "rejected", "out_of_order_rejected": isinstance(out_of_order_result, Mapping) and out_of_order_result.get("input") == "rejected"},
        ),
        _matrix_row(
            "market_close_or_resolution",
            _unsupported_without_native_trace(close_native),
            "No close/resolution event is synthesized: the challenger cannot replace verified supervisor and native-bid Paper evidence.",
            inputs={"close_event": "not synthesized"},
            local={"policy": "local closed evidence plus contemporaneous native bid only"},
            nautilus=close_native,
            compared_fields=("close_event", "native_bid"),
            assertions={"native_trace_available": close_native["native_trace_available"]},
        ),
        _matrix_row(
            "deterministic_fixture_replay",
            deterministic_classification,
            "Two independent Sandbox runs compare normalized traces and active venue semantics with ephemeral engine identifiers excluded.",
            inputs=fixture_manifest(snapshot, verified_trades),
            local={"fixture_hash": fixture.fixture_hash},
            nautilus={"first": native, "second": repeated_native},
            compared_fields=("observed_trace", "active_venue_config_hash", "order_state", "fills"),
            assertions={"equal_normalized_traces": native == repeated_native},
        ),
        _matrix_row(
            "restart_reset_boundary",
            _unsupported_without_native_trace(restart_native),
            "The Sandbox run is ephemeral and does not establish durable queue/account recovery equivalence with the Paper transition ledger.",
            inputs={"restart": "not supported by Sandbox fixture"},
            local={"recovery": "durable Paper transition ledger"},
            nautilus=restart_native,
            compared_fields=("ledger", "cursor", "recovery"),
            assertions={"native_trace_available": restart_native["native_trace_available"]},
        ),
    ]
    scan = forbidden_execution_dependency_scan()
    if scan["status"] != "clear":
        raise NautilusConformanceError("forbidden execution module loaded during challenger run")
    supplied_config_hash = config_hash or _digest(
        {
            "name": "isolated-nautilus-conformance",
            "execution_enabled": False,
            "order_shares": str(requested_shares),
        }
    )
    return {
        "schema_version": 1,
        "official_score": False,
        "challenger_only": True,
        "execution_enabled": False,
        "nautilus_version": NAUTILUS_VERSION,
        "config_hash": supplied_config_hash,
        "local_engine_version": LOCAL_ENGINE_VERSION,
        "local_engine_config_hash": _digest(
            {
                "budget_usdc": str(INITIAL_CASH_USDC),
                "fill_model": FillModel.QUEUE_AWARE.value,
                "execution_enabled": False,
            }
        ),
        "nautilus_sandbox_config_hash": fixture.sandbox_config_hash,
        "active_venue_config": native["active_venue_config"],
        "active_venue_config_hash": native["active_venue_config_hash"],
        "fixture_hash": fixture.fixture_hash,
        "semantic_differences": [
            "DefaultFillModel(0, 0) prevents a touch fill but also suppresses verified-tape queue fills in the pinned Sandbox; native queue-fill equivalence is not established.",
            "Nautilus Sandbox callbacks do not expose the local queue-ahead/consumed-volume evidence model; those fields remain N/A rather than inferred.",
            "No durable restart, market-close, or local Paper account lifecycle is delegated to Nautilus in this challenger.",
            "No Nautilus output is an official Paper score, order, fill, PnL, or statistical sample.",
        ],
        "input_manifest": fixture_manifest(snapshot, verified_trades),
        "sandbox_config": dict(fixture.sandbox_config_payload),
        "dependency_scan": scan,
        "matrix": matrix,
    }


def write_conformance_result(result: Mapping[str, Any], output_path: Path | str) -> dict[str, Any]:
    """Write only an explicit non-``data/`` challenger artifact.

    The caller must choose the destination; there is intentionally no default
    report path.  The project formal data root is rejected before any parent
    directory is created.
    """

    destination = Path(output_path).resolve()
    formal_data = (PROJECT_ROOT / "data").resolve()
    try:
        destination.relative_to(formal_data)
    except ValueError:
        pass
    else:
        raise ValueError("Nautilus conformance output must not be written below the formal data root")
    required = {
        "official_score": False,
        "challenger_only": True,
        "execution_enabled": False,
    }
    for key, expected in required.items():
        if result.get(key) is not expected:
            raise ValueError(f"conformance result must retain {key}={expected!r}")
    return atomic_json_write(destination, dict(result))
