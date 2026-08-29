"""Read-only QUIET-window maker replay and causal diagnostics.

This module is intentionally a separate strategy family.  It shares the
existing token-scoped :mod:`shadow_orders` fill model, but it does not share
the weather lead-lag follower's ledger, decisions, inventory, or statistics.
The only cross-family state is a station/market-day risk view whose purpose is
to enforce the common $200 cap.

There is no exchange client in this module.  A result is a historical model
estimate based on archived token-native books and public trades, never an
order acknowledgement or a claim that a maker order would have filled.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.information_clock import (
    ImpactClass,
    InformationClock,
    InformationEvent,
    deduplicate_information_events,
)
from poly_weather.market_microstructure import L2ChurnIndex, TradeIntensityTracker
from poly_weather.market_regime import (
    MarketMetrics,
    MarketRegimeStateMachine,
    RegimeState,
    RegimeThresholds,
    ThresholdTier,
    derive_predictable_release,
    metrics_from_snapshot,
    predefined_regime_thresholds,
    reaction_duration_summary,
)
from poly_weather.no_forward import wilson_interval
from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    QuoteMode,
    ReplenishMode,
    ShadowFill,
    ShadowLedger,
    ShadowOrder,
    ShadowOrderEngine,
    ShadowOrderRejected,
    ShadowPortfolioInvariantError,
    ShadowSide,
    StationDayBudgetMode,
    TokenPortfolioKey,
    TradeEvent,
    quote_ladder,
    quote_limit,
)

ZERO = Decimal("0")
GLOBAL_INFORMATION_KINDS = frozenset({"official_status", "market_health_status"})
ONE = Decimal("1")
QUIET_FAMILY = "quiet_window_noise_maker"
LEGACY_QUIET_V1_LEDGER = Path("data/raw/shadow_orders/shadow_orders_quiet_window_v1_token_scoped.jsonl")
DEFAULT_QUIET_LEDGER = Path("data/raw/shadow_orders/shadow_orders_quiet_window_v2_token_scoped.jsonl")
DEFAULT_FAMILY_LEDGER_IDENTITIES = {
    "weather_lead_lag_maker": "data/raw/shadow_orders/shadow_orders_v2_token_scoped.jsonl",
    QUIET_FAMILY: str(DEFAULT_QUIET_LEDGER),
    "complement_pair_maker": "data/raw/shadow_orders/complement_pair_shadow_ledger.jsonl",
}


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
        else:
            if abs(number) > 10**14:
                number /= 1_000_000_000
            elif abs(number) > 10**11:
                number /= 1_000
            try:
                parsed = datetime.fromtimestamp(number, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _required_utc(value: Any) -> datetime:
    parsed = _utc(value)
    if parsed is None:
        raise ValueError(f"invalid timestamp: {value!r}")
    return parsed


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _flag(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _rate(successes: int, sample_count: int) -> dict[str, Any]:
    interval = wilson_interval(successes, sample_count)
    return {
        "count": successes,
        "sample_count": sample_count,
        "rate": successes / sample_count if sample_count else None,
        "wilson_low": interval[0] if interval else None,
        "wilson_high": interval[1] if interval else None,
        "statistically_unreliable": sample_count < 30,
    }


def _summary(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"sample_count": 0, "mean": None, "p50": None, "p90": None}

    def percentile(q: float) -> float:
        position = q * (len(ordered) - 1)
        lower = int(position)
        upper = min(len(ordered) - 1, lower + 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "sample_count": len(ordered),
        "mean": fmean(ordered),
        "p50": median(ordered),
        "p90": percentile(0.90),
    }


def _as_snapshot(value: BookSnapshot | Mapping[str, Any]) -> BookSnapshot:
    if isinstance(value, BookSnapshot):
        return value
    # Keep archive-row compatibility with the existing paired-book loader,
    # including its station/day metadata enrichment.
    from poly_weather.shadow_spread_replay import _row_snapshot

    return _row_snapshot(value)


def normalise_quiet_snapshots(
    values: Iterable[BookSnapshot | Mapping[str, Any]],
) -> tuple[BookSnapshot, ...]:
    """Materialize only the token-native fields needed by a QUIET replay."""
    return tuple(sorted((_as_snapshot(value) for value in values), key=lambda row: row.timestamp))


@dataclass(frozen=True, slots=True)
class QuietSnapshotStore:
    """Disk-backed chronological snapshot source for large offline replays."""

    path: Path
    snapshot_count: int
    start_at: datetime
    end_at: datetime
    scopes: tuple[tuple[str, str], ...] = ()
    token_ids: tuple[str, ...] = ()

    def iter_snapshots(self) -> Iterator[BookSnapshot]:
        connection = sqlite3.connect(self.path)
        try:
            cursor = connection.execute(
                "SELECT payload FROM snapshots ORDER BY timestamp, sequence"
            )
            for (payload_text,) in cursor:
                payload = json.loads(payload_text)
                if not isinstance(payload, Mapping):
                    raise ValueError("quiet snapshot store row is not an object")
                yield _as_snapshot(payload)
        finally:
            connection.close()

    def snapshot_times_by_token(self) -> dict[str, tuple[datetime, ...]]:
        """Return exact replay timestamps for sparse causal L2 aggregation."""
        values: dict[str, list[datetime]] = defaultdict(list)
        connection = sqlite3.connect(self.path)
        try:
            cursor = connection.execute(
                "SELECT token_id, timestamp_iso FROM snapshots "
                "ORDER BY token_id, timestamp, sequence"
            )
            for token_id, timestamp_text in cursor:
                try:
                    timestamp = datetime.fromisoformat(str(timestamp_text))
                except (TypeError, ValueError):
                    continue
                if timestamp.tzinfo is None:
                    continue
                values[str(token_id)].append(timestamp.astimezone(UTC))
        finally:
            connection.close()
        return {token_id: tuple(rows) for token_id, rows in values.items()}


def _snapshot_store_payload(snapshot: BookSnapshot) -> dict[str, Any]:
    return _json_value(
        {
            "timestamp": snapshot.timestamp,
            "event_id": snapshot.event_id,
            "market_id": snapshot.market_id,
            "token_id": snapshot.token_id,
            "bids": snapshot.bids,
            "asks": snapshot.asks,
            "station_id": snapshot.station_id,
            "market_day": snapshot.market_day,
            "tick_size": snapshot.tick_size,
            "min_order_size": snapshot.min_order_size,
            "book_complete": snapshot.book_complete,
            "market_stale": snapshot.market_stale,
            "weather_stale": snapshot.weather_stale,
            "settlement_verified": snapshot.settlement_verified,
            "in_season": snapshot.in_season,
            "season_version": snapshot.season_version,
            "warming_valid": snapshot.warming_valid,
            "upstream_status": snapshot.upstream_status,
            "quality_excluded": snapshot.quality_excluded,
            "metadata": snapshot.metadata,
        }
    )


def write_quiet_snapshot_store(
    values: Iterable[BookSnapshot | Mapping[str, Any]],
    path: Path | str,
) -> QuietSnapshotStore:
    """Spool full token-native books while retaining no archive-wide object graph."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    first: datetime | None = None
    last: datetime | None = None
    scopes: set[tuple[str, str]] = set()
    token_ids: set[str] = set()
    with sqlite3.connect(target) as connection:
        connection.execute(
            "CREATE TABLE snapshots ("
            "timestamp REAL NOT NULL, sequence INTEGER NOT NULL, "
            "token_id TEXT NOT NULL, timestamp_iso TEXT NOT NULL, payload TEXT NOT NULL"
            ")"
        )
        for value in values:
            snapshot = _as_snapshot(value)
            payload = json.dumps(_snapshot_store_payload(snapshot), separators=(",", ":"))
            connection.execute(
                "INSERT INTO snapshots("
                "timestamp, sequence, token_id, timestamp_iso, payload"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.timestamp.timestamp(),
                    count,
                    snapshot.token_id,
                    snapshot.timestamp.isoformat(),
                    payload,
                ),
            )
            if first is None or snapshot.timestamp < first:
                first = snapshot.timestamp
            if last is None or snapshot.timestamp > last:
                last = snapshot.timestamp
            scopes.add(
                (
                    snapshot.station_id or "unknown",
                    snapshot.market_day or snapshot.timestamp.date().isoformat(),
                )
            )
            token_ids.add(snapshot.token_id)
            count += 1
        connection.execute(
            "CREATE INDEX snapshots_timestamp_sequence_idx "
            "ON snapshots(timestamp, sequence)"
        )
        connection.execute(
            "CREATE INDEX snapshots_token_timestamp_idx "
            "ON snapshots(token_id, timestamp, sequence)"
        )
    if count == 0 or first is None or last is None:
        raise ValueError("quiet snapshot store cannot be empty")
    return QuietSnapshotStore(
        target,
        count,
        first,
        last,
        tuple(sorted(scopes)),
        tuple(sorted(token_ids)),
    )


def _as_trade(value: TradeEvent | Mapping[str, Any]) -> TradeEvent:
    if isinstance(value, TradeEvent):
        return value
    return TradeEvent.from_mapping(value)


class StrategyFamily(StrEnum):
    WEATHER_LEAD_LAG = "weather_lead_lag_maker"
    QUIET_WINDOW = QUIET_FAMILY
    COMPLEMENT_PAIR = "complement_pair_maker"


@dataclass(frozen=True, slots=True)
class QuietWindowConfig:
    """A finite, versioned candidate configuration for one replay family."""

    version: str = "quiet-window-v2-token-scoped"
    strategy_family: str = QUIET_FAMILY
    quote_mode: QuoteMode = QuoteMode.BEST_BID
    fill_model: FillModel = FillModel.QUEUE_AWARE
    quote_size_usd: Decimal = Decimal("20")
    size_scenarios: tuple[Decimal, ...] = (
        Decimal("20"),
        Decimal("50"),
        Decimal("100"),
        Decimal("200"),
    )
    ladder_ticks: tuple[int, ...] = (0, -1)
    exit_rises: tuple[Decimal, ...] = (
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
        Decimal("0.05"),
    )
    partial_exit_fraction: Decimal = Decimal("0.50")
    order_timeout: timedelta = timedelta(minutes=15)
    max_hold: timedelta | None = timedelta(hours=4)
    max_active_orders: int = 2
    station_day_budget_usd: Decimal = Decimal("200")
    station_day_budget_mode: StationDayBudgetMode = StationDayBudgetMode.CUMULATIVE_BUY_COST
    pre_release_offsets_minutes: tuple[int, ...] = (1, 3, 5, 10)
    threshold_tier: ThresholdTier = ThresholdTier.NEUTRAL
    allow_conditional_replenishment: bool = True
    max_replenishments: int = 1
    anchor_change_tolerance: Decimal = Decimal("0.02")
    primary_stations: tuple[str, ...] = ("KLAX", "KLGA")
    execution_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "quote_mode", QuoteMode(str(self.quote_mode)))
        object.__setattr__(self, "fill_model", FillModel(str(self.fill_model)))
        object.__setattr__(self, "threshold_tier", ThresholdTier(self.threshold_tier))
        object.__setattr__(
            self,
            "station_day_budget_mode",
            StationDayBudgetMode(str(self.station_day_budget_mode)),
        )
        object.__setattr__(self, "quote_size_usd", _decimal(self.quote_size_usd))
        object.__setattr__(
            self,
            "size_scenarios",
            tuple(_decimal(value) for value in self.size_scenarios),
        )
        object.__setattr__(
            self,
            "exit_rises",
            tuple(_decimal(value) for value in self.exit_rises),
        )
        object.__setattr__(self, "partial_exit_fraction", _decimal(self.partial_exit_fraction))
        object.__setattr__(
            self,
            "station_day_budget_usd",
            _decimal(self.station_day_budget_usd),
        )
        object.__setattr__(
            self,
            "anchor_change_tolerance",
            _decimal(self.anchor_change_tolerance),
        )
        object.__setattr__(
            self,
            "primary_stations",
            tuple(str(station).upper() for station in self.primary_stations),
        )
        self.validate()

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("quiet-window strategy version is required")
        if self.strategy_family != QUIET_FAMILY:
            raise ValueError("quiet-window config cannot use another strategy family")
        if self.execution_enabled is not False:
            raise AssertionError("QUIET window strategy is read-only")
        if self.quote_size_usd is None or self.quote_size_usd <= ZERO:
            raise ValueError("quote_size_usd must be positive")
        if (
            self.station_day_budget_usd is None
            or self.station_day_budget_usd <= ZERO
        ):
            raise ValueError("station_day_budget_usd must be positive")
        if self.quote_size_usd > self.station_day_budget_usd:
            raise ValueError("quote_size_usd exceeds station-day budget")
        if not self.size_scenarios or any(
            value is None or value <= ZERO or value > self.station_day_budget_usd
            for value in self.size_scenarios
        ):
            raise ValueError("size_scenarios must be positive and within the budget")
        if not self.exit_rises or any(value is None or value <= ZERO for value in self.exit_rises):
            raise ValueError("exit_rises must contain positive values")
        if not ZERO < self.partial_exit_fraction <= ONE:
            raise ValueError("partial_exit_fraction must be in (0, 1]")
        if self.order_timeout <= timedelta(0):
            raise ValueError("order_timeout must be positive")
        if self.max_hold is not None and self.max_hold <= timedelta(0):
            raise ValueError("max_hold must be positive")
        if self.max_active_orders < 1:
            raise ValueError("max_active_orders must be positive")
        if self.max_replenishments < 0:
            raise ValueError("max_replenishments cannot be negative")
        if self.anchor_change_tolerance is None or self.anchor_change_tolerance < ZERO:
            raise ValueError("anchor_change_tolerance cannot be negative")
        if any(value < 0 for value in self.pre_release_offsets_minutes):
            raise ValueError("pre-release offsets cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "version": self.version,
                "strategy_family": self.strategy_family,
                "quote_mode": self.quote_mode,
                "fill_model": self.fill_model,
                "quote_size_usd": self.quote_size_usd,
                "size_scenarios": self.size_scenarios,
                "ladder_ticks": self.ladder_ticks,
                "exit_rises": self.exit_rises,
                "partial_exit_fraction": self.partial_exit_fraction,
                "order_timeout_seconds": self.order_timeout.total_seconds(),
                "max_hold_seconds": self.max_hold.total_seconds() if self.max_hold else None,
                "max_active_orders": self.max_active_orders,
                "station_day_budget_usd": self.station_day_budget_usd,
                "station_day_budget_mode": self.station_day_budget_mode,
                "pre_release_offsets_minutes": self.pre_release_offsets_minutes,
                "threshold_tier": self.threshold_tier,
                "allow_conditional_replenishment": self.allow_conditional_replenishment,
                "max_replenishments": self.max_replenishments,
                "anchor_change_tolerance": self.anchor_change_tolerance,
                "primary_stations": self.primary_stations,
                "execution_enabled": False,
            }
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> QuietWindowConfig:
        if _flag(payload.get("execution_enabled")):
            raise AssertionError("QUIET window strategy cannot enable execution")

        def seconds(key: str, default: float | None) -> timedelta | None:
            value = payload.get(key)
            if value is None:
                return None if default is None else timedelta(seconds=default)
            return timedelta(seconds=float(value))

        config = cls(
            version=str(payload.get("version") or "quiet-window-v2-token-scoped"),
            strategy_family=str(payload.get("strategy_family") or QUIET_FAMILY),
            quote_mode=QuoteMode(str(payload.get("quote_mode") or QuoteMode.BEST_BID)),
            fill_model=FillModel(str(payload.get("fill_model") or FillModel.QUEUE_AWARE)),
            quote_size_usd=_decimal(payload.get("quote_size_usd", "20")),
            size_scenarios=tuple(
                _decimal(value)
                for value in payload.get("size_scenarios", ("20", "50", "100", "200"))
            ),
            ladder_ticks=tuple(int(value) for value in payload.get("ladder_ticks", (0, -1))),
            exit_rises=tuple(
                _decimal(value)
                for value in payload.get("exit_rises", ("0.01", "0.02", "0.03", "0.05"))
            ),
            partial_exit_fraction=_decimal(payload.get("partial_exit_fraction", "0.50")),
            order_timeout=seconds("order_timeout_seconds", 900) or timedelta(minutes=15),
            max_hold=seconds("max_hold_seconds", 14400),
            max_active_orders=int(payload.get("max_active_orders", 2)),
            station_day_budget_usd=_decimal(payload.get("station_day_budget_usd", "200")),
            station_day_budget_mode=StationDayBudgetMode(
                str(
                    payload.get(
                        "station_day_budget_mode",
                        StationDayBudgetMode.CUMULATIVE_BUY_COST,
                    )
                )
            ),
            pre_release_offsets_minutes=tuple(
                int(value) for value in payload.get("pre_release_offsets_minutes", (1, 3, 5, 10))
            ),
            threshold_tier=ThresholdTier(
                str(payload.get("threshold_tier") or ThresholdTier.NEUTRAL)
            ),
            allow_conditional_replenishment=_flag(
                payload.get("allow_conditional_replenishment"), default=True
            ),
            max_replenishments=int(payload.get("max_replenishments", 1)),
            anchor_change_tolerance=_decimal(
                payload.get("anchor_change_tolerance", "0.02")
            ),
            primary_stations=tuple(payload.get("primary_stations", ("KLAX", "KLGA"))),
            execution_enabled=False,
        )
        config.validate()
        return config


def default_quiet_window_config() -> QuietWindowConfig:
    return QuietWindowConfig()


@dataclass(frozen=True, slots=True)
class StrategyFamilyRegistration:
    family: str
    version: str
    ledger_identity: str

    def as_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "version": self.version,
            "ledger_identity": self.ledger_identity,
        }


@dataclass
class ThreeStrategyRiskBook:
    """Cross-family station/day cap with no shared inventory or cost basis."""

    cap_usd: Decimal = Decimal("200")
    registrations: dict[str, StrategyFamilyRegistration] = field(default_factory=dict)
    entries: dict[tuple[str, str, str], dict[str, Decimal | int]] = field(default_factory=dict)
    discrepancies: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cap_usd = _decimal(self.cap_usd)
        if self.cap_usd is None or self.cap_usd <= ZERO:
            raise ValueError("risk cap must be positive")
        if not self.registrations:
            for family, identity in DEFAULT_FAMILY_LEDGER_IDENTITIES.items():
                self.register_family(
                    family,
                    version=f"{family}-independent-v1",
                    ledger_identity=identity,
                )
        self.assert_isolation()

    def register_family(self, family: str, *, version: str, ledger_identity: str) -> None:
        family = str(family)
        identity = str(ledger_identity)
        if not family or not version.strip() or not identity:
            raise ValueError("strategy family registration is incomplete")
        for other_family, registration in self.registrations.items():
            if other_family != family and registration.ledger_identity == identity:
                raise ShadowPortfolioInvariantError(
                    "strategy_family_ledger_not_isolated",
                    details={
                        "family": family,
                        "other_family": other_family,
                        "ledger_identity": identity,
                    },
                )
        self.registrations[family] = StrategyFamilyRegistration(family, version, identity)

    def assert_isolation(self) -> None:
        identities = [row.ledger_identity for row in self.registrations.values()]
        if len(identities) != len(set(identities)):
            raise ShadowPortfolioInvariantError(
                "strategy_family_ledger_not_isolated",
                details={"registrations": [row.as_dict() for row in self.registrations.values()]},
            )

    def update(
        self,
        family: str,
        station_id: str,
        market_day: str,
        *,
        inventory_cost_usd: Decimal | float | str,
        active_reserved_usd: Decimal | float | str,
        cumulative_buy_cost_usd: Decimal | float | str,
        portfolio_count: int,
    ) -> None:
        if family not in self.registrations:
            raise ShadowPortfolioInvariantError(
                "unregistered_strategy_family",
                details={"family": family},
            )
        values = {
            "inventory_cost_usd": _decimal(inventory_cost_usd) or ZERO,
            "active_reserved_usd": _decimal(active_reserved_usd) or ZERO,
            "cumulative_buy_cost_usd": _decimal(cumulative_buy_cost_usd) or ZERO,
            "portfolio_count": int(portfolio_count),
        }
        if any(
            value < ZERO
            for key, value in values.items()
            if key != "portfolio_count" and isinstance(value, Decimal)
        ):
            raise ShadowPortfolioInvariantError(
                "negative_strategy_risk_measure",
                details={"family": family, "station_id": station_id, "market_day": market_day},
            )
        self.entries[(family, station_id.upper(), market_day)] = values

    def _rows(self, station_id: str, market_day: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "family": family,
                **values,
            }
            for (family, station, day), values in self.entries.items()
            if station == station_id.upper() and day == market_day
        )

    def assert_station_day(self, station_id: str, market_day: str) -> dict[str, Any]:
        rows = self._rows(station_id, market_day)
        current_open = sum(
            (
                row["inventory_cost_usd"] + row["active_reserved_usd"]
                for row in rows
            ),
            start=ZERO,
        )
        cumulative_with_reservations = sum(
            (
                row["cumulative_buy_cost_usd"] + row["active_reserved_usd"]
                for row in rows
            ),
            start=ZERO,
        )
        summary = {
            "station_id": station_id.upper(),
            "market_day": market_day,
            "cap_usd": self.cap_usd,
            "current_open_exposure_usd": current_open,
            "cumulative_buy_cost_plus_reservations_usd": cumulative_with_reservations,
            "family_rows": rows,
            "within_cap": current_open <= self.cap_usd
            and cumulative_with_reservations <= self.cap_usd,
        }
        if not summary["within_cap"]:
            self.discrepancies.append(
                {
                    "code": "three_strategy_station_day_cap_exceeded",
                    "details": _json_value(summary),
                }
            )
            raise ShadowPortfolioInvariantError(
                "three_strategy_station_day_cap_exceeded",
                details=_json_value(summary),
            )
        return summary

    def can_reserve(
        self,
        family: str,
        station_id: str,
        market_day: str,
        *,
        requested_usd: Decimal | float | str,
        estimated_fee_usd: Decimal | float | str = ZERO,
    ) -> bool:
        requested = _decimal(requested_usd) or ZERO
        fee = _decimal(estimated_fee_usd) or ZERO
        if requested <= ZERO or family not in self.registrations:
            return False
        rows = list(self._rows(station_id, market_day))
        current_open = sum(
            (row["inventory_cost_usd"] + row["active_reserved_usd"] for row in rows),
            start=ZERO,
        )
        cumulative = sum(
            (row["cumulative_buy_cost_usd"] + row["active_reserved_usd"] for row in rows),
            start=ZERO,
        )
        return (
            current_open + requested + fee <= self.cap_usd
            and cumulative + requested + fee <= self.cap_usd
        )

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "cap_usd": self.cap_usd,
                "registrations": {
                    family: registration.as_dict()
                    for family, registration in sorted(self.registrations.items())
                },
                "entries": [
                    {"family": family, "station_id": station, "market_day": day, **values}
                    for (family, station, day), values in sorted(self.entries.items())
                ],
                "discrepancies": self.discrepancies,
                "isolation_asserted": True,
            }
        )


@dataclass(frozen=True, slots=True)
class StopEverythingInvariant:
    """A structured record for fail-closed cross-domain mismatches."""

    code: str
    details: Mapping[str, Any]
    timestamp: datetime

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "code": self.code,
                "details": self.details,
                "timestamp": self.timestamp,
                "action": "stop_everything_cancel_and_stop_accumulating",
            }
        )


def stop_everything_mismatches(
    *,
    expected_scope: tuple[str, str] | None = None,
    actual_scope: tuple[str, str] | None = None,
    expected_receipt_hash: str | None = None,
    actual_receipt_hash: str | None = None,
    expected_regime_version: str | None = None,
    actual_regime_version: str | None = None,
    expected_token_id: str | None = None,
    actual_token_id: str | None = None,
    expected_pnl_scope: str | None = None,
    actual_pnl_scope: str | None = None,
    settlement_verified: bool | None = None,
) -> tuple[str, ...]:
    """Return invariant codes; callers must halt when the tuple is nonempty."""
    mismatches: list[str] = []
    if expected_scope is not None and actual_scope is not None and expected_scope != actual_scope:
        mismatches.append("weather_scope_mismatch")
    if (
        expected_receipt_hash is not None
        and actual_receipt_hash is not None
        and expected_receipt_hash != actual_receipt_hash
    ):
        mismatches.append("weather_receipt_mismatch")
    if (
        expected_regime_version is not None
        and actual_regime_version is not None
        and expected_regime_version != actual_regime_version
    ):
        mismatches.append("regime_version_mismatch")
    if (
        expected_token_id is not None
        and actual_token_id is not None
        and expected_token_id != actual_token_id
    ):
        mismatches.append("token_ledger_scope_mismatch")
    if (
        expected_pnl_scope is not None
        and actual_pnl_scope is not None
        and expected_pnl_scope != actual_pnl_scope
    ):
        mismatches.append("pnl_scope_mismatch")
    if settlement_verified is False:
        mismatches.append("settlement_verification_missing")
    return tuple(mismatches)


@dataclass(frozen=True, slots=True)
class QuietDecision:
    timestamp: datetime
    scope: tuple[str, str]
    token_id: str
    state: RegimeState
    action: str
    reason: str
    order_ids: tuple[str, ...] = ()
    target_prices: tuple[Decimal, ...] = ()
    event_ids: tuple[str, ...] = ()
    missed_opportunity: bool = False
    reduce_only: bool = False
    lookahead_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "timestamp": self.timestamp,
                "scope": {"station_id": self.scope[0], "market_day": self.scope[1]},
                "token_id": self.token_id,
                "state": self.state,
                "action": self.action,
                "reason": self.reason,
                "order_ids": self.order_ids,
                "target_prices": self.target_prices,
                "event_ids": self.event_ids,
                "missed_opportunity": self.missed_opportunity,
                "reduce_only": self.reduce_only,
                "lookahead_used": False,
            }
        )


@dataclass(frozen=True, slots=True)
class DecisionRegret:
    """After-the-fact counterfactual; never consulted by replay decisions."""

    decision_timestamp: datetime
    scope: tuple[str, str]
    token_id: str
    action: str
    next_event_at: datetime | None
    horizon_end: datetime
    no_cancel_best_bid_change: float | None
    no_replenishment_best_ask_change: float | None
    next_event_gap: float | None
    lookahead_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "decision_timestamp": self.decision_timestamp,
                "scope": {"station_id": self.scope[0], "market_day": self.scope[1]},
                "token_id": self.token_id,
                "action": self.action,
                "next_event_at": self.next_event_at,
                "horizon_end": self.horizon_end,
                "no_cancel_best_bid_change": self.no_cancel_best_bid_change,
                "no_replenishment_best_ask_change": self.no_replenishment_best_ask_change,
                "next_event_gap": self.next_event_gap,
                "lookahead_only": True,
            }
        )


@dataclass(frozen=True, slots=True)
class SnapshotDiagnostic:
    """Small quote/index row retained for post-replay diagnostics.

    Full depth is needed only while the shadow engine consumes a snapshot.  A
    long replay must not keep every historical ladder alive merely to compute
    later markouts and matched controls, so the diagnostic pass keeps only
    token identity, top-of-book values, and the declared matching covariate.
    """

    timestamp: datetime
    portfolio_key: TokenPortfolioKey
    token_id: str
    station_id: str | None
    market_day: str | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    mid: float | None
    physical_margin_f: float | None = None
    state: RegimeState | None = None

    @property
    def metadata(self) -> Mapping[str, Any]:
        return (
            {"physical_margin_f": self.physical_margin_f}
            if self.physical_margin_f is not None
            else {}
        )


def _diagnostic_snapshot(
    snapshot: BookSnapshot,
    *,
    portfolio_key: TokenPortfolioKey | None = None,
    scope: tuple[str, str] | None = None,
) -> SnapshotDiagnostic:
    best_bid = snapshot.best_bid
    best_ask = snapshot.best_ask
    margin = _decimal(snapshot.metadata.get("physical_margin_f"))
    mid = (best_bid + best_ask) / Decimal("2") if best_bid is not None and best_ask is not None else None
    key = portfolio_key or snapshot.portfolio_key
    canonical_scope = scope or (
        snapshot.station_id or "unknown",
        snapshot.market_day or key.market_day,
    )
    return SnapshotDiagnostic(
        timestamp=snapshot.timestamp,
        portfolio_key=key,
        token_id=key.token_id,
        station_id=canonical_scope[0],
        market_day=canonical_scope[1],
        best_bid=float(best_bid) if best_bid is not None else None,
        best_ask=float(best_ask) if best_ask is not None else None,
        spread=float(snapshot.spread) if snapshot.spread is not None else None,
        mid=float(mid) if mid is not None else None,
        physical_margin_f=float(margin) if margin is not None else None,
    )


@dataclass(frozen=True, slots=True)
class _CompactLastMetric:
    """Only the fields needed to continue one token's state machine."""

    timestamp: datetime
    station_id: str
    market_day: str
    token_id: str
    mid: Decimal | None
    metadata: Mapping[str, Any]

    @property
    def scope(self) -> tuple[str, str]:
        return self.station_id, self.market_day

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "station_id": self.station_id,
            "market_day": self.market_day,
            "token_id": self.token_id,
            "mid": str(self.mid) if self.mid is not None else None,
            "metadata": dict(self.metadata),
        }


def _compact_metric(metrics: MarketMetrics) -> _CompactLastMetric:
    anchor = metrics.metadata.get("anchor_mid", metrics.mid)
    return _CompactLastMetric(
        timestamp=metrics.timestamp,
        station_id=metrics.station_id,
        market_day=metrics.market_day,
        token_id=metrics.token_id,
        mid=metrics.mid,
        metadata={"anchor_mid": anchor},
    )


def compute_decision_regret(
    decisions: Sequence[QuietDecision],
    snapshots: Sequence[BookSnapshot | SnapshotDiagnostic],
    information_events: Sequence[InformationEvent],
    *,
    horizon: timedelta = timedelta(minutes=30),
) -> tuple[DecisionRegret, ...]:
    """Compute cancel/hold, refill, and next-event diagnostics strictly later."""
    output: list[DecisionRegret] = []
    def snapshot_scope(row: BookSnapshot | SnapshotDiagnostic) -> tuple[str, str]:
        return (
            row.station_id or "unknown",
            row.market_day or row.timestamp.date().isoformat(),
        )

    by_token: dict[str, list[BookSnapshot | SnapshotDiagnostic]] = defaultdict(list)
    for snapshot in snapshots:
        by_token[snapshot.token_id].append(snapshot)
    for rows in by_token.values():
        rows.sort(key=lambda row: row.timestamp)
    by_token_scope: dict[
        tuple[str, tuple[str, str]], list[BookSnapshot | SnapshotDiagnostic]
    ] = defaultdict(list)
    for token_id, rows in by_token.items():
        for row in rows:
            by_token_scope[(token_id, snapshot_scope(row))].append(row)
    eligible_events = [event for event in information_events if event.phase_eligible]
    event_rows_by_scope: dict[tuple[str, str], list[InformationEvent]] = defaultdict(list)
    for event in eligible_events:
        event_rows_by_scope[event.scope].append(event)
    event_times_by_scope = {
        scope: [event.available_at for event in rows]
        for scope, rows in event_rows_by_scope.items()
    }
    for decision in decisions:
        end = decision.timestamp + horizon
        token_rows = by_token_scope.get((decision.token_id, decision.scope), [])
        token_times = [row.timestamp for row in token_rows]
        future_start = bisect_right(token_times, decision.timestamp)
        future_end = bisect_right(token_times, end)
        future_rows = token_rows[future_start:future_end]
        before_index = future_start - 1
        before = token_rows[before_index] if before_index >= 0 else None
        event_rows = event_rows_by_scope.get(decision.scope, [])
        event_times = event_times_by_scope.get(decision.scope, [])
        event_start = bisect_right(event_times, decision.timestamp)
        event_end = bisect_right(event_times, end)
        next_event = event_rows[event_start] if event_start < event_end else None
        last = future_rows[-1] if future_rows else None
        bid_change = (
            float(last.best_bid - before.best_bid)
            if before is not None
            and last is not None
            and before.best_bid is not None
            and last.best_bid is not None
            else None
        )
        ask_change = (
            float(last.best_ask - before.best_ask)
            if before is not None
            and last is not None
            and before.best_ask is not None
            and last.best_ask is not None
            else None
        )
        gap = (
            (next_event.available_at - decision.timestamp).total_seconds()
            if next_event is not None and next_event.available_at is not None
            else None
        )
        output.append(
            DecisionRegret(
                decision_timestamp=decision.timestamp,
                scope=decision.scope,
                token_id=decision.token_id,
                action=decision.action,
                next_event_at=next_event.available_at if next_event else None,
                horizon_end=end,
                no_cancel_best_bid_change=bid_change,
                no_replenishment_best_ask_change=ask_change,
                next_event_gap=gap,
            )
        )
    return tuple(output)


class QuietWindowEngine:
    """Independent token portfolios controlled by a station/day regime."""

    def __init__(
        self,
        *,
        config: QuietWindowConfig | None = None,
        thresholds: RegimeThresholds | None = None,
        ledger: ShadowLedger | None = None,
        ledger_path: Path | str | None = None,
        risk_book: ThreeStrategyRiskBook | None = None,
        retain_trace: bool = True,
        retain_snapshots: bool = True,
        retain_diagnostics: bool = True,
        lazy_engines: bool = False,
        l2_churn_index: L2ChurnIndex | None = None,
        station_timezones: Mapping[str, str] | None = None,
        use_real_microstructure: bool = False,
    ) -> None:
        self.config = config or default_quiet_window_config()
        self.config.validate()
        if ledger is not None and ledger_path is not None:
            raise ValueError("provide ledger or ledger_path, not both")
        self.ledger = ledger or (ShadowLedger(ledger_path) if ledger_path else None)
        self.risk_book = risk_book or ThreeStrategyRiskBook(
            cap_usd=self.config.station_day_budget_usd
        )
        ledger_identity = (
            str(self.ledger.path)
            if self.ledger is not None
            else f"in_memory:{self.config.strategy_family}:{self.config.version}"
        )
        self.risk_book.register_family(
            self.config.strategy_family,
            version=self.config.version,
            ledger_identity=ledger_identity,
        )
        self.thresholds = thresholds or predefined_regime_thresholds()[
            self.config.threshold_tier
        ]
        self.retain_trace = retain_trace
        self.retain_snapshots = retain_snapshots
        self.retain_diagnostics = retain_diagnostics
        self.lazy_engines = lazy_engines
        self.l2_churn_index = l2_churn_index
        self.use_real_microstructure = use_real_microstructure
        self._trade_intensity_tracker = (
            TradeIntensityTracker(station_timezones=station_timezones or {})
            if use_real_microstructure
            else None
        )
        self.engines: dict[TokenPortfolioKey, ShadowOrderEngine] = {}
        self.machines: dict[tuple[str, str, str], MarketRegimeStateMachine] = {}
        self._scope_by_key: dict[TokenPortfolioKey, tuple[str, str]] = {}
        self._key_by_token: dict[str, TokenPortfolioKey] = {}
        self._canonical_keys: dict[tuple[str, str, str, str], TokenPortfolioKey] = {}
        self._canonical_scopes: dict[tuple[str, str], tuple[str, str]] = {}
        self._previous_metrics: dict[TokenPortfolioKey, _CompactLastMetric] = {}
        self._anchors: dict[TokenPortfolioKey, Decimal] = {}
        self._physical_state_by_key: dict[
            TokenPortfolioKey, tuple[str | None, str | None, str | None]
        ] = {}
        self._information_keys: dict[TokenPortfolioKey, set[tuple[str, str, str]]] = defaultdict(set)
        self._accepted_information_events: list[InformationEvent] = []
        self._accepted_information_keys: set[tuple[str, str, str, str, str]] = set()
        self._replenishments: dict[TokenPortfolioKey, int] = defaultdict(int)
        self.decisions: list[QuietDecision] = []
        self.snapshots: list[BookSnapshot] = []
        self.diagnostic_snapshots: list[SnapshotDiagnostic] = []
        self._snapshot_count = 0
        self.state_observation_counts: Counter[str] = Counter()
        self.metric_coverage_counts: Counter[tuple[str, str]] = Counter()
        self.non_quiet_reason_counts: Counter[str] = Counter()
        self.coverage_blocking_counts: Counter[str] = Counter()
        self.true_violation_counts: Counter[str] = Counter()
        self.information_impact_counts: Counter[str] = Counter()
        self.quiet_machine_keys: set[tuple[str, str, str]] = set()
        self._observed_scopes: set[tuple[str, str]] = set()
        self.trades: list[TradeEvent] = []
        self.discrepancies: list[StopEverythingInvariant] = []
        self.rejection_reasons: dict[str, int] = defaultdict(int)
        self.halted = False
        self.halt_reason: str | None = None

    def _canonical_key(self, snapshot: BookSnapshot) -> TokenPortfolioKey:
        raw = (
            snapshot.event_id,
            snapshot.market_id,
            snapshot.token_id,
            snapshot.market_day or snapshot.timestamp.date().isoformat(),
        )
        key = self._canonical_keys.get(raw)
        if key is None:
            key = TokenPortfolioKey(*raw)
            self._canonical_keys[raw] = key
        return key

    def _canonical_scope(
        self, snapshot: BookSnapshot, key: TokenPortfolioKey
    ) -> tuple[str, str]:
        raw = (snapshot.station_id or "unknown", snapshot.market_day or key.market_day)
        scope = self._canonical_scopes.get(raw)
        if scope is None:
            scope = raw
            self._canonical_scopes[raw] = scope
        return scope

    def _compact_machine(self, machine: MarketRegimeStateMachine) -> None:
        if self.retain_trace or self.retain_snapshots:
            return
        last_metric = machine.last_metric
        if last_metric is not None and not isinstance(last_metric, _CompactLastMetric):
            machine.last_metric = _compact_metric(last_metric)
        if (
            machine.last_transition is not None
            and machine.last_transition.metrics is not None
        ):
            machine.last_transition = replace(machine.last_transition, metrics=None)

    def _machine(self, key: TokenPortfolioKey, scope: tuple[str, str]) -> MarketRegimeStateMachine:
        machine_key = (scope[0], scope[1], key.token_id)
        machine = self.machines.get(machine_key)
        if machine is None:
            machine = MarketRegimeStateMachine(
                station_id=scope[0],
                market_day=scope[1],
                thresholds=self.thresholds,
                retain_transitions=self.retain_trace,
                retain_event_keys=self.retain_trace,
            )
            self.machines[machine_key] = machine
        return machine

    def _engine(self, snapshot: BookSnapshot) -> ShadowOrderEngine:
        key = self._canonical_key(snapshot)
        existing_key = self._key_by_token.get(snapshot.token_id)
        if existing_key is not None and existing_key != key:
            raise ShadowPortfolioInvariantError(
                "quiet_token_maps_to_multiple_portfolios",
                details={"token_id": snapshot.token_id, "first": existing_key.as_dict(), "second": key.as_dict()},
            )
        self._key_by_token[snapshot.token_id] = key
        scope = self._canonical_scope(snapshot, key)
        self._scope_by_key[key] = scope
        engine = self.engines.get(key)
        if engine is None:
            engine = ShadowOrderEngine(
                ledger=self.ledger,
                budget_usd=self.config.station_day_budget_usd,
                max_active_orders=self.config.max_active_orders,
                fill_model=self.config.fill_model,
                order_timeout=self.config.order_timeout,
                portfolio_key=key,
                budget_mode=self.config.station_day_budget_mode,
                require_season_version=True,
                execution_enabled=False,
            )
            self.engines[key] = engine
        for order in engine.orders:
            if order.strategy_version != self.config.version:
                raise ShadowPortfolioInvariantError(
                    "quiet_ledger_strategy_version_mismatch",
                    details={
                        "portfolio_key": key.as_dict(),
                        "expected_strategy_version": self.config.version,
                        "actual_strategy_version": order.strategy_version,
                    },
                )
        return engine

    def _scope_engines(self, scope: tuple[str, str]) -> tuple[ShadowOrderEngine, ...]:
        return tuple(
            engine
            for key, engine in self.engines.items()
            if self._scope_by_key.get(key) == scope
        )

    def _refresh_risk(self, scope: tuple[str, str]) -> dict[str, Any] | None:
        engines = self._scope_engines(scope)
        if not engines:
            return None
        inventory = sum((engine.inventory_cost_usd for engine in engines), start=ZERO)
        reserved = sum((engine.active_reserved_usd for engine in engines), start=ZERO)
        cumulative = sum((engine.cumulative_buy_cost_usd for engine in engines), start=ZERO)
        self.risk_book.update(
            self.config.strategy_family,
            scope[0],
            scope[1],
            inventory_cost_usd=inventory,
            active_reserved_usd=reserved,
            cumulative_buy_cost_usd=cumulative,
            portfolio_count=len(engines),
        )
        return self.risk_book.assert_station_day(scope[0], scope[1])

    def _record_decision(
        self,
        snapshot: BookSnapshot,
        machine: MarketRegimeStateMachine,
        *,
        action: str,
        reason: str,
        order_ids: Sequence[str] = (),
        target_prices: Sequence[Decimal] = (),
        event_ids: Sequence[str] = (),
        missed_opportunity: bool = False,
        reduce_only: bool = False,
    ) -> QuietDecision:
        decision = QuietDecision(
            timestamp=snapshot.timestamp,
            scope=machine.scope,
            token_id=snapshot.token_id,
            state=machine.state,
            action=action,
            reason=reason,
            order_ids=tuple(order_ids),
            target_prices=tuple(target_prices),
            event_ids=tuple(event_ids),
            missed_opportunity=missed_opportunity,
            reduce_only=reduce_only,
        )
        self.state_observation_counts[decision.state.value] += 1
        if (
            self.retain_trace
            or decision.action != "SKIP"
            or decision.missed_opportunity
        ):
            self.decisions.append(decision)
        if self.retain_diagnostics and self.diagnostic_snapshots:
            last = self.diagnostic_snapshots[-1]
            if last.token_id == snapshot.token_id and last.timestamp == snapshot.timestamp:
                self.diagnostic_snapshots[-1] = replace(last, state=decision.state)
        self._compact_machine(machine)
        return decision

    def stop_everything(
        self,
        code: str,
        *,
        timestamp: datetime,
        details: Mapping[str, Any] | None = None,
        snapshot: BookSnapshot | None = None,
        allow_reduce_only_exit: bool = False,
    ) -> None:
        """Permanently halt this replay/follower and cancel all resting orders."""
        at = _required_utc(timestamp)
        if not self.halted:
            record = StopEverythingInvariant(code=code, details=dict(details or {}), timestamp=at)
            self.discrepancies.append(record)
            self.halted = True
            self.halt_reason = code
            if self.ledger is not None:
                self.ledger.record_discrepancy(
                    code=code,
                    details=dict(details or {}),
                    portfolio_key=snapshot.portfolio_key if snapshot else None,
                )
        for engine in self.engines.values():
            try:
                engine.cancel_all(timestamp=at, reason=code)
            except (KeyError, ShadowPortfolioInvariantError):
                pass
        if snapshot is not None and allow_reduce_only_exit:
            engine = self.engines.get(snapshot.portfolio_key)
            if engine is not None and engine.inventory_shares > ZERO:
                try:
                    engine.simulate_taker_exit(snapshot, reason=code)
                except ShadowPortfolioInvariantError as exc:
                    self.discrepancies.append(
                        StopEverythingInvariant(
                            code=exc.code,
                            details=dict(exc.details),
                            timestamp=at,
                        )
                    )
        for machine in self.machines.values():
            if not machine.halted:
                machine.halt(code, timestamp=at, details=details)

    def _event_for_snapshot(
        self, event: InformationEvent, snapshot: BookSnapshot
    ) -> InformationEvent | None:
        if event.station_id != (snapshot.station_id or "").upper():
            return None
        if event.market_day is None:
            return replace(event, market_day=snapshot.market_day)
        if event.market_day != snapshot.market_day:
            return None
        return event

    def _validate_information_event(
        self, event: InformationEvent, snapshot: BookSnapshot
    ) -> bool:
        scoped = self._event_for_snapshot(event, snapshot)
        if scoped is None:
            self.stop_everything(
                "weather_information_scope_mismatch",
                timestamp=snapshot.timestamp,
                details={"event": event.as_dict(), "snapshot_scope": snapshot.portfolio_key.as_dict()},
                snapshot=snapshot,
            )
            return False
        metadata = scoped.metadata if isinstance(scoped.metadata, Mapping) else {}
        mismatches = stop_everything_mismatches(
            expected_scope=(snapshot.station_id or "unknown", snapshot.market_day or "unknown"),
            actual_scope=scoped.scope,
            expected_receipt_hash=(
                str(metadata.get("expected_receipt_hash"))
                if metadata.get("expected_receipt_hash") is not None
                else None
            ),
            actual_receipt_hash=(
                str(metadata.get("receipt_hash"))
                if metadata.get("receipt_hash") is not None
                else None
            ),
            expected_regime_version=self.config.version,
            actual_regime_version=(
                str(metadata.get("regime_version"))
                if metadata.get("regime_version") is not None
                else None
            ),
            expected_token_id=snapshot.token_id
            if metadata.get("token_id") is not None
            else None,
            actual_token_id=str(metadata.get("token_id"))
            if metadata.get("token_id") is not None
            else None,
            expected_pnl_scope=snapshot.portfolio_key.identifier
            if metadata.get("pnl_scope") is not None
            else None,
            actual_pnl_scope=str(metadata.get("pnl_scope"))
            if metadata.get("pnl_scope") is not None
            else None,
            settlement_verified=snapshot.settlement_verified,
        )
        if mismatches:
            self.stop_everything(
                mismatches[0],
                timestamp=snapshot.timestamp,
                details={"mismatches": mismatches, "event": scoped.as_dict()},
                snapshot=snapshot,
            )
            return False
        if (
            not scoped.phase_eligible
            and scoped.source_at is not None
            and scoped.available_at is not None
            and not scoped.receipt_verified
        ):
            self.stop_everything(
                "weather_receipt_mismatch",
                timestamp=snapshot.timestamp,
                details={"event": scoped.as_dict()},
                snapshot=snapshot,
            )
            return False
        if not scoped.phase_eligible:
            # Missing timestamps are diagnostics, not an information reset.
            # A receipt that exists but is later than source time is a hard
            # mismatch and is stopped above by the explicit flag.
            return False
        return True

    def _exit_stage(self, engine: ShadowOrderEngine) -> int:
        stages = [
            int(order.metadata.get("exit_stage", 0))
            for order in engine.orders
            if order.side is ShadowSide.SELL and order.fills
        ]
        return min(max(stages, default=-1) + 1, len(self.config.exit_rises) - 1)

    def _manage_exit(
        self,
        snapshot: BookSnapshot,
        machine: MarketRegimeStateMachine,
        engine: ShadowOrderEngine | None,
    ) -> tuple[str, tuple[str, ...], tuple[Decimal, ...]]:
        if engine is None or engine.inventory_shares <= ZERO:
            return "", (), ()
        buy_fills = [
            fill
            for order in engine.orders
            if order.side is ShadowSide.BUY
            for fill in order.fills
        ]
        oldest = min((fill.timestamp for fill in buy_fills), default=None)
        if (
            self.config.max_hold is not None
            and oldest is not None
            and snapshot.timestamp - oldest >= self.config.max_hold
            and snapshot.bids
        ):
            try:
                fills = engine.simulate_taker_exit(snapshot, reason="max_hold_reduce_only_exit")
            except ShadowPortfolioInvariantError as exc:
                self.stop_everything(
                    exc.code,
                    timestamp=snapshot.timestamp,
                    details=exc.details,
                    snapshot=snapshot,
                )
                return "HALT", (), ()
            if fills:
                return "REDUCE_ONLY_EXIT", tuple(fill.fill_id for fill in fills), ()
        if engine.active_orders or snapshot.best_bid is None:
            return "", (), ()
        average = engine.average_inventory_cost
        if average is None:
            return "", (), ()
        stage = self._exit_stage(engine)
        target = average + self.config.exit_rises[stage]
        if snapshot.best_bid < target:
            return "", (), (target,)
        shares = (
            engine.inventory_shares
            if stage > 0
            else engine.inventory_shares * self.config.partial_exit_fraction
        )
        if shares < snapshot.min_order_size:
            shares = engine.inventory_shares
        try:
            order = engine.submit_maker_exit(
                snapshot,
                target_price=target,
                shares=shares,
                idempotency_key=(
                    f"{snapshot.portfolio_key.identifier}:exit:{stage}:"
                    f"{snapshot.timestamp.isoformat()}"
                ),
                strategy_version=self.config.version,
                trigger_reason=f"quiet_target_plus_{self.config.exit_rises[stage]}",
                metadata={
                    "strategy_family": self.config.strategy_family,
                    "regime_state": machine.state.value,
                    "exit_stage": stage,
                },
            )
        except (ShadowOrderRejected, ValueError) as exc:
            reason = getattr(getattr(exc, "reason", None), "value", "exit_quote_rejected")
            self.rejection_reasons[str(reason)] += 1
            return "", (), (target,)
        return "REDUCE_ONLY_EXIT_QUOTE", (order.order_id,), (target,)

    def _maybe_replenish(
        self,
        snapshot: BookSnapshot,
        machine: MarketRegimeStateMachine,
        metrics: MarketMetrics,
        engine: ShadowOrderEngine | None,
    ) -> tuple[str, tuple[str, ...], tuple[Decimal, ...]]:
        if (
            not self.config.allow_conditional_replenishment
            or engine is None
            or self._replenishments[engine.portfolio_key] >= self.config.max_replenishments
            or not machine.allow_new_orders
            or engine.inventory_shares <= ZERO
            or engine.active_orders
            or snapshot.best_ask is None
            or not _flag(metrics.metadata.get("weather_unchanged"))
            or _flag(metrics.metadata.get("weather_worsening"))
        ):
            return "", (), ()
        anchor = _decimal(metrics.metadata.get("anchor_mid")) or metrics.mid
        if anchor is None:
            return "", (), ()
        prior_anchor = self._anchors.get(engine.portfolio_key)
        if prior_anchor is None:
            self._anchors[engine.portfolio_key] = anchor
            return "", (), ()
        if abs(anchor - prior_anchor) > self.config.anchor_change_tolerance:
            return "", (), ()
        average = engine.average_inventory_cost
        if average is None or snapshot.best_ask >= average:
            return "", (), ()
        target = average + self.config.exit_rises[0]
        quote = quote_limit(snapshot, mode=self.config.quote_mode)
        if quote is None:
            return "", (), ()
        if not self.risk_book.can_reserve(
            self.config.strategy_family,
            machine.scope[0],
            machine.scope[1],
            requested_usd=self.config.quote_size_usd,
        ):
            return "", (), ()
        try:
            order = engine.submit_replenishment(
                snapshot,
                limit_price=quote,
                size_usd=self.config.quote_size_usd,
                mode=ReplenishMode.CONDITIONAL_DIP,
                target_price=target,
                prior_order_resolved=True,
                weather_unchanged=True,
                weather_worsening=False,
                spread_ok=metrics.spread is not None,
                book_ok=snapshot.book_complete,
                idempotency_key=(
                    f"{engine.portfolio_key.identifier}:replenish:"
                    f"{self._replenishments[engine.portfolio_key]}:{snapshot.timestamp.isoformat()}"
                ),
                strategy_version=self.config.version,
            )
        except (ShadowOrderRejected, ValueError) as exc:
            reason = getattr(getattr(exc, "reason", None), "value", "replenish_rejected")
            self.rejection_reasons[str(reason)] += 1
            return "", (), ()
        self._replenishments[engine.portfolio_key] += 1
        return "CONDITIONAL_REPLENISH", (order.order_id,), (quote,)

    def _submit_quiet_quotes(
        self,
        snapshot: BookSnapshot,
        machine: MarketRegimeStateMachine,
        engine: ShadowOrderEngine | None,
    ) -> tuple[str, tuple[str, ...], tuple[Decimal, ...], bool]:
        if (
            not machine.allow_new_orders
            or (
                engine is not None
                and (engine.inventory_shares > ZERO or bool(engine.active_orders))
            )
        ):
            return "", (), (), False
        if self.config.quote_mode is QuoteMode.LADDER:
            quotes = quote_ladder(snapshot, tick_offsets=self.config.ladder_ticks)
        else:
            quote = quote_limit(snapshot, mode=self.config.quote_mode)
            quotes = (quote,) if quote is not None else ()
        if not quotes:
            return "", (), (), True
        order_ids: list[str] = []
        submitted_quotes: list[Decimal] = []
        for quote in quotes:
            if not self.risk_book.can_reserve(
                self.config.strategy_family,
                machine.scope[0],
                machine.scope[1],
                requested_usd=self.config.quote_size_usd,
            ):
                return (
                    "",
                    tuple(order_ids),
                    tuple(submitted_quotes),
                    True,
                )
            try:
                if engine is None:
                    engine = self._engine(snapshot)
                order = engine.submit_limit(
                    snapshot,
                    side=ShadowSide.BUY,
                    limit_price=quote,
                    size_usd=self.config.quote_size_usd,
                    idempotency_key=(
                        f"{engine.portfolio_key.identifier}:quiet-entry:"
                        f"{snapshot.timestamp.isoformat()}:{quote}"
                    ),
                    strategy_version=self.config.version,
                    season_version=snapshot.season_version,
                    trigger_reason="quiet_window_post_only_maker",
                    maker_assumption=True,
                    metadata={
                        "strategy_family": self.config.strategy_family,
                        "regime_state": RegimeState.QUIET.value,
                        "anchor_mid": snapshot.best_bid,
                    },
                )
            except (ShadowOrderRejected, ValueError) as exc:
                reason = getattr(getattr(exc, "reason", None), "value", "entry_quote_rejected")
                self.rejection_reasons[str(reason)] += 1
                continue
            order_ids.append(order.order_id)
            submitted_quotes.append(quote)
            self._anchors.setdefault(
                engine.portfolio_key,
                _decimal(snapshot.best_bid) or ZERO,
            )
        if order_ids:
            return "QUIET_QUOTES_SUBMITTED", tuple(order_ids), tuple(submitted_quotes), False
        return "", (), (), True

    @staticmethod
    def _merge_metric_context(
        snapshot: BookSnapshot, context: Mapping[str, Any]
    ) -> BookSnapshot:
        metadata = dict(snapshot.metadata) if isinstance(snapshot.metadata, Mapping) else {}
        statuses = (
            dict(metadata.get("metric_status"))
            if isinstance(metadata.get("metric_status"), Mapping)
            else {}
        )
        incoming = context.get("metric_status")
        if isinstance(incoming, Mapping):
            statuses.update({str(key): str(value) for key, value in incoming.items()})
        metadata.update({key: value for key, value in context.items() if key != "metric_status"})
        if statuses:
            metadata["metric_status"] = statuses
        return replace(snapshot, metadata=metadata)

    def _physical_state_event(
        self, snapshot: BookSnapshot, key: TokenPortfolioKey
    ) -> InformationEvent | None:
        metadata = snapshot.metadata if isinstance(snapshot.metadata, Mapping) else {}
        state = (
            str(metadata.get("physical_margin_f"))
            if metadata.get("physical_margin_f") is not None
            else None,
            str(metadata.get("physical_margin_tier"))
            if metadata.get("physical_margin_tier") is not None
            else None,
            str(metadata.get("physical_eliminated"))
            if metadata.get("physical_eliminated") is not None
            else None,
        )
        if state == (None, None, None):
            return None
        previous = self._physical_state_by_key.get(key)
        self._physical_state_by_key[key] = state
        # The first available value establishes this token's own baseline.  A
        # later settlement-rounded margin/tier/elimination transition is a
        # real physical state change and must reset only this token's anchor.
        if previous is None or previous == state:
            return None
        return InformationEvent(
            event_id=(
                f"physical:{key.identifier}:{snapshot.timestamp.isoformat()}:"
                f"{state[0]}:{state[1]}:{state[2]}"
            ),
            source="derived_token_physical_state",
            kind="physical_margin",
            source_at=snapshot.timestamp,
            available_at=snapshot.timestamp,
            station_id=snapshot.station_id,
            market_day=snapshot.market_day,
            payload_hash="|".join(value or "" for value in state),
            changed_physical_margin=True,
            impact_class=ImpactClass.HARD_RESET,
            impact_reason="token_physical_margin_tier_or_elimination_changed",
            metadata={
                "token_id": snapshot.token_id,
                "physical_margin_f": state[0],
                "physical_margin_tier": state[1],
                "physical_eliminated": state[2],
            },
        )

    def _record_metric_coverage(
        self, metrics: MarketMetrics
    ) -> None:
        for name in (
            "mid",
            "spread",
            "top_bid_depth",
            "top_ask_depth",
            "imbalance",
            "price_slope",
            "cumulative_move",
            "trade_intensity_multiple",
            "churn_rate",
            "cross_bucket_mass_error",
        ):
            self.metric_coverage_counts[(name, metrics.knowledge_for(name).value)] += 1

    @staticmethod
    def _global_health_ok(snapshot: BookSnapshot) -> bool:
        """Keep station-wide safety gates separate from one token's quote gap.

        A missing side on one token must cancel/disable that token through the
        mandatory UNQUOTABLE metric, but it cannot halt another healthy token
        in the same station-day.  Maintenance, stale weather, rule, season and
        quality failures remain stop-everything conditions.
        """
        return bool(
            not snapshot.market_stale
            and not snapshot.weather_stale
            and snapshot.settlement_verified
            and snapshot.in_season
            and snapshot.warming_valid
            and snapshot.season_version
            and snapshot.season_version.casefold() not in {"unknown", "none"}
            and not snapshot.quality_excluded
            and snapshot.upstream_status.casefold() == "normal"
        )

    def process_snapshot(
        self,
        snapshot: BookSnapshot,
        *,
        information_events: Sequence[InformationEvent] = (),
        next_release_at: datetime | None = None,
        schedule_verified: bool = False,
        trades_since_previous: Sequence[TradeEvent] = (),
        health_ok: bool | None = None,
    ) -> QuietDecision:
        """Advance one snapshot with strict source/receipt and token gates."""
        snapshot = _as_snapshot(snapshot)
        canonical_key = self._canonical_key(snapshot)
        canonical_scope = self._canonical_scope(snapshot, canonical_key)
        previous = self._previous_metrics.get(canonical_key)
        if self.use_real_microstructure:
            context: dict[str, Any] = {}
            if self._trade_intensity_tracker is not None:
                context.update(
                    self._trade_intensity_tracker.observe(
                        snapshot,
                        previous_at=previous.timestamp if previous is not None else None,
                        trades=trades_since_previous,
                    )
                )
            if self.l2_churn_index is not None:
                context.update(self.l2_churn_index.lookup(snapshot.token_id, snapshot.timestamp))
            snapshot = self._merge_metric_context(snapshot, context)
        self._observed_scopes.add(canonical_scope)
        self._snapshot_count += 1
        if self.retain_snapshots:
            self.snapshots.append(snapshot)
        if self.retain_diagnostics:
            self.diagnostic_snapshots.append(
                _diagnostic_snapshot(
                    snapshot,
                    portfolio_key=canonical_key,
                    scope=canonical_scope,
                )
            )
        if self.halted:
            return self._record_decision(
                snapshot,
                self._machine(canonical_key, canonical_scope),
                action="HALTED",
                reason=self.halt_reason or "stop_everything",
                reduce_only=True,
            )
        scope = canonical_scope
        try:
            existing_key = self._key_by_token.get(snapshot.token_id)
            if existing_key is not None and existing_key != canonical_key:
                raise ShadowPortfolioInvariantError(
                    "quiet_token_maps_to_multiple_portfolios",
                    details={
                        "token_id": snapshot.token_id,
                        "first": existing_key.as_dict(),
                        "second": canonical_key.as_dict(),
                    },
                )
            self._key_by_token[snapshot.token_id] = canonical_key
            if not self.lazy_engines:
                self._scope_by_key[canonical_key] = scope
            engine = self.engines.get(canonical_key)
            if engine is None and not self.lazy_engines:
                engine = self._engine(snapshot)
            elif (
                engine is None
                and self.ledger is not None
                and self.ledger.orders_for(canonical_key)
            ):
                engine = self._engine(snapshot)
            machine = self._machine(canonical_key, scope)
        except ShadowPortfolioInvariantError as exc:
            self.stop_everything(exc.code, timestamp=snapshot.timestamp, details=exc.details, snapshot=snapshot)
            machine = self._machine(canonical_key, scope)
            return self._record_decision(snapshot, machine, action="HALTED", reason=exc.code, reduce_only=True)
        metrics = metrics_from_snapshot(
            snapshot,
            previous=previous,
            trades=trades_since_previous,
        )
        self._record_metric_coverage(metrics)
        observed_health = (
            self._global_health_ok(snapshot) if health_ok is None else bool(health_ok)
        )
        scoped_events: list[InformationEvent] = []
        for event in information_events:
            scoped = self._event_for_snapshot(event, snapshot)
            if scoped is None:
                self._validate_information_event(event, snapshot)
                continue
            if not self._validate_information_event(scoped, snapshot):
                continue
            scoped_events.append(scoped)
        physical_event = self._physical_state_event(snapshot, canonical_key)
        if physical_event is not None:
            scoped_events.append(physical_event)
        for event in scoped_events:
            impact = event.impact_class or ImpactClass.HARD_RESET
            self.information_impact_counts[ImpactClass(impact).value] += 1
        transitions = []
        if not observed_health:
            transitions.append(
                machine.observe(
                    metrics,
                    health_ok=False,
                    health_reason="market_or_weather_health_gate",
                )
            )
        else:
            for event in scoped_events:
                event_key = (event.source, event.kind, event.payload_hash)
                if self.retain_trace and event_key in self._information_keys.get(
                    canonical_key, ()
                ):
                    continue
                transition = machine.observe(
                    metrics,
                    information_event=event,
                    next_release_at=next_release_at,
                    schedule_verified=schedule_verified,
                )
                transitions.append(transition)
                if self.retain_trace:
                    self._information_keys.setdefault(canonical_key, set()).add(event_key)
                if transition.event_id == event.event_id:
                    global_event_key = (*event.scope, *event_key)
                    if global_event_key not in self._accepted_information_keys:
                        self._accepted_information_keys.add(global_event_key)
                        self._accepted_information_events.append(event)
            if not transitions:
                transitions.append(
                    machine.observe(
                        metrics,
                        next_release_at=next_release_at,
                        schedule_verified=schedule_verified,
                    )
                )
        transition = transitions[-1]
        if any(item.hard_reset for item in transitions):
            anchored_metadata = dict(metrics.metadata)
            if metrics.mid is not None:
                anchored_metadata["anchor_mid"] = str(metrics.mid)
            metrics = replace(metrics, metadata=anchored_metadata)
            machine.last_metric = metrics
        self._previous_metrics[canonical_key] = _compact_metric(metrics)
        if transition.state is not RegimeState.QUIET:
            self.non_quiet_reason_counts[transition.reason] += 1
            for reason in transition.coverage_reasons:
                self.coverage_blocking_counts[reason] += 1
            for reason in transition.true_violations:
                self.true_violation_counts[reason] += 1
        else:
            self.quiet_machine_keys.add((scope[0], scope[1], canonical_key.token_id))
        event_ids = tuple(event.event_id for event in scoped_events)
        if not observed_health or transition.state is RegimeState.HALTED:
            self.stop_everything(
                transition.reason if not observed_health else "regime_halted",
                timestamp=snapshot.timestamp,
                details={"transition": transition.as_dict()},
                snapshot=snapshot,
                allow_reduce_only_exit=not observed_health,
            )
            return self._record_decision(
                snapshot,
                machine,
                action="HALTED",
                reason=self.halt_reason or transition.reason,
                event_ids=event_ids,
                reduce_only=True,
            )
        try:
            if engine is not None and (
                transition.state is not RegimeState.QUIET or transition.cancel_required
            ):
                engine.cancel_all(timestamp=snapshot.timestamp, reason=transition.reason)
            elif engine is not None:
                engine.process_snapshot(snapshot)
            if engine is not None and transition.state is not RegimeState.QUIET:
                # The only permissible action outside QUIET is to reduce an
                # existing token inventory; no new BUY can be submitted here.
                engine.cancel_all(timestamp=snapshot.timestamp, reason="non_quiet_reduce_only")
            action, order_ids, targets = self._manage_exit(snapshot, machine, engine)
            if not action and transition.state is RegimeState.QUIET:
                action, order_ids, targets = self._maybe_replenish(
                    snapshot, machine, metrics, engine
                )
            missed = False
            if not action and transition.state is RegimeState.QUIET:
                action, order_ids, targets, missed = self._submit_quiet_quotes(
                    snapshot, machine, engine
                )
            risk = self._refresh_risk(scope)
            del risk
        except (ShadowPortfolioInvariantError, ShadowOrderRejected, ValueError) as exc:
            code = getattr(exc, "code", None) or getattr(
                getattr(exc, "reason", None), "value", "quiet_order_validation_failed"
            )
            details = getattr(exc, "details", {})
            self.stop_everything(code, timestamp=snapshot.timestamp, details=details, snapshot=snapshot)
            return self._record_decision(
                snapshot,
                machine,
                action="HALTED",
                reason=code,
                event_ids=event_ids,
                reduce_only=True,
            )
        if not action:
            action = "SKIP"
            reason = transition.reason
        else:
            reason = action.casefold()
        return self._record_decision(
            snapshot,
            machine,
            action=action,
            reason=reason,
            order_ids=order_ids,
            target_prices=targets,
            event_ids=event_ids,
            missed_opportunity=missed,
            reduce_only=transition.state is not RegimeState.QUIET,
        )

    def process_trade(self, trade: TradeEvent) -> tuple[ShadowFill, ...]:
        """Route only to the exact token portfolio; no trade creates a quote."""
        trade = _as_trade(trade)
        self.trades.append(trade)
        if self.halted:
            return ()
        key = self._key_by_token.get(trade.asset_id)
        if key is None:
            return ()
        engine = self.engines.get(key)
        if engine is None:
            return ()
        try:
            fills = engine.process_trade(trade)
            self._refresh_risk(self._scope_by_key[key])
            return fills
        except ShadowPortfolioInvariantError as exc:
            self.stop_everything(
                exc.code,
                timestamp=trade.timestamp,
                details=exc.details,
            )
            return ()

    def finalize(self, *, timestamp: datetime | None = None) -> None:
        at = _required_utc(
            timestamp
            or max(
                (row.timestamp for row in self.snapshots),
                default=(
                    self.diagnostic_snapshots[-1].timestamp
                    if self.diagnostic_snapshots
                    else datetime.now(UTC)
                ),
            )
        )
        for engine in self.engines.values():
            engine.cancel_all(timestamp=at, reason="replay_cutoff")

    def summary(self, *, include_trace: bool = True) -> dict[str, Any]:
        all_orders = [order for engine in self.engines.values() for order in engine.orders]
        all_fills = [fill for order in all_orders for fill in order.fills]
        scope_risks = []
        for scope in sorted(set(self._scope_by_key.values())):
            try:
                scope_risks.append(self._refresh_risk(scope))
            except ShadowPortfolioInvariantError:
                pass
        flat = all(engine.inventory_shares == ZERO for engine in self.engines.values())
        metric_coverage: dict[str, dict[str, Any]] = {}
        for name in sorted({name for name, _status in self.metric_coverage_counts}):
            counts = {
                status: count
                for (metric_name, status), count in self.metric_coverage_counts.items()
                if metric_name == name
            }
            total = sum(counts.values())
            metric_coverage[name] = {
                "observation_count": total,
                "status_counts": dict(sorted(counts.items())),
                "known_count": counts.get("OK", 0),
                "known_fraction": counts.get("OK", 0) / total if total else None,
            }
        return {
            "strategy_family": self.config.strategy_family,
            "strategy_version": self.config.version,
            "config": self.config.as_dict(),
            "thresholds": self.thresholds.as_dict(),
            "execution_enabled": False,
            "lookahead_guard": {
                "source_and_receipt_cutoff": True,
                "future_decision_regret_used_for_control": False,
                "trade_timestamp_must_be_before_order_submission": True,
            },
            "raw_snapshot_count": self._snapshot_count,
            "diagnostic_snapshot_count": len(self.diagnostic_snapshots),
            "shadow_order_count": len(all_orders),
            "fill_count": len(all_fills),
            "token_portfolio_count": len(self.engines),
            "station_day_cluster_count": len(self._observed_scopes),
            "state_observation_counts": dict(
                sorted(self.state_observation_counts.items())
            ),
            "metric_coverage": metric_coverage,
            "non_quiet_reason_counts": dict(sorted(self.non_quiet_reason_counts.items())),
            "coverage_blocking_counts": dict(sorted(self.coverage_blocking_counts.items())),
            "true_violation_counts": dict(sorted(self.true_violation_counts.items())),
            "information_impact_counts": dict(sorted(self.information_impact_counts.items())),
            "quiet_token_machine_count": len(self.quiet_machine_keys),
            "quiet_station_day_count": len(
                {(station, day) for station, day, _token in self.quiet_machine_keys}
            ),
            "inventory_scope": "event_id + market_id + token_id + market_day",
            "risk_scope": "station_id + market_day; three strategy families aggregated only for cap",
            "state_machines": [
                machine.summary(include_transitions=include_trace)
                for machine in self.machines.values()
            ],
            "reaction_duration": reaction_duration_summary(self.machines.values()),
            "orders": [order.as_dict() for order in all_orders],
            "fills": [
                {
                    "order_id": order.order_id,
                    "portfolio_key": order.portfolio_key.as_dict(),
                    "side": order.side.value,
                    "timestamp": fill.timestamp.isoformat(),
                    "shares": fill.shares,
                    "price": fill.price,
                    "fee_usd": fill.fee_usd,
                    "model": fill.model.value,
                    "source": fill.source,
                }
                for order in all_orders
                for fill in order.fills
            ],
            "portfolios": [
                {
                    "portfolio_key": key.as_dict(),
                    "order_count": len(engine.orders),
                    "fill_count": sum(len(order.fills) for order in engine.orders),
                    "inventory_shares": engine.inventory_shares,
                    "inventory_cost_usd": engine.inventory_cost_usd,
                    "cumulative_buy_cost_usd": engine.cumulative_buy_cost_usd,
                    "active_reserved_usd": engine.active_reserved_usd,
                    "realized_pnl_usd": engine.realized_pnl_usd
                    if engine.inventory_shares == ZERO
                    else None,
                    "pnl_status": "closed_token_native_inventory"
                    if engine.inventory_shares == ZERO
                    else "N/A: token inventory remains open",
                    "round_trips": engine.round_trip_count,
                }
                for key, engine in sorted(self.engines.items())
            ],
            "risk": self.risk_book.as_dict(),
            "scope_risks": scope_risks,
            "trace_retained": self.retain_trace and include_trace,
            "decisions": [decision.as_dict() for decision in self.decisions]
            if self.retain_trace and include_trace
            else [],
            "decision_count": len(self.decisions),
            "missed_opportunity_count": sum(
                decision.missed_opportunity for decision in self.decisions
            ),
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "discrepancies": [record.as_dict() for record in self.discrepancies],
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "flat_at_cutoff": flat,
            "realized_pnl_usd": sum(
                (engine.realized_pnl_usd for engine in self.engines.values()), start=ZERO
            )
            if flat and not self.discrepancies
            else None,
            "pnl_status": "diagnostic only; not an execution result"
            if all_fills and flat and not self.discrepancies
            else "N/A: no fills"
            if not all_fills
            else "N/A: open inventory or invariant discrepancy",
        }


def _scope_events(
    events: Sequence[InformationEvent], snapshots: Sequence[BookSnapshot]
) -> tuple[InformationEvent, ...]:
    """Assign an omitted market day only when the replay has one unambiguous day."""
    return _scope_events_from_scopes(
        events,
        {
            (
                snapshot.station_id or "unknown",
                snapshot.market_day or snapshot.timestamp.date().isoformat(),
            )
            for snapshot in snapshots
        },
    )


def _scope_events_from_scopes(
    events: Sequence[InformationEvent],
    scopes: Iterable[tuple[str, str]],
    station_timezones: Mapping[str, str] | None = None,
) -> tuple[InformationEvent, ...]:
    days_by_station: dict[str, set[str]] = defaultdict(set)
    for station, market_day in scopes:
        if station and market_day:
            days_by_station[station.upper()].add(market_day)
    output: list[InformationEvent] = []
    for event in events:
        if event.station_id is None and event.kind in GLOBAL_INFORMATION_KINDS:
            # Official status is not tied to one weather station.  Expand it
            # once per observed station-day so every token machine receives
            # the same receipt-gated health transition without sharing a
            # portfolio or assigning an unrelated weather event globally.
            expanded = [
                replace(event, station_id=station, market_day=target_day)
                for station, station_days in sorted(days_by_station.items())
                for target_day in sorted(station_days)
            ]
            output.extend(expanded)
            continue
        if event.market_day is not None or event.station_id is None:
            output.append(event)
            continue
        days = days_by_station.get(event.station_id.upper(), set())
        if len(days) == 1:
            output.append(replace(event, market_day=next(iter(days))))
        elif len(days) > 1 and station_timezones:
            timezone_name = station_timezones.get(event.station_id.upper())
            at = event.source_at or event.available_at
            if timezone_name and at is not None:
                try:
                    local_day = at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
                except (KeyError, TypeError, ValueError):
                    local_day = ""
                if local_day in days:
                    output.append(replace(event, market_day=local_day))
                    continue
            # Ambiguous station-day assignment remains ineligible rather than
            # applying one receipt to multiple target markets.
            output.append(event)
        else:
            # Ambiguous station-day assignment is deliberately left ineligible
            # rather than applying one weather receipt to multiple market days.
            output.append(event)
    return deduplicate_information_events(output)


def _timeline(
    snapshots: Sequence[BookSnapshot], trades: Sequence[TradeEvent]
) -> dict[datetime, dict[str, list[Any]]]:
    timeline: dict[datetime, dict[str, list[Any]]] = defaultdict(
        lambda: {"snapshots": [], "trades": []}
    )
    for snapshot in snapshots:
        timeline[snapshot.timestamp]["snapshots"].append(snapshot)
    for trade in trades:
        available = trade.available_at or trade.timestamp
        timeline[max(trade.timestamp, available)]["trades"].append(trade)
    return timeline


def _cluster_bootstrap_interval(
    values_by_cluster: Mapping[tuple[str, str], Sequence[float]],
    *,
    seed: int = 0,
    resamples: int = 1000,
) -> tuple[float, float] | None:
    clusters = [tuple(values) for values in values_by_cluster.values() if values]
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    means = [fmean(values) for values in clusters]
    draws: list[float] = []
    for _ in range(resamples):
        draws.append(fmean(rng.choice(means) for _ in means))
    draws.sort()
    lower = draws[int(0.025 * (len(draws) - 1))]
    upper = draws[int(0.975 * (len(draws) - 1))]
    return lower, upper


def _causal_control_diagnostics(engine: QuietWindowEngine) -> dict[str, Any]:
    """Match QUIET observations to same-day non-QUIET controls.

    Matching is intentionally deterministic and declared up front.  It is a
    covariate control for diagnostics, not an identification claim: a public
    book replay cannot prove that the counterfactual order would have rested.
    """
    def scope(row: SnapshotDiagnostic) -> tuple[str, str]:
        return (
            row.station_id or "unknown",
            row.market_day or row.timestamp.date().isoformat(),
        )

    rows_by_key: dict[TokenPortfolioKey, list[SnapshotDiagnostic]] = defaultdict(list)
    for snapshot in engine.diagnostic_snapshots:
        rows_by_key[snapshot.portfolio_key].append(snapshot)
    for values in rows_by_key.values():
        values.sort(key=lambda row: row.timestamp)

    def covariates(row: SnapshotDiagnostic) -> tuple[float | None, float | None, int, float | None]:
        price = row.best_bid or row.best_ask
        distance = row.physical_margin_f
        return price, row.spread, row.timestamp.hour, distance

    outcome_cache: dict[int, float | None] = {}

    def outcome(row: SnapshotDiagnostic) -> float | None:
        cached = outcome_cache.get(id(row))
        if cached is not None or id(row) in outcome_cache:
            return cached
        target = row.timestamp + timedelta(minutes=5)
        values = rows_by_key.get(row.portfolio_key, [])
        times = [candidate.timestamp for candidate in values]
        start = bisect_right(times, target - timedelta(microseconds=1))
        future = next(
            (candidate for candidate in values[start:] if candidate.best_bid is not None),
            None,
        )
        result = (
            future.best_bid - row.best_bid
            if future is not None and row.best_bid is not None
            else None
        )
        outcome_cache[id(row)] = result
        return result

    rows = sorted(engine.diagnostic_snapshots, key=lambda row: row.timestamp)
    controls_by_bin: dict[
        tuple[tuple[str, str], int, int, int], list[SnapshotDiagnostic]
    ] = defaultdict(list)
    for candidate in rows:
        if candidate.state is None or candidate.state is RegimeState.QUIET:
            continue
        price, spread, hour, _distance = covariates(candidate)
        if price is None or spread is None:
            continue
        controls_by_bin[
            (
                scope(candidate),
                hour,
                math.floor(price / 0.05),
                math.floor(spread / 0.02),
            )
        ].append(candidate)
    treatments: list[float] = []
    controls: list[float] = []
    pairs: list[dict[str, Any]] = []
    for row in rows:
        if row.state is not RegimeState.QUIET:
            continue
        treatment_outcome = outcome(row)
        if treatment_outcome is None:
            continue
        price, spread, hour, distance = covariates(row)
        if price is None or spread is None:
            continue
        best: tuple[float, datetime, str, SnapshotDiagnostic, float] | None = None
        for candidate_hour in range(hour - 1, hour + 2):
            for price_bin in range(math.floor(price / 0.05) - 1, math.floor(price / 0.05) + 2):
                for spread_bin in range(
                    math.floor(spread / 0.02) - 1,
                    math.floor(spread / 0.02) + 2,
                ):
                    for candidate in controls_by_bin.get(
                        (scope(row), candidate_hour, price_bin, spread_bin), ()
                    ):
                        if candidate is row or candidate.timestamp == row.timestamp:
                            continue
                        candidate_outcome = outcome(candidate)
                        candidate_price, candidate_spread, _candidate_hour, candidate_distance = covariates(candidate)
                        if (
                            candidate_outcome is None
                            or candidate_price is None
                            or candidate_spread is None
                            or abs(price - candidate_price) > 0.05
                            or abs(spread - candidate_spread) > 0.02
                            or (
                                distance is not None
                                and candidate_distance is not None
                                and abs(distance - candidate_distance) > 1.5
                            )
                        ):
                            continue
                        score = abs(price - candidate_price) + abs(spread - candidate_spread)
                        rank = (score, candidate.timestamp, candidate.token_id, candidate, candidate_outcome)
                        if best is None or rank[:3] < best[:3]:
                            best = rank
        if best is None:
            continue
        _score, _timestamp, _token_id, control_row, control_outcome = best
        treatments.append(treatment_outcome)
        controls.append(control_outcome)
        pairs.append(
            {
                "treatment_timestamp": row.timestamp,
                "treatment_token_id": row.token_id,
                "control_timestamp": control_row.timestamp,
                "control_token_id": control_row.token_id,
                "treatment_outcome_5m": treatment_outcome,
                "control_outcome_5m": control_outcome,
                "difference_5m": treatment_outcome - control_outcome,
            }
        )
    differences = [row["difference_5m"] for row in pairs]
    clusters: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in pairs:
        clusters[(str(row["treatment_token_id"]), str(row["control_token_id"]))].append(
            float(row["difference_5m"])
        )
    return {
        "matching_rule": {
            "same_station_day": True,
            "price_tolerance": "0.05 token price",
            "spread_tolerance": "0.02 token price",
            "hour_tolerance": 1,
            "physical_distance_tolerance_f": "1.5",
            "control_state": "EVENT/DIGESTION/PRE_RELEASE/HALTED",
        },
        "matched_pair_count": len(pairs),
        "treatment_outcome_5m": _summary(treatments),
        "control_outcome_5m": _summary(controls),
        "difference_5m": {
            **_summary(differences),
            "cluster_bootstrap_95": _cluster_bootstrap_interval(clusters),
        },
        "pairs": pairs,
        "causal_claim": "diagnostic matched control only; not causal identification",
    }


def _causal_metrics(
    engine: QuietWindowEngine,
    *,
    gap_threshold: Decimal = Decimal("0.05"),
) -> dict[str, Any]:
    horizons = (1, 5, 15, 30)
    all_orders = [order for portfolio in engine.engines.values() for order in portfolio.orders]
    fills = [
        (order, fill)
        for order in all_orders
        for fill in order.fills
    ]
    fills.sort(key=lambda row: (row[1].timestamp, row[0].order_id, row[1].fill_id))
    snapshots_by_key: dict[TokenPortfolioKey, list[SnapshotDiagnostic]] = defaultdict(list)
    for snapshot in engine.diagnostic_snapshots:
        snapshots_by_key[snapshot.portfolio_key].append(snapshot)
    for rows in snapshots_by_key.values():
        rows.sort(key=lambda row: row.timestamp)
    decisions_by_key: dict[TokenPortfolioKey, list[QuietDecision]] = defaultdict(list)
    for decision in engine.decisions:
        key = next(
            (
                portfolio_key
                for portfolio_key, scope in engine._scope_by_key.items()
                if scope == decision.scope and portfolio_key.token_id == decision.token_id
            ),
            None,
        )
        if key is not None:
            decisions_by_key[key].append(decision)
    by_state: dict[str, dict[str, Any]] = {}
    fill_rows_by_state: dict[str, list[tuple[ShadowOrder, ShadowFill]]] = defaultdict(list)
    for order, fill in fills:
        state = str(order.metadata.get("regime_state") or RegimeState.QUIET.value)
        fill_rows_by_state[state].append((order, fill))
    for state in RegimeState:
        state_value = state.value
        attempted = sum(
            decision.action in {"QUIET_QUOTES_SUBMITTED", "CONDITIONAL_REPLENISH"}
            and decision.state.value == state_value
            for decision in engine.decisions
        )
        state_orders = [
            order
            for order in all_orders
            if str(order.metadata.get("regime_state") or RegimeState.QUIET.value) == state_value
        ]
        state_fills = fill_rows_by_state.get(state_value, [])
        by_state[state_value] = {
            "observation_count": engine.state_observation_counts.get(state_value, 0),
            "quote_attempts": attempted,
            "order_count": len(state_orders),
            "filled_order_count": len({order.order_id for order, _fill in state_fills}),
            "fill_rate": _rate(
                len({order.order_id for order, _fill in state_fills}),
                len(state_orders),
            ),
        }
    markouts: dict[str, list[float]] = {f"{minutes}m": [] for minutes in horizons}
    markout_clusters: dict[str, dict[tuple[str, str], list[float]]] = {
        f"{minutes}m": defaultdict(list) for minutes in horizons
    }
    adverse_known = adverse_success = adverse_unknown = 0
    reversion_known = reversion_success = reversion_unknown = 0
    trend_known = trend_success = 0
    jump_known = jump_count = 0
    jump_unknown = 0
    fill_state_counts: dict[str, int] = defaultdict(int)
    round_trip_spread: list[float] = []
    for order, fill in fills:
        key = order.portfolio_key
        rows = snapshots_by_key.get(key, [])
        prior = next((row for row in reversed(rows) if row.timestamp <= fill.timestamp), None)
        future = [row for row in rows if row.timestamp > fill.timestamp]
        state_value = str(order.metadata.get("regime_state") or RegimeState.QUIET.value)
        fill_state_counts[state_value] += 1
        for minutes in horizons:
            target_time = fill.timestamp + timedelta(minutes=minutes)
            row = next(
                (
                    candidate
                    for candidate in future
                    if candidate.timestamp >= target_time
                    and (
                        candidate.best_bid is not None
                        if order.side is ShadowSide.BUY
                        else candidate.best_ask is not None
                    )
                ),
                None,
            )
            if row is None:
                continue
            markout = (
                row.best_bid - float(fill.price)
                if order.side is ShadowSide.BUY
                else float(fill.price) - row.best_ask
            )
            value = float(markout)
            markouts[f"{minutes}m"].append(value)
            cluster = (order.station_id or "unknown", order.market_day)
            markout_clusters[f"{minutes}m"][cluster].append(value)
        row_5m = next(
            (candidate for candidate in future if candidate.timestamp >= fill.timestamp + timedelta(minutes=5)),
            None,
        )
        if order.side is ShadowSide.BUY:
            if row_5m is None or row_5m.best_bid is None:
                adverse_unknown += 1
            else:
                adverse_known += 1
                if row_5m.best_bid < float(fill.price):
                    adverse_success += 1
        if prior is None or prior.mid is None or row_5m is None or row_5m.mid is None:
            reversion_unknown += 1
        else:
            initial = float(fill.price) - prior.mid
            later = row_5m.mid - float(fill.price)
            reversion_known += 1
            if initial != ZERO and initial * later < ZERO:
                reversion_success += 1
            if initial != ZERO and initial * later > ZERO:
                trend_success += 1
            if initial != ZERO:
                trend_known += 1
        if row_5m is None or row_5m.mid is None:
            jump_unknown += 1
        else:
            jump_known += 1
            if abs(row_5m.mid - float(fill.price)) >= float(gap_threshold):
                jump_count += 1
        # Pair only same-token fills and allocate each sell to the first
        # earlier buy with remaining shares.  This is an audit pairing, not a
        # cross-token or complement-price substitution.
        if order.side is ShadowSide.BUY:
            remaining = fill.shares
            for later_order, later_fill in fills:
                if (
                    later_order.portfolio_key == key
                    and later_order.side is ShadowSide.SELL
                    and later_fill.timestamp > fill.timestamp
                    and remaining > ZERO
                ):
                    quantity = min(remaining, later_fill.shares)
                    if quantity > ZERO:
                        round_trip_spread.append(
                            float(later_fill.price - fill.price)
                        )
                        remaining -= quantity
    capital_minutes = ZERO
    for order in all_orders:
        end = order.last_fill_at or order.cancelled_at or order.expired_at
        if end is None:
            end = max(
                (row.timestamp for row in snapshots_by_key.get(order.portfolio_key, ())),
                default=order.submitted_at,
            )
        capital_minutes += order.requested_usd * Decimal(
            max(0.0, (end - order.submitted_at).total_seconds() / 60)
        )
    flat = all(portfolio.inventory_shares == ZERO for portfolio in engine.engines.values())
    realized = sum((portfolio.realized_pnl_usd for portfolio in engine.engines.values()), start=ZERO)
    return_per_cap_hour = (
        float(realized / (capital_minutes / Decimal("60")))
        if flat and not engine.discrepancies and capital_minutes > ZERO
        else None
    )
    regret_decisions = tuple(
        decision
        for decision in engine.decisions
        if decision.action != "SKIP" or decision.missed_opportunity
    )
    regrets = compute_decision_regret(
        regret_decisions,
        engine.diagnostic_snapshots,
        engine._accepted_information_events,
    )
    return {
        "unit": "station/market-day cluster; not token/order/snapshot independent",
        "by_state": by_state,
        "fill_state_counts": dict(sorted(fill_state_counts.items())),
        "fill_count": len(fills),
        "markout_by_horizon": {
            key: {
                **_summary(values),
                "cluster_bootstrap_95": _cluster_bootstrap_interval(markout_clusters[key]),
            }
            for key, values in markouts.items()
        },
        "adverse_selection": {
            "buy_fill_adverse_at_5m": _rate(adverse_success, adverse_known),
            "unknown_count": adverse_unknown,
        },
        "mean_reversion_at_5m": {
            "rate": _rate(reversion_success, reversion_known),
            "unknown_count": reversion_unknown,
        },
        "trend_continuation_at_5m": _rate(trend_success, trend_known),
        "jump_or_gap_at_5m": {
            "rate": _rate(jump_count, jump_known),
            "unknown_count": jump_unknown,
            "threshold": gap_threshold,
        },
        "spread_capture_per_same_token_round_trip": _summary(round_trip_spread),
        "capital_minutes_usd": float(capital_minutes) if capital_minutes else None,
        "return_per_capital_hour": return_per_cap_hour,
        "round_trips": sum(portfolio.round_trip_count for portfolio in engine.engines.values()),
        "missed_opportunities": sum(
            decision.missed_opportunity for decision in engine.decisions
        ),
        "decision_regret": {
            "lookahead_only": True,
            "control_path_used": False,
            "count": len(regrets),
            "eligible_decision_count": len(regret_decisions),
            "plain_skip_decision_count_excluded": len(engine.decisions) - len(regret_decisions),
            "rows": [row.as_dict() for row in regrets],
        },
        "matched_control": _causal_control_diagnostics(engine),
        "statistical_gate": {
            "all_rates_have_wilson_95": True,
            "n_lt_30_label": True,
            "cluster_bootstrap_for_mean_markouts": True,
        },
    }


def _replay_one(
    snapshots: Sequence[BookSnapshot] | Callable[[], Iterator[BookSnapshot]],
    trades: Sequence[TradeEvent],
    information_events: Sequence[InformationEvent],
    *,
    config: QuietWindowConfig,
    tier: ThresholdTier,
    quote_size_usd: Decimal,
    snapshot_count: int | None = None,
    snapshot_cutoff: datetime | None = None,
    scoped_events: Sequence[InformationEvent] | None = None,
    l2_churn_index: L2ChurnIndex | None = None,
    station_timezones: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    selected = replace(
        config,
        threshold_tier=tier,
        quote_size_usd=quote_size_usd,
    )
    selected.validate()
    risk_book = ThreeStrategyRiskBook(cap_usd=selected.station_day_budget_usd)
    engine = QuietWindowEngine(
        config=selected,
        thresholds=predefined_regime_thresholds()[tier],
        risk_book=risk_book,
        retain_trace=False,
        retain_snapshots=False,
        retain_diagnostics=True,
        lazy_engines=True,
        l2_churn_index=l2_churn_index,
        station_timezones=station_timezones,
        use_real_microstructure=True,
    )
    if callable(snapshots):
        snapshot_factory = snapshots
        replay_snapshot_count = snapshot_count
        replay_cutoff = snapshot_cutoff
        if scoped_events is None:
            raise ValueError("streaming replay requires pre-scoped information events")
    else:
        normalised = tuple(snapshots)

        def snapshot_factory() -> Iterator[BookSnapshot]:
            return iter(normalised)

        replay_snapshot_count = len(normalised)
        replay_cutoff = max(
            (row.timestamp for row in normalised), default=snapshot_cutoff
        )
        scoped_events = _scope_events(information_events, normalised)
    clock = InformationClock()
    clock.ingest_many(scoped_events or ())
    pending_trades: dict[str, list[TradeEvent]] = defaultdict(list)
    last_snapshot_time: dict[TokenPortfolioKey, datetime] = {}
    events_by_scope: dict[tuple[str, str], list[InformationEvent]] = defaultdict(list)
    for event in clock.events:
        events_by_scope[event.scope].append(event)
    event_times_by_scope = {
        scope: [event.available_at for event in rows]
        for scope, rows in events_by_scope.items()
    }
    event_cursor_by_key: dict[TokenPortfolioKey, int] = defaultdict(int)
    prediction_history_by_scope: dict[tuple[str, str], list[InformationEvent]] = defaultdict(list)
    prediction_cursor_by_scope: dict[tuple[str, str], int] = defaultdict(int)
    predicted_release_by_scope: dict[tuple[str, str], Any] = {}
    pre_release_diagnostics: dict[str, dict[str, int]] = {
        str(offset): {"eligible_prediction_count": 0, "pre_release_state_count": 0}
        for offset in selected.pre_release_offsets_minutes
    }
    trade_index = 0
    last_snapshot: datetime | None = None
    for snapshot in snapshot_factory():
        snapshot = _as_snapshot(snapshot)
        if last_snapshot is not None and snapshot.timestamp < last_snapshot:
            raise ValueError("streaming replay input must be chronological")
        last_snapshot = snapshot.timestamp
        while trade_index < len(trades):
            trade = trades[trade_index]
            effective_at = max(trade.timestamp, trade.available_at or trade.timestamp)
            if effective_at > snapshot.timestamp:
                break
            pending_trades[trade.asset_id].append(trade)
            # The engine itself enforces timestamp > submitted_at.  Processing
            # trades before the same-time snapshot therefore cannot fill an
            # order created by that snapshot.
            engine.process_trade(trade)
            trade_index += 1

        snapshot_scope = (
            snapshot.station_id or "unknown",
            snapshot.market_day or snapshot.portfolio_key.market_day,
        )
        scope_events = events_by_scope.get(snapshot_scope, [])
        scope_event_times = event_times_by_scope.get(snapshot_scope, [])
        prediction_cursor = prediction_cursor_by_scope[snapshot_scope]
        while (
            prediction_cursor < len(scope_events)
            and scope_event_times[prediction_cursor] < snapshot.timestamp
        ):
            prediction_history_by_scope[snapshot_scope].append(
                scope_events[prediction_cursor]
            )
            prediction_cursor += 1
        if prediction_cursor != prediction_cursor_by_scope[snapshot_scope]:
            predicted_release_by_scope[snapshot_scope] = derive_predictable_release(
                prediction_history_by_scope[snapshot_scope],
                now=snapshot.timestamp,
                scope=snapshot_scope,
                minimum_samples=3,
            )
            prediction_cursor_by_scope[snapshot_scope] = prediction_cursor

        key = snapshot.portfolio_key
        event_end = bisect_right(scope_event_times, snapshot.timestamp)
        event_start = event_cursor_by_key[key]
        events = scope_events[event_start:event_end]
        event_cursor_by_key[key] = event_end
        predicted_release = predicted_release_by_scope.get(snapshot_scope)
        next_release_at = (
            predicted_release.release_at
            if predicted_release is not None and predicted_release.verified
            else None
        )
        prior_snapshot_at = last_snapshot_time.get(key)
        metric_trades = tuple(
            trade
            for trade in pending_trades.get(snapshot.token_id, ())
            if trade.timestamp < snapshot.timestamp
            and (prior_snapshot_at is None or trade.timestamp > prior_snapshot_at)
        )
        pending_trades[snapshot.token_id] = [
            trade
            for trade in pending_trades.get(snapshot.token_id, ())
            if trade.timestamp >= snapshot.timestamp
        ]
        last_snapshot_time[key] = snapshot.timestamp
        decision = engine.process_snapshot(
            snapshot,
            information_events=events,
            next_release_at=next_release_at,
            schedule_verified=next_release_at is not None,
            trades_since_previous=metric_trades,
        )
        if next_release_at is not None:
            seconds_to_release = (next_release_at - snapshot.timestamp).total_seconds()
            for offset in selected.pre_release_offsets_minutes:
                row = pre_release_diagnostics[str(offset)]
                if 0 <= seconds_to_release <= offset * 60:
                    row["eligible_prediction_count"] += 1
                    if decision.state is RegimeState.PRE_RELEASE:
                        row["pre_release_state_count"] += 1
    cutoff = replay_cutoff or last_snapshot or datetime.now(UTC)
    engine.finalize(timestamp=cutoff)
    result = engine.summary(include_trace=False)
    result["information_clock"] = clock.as_dict()
    result["causal_metrics"] = _causal_metrics(engine)
    result["replay_cutoff"] = _required_utc(cutoff)
    if replay_snapshot_count is not None:
        result["raw_snapshot_count"] = replay_snapshot_count
    result["threshold_tier"] = tier.value
    result["quote_size_usd"] = quote_size_usd
    result["pre_release_diagnostics"] = pre_release_diagnostics
    return result


def _normalise_replay_trades(
    trades: Iterable[TradeEvent | Mapping[str, Any]],
) -> tuple[TradeEvent, ...]:
    return tuple(
        sorted(
            (_as_trade(row) for row in trades),
            key=lambda row: (
                max(row.timestamp, row.available_at or row.timestamp),
                row.timestamp,
                row.sequence is None,
                row.sequence or 0,
            ),
        )
    )


def _replay_profile_result(
    store: QuietSnapshotStore,
    trades: Sequence[TradeEvent],
    information_events: Sequence[InformationEvent],
    *,
    config: QuietWindowConfig,
    tier: ThresholdTier,
    quote_size_usd: Decimal,
    scoped_events: Sequence[InformationEvent],
    l2_churn_index: L2ChurnIndex | None = None,
    station_timezones: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _replay_one(
        store.iter_snapshots,
        trades,
        information_events,
        config=config,
        tier=tier,
        quote_size_usd=quote_size_usd,
        snapshot_count=store.snapshot_count,
        snapshot_cutoff=store.end_at,
        scoped_events=scoped_events,
        l2_churn_index=l2_churn_index,
        station_timezones=station_timezones,
    )


def replay_quiet_window(
    snapshots: Iterable[BookSnapshot | Mapping[str, Any]],
    *,
    trades: Iterable[TradeEvent | Mapping[str, Any]] = (),
    information_events: Iterable[InformationEvent] = (),
    config: QuietWindowConfig | None = None,
) -> dict[str, Any]:
    """Run fixed strict/neutral/lenient diagnostics without PnL selection."""
    selected = config or default_quiet_window_config()
    selected.validate()
    normalised_snapshots = normalise_quiet_snapshots(snapshots)
    normalised_trades = _normalise_replay_trades(trades)
    normalised_events = deduplicate_information_events(information_events)
    profiles = {
        tier.value: _replay_one(
            normalised_snapshots,
            normalised_trades,
            normalised_events,
            config=selected,
            tier=tier,
            quote_size_usd=selected.quote_size_usd,
        )
        for tier in ThresholdTier
    }
    return {
        "strategy_family": QUIET_FAMILY,
        "strategy_version": selected.version,
        "execution_enabled": False,
        "selected_threshold_tier": selected.threshold_tier.value,
        "no_parameter_search": True,
        "input_snapshot_count": len(normalised_snapshots),
        "input_trade_count": len(normalised_trades),
        "input_information_event_count": len(normalised_events),
        "config": selected.as_dict(),
        "models": profiles,
        "primary_profile": profiles[selected.threshold_tier.value],
        "station_scope": {
            "primary": list(selected.primary_stations),
            "control": "all other stations retained as ten-city control where available",
        },
        "risk_isolation": profiles[selected.threshold_tier.value]["risk"],
        "conclusion_status": "diagnostic only; no positive-expectation or execution claim",
    }


def replay_quiet_window_store(
    store: QuietSnapshotStore,
    *,
    trades: Iterable[TradeEvent | Mapping[str, Any]] = (),
    information_events: Iterable[InformationEvent] = (),
    config: QuietWindowConfig | None = None,
    station_timezones: Mapping[str, str] | None = None,
    l2_churn_index: L2ChurnIndex | None = None,
) -> dict[str, Any]:
    """Replay a disk-backed source without materialising full depth in RAM."""
    selected = config or default_quiet_window_config()
    selected.validate()
    normalised_trades = _normalise_replay_trades(trades)
    normalised_events = deduplicate_information_events(information_events)
    scoped_events = _scope_events_from_scopes(
        normalised_events,
        store.scopes,
        station_timezones,
    )
    profiles = {
        tier.value: _replay_profile_result(
            store,
            normalised_trades,
            normalised_events,
            config=selected,
            tier=tier,
            quote_size_usd=selected.quote_size_usd,
            scoped_events=scoped_events,
            l2_churn_index=l2_churn_index,
            station_timezones=station_timezones,
        )
        for tier in ThresholdTier
    }
    return {
        "strategy_family": QUIET_FAMILY,
        "strategy_version": selected.version,
        "execution_enabled": False,
        "selected_threshold_tier": selected.threshold_tier.value,
        "no_parameter_search": True,
        "input_snapshot_count": store.snapshot_count,
        "input_trade_count": len(normalised_trades),
        "input_information_event_count": len(normalised_events),
        "config": selected.as_dict(),
        "models": profiles,
        "primary_profile": profiles[selected.threshold_tier.value],
        "station_scope": {
            "primary": list(selected.primary_stations),
            "control": "all other stations retained as ten-city control where available",
        },
        "risk_isolation": profiles[selected.threshold_tier.value]["risk"],
        "conclusion_status": "diagnostic only; no positive-expectation or execution claim",
        "replay_source": "disk_backed_chronological_token_native_books",
    }


def replay_quiet_window_size_grid(
    snapshots: Iterable[BookSnapshot | Mapping[str, Any]],
    *,
    trades: Iterable[TradeEvent | Mapping[str, Any]] = (),
    information_events: Iterable[InformationEvent] = (),
    config: QuietWindowConfig | None = None,
) -> dict[str, Any]:
    """Run the predeclared $20/$50/$100/$200 sizing scenarios."""
    selected = config or default_quiet_window_config()
    selected.validate()
    normalised_snapshots = normalise_quiet_snapshots(snapshots)
    normalised_trades = _normalise_replay_trades(trades)
    normalised_events = deduplicate_information_events(information_events)
    grid: dict[str, Any] = {}
    for size in selected.size_scenarios:
        result = _replay_one(
            normalised_snapshots,
            normalised_trades,
            normalised_events,
            config=selected,
            tier=selected.threshold_tier,
            quote_size_usd=size,
        )
        grid[str(size)] = {
            "quote_size_usd": size,
            "threshold_tier": selected.threshold_tier.value,
            "raw_snapshot_count": result["raw_snapshot_count"],
            "state_observation_counts": result["state_observation_counts"],
            "shadow_order_count": result["shadow_order_count"],
            "fill_count": result["fill_count"],
            "halted": result["halted"],
            "halt_reason": result["halt_reason"],
            "causal_metrics": result["causal_metrics"],
            "pnl_status": result["pnl_status"],
            "risk": result["risk"],
        }
    return {
        "strategy_family": QUIET_FAMILY,
        "strategy_version": selected.version,
        "execution_enabled": False,
        "predeclared_size_scenarios_usd": selected.size_scenarios,
        "grid": grid,
        "no_parameter_search": True,
    }


def replay_quiet_window_size_grid_store(
    store: QuietSnapshotStore,
    *,
    trades: Iterable[TradeEvent | Mapping[str, Any]] = (),
    information_events: Iterable[InformationEvent] = (),
    config: QuietWindowConfig | None = None,
    station_timezones: Mapping[str, str] | None = None,
    l2_churn_index: L2ChurnIndex | None = None,
) -> dict[str, Any]:
    """Run the declared size scenarios against a disk-backed snapshot source."""
    selected = config or default_quiet_window_config()
    selected.validate()
    normalised_trades = _normalise_replay_trades(trades)
    normalised_events = deduplicate_information_events(information_events)
    scoped_events = _scope_events_from_scopes(
        normalised_events,
        store.scopes,
        station_timezones,
    )
    grid: dict[str, Any] = {}
    for size in selected.size_scenarios:
        result = _replay_profile_result(
            store,
            normalised_trades,
            normalised_events,
            config=selected,
            tier=selected.threshold_tier,
            quote_size_usd=size,
            scoped_events=scoped_events,
            l2_churn_index=l2_churn_index,
            station_timezones=station_timezones,
        )
        grid[str(size)] = {
            "quote_size_usd": size,
            "threshold_tier": selected.threshold_tier.value,
            "raw_snapshot_count": result["raw_snapshot_count"],
            "state_observation_counts": result["state_observation_counts"],
            "shadow_order_count": result["shadow_order_count"],
            "fill_count": result["fill_count"],
            "halted": result["halted"],
            "halt_reason": result["halt_reason"],
            "causal_metrics": result["causal_metrics"],
            "pnl_status": result["pnl_status"],
            "risk": result["risk"],
        }
    return {
        "strategy_family": QUIET_FAMILY,
        "strategy_version": selected.version,
        "execution_enabled": False,
        "predeclared_size_scenarios_usd": selected.size_scenarios,
        "grid": grid,
        "no_parameter_search": True,
        "replay_source": "disk_backed_chronological_token_native_books",
    }


def derive_size_grid_from_no_quiet_profile(
    profile: Mapping[str, Any],
    config: QuietWindowConfig,
) -> dict[str, Any]:
    """Derive the declared size grid when the entry gate never opens.

    Size cannot affect a replay before the first QUIET entry quote.  This
    short-circuit is exact, not a best-size selection, and is marked in the
    machine result so it cannot be mistaken for four independently replayed
    fill estimates.
    """
    counts = profile.get("state_observation_counts") or {}
    if counts.get(RegimeState.QUIET.value, 0):
        raise ValueError("no-quiet size-grid shortcut requires zero QUIET observations")
    grid: dict[str, Any] = {}
    for size in config.size_scenarios:
        grid[str(size)] = {
            "quote_size_usd": size,
            "threshold_tier": config.threshold_tier.value,
            "raw_snapshot_count": profile.get("raw_snapshot_count", 0),
            "state_observation_counts": counts,
            "shadow_order_count": 0,
            "fill_count": 0,
            "halted": profile.get("halted", False),
            "halt_reason": profile.get("halt_reason"),
            "causal_metrics": profile.get("causal_metrics") or {},
            "pnl_status": profile.get("pnl_status", "N/A: no fills"),
            "risk": profile.get("risk") or {},
            "short_circuited_no_quiet": True,
        }
    return {
        "strategy_family": QUIET_FAMILY,
        "strategy_version": config.version,
        "execution_enabled": False,
        "predeclared_size_scenarios_usd": config.size_scenarios,
        "grid": grid,
        "no_parameter_search": True,
        "replay_source": "exact_no_quiet_entry_gate_short_circuit",
        "short_circuit_reason": "zero QUIET observations; size cannot affect pre-entry state",
    }


def analyze_information_clock(events: Iterable[InformationEvent]) -> dict[str, Any]:
    clock = InformationClock()
    accepted = clock.ingest_many(events)
    by_source: defaultdict[str, int] = defaultdict(int)
    by_kind: defaultdict[str, int] = defaultdict(int)
    by_kind_impact: defaultdict[str, Counter[str]] = defaultdict(Counter)
    by_station_impact: defaultdict[str, Counter[str]] = defaultdict(Counter)
    revisions: defaultdict[tuple[str, str, str | None], int] = defaultdict(int)
    for event in accepted:
        by_source[event.source] += 1
        by_kind[event.kind] += 1
        impact = (event.impact_class or ImpactClass.NO_OP).value
        by_kind_impact[event.kind][impact] += 1
        by_station_impact[event.station_id or "GLOBAL"][
            impact
        ] += 1
        revisions[(event.source, event.kind, event.source_at.isoformat() if event.source_at else None)] += 1
    for rejected in clock.invalid_events:
        event = rejected.get("event")
        if not isinstance(event, Mapping):
            continue
        kind = str(event.get("kind") or "UNKNOWN")
        station = str(event.get("station_id") or "GLOBAL")
        by_kind_impact[kind][ImpactClass.INVALID.value] += 1
        by_station_impact[station][ImpactClass.INVALID.value] += 1
    return {
        "clock": clock.as_dict(),
        "accepted_event_count": len(accepted),
        "events_by_source": dict(sorted(by_source.items())),
        "events_by_kind": dict(sorted(by_kind.items())),
        "events_by_kind_and_impact_class": {
            kind: dict(sorted(counts.items()))
            for kind, counts in sorted(by_kind_impact.items())
        },
        "events_by_station_and_impact_class": {
            station: dict(sorted(counts.items()))
            for station, counts in sorted(by_station_impact.items())
        },
        "same_source_time_revision_groups": sum(count > 1 for count in revisions.values()),
        "revision_group_max_size": max(revisions.values(), default=0),
        "receipt_gate": "source_at <= available_at <= decision_at",
        "missing_receipt_policy": "phase ineligible; never resets EVENT/DIGESTION/QUIET/PRE_RELEASE",
        "open_meteo_run_initialization_policy": "explicit initialization required; missing remains N/A",
        "impact_policy": {
            "HARD_RESET": "new daily high, verified forecast distribution, physical state, settlement/status or critical weather-regime change",
            "SOFT_UPDATE": "visible risk-field change without a hard anchor; fixed DIGESTION/cancel rule",
            "NO_OP": "new receipt timestamp alone or unchanged semantic state; diagnostic only",
            "INVALID": "missing/inconsistent receipt or unknown non-global scope; rejected",
        },
        "execution_enabled": False,
    }


def render_information_reaction_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clock = result.get("clock") if isinstance(result.get("clock"), Mapping) else result
    lines = [
        "# External-information clock and four-state reaction report",
        "",
        "This is a read-only, receipt-gated diagnostic. Missing source/receipt timestamps are not used to reset a regime.",
        "",
        f"- Accepted events: `{clock.get('event_count', result.get('accepted_event_count', 0))}`",
        f"- Invalid/missing-receipt events: `{clock.get('invalid_event_count', 0)}`",
        f"- Receipt gate: `{result.get('receipt_gate', 'source_at <= available_at <= decision_at')}`",
        f"- Same-source-time revision groups: `{result.get('same_source_time_revision_groups', 'N/A')}`",
        "",
        "## Event kinds",
        "",
        "| Kind | Count |",
        "|---|---:|",
    ]
    for kind, count in sorted((result.get("events_by_kind") or {}).items()):
        lines.append(f"| {kind} | {count} |")
    lines.extend(
        [
            "",
            "## Event importance (v2 present-time classification)",
            "",
            "| Kind | HARD_RESET | SOFT_UPDATE | NO_OP | INVALID |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    by_kind_impact = (
        result.get("events_by_kind_and_impact_class")
        or clock.get("events_by_kind_and_impact_class")
        or {}
    )
    for kind, counts in sorted(by_kind_impact.items()):
        lines.append(
            f"| {kind} | {counts.get('HARD_RESET', 0)} | {counts.get('SOFT_UPDATE', 0)} | {counts.get('NO_OP', 0)} | {counts.get('INVALID', 0)} |"
        )
    by_station_impact = result.get("events_by_station_and_impact_class") or {}
    if by_station_impact:
        lines.extend(
            [
                "",
                "## Event importance by station",
                "",
                "| Station | HARD_RESET | SOFT_UPDATE | NO_OP | INVALID |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for station, counts in sorted(by_station_impact.items()):
            lines.append(
                f"| {station} | {counts.get('HARD_RESET', 0)} | {counts.get('SOFT_UPDATE', 0)} | {counts.get('NO_OP', 0)} | {counts.get('INVALID', 0)} |"
            )
    lines.extend(
        [
            "",
            "`INVALID` rows are rejected before state progression.  A new report timestamp alone is not a reset.  Regular weather products use fixed material-risk bands (wind, cloud, phenomena, dew point); decimal/report-format jitter inside a band is NO_OP.",
            "",
            "## Reaction samples by fixed threshold tier",
            "",
            "| Tier | Reaction samples | p50 minutes | p90 minutes | n<30 |",
            "|---|---:|---:|---:|---|",
        ]
    )
    profiles = result.get("models") or result.get("threshold_profiles") or {}
    for tier, profile in sorted(profiles.items()):
        reaction = profile.get("reaction_duration") or {}
        count = reaction.get("sample_count", 0)
        lines.append(
            f"| {tier} | {count} | {reaction.get('p50_minutes')} | {reaction.get('p90_minutes')} | {'yes' if count < 30 else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Four-state observation counts",
            "",
            "| Tier | EVENT | DIGESTION | QUIET | PRE_RELEASE | HALTED |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for tier, profile in sorted((result.get("models") or {}).items()):
        counts = profile.get("state_observation_counts") or {}
        lines.append(
            f"| {tier} | {counts.get('EVENT', 0)} | {counts.get('DIGESTION', 0)} | {counts.get('QUIET', 0)} | {counts.get('PRE_RELEASE', 0)} | {counts.get('HALTED', 0)} |"
        )
    lines.extend(
        [
            "",
            "No tier is selected by realized PnL. State thresholds are declared before replay and reported as sensitivity diagnostics.",
            "",
            "A v1 result with `QUIET=0` is N/A under incomplete event semantics/metric coverage; it is not evidence that no quiet interval exists.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_quiet_window_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# QUIET-window maker validation",
        "",
        "Read-only historical shadow replay. Touch is an optimistic upper bound; queue-aware and trade-through use token-native public trade events. No row is an exchange order or execution acknowledgement.",
        "",
        f"- Strategy family: `{result.get('strategy_family')}`",
        f"- Strategy version: `{result.get('strategy_version')}`",
        "- Execution enabled: `false`",
        f"- No parameter search: `{result.get('no_parameter_search')}`",
        f"- Input book snapshots: `{result.get('input_snapshot_count')}`",
        f"- Input information events: `{result.get('input_information_event_count')}`",
        "",
        "## Fixed threshold profiles",
        "",
        "| Tier | Snapshots | Orders | Fills | Market-day clusters | Halted | PnL status |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    for tier, profile in sorted((result.get("models") or {}).items()):
        lines.append(
            f"| {tier} | {profile.get('raw_snapshot_count')} | {profile.get('shadow_order_count')} | {profile.get('fill_count')} | {profile.get('station_day_cluster_count')} | {profile.get('halted')} | {profile.get('pnl_status')} |"
        )
    lines.extend(
        [
            "",
            "## Four-state observation counts",
            "",
            "| Tier | EVENT | DIGESTION | QUIET | PRE_RELEASE | HALTED |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for tier, profile in sorted((result.get("models") or {}).items()):
        counts = profile.get("state_observation_counts") or {}
        lines.append(
            f"| {tier} | {counts.get('EVENT', 0)} | {counts.get('DIGESTION', 0)} | {counts.get('QUIET', 0)} | {counts.get('PRE_RELEASE', 0)} | {counts.get('HALTED', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Mandatory metric coverage (primary profile)",
            "",
            "Unknown is never converted to zero.  `WARMUP_INSUFFICIENT_BASELINE`, `UNQUOTABLE_INCOMPLETE_BOOK`, `UNKNOWN_TAPE_GAP`, and `UNKNOWN_CROSS_BUCKET_SYNC` are coverage states; `UNSTABLE_TRUE_VIOLATION:*` is a known failed stability check.",
            "",
            "| Metric | Known / observations | Known % | Status counts |",
            "|---|---:|---:|---|",
        ]
    )
    primary = result.get("primary_profile") or {}
    for metric, coverage in sorted((primary.get("metric_coverage") or {}).items()):
        total = coverage.get("observation_count", 0)
        known = coverage.get("known_count", 0)
        fraction = coverage.get("known_fraction")
        lines.append(
            f"| {metric} | {known} / {total} | {fraction:.1%} | {coverage.get('status_counts')} |"
            if isinstance(fraction, (int, float))
            else f"| {metric} | {known} / {total} | N/A | {coverage.get('status_counts')} |"
        )
    lines.extend(
        [
            "",
            "## Non-QUIET reason distribution (primary profile)",
            "",
            "| Reason | Observations |",
            "|---|---:|",
        ]
    )
    for reason, count in sorted((primary.get("non_quiet_reason_counts") or {}).items()):
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            "## Why mandatory gates blocked QUIET (primary profile)",
            "",
            "Coverage states are unavailable observations, not zero-valued stability metrics.  True violations are separately observed failed checks.",
            "",
            "| Coverage state | Non-QUIET observations |",
            "|---|---:|",
        ]
    )
    coverage_blocks = primary.get("coverage_blocking_counts") or {}
    if coverage_blocks:
        for reason, count in sorted(coverage_blocks.items()):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(["", "| True stability violation | Non-QUIET observations |", "|---|---:|"])
    true_violations = primary.get("true_violation_counts") or {}
    if true_violations:
        for reason, count in sorted(true_violations.items()):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| none | 0 |")
    causal = primary.get("causal_metrics") or {}
    lines.extend(
        [
            "",
            "## Primary profile causal diagnostics",
            "",
            f"- Unit: {causal.get('unit')}",
            f"- Round trips: `{causal.get('round_trips')}`",
            f"- Missed opportunities: `{causal.get('missed_opportunities')}`",
            f"- Return per capital hour: `{causal.get('return_per_capital_hour')}`",
            f"- Matched control pairs: `{(causal.get('matched_control') or {}).get('matched_pair_count')}`",
            "",
            "| Horizon | Markout mean | Markout p50 | Cluster bootstrap 95% |",
            "|---|---:|---:|---|",
        ]
    )
    for horizon, values in sorted((causal.get("markout_by_horizon") or {}).items()):
        lines.append(
            f"| {horizon} | {values.get('mean')} | {values.get('p50')} | {values.get('cluster_bootstrap_95')} |"
        )
    lines.extend(["", "## Pre-release diagnostics", ""])
    lines.append("| Offset minutes | Prediction-eligible snapshots | PRE_RELEASE state snapshots |")
    lines.append("|---:|---:|---:|")
    for tier, profile in sorted((result.get("models") or {}).items()):
        for offset, values in sorted((profile.get("pre_release_diagnostics") or {}).items(), key=lambda row: int(row[0])):
            lines.append(
                f"| {tier} / {offset} | {values.get('eligible_prediction_count')} | {values.get('pre_release_state_count')} |"
            )
    lines.extend(
        [
            "",
            "## Safety and interpretation",
            "",
            "- New entry quotes are allowed only in QUIET; EVENT, DIGESTION, PRE_RELEASE and HALTED are reduce-only/cancel states.",
            "- Maintenance, stale, incomplete, unverified-settlement, scope, receipt, ledger and cap mismatches stop everything and stop accumulating.",
            "- Decision regret is computed only after the replay from strictly later observations; it cannot change a historical quote, cancel, or refill.",
            "- All ratios carry Wilson 95% bounds and n<30 is marked statistically unreliable. Station/market-day is the independent cluster.",
            "- `1-p`, midpoint, trade price, and the other token's book are never used as a quote or exit price.",
            "",
            "Conclusion: diagnostic evidence only; this report does not establish positive expectation or execution feasibility. If QUIET remains zero, read the coverage and true-violation tables before interpreting it as a market result.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_quiet_window_result(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_value(dict(result)), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "DEFAULT_QUIET_LEDGER",
    "DecisionRegret",
    "QUIET_FAMILY",
    "QuietDecision",
    "QuietSnapshotStore",
    "QuietWindowConfig",
    "QuietWindowEngine",
    "StopEverythingInvariant",
    "StrategyFamily",
    "StrategyFamilyRegistration",
    "ThreeStrategyRiskBook",
    "analyze_information_clock",
    "compute_decision_regret",
    "default_quiet_window_config",
    "derive_size_grid_from_no_quiet_profile",
    "normalise_quiet_snapshots",
    "render_information_reaction_report",
    "render_quiet_window_report",
    "replay_quiet_window",
    "replay_quiet_window_store",
    "replay_quiet_window_size_grid",
    "replay_quiet_window_size_grid_store",
    "SnapshotDiagnostic",
    "stop_everything_mismatches",
    "write_quiet_snapshot_store",
    "write_quiet_window_result",
]
