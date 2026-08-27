"""Read-only shadow limit orders and conservative fill simulation.

This module intentionally contains no Polymarket execution client.  It models
what a *post-only* order might have done using archived books and public trade
events.  A shadow fill is not an order acknowledgement and must never be read
as an executable result:

* ``touch`` is an optimistic upper bound (a quote touching the limit fills the
  remaining order);
* ``queue_aware`` consumes only the matching taker side after the recorded
  better-price and same-price-ahead quantities;
* ``trade_through`` additionally requires a trade strictly beyond the limit.

In particular, a decrease in an order-book level is never treated as a trade.
The ledger is append-only JSONL and is deliberately separate from signal
retention so that replay and restart tests can be repeated without side effects.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from poly_weather.fees import LiquidityRole, trading_fee_usdc

ZERO = Decimal("0")
ONE = Decimal("1")
EXECUTION_ENABLED = False
SHADOW_LEDGER_SCHEMA_VERSION = 2


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value: Decimal | float | int | str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _flag(value: Any, *, default: bool = False) -> bool:
    """Parse archive booleans without treating the string ``"false"`` as true."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _price_levels(value: Iterable[tuple[Any, Any]] | None) -> tuple[tuple[Decimal, Decimal], ...]:
    if value is None:
        return ()
    output: list[tuple[Decimal, Decimal]] = []
    for row in value:
        if isinstance(row, Mapping):
            price_value = row.get("price")
            size_value = row.get("size")
        else:
            try:
                price_value, size_value = row
            except (TypeError, ValueError):
                continue
        try:
            price = _decimal(price_value)
            size = _decimal(size_value)
        except (TypeError, ValueError):
            continue
        if ZERO < price <= ONE and size > ZERO:
            output.append((price, size))
    return tuple(output)


def _best(levels: Sequence[tuple[Decimal, Decimal]], *, bids: bool) -> Decimal | None:
    if not levels:
        return None
    return (max if bids else min)(price for price, _size in levels)


class ShadowOrderState(StrEnum):
    NEW = "NEW"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ShadowSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FillModel(StrEnum):
    TOUCH = "touch"
    QUEUE_AWARE = "queue_aware"
    TRADE_THROUGH = "trade_through"


class QuoteMode(StrEnum):
    BEST_BID = "best_bid"
    IMPROVE_ONE_TICK = "improve_one_tick"
    LADDER = "ladder"


class ReplenishMode(StrEnum):
    NONE = "none"
    CONFIRMATION = "confirmation"
    CONDITIONAL_DIP = "conditional_dip"


class StationDayBudgetMode(StrEnum):
    """How the station-day risk manager accounts for the fixed $200 cap."""

    CURRENT_OPEN_EXPOSURE = "current_open_exposure"
    CUMULATIVE_BUY_COST = "cumulative_buy_cost"


@dataclass(frozen=True, slots=True, order=True)
class TokenPortfolioKey:
    """The only valid execution-state scope for a shadow token position."""

    event_id: str
    market_id: str
    token_id: str
    market_day: str

    @classmethod
    def from_snapshot(cls, snapshot: BookSnapshot) -> TokenPortfolioKey:
        return cls(
            event_id=snapshot.event_id,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            market_day=snapshot.market_day or snapshot.timestamp.date().isoformat(),
        )

    @classmethod
    def from_order(cls, order: ShadowOrder) -> TokenPortfolioKey:
        return cls(
            event_id=order.event_id,
            market_id=order.market_id,
            token_id=order.token_id,
            market_day=order.market_day,
        )

    @property
    def identifier(self) -> str:
        return "\x1f".join((self.event_id, self.market_id, self.token_id, self.market_day))

    def as_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "market_day": self.market_day,
        }


@dataclass(frozen=True, slots=True)
class InventorySchedule:
    """A finite candidate tranche schedule under the per-day $200 cap."""

    name: str
    tranches_usd: tuple[Decimal, ...]
    total_usd: Decimal
    control: bool = False


CANDIDATE_INVENTORY_SCHEDULES: tuple[InventorySchedule, ...] = (
    InventorySchedule("4x50", (Decimal("50"),) * 4, Decimal("200")),
    InventorySchedule(
        "20+30+50+100",
        (Decimal("20"), Decimal("30"), Decimal("50"), Decimal("100")),
        Decimal("200"),
    ),
    InventorySchedule("2x100", (Decimal("100"), Decimal("100")), Decimal("200")),
    InventorySchedule("single20", (Decimal("20"),), Decimal("20")),
    InventorySchedule("single50", (Decimal("50"),), Decimal("50")),
    InventorySchedule("single100", (Decimal("100"),), Decimal("100")),
    InventorySchedule("single200_control", (Decimal("200"),), Decimal("200"), True),
)


@dataclass(frozen=True, slots=True)
class ShadowExitPolicy:
    """Finite maker-first exits and risk fallbacks for one inventory lot."""

    target_rises: tuple[Decimal, ...] = (
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.13"),
        Decimal("0.20"),
    )
    partial_first_fraction: Decimal = Decimal("0.50")
    protection_rise: Decimal | None = None
    max_hold: timedelta | None = timedelta(hours=4)
    force_local_eod: bool = True

    def validate(self) -> None:
        if not self.target_rises or any(value <= ZERO for value in self.target_rises):
            raise ValueError("target_rises must be positive")
        if not ZERO < self.partial_first_fraction <= ONE:
            raise ValueError("partial_first_fraction must be in (0, 1]")
        if self.max_hold is not None and self.max_hold <= timedelta(0):
            raise ValueError("max_hold must be positive")


class ShadowOrderReason(StrEnum):
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL_FILL = "partial_fill"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REQUOTED = "requote"
    TIMEOUT = "timeout"
    MAINTENANCE = "upstream_maintenance"
    RECOVERY = "upstream_recovery"
    STALE_MARKET = "stale_market"
    STALE_WEATHER = "stale_weather"
    INCOMPLETE_BOOK = "incomplete_book"
    SETTLEMENT_UNVERIFIED = "settlement_unverified"
    OUT_OF_SEASON = "out_of_season"
    INVALID_SEASON_VERSION = "invalid_season_version"
    WEATHER_WORSENING = "weather_worsening"
    BUDGET = "budget_exceeded"
    ACTIVE_LIMIT = "active_order_limit"
    POST_ONLY = "post_only_would_cross"
    INVALID_TICK = "invalid_tick"
    MIN_ORDER_SIZE = "below_min_order_size"
    DUPLICATE = "duplicate_idempotency_key"
    INVENTORY = "insufficient_inventory"


class ShadowOrderRejected(ValueError):
    """Raised when fail-closed shadow-order validation rejects a new order."""

    def __init__(self, reason: ShadowOrderReason, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason.value)


class ShadowPortfolioInvariantError(RuntimeError):
    """Fail-closed signal that a token portfolio boundary was crossed."""

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """A true two-sided quote used by the shadow engine.

    ``bids`` and ``asks`` are token-native levels (price, shares).  No
    midpoint, history point or YES complement is accepted by this class.
    """

    timestamp: datetime
    event_id: str
    market_id: str
    token_id: str
    bids: tuple[tuple[Decimal, Decimal], ...] = ()
    asks: tuple[tuple[Decimal, Decimal], ...] = ()
    station_id: str | None = None
    market_day: str | None = None
    tick_size: Decimal = Decimal("0.01")
    min_order_size: Decimal = Decimal("0")
    book_complete: bool = True
    market_stale: bool = False
    weather_stale: bool = False
    settlement_verified: bool = True
    in_season: bool = True
    season_version: str | None = "unknown"
    warming_valid: bool = True
    upstream_status: str = "normal"
    quality_excluded: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "bids", _price_levels(self.bids))
        object.__setattr__(self, "asks", _price_levels(self.asks))
        object.__setattr__(self, "tick_size", _decimal(self.tick_size))
        object.__setattr__(self, "min_order_size", _decimal(self.min_order_size))
        if self.tick_size <= ZERO:
            raise ValueError("tick_size must be positive")
        if self.min_order_size < ZERO:
            raise ValueError("min_order_size cannot be negative")

    @property
    def best_bid(self) -> Decimal | None:
        return _best(self.bids, bids=True)

    @property
    def best_ask(self) -> Decimal | None:
        return _best(self.asks, bids=False)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def portfolio_key(self) -> TokenPortfolioKey:
        return TokenPortfolioKey.from_snapshot(self)

    @property
    def health_ok(self) -> bool:
        return (
            self.book_complete
            and not self.market_stale
            and not self.weather_stale
            and self.settlement_verified
            and self.in_season
            and self.warming_valid
            and bool(self.season_version)
            and self.season_version.casefold() not in {"unknown", "none"}
            and not self.quality_excluded
            and self.upstream_status.casefold() == "normal"
        )

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Self:
        """Build a snapshot from an archived ``no`` row or a flat mapping."""
        no = row.get("no") if isinstance(row.get("no"), Mapping) else row
        timestamp = no.get("_timestamp") or row.get("observed_at") or row.get("timestamp")
        if timestamp is None:
            raise ValueError("book snapshot has no timestamp")
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        return cls(
            timestamp=_utc(timestamp),
            event_id=str(row.get("event_id") or row.get("event_slug") or ""),
            market_id=str(row.get("market_id") or row.get("market_slug") or ""),
            token_id=str(no.get("asset_id") or row.get("token_id") or ""),
            bids=_price_levels(no.get("bids")),
            asks=_price_levels(no.get("asks")),
            station_id=str(row["station_id"]) if row.get("station_id") else None,
            market_day=str(row["market_day"]) if row.get("market_day") else None,
            tick_size=_decimal(no.get("tick_size") or row.get("tick_size") or "0.01"),
            min_order_size=_decimal(
                no.get("min_order_size") or row.get("min_order_size") or "0"
            ),
            book_complete=_flag(no.get("book_complete", row.get("book_complete", True)), default=True),
            market_stale=_flag(row.get("market_stale", False)),
            weather_stale=_flag(row.get("weather_stale", False)),
            settlement_verified=_flag(row.get("settlement_verified", True), default=True),
            in_season=_flag(row.get("in_season", True), default=True),
            season_version=(
                str(row["season_version"]) if row.get("season_version") is not None else "unknown"
            ),
            warming_valid=_flag(row.get("warming_valid", True), default=True),
            upstream_status=str(row.get("upstream_status") or "normal"),
            quality_excluded=_flag(row.get("quality_excluded", False)),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A public taker trade; ``side`` is the aggressor side, never a quote."""

    timestamp: datetime
    asset_id: str
    side: ShadowSide | str
    price: Decimal
    size: Decimal
    event_id: str | None = None
    source: str = "data_api"
    sequence: int | None = None
    # Receipt/availability time is distinct from the exchange event timestamp.
    # Replays must never consume a trade before its local archive receipt.
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", _utc(self.available_at))
        object.__setattr__(self, "side", ShadowSide(str(self.side).upper()))
        object.__setattr__(self, "price", _decimal(self.price))
        object.__setattr__(self, "size", _decimal(self.size))
        if not ZERO < self.price <= ONE:
            raise ValueError("trade price must be in (0, 1]")
        if self.size <= ZERO:
            raise ValueError("trade size must be positive")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> Self:
        raw_timestamp = row.get("timestamp") or row.get("timestamp_ms")
        if isinstance(raw_timestamp, (int, float)):
            raw_timestamp = datetime.fromtimestamp(
                float(raw_timestamp) / (1000 if float(raw_timestamp) > 10**11 else 1), tz=UTC
            )
        return cls(
            timestamp=_utc(raw_timestamp),
            asset_id=str(row.get("asset") or row.get("asset_id") or ""),
            side=str(row.get("side") or ""),
            price=_decimal(row.get("price")),
            size=_decimal(row.get("size")),
            event_id=str(row["id"]) if row.get("id") is not None else None,
            source=str(row.get("source") or "data_api"),
            sequence=int(row["sequence"]) if row.get("sequence") is not None else None,
            available_at=(
                _utc(str(row["available_at"])) if row.get("available_at") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ShadowFill:
    fill_id: str
    order_id: str
    timestamp: datetime
    shares: Decimal
    price: Decimal
    model: FillModel
    source: str
    trade_id: str | None = None
    fee_usd: Decimal = ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "shares", _decimal(self.shares))
        object.__setattr__(self, "price", _decimal(self.price))
        object.__setattr__(self, "fee_usd", _decimal(self.fee_usd))


@dataclass
class ShadowOrder:
    order_id: str
    idempotency_key: str
    event_id: str
    market_id: str
    token_id: str
    station_id: str | None
    market_day: str
    side: ShadowSide
    state: ShadowOrderState
    submitted_at: datetime
    limit_price: Decimal
    requested_shares: Decimal
    requested_usd: Decimal
    maker_assumption: bool
    fill_model: FillModel
    tick_size: Decimal
    min_order_size: Decimal
    better_level_shares: Decimal
    volume_ahead: Decimal
    remaining_shares: Decimal
    filled_shares: Decimal = ZERO
    filled_usd: Decimal = ZERO
    cancelled_at: datetime | None = None
    expired_at: datetime | None = None
    last_fill_at: datetime | None = None
    cancel_reason: str | None = None
    strategy_version: str = "shadow-v1"
    season_version: str | None = None
    trigger_reason: str = ""
    weather_state: Mapping[str, Any] = field(default_factory=dict)
    physical_margin_f: Decimal | None = None
    minutes_to_peak: Decimal | None = None
    upstream_status: str = "normal"
    quality_excluded: bool = False
    fills: list[ShadowFill] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_reason: str = ShadowOrderReason.SUBMITTED.value
    execution_enabled: bool = False

    def __post_init__(self) -> None:
        if self.execution_enabled is not False:
            raise AssertionError("shadow orders must keep execution_enabled=false")
        self.submitted_at = _utc(self.submitted_at)
        if self.cancelled_at is not None:
            self.cancelled_at = _utc(self.cancelled_at)
        if self.expired_at is not None:
            self.expired_at = _utc(self.expired_at)
        if self.last_fill_at is not None:
            self.last_fill_at = _utc(self.last_fill_at)
        self.limit_price = _decimal(self.limit_price)
        self.requested_shares = _decimal(self.requested_shares)
        self.requested_usd = _decimal(self.requested_usd)
        self.tick_size = _decimal(self.tick_size)
        self.min_order_size = _decimal(self.min_order_size)
        self.better_level_shares = _decimal(self.better_level_shares)
        self.volume_ahead = _decimal(self.volume_ahead)
        self.remaining_shares = _decimal(self.remaining_shares)
        self.filled_shares = _decimal(self.filled_shares)
        self.filled_usd = _decimal(self.filled_usd)
        if self.physical_margin_f is not None:
            self.physical_margin_f = _decimal(self.physical_margin_f)
        if self.minutes_to_peak is not None:
            self.minutes_to_peak = _decimal(self.minutes_to_peak)

    @property
    def is_active(self) -> bool:
        return self.state in {
            ShadowOrderState.NEW,
            ShadowOrderState.RESTING,
            ShadowOrderState.PARTIALLY_FILLED,
        }

    @property
    def average_fill_price(self) -> Decimal | None:
        return self.filled_usd / self.filled_shares if self.filled_shares else None

    @property
    def reserved_usd(self) -> Decimal:
        return (
            self.remaining_shares * self.limit_price
            if self.is_active and self.side is ShadowSide.BUY
            else ZERO
        )

    @property
    def portfolio_key(self) -> TokenPortfolioKey:
        return TokenPortfolioKey.from_order(self)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = str(self.side)
        payload["state"] = str(self.state)
        payload["fill_model"] = str(self.fill_model)
        payload["submitted_at"] = self.submitted_at.isoformat()
        for key in ("cancelled_at", "expired_at", "last_fill_at"):
            if payload[key] is not None:
                payload[key] = payload[key].isoformat()
        payload["fills"] = [
            {
                **_json_value(asdict(fill)),
                "model": str(fill.model),
                "timestamp": fill.timestamp.isoformat(),
            }
            for fill in self.fills
        ]
        payload["portfolio_key"] = self.portfolio_key.as_dict()
        return _json_value(payload)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Self:
        fill_rows = payload.get("fills") or []
        fills = [
            ShadowFill(
                fill_id=str(row["fill_id"]),
                order_id=str(row["order_id"]),
                timestamp=_utc(str(row["timestamp"])),
                shares=_decimal(row["shares"]),
                price=_decimal(row["price"]),
                model=FillModel(str(row["model"])),
                source=str(row.get("source") or "unknown"),
                trade_id=str(row["trade_id"]) if row.get("trade_id") else None,
                fee_usd=_decimal(row.get("fee_usd") or "0"),
            )
            for row in fill_rows
        ]
        allowed = {
            field_name
            for field_name in cls.__dataclass_fields__
            if field_name != "fills"
        }
        values = {key: value for key, value in payload.items() if key in allowed}
        values["side"] = ShadowSide(str(values["side"]).upper())
        values["state"] = ShadowOrderState(str(values["state"]).upper())
        values["fill_model"] = FillModel(str(values["fill_model"]))
        for key in ("submitted_at", "cancelled_at", "expired_at", "last_fill_at"):
            if values.get(key) is not None:
                values[key] = _utc(str(values[key]))
        values["fills"] = fills
        return cls(**values)


class ShadowLedger:
    """Append-only, restart-safe shadow order ledger.

    Each mutation writes a complete order snapshot.  Loading uses the latest
    snapshot for each order and remembers idempotency keys/event IDs so a
    restarted replay cannot create a duplicate shadow order.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.orders: dict[str, ShadowOrder] = {}
        self._idempotency: dict[str, str] = {}
        self._seen_events: set[str] = set()
        self.legacy_invalid_records: list[dict[str, Any]] = []
        self.discrepancies: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                if row.get("record_type") == "discrepancy":
                    self.discrepancies.append(dict(row))
                    continue
                if row.get("record_type") != "order":
                    continue
                try:
                    schema_version = int(row.get("schema_version") or 0)
                except (TypeError, ValueError):
                    schema_version = 0
                if schema_version < SHADOW_LEDGER_SCHEMA_VERSION:
                    # v1 had no explicit portfolio key.  Retain a compact
                    # audit marker but never inject it into the new token-
                    # scoped runtime, even if individual fields look usable.
                    legacy_order = row.get("order")
                    self.legacy_invalid_records.append(
                        {
                            "reason": "legacy_schema_without_explicit_portfolio_key",
                            "order_id": str(
                                legacy_order.get("order_id")
                                if isinstance(legacy_order, Mapping)
                                else ""
                            ),
                        }
                    )
                    continue
                try:
                    order = ShadowOrder.from_dict(row["order"])
                except (KeyError, TypeError, ValueError):
                    self.legacy_invalid_records.append(
                        {"reason": "unreadable_token_scoped_order", "order_id": ""}
                    )
                    continue
                declared = row.get("portfolio_key")
                if not isinstance(declared, Mapping) or {
                    str(key): str(value) for key, value in declared.items()
                } != order.portfolio_key.as_dict():
                    self.legacy_invalid_records.append(
                        {
                            "reason": "portfolio_key_mismatch",
                            "order_id": order.order_id,
                        }
                    )
                    continue
                self.orders[order.order_id] = order
                self._idempotency[order.idempotency_key] = order.order_id
                event_id = row.get("event_key")
                if event_id:
                    self._seen_events.add(str(event_id))

    def save(self, order: ShadowOrder, *, event_key: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if event_key is not None and event_key in self._seen_events:
            return
        row = {
            "schema_version": SHADOW_LEDGER_SCHEMA_VERSION,
            "record_type": "order",
            "recorded_at": datetime.now(UTC).isoformat(),
            "event_key": event_key,
            "portfolio_key": order.portfolio_key.as_dict(),
            "order": order.as_dict(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.orders[order.order_id] = order
        self._idempotency[order.idempotency_key] = order.order_id
        if event_key is not None:
            self._seen_events.add(event_key)

    def by_idempotency(self, key: str) -> ShadowOrder | None:
        order_id = self._idempotency.get(key)
        return self.orders.get(order_id) if order_id else None

    def event_seen(self, event_key: str) -> bool:
        return event_key in self._seen_events

    def active(
        self, *, portfolio_key: TokenPortfolioKey | None = None
    ) -> tuple[ShadowOrder, ...]:
        return tuple(
            order
            for order in self.orders.values()
            if order.is_active and (portfolio_key is None or order.portfolio_key == portfolio_key)
        )

    def orders_for(self, portfolio_key: TokenPortfolioKey | None = None) -> tuple[ShadowOrder, ...]:
        return tuple(
            order
            for order in self.orders.values()
            if portfolio_key is None or order.portfolio_key == portfolio_key
        )

    def record_discrepancy(
        self,
        *,
        code: str,
        details: Mapping[str, Any],
        portfolio_key: TokenPortfolioKey | None = None,
    ) -> None:
        row = {
            "schema_version": SHADOW_LEDGER_SCHEMA_VERSION,
            "record_type": "discrepancy",
            "recorded_at": datetime.now(UTC).isoformat(),
            "code": code,
            "portfolio_key": portfolio_key.as_dict() if portfolio_key else None,
            "details": _json_value(dict(details)),
            "execution_enabled": False,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.discrepancies.append(row)


@dataclass(frozen=True, slots=True)
class ShadowStrategyConfig:
    """Finite, versioned candidate strategy family for replay.

    Values are configuration, not an invitation to search until a good result
    appears.  A missing season/version or an invalid gate is deliberately
    rejected by ``validate``.
    """

    version: str = "shadow-spread-v2-token-scoped"
    quote_mode: QuoteMode = QuoteMode.BEST_BID
    fill_model: FillModel = FillModel.QUEUE_AWARE
    entry_bands: Mapping[str, tuple[tuple[Decimal, Decimal], ...]] = field(default_factory=dict)
    target_rise: Decimal = Decimal("0.10")
    order_timeout: timedelta = timedelta(minutes=15)
    max_active_orders: int = 1
    market_budget_usd: Decimal = Decimal("200")
    tranche_usd: tuple[Decimal, ...] = (Decimal("50"),)
    replenish_mode: ReplenishMode = ReplenishMode.NONE
    improve_tick_limit: Decimal = Decimal("0")
    ladder_ticks: tuple[int, ...] = (0, -1, -2)
    require_health_gates: bool = True
    require_season_version: bool = True
    exit_targets: tuple[Decimal, ...] = (Decimal("0.05"), Decimal("0.10"), Decimal("0.13"))
    # Kept at the end to preserve positional compatibility for older callers.
    trigger_strategy: str = "price_band"
    partial_exit_fraction: Decimal = Decimal("0.50")
    max_hold: timedelta | None = timedelta(hours=4)
    # Appended at the end to preserve positional compatibility for older callers.
    station_day_budget_mode: StationDayBudgetMode = StationDayBudgetMode.CUMULATIVE_BUY_COST

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("strategy version is required")
        if self.trigger_strategy not in {"price_band", "weather_market_lag", "either"}:
            raise ValueError("trigger_strategy must be price_band, weather_market_lag, or either")
        if self.target_rise <= ZERO:
            raise ValueError("target_rise must be positive")
        if self.order_timeout <= timedelta(0):
            raise ValueError("order_timeout must be positive")
        if self.max_active_orders < 1:
            raise ValueError("max_active_orders must be positive")
        if self.market_budget_usd <= ZERO:
            raise ValueError("market_budget_usd must be positive")
        if self.station_day_budget_mode not in {
            StationDayBudgetMode.CURRENT_OPEN_EXPOSURE,
            StationDayBudgetMode.CUMULATIVE_BUY_COST,
        }:
            raise ValueError("invalid station_day_budget_mode")
        if not self.tranche_usd or any(value <= ZERO for value in self.tranche_usd):
            raise ValueError("tranche_usd must contain positive values")
        if sum(self.tranche_usd, start=ZERO) > self.market_budget_usd:
            raise ValueError("tranche schedule exceeds market budget")
        if not self.exit_targets:
            raise ValueError("exit targets must not be empty")
        if any(value <= ZERO for value in self.exit_targets):
            raise ValueError("exit targets must be positive")
        if not ZERO < self.partial_exit_fraction <= ONE:
            raise ValueError("partial_exit_fraction must be in (0, 1]")
        if self.max_hold is not None and self.max_hold <= timedelta(0):
            raise ValueError("max_hold must be positive")
        for station, bands in self.entry_bands.items():
            if not station or any(
                lower < ZERO or upper > ONE or lower >= upper for lower, upper in bands
            ):
                raise ValueError(f"invalid entry band for {station}")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> Self:
        bands: dict[str, tuple[tuple[Decimal, Decimal], ...]] = {}
        for station, rows in (payload.get("entry_bands") or {}).items():
            bands[str(station)] = tuple(
                (_decimal(row[0]), _decimal(row[1])) for row in rows
            )
        max_hold_value = payload.get("max_hold_seconds", 4 * 60 * 60)
        config = cls(
            version=str(payload.get("version") or "shadow-spread-v2-token-scoped"),
            trigger_strategy=str(payload.get("trigger_strategy") or "price_band"),
            quote_mode=QuoteMode(str(payload.get("quote_mode") or QuoteMode.BEST_BID)),
            fill_model=FillModel(str(payload.get("fill_model") or FillModel.QUEUE_AWARE)),
            entry_bands=bands,
            target_rise=_decimal(payload.get("target_rise", "0.10")),
            order_timeout=timedelta(seconds=float(payload.get("order_timeout_seconds", 900))),
            max_active_orders=int(payload.get("max_active_orders", 1)),
            market_budget_usd=_decimal(payload.get("market_budget_usd", "200")),
            station_day_budget_mode=StationDayBudgetMode(
                str(
                    payload.get(
                        "station_day_budget_mode",
                        StationDayBudgetMode.CUMULATIVE_BUY_COST,
                    )
                )
            ),
            tranche_usd=tuple(_decimal(value) for value in payload.get("tranche_usd", ("50",))),
            replenish_mode=ReplenishMode(
                str(payload.get("replenish_mode") or ReplenishMode.NONE)
            ),
            improve_tick_limit=_decimal(payload.get("improve_tick_limit", "0")),
            ladder_ticks=tuple(int(value) for value in payload.get("ladder_ticks", (0, -1, -2))),
            require_health_gates=_flag(payload.get("require_health_gates"), default=True),
            require_season_version=_flag(payload.get("require_season_version"), default=True),
            exit_targets=tuple(
                _decimal(value) for value in payload.get("exit_targets", ("0.05", "0.10", "0.13"))
            ),
            partial_exit_fraction=_decimal(payload.get("partial_exit_fraction", "0.50")),
            max_hold=(
                None
                if max_hold_value is None
                else timedelta(seconds=float(max_hold_value))
            ),
        )
        config.validate()
        return config


def _is_tick(value: Decimal, tick: Decimal) -> bool:
    quotient = value / tick
    return quotient == quotient.to_integral_value()


def quote_limit(
    snapshot: BookSnapshot,
    *,
    side: ShadowSide | str = ShadowSide.BUY,
    mode: QuoteMode | str = QuoteMode.BEST_BID,
    tick_size: Decimal | float | str | None = None,
) -> Decimal | None:
    """Return a non-crossing post-only quote from the token's own book."""
    side_value = ShadowSide(str(side).upper())
    mode_value = QuoteMode(str(mode))
    tick = _decimal(tick_size or snapshot.tick_size)
    if tick <= ZERO:
        raise ValueError("tick_size must be positive")
    reference = snapshot.best_bid if side_value is ShadowSide.BUY else snapshot.best_ask
    opposite = snapshot.best_ask if side_value is ShadowSide.BUY else snapshot.best_bid
    if reference is None:
        return None
    if mode_value is QuoteMode.BEST_BID:
        candidate = reference
    elif mode_value is QuoteMode.IMPROVE_ONE_TICK:
        candidate = reference + tick if side_value is ShadowSide.BUY else reference - tick
    else:
        candidate = reference
    candidate = candidate.quantize(tick)
    if not ZERO < candidate <= ONE:
        return None
    if opposite is not None:
        if side_value is ShadowSide.BUY and candidate >= opposite:
            return None
        if side_value is ShadowSide.SELL and candidate <= opposite:
            return None
    return candidate


def quote_ladder(
    snapshot: BookSnapshot,
    *,
    side: ShadowSide | str = ShadowSide.BUY,
    tick_offsets: Sequence[int] = (0, -1, -2),
) -> tuple[Decimal, ...]:
    """Build a finite ladder, dropping any quote that would cross the book."""
    base = quote_limit(snapshot, side=side, mode=QuoteMode.BEST_BID)
    if base is None:
        return ()
    side_value = ShadowSide(str(side).upper())
    tick = snapshot.tick_size
    output: list[Decimal] = []
    for offset in tick_offsets:
        candidate = base + tick * int(offset) if side_value is ShadowSide.BUY else base - tick * int(offset)
        if not ZERO < candidate <= ONE or not _is_tick(candidate, tick):
            continue
        opposite = snapshot.best_ask if side_value is ShadowSide.BUY else snapshot.best_bid
        if opposite is not None and (
            candidate >= opposite if side_value is ShadowSide.BUY else candidate <= opposite
        ):
            continue
        if candidate not in output:
            output.append(candidate)
    return tuple(output)


def _queue_ahead(snapshot: BookSnapshot, side: ShadowSide, limit_price: Decimal) -> tuple[Decimal, Decimal]:
    if side is ShadowSide.BUY:
        better = sum((size for price, size in snapshot.bids if price > limit_price), start=ZERO)
        same = sum((size for price, size in snapshot.bids if price == limit_price), start=ZERO)
    else:
        better = sum((size for price, size in snapshot.asks if price < limit_price), start=ZERO)
        same = sum((size for price, size in snapshot.asks if price == limit_price), start=ZERO)
    return better, same


def _first_level_quantity(
    levels: Sequence[tuple[Decimal, Decimal]], *, side: ShadowSide, limit: Decimal
) -> Decimal:
    if side is ShadowSide.BUY:
        return sum((size for price, size in levels if price <= limit), start=ZERO)
    return sum((size for price, size in levels if price >= limit), start=ZERO)


class ShadowOrderEngine:
    """State machine for one token portfolio's finite, single-direction inventory."""

    def __init__(
        self,
        *,
        ledger: ShadowLedger | None = None,
        ledger_path: Path | str | None = None,
        budget_usd: Decimal | float | str = Decimal("200"),
        max_active_orders: int = 1,
        fill_model: FillModel | str = FillModel.QUEUE_AWARE,
        order_timeout: timedelta = timedelta(minutes=15),
        market_day: str | None = None,
        portfolio_key: TokenPortfolioKey | None = None,
        budget_mode: StationDayBudgetMode | str = StationDayBudgetMode.CUMULATIVE_BUY_COST,
        execution_enabled: bool = EXECUTION_ENABLED,
        enforce_inventory: bool = True,
        require_season_version: bool = True,
    ) -> None:
        if execution_enabled is not False:
            raise AssertionError("shadow engine must keep execution_enabled=false")
        if ledger is not None and ledger_path is not None:
            raise ValueError("provide ledger or ledger_path, not both")
        self.ledger = ledger or (ShadowLedger(ledger_path) if ledger_path else None)
        self.budget_usd = _decimal(budget_usd)
        if self.budget_usd <= ZERO:
            raise ValueError("budget_usd must be positive")
        self.max_active_orders = int(max_active_orders)
        if self.max_active_orders < 1:
            raise ValueError("max_active_orders must be positive")
        self.fill_model = FillModel(str(fill_model))
        self.order_timeout = order_timeout
        self.market_day = market_day
        self.portfolio_key = portfolio_key
        self.budget_mode = StationDayBudgetMode(str(budget_mode))
        if self.portfolio_key is not None and self.market_day not in {
            None,
            self.portfolio_key.market_day,
        }:
            raise ValueError("market_day conflicts with token portfolio key")
        if self.portfolio_key is not None:
            self.market_day = self.portfolio_key.market_day
        self.enforce_inventory = enforce_inventory
        self.require_season_version = require_season_version
        self.inventory_shares = ZERO
        self.inventory_cost_usd = ZERO
        self.cumulative_buy_cost_usd = ZERO
        self.realized_pnl_usd = ZERO
        self.realized_pnl_by_exit_source: dict[str, Decimal] = {}
        self.realized_pnl_by_exit_reason: dict[str, Decimal] = {}
        self.fees_usd = ZERO
        self.round_trip_count = 0
        self._open_round_trip = False
        self.rejection_reasons: list[str] = []
        self.last_snapshot: BookSnapshot | None = None
        self._seen_fill_events: set[str] = set()
        self._seen_event_keys: set[str] = set()
        self._local_idempotency: dict[str, ShadowOrder] = {}
        if self.ledger is not None:
            self._bind_ledger_scope_if_unambiguous()
            self._restore_inventory()

    def _bind_portfolio_key(self, key: TokenPortfolioKey) -> None:
        if self.portfolio_key is not None and self.portfolio_key != key:
            raise ShadowPortfolioInvariantError(
                "token_portfolio_rebinding_attempt",
                details={
                    "expected": self.portfolio_key.as_dict(),
                    "actual": key.as_dict(),
                },
            )
        if self.market_day not in {None, key.market_day}:
            raise ShadowPortfolioInvariantError(
                "market_day_conflicts_with_token_portfolio",
                details={"market_day": self.market_day, "portfolio_key": key.as_dict()},
            )
        self.portfolio_key = key
        self.market_day = key.market_day

    def _bind_ledger_scope_if_unambiguous(self) -> None:
        """Never restore a multi-token ledger through an unscoped engine."""
        if self.ledger is None or self.portfolio_key is not None:
            return
        keys = {order.portfolio_key for order in self.ledger.orders.values()}
        if len(keys) > 1:
            raise ShadowPortfolioInvariantError(
                "unscoped_ledger_contains_multiple_token_portfolios",
                details={"portfolio_count": len(keys)},
            )
        if keys:
            self._bind_portfolio_key(next(iter(keys)))

    def _restore_inventory(self) -> None:
        fills = sorted(
            ((order, fill) for order in self.orders for fill in order.fills),
            key=lambda item: item[1].timestamp,
        )
        for order, fill in fills:
            event_key = f"{order.order_id}:{fill.fill_id}"
            if event_key in self._seen_fill_events:
                continue
            self._seen_fill_events.add(event_key)
            if order.side is ShadowSide.BUY:
                self.inventory_shares += fill.shares
                self.inventory_cost_usd += fill.shares * fill.price + fill.fee_usd
                self.cumulative_buy_cost_usd += fill.shares * fill.price + fill.fee_usd
                self.fees_usd += fill.fee_usd
                self._open_round_trip = True
            else:
                if fill.shares > self.inventory_shares:
                    raise ShadowPortfolioInvariantError(
                        "legacy_or_corrupt_sell_exceeds_token_inventory",
                        details={
                            "portfolio_key": order.portfolio_key.as_dict(),
                            "fill_id": fill.fill_id,
                            "shares": str(fill.shares),
                            "inventory_shares": str(self.inventory_shares),
                        },
                    )
                average_cost = self.average_inventory_cost or ZERO
                self.inventory_shares -= fill.shares
                self.inventory_cost_usd -= fill.shares * average_cost
                realized = fill.shares * (fill.price - average_cost) - fill.fee_usd
                self.realized_pnl_usd += realized
                self.realized_pnl_by_exit_source[fill.source] = (
                    self.realized_pnl_by_exit_source.get(fill.source, ZERO) + realized
                )
                self.realized_pnl_by_exit_reason[order.trigger_reason] = (
                    self.realized_pnl_by_exit_reason.get(order.trigger_reason, ZERO) + realized
                )
                self.fees_usd += fill.fee_usd
                if self.inventory_shares == ZERO:
                    self.inventory_cost_usd = ZERO
                    if self._open_round_trip:
                        self.round_trip_count += 1
                        self._open_round_trip = False

    @property
    def active_orders(self) -> tuple[ShadowOrder, ...]:
        if self.ledger is None:
            return tuple(order for order in getattr(self, "_orders", {}).values() if order.is_active)
        return self.ledger.active(portfolio_key=self.portfolio_key)

    @property
    def orders(self) -> tuple[ShadowOrder, ...]:
        if self.ledger is None:
            return tuple(getattr(self, "_orders", {}).values())
        return self.ledger.orders_for(self.portfolio_key)

    @property
    def average_inventory_cost(self) -> Decimal | None:
        return self.inventory_cost_usd / self.inventory_shares if self.inventory_shares else None

    @property
    def active_reserved_usd(self) -> Decimal:
        return sum((order.reserved_usd for order in self.active_orders), start=ZERO)

    @property
    def total_risk_usd(self) -> Decimal:
        return self.inventory_cost_usd + self.active_reserved_usd

    @property
    def available_budget_usd(self) -> Decimal:
        used = (
            self.cumulative_buy_cost_usd
            if self.budget_mode is StationDayBudgetMode.CUMULATIVE_BUY_COST
            else self.inventory_cost_usd
        )
        return max(ZERO, self.budget_usd - used - self.active_reserved_usd)

    @property
    def budget_used_usd(self) -> Decimal:
        used = (
            self.cumulative_buy_cost_usd
            if self.budget_mode is StationDayBudgetMode.CUMULATIVE_BUY_COST
            else self.inventory_cost_usd
        )
        return used + self.active_reserved_usd

    def _assert_snapshot_scope(self, snapshot: BookSnapshot) -> None:
        if self.portfolio_key is None:
            self._bind_portfolio_key(snapshot.portfolio_key)
            return
        if snapshot.portfolio_key != self.portfolio_key:
            raise ShadowPortfolioInvariantError(
                "snapshot_token_portfolio_mismatch",
                details={
                    "expected": self.portfolio_key.as_dict(),
                    "actual": snapshot.portfolio_key.as_dict(),
                },
            )

    def _assert_order_scope(self, order: ShadowOrder) -> None:
        if self.portfolio_key is None:
            self._bind_portfolio_key(order.portfolio_key)
            return
        if order.portfolio_key != self.portfolio_key:
            raise ShadowPortfolioInvariantError(
                "order_token_portfolio_mismatch",
                details={
                    "expected": self.portfolio_key.as_dict(),
                    "actual": order.portfolio_key.as_dict(),
                    "order_id": order.order_id,
                },
            )

    def _store(self, order: ShadowOrder, *, event_key: str | None = None) -> None:
        self._assert_order_scope(order)
        if event_key is not None and event_key in self._seen_event_keys:
            return
        if self.ledger is None:
            if not hasattr(self, "_orders"):
                self._orders: dict[str, ShadowOrder] = {}
            if event_key is not None and event_key in self._seen_fill_events:
                return
            self._orders[order.order_id] = order
            self._local_idempotency[order.idempotency_key] = order
        else:
            self.ledger.save(order, event_key=event_key)
        if event_key is not None:
            self._seen_event_keys.add(event_key)

    def _reject(self, reason: ShadowOrderReason, message: str | None = None) -> None:
        self.rejection_reasons.append(reason.value)
        raise ShadowOrderRejected(reason, message)

    def _validate_snapshot_for_new(self, snapshot: BookSnapshot) -> None:
        if not snapshot.health_ok:
            if snapshot.upstream_status.casefold() in {"maintenance", "degraded", "recovery"}:
                reason = (
                    ShadowOrderReason.RECOVERY
                    if snapshot.upstream_status.casefold() == "recovery"
                    else ShadowOrderReason.MAINTENANCE
                )
            elif snapshot.market_stale:
                reason = ShadowOrderReason.STALE_MARKET
            elif snapshot.weather_stale:
                reason = ShadowOrderReason.STALE_WEATHER
            elif not snapshot.book_complete:
                reason = ShadowOrderReason.INCOMPLETE_BOOK
            elif not snapshot.settlement_verified:
                reason = ShadowOrderReason.SETTLEMENT_UNVERIFIED
            elif not snapshot.in_season:
                reason = ShadowOrderReason.OUT_OF_SEASON
            else:
                reason = ShadowOrderReason.INVALID_SEASON_VERSION
            self._reject(reason)
        if self.require_season_version and (
            not snapshot.season_version
            or snapshot.season_version.casefold() in {"unknown", "none"}
        ):
            self._reject(ShadowOrderReason.INVALID_SEASON_VERSION)

    def _active_for(self, snapshot: BookSnapshot) -> tuple[ShadowOrder, ...]:
        self._assert_snapshot_scope(snapshot)
        return tuple(
            order
            for order in self.active_orders
            if order.market_id == snapshot.market_id
            and order.token_id == snapshot.token_id
            and (order.market_day == (snapshot.market_day or self.market_day or order.market_day))
        )

    def _active_for_market_day(self, snapshot: BookSnapshot) -> tuple[ShadowOrder, ...]:
        self._assert_snapshot_scope(snapshot)
        day = snapshot.market_day or self.market_day or snapshot.timestamp.date().isoformat()
        return tuple(
            order
            for order in self.active_orders
            if order.market_id == snapshot.market_id
            and order.token_id == snapshot.token_id
            and order.market_day == day
        )

    def submit_limit(
        self,
        snapshot: BookSnapshot,
        *,
        side: ShadowSide | str = ShadowSide.BUY,
        limit_price: Decimal | float | str,
        size_usd: Decimal | float | str | None = None,
        shares: Decimal | float | str | None = None,
        idempotency_key: str | None = None,
        order_id: str | None = None,
        strategy_version: str = "shadow-v1",
        season_version: str | None = None,
        trigger_reason: str = "",
        weather_state: Mapping[str, Any] | None = None,
        physical_margin_f: Decimal | float | str | None = None,
        minutes_to_peak: Decimal | float | str | None = None,
        maker_assumption: bool = True,
        metadata: Mapping[str, Any] | None = None,
        risk_exit: bool = False,
    ) -> ShadowOrder:
        self._assert_snapshot_scope(snapshot)
        if not (risk_exit and side is not None and ShadowSide(str(side).upper()) is ShadowSide.SELL):
            self._validate_snapshot_for_new(snapshot)
        side_value = ShadowSide(str(side).upper())
        price = _decimal(limit_price)
        if not ZERO < price <= ONE:
            self._reject(ShadowOrderReason.INVALID_TICK, "limit price must be in (0, 1]")
        if not _is_tick(price, snapshot.tick_size):
            self._reject(ShadowOrderReason.INVALID_TICK)
        if maker_assumption:
            opposite = snapshot.best_ask if side_value is ShadowSide.BUY else snapshot.best_bid
            if opposite is not None and (
                price >= opposite if side_value is ShadowSide.BUY else price <= opposite
            ):
                self._reject(ShadowOrderReason.POST_ONLY)
        if idempotency_key is None:
            idempotency_key = (
                f"{snapshot.event_id}:{snapshot.market_id}:{snapshot.token_id}:"
                f"{snapshot.timestamp.isoformat()}:{side_value}:{price}:{size_usd}:{shares}"
            )
        existing = (
            self.ledger.by_idempotency(idempotency_key)
            if self.ledger
            else self._local_idempotency.get(idempotency_key)
        )
        if existing is not None:
            # Idempotency keys are caller-supplied.  Never let a reused key
            # return an order belonging to a different token portfolio.
            self._assert_order_scope(existing)
            return existing
        active = self._active_for_market_day(snapshot)
        if len(active) >= self.max_active_orders:
            self._reject(ShadowOrderReason.ACTIVE_LIMIT)
        if shares is None:
            if size_usd is None:
                raise ValueError("one of size_usd or shares is required")
            notional = _decimal(size_usd)
            if notional <= ZERO:
                raise ValueError("size_usd must be positive")
            requested_shares = notional / price
        else:
            requested_shares = _decimal(shares)
            if requested_shares <= ZERO:
                raise ValueError("shares must be positive")
            notional = requested_shares * price if size_usd is None else _decimal(size_usd)
        if requested_shares < snapshot.min_order_size:
            self._reject(ShadowOrderReason.MIN_ORDER_SIZE)
        estimated_fee = trading_fee_usdc(
            requested_shares,
            price,
            liquidity_role=LiquidityRole.MAKER if maker_assumption else LiquidityRole.TAKER,
            market_category="weather",
        )
        if (
            side_value is ShadowSide.BUY
            and self.budget_used_usd + notional + estimated_fee > self.budget_usd
        ):
            self._reject(ShadowOrderReason.BUDGET)
        if side_value is ShadowSide.SELL and self.enforce_inventory and requested_shares > self.inventory_shares:
            self._reject(ShadowOrderReason.INVENTORY)
        better, ahead = _queue_ahead(snapshot, side_value, price)
        order = ShadowOrder(
            order_id=order_id or str(uuid.uuid4()),
            idempotency_key=idempotency_key,
            event_id=snapshot.event_id,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            station_id=snapshot.station_id,
            market_day=snapshot.market_day or self.market_day or snapshot.timestamp.date().isoformat(),
            side=side_value,
            state=ShadowOrderState.NEW,
            submitted_at=snapshot.timestamp,
            limit_price=price,
            requested_shares=requested_shares,
            requested_usd=notional,
            maker_assumption=maker_assumption,
            fill_model=self.fill_model,
            tick_size=snapshot.tick_size,
            min_order_size=snapshot.min_order_size,
            better_level_shares=better,
            volume_ahead=ahead,
            remaining_shares=requested_shares,
            strategy_version=strategy_version,
            season_version=season_version or snapshot.season_version,
            trigger_reason=trigger_reason,
            weather_state=dict(weather_state or snapshot.metadata),
            physical_margin_f=(
                _decimal(physical_margin_f)
                if physical_margin_f is not None
                else _decimal(snapshot.metadata["physical_margin_f"])
                if snapshot.metadata.get("physical_margin_f") is not None
                else None
            ),
            minutes_to_peak=(
                _decimal(minutes_to_peak)
                if minutes_to_peak is not None
                else _decimal(snapshot.metadata["minutes_to_peak"])
                if snapshot.metadata.get("minutes_to_peak") is not None
                else None
            ),
            upstream_status=snapshot.upstream_status,
            quality_excluded=snapshot.quality_excluded,
            metadata=dict(metadata or {}),
        )
        order.state = ShadowOrderState.RESTING
        self._store(order, event_key=f"submit:{idempotency_key}")
        return order

    def _apply_fill(
        self,
        order: ShadowOrder,
        *,
        timestamp: datetime,
        shares: Decimal,
        price: Decimal,
        model: FillModel,
        source: str,
        trade_id: str | None = None,
        asset_id: str | None = None,
    ) -> ShadowFill | None:
        self._assert_order_scope(order)
        if asset_id is not None and asset_id != order.token_id:
            raise ShadowPortfolioInvariantError(
                "fill_asset_does_not_match_order_token",
                details={
                    "order_id": order.order_id,
                    "order_token_id": order.token_id,
                    "fill_asset_id": asset_id,
                },
            )
        if not order.is_active or shares <= ZERO:
            return None
        quantity = min(shares, order.remaining_shares)
        if quantity <= ZERO:
            return None
        if order.side is ShadowSide.SELL and quantity > self.inventory_shares:
            raise ShadowPortfolioInvariantError(
                "sell_exceeds_same_token_inventory",
                details={
                    "portfolio_key": order.portfolio_key.as_dict(),
                    "order_id": order.order_id,
                    "shares": str(quantity),
                    "inventory_shares": str(self.inventory_shares),
                },
            )
        fill_id = str(uuid.uuid4())
        fee = trading_fee_usdc(
            quantity,
            price,
            liquidity_role=LiquidityRole.MAKER
            if order.maker_assumption
            else LiquidityRole.TAKER,
            market_category="weather",
        )
        fill = ShadowFill(
            fill_id=fill_id,
            order_id=order.order_id,
            timestamp=timestamp,
            shares=quantity,
            price=price,
            model=model,
            source=source,
            trade_id=trade_id,
            fee_usd=fee,
        )
        order.fills.append(fill)
        order.remaining_shares -= quantity
        order.filled_shares += quantity
        order.filled_usd += quantity * price
        order.last_fill_at = _utc(timestamp)
        order.state = (
            ShadowOrderState.FILLED
            if order.remaining_shares <= ZERO
            else ShadowOrderState.PARTIALLY_FILLED
        )
        if order.state is ShadowOrderState.FILLED:
            order.remaining_shares = ZERO
        if order.side is ShadowSide.BUY:
            self.inventory_shares += quantity
            self.inventory_cost_usd += quantity * price + fee
            self.cumulative_buy_cost_usd += quantity * price + fee
            self.fees_usd += fee
            self._open_round_trip = True
        else:
            average_cost = self.average_inventory_cost or ZERO
            self.inventory_shares -= quantity
            self.inventory_cost_usd -= quantity * average_cost
            realized = quantity * (price - average_cost) - fee
            self.realized_pnl_usd += realized
            self.realized_pnl_by_exit_source[source] = (
                self.realized_pnl_by_exit_source.get(source, ZERO) + realized
            )
            self.realized_pnl_by_exit_reason[order.trigger_reason] = (
                self.realized_pnl_by_exit_reason.get(order.trigger_reason, ZERO) + realized
            )
            self.fees_usd += fee
            if self.inventory_shares == ZERO:
                self.inventory_cost_usd = ZERO
                if self._open_round_trip:
                    self.round_trip_count += 1
                    self._open_round_trip = False
        self._store(order, event_key=f"fill:{order.order_id}:{fill_id}")
        return fill

    def submit_maker_exit(
        self,
        snapshot: BookSnapshot,
        *,
        target_price: Decimal | float | str,
        shares: Decimal | float | str | None = None,
        idempotency_key: str | None = None,
        strategy_version: str = "shadow-v1",
        trigger_reason: str = "target_maker_exit",
    ) -> ShadowOrder:
        """Post a non-crossing maker exit only when real bid has target space."""
        target = _decimal(target_price)
        if snapshot.best_bid is None or snapshot.best_bid < target:
            self._reject(ShadowOrderReason.POST_ONLY, "real NO bid has not reached exit target")
        quote = quote_limit(snapshot, side=ShadowSide.SELL, mode=QuoteMode.BEST_BID)
        if quote is None or quote < target:
            self._reject(ShadowOrderReason.POST_ONLY)
        return self.submit_limit(
            snapshot,
            side=ShadowSide.SELL,
            limit_price=quote,
            shares=self.inventory_shares if shares is None else shares,
            idempotency_key=idempotency_key,
            strategy_version=strategy_version,
            season_version=snapshot.season_version,
            trigger_reason=trigger_reason,
            maker_assumption=True,
        )

    def submit_replenishment(
        self,
        snapshot: BookSnapshot,
        *,
        limit_price: Decimal | float | str,
        size_usd: Decimal | float | str,
        mode: ReplenishMode | str,
        target_price: Decimal | float | str,
        prior_order_resolved: bool,
        weather_improving: bool = False,
        weather_unchanged: bool = False,
        weather_worsening: bool = False,
        duplicate_event: bool = False,
        spread_ok: bool = True,
        book_ok: bool = True,
        idempotency_key: str | None = None,
        strategy_version: str = "shadow-v1",
    ) -> ShadowOrder:
        """Create a gated additional buy; never performs unconditional averaging down."""
        decision = decide_replenish(
            mode=mode,
            current_ask=snapshot.best_ask if snapshot.best_ask is not None else limit_price,
            target_price=target_price,
            prior_order_resolved=prior_order_resolved,
            weather_improving=weather_improving,
            weather_unchanged=weather_unchanged,
            weather_worsening=weather_worsening,
            health_ok=snapshot.health_ok,
            season_version_valid=bool(
                snapshot.season_version
                and snapshot.season_version.casefold() not in {"unknown", "none"}
            ),
            budget_remaining_usd=self.available_budget_usd,
            weighted_average_cost=self.average_inventory_cost,
            duplicate_event=duplicate_event,
            spread_ok=spread_ok,
            book_ok=book_ok,
        )
        if not decision.allowed:
            reason = (
                ShadowOrderReason.WEATHER_WORSENING
                if decision.reason == ShadowOrderReason.WEATHER_WORSENING.value
                else ShadowOrderReason.BUDGET
                if decision.reason == ShadowOrderReason.BUDGET.value
                else ShadowOrderReason.INVALID_SEASON_VERSION
                if decision.reason == ShadowOrderReason.INVALID_SEASON_VERSION.value
                else ShadowOrderReason.POST_ONLY
            )
            self._reject(reason, decision.reason)
        return self.submit_limit(
            snapshot,
            side=ShadowSide.BUY,
            limit_price=limit_price,
            size_usd=size_usd,
            idempotency_key=idempotency_key,
            strategy_version=strategy_version,
            season_version=snapshot.season_version,
            trigger_reason=f"replenish:{decision.mode.value}",
            maker_assumption=True,
        )

    def process_snapshot(self, snapshot: BookSnapshot) -> tuple[ShadowFill, ...]:
        """Process a later quote; only touch mode can fill from a quote."""
        self._assert_snapshot_scope(snapshot)
        self.last_snapshot = snapshot
        if not snapshot.health_ok:
            for order in self._active_for(snapshot):
                self.cancel(
                    order.order_id,
                    timestamp=snapshot.timestamp,
                    reason=(
                        ShadowOrderReason.RECOVERY.value
                        if snapshot.upstream_status.casefold() == "recovery"
                        else ShadowOrderReason.MAINTENANCE.value
                        if snapshot.upstream_status.casefold() in {"maintenance", "degraded"}
                        else ShadowOrderReason.STALE_MARKET.value
                    ),
                )
            return ()
        fills: list[ShadowFill] = []
        for order in self._active_for(snapshot):
            if snapshot.timestamp <= order.submitted_at:
                continue
            if snapshot.timestamp - order.submitted_at >= self.order_timeout:
                self.expire(order.order_id, timestamp=snapshot.timestamp, reason=ShadowOrderReason.TIMEOUT.value)
                continue
            if order.fill_model is not FillModel.TOUCH:
                continue
            touch_key = f"touch:{order.order_id}:{snapshot.timestamp.isoformat()}"
            if self.ledger is not None and self.ledger.event_seen(touch_key):
                continue
            best = snapshot.best_ask if order.side is ShadowSide.BUY else snapshot.best_bid
            if best is None:
                continue
            touched = best <= order.limit_price if order.side is ShadowSide.BUY else best >= order.limit_price
            if touched:
                fill = self._apply_fill(
                    order,
                    timestamp=snapshot.timestamp,
                    shares=order.remaining_shares,
                    price=order.limit_price,
                    model=FillModel.TOUCH,
                    source="book_touch_upper_bound",
                    asset_id=snapshot.token_id,
                )
                if fill is not None:
                    fills.append(fill)
                    self._store(order, event_key=touch_key)
        return tuple(fills)

    def process_trade(self, trade: TradeEvent) -> tuple[ShadowFill, ...]:
        """Consume a single real taker trade; book changes alone never fill."""
        if self.portfolio_key is not None and trade.asset_id != self.portfolio_key.token_id:
            raise ShadowPortfolioInvariantError(
                "trade_routed_to_wrong_token_portfolio",
                details={
                    "expected_token_id": self.portfolio_key.token_id,
                    "trade_asset_id": trade.asset_id,
                    "portfolio_key": self.portfolio_key.as_dict(),
                },
            )
        # A transaction can contain multiple fills for one token.  The public
        # API identity is the transaction hash, so include the tape fields in
        # the idempotency key rather than dropping later fills from the same
        # transaction.
        trade_key = (
            f"trade:{trade.event_id}:{trade.asset_id}:{trade.timestamp.isoformat()}"
            f":{trade.price}:{trade.size}:{trade.sequence or ''}"
            if trade.event_id
            else None
        )
        if trade_key is not None and (
            trade_key in self._seen_event_keys
            or self.ledger is not None
            and self.ledger.event_seen(trade_key)
        ):
            return ()
        fills: list[ShadowFill] = []
        for order in self.active_orders:
            if order.token_id != trade.asset_id or trade.timestamp <= order.submitted_at:
                continue
            if order.side is ShadowSide.BUY:
                eligible_side = trade.side is ShadowSide.SELL
                eligible_price = trade.price <= order.limit_price
                strict_cross = trade.price < order.limit_price
            else:
                eligible_side = trade.side is ShadowSide.BUY
                eligible_price = trade.price >= order.limit_price
                strict_cross = trade.price > order.limit_price
            if not eligible_side or not eligible_price:
                continue
            if order.fill_model is FillModel.TOUCH:
                continue
            if order.fill_model is FillModel.TRADE_THROUGH and not strict_cross:
                continue
            queue = order.better_level_shares + order.volume_ahead
            consumed = min(queue, trade.size)
            order.better_level_shares = max(ZERO, order.better_level_shares - consumed)
            consumed_ahead = max(ZERO, consumed - max(ZERO, queue - order.volume_ahead))
            order.volume_ahead = max(ZERO, order.volume_ahead - consumed_ahead)
            available = max(ZERO, trade.size - queue)
            if available <= ZERO:
                self._store(order, event_key=trade_key)
                continue
            fill = self._apply_fill(
                order,
                timestamp=trade.timestamp,
                shares=available,
                price=order.limit_price,
                model=order.fill_model,
                source=trade.source,
                trade_id=trade.event_id,
                asset_id=trade.asset_id,
            )
            if fill is not None:
                fills.append(fill)
        if trade_key is not None:
            self._seen_fill_events.add(trade_key)
            for order in self.active_orders:
                self._store(order, event_key=trade_key)
            self._seen_event_keys.add(trade_key)
        return tuple(fills)

    def process_trades(
        self,
        trades: Sequence[TradeEvent],
        *,
        reject_ambiguous_same_second: bool = True,
    ) -> tuple[ShadowFill, ...]:
        """Process trades chronologically without inventing Data API order.

        A same-second group with no sequence is skipped by default.  This is
        deliberately conservative: Data API timestamps are seconds, so a
        replay must not choose an arbitrary intra-second queue order.
        """
        ordered = sorted(trades, key=lambda row: (row.timestamp, row.sequence is None, row.sequence or 0))
        fills: list[ShadowFill] = []
        index = 0
        while index < len(ordered):
            timestamp = ordered[index].timestamp
            group: list[TradeEvent] = []
            while index < len(ordered) and ordered[index].timestamp == timestamp:
                group.append(ordered[index])
                index += 1
            ambiguous = len(group) > 1 and any(row.sequence is None for row in group)
            if ambiguous and reject_ambiguous_same_second:
                continue
            for trade in group:
                fills.extend(self.process_trade(trade))
        return tuple(fills)

    def cancel(
        self,
        order_id: str,
        *,
        timestamp: datetime | None = None,
        reason: str = ShadowOrderReason.CANCELLED.value,
    ) -> ShadowOrder:
        order = self._find(order_id)
        if not order.is_active:
            return order
        order.state = ShadowOrderState.CANCELLED
        order.cancelled_at = _utc(timestamp or datetime.now(UTC))
        order.cancel_reason = reason
        self._store(order, event_key=f"cancel:{order_id}:{order.cancelled_at.isoformat()}")
        return order

    def expire(
        self,
        order_id: str,
        *,
        timestamp: datetime | None = None,
        reason: str = ShadowOrderReason.EXPIRED.value,
    ) -> ShadowOrder:
        order = self._find(order_id)
        if not order.is_active:
            return order
        order.state = ShadowOrderState.EXPIRED
        order.expired_at = _utc(timestamp or datetime.now(UTC))
        order.cancel_reason = reason
        self._store(order, event_key=f"expire:{order_id}:{order.expired_at.isoformat()}")
        return order

    def requote(
        self,
        order_id: str,
        snapshot: BookSnapshot,
        *,
        limit_price: Decimal | float | str,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> ShadowOrder:
        old = self.cancel(order_id, timestamp=snapshot.timestamp, reason=ShadowOrderReason.REQUOTED.value)
        return self.submit_limit(
            snapshot,
            side=old.side,
            limit_price=limit_price,
            shares=old.remaining_shares,
            idempotency_key=idempotency_key or f"requote:{old.order_id}:{snapshot.timestamp.isoformat()}",
            strategy_version=old.strategy_version,
            season_version=old.season_version,
            trigger_reason=kwargs.pop("trigger_reason", ShadowOrderReason.REQUOTED.value),
            maker_assumption=old.maker_assumption,
            metadata={**old.metadata, **kwargs.pop("metadata", {})},
            **kwargs,
        )

    def cancel_all(self, *, timestamp: datetime, reason: str) -> tuple[ShadowOrder, ...]:
        return tuple(
            self.cancel(order.order_id, timestamp=timestamp, reason=reason)
            for order in self.active_orders
        )

    def simulate_taker_exit(
        self,
        snapshot: BookSnapshot,
        *,
        shares: Decimal | float | str | None = None,
        reason: str = "urgent_taker_exit",
    ) -> tuple[ShadowFill, ...]:
        """Walk the real bid ladder for an emergency read-only exit.

        This method is a local cost simulation only.  It never sends an order;
        the fill carries ``source=emergency_taker_depth`` and the official
        weather taker fee is recorded separately in ``realized_pnl_usd``.
        """
        self._assert_snapshot_scope(snapshot)
        quantity = self.inventory_shares if shares is None else _decimal(shares)
        if quantity <= ZERO:
            return ()
        quantity = min(quantity, self.inventory_shares)
        # Cancel any resting maker exit before creating the synthetic urgent
        # taker path; this is a local risk disposition, not a new exchange
        # order.
        for active in self.active_orders:
            if active.token_id == snapshot.token_id:
                self.cancel(active.order_id, timestamp=snapshot.timestamp, reason=reason)
        remaining = quantity
        fills: list[ShadowFill] = []
        for price, level_size in sorted(snapshot.bids, key=lambda row: row[0], reverse=True):
            if remaining <= ZERO:
                break
            take = min(remaining, level_size)
            if take <= ZERO:
                continue
            # A synthetic order is retained in the ledger for an auditable
            # exit path, but no network/execution client is involved.
            order = next(
                (
                    row
                    for row in self.orders
                    if row.side is ShadowSide.SELL
                    and row.token_id == snapshot.token_id
                    and row.is_active
                ),
                None,
            )
            if order is None:
                try:
                    order = self.submit_limit(
                        snapshot,
                        side=ShadowSide.SELL,
                        limit_price=price,
                        shares=take,
                        maker_assumption=False,
                        risk_exit=True,
                        idempotency_key=f"{snapshot.event_id}:{snapshot.timestamp.isoformat()}:taker:{price}",
                        trigger_reason=reason,
                    )
                except ShadowOrderRejected:
                    # A sub-minimum residual cannot be represented as an
                    # executable order; fail closed and leave inventory open.
                    break
            fill = self._apply_fill(
                order,
                timestamp=snapshot.timestamp,
                shares=take,
                price=price,
                model=FillModel.TRADE_THROUGH,
                source="emergency_taker_depth",
                asset_id=snapshot.token_id,
            )
            if fill is not None:
                fills.append(fill)
            remaining -= take
        return tuple(fills)

    def _find(self, order_id: str) -> ShadowOrder:
        if self.ledger is not None:
            try:
                order = self.ledger.orders[order_id]
            except KeyError as exc:
                raise KeyError(f"unknown shadow order {order_id}") from exc
            self._assert_order_scope(order)
            return order
        try:
            order = self._orders[order_id]
        except (AttributeError, KeyError) as exc:
            raise KeyError(f"unknown shadow order {order_id}") from exc
        self._assert_order_scope(order)
        return order


@dataclass
class StationDayRiskManager:
    """Aggregate a station-day budget without owning any token inventory.

    The manager intentionally receives engines as inputs.  It may sum their
    exposure, but it never routes a fill, mutates inventory, or supplies a
    cost basis.  That separation prevents a station-day statistical cluster
    from becoming an execution portfolio.
    """

    station_id: str
    market_day: str
    budget_usd: Decimal
    budget_mode: StationDayBudgetMode = StationDayBudgetMode.CUMULATIVE_BUY_COST
    halted: bool = False
    halt_reason: str | None = None
    discrepancies: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.budget_usd = _decimal(self.budget_usd)
        self.budget_mode = StationDayBudgetMode(str(self.budget_mode))
        if self.budget_usd <= ZERO:
            raise ValueError("station-day budget must be positive")

    def _matching_engines(
        self, engines: Iterable[ShadowOrderEngine]
    ) -> tuple[ShadowOrderEngine, ...]:
        output: list[ShadowOrderEngine] = []
        for engine in engines:
            key = engine.portfolio_key
            if key is None:
                raise ShadowPortfolioInvariantError(
                    "unscoped_engine_cannot_join_station_day_risk_manager",
                    details={"station_id": self.station_id, "market_day": self.market_day},
                )
            if key.market_day != self.market_day:
                raise ShadowPortfolioInvariantError(
                    "portfolio_market_day_mismatch",
                    details={
                        "expected_market_day": self.market_day,
                        "actual_portfolio_key": key.as_dict(),
                    },
                )
            output.append(engine)
        return tuple(output)

    def active_reserved_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        return sum((engine.active_reserved_usd for engine in self._matching_engines(engines)), start=ZERO)

    def inventory_cost_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        return sum((engine.inventory_cost_usd for engine in self._matching_engines(engines)), start=ZERO)

    def cumulative_buy_cost_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        return sum(
            (engine.cumulative_buy_cost_usd for engine in self._matching_engines(engines)),
            start=ZERO,
        )

    def current_open_exposure_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        """Current inventory cost plus still-resting BUY reservations.

        This is always reported even when the configured daily cap uses
        cumulative buy cost.  A completed exit therefore visibly releases
        *current* exposure without silently changing the configured daily
        budget policy.
        """
        matching = self._matching_engines(engines)
        return sum((engine.total_risk_usd for engine in matching), start=ZERO)

    def budget_used_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        matching = self._matching_engines(engines)
        basis = (
            sum((engine.cumulative_buy_cost_usd for engine in matching), start=ZERO)
            if self.budget_mode is StationDayBudgetMode.CUMULATIVE_BUY_COST
            else sum((engine.inventory_cost_usd for engine in matching), start=ZERO)
        )
        return basis + sum((engine.active_reserved_usd for engine in matching), start=ZERO)

    def available_budget_usd(self, engines: Iterable[ShadowOrderEngine]) -> Decimal:
        return max(ZERO, self.budget_usd - self.budget_used_usd(engines))

    def can_reserve_buy(
        self,
        engines: Iterable[ShadowOrderEngine],
        *,
        requested_usd: Decimal | float | str,
        estimated_fee_usd: Decimal | float | str = ZERO,
    ) -> bool:
        if self.halted:
            return False
        requested = _decimal(requested_usd)
        fee = _decimal(estimated_fee_usd)
        if requested <= ZERO:
            return False
        # Both views are hard constraints.  The configured cumulative policy
        # may be stricter than current exposure after an exit, but it must
        # never let active BUY reservations plus token inventory exceed $200.
        return (
            self.current_open_exposure_usd(engines) + requested + fee <= self.budget_usd
            and self.budget_used_usd(engines) + requested + fee <= self.budget_usd
        )

    def halt(self, code: str, *, details: Mapping[str, Any]) -> None:
        self.halted = True
        self.halt_reason = code
        self.discrepancies.append({"code": code, "details": _json_value(dict(details))})

    def assert_conservation(self, engines: Iterable[ShadowOrderEngine]) -> None:
        for engine in self._matching_engines(engines):
            if engine.inventory_shares < ZERO or engine.inventory_cost_usd < ZERO:
                details = {
                    "portfolio_key": engine.portfolio_key.as_dict() if engine.portfolio_key else None,
                    "inventory_shares": str(engine.inventory_shares),
                    "inventory_cost_usd": str(engine.inventory_cost_usd),
                }
                self.halt("negative_token_inventory", details=details)
                raise ShadowPortfolioInvariantError("negative_token_inventory", details=details)
        current_open_exposure = self.current_open_exposure_usd(engines)
        if current_open_exposure > self.budget_usd:
            details = {
                "station_id": self.station_id,
                "market_day": self.market_day,
                "current_open_exposure_usd": str(current_open_exposure),
                "budget_usd": str(self.budget_usd),
            }
            self.halt("station_day_current_exposure_exceeded", details=details)
            raise ShadowPortfolioInvariantError(
                "station_day_current_exposure_exceeded", details=details
            )
        if self.budget_used_usd(engines) > self.budget_usd:
            details = {
                "station_id": self.station_id,
                "market_day": self.market_day,
                "budget_used_usd": str(self.budget_used_usd(engines)),
                "budget_usd": str(self.budget_usd),
            }
            self.halt("station_day_budget_exceeded", details=details)
            raise ShadowPortfolioInvariantError("station_day_budget_exceeded", details=details)

    def as_dict(self, engines: Iterable[ShadowOrderEngine]) -> dict[str, Any]:
        matching = self._matching_engines(engines)
        return {
            "station_id": self.station_id,
            "market_day": self.market_day,
            "budget_usd": str(self.budget_usd),
            "budget_mode": str(self.budget_mode),
            "inventory_cost_usd": str(sum((engine.inventory_cost_usd for engine in matching), start=ZERO)),
            "cumulative_buy_cost_usd": str(
                sum((engine.cumulative_buy_cost_usd for engine in matching), start=ZERO)
            ),
            "current_open_exposure_usd": str(self.current_open_exposure_usd(matching)),
            "active_reserved_usd": str(
                sum((engine.active_reserved_usd for engine in matching), start=ZERO)
            ),
            "budget_used_usd": str(self.budget_used_usd(matching)),
            "available_budget_usd": str(self.available_budget_usd(matching)),
            "portfolio_count": len(matching),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "discrepancies": list(self.discrepancies),
        }


@dataclass(frozen=True, slots=True)
class ReplenishDecision:
    allowed: bool
    mode: ReplenishMode
    reason: str
    budget_remaining_usd: Decimal
    weighted_average_cost: Decimal | None


def decide_replenish(
    *,
    mode: ReplenishMode | str,
    current_ask: Decimal | float | str,
    target_price: Decimal | float | str,
    prior_order_resolved: bool,
    weather_improving: bool = False,
    weather_unchanged: bool = False,
    weather_worsening: bool = False,
    health_ok: bool = True,
    season_version_valid: bool = True,
    budget_remaining_usd: Decimal | float | str,
    weighted_average_cost: Decimal | float | str | None = None,
    duplicate_event: bool = False,
    spread_ok: bool = True,
    book_ok: bool = True,
) -> ReplenishDecision:
    """Apply the separate confirmation/dip/no-averaging-down gates."""
    mode_value = ReplenishMode(str(mode))
    budget = _decimal(budget_remaining_usd)
    average = _decimal(weighted_average_cost) if weighted_average_cost is not None else None
    reason = "allowed"
    allowed = True
    if mode_value is ReplenishMode.NONE:
        allowed, reason = False, "replenish_disabled"
    elif not prior_order_resolved:
        allowed, reason = False, "prior_order_unresolved"
    elif duplicate_event:
        allowed, reason = False, "duplicate_event"
    elif weather_worsening:
        allowed, reason = False, ShadowOrderReason.WEATHER_WORSENING.value
    elif not health_ok:
        allowed, reason = False, "health_gate"
    elif not season_version_valid:
        allowed, reason = False, ShadowOrderReason.INVALID_SEASON_VERSION.value
    elif budget <= ZERO:
        allowed, reason = False, ShadowOrderReason.BUDGET.value
    elif not spread_ok or not book_ok:
        allowed, reason = False, "spread_or_book_gate"
    elif _decimal(target_price) <= _decimal(current_ask):
        allowed, reason = False, "no_positive_space_to_target"
    elif average is not None and _decimal(target_price) <= average:
        # An additional fill is useful only when the resulting inventory can
        # still reach the configured exit above its weighted cost.
        allowed, reason = False, "no_positive_space_to_target"
    elif mode_value is ReplenishMode.CONFIRMATION and not weather_improving:
        allowed, reason = False, "confirmation_missing"
    elif mode_value is ReplenishMode.CONDITIONAL_DIP and not weather_unchanged:
        allowed, reason = False, "weather_changed"
    return ReplenishDecision(allowed, mode_value, reason, budget, average)


def validate_inventory_schedule(
    tranches_usd: Sequence[Decimal | float | str], *, budget_usd: Decimal | float | str = "200"
) -> tuple[Decimal, ...]:
    """Validate a finite schedule without silently changing its amounts."""
    values = tuple(_decimal(value) for value in tranches_usd)
    budget = _decimal(budget_usd)
    if not values or any(value <= ZERO for value in values):
        raise ValueError("inventory schedule must contain positive tranches")
    if budget <= ZERO or sum(values, start=ZERO) > budget:
        raise ValueError("inventory schedule exceeds its finite budget")
    return values


def exit_action(
    snapshot: BookSnapshot,
    *,
    average_cost: Decimal | float | str,
    submitted_at: datetime,
    policy: ShadowExitPolicy,
    weather_invalidated: bool = False,
    stale_risk: bool = False,
    local_eod: bool = False,
) -> str | None:
    """Return a maker-first exit reason based only on current/later data."""
    policy.validate()
    cost = _decimal(average_cost)
    if weather_invalidated:
        return "weather_invalidation"
    if stale_risk or not snapshot.health_ok:
        return "stale_or_health_risk"
    if policy.max_hold is not None and snapshot.timestamp - _utc(submitted_at) >= policy.max_hold:
        return "time_exit"
    if local_eod and policy.force_local_eod:
        return "local_eod"
    if snapshot.best_bid is not None and any(
        snapshot.best_bid >= cost + rise for rise in policy.target_rises
    ):
        return "target"
    return None


@dataclass(frozen=True, slots=True)
class ExitLeg:
    fraction: Decimal
    target_price: Decimal
    urgent: bool = False
    reason: str = "target"


def build_exit_plan(
    *,
    inventory_shares: Decimal | float | str,
    average_cost: Decimal | float | str,
    targets: Sequence[Decimal | float | str] = ("0.05", "0.10", "0.13"),
    protection_price: Decimal | float | str | None = None,
    partial_first_fraction: Decimal | float | str = Decimal("0.5"),
) -> tuple[ExitLeg, ...]:
    """Create finite partial exits; normal maker legs have zero fee by role."""
    shares = _decimal(inventory_shares)
    cost = _decimal(average_cost)
    if shares <= ZERO:
        return ()
    ordered = tuple(sorted({_decimal(value) for value in targets}))
    if not ordered:
        return ()
    first_fraction = min(ONE, max(ZERO, _decimal(partial_first_fraction)))
    first_target = cost + ordered[0]
    legs = [ExitLeg(first_fraction, first_target, reason="partial_target")]
    remaining = ONE - first_fraction
    later_targets = ordered[1:]
    if later_targets and remaining > ZERO:
        leg_fraction = remaining / Decimal(len(later_targets))
        for index, rise in enumerate(later_targets):
            fraction = remaining - leg_fraction * Decimal(index) if index == len(later_targets) - 1 else leg_fraction
            legs.append(ExitLeg(fraction, cost + rise, reason="target"))
        remaining = ZERO
    if remaining > ZERO:
        legs.append(ExitLeg(remaining, cost + ordered[-1], reason="target"))
    if protection_price is not None:
        legs.append(ExitLeg(ONE, _decimal(protection_price), urgent=True, reason="protection"))
    return tuple(legs)


@dataclass(frozen=True, slots=True)
class StopLossResult:
    touched: bool
    queue_aware_executable: bool
    trade_through_executable: bool
    first_post_gap_bid: Decimal | None
    lowest_bid_before_collapse: Decimal | None
    simulated_loss_per_share: Decimal | None
    note: str

    @property
    def first_executable_post_gap_price(self) -> Decimal | None:
        """Alias used by reports; it is observed bid evidence, not a guarantee."""
        return self.first_post_gap_bid

    @property
    def actual_simulated_loss(self) -> Decimal | None:
        return self.simulated_loss_per_share


def evaluate_stop_loss(
    *,
    entry_price: Decimal | float | str,
    stop_price: Decimal | float | str,
    snapshots: Sequence[BookSnapshot],
    trades: Sequence[TradeEvent] = (),
    token_id: str,
    entry_time: datetime,
) -> StopLossResult:
    """Report quote-touch and model fills without claiming a stop guarantee."""
    entry = _decimal(entry_price)
    stop = _decimal(stop_price)
    later = sorted(
        (row for row in snapshots if row.token_id == token_id and row.timestamp > _utc(entry_time)),
        key=lambda row: row.timestamp,
    )
    bids = [row.best_bid for row in later if row.best_bid is not None]
    touched = any(bid <= stop for bid in bids)
    first = bids[0] if bids else None
    lowest = min(bids) if bids else None
    # A stop is not a resting order in this diagnostic.  We only call the
    # model executable when an actual opposite taker trade crosses it.
    relevant = [
        trade
        for trade in trades
        if trade.asset_id == token_id and trade.timestamp > _utc(entry_time)
    ]
    queue_executable = any(
        trade.side is ShadowSide.BUY and trade.price >= stop for trade in relevant
    )
    trade_through = any(
        trade.side is ShadowSide.BUY and trade.price > stop for trade in relevant
    )
    loss = entry - first if first is not None and first < entry else ZERO if first is not None else None
    return StopLossResult(
        touched=touched,
        queue_aware_executable=queue_executable,
        trade_through_executable=trade_through,
        first_post_gap_bid=first,
        lowest_bid_before_collapse=lowest,
        simulated_loss_per_share=loss,
        note="Observed bids are not a guarantee that a user stop order would have filled.",
    )


def maker_fee_usdc(shares: Decimal | float | str, price: Decimal | float | str) -> Decimal:
    """Maker fee is zero under the configured Polymarket weather schedule."""
    return trading_fee_usdc(shares, price, liquidity_role=LiquidityRole.MAKER, market_category="weather")


def taker_fee_usdc(shares: Decimal | float | str, price: Decimal | float | str) -> Decimal:
    """Official weather taker fee helper for emergency exits only."""
    return trading_fee_usdc(shares, price, liquidity_role=LiquidityRole.TAKER, market_category="weather")


def inventory_risk_summary(engine: ShadowOrderEngine) -> dict[str, Any]:
    """Machine-readable finite-budget/inventory state for status and reports."""
    return {
        "execution_enabled": False,
        "budget_usd": float(engine.budget_usd),
        "inventory_shares": float(engine.inventory_shares),
        "inventory_cost_usd": float(engine.inventory_cost_usd),
        "cumulative_buy_cost_usd": float(engine.cumulative_buy_cost_usd),
        "average_inventory_cost": (
            float(engine.average_inventory_cost) if engine.average_inventory_cost is not None else None
        ),
        "active_reserved_usd": float(engine.active_reserved_usd),
        "budget_used_usd": float(engine.budget_usd - engine.available_budget_usd),
        "total_risk_usd": float(engine.total_risk_usd),
        "available_budget_usd": float(engine.available_budget_usd),
        "realized_pnl_usd": float(engine.realized_pnl_usd),
        "realized_pnl_by_exit_source": {
            source: float(value)
            for source, value in sorted(engine.realized_pnl_by_exit_source.items())
        },
        "realized_pnl_by_exit_reason": {
            reason: float(value)
            for reason, value in sorted(engine.realized_pnl_by_exit_reason.items())
        },
        "fees_usd": float(engine.fees_usd),
        "round_trip_count": engine.round_trip_count,
        "active_orders": len(engine.active_orders),
        "order_count": len(engine.orders),
        "rejection_reasons": list(engine.rejection_reasons),
    }
