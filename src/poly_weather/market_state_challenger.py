"""Receipt-safe market-state diagnostics for weather/market-lag candidates.

The challenger classifies token-native book paths.  It never submits or
simulates an order, never substitutes a trade/midpoint/complement for a quote,
and never treats the confirmation window as a future outcome window.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from typing import Any

from poly_weather.information_clock import ImpactClass, InformationEvent
from poly_weather.market_microstructure import L2ChurnIndex, TradeIntensityTracker
from poly_weather.market_regime import MetricKnowledge, metrics_from_snapshot
from poly_weather.no_forward import wilson_interval
from poly_weather.shadow_orders import BookSnapshot, TradeEvent

ZERO = Decimal("0")
EXECUTION_ENABLED = False
OUTCOME_REQUIRED_METRICS = (
    "mid",
    "spread",
    "top_bid_depth",
    "top_ask_depth",
    "imbalance",
)


class BreakoutState(StrEnum):
    SURVIVING_BREAKOUT = "SURVIVING_BREAKOUT"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    UNCONFIRMED = "UNCONFIRMED"
    UNKNOWN = "UNKNOWN"


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"decimal value must be finite: {value!r}")
    return parsed


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MarketStateChallengerConfig:
    """Frozen v1 decision and outcome windows.

    Decimal-like inputs are accepted so the JSON config can preserve exact
    tick/fraction values without passing through binary floating point.
    """

    version: str
    trailing_window_minutes: int
    confirmation_window_minutes: int
    breakout_ticks: int
    survival_fraction: Decimal | str
    minimum_trailing_observations: int
    minimum_confirmation_observations: int
    maximum_snapshot_gap_minutes: int | None
    outcome_horizons_minutes: tuple[int, ...]
    bid_target_rises: tuple[Decimal | str, ...]
    required_metric_names: tuple[str, ...]
    forward_cutoff: datetime | str
    require_rule_provenance: bool = True
    diagnostic_metric_names: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "survival_fraction", _decimal(self.survival_fraction))
        object.__setattr__(
            self,
            "bid_target_rises",
            tuple(_decimal(value) for value in self.bid_target_rises),
        )
        object.__setattr__(self, "forward_cutoff", _utc(self.forward_cutoff))
        object.__setattr__(
            self,
            "outcome_horizons_minutes",
            tuple(int(value) for value in self.outcome_horizons_minutes),
        )
        object.__setattr__(
            self,
            "required_metric_names",
            tuple(str(value) for value in self.required_metric_names),
        )
        object.__setattr__(
            self,
            "diagnostic_metric_names",
            tuple(str(value) for value in self.diagnostic_metric_names),
        )
        if not self.version:
            raise ValueError("version is required")
        if self.trailing_window_minutes <= 0 or self.confirmation_window_minutes <= 0:
            raise ValueError("decision windows must be positive")
        if self.breakout_ticks <= 0:
            raise ValueError("breakout_ticks must be positive")
        if not ZERO < self.survival_fraction <= Decimal("1"):
            raise ValueError("survival_fraction must be in (0, 1]")
        if self.minimum_trailing_observations <= 0:
            raise ValueError("minimum_trailing_observations must be positive")
        if self.minimum_confirmation_observations <= 0:
            raise ValueError("minimum_confirmation_observations must be positive")
        if self.maximum_snapshot_gap_minutes is not None and self.maximum_snapshot_gap_minutes <= 0:
            raise ValueError("maximum_snapshot_gap_minutes must be positive when set")
        if not self.outcome_horizons_minutes or any(
            value <= 0 for value in self.outcome_horizons_minutes
        ):
            raise ValueError("outcome horizons must be positive")
        if tuple(sorted(set(self.outcome_horizons_minutes))) != self.outcome_horizons_minutes:
            raise ValueError("outcome horizons must be sorted and unique")
        if not self.bid_target_rises or any(value <= ZERO for value in self.bid_target_rises):
            raise ValueError("bid target rises must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MarketStateChallengerConfig:
        return cls(
            version=str(value["version"]),
            schema_version=int(value.get("schema_version", 1)),
            trailing_window_minutes=int(value["trailing_window_minutes"]),
            confirmation_window_minutes=int(value["confirmation_window_minutes"]),
            breakout_ticks=int(value["breakout_ticks"]),
            survival_fraction=str(value["survival_fraction"]),
            minimum_trailing_observations=int(value["minimum_trailing_observations"]),
            minimum_confirmation_observations=int(
                value["minimum_confirmation_observations"]
            ),
            maximum_snapshot_gap_minutes=(
                int(value["maximum_snapshot_gap_minutes"])
                if value.get("maximum_snapshot_gap_minutes") is not None
                else None
            ),
            outcome_horizons_minutes=tuple(
                int(item) for item in value["outcome_horizons_minutes"]
            ),
            bid_target_rises=tuple(str(item) for item in value["bid_target_rises"]),
            required_metric_names=tuple(
                str(item) for item in value["required_metric_names"]
            ),
            diagnostic_metric_names=tuple(
                str(item) for item in value.get("diagnostic_metric_names", ())
            ),
            require_rule_provenance=bool(value.get("require_rule_provenance", True)),
            forward_cutoff=str(value["forward_cutoff"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "schema_version": self.schema_version,
            "trailing_window_minutes": self.trailing_window_minutes,
            "confirmation_window_minutes": self.confirmation_window_minutes,
            "breakout_ticks": self.breakout_ticks,
            "survival_fraction": str(self.survival_fraction),
            "minimum_trailing_observations": self.minimum_trailing_observations,
            "minimum_confirmation_observations": self.minimum_confirmation_observations,
            "maximum_snapshot_gap_minutes": self.maximum_snapshot_gap_minutes,
            "outcome_horizons_minutes": list(self.outcome_horizons_minutes),
            "bid_target_rises": [str(value) for value in self.bid_target_rises],
            "required_metric_names": list(self.required_metric_names),
            "diagnostic_metric_names": list(self.diagnostic_metric_names),
            "require_rule_provenance": self.require_rule_provenance,
            "forward_cutoff": self.forward_cutoff.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PricePathOutcome:
    horizon_minutes: int
    status: str
    observation_count: int = 0
    terminal_at: datetime | None = None
    best_bid_change: Decimal | None = None
    best_ask_change: Decimal | None = None
    maximum_favorable_bid_change: Decimal | None = None
    maximum_adverse_bid_change: Decimal | None = None
    maximum_bid_jump: Decimal | None = None
    spread_change: Decimal | None = None
    bid_depth_usd_change: Decimal | None = None
    target_hits: Mapping[Decimal, bool] = field(default_factory=dict)
    new_information_before_endpoint: bool = False
    new_information_event_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "horizon_minutes": self.horizon_minutes,
                "status": self.status,
                "observation_count": self.observation_count,
                "terminal_at": self.terminal_at,
                "best_bid_change": self.best_bid_change,
                "best_ask_change": self.best_ask_change,
                "maximum_favorable_bid_change": self.maximum_favorable_bid_change,
                "maximum_adverse_bid_change": self.maximum_adverse_bid_change,
                "maximum_bid_jump": self.maximum_bid_jump,
                "spread_change": self.spread_change,
                "bid_depth_usd_change": self.bid_depth_usd_change,
                "new_information_before_endpoint": self.new_information_before_endpoint,
                "new_information_event_count": self.new_information_event_count,
                "target_hits": {
                    str(target): hit for target, hit in self.target_hits.items()
                },
            }
        )


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    event_id: str
    market_id: str
    token_id: str
    station_id: str
    market_day: str
    episode_id: str
    candidate_at: datetime
    decision_at: datetime
    state: BreakoutState
    state_reason: str
    trailing_high_bid: Decimal | None
    breakout_level: Decimal | None
    confirmation_observation_count: int
    breakout_observation_count: int
    unknown_reasons: tuple[str, ...]
    temporal_coverage: bool
    microstructure_known: bool
    diagnostic_metric_status: Mapping[str, str]
    outcomes: Mapping[int, PricePathOutcome]

    @property
    def station_day(self) -> tuple[str, str]:
        return self.station_id, self.market_day

    def as_dict(self) -> dict[str, Any]:
        return _json_value(
            {
                "event_id": self.event_id,
                "market_id": self.market_id,
                "token_id": self.token_id,
                "station_id": self.station_id,
                "market_day": self.market_day,
                "episode_id": self.episode_id,
                "candidate_at": self.candidate_at,
                "decision_at": self.decision_at,
                "state": self.state,
                "state_reason": self.state_reason,
                "trailing_high_bid": self.trailing_high_bid,
                "breakout_level": self.breakout_level,
                "confirmation_observation_count": self.confirmation_observation_count,
                "breakout_observation_count": self.breakout_observation_count,
                "unknown_reasons": self.unknown_reasons,
                "temporal_coverage": self.temporal_coverage,
                "microstructure_known": self.microstructure_known,
                "diagnostic_metric_status": self.diagnostic_metric_status,
                "outcomes": {
                    str(horizon): outcome.as_dict()
                    for horizon, outcome in self.outcomes.items()
                },
            }
        )


def _portfolio_key(snapshot: BookSnapshot) -> tuple[str, str, str, str]:
    return (
        snapshot.event_id,
        snapshot.market_id,
        snapshot.token_id,
        snapshot.market_day or snapshot.timestamp.date().isoformat(),
    )


def _scope(snapshot: BookSnapshot) -> tuple[str, str]:
    return (
        (snapshot.station_id or "unknown").upper(),
        snapshot.market_day or snapshot.timestamp.date().isoformat(),
    )


def _candidate(snapshot: BookSnapshot) -> bool:
    return _flag(snapshot.metadata.get("weather_market_lag")) and _flag(
        snapshot.metadata.get("weather_improving")
    )


def _eligible_information_events(
    events: Sequence[InformationEvent], scope: tuple[str, str]
) -> tuple[InformationEvent, ...]:
    return tuple(
        sorted(
            (
                event
                for event in events
                if event.scope == scope and event.phase_eligible
            ),
            key=lambda event: (event.available_at, event.source_at, event.event_id),
        )
    )


def _hard_resets(
    events: Sequence[InformationEvent], scope: tuple[str, str]
) -> tuple[InformationEvent, ...]:
    return tuple(
        event
        for event in _eligible_information_events(events, scope)
        if event.impact_class is ImpactClass.HARD_RESET
    )


def _episode_id(
    resets: Sequence[InformationEvent], candidate_at: datetime, scope: tuple[str, str]
) -> str:
    eligible = [event for event in resets if event.available_by(candidate_at)]
    if eligible:
        return eligible[-1].event_id
    return f"initial:{scope[0]}:{scope[1]}"


def _information_between(
    events: Sequence[InformationEvent], start: datetime, end: datetime
) -> tuple[InformationEvent, ...]:
    return tuple(
        event
        for event in events
        if event.available_at is not None
        and event.source_at is not None
        and start < event.available_at <= end
        and event.source_at <= end
    )


def _resets_between(
    resets: Sequence[InformationEvent], start: datetime, end: datetime
) -> tuple[InformationEvent, ...]:
    return _information_between(resets, start, end)


def _gap_reasons(
    snapshots: Sequence[BookSnapshot],
    *,
    start: datetime,
    end: datetime,
    maximum_gap: timedelta | None,
    label: str,
) -> list[str]:
    if not snapshots:
        return [f"insufficient_{label}_coverage"]
    if maximum_gap is None:
        return []
    points = [start, *(snapshot.timestamp for snapshot in snapshots), end]
    return (
        [f"{label}_archive_gap"]
        if any(
            later - earlier > maximum_gap
            for earlier, later in zip(points, points[1:], strict=False)
        )
        else []
    )


def _rule_reasons(snapshot: BookSnapshot) -> list[str]:
    provenance = snapshot.metadata.get("rule_provenance")
    if not isinstance(provenance, Mapping):
        return ["rule_provenance_missing"]
    reasons: list[str] = []
    for field_name in ("tick_size", "min_order_size"):
        source = str(provenance.get(f"{field_name}_source") or "UNKNOWN").upper()
        try:
            value = _decimal(provenance.get(field_name))
        except ValueError:
            value = None
        if value is None or source == "UNKNOWN":
            reasons.append(f"{field_name}_provenance_unknown")
    return reasons


def _snapshot_reasons(
    snapshot: BookSnapshot,
    *,
    required_metrics: Sequence[str],
    require_rule_provenance: bool,
) -> list[str]:
    reasons: list[str] = []
    if snapshot.quality_excluded:
        reasons.append("quality_window_excluded")
    if _flag(snapshot.metadata.get("archive_gap")):
        reasons.append("archive_gap")
    if _flag(snapshot.metadata.get("snapshot_interval_gap")):
        reasons.append("snapshot_interval_gap")
    if not snapshot.book_complete:
        reasons.append("incomplete_book")
    if snapshot.market_stale:
        reasons.append("market_stale")
    if snapshot.upstream_status.casefold() != "normal":
        reasons.append("upstream_not_normal")
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    if bid is None or ask is None:
        reasons.append("unquotable_book")
    elif bid >= ask:
        reasons.append("non_positive_spread")
    metrics = metrics_from_snapshot(snapshot)
    for name in required_metrics:
        if metrics.knowledge_for(name) is not MetricKnowledge.OK:
            reasons.append(f"metric_unknown:{name}:{metrics.knowledge_for(name).value}")
    if require_rule_provenance:
        reasons.extend(_rule_reasons(snapshot))
    return reasons


def _bid_depth_usd(snapshot: BookSnapshot) -> Decimal:
    return sum((price * size for price, size in snapshot.bids), start=ZERO)


def _outcome(
    *,
    decision_snapshot: BookSnapshot,
    future: Sequence[BookSnapshot],
    horizon: int,
    endpoint: datetime,
    maximum_gap: timedelta | None,
    target_rises: Sequence[Decimal],
    contaminated: bool,
    information_event_count: int,
    required_metrics: Sequence[str],
    require_rule_provenance: bool,
) -> PricePathOutcome:
    if contaminated:
        return PricePathOutcome(
            horizon,
            "contaminated_by_hard_reset",
            new_information_before_endpoint=information_event_count > 0,
            new_information_event_count=information_event_count,
        )
    relevant = [snapshot for snapshot in future if snapshot.timestamp <= endpoint]
    gap_reasons = _gap_reasons(
        relevant,
        start=decision_snapshot.timestamp,
        end=endpoint,
        maximum_gap=maximum_gap,
        label="outcome",
    )
    if gap_reasons:
        return PricePathOutcome(
            horizon,
            gap_reasons[0],
            observation_count=len(relevant),
            new_information_before_endpoint=information_event_count > 0,
            new_information_event_count=information_event_count,
        )
    bad_reasons = {
        reason
        for snapshot in relevant
        for reason in _snapshot_reasons(
            snapshot,
            required_metrics=required_metrics,
            require_rule_provenance=require_rule_provenance,
        )
    }
    if bad_reasons:
        return PricePathOutcome(
            horizon,
            f"unknown:{sorted(bad_reasons)[0]}",
            observation_count=len(relevant),
            new_information_before_endpoint=information_event_count > 0,
            new_information_event_count=information_event_count,
        )
    terminal = relevant[-1]
    decision_bid = decision_snapshot.best_bid
    decision_ask = decision_snapshot.best_ask
    terminal_bid = terminal.best_bid
    terminal_ask = terminal.best_ask
    if None in (decision_bid, decision_ask, terminal_bid, terminal_ask):
        return PricePathOutcome(
            horizon,
            "unknown:unquotable_book",
            new_information_before_endpoint=information_event_count > 0,
            new_information_event_count=information_event_count,
        )
    bids = [snapshot.best_bid for snapshot in relevant if snapshot.best_bid is not None]
    spreads = (decision_snapshot.spread, terminal.spread)
    all_bids = [decision_bid, *bids]
    jumps = [
        abs(later - earlier)
        for earlier, later in zip(all_bids, all_bids[1:], strict=False)
    ]
    return PricePathOutcome(
        horizon_minutes=horizon,
        status="complete",
        observation_count=len(relevant),
        terminal_at=terminal.timestamp,
        best_bid_change=terminal_bid - decision_bid,
        best_ask_change=terminal_ask - decision_ask,
        maximum_favorable_bid_change=max(bids) - decision_bid,
        maximum_adverse_bid_change=min(bids) - decision_bid,
        maximum_bid_jump=max(jumps) if jumps else ZERO,
        spread_change=(
            spreads[1] - spreads[0]
            if spreads[0] is not None and spreads[1] is not None
            else None
        ),
        bid_depth_usd_change=_bid_depth_usd(terminal) - _bid_depth_usd(decision_snapshot),
        new_information_before_endpoint=information_event_count > 0,
        new_information_event_count=information_event_count,
        target_hits={
            target: any(bid >= decision_bid + target for bid in bids)
            for target in target_rises
        },
    )


def _evaluate_one(
    candidate: BookSnapshot,
    snapshots: Sequence[BookSnapshot],
    *,
    episode_id: str,
    resets: Sequence[InformationEvent],
    information_events: Sequence[InformationEvent],
    config: MarketStateChallengerConfig,
) -> CandidateEvaluation:
    trailing_start = candidate.timestamp - timedelta(
        minutes=config.trailing_window_minutes
    )
    latest_reset = next(
        (
            event
            for event in reversed(resets)
            if event.available_by(candidate.timestamp)
        ),
        None,
    )
    trailing_boundary = max(
        trailing_start,
        latest_reset.available_at
        if latest_reset is not None and latest_reset.available_at is not None
        else trailing_start,
    )
    confirmation_cutoff = candidate.timestamp + timedelta(
        minutes=config.confirmation_window_minutes
    )
    decision_candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot.timestamp >= confirmation_cutoff
    ]
    decision_snapshot = decision_candidates[0] if decision_candidates else None
    decision_at = (
        decision_snapshot.timestamp
        if decision_snapshot is not None
        else confirmation_cutoff
    )
    maximum_gap = (
        timedelta(minutes=config.maximum_snapshot_gap_minutes)
        if config.maximum_snapshot_gap_minutes is not None
        else None
    )
    trailing = [
        snapshot
        for snapshot in snapshots
        if trailing_start <= snapshot.timestamp < candidate.timestamp
        and (
            latest_reset is None
            or latest_reset.available_at is None
            or snapshot.timestamp > latest_reset.available_at
        )
    ]
    confirmation = [
        snapshot
        for snapshot in snapshots
        if candidate.timestamp < snapshot.timestamp <= decision_at
    ]
    unknown: list[str] = []
    if len(trailing) < config.minimum_trailing_observations:
        unknown.append("insufficient_trailing_observations")
    if len(confirmation) < config.minimum_confirmation_observations:
        unknown.append("insufficient_confirmation_observations")
    if decision_snapshot is None:
        unknown.append("decision_snapshot_unavailable")
    window_timestamps = [
        snapshot.timestamp
        for snapshot in snapshots
        if trailing_start <= snapshot.timestamp <= decision_at
    ]
    if len(window_timestamps) != len(set(window_timestamps)):
        unknown.append("ambiguous_duplicate_snapshot_timestamp")
    unknown.extend(
        _gap_reasons(
            trailing,
            start=trailing_boundary,
            end=candidate.timestamp,
            maximum_gap=maximum_gap,
            label="trailing",
        )
    )
    unknown.extend(
        _gap_reasons(
            confirmation,
            start=candidate.timestamp,
            end=decision_at,
            maximum_gap=maximum_gap,
            label="confirmation",
        )
    )
    if _resets_between(resets, candidate.timestamp, decision_at):
        unknown.append("hard_reset_during_confirmation")
    diagnostic_statuses: dict[str, set[str]] = {
        name: set() for name in config.diagnostic_metric_names
    }
    for snapshot in (*trailing, candidate, *confirmation):
        metrics = metrics_from_snapshot(snapshot)
        for name in config.diagnostic_metric_names:
            diagnostic_statuses[name].add(metrics.knowledge_for(name).value)
        unknown.extend(
            _snapshot_reasons(
                snapshot,
                required_metrics=config.required_metric_names,
                require_rule_provenance=config.require_rule_provenance,
            )
        )
    unknown = sorted(set(unknown))
    temporal_reasons = {
        "insufficient_trailing_observations",
        "insufficient_confirmation_observations",
        "decision_snapshot_unavailable",
        "ambiguous_duplicate_snapshot_timestamp",
        "trailing_archive_gap",
        "confirmation_archive_gap",
        "hard_reset_during_confirmation",
    }
    temporal_coverage = not any(reason in temporal_reasons for reason in unknown)
    microstructure_known = temporal_coverage and not unknown
    diagnostic_metric_status = {
        name: (
            MetricKnowledge.OK.value
            if values == {MetricKnowledge.OK.value}
            else "|".join(sorted(values))
            if values
            else MetricKnowledge.UNKNOWN_TAPE_GAP.value
        )
        for name, values in diagnostic_statuses.items()
    }
    trailing_bids = [snapshot.best_bid for snapshot in trailing if snapshot.best_bid is not None]
    trailing_high = max(trailing_bids) if trailing_bids else None
    breakout_level = (
        trailing_high + candidate.tick_size * config.breakout_ticks
        if trailing_high is not None
        else None
    )
    confirmation_bids = [
        snapshot.best_bid
        for snapshot in confirmation
        if snapshot.best_bid is not None
    ]
    breakout_count = (
        sum(bid >= breakout_level for bid in confirmation_bids)
        if breakout_level is not None
        else 0
    )
    if unknown:
        state = BreakoutState.UNKNOWN
        reason = unknown[0]
    elif breakout_level is None or trailing_high is None or not confirmation_bids:
        state = BreakoutState.UNKNOWN
        reason = "price_coverage_unknown"
        unknown = sorted({*unknown, reason})
        microstructure_known = False
    else:
        survival_ratio = Decimal(breakout_count) / Decimal(len(confirmation_bids))
        final_bid = confirmation_bids[-1]
        if survival_ratio >= config.survival_fraction and final_bid >= breakout_level:
            state = BreakoutState.SURVIVING_BREAKOUT
            reason = "breakout_above_strict_prior_range_survived_confirmation"
        elif breakout_count > 0 and final_bid <= trailing_high:
            state = BreakoutState.FAILED_BREAKOUT
            reason = "breakout_returned_to_strict_prior_range"
        else:
            state = BreakoutState.UNCONFIRMED
            reason = "breakout_threshold_not_confirmed"
    outcome_anchor = decision_snapshot or candidate
    future = [
        snapshot
        for snapshot in snapshots
        if decision_at < snapshot.timestamp <= config.forward_cutoff
    ]
    scope = _scope(candidate)
    scoped_information = _eligible_information_events(information_events, scope)
    outcomes: dict[int, PricePathOutcome] = {}
    for horizon in config.outcome_horizons_minutes:
        endpoint = decision_at + timedelta(minutes=horizon)
        new_information = _information_between(
            scoped_information, decision_at, endpoint
        )
        contaminated = any(
            event.impact_class is ImpactClass.HARD_RESET
            for event in new_information
        )
        if endpoint > config.forward_cutoff:
            outcomes[horizon] = PricePathOutcome(horizon, "beyond_forward_cutoff")
        elif state is BreakoutState.UNKNOWN:
            outcomes[horizon] = PricePathOutcome(horizon, "candidate_state_unknown")
        else:
            outcomes[horizon] = _outcome(
                decision_snapshot=outcome_anchor,
                future=future,
                horizon=horizon,
                endpoint=endpoint,
                maximum_gap=maximum_gap,
                target_rises=config.bid_target_rises,
                contaminated=contaminated,
                information_event_count=len(new_information),
                required_metrics=OUTCOME_REQUIRED_METRICS,
                require_rule_provenance=config.require_rule_provenance,
            )
    scope = _scope(candidate)
    return CandidateEvaluation(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        token_id=candidate.token_id,
        station_id=scope[0],
        market_day=scope[1],
        episode_id=episode_id,
        candidate_at=candidate.timestamp,
        decision_at=decision_at,
        state=state,
        state_reason=reason,
        trailing_high_bid=trailing_high,
        breakout_level=breakout_level,
        confirmation_observation_count=len(confirmation),
        breakout_observation_count=breakout_count,
        unknown_reasons=tuple(unknown),
        temporal_coverage=temporal_coverage,
        microstructure_known=microstructure_known,
        diagnostic_metric_status=diagnostic_metric_status,
        outcomes=outcomes,
    )


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
    return BookSnapshot(
        timestamp=snapshot.timestamp,
        event_id=snapshot.event_id,
        market_id=snapshot.market_id,
        token_id=snapshot.token_id,
        bids=snapshot.bids,
        asks=snapshot.asks,
        station_id=snapshot.station_id,
        market_day=snapshot.market_day,
        tick_size=snapshot.tick_size,
        min_order_size=snapshot.min_order_size,
        book_complete=snapshot.book_complete,
        market_stale=snapshot.market_stale,
        weather_stale=snapshot.weather_stale,
        settlement_verified=snapshot.settlement_verified,
        in_season=snapshot.in_season,
        season_version=snapshot.season_version,
        warming_valid=snapshot.warming_valid,
        upstream_status=snapshot.upstream_status,
        quality_excluded=snapshot.quality_excluded,
        metadata=metadata,
    )


def _enrich_microstructure(
    snapshots: Sequence[BookSnapshot],
    *,
    trades: Sequence[TradeEvent],
    l2_churn_index: L2ChurnIndex | None,
    station_timezones: Mapping[str, str],
) -> tuple[BookSnapshot, ...]:
    if not trades and l2_churn_index is None:
        return tuple(snapshots)
    tracker = TradeIntensityTracker(station_timezones=station_timezones)
    trades_by_token: dict[str, list[TradeEvent]] = defaultdict(list)
    for trade in trades:
        trades_by_token[trade.asset_id].append(trade)
    trade_times = {
        token_id: [max(trade.timestamp, trade.available_at or trade.timestamp) for trade in rows]
        for token_id, rows in trades_by_token.items()
    }
    previous_at: dict[tuple[str, str, str, str], datetime] = {}
    output: list[BookSnapshot] = []
    for snapshot in sorted(
        snapshots,
        key=lambda row: (row.timestamp, row.event_id, row.market_id, row.token_id),
    ):
        key = _portfolio_key(snapshot)
        prior_at = previous_at.get(key)
        rows = trades_by_token.get(snapshot.token_id, ())
        times = trade_times.get(snapshot.token_id, ())
        start = bisect_right(times, prior_at) if prior_at is not None else 0
        end = bisect_left(times, snapshot.timestamp)
        context = tracker.observe(
            snapshot,
            previous_at=prior_at,
            trades=rows[start:end],
        )
        if l2_churn_index is not None:
            churn_context = l2_churn_index.lookup(snapshot.token_id, snapshot.timestamp)
            incoming_status = dict(context.get("metric_status") or {})
            incoming_status.update(churn_context.get("metric_status") or {})
            context = {
                **context,
                **{key: value for key, value in churn_context.items() if key != "metric_status"},
                "metric_status": incoming_status,
            }
        output.append(_merge_metric_context(snapshot, context))
        previous_at[key] = snapshot.timestamp
    return tuple(output)


def evaluate_candidates(
    snapshots: Sequence[BookSnapshot],
    *,
    config: MarketStateChallengerConfig,
    information_events: Sequence[InformationEvent] = (),
) -> tuple[CandidateEvaluation, ...]:
    """Classify the first receipt-safe candidate in each token episode."""
    grouped: dict[tuple[str, str, str, str], list[BookSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.timestamp <= config.forward_cutoff:
            grouped[_portfolio_key(snapshot)].append(snapshot)
    output: list[CandidateEvaluation] = []
    for token_snapshots in grouped.values():
        token_snapshots.sort(key=lambda snapshot: snapshot.timestamp)
        scope = _scope(token_snapshots[0])
        resets = _hard_resets(information_events, scope)
        seen_episodes: set[str] = set()
        for candidate in token_snapshots:
            if not _candidate(candidate):
                continue
            episode_id = _episode_id(resets, candidate.timestamp, scope)
            if episode_id in seen_episodes:
                continue
            seen_episodes.add(episode_id)
            output.append(
                _evaluate_one(
                    candidate,
                    token_snapshots,
                    episode_id=episode_id,
                    resets=resets,
                    information_events=information_events,
                    config=config,
                )
            )
    return tuple(
        sorted(
            output,
            key=lambda row: (
                row.candidate_at,
                row.station_id,
                row.market_day,
                row.market_id,
                row.token_id,
            ),
        )
    )


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


def _number_summary(values: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"sample_count": 0, "mean": None, "p50": None, "p90": None}

    def percentile(probability: float) -> float:
        position = probability * (len(ordered) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "sample_count": len(ordered),
        "mean": fmean(ordered),
        "p50": median(ordered),
        "p90": percentile(0.90),
    }


def _clustered_summary(
    candidates: Sequence[CandidateEvaluation],
    *,
    horizon: int,
    targets: Sequence[Decimal],
    information_cohort: bool | None = None,
) -> dict[str, Any]:
    complete = [
        candidate
        for candidate in candidates
        if candidate.outcomes[horizon].status == "complete"
        and (
            information_cohort is None
            or candidate.outcomes[horizon].new_information_before_endpoint
            is information_cohort
        )
    ]
    clusters: dict[tuple[str, str], list[PricePathOutcome]] = defaultdict(list)
    for candidate in complete:
        clusters[candidate.station_day].append(candidate.outcomes[horizon])
    bid_changes = [
        float(fmean(float(outcome.best_bid_change) for outcome in outcomes))
        for outcomes in clusters.values()
        if all(outcome.best_bid_change is not None for outcome in outcomes)
    ]
    favorable = [
        float(
            fmean(float(outcome.maximum_favorable_bid_change) for outcome in outcomes)
        )
        for outcomes in clusters.values()
        if all(outcome.maximum_favorable_bid_change is not None for outcome in outcomes)
    ]
    adverse = [
        float(fmean(float(outcome.maximum_adverse_bid_change) for outcome in outcomes))
        for outcomes in clusters.values()
        if all(outcome.maximum_adverse_bid_change is not None for outcome in outcomes)
    ]
    return {
        "candidate_count": len(candidates),
        "outcome_complete_candidate_count": len(complete),
        "station_day_count": len(clusters),
        "information_cohort": (
            "all"
            if information_cohort is None
            else "new_information"
            if information_cohort
            else "no_new_information"
        ),
        "cluster_rule": "one station-day; target hit if any same-cluster candidate hit",
        "new_information_before_endpoint": _rate(
            sum(
                any(outcome.new_information_before_endpoint for outcome in outcomes)
                for outcomes in clusters.values()
            ),
            len(clusters),
        ),
        "best_bid_change": _number_summary(bid_changes),
        "maximum_favorable_bid_change": _number_summary(favorable),
        "maximum_adverse_bid_change": _number_summary(adverse),
        "bid_targets": {
            str(target): _rate(
                sum(
                    any(outcome.target_hits.get(target, False) for outcome in outcomes)
                    for outcomes in clusters.values()
                ),
                len(clusters),
            )
            for target in targets
        },
    }


def analyze_market_state_challenger(
    snapshots: Sequence[BookSnapshot],
    *,
    config: MarketStateChallengerConfig,
    information_events: Sequence[InformationEvent] = (),
    trades: Sequence[TradeEvent] = (),
    l2_churn_index: L2ChurnIndex | None = None,
    station_timezones: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a conservation-checked direction/path report, never a PnL report."""
    snapshots = _enrich_microstructure(
        snapshots,
        trades=trades,
        l2_churn_index=l2_churn_index,
        station_timezones=station_timezones or {},
    )
    raw_candidates = sum(
        _candidate(snapshot) and snapshot.timestamp <= config.forward_cutoff
        for snapshot in snapshots
    )
    candidates = evaluate_candidates(
        snapshots,
        config=config,
        information_events=information_events,
    )
    state_counts = Counter(candidate.state.value for candidate in candidates)
    for state in BreakoutState:
        state_counts.setdefault(state.value, 0)
    classified = [
        candidate for candidate in candidates if candidate.state is not BreakoutState.UNKNOWN
    ]
    outcome_complete = [
        candidate
        for candidate in classified
        if all(outcome.status == "complete" for outcome in candidate.outcomes.values())
    ]
    groups: dict[str, list[CandidateEvaluation]] = {
        "RAW_COHORT": list(candidates),
        **{
            state.value: [
                candidate for candidate in candidates if candidate.state is state
            ]
            for state in BreakoutState
        },
    }
    summaries = {
        label: {
            str(horizon): _clustered_summary(
                rows,
                horizon=horizon,
                targets=config.bid_target_rises,
            )
            for horizon in config.outcome_horizons_minutes
        }
        for label, rows in groups.items()
    }
    information_summaries = {
        label: {
            str(horizon): {
                cohort: _clustered_summary(
                    rows,
                    horizon=horizon,
                    targets=config.bid_target_rises,
                    information_cohort=has_new_information,
                )
                for cohort, has_new_information in (
                    ("no_new_information", False),
                    ("new_information", True),
                )
            }
            for horizon in config.outcome_horizons_minutes
        }
        for label, rows in groups.items()
    }
    station_summaries: dict[str, Any] = {}
    stations = sorted({candidate.station_id for candidate in candidates})
    for station in stations:
        station_rows = [
            candidate for candidate in candidates if candidate.station_id == station
        ]
        station_summaries[station] = {
            state.value: {
                str(horizon): _clustered_summary(
                    [candidate for candidate in station_rows if candidate.state is state],
                    horizon=horizon,
                    targets=config.bid_target_rises,
                )
                for horizon in config.outcome_horizons_minutes
            }
            for state in BreakoutState
        }
    conservation_ok = sum(state_counts.values()) == len(candidates)
    if not conservation_ok:
        raise AssertionError("candidate states do not conserve unique episodes")
    return {
        "report_type": "market_direction_path_diagnostic",
        "pnl_claim": False,
        "maker_fill_claim": False,
        "execution_enabled": EXECUTION_ENABLED,
        "config": config.as_dict(),
        "candidate_funnel": {
            "raw_candidate": raw_candidates,
            "unique_episode": len(candidates),
            "temporal_coverage": sum(
                candidate.temporal_coverage for candidate in candidates
            ),
            "known_microstructure": sum(
                candidate.microstructure_known for candidate in candidates
            ),
            "classified": len(classified),
            "outcome_complete": len(outcome_complete),
        },
        "state_counts": dict(sorted(state_counts.items())),
        "state_conservation_ok": conservation_ok,
        "summaries": summaries,
        "information_summaries": information_summaries,
        "station_summaries": station_summaries,
        "candidates": [candidate.as_dict() for candidate in candidates],
    }


def render_market_state_challenger_report(
    result: Mapping[str, Any], output_path: Path | str
) -> None:
    """Write a compact Markdown diagnostic with explicit evidence boundaries."""
    funnel = result.get("candidate_funnel") or {}
    state_counts = result.get("state_counts") or {}
    lines = [
        "# Market-State Challenger v1",
        "",
        "## Evidence boundary",
        "",
        "- This is a market direction/path diagnostic, not a maker-fill or PnL result.",
        "- All outcomes use the same token's archived best bid/ask strictly after `decision_at`.",
        "- `execution_enabled=false`; no strategy ledger or live runtime is modified.",
        "- UNKNOWN is retained separately and is never converted to failure or zero.",
        "",
        "## Candidate attrition",
        "",
        "| Stage | Count |",
        "|---|---:|",
    ]
    for name in (
        "raw_candidate",
        "unique_episode",
        "temporal_coverage",
        "known_microstructure",
        "classified",
        "outcome_complete",
    ):
        lines.append(f"| {name} | {int(funnel.get(name, 0))} |")
    lines.extend(
        [
            "",
            "## State counts",
            "",
            "| State | Count |",
            "|---|---:|",
        ]
    )
    for state in BreakoutState:
        lines.append(f"| {state.value} | {int(state_counts.get(state.value, 0))} |")
    lines.extend(
        [
            "",
            f"State conservation: `{bool(result.get('state_conservation_ok'))}`",
            "",
            "## Clustered path summaries",
            "",
            "Rates below use one station-day as the independent unit. n<30 is marked unreliable.",
            "",
        ]
    )
    summaries = result.get("summaries") or {}
    config = result.get("config") or {}
    horizons = [str(value) for value in config.get("outcome_horizons_minutes", ())]
    targets = [str(value) for value in config.get("bid_target_rises", ())]
    for label in ("RAW_COHORT", *[state.value for state in BreakoutState]):
        lines.extend(
            [
                f"### {label}",
                "",
                "| Horizon | Station-days | Mean bid change | Targets (Wilson 95%) |",
                "|---:|---:|---:|---|",
            ]
        )
        values = summaries.get(label) or {}
        for horizon in horizons:
            summary = values.get(horizon) or {}
            bid = summary.get("best_bid_change") or {}
            mean = bid.get("mean")
            target_values = summary.get("bid_targets") or {}
            target_text: list[str] = []
            for target in targets:
                rate = target_values.get(target) or {}
                sample_count = int(rate.get("sample_count", 0))
                if rate.get("rate") is None:
                    target_text.append(f"+{target}: N=0")
                    continue
                unreliable = " (n<30)" if rate.get("statistically_unreliable") else ""
                low = rate.get("wilson_low")
                high = rate.get("wilson_high")
                interval = (
                    "N/A"
                    if low is None or high is None
                    else f"{float(low):.1%}–{float(high):.1%}"
                )
                target_text.append(
                    f"+{target}: {int(rate.get('count', 0))}/{sample_count} "
                    f"({float(rate['rate']):.1%}; {interval}){unreliable}"
                )
            mean_text = "N/A" if mean is None else f"{float(mean):+.4f}"
            lines.append(
                f"| {horizon}m | {int(summary.get('station_day_count', 0))} | "
                f"{mean_text} | {'; '.join(target_text)} |"
            )
        lines.append("")
    lines.extend(
        [
            "## New-information stratification",
            "",
            "Complete outcomes are split by whether a receipt-safe information event arrived "
            "after `decision_at` and by the endpoint. HARD_RESET-contaminated paths are "
            "censored before this split.",
            "",
            "| State | Horizon | Cohort | Station-days | Mean bid change |",
            "|---|---:|---|---:|---:|",
        ]
    )
    information_summaries = result.get("information_summaries") or {}
    for label in ("RAW_COHORT", *[state.value for state in BreakoutState]):
        label_values = information_summaries.get(label) or {}
        for horizon in horizons:
            horizon_values = label_values.get(horizon) or {}
            for cohort in ("no_new_information", "new_information"):
                summary = horizon_values.get(cohort) or {}
                bid = summary.get("best_bid_change") or {}
                mean = bid.get("mean")
                mean_text = "N/A" if mean is None else f"{float(mean):+.4f}"
                lines.append(
                    f"| {label} | {horizon}m | {cohort} | "
                    f"{int(summary.get('station_day_count', 0))} | {mean_text} |"
                )
    lines.append("")
    Path(output_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_market_state_challenger_result(
    result: Mapping[str, Any], output_path: Path | str
) -> None:
    Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
