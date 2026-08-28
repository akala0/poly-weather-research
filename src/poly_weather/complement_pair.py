"""Read-only YES+NO complement-pair maker replay.

The module models one binary market's YES and NO tokens as two *separate*
token portfolios.  A ``ComplementPairPortfolio`` is only an accounting link
between those portfolios after both token-native fills occur.  It deliberately
does not share a ledger, inventory, cost basis, or active orders with the
weather lead-lag shadow strategy.

Planned maker limits below one are not a locked pair.  A pair becomes locked
only after equal YES/NO shares have actually filled and their realised VWAPs
plus fees remain below one.  Touch is an optimistic diagnostic; conclusions
must use queue-aware or trade-through fills.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Any

from poly_weather.no_forward import wilson_interval
from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    ShadowFill,
    ShadowOrder,
    ShadowOrderEngine,
    ShadowOrderRejected,
    ShadowPortfolioInvariantError,
    ShadowSide,
    StationDayRiskManager,
    TradeEvent,
    taker_fee_usdc,
)

ZERO = Decimal("0")
ONE = Decimal("1")
MAKER_MODELS = (FillModel.TOUCH, FillModel.QUEUE_AWARE, FillModel.TRADE_THROUGH)


def _decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _as_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


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


class ComplementPairQuoteMode(StrEnum):
    JOIN_BEST_BID = "join_best_bid"
    IMPROVE_ONE_TICK = "improve_one_tick"


class ComplementPairState(StrEnum):
    NEW = "new"
    RESTING = "resting"
    UNMATCHED = "unmatched"
    HEDGING = "hedging"
    LOCKED = "locked"
    REDUCE_ONLY = "reduce_only"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class ComplementPairKey:
    """Identity of exactly one YES/NO binary market and its local day."""

    event_id: str
    market_id: str
    yes_token_id: str
    no_token_id: str
    market_day: str

    @classmethod
    def from_snapshots(
        cls, yes: BookSnapshot, no: BookSnapshot
    ) -> ComplementPairKey:
        if yes.event_id != no.event_id or yes.market_id != no.market_id:
            raise ShadowPortfolioInvariantError(
                "complement_pair_market_mismatch",
                details={
                    "yes_event_id": yes.event_id,
                    "no_event_id": no.event_id,
                    "yes_market_id": yes.market_id,
                    "no_market_id": no.market_id,
                },
            )
        if yes.token_id == no.token_id:
            raise ShadowPortfolioInvariantError(
                "complement_pair_tokens_must_be_distinct",
                details={"token_id": yes.token_id},
            )
        yes_day = yes.market_day or yes.timestamp.date().isoformat()
        no_day = no.market_day or no.timestamp.date().isoformat()
        if yes_day != no_day:
            raise ShadowPortfolioInvariantError(
                "complement_pair_market_day_mismatch",
                details={"yes_market_day": yes_day, "no_market_day": no_day},
            )
        return cls(yes.event_id, yes.market_id, yes.token_id, no.token_id, yes_day)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ComplementPairConfig:
    """Precommitted pair-maker parameters; not an optimised parameter search."""

    version: str = "shadow-complement-pair-v1"
    quote_mode: ComplementPairQuoteMode = ComplementPairQuoteMode.JOIN_BEST_BID
    safety_buffer: Decimal = Decimal("0.02")
    pair_notional_usd: Decimal = Decimal("20")
    station_day_budget_usd: Decimal = Decimal("200")
    hedge_timeout: timedelta = timedelta(minutes=15)
    max_unmatched_usd: Decimal = Decimal("20")
    max_hedge_price: Decimal = Decimal("0.99")
    taker_hedge_enabled: bool = True
    require_season_version: bool = True
    primary_stations: tuple[str, ...] = ("KLAX", "KLGA")

    def __post_init__(self) -> None:
        object.__setattr__(self, "quote_mode", ComplementPairQuoteMode(self.quote_mode))
        for field_name in (
            "safety_buffer",
            "pair_notional_usd",
            "station_day_budget_usd",
            "max_unmatched_usd",
            "max_hedge_price",
        ):
            object.__setattr__(self, field_name, _decimal(getattr(self, field_name)))
        self.validate()

    def validate(self) -> None:
        if not self.version:
            raise ValueError("complement pair strategy version is required")
        if not ZERO < self.safety_buffer < ONE:
            raise ValueError("safety_buffer must be in (0, 1)")
        if self.pair_notional_usd <= ZERO or self.station_day_budget_usd <= ZERO:
            raise ValueError("pair and station-day budgets must be positive")
        if self.pair_notional_usd > self.station_day_budget_usd:
            raise ValueError("one pair cannot exceed the station-day budget")
        if self.max_unmatched_usd <= ZERO:
            raise ValueError("max_unmatched_usd must be positive")
        if not ZERO < self.max_hedge_price <= ONE:
            raise ValueError("max_hedge_price must be in (0, 1]")
        if self.hedge_timeout <= timedelta(0):
            raise ValueError("hedge_timeout must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "quote_mode": str(self.quote_mode),
            "safety_buffer": str(self.safety_buffer),
            "pair_notional_usd": str(self.pair_notional_usd),
            "station_day_budget_usd": str(self.station_day_budget_usd),
            "hedge_timeout_minutes": self.hedge_timeout.total_seconds() / 60,
            "max_unmatched_usd": str(self.max_unmatched_usd),
            "max_hedge_price": str(self.max_hedge_price),
            "taker_hedge_enabled": self.taker_hedge_enabled,
            "primary_stations": list(self.primary_stations),
            "execution_enabled": False,
        }


def default_complement_pair_config() -> ComplementPairConfig:
    return ComplementPairConfig()


def predefined_complement_pair_configs(
    base: ComplementPairConfig | None = None,
) -> tuple[ComplementPairConfig, ...]:
    """Return the declared quote/buffer/timeout sensitivity grid.

    The grid is diagnostic only.  It is intentionally fixed before replay and
    is never selected by maximising a realised outcome.
    """
    seed = base or default_complement_pair_config()
    return tuple(
        replace(
            seed,
            quote_mode=quote_mode,
            safety_buffer=safety_buffer,
            hedge_timeout=timedelta(minutes=timeout_minutes),
        )
        for quote_mode in (
            ComplementPairQuoteMode.JOIN_BEST_BID,
            ComplementPairQuoteMode.IMPROVE_ONE_TICK,
        )
        for safety_buffer in (Decimal("0.01"), Decimal("0.02"), Decimal("0.03"))
        for timeout_minutes in (5, 15, 30, 60)
    )


@dataclass(slots=True)
class _FillLot:
    shares: Decimal
    price: Decimal
    fee_usd: Decimal
    timestamp: datetime
    source: str
    model: FillModel

    @property
    def cost_per_share(self) -> Decimal:
        return self.price + self.fee_usd / self.shares


@dataclass(frozen=True, slots=True)
class LockedPair:
    shares: Decimal
    yes_cost_per_share: Decimal
    no_cost_per_share: Decimal
    first_fill_at: datetime
    completed_at: datetime
    yes_source: str
    no_source: str

    @property
    def combined_cost_per_share(self) -> Decimal:
        return self.yes_cost_per_share + self.no_cost_per_share

    @property
    def locked_edge_per_share(self) -> Decimal:
        return ONE - self.combined_cost_per_share

    @property
    def locked_edge_usd(self) -> Decimal:
        return self.shares * self.locked_edge_per_share


def _post_only_quote(
    snapshot: BookSnapshot, mode: ComplementPairQuoteMode
) -> Decimal | None:
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    if bid is None or ask is None or bid >= ask:
        return None
    if mode is ComplementPairQuoteMode.JOIN_BEST_BID:
        return bid
    quote = bid + snapshot.tick_size
    return quote if quote < ask else None


def _taker_cap_for_remaining_cost(
    snapshot: BookSnapshot,
    *,
    remaining_pair_cost: Decimal,
    max_hedge_price: Decimal,
) -> Decimal | None:
    """Find the largest token-native ask whose fee-inclusive cost fits."""
    cap = min(max_hedge_price, remaining_pair_cost, ONE)
    cap = (cap / snapshot.tick_size).to_integral_value(rounding=ROUND_FLOOR) * snapshot.tick_size
    while cap > ZERO:
        if cap + taker_fee_usdc(Decimal("1"), cap) <= remaining_pair_cost:
            return cap
        cap -= snapshot.tick_size
    return None


class ComplementPairPortfolio:
    """One isolated binary YES/NO pair with two token-scoped engines."""

    def __init__(
        self,
        *,
        key: ComplementPairKey,
        yes: BookSnapshot,
        no: BookSnapshot,
        station_id: str,
        config: ComplementPairConfig,
        fill_model: FillModel,
    ) -> None:
        self.key = key
        self.station_id = station_id
        self.config = config
        self.fill_model = fill_model
        self.yes_engine = ShadowOrderEngine(
            portfolio_key=yes.portfolio_key,
            budget_usd=config.station_day_budget_usd,
            max_active_orders=1,
            fill_model=fill_model,
            order_timeout=config.hedge_timeout,
            require_season_version=config.require_season_version,
        )
        self.no_engine = ShadowOrderEngine(
            portfolio_key=no.portfolio_key,
            budget_usd=config.station_day_budget_usd,
            max_active_orders=1,
            fill_model=fill_model,
            order_timeout=config.hedge_timeout,
            require_season_version=config.require_season_version,
        )
        self.state = ComplementPairState.NEW
        self.candidate_count = 0
        self.submitted_pair_count = 0
        self.had_one_leg_fill = False
        self.planned_shares: Decimal | None = None
        self._yes_lots: deque[_FillLot] = deque()
        self._no_lots: deque[_FillLot] = deque()
        self.locked_pairs: list[LockedPair] = []
        self.unprofitable_paired_shares = ZERO
        self.first_unmatched_at: datetime | None = None
        self.unmatched_durations_minutes: list[float] = []
        self.max_unmatched_usd_observed = ZERO
        self.unwind_pnl_usd = ZERO
        self.unwind_fill_count = 0
        self.halted = False
        self.halt_reason: str | None = None
        self.last_yes: BookSnapshot = yes
        self.last_no: BookSnapshot = no
        self.rejection_reasons: list[str] = []

    @property
    def engines(self) -> tuple[ShadowOrderEngine, ShadowOrderEngine]:
        return self.yes_engine, self.no_engine

    @property
    def active_orders(self) -> tuple[ShadowOrder, ...]:
        return (*self.yes_engine.active_orders, *self.no_engine.active_orders)

    @property
    def paired_shares(self) -> Decimal:
        return sum((pair.shares for pair in self.locked_pairs), start=ZERO)

    @property
    def locked_edge_usd(self) -> Decimal:
        return sum((pair.locked_edge_usd for pair in self.locked_pairs), start=ZERO)

    @property
    def locked_cost_usd(self) -> Decimal:
        return sum((pair.shares * pair.combined_cost_per_share for pair in self.locked_pairs), start=ZERO)

    @property
    def combined_vwap(self) -> Decimal | None:
        return self.locked_cost_usd / self.paired_shares if self.paired_shares else None

    @property
    def unmatched_side(self) -> str | None:
        if self._yes_lots and self._no_lots:
            self._halt("unmatched_lots_exist_on_both_sides")
            return None
        if self._yes_lots:
            return "yes"
        if self._no_lots:
            return "no"
        return None

    @property
    def unmatched_shares(self) -> Decimal:
        lots = self._yes_lots if self._yes_lots else self._no_lots
        return sum((lot.shares for lot in lots), start=ZERO)

    @property
    def unmatched_cost_usd(self) -> Decimal:
        lots = self._yes_lots if self._yes_lots else self._no_lots
        return sum((lot.shares * lot.cost_per_share for lot in lots), start=ZERO)

    def _halt(self, code: str) -> None:
        self.halted = True
        self.halt_reason = code
        self.state = ComplementPairState.HALTED
        self._cancel_active(self.last_yes.timestamp, reason=code)

    def _assert_pair(self, yes: BookSnapshot, no: BookSnapshot) -> None:
        actual = ComplementPairKey.from_snapshots(yes, no)
        if actual != self.key:
            raise ShadowPortfolioInvariantError(
                "complement_pair_key_rebinding_attempt",
                details={"expected": self.key.as_dict(), "actual": actual.as_dict()},
            )
        if (yes.station_id or self.station_id) != self.station_id or (no.station_id or self.station_id) != self.station_id:
            raise ShadowPortfolioInvariantError(
                "complement_pair_station_mismatch",
                details={
                    "expected_station_id": self.station_id,
                    "yes_station_id": yes.station_id,
                    "no_station_id": no.station_id,
                },
            )

    def _cancel_active(self, timestamp: datetime, *, reason: str) -> None:
        self.yes_engine.cancel_all(timestamp=timestamp, reason=reason)
        self.no_engine.cancel_all(timestamp=timestamp, reason=reason)

    def _record_fills(self, side: str, fills: Iterable[ShadowFill]) -> None:
        lots = self._yes_lots if side == "yes" else self._no_lots
        for fill in fills:
            lots.append(
                _FillLot(
                    shares=fill.shares,
                    price=fill.price,
                    fee_usd=fill.fee_usd,
                    timestamp=fill.timestamp,
                    source=fill.source,
                    model=fill.model,
                )
            )
        self._match_filled_lots()
        if self.unmatched_side is not None:
            self.had_one_leg_fill = True
            unmatched_at = min(
                (lot.timestamp for lot in (*self._yes_lots, *self._no_lots)),
                default=None,
            )
            if self.first_unmatched_at is None:
                self.first_unmatched_at = unmatched_at
            self.max_unmatched_usd_observed = max(
                self.max_unmatched_usd_observed, self.unmatched_cost_usd
            )
            if not self.halted:
                self.state = ComplementPairState.UNMATCHED
        elif self.first_unmatched_at is not None:
            self.unmatched_durations_minutes.append(
                max(
                    0.0,
                    (self.last_yes.timestamp - self.first_unmatched_at).total_seconds() / 60,
                )
            )
            self.first_unmatched_at = None
            if self.paired_shares:
                self.state = ComplementPairState.LOCKED

    def _match_filled_lots(self) -> None:
        while self._yes_lots and self._no_lots:
            yes_lot = self._yes_lots[0]
            no_lot = self._no_lots[0]
            shares = min(yes_lot.shares, no_lot.shares)
            if shares <= ZERO:
                self._halt("nonpositive_complement_pair_fill")
                return
            record = LockedPair(
                shares=shares,
                yes_cost_per_share=yes_lot.cost_per_share,
                no_cost_per_share=no_lot.cost_per_share,
                first_fill_at=min(yes_lot.timestamp, no_lot.timestamp),
                completed_at=max(yes_lot.timestamp, no_lot.timestamp),
                yes_source=yes_lot.source,
                no_source=no_lot.source,
            )
            if record.locked_edge_per_share > ZERO:
                self.locked_pairs.append(record)
            else:
                # A completed pair whose realised all-in cost is not below one
                # is not a locked edge and must never enter return statistics.
                self.unprofitable_paired_shares += shares
                self.state = ComplementPairState.REDUCE_ONLY
            yes_lot.shares -= shares
            no_lot.shares -= shares
            if yes_lot.shares == ZERO:
                self._yes_lots.popleft()
            if no_lot.shares == ZERO:
                self._no_lots.popleft()

    def _submit_pair(
        self,
        *,
        yes: BookSnapshot,
        no: BookSnapshot,
        risk_manager: StationDayRiskManager,
        cluster_engines: Sequence[ShadowOrderEngine],
    ) -> None:
        yes_quote = _post_only_quote(yes, self.config.quote_mode)
        no_quote = _post_only_quote(no, self.config.quote_mode)
        if yes_quote is None or no_quote is None:
            return
        planned_cost = yes_quote + no_quote
        if planned_cost >= ONE - self.config.safety_buffer:
            return
        self.candidate_count += 1
        shares = self.config.pair_notional_usd / planned_cost
        minimum = max(yes.min_order_size, no.min_order_size)
        if shares < minimum:
            self.rejection_reasons.append("pair_min_order_size")
            return
        requested_usd = shares * planned_cost
        if not risk_manager.can_reserve_buy(cluster_engines, requested_usd=requested_usd):
            self.rejection_reasons.append("station_day_budget_exceeded")
            return
        marker = f"{self.key.event_id}:{self.key.market_id}:{yes.timestamp.isoformat()}"
        metadata = {
            "strategy_family": "complement_pair",
            "pair_key": self.key.as_dict(),
            "planned_pair_cost": str(planned_cost),
            "safety_buffer": str(self.config.safety_buffer),
        }
        try:
            yes_order = self.yes_engine.submit_limit(
                yes,
                side=ShadowSide.BUY,
                limit_price=yes_quote,
                shares=shares,
                idempotency_key=f"{marker}:yes-maker",
                strategy_version=self.config.version,
                season_version=yes.season_version,
                trigger_reason="complement_pair_maker",
                metadata=metadata,
            )
            try:
                self.no_engine.submit_limit(
                    no,
                    side=ShadowSide.BUY,
                    limit_price=no_quote,
                    shares=shares,
                    idempotency_key=f"{marker}:no-maker",
                    strategy_version=self.config.version,
                    season_version=no.season_version,
                    trigger_reason="complement_pair_maker",
                    metadata=metadata,
                )
            except (ShadowOrderRejected, ValueError):
                self.yes_engine.cancel(
                    yes_order.order_id,
                    timestamp=yes.timestamp,
                    reason="complement_pair_second_leg_rejected",
                )
                self.rejection_reasons.append("second_leg_rejected")
                return
        except (ShadowOrderRejected, ValueError) as exc:
            self.rejection_reasons.append(type(exc).__name__)
            return
        self.submitted_pair_count += 1
        self.planned_shares = shares
        self.state = ComplementPairState.RESTING

    def _consume_unmatched_lots(self, side: str, shares: Decimal) -> None:
        lots = self._yes_lots if side == "yes" else self._no_lots
        remaining = shares
        while lots and remaining > ZERO:
            lot = lots[0]
            consumed = min(lot.shares, remaining)
            lot.shares -= consumed
            remaining -= consumed
            if lot.shares == ZERO:
                lots.popleft()
        if remaining > ZERO:
            self._halt("unwind_exceeds_unmatched_inventory")

    def _unwind_unmatched(self, snapshot: BookSnapshot, side: str) -> None:
        engine = self.yes_engine if side == "yes" else self.no_engine
        requested = self.unmatched_shares
        if requested <= ZERO:
            return
        before_pnl = engine.realized_pnl_usd
        fills = engine.simulate_taker_exit(
            snapshot,
            shares=requested,
            reason="complement_pair_unmatched_unwind",
        )
        exited = sum((fill.shares for fill in fills), start=ZERO)
        if exited:
            self._consume_unmatched_lots(side, exited)
            self.unwind_fill_count += len(fills)
            self.unwind_pnl_usd += engine.realized_pnl_usd - before_pnl
        if self.unmatched_side is None:
            if self.first_unmatched_at is not None:
                self.unmatched_durations_minutes.append(
                    max(0.0, (snapshot.timestamp - self.first_unmatched_at).total_seconds() / 60)
                )
                self.first_unmatched_at = None
            self.state = ComplementPairState.REDUCE_ONLY

    def _enforce_unmatched_limit(self, timestamp: datetime) -> None:
        side = self.unmatched_side
        if side is None or self.halted:
            return
        first = self.first_unmatched_at or timestamp
        over_timeout = timestamp - first >= self.config.hedge_timeout
        over_exposure = self.unmatched_cost_usd > self.config.max_unmatched_usd
        if not over_timeout and not over_exposure:
            return
        self.state = ComplementPairState.HEDGING
        self._cancel_active(timestamp, reason="complement_pair_hedge_timer")
        missing_side = "no" if side == "yes" else "yes"
        missing_snapshot = self.last_no if missing_side == "no" else self.last_yes
        existing_cost_per_share = self.unmatched_cost_usd / self.unmatched_shares
        remaining_pair_cost = ONE - self.config.safety_buffer - existing_cost_per_share
        if self.config.taker_hedge_enabled and missing_snapshot.health_ok:
            cap = _taker_cap_for_remaining_cost(
                missing_snapshot,
                remaining_pair_cost=remaining_pair_cost,
                max_hedge_price=self.config.max_hedge_price,
            )
            if cap is not None:
                missing_engine = self.no_engine if missing_side == "no" else self.yes_engine
                fills = missing_engine.simulate_taker_entry(
                    missing_snapshot,
                    shares=self.unmatched_shares,
                    max_price=cap,
                    reason="complement_pair_taker_hedge",
                )
                self._record_fills(missing_side, fills)
        remaining_side = self.unmatched_side
        if remaining_side is not None:
            unwind_snapshot = self.last_yes if remaining_side == "yes" else self.last_no
            if unwind_snapshot.health_ok:
                self._unwind_unmatched(unwind_snapshot, remaining_side)
            else:
                self.state = ComplementPairState.REDUCE_ONLY

    def process_snapshot(
        self,
        yes: BookSnapshot,
        no: BookSnapshot,
        *,
        risk_manager: StationDayRiskManager,
        cluster_engines: Sequence[ShadowOrderEngine],
    ) -> None:
        if self.halted:
            return
        self._assert_pair(yes, no)
        self.last_yes, self.last_no = yes, no
        self._record_fills("yes", self.yes_engine.process_snapshot(yes))
        self._record_fills("no", self.no_engine.process_snapshot(no))
        if not yes.health_ok or not no.health_ok:
            self._cancel_active(max(yes.timestamp, no.timestamp), reason="complement_pair_health_gate")
            if self.unmatched_side is not None:
                self.state = ComplementPairState.REDUCE_ONLY
            return
        if self.state is ComplementPairState.NEW and self.submitted_pair_count == 0:
            self._submit_pair(
                yes=yes,
                no=no,
                risk_manager=risk_manager,
                cluster_engines=cluster_engines,
            )
        self._enforce_unmatched_limit(max(yes.timestamp, no.timestamp))

    def process_trade(self, trade: TradeEvent) -> None:
        if self.halted:
            return
        if trade.asset_id == self.key.yes_token_id:
            self._record_fills("yes", self.yes_engine.process_trade(trade))
        elif trade.asset_id == self.key.no_token_id:
            self._record_fills("no", self.no_engine.process_trade(trade))
        else:
            self._halt("cross_token_trade_routed_to_complement_pair")

    def summary(self, cutoff: datetime | None = None) -> dict[str, Any]:
        cutoff_at = cutoff or max(self.last_yes.timestamp, self.last_no.timestamp)
        first_fill = min(
            (fill.timestamp for order in (*self.yes_engine.orders, *self.no_engine.orders) for fill in order.fills),
            default=None,
        )
        capital_minutes = sum(
            (
                pair.shares
                * pair.combined_cost_per_share
                * Decimal(str((pair.completed_at - pair.first_fill_at).total_seconds() / 60))
                for pair in self.locked_pairs
            ),
            start=ZERO,
        )
        if first_fill is not None and self.unmatched_side is not None:
            capital_minutes += self.unmatched_cost_usd * Decimal(
                str(max(0.0, (cutoff_at - first_fill).total_seconds() / 60))
            )
        return {
            "pair_key": self.key.as_dict(),
            "station_id": self.station_id,
            "market_day": self.key.market_day,
            "state": str(self.state),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "candidate_count": self.candidate_count,
            "submitted_pair_count": self.submitted_pair_count,
            "had_one_leg_fill": self.had_one_leg_fill,
            "paired_shares": str(self.paired_shares),
            "planned_shares": str(self.planned_shares) if self.planned_shares is not None else None,
            "fully_completed": bool(
                self.planned_shares is not None and self.paired_shares >= self.planned_shares
            ),
            "combined_vwap": str(self.combined_vwap) if self.combined_vwap is not None else None,
            "locked_edge_usd": str(self.locked_edge_usd),
            "locked_edge_per_share": (
                str(ONE - self.combined_vwap) if self.combined_vwap is not None else None
            ),
            "unprofitable_paired_shares": str(self.unprofitable_paired_shares),
            "unmatched_side": self.unmatched_side,
            "unmatched_shares": str(self.unmatched_shares),
            "unmatched_cost_usd": str(self.unmatched_cost_usd),
            "max_unmatched_usd": str(self.max_unmatched_usd_observed),
            "unmatched_duration_minutes": list(self.unmatched_durations_minutes),
            "unwind_pnl_usd": str(self.unwind_pnl_usd),
            "unwind_fill_count": self.unwind_fill_count,
            "maker_fill_count": sum(
                1
                for order in (*self.yes_engine.orders, *self.no_engine.orders)
                for fill in order.fills
                if order.maker_assumption
            ),
            "taker_fill_count": sum(
                1
                for order in (*self.yes_engine.orders, *self.no_engine.orders)
                for fill in order.fills
                if not order.maker_assumption
            ),
            "capital_minutes_to_lock_or_cutoff": str(capital_minutes),
            "yes_orders": [order.as_dict() for order in self.yes_engine.orders],
            "no_orders": [order.as_dict() for order in self.no_engine.orders],
            "rejection_reasons": list(self.rejection_reasons),
            "execution_enabled": False,
        }


class ComplementPairReplay:
    """Route token-native snapshots/trades into isolated pair portfolios."""

    def __init__(self, *, config: ComplementPairConfig, fill_model: FillModel) -> None:
        self.config = config
        self.fill_model = fill_model
        self.portfolios: dict[ComplementPairKey, ComplementPairPortfolio] = {}
        self.token_owner: dict[str, ComplementPairKey] = {}
        self.risk_managers: dict[tuple[str, str], StationDayRiskManager] = {}
        self.halted = False
        self.halt_reason: str | None = None
        self.ambiguous_trade_groups_skipped = 0
        self.quality_excluded_pair_count = 0

    @staticmethod
    def _cluster(yes: BookSnapshot, no: BookSnapshot) -> tuple[str, str]:
        station = yes.station_id or no.station_id or "unknown"
        if yes.station_id and no.station_id and yes.station_id != no.station_id:
            raise ShadowPortfolioInvariantError(
                "complement_pair_station_mismatch",
                details={"yes_station_id": yes.station_id, "no_station_id": no.station_id},
            )
        market_day = yes.market_day or no.market_day or yes.timestamp.date().isoformat()
        return station, market_day

    def _manager(self, cluster: tuple[str, str]) -> StationDayRiskManager:
        return self.risk_managers.setdefault(
            cluster,
            StationDayRiskManager(
                station_id=cluster[0],
                market_day=cluster[1],
                budget_usd=self.config.station_day_budget_usd,
            ),
        )

    def _cluster_engines(self, cluster: tuple[str, str]) -> tuple[ShadowOrderEngine, ...]:
        return tuple(
            engine
            for portfolio in self.portfolios.values()
            if (portfolio.station_id, portfolio.key.market_day) == cluster
            for engine in portfolio.engines
        )

    def _halt(self, code: str) -> None:
        self.halted = True
        self.halt_reason = code
        for portfolio in self.portfolios.values():
            portfolio._halt(code)

    def _portfolio(
        self, yes: BookSnapshot, no: BookSnapshot
    ) -> tuple[ComplementPairPortfolio, StationDayRiskManager, tuple[ShadowOrderEngine, ...]]:
        key = ComplementPairKey.from_snapshots(yes, no)
        cluster = self._cluster(yes, no)
        for token_id in (key.yes_token_id, key.no_token_id):
            owner = self.token_owner.get(token_id)
            if owner is not None and owner != key:
                raise ShadowPortfolioInvariantError(
                    "complement_pair_token_reused_by_other_market",
                    details={
                        "token_id": token_id,
                        "existing_pair": owner.as_dict(),
                        "incoming_pair": key.as_dict(),
                    },
                )
        if key not in self.portfolios:
            portfolio = ComplementPairPortfolio(
                key=key,
                yes=yes,
                no=no,
                station_id=cluster[0],
                config=self.config,
                fill_model=self.fill_model,
            )
            self.portfolios[key] = portfolio
            self.token_owner[key.yes_token_id] = key
            self.token_owner[key.no_token_id] = key
        manager = self._manager(cluster)
        return self.portfolios[key], manager, self._cluster_engines(cluster)

    def process_pair(self, yes: BookSnapshot, no: BookSnapshot) -> None:
        if self.halted:
            return
        if yes.quality_excluded or no.quality_excluded:
            self.quality_excluded_pair_count += 1
        try:
            portfolio, manager, engines = self._portfolio(yes, no)
            portfolio.process_snapshot(yes, no, risk_manager=manager, cluster_engines=engines)
            manager.assert_conservation(self._cluster_engines((portfolio.station_id, portfolio.key.market_day)))
        except ShadowPortfolioInvariantError as exc:
            self._halt(exc.code)

    def process_trades(self, trades: Sequence[TradeEvent]) -> None:
        grouped: dict[tuple[datetime, str], list[TradeEvent]] = defaultdict(list)
        for trade in trades:
            grouped[(trade.timestamp, trade.asset_id)].append(trade)
        for _, group in sorted(grouped.items(), key=lambda item: item[0]):
            if len(group) > 1 and any(trade.sequence is None for trade in group):
                self.ambiguous_trade_groups_skipped += 1
                continue
            for trade in sorted(group, key=lambda row: (row.sequence is None, row.sequence or 0)):
                owner = self.token_owner.get(trade.asset_id)
                if owner is None:
                    continue
                portfolio = self.portfolios[owner]
                portfolio.process_trade(trade)
                try:
                    self._manager((portfolio.station_id, portfolio.key.market_day)).assert_conservation(
                        self._cluster_engines((portfolio.station_id, portfolio.key.market_day))
                    )
                except ShadowPortfolioInvariantError as exc:
                    self._halt(exc.code)

    def _aggregate(self, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        submitted = [row for row in rows if int(row["submitted_pair_count"]) > 0]
        one_leg = [row for row in rows if bool(row["had_one_leg_fill"])]
        completed = [row for row in rows if _decimal(str(row["paired_shares"])) > ZERO]
        full = [row for row in rows if bool(row["fully_completed"])]
        maker_fills = sum(int(row["maker_fill_count"]) for row in rows)
        taker_fills = sum(int(row["taker_fill_count"]) for row in rows)
        locked_edge = sum((_decimal(str(row["locked_edge_usd"])) for row in rows), start=ZERO)
        locked_shares = sum((_decimal(str(row["paired_shares"])) for row in rows), start=ZERO)
        locked_cost = sum(
            (
                _decimal(str(row["paired_shares"])) * _decimal(str(row["combined_vwap"]))
                for row in rows
                if row.get("combined_vwap") is not None
            ),
            start=ZERO,
        )
        capital_minutes = sum(
            (_decimal(str(row["capital_minutes_to_lock_or_cutoff"])) for row in rows), start=ZERO
        )
        unmatched_minutes = [
            float(value)
            for row in rows
            for value in row.get("unmatched_duration_minutes", ())
        ]
        station_days = {(str(row["station_id"]), str(row["market_day"])) for row in rows}
        cluster_rows: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            cluster_rows[(str(row["station_id"]), str(row["market_day"]))].append(row)
        cluster_submitted = sum(
            any(int(row["submitted_pair_count"]) > 0 for row in group)
            for group in cluster_rows.values()
        )
        cluster_one_leg = sum(
            any(bool(row["had_one_leg_fill"]) for row in group) for group in cluster_rows.values()
        )
        cluster_completed = sum(
            any(_decimal(str(row["paired_shares"])) > ZERO for row in group)
            for group in cluster_rows.values()
        )
        cluster_full = sum(
            any(bool(row["fully_completed"]) for row in group) for group in cluster_rows.values()
        )
        return {
            "pair_portfolio_count": len(rows),
            "station_day_cluster_count": len(station_days),
            "pair_candidate_count": sum(int(row["candidate_count"]) for row in rows),
            "submitted_pair_count": len(submitted),
            "one_leg_fill_pair_count": len(one_leg),
            "completed_pair_count": len(completed),
            "fully_completed_pair_count": len(full),
            "completion_given_one_leg_pair_rate": _rate(len(completed), len(one_leg)),
            "full_completion_pair_rate": _rate(len(full), len(submitted)),
            "cluster_submitted_count": cluster_submitted,
            "cluster_one_leg_count": cluster_one_leg,
            "cluster_completed_count": cluster_completed,
            "cluster_fully_completed_count": cluster_full,
            "completion_given_one_leg_station_day_rate": _rate(
                cluster_completed, cluster_one_leg
            ),
            "full_completion_station_day_rate": _rate(cluster_full, cluster_submitted),
            "locked_shares": str(locked_shares),
            "combined_vwap": str(locked_cost / locked_shares) if locked_shares else None,
            "locked_edge_usd": str(locked_edge),
            "max_unmatched_usd": str(
                max((_decimal(str(row["max_unmatched_usd"])) for row in rows), default=ZERO)
            ),
            "unmatched_duration_minutes_p50": median(unmatched_minutes) if unmatched_minutes else None,
            "unmatched_duration_minutes_p90": _percentile(unmatched_minutes, 0.9),
            "unwind_pnl_usd": str(
                sum((_decimal(str(row["unwind_pnl_usd"])) for row in rows), start=ZERO)
            ),
            "maker_fill_count": maker_fills,
            "taker_fill_count": taker_fills,
            "maker_fill_share": _rate(maker_fills, maker_fills + taker_fills),
            "capital_minutes_to_lock_or_cutoff": str(capital_minutes),
            "diagnostic_locked_edge_per_capital_hour": (
                str(locked_edge / (capital_minutes / Decimal("60")))
                if capital_minutes > ZERO
                else None
            ),
            "statistical_unit": "station_id + market_day; n<30 is statistically unreliable",
        }

    def summary(self, cutoff: datetime | None = None) -> dict[str, Any]:
        rows = [portfolio.summary(cutoff) for portfolio in self.portfolios.values()]
        by_station: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_station[str(row["station_id"])].append(row)
        overall = self._aggregate(rows)
        return {
            **overall,
            "fill_model": str(self.fill_model),
            "strategy_family": "complement_pair_independent_shadow",
            "execution_enabled": False,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "ambiguous_trade_groups_skipped": self.ambiguous_trade_groups_skipped,
            "quality_excluded_pair_count": self.quality_excluded_pair_count,
            "station_summaries": {
                station: self._aggregate(station_rows)
                for station, station_rows in sorted(by_station.items())
            },
            "portfolio_summaries": rows,
            "station_day_budgets": [
                manager.as_dict(self._cluster_engines(cluster))
                for cluster, manager in sorted(self.risk_managers.items())
            ],
        }


def pair_snapshots_from_mapping(row: Mapping[str, Any]) -> tuple[BookSnapshot, BookSnapshot]:
    """Build simultaneous YES/NO token-native quotes from one paired archive row."""
    yes_row = row.get("yes")
    no_row = row.get("no")
    if not isinstance(yes_row, Mapping) or not isinstance(no_row, Mapping):
        raise ValueError("complement pair replay requires paired yes/no book rows")
    common = dict(row)
    common.pop("yes", None)
    common.pop("no", None)
    yes = BookSnapshot.from_mapping({**common, "no": yes_row})
    no = BookSnapshot.from_mapping({**common, "no": no_row})
    decision_time = max(yes.timestamp, no.timestamp)
    # The pair decision cannot predate either leg's local archive receipt.
    return replace(yes, timestamp=decision_time), replace(no, timestamp=decision_time)


def _run_one_model(
    snapshots: Sequence[tuple[BookSnapshot, BookSnapshot]],
    trades: Sequence[TradeEvent],
    *,
    config: ComplementPairConfig,
    fill_model: FillModel,
) -> dict[str, Any]:
    replay = ComplementPairReplay(config=config, fill_model=fill_model)
    ordered = sorted(snapshots, key=lambda item: item[0].timestamp)
    ordered_trades = sorted(
        trades,
        key=lambda row: (
            row.available_at or row.timestamp,
            row.timestamp,
            row.sequence is None,
            row.sequence or 0,
        ),
    )
    trade_index = 0
    snapshot_index = 0
    while snapshot_index < len(ordered):
        timestamp = ordered[snapshot_index][0].timestamp
        available: list[TradeEvent] = []
        while trade_index < len(ordered_trades):
            trade = ordered_trades[trade_index]
            if (trade.available_at or trade.timestamp) >= timestamp:
                break
            available.append(trade)
            trade_index += 1
        replay.process_trades(available)
        while snapshot_index < len(ordered) and ordered[snapshot_index][0].timestamp == timestamp:
            yes, no = ordered[snapshot_index]
            replay.process_pair(yes, no)
            snapshot_index += 1
    cutoff = max((yes.timestamp for yes, _ in ordered), default=None)
    return replay.summary(cutoff)


def replay_complement_pairs(
    pairs: Sequence[Mapping[str, Any]],
    *,
    trades: Sequence[TradeEvent] = (),
    config: ComplementPairConfig | None = None,
) -> dict[str, Any]:
    """Replay precommitted complement-pair maker rules without execution."""
    selected = config or default_complement_pair_config()
    selected.validate()
    snapshots: list[tuple[BookSnapshot, BookSnapshot]] = []
    malformed_pair_count = 0
    for row in pairs:
        try:
            snapshots.append(pair_snapshots_from_mapping(row))
        except (TypeError, ValueError, KeyError):
            malformed_pair_count += 1
    models = {
        str(fill_model): _run_one_model(snapshots, trades, config=selected, fill_model=fill_model)
        for fill_model in MAKER_MODELS
    }
    cutoff = max((yes.timestamp for yes, _ in snapshots), default=None)
    return {
        "strategy_version": selected.version,
        "strategy": selected.as_dict(),
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": cutoff.isoformat() if cutoff else None,
        "raw_pair_snapshot_count": len(pairs),
        "usable_pair_snapshot_count": len(snapshots),
        "malformed_pair_snapshot_count": malformed_pair_count,
        "trade_event_count": len(trades),
        "models": models,
        "execution_enabled": False,
        "limitations": [
            "planned YES+NO limits below one are not a locked pair",
            "only equal actual token-native fills with realised all-in cost below one are locked",
            "touch is optimistic; queue-aware and trade-through are the conservative evidence",
            "trade prices are queue-consumption events, never quote or depth proxies",
            "no wallet, credentials, User WebSocket, order, cancellation, or Relayer endpoint is used",
        ],
    }


def replay_complement_pairs_streaming(
    pairs: Iterable[Mapping[str, Any]],
    *,
    trades: Sequence[TradeEvent] = (),
    config: ComplementPairConfig | None = None,
) -> dict[str, Any]:
    """Replay a chronological pair stream without retaining every full book.

    This is deliberately separate from ``replay_complement_pairs`` so existing
    finite replay callers preserve their sorted-list behaviour.  The iterator
    is for large append-only archives: it must be chronological and only the
    latest token books and strategy state are retained.  Trade availability is
    checked strictly before each snapshot timestamp, exactly as in the finite
    replay.
    """
    selected = config or default_complement_pair_config()
    selected.validate()
    replays = {
        fill_model: ComplementPairReplay(config=selected, fill_model=fill_model)
        for fill_model in MAKER_MODELS
    }
    ordered_trades = sorted(
        trades,
        key=lambda row: (
            row.available_at or row.timestamp,
            row.timestamp,
            row.sequence is None,
            row.sequence or 0,
        ),
    )
    trade_index = 0
    raw_pair_snapshot_count = 0
    usable_pair_snapshot_count = 0
    malformed_pair_snapshot_count = 0
    prior_timestamp: datetime | None = None
    cutoff: datetime | None = None
    for row in pairs:
        raw_pair_snapshot_count += 1
        try:
            yes, no = pair_snapshots_from_mapping(row)
        except (TypeError, ValueError, KeyError):
            malformed_pair_snapshot_count += 1
            continue
        timestamp = yes.timestamp
        if prior_timestamp is not None and timestamp < prior_timestamp:
            raise ValueError("streaming complement pairs must be chronological")
        prior_timestamp = timestamp
        available: list[TradeEvent] = []
        while trade_index < len(ordered_trades):
            trade = ordered_trades[trade_index]
            if (trade.available_at or trade.timestamp) >= timestamp:
                break
            available.append(trade)
            trade_index += 1
        if available:
            for replay in replays.values():
                replay.process_trades(available)
        for replay in replays.values():
            replay.process_pair(yes, no)
        usable_pair_snapshot_count += 1
        cutoff = timestamp
    return {
        "strategy_version": selected.version,
        "strategy": selected.as_dict(),
        "generated_at": datetime.now(UTC).isoformat(),
        "data_cutoff": cutoff.isoformat() if cutoff else None,
        "raw_pair_snapshot_count": raw_pair_snapshot_count,
        "usable_pair_snapshot_count": usable_pair_snapshot_count,
        "malformed_pair_snapshot_count": malformed_pair_snapshot_count,
        "trade_event_count": len(trades),
        "models": {str(model): replay.summary(cutoff) for model, replay in replays.items()},
        "execution_enabled": False,
        "limitations": [
            "planned YES+NO limits below one are not a locked pair",
            "only equal actual token-native fills with realised all-in cost below one are locked",
            "touch is optimistic; queue-aware and trade-through are the conservative evidence",
            "trade prices are queue-consumption events, never quote or depth proxies",
            "no wallet, credentials, User WebSocket, order, cancellation, or Relayer endpoint is used",
            "streaming replay retains only current pair state; input snapshots are chronological",
        ],
    }


def replay_complement_pairs_streaming_grid(
    pairs: Iterable[Mapping[str, Any]],
    *,
    trades: Sequence[TradeEvent] = (),
    configs: Sequence[ComplementPairConfig],
) -> tuple[dict[str, Any], ...]:
    """Replay several predeclared configs while scanning a pair stream once.

    The strategy state remains separate for every config and fill model, but
    the append-only archive iterator and sorted trade tape are shared.  Callers
    may batch the config sequence when memory is constrained; this function
    never materialises the historical pair stream.
    """
    selected_configs = tuple(configs)
    if not selected_configs:
        return ()
    for selected in selected_configs:
        selected.validate()
    replays = tuple(
        {
            fill_model: ComplementPairReplay(config=selected, fill_model=fill_model)
            for fill_model in MAKER_MODELS
        }
        for selected in selected_configs
    )
    ordered_trades = sorted(
        trades,
        key=lambda row: (
            row.available_at or row.timestamp,
            row.timestamp,
            row.sequence is None,
            row.sequence or 0,
        ),
    )
    trade_index = 0
    raw_pair_snapshot_count = 0
    usable_pair_snapshot_count = 0
    malformed_pair_snapshot_count = 0
    prior_timestamp: datetime | None = None
    cutoff: datetime | None = None
    for row in pairs:
        raw_pair_snapshot_count += 1
        try:
            yes, no = pair_snapshots_from_mapping(row)
        except (TypeError, ValueError, KeyError):
            malformed_pair_snapshot_count += 1
            continue
        timestamp = yes.timestamp
        if prior_timestamp is not None and timestamp < prior_timestamp:
            raise ValueError("streaming complement pairs must be chronological")
        prior_timestamp = timestamp
        available: list[TradeEvent] = []
        while trade_index < len(ordered_trades):
            trade = ordered_trades[trade_index]
            if (trade.available_at or trade.timestamp) >= timestamp:
                break
            available.append(trade)
            trade_index += 1
        if available:
            for model_replays in replays:
                for replay in model_replays.values():
                    replay.process_trades(available)
        for model_replays in replays:
            for replay in model_replays.values():
                replay.process_pair(yes, no)
        usable_pair_snapshot_count += 1
        cutoff = timestamp
    generated_at = datetime.now(UTC).isoformat()
    output: list[dict[str, Any]] = []
    for selected, model_replays in zip(selected_configs, replays, strict=True):
        output.append(
            {
                "strategy_version": selected.version,
                "strategy": selected.as_dict(),
                "generated_at": generated_at,
                "data_cutoff": cutoff.isoformat() if cutoff else None,
                "raw_pair_snapshot_count": raw_pair_snapshot_count,
                "usable_pair_snapshot_count": usable_pair_snapshot_count,
                "malformed_pair_snapshot_count": malformed_pair_snapshot_count,
                "trade_event_count": len(trades),
                "models": {
                    str(model): replay.summary(cutoff)
                    for model, replay in model_replays.items()
                },
                "execution_enabled": False,
                "limitations": [
                    "planned YES+NO limits below one are not a locked pair",
                    "only equal actual token-native fills with realised all-in cost below one are locked",
                    "touch is optimistic; queue-aware and trade-through are the conservative evidence",
                    "trade prices are queue-consumption events, never quote or depth proxies",
                    "no wallet, credentials, User WebSocket, order, cancellation, or Relayer endpoint is used",
                    "streaming replay retains only current pair state; input snapshots are chronological",
                ],
            }
        )
    return tuple(output)


def render_complement_pair_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    """Render a compact, explicit-boundary Markdown report."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# YES+NO 互补配对 Maker 影子策略",
        "",
        f"生成时间（UTC）：{result.get('generated_at', 'N/A')}",
        f"数据截止（UTC）：{result.get('data_cutoff', 'N/A')}",
        f"固定分析 vintage 上限（UTC）：{result.get('analysis_cutoff', 'N/A')}",
        f"策略版本：`{result.get('strategy_version', 'N/A')}`；`execution_enabled=false`。",
        "",
        "> 这是只读历史盘口重放，不是订单记录。计划 YES+NO 限价之和小于 1 不是锁定收益；只有两腿真实、token-native 的成交份数配平且实际 VWAP 加费用低于 1 才记为 locked pair。成交价不是可成交 ask/bid。",
        "",
        "## 预先定义的配置",
        "",
        f"`{result.get('strategy')}`",
        "",
        "## 各成交模型",
        "",
        "| 模型 | 候选 | 提交 pair | 单腿 fill pair | 已配平 pair | 配平/单腿（station-day Wilson 95%） | locked edge | 未配平最大敞口 | maker/taker fills | 状态 |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---|",
    ]
    for name, model in (result.get("models") or {}).items():
        rate = model.get("completion_given_one_leg_station_day_rate") or {}
        rate_text = "N/A"
        if rate.get("rate") is not None:
            rate_text = (
                f"{float(rate['rate']):.1%} "
                f"({float(rate['wilson_low']):.1%}–{float(rate['wilson_high']):.1%})"
            )
            if rate.get("statistically_unreliable"):
                rate_text += "; n<30"
        lines.append(
            "| "
            + " | ".join(
                (
                    name,
                    str(model.get("pair_candidate_count", 0)),
                    str(model.get("submitted_pair_count", 0)),
                    str(model.get("one_leg_fill_pair_count", 0)),
                    str(model.get("completed_pair_count", 0)),
                    rate_text,
                    str(model.get("locked_edge_usd", "0")),
                    str(model.get("max_unmatched_usd", "0")),
                    f"{model.get('maker_fill_count', 0)}/{model.get('taker_fill_count', 0)}",
                    "HALTED" if model.get("halted") else "read-only shadow",
                )
            )
            + " |"
        )
    queue_model = (result.get("models") or {}).get(str(FillModel.QUEUE_AWARE))
    station_summaries = (
        queue_model.get("station_summaries", {})
        if isinstance(queue_model, Mapping)
        else {}
    )
    strategy = result.get("strategy") or {}
    primary_stations = tuple(strategy.get("primary_stations") or ("KLAX", "KLGA"))
    ordered_stations = [
        *[station for station in primary_stations if station in station_summaries],
        *sorted(station for station in station_summaries if station not in primary_stations),
    ]
    if ordered_stations:
        lines.extend(
            [
                "",
                "## 按站点对照（queue-aware；KLAX/KLGA 优先）",
                "",
                "统计聚类单位仍是 `station_id + market_day`，不是 pair portfolio；n<30 的 Wilson 区间标记为统计不可靠。",
                "",
                "| 站点 | pair portfolio | station-day | 提交 pair | 单腿 fill | 配平 pair | 配平/单腿（Wilson 95%） | locked edge | unwind PnL |",
                "|---|---:|---:|---:|---:|---:|---|---:|---:|",
            ]
        )
        for station in ordered_stations:
            row = station_summaries[station]
            rate = row.get("completion_given_one_leg_station_day_rate") or {}
            rate_text = "N/A"
            if rate.get("rate") is not None:
                rate_text = (
                    f"{float(rate['rate']):.1%} "
                    f"({float(rate['wilson_low']):.1%}–{float(rate['wilson_high']):.1%})"
                )
                if rate.get("statistically_unreliable"):
                    rate_text += "; n<30"
            lines.append(
                "| "
                + " | ".join(
                    (
                        station,
                        str(row.get("pair_portfolio_count", 0)),
                        str(row.get("station_day_cluster_count", 0)),
                        str(row.get("submitted_pair_count", 0)),
                        str(row.get("one_leg_fill_pair_count", 0)),
                        str(row.get("completed_pair_count", 0)),
                        rate_text,
                        str(row.get("locked_edge_usd", "0")),
                        str(row.get("unwind_pnl_usd", "0")),
                    )
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "- queue-aware / trade-through 才可作为保守诊断；touch 仅为乐观上界。",
            "- 未配平单腿达到时间或美元上限后，先取消未成交 maker 腿；仅当真实缺腿 ask 深度在费用后仍满足 pair cost cap 时才做影子 taker hedge，否则用同 token 的真实 bid 深度影子 unwind。",
            "- 每个 YES/NO token 都有独立库存、成本、订单和队列；station-day 只聚合 $200 风险和统计，不传递库存或 PnL。",
            "- 所有比例按 station-day 聚类并附 Wilson 95%；n<30 明确不可靠。locked edge / capital-hour 是诊断值，未当作已结算或可执行收益。",
            "",
            "## 限制",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in result.get("limitations") or ())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_complement_pair_result(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(result) + "\n", encoding="utf-8")


def json_dumps(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
