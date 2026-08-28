"""Four-state market regime classification for read-only shadow research.

The classifier consumes a receipt-aware :class:`InformationEvent` and true
token-native book metrics.  It has no PnL objective and exposes three fixed
threshold profiles for sensitivity reporting.  A profile is selected before a
replay starts; the replay never searches for the profile that produces the
best result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import fmean, median
from typing import Any

from poly_weather.information_clock import InformationEvent
from poly_weather.shadow_orders import BookSnapshot, TradeEvent

ZERO = Decimal("0")
ONE = Decimal("1")


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


class RegimeState(StrEnum):
    EVENT = "EVENT"
    DIGESTION = "DIGESTION"
    QUIET = "QUIET"
    PRE_RELEASE = "PRE_RELEASE"
    HALTED = "HALTED"


class ThresholdTier(StrEnum):
    STRICT = "strict"
    NEUTRAL = "neutral"
    LENIENT = "lenient"


@dataclass(frozen=True, slots=True)
class RegimeThresholds:
    """Predeclared stability profile; values are diagnostics, not fitted PnL."""

    tier: ThresholdTier = ThresholdTier.NEUTRAL
    stable_windows: int = 3
    minimum_observation_spacing: timedelta = timedelta(minutes=1)
    reaction_cooldown: timedelta = timedelta(minutes=1)
    max_abs_price_slope: Decimal = Decimal("0.002")
    max_cumulative_move: Decimal = Decimal("0.02")
    max_trade_intensity_multiple: Decimal = Decimal("1.50")
    max_spread: Decimal = Decimal("0.08")
    min_top_depth: Decimal = Decimal("5")
    max_abs_imbalance: Decimal = Decimal("0.40")
    max_churn_rate: Decimal = Decimal("0.60")
    max_cross_bucket_mass_error: Decimal = Decimal("0.10")
    require_cross_bucket_quality: bool = True
    pre_release_minutes: int = 5

    def __post_init__(self) -> None:
        object.__setattr__(self, "tier", ThresholdTier(self.tier))
        for field_name in (
            "max_abs_price_slope",
            "max_cumulative_move",
            "max_trade_intensity_multiple",
            "max_spread",
            "min_top_depth",
            "max_abs_imbalance",
            "max_churn_rate",
            "max_cross_bucket_mass_error",
        ):
            value = _decimal(getattr(self, field_name))
            if value is None:
                raise ValueError(f"{field_name} must be a decimal")
            object.__setattr__(self, field_name, value)
        if self.stable_windows < 1:
            raise ValueError("stable_windows must be positive")
        if self.reaction_cooldown < timedelta(0):
            raise ValueError("reaction_cooldown cannot be negative")
        if self.pre_release_minutes < 0:
            raise ValueError("pre_release_minutes cannot be negative")
        if self.max_spread <= ZERO or self.min_top_depth < ZERO:
            raise ValueError("invalid regime stability thresholds")

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": str(self.tier),
            "stable_windows": self.stable_windows,
            "minimum_observation_spacing_seconds": self.minimum_observation_spacing.total_seconds(),
            "reaction_cooldown_seconds": self.reaction_cooldown.total_seconds(),
            "max_abs_price_slope": str(self.max_abs_price_slope),
            "max_cumulative_move": str(self.max_cumulative_move),
            "max_trade_intensity_multiple": str(self.max_trade_intensity_multiple),
            "max_spread": str(self.max_spread),
            "min_top_depth": str(self.min_top_depth),
            "max_abs_imbalance": str(self.max_abs_imbalance),
            "max_churn_rate": str(self.max_churn_rate),
            "max_cross_bucket_mass_error": str(self.max_cross_bucket_mass_error),
            "require_cross_bucket_quality": self.require_cross_bucket_quality,
            "pre_release_minutes": self.pre_release_minutes,
        }


def predefined_regime_thresholds() -> dict[ThresholdTier, RegimeThresholds]:
    """Return the fixed strict/neutral/lenient sensitivity profiles."""
    return {
        ThresholdTier.STRICT: RegimeThresholds(
            tier=ThresholdTier.STRICT,
            stable_windows=4,
            reaction_cooldown=timedelta(minutes=2),
            max_abs_price_slope=Decimal("0.001"),
            max_cumulative_move=Decimal("0.01"),
            max_trade_intensity_multiple=Decimal("1.25"),
            max_spread=Decimal("0.05"),
            min_top_depth=Decimal("10"),
            max_abs_imbalance=Decimal("0.25"),
            max_churn_rate=Decimal("0.40"),
            max_cross_bucket_mass_error=Decimal("0.05"),
            pre_release_minutes=10,
        ),
        ThresholdTier.NEUTRAL: RegimeThresholds(),
        ThresholdTier.LENIENT: RegimeThresholds(
            tier=ThresholdTier.LENIENT,
            stable_windows=2,
            reaction_cooldown=timedelta(0),
            max_abs_price_slope=Decimal("0.004"),
            max_cumulative_move=Decimal("0.04"),
            max_trade_intensity_multiple=Decimal("2.00"),
            max_spread=Decimal("0.12"),
            min_top_depth=Decimal("2"),
            max_abs_imbalance=Decimal("0.60"),
            max_churn_rate=Decimal("0.80"),
            max_cross_bucket_mass_error=Decimal("0.20"),
            pre_release_minutes=3,
        ),
    }


@dataclass(frozen=True, slots=True)
class MarketMetrics:
    """One true-book observation used by the regime state machine."""

    timestamp: datetime
    station_id: str
    market_day: str
    token_id: str
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    mid: Decimal | None = None
    spread: Decimal | None = None
    top_bid_depth: Decimal | None = None
    top_ask_depth: Decimal | None = None
    imbalance: Decimal | None = None
    price_slope: Decimal | None = None
    cumulative_move: Decimal | None = None
    trade_intensity_multiple: Decimal | None = None
    churn_rate: Decimal | None = None
    cross_bucket_mass_error: Decimal | None = None
    trade_count: int = 0
    trade_volume: Decimal = ZERO
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        object.__setattr__(self, "station_id", self.station_id.upper())
        for field_name in (
            "best_bid",
            "best_ask",
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
            "trade_volume",
        ):
            object.__setattr__(self, field_name, _decimal(getattr(self, field_name)))

    @property
    def scope(self) -> tuple[str, str]:
        return self.station_id, self.market_day

    @property
    def complete_for_quiet(self) -> bool:
        return self.mid is not None and self.spread is not None

    def as_dict(self) -> dict[str, Any]:
        def value(item: Any) -> Any:
            return str(item) if isinstance(item, Decimal) else item

        return {
            "timestamp": self.timestamp.isoformat(),
            "station_id": self.station_id,
            "market_day": self.market_day,
            "token_id": self.token_id,
            **{
                key: value(getattr(self, key))
                for key in (
                    "best_bid",
                    "best_ask",
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
                    "trade_volume",
                )
            },
            "trade_count": self.trade_count,
            "metadata": dict(self.metadata),
        }


def _level_at(levels: Sequence[tuple[Decimal, Decimal]], price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    return next((size for level_price, size in levels if level_price == price), None)


def metrics_from_snapshot(
    snapshot: BookSnapshot,
    *,
    previous: MarketMetrics | None = None,
    trades: Sequence[TradeEvent] = (),
    baseline_trade_intensity: Decimal | float | str | None = None,
) -> MarketMetrics:
    """Compute diagnostics from a token-native snapshot and real trade tape."""
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    mid = (bid + ask) / Decimal("2") if bid is not None and ask is not None else None
    spread = ask - bid if bid is not None and ask is not None else None
    bid_depth = _level_at(snapshot.bids, bid)
    ask_depth = _level_at(snapshot.asks, ask)
    total_depth = (bid_depth or ZERO) + (ask_depth or ZERO)
    imbalance = (
        ((bid_depth or ZERO) - (ask_depth or ZERO)) / total_depth
        if total_depth > ZERO
        else None
    )
    slope = None
    cumulative = None
    if previous is not None and mid is not None and previous.mid is not None:
        elapsed = Decimal(str((snapshot.timestamp - previous.timestamp).total_seconds() / 60))
        if elapsed > ZERO:
            slope = (mid - previous.mid) / elapsed
        anchor = previous.metadata.get("anchor_mid", previous.mid)
        anchor_value = _decimal(anchor)
        cumulative = abs(mid - anchor_value) if anchor_value is not None else None
    metadata = snapshot.metadata if isinstance(snapshot.metadata, Mapping) else {}
    baseline = _decimal(
        baseline_trade_intensity
        if baseline_trade_intensity is not None
        else metadata.get("trade_intensity_baseline")
    )
    intensity = (
        Decimal(str(len(trades))) / baseline
        if baseline is not None and baseline > ZERO
        else _decimal(metadata.get("trade_intensity_multiple"))
    )
    churn = _decimal(metadata.get("churn_rate"))
    cross_bucket = _decimal(
        metadata.get("cross_bucket_mass_error")
        if metadata.get("cross_bucket_mass_error") is not None
        else metadata.get("probability_mass_error")
    )
    values = dict(metadata)
    if mid is not None and "anchor_mid" not in values:
        values["anchor_mid"] = str(previous.mid if previous and previous.mid is not None else mid)
    return MarketMetrics(
        timestamp=snapshot.timestamp,
        station_id=snapshot.station_id or "unknown",
        market_day=snapshot.market_day or snapshot.timestamp.date().isoformat(),
        token_id=snapshot.token_id,
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        spread=spread,
        top_bid_depth=bid_depth,
        top_ask_depth=ask_depth,
        imbalance=imbalance,
        price_slope=slope if slope is not None else _decimal(metadata.get("price_slope")),
        cumulative_move=cumulative
        if cumulative is not None
        else _decimal(metadata.get("cumulative_move")),
        trade_intensity_multiple=intensity,
        churn_rate=churn,
        cross_bucket_mass_error=cross_bucket,
        trade_count=len(trades),
        trade_volume=sum((trade.size for trade in trades), start=ZERO),
        metadata=values,
    )


@dataclass(frozen=True, slots=True)
class RegimeTransition:
    timestamp: datetime
    previous_state: RegimeState
    state: RegimeState
    reason: str
    scope: tuple[str, str]
    event_id: str | None = None
    reaction_duration_seconds: float | None = None
    allow_new_orders: bool = False
    reduce_only: bool = False
    cancel_required: bool = False
    stable_window_count: int = 0
    violations: tuple[str, ...] = ()
    next_release_at: datetime | None = None
    schedule_verified: bool = False
    metrics: MarketMetrics | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "previous_state": str(self.previous_state),
            "state": str(self.state),
            "reason": self.reason,
            "scope": {"station_id": self.scope[0], "market_day": self.scope[1]},
            "event_id": self.event_id,
            "reaction_duration_seconds": self.reaction_duration_seconds,
            "allow_new_orders": self.allow_new_orders,
            "reduce_only": self.reduce_only,
            "cancel_required": self.cancel_required,
            "stable_window_count": self.stable_window_count,
            "violations": list(self.violations),
            "next_release_at": self.next_release_at.isoformat()
            if self.next_release_at
            else None,
            "schedule_verified": self.schedule_verified,
            "metrics": self.metrics.as_dict() if self.metrics else None,
        }


@dataclass(frozen=True, slots=True)
class PredictableRelease:
    release_at: datetime
    source: str
    interval_seconds: float
    sample_count: int
    verified: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_at": self.release_at.isoformat(),
            "source": self.source,
            "interval_seconds": self.interval_seconds,
            "sample_count": self.sample_count,
            "verified": self.verified,
        }


def derive_predictable_release(
    events: Iterable[InformationEvent],
    *,
    now: datetime,
    scope: tuple[str, str] | None = None,
    kinds: Sequence[str] = (
        "metar",
        "speci",
        "wrh_observation",
        "nws_observation",
        "taf",
        "model_run",
    ),
    minimum_samples: int = 3,
) -> PredictableRelease | None:
    """Estimate the next release only from already available prior events.

    The function intentionally returns ``None`` until a source has enough
    observed intervals.  It never consumes the next future event to decide a
    current quote; callers may compare the prediction to future observations
    only in an after-the-fact audit.
    """
    point = _utc(now)
    grouped: dict[tuple[str, str], set[datetime]] = {}
    for event in events:
        if (
            event.phase_eligible
            and event.kind in kinds
            and (scope is None or event.scope == scope)
            and event.available_at is not None
            and event.available_at < point
        ):
            grouped.setdefault((event.source, event.kind), set()).add(event.available_at)
    candidates: list[
        tuple[float, float, float, tuple[str, str], list[datetime], bool]
    ] = []
    for source_kind, points in grouped.items():
        eligible = sorted(points)
        if len(eligible) < minimum_samples:
            continue
        intervals = [
            (later - earlier).total_seconds()
            for earlier, later in zip(eligible, eligible[1:], strict=False)
            if later > earlier
        ]
        if len(intervals) < minimum_samples - 1:
            continue
        typical = median(intervals)
        dispersion = max(intervals) - min(intervals)
        verified = typical > 0 and dispersion <= max(typical, 1.0) * 0.50
        # Prefer a verified source, then the source with more observations,
        # then the smallest relative dispersion.  This is a fixed schedule
        # rule, never a PnL-selected parameter.
        relative_dispersion = dispersion / typical if typical > 0 else float("inf")
        candidates.append(
            (
                0.0 if verified else 1.0,
                -float(len(eligible)),
                relative_dispersion,
                source_kind,
                eligible,
                verified,
            )
        )
    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    _verified_rank, _sample_rank, _dispersion_rank, source_kind, eligible, verified = candidates[0]
    intervals = [
        (later - earlier).total_seconds()
        for earlier, later in zip(eligible, eligible[1:], strict=False)
        if later > earlier
    ]
    typical = median(intervals)
    return PredictableRelease(
        release_at=eligible[-1] + timedelta(seconds=typical),
        source=f"{source_kind[0]}:{source_kind[1]}",
        interval_seconds=typical,
        sample_count=len(eligible),
        verified=verified,
    )


class MarketRegimeStateMachine:
    """One station/market-day four-state machine."""

    def __init__(
        self,
        *,
        station_id: str,
        market_day: str,
        thresholds: RegimeThresholds | None = None,
        retain_transitions: bool = True,
        retain_event_keys: bool = True,
    ) -> None:
        self.scope = (station_id.upper(), market_day)
        self.thresholds = thresholds or RegimeThresholds()
        self.state = RegimeState.DIGESTION
        self.halted = False
        self.halt_reason: str | None = None
        self.last_event: InformationEvent | None = None
        self.last_metric: MarketMetrics | None = None
        self.last_transition: RegimeTransition | None = None
        self.transitions: list[RegimeTransition] = []
        self.retain_transitions = retain_transitions
        self.retain_event_keys = retain_event_keys
        self._transition_count = 0
        self.reaction_durations_seconds: list[float] = []
        self.invalid_information_events: list[dict[str, Any]] = []
        self._seen_event_keys: set[tuple[str, str, str]] = set()
        self._stable_windows = 0

    @property
    def allow_new_orders(self) -> bool:
        return self.state is RegimeState.QUIET and not self.halted

    @property
    def reduce_only(self) -> bool:
        return self.state in {
            RegimeState.EVENT,
            RegimeState.DIGESTION,
            RegimeState.PRE_RELEASE,
            RegimeState.HALTED,
        }

    def halt(self, reason: str, *, timestamp: datetime | None = None, details: Mapping[str, Any] | None = None) -> RegimeTransition:
        at = _utc(timestamp or datetime.now(UTC))
        previous = self.state
        self.halted = True
        self.halt_reason = reason
        self.state = RegimeState.HALTED
        transition = RegimeTransition(
            timestamp=at,
            previous_state=previous,
            state=RegimeState.HALTED,
            reason=reason,
            scope=self.scope,
            allow_new_orders=False,
            reduce_only=True,
            cancel_required=True,
            violations=tuple(str(key) for key in (details or {}).keys()),
        )
        self.last_transition = transition
        self._transition_count += 1
        if self.retain_transitions:
            self.transitions.append(transition)
        return transition

    def _stable_violations(self, metrics: MarketMetrics) -> tuple[str, ...]:
        threshold = self.thresholds
        missing: list[str] = []
        violations: list[str] = []
        required = {
            "mid": metrics.mid,
            "spread": metrics.spread,
            "top_bid_depth": metrics.top_bid_depth,
            "top_ask_depth": metrics.top_ask_depth,
            "imbalance": metrics.imbalance,
            "price_slope": metrics.price_slope,
            "cumulative_move": metrics.cumulative_move,
            "trade_intensity_multiple": metrics.trade_intensity_multiple,
            "churn_rate": metrics.churn_rate,
        }
        if threshold.require_cross_bucket_quality:
            required["cross_bucket_mass_error"] = metrics.cross_bucket_mass_error
        for name, value in required.items():
            if value is None:
                missing.append(f"missing_{name}")
        if missing:
            return tuple(missing)
        if abs(metrics.price_slope or ZERO) > threshold.max_abs_price_slope:
            violations.append("price_slope_unstable")
        if (metrics.cumulative_move or ZERO) > threshold.max_cumulative_move:
            violations.append("cumulative_move_unstable")
        if (metrics.trade_intensity_multiple or ZERO) > threshold.max_trade_intensity_multiple:
            violations.append("trade_intensity_elevated")
        if (metrics.spread or ZERO) > threshold.max_spread:
            violations.append("spread_wide")
        if (metrics.top_bid_depth or ZERO) < threshold.min_top_depth:
            violations.append("bid_depth_thin")
        if (metrics.top_ask_depth or ZERO) < threshold.min_top_depth:
            violations.append("ask_depth_thin")
        if abs(metrics.imbalance or ZERO) > threshold.max_abs_imbalance:
            violations.append("imbalance_extreme")
        if (metrics.churn_rate or ZERO) > threshold.max_churn_rate:
            violations.append("book_churn_elevated")
        if (metrics.cross_bucket_mass_error or ZERO) > threshold.max_cross_bucket_mass_error:
            violations.append("cross_bucket_probability_inconsistent")
        return tuple((*missing, *violations))

    def _record(
        self,
        *,
        metrics: MarketMetrics,
        previous: RegimeState,
        reason: str,
        event_id: str | None = None,
        cancel_required: bool = False,
        violations: Sequence[str] = (),
        next_release_at: datetime | None = None,
        schedule_verified: bool = False,
    ) -> RegimeTransition:
        reaction = None
        if (
            self.state is RegimeState.QUIET
            and previous is not RegimeState.QUIET
            and self.last_event is not None
        ):
            if self.last_event.available_at is not None:
                reaction = max(
                    0.0,
                    (metrics.timestamp - self.last_event.available_at).total_seconds(),
                )
                self.reaction_durations_seconds.append(reaction)
        transition = RegimeTransition(
            timestamp=metrics.timestamp,
            previous_state=previous,
            state=self.state,
            reason=reason,
            scope=self.scope,
            event_id=event_id,
            reaction_duration_seconds=reaction,
            allow_new_orders=self.allow_new_orders,
            reduce_only=self.reduce_only,
            cancel_required=cancel_required,
            stable_window_count=self._stable_windows,
            violations=tuple(violations),
            next_release_at=next_release_at,
            schedule_verified=schedule_verified,
            metrics=metrics,
        )
        self.last_transition = transition
        self._transition_count += 1
        if self.retain_transitions:
            self.transitions.append(transition)
        return transition

    def ingest_event(
        self,
        event: InformationEvent,
        *,
        decision_at: datetime | None = None,
    ) -> RegimeTransition | None:
        accepted = self._accept_event(event, decision_at=decision_at)
        if accepted is None:
            return None
        if self.last_metric is None:
            return None
        previous = self.state
        return self._record(
            metrics=self.last_metric,
            previous=previous,
            reason="new_external_information",
            event_id=accepted.event_id,
            cancel_required=True,
        )

    def _accept_event(
        self,
        event: InformationEvent,
        *,
        decision_at: datetime | None = None,
    ) -> InformationEvent | None:
        if self.halted:
            self.invalid_information_events.append(
                {"event_id": event.event_id, "reason": "regime_already_halted"}
            )
            return None
        if event.scope != self.scope:
            self.invalid_information_events.append(
                {"event_id": event.event_id, "reason": "information_scope_mismatch"}
            )
            return None
        if not event.phase_eligible or (
            decision_at is not None and not event.available_by(decision_at)
        ):
            self.invalid_information_events.append(
                {"event_id": event.event_id, "reason": "information_not_strictly_available"}
            )
            return None
        key = (event.source, event.kind, event.payload_hash)
        if self.retain_event_keys and key in self._seen_event_keys:
            return None
        if self.retain_event_keys:
            self._seen_event_keys.add(key)
        self.last_event = event
        self._stable_windows = 0
        self.state = RegimeState.EVENT
        return event

    def observe(
        self,
        metrics: MarketMetrics,
        *,
        information_event: InformationEvent | None = None,
        next_release_at: datetime | None = None,
        schedule_verified: bool = False,
        health_ok: bool = True,
        health_reason: str | None = None,
    ) -> RegimeTransition:
        """Advance one state using only information available at this timestamp."""
        if metrics.scope != self.scope:
            return self.halt(
                "market_metrics_scope_mismatch",
                timestamp=metrics.timestamp,
                details={"actual_scope": metrics.scope},
            )
        if self.halted:
            previous = self.state
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="already_halted",
                cancel_required=True,
            )
        if not health_ok:
            return self.halt(
                health_reason or "market_or_weather_health_gate",
                timestamp=metrics.timestamp,
            )
        if self.last_metric is not None and metrics.timestamp < self.last_metric.timestamp:
            return self.halt(
                "regime_metrics_out_of_order",
                timestamp=metrics.timestamp,
            )
        accepted_event = None
        if information_event is not None:
            accepted_event = self._accept_event(
                information_event,
                decision_at=metrics.timestamp,
            )
            if accepted_event is not None:
                previous = self.state
                self.last_metric = metrics
                return self._record(
                    metrics=metrics,
                    previous=previous,
                    reason="new_external_information",
                    event_id=accepted_event.event_id,
                    cancel_required=True,
                )
        if self.last_metric is not None:
            spacing = metrics.timestamp - self.last_metric.timestamp
            if spacing < self.thresholds.minimum_observation_spacing:
                self.last_metric = metrics
                previous = self.state
                return self._record(
                    metrics=metrics,
                    previous=previous,
                    reason="duplicate_or_too_frequent_market_observation",
                )
        self.last_metric = metrics
        previous = self.state
        if self.last_event is None:
            self.state = RegimeState.DIGESTION
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="awaiting_first_receipt_eligible_information_event",
                violations=("no_information_anchor",),
            )
        if self.last_event.available_at is None or metrics.timestamp < self.last_event.available_at:
            return self.halt(
                "information_event_after_market_decision",
                timestamp=metrics.timestamp,
            )
        if next_release_at is not None:
            release = _utc(next_release_at)
            if schedule_verified and release <= metrics.timestamp:
                next_release_at = None
                schedule_verified = False
            elif schedule_verified and release - metrics.timestamp <= timedelta(
                minutes=self.thresholds.pre_release_minutes
            ):
                self.state = RegimeState.PRE_RELEASE
                self._stable_windows = 0
                return self._record(
                    metrics=metrics,
                    previous=previous,
                    reason="predictable_release_approaching",
                    cancel_required=True,
                    next_release_at=release,
                    schedule_verified=True,
                )
        violations = self._stable_violations(metrics)
        stable = not violations
        if stable:
            self._stable_windows += 1
        else:
            self._stable_windows = 0
        since_event = metrics.timestamp - self.last_event.available_at
        if self.state is RegimeState.EVENT:
            if stable and since_event >= self.thresholds.reaction_cooldown:
                if self._stable_windows >= self.thresholds.stable_windows:
                    self.state = RegimeState.QUIET
                    return self._record(
                        metrics=metrics,
                        previous=previous,
                        reason="reaction_end_stability_confirmed",
                        violations=violations,
                    )
                self.state = RegimeState.DIGESTION
            elif since_event > timedelta(0):
                self.state = RegimeState.DIGESTION
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="event_reaction_in_progress" if not stable else "waiting_for_stable_windows",
                cancel_required=True,
                violations=violations,
            )
        if self.state is RegimeState.PRE_RELEASE:
            self.state = RegimeState.DIGESTION
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="pre_release_window_elapsed_without_new_receipt",
                cancel_required=True,
                violations=violations,
            )
        if self.state is RegimeState.QUIET and not stable:
            self.state = RegimeState.DIGESTION
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="stability_lost",
                cancel_required=True,
                violations=violations,
            )
        if self.state is RegimeState.QUIET and stable:
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="quiet_window_stable",
                violations=violations,
            )
        if self.state is RegimeState.DIGESTION and stable and self._stable_windows >= self.thresholds.stable_windows:
            self.state = RegimeState.QUIET
            return self._record(
                metrics=metrics,
                previous=previous,
                reason="reaction_end_stability_confirmed",
                violations=violations,
            )
        self.state = RegimeState.DIGESTION
        return self._record(
            metrics=metrics,
            previous=previous,
            reason="market_still_digesting" if not stable else "waiting_for_stable_windows",
            cancel_required=True,
            violations=violations,
        )

    def summary(self, *, include_transitions: bool = True) -> dict[str, Any]:
        durations = self.reaction_durations_seconds
        return {
            "scope": {"station_id": self.scope[0], "market_day": self.scope[1]},
            "state": str(self.state),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "thresholds": self.thresholds.as_dict(),
            "transition_count": self._transition_count,
            "trace_retained": self.retain_transitions and include_transitions,
            "reaction_duration_count": len(durations),
            "reaction_duration_minutes": {
                "p50": median(durations) / 60 if durations else None,
                "p90": sorted(durations)[int((len(durations) - 1) * 0.9)] / 60
                if durations
                else None,
            },
            "invalid_information_events": list(self.invalid_information_events),
            "last_transition": (
                self.last_transition.as_dict() if self.last_transition else None
            ),
            "transitions": [transition.as_dict() for transition in self.transitions]
            if self.retain_transitions and include_transitions
            else [],
        }


def reaction_duration_summary(state_machines: Iterable[MarketRegimeStateMachine]) -> dict[str, Any]:
    durations = [
        duration
        for machine in state_machines
        for duration in machine.reaction_durations_seconds
    ]
    return {
        "sample_count": len(durations),
        "p50_minutes": median(durations) / 60 if durations else None,
        "p90_minutes": sorted(durations)[int((len(durations) - 1) * 0.9)] / 60
        if durations
        else None,
        "mean_minutes": fmean(durations) / 60 if durations else None,
        "statistically_unreliable": len(durations) < 30,
    }


__all__ = [
    "MarketMetrics",
    "MarketRegimeStateMachine",
    "PredictableRelease",
    "RegimeState",
    "RegimeThresholds",
    "RegimeTransition",
    "ThresholdTier",
    "derive_predictable_release",
    "metrics_from_snapshot",
    "predefined_regime_thresholds",
    "reaction_duration_summary",
]
