"""Receipt-time forensics for the historical QUIET v2 zero-fill cohort.

The module deliberately separates three questions that are easy to conflate:

* whether the contemporaneous market rules were archived and therefore make an
  historical order eligible for an executable claim;
* whether a token-native quote path ever reached a resting maker limit; and
* whether receipt-available opposite-side trades could consume the recorded
  queue ahead.

It is read-only diagnostic code.  A TOUCH observation is an optimistic upper
bound, L2 removals are never converted into a fill, and current CLOB rules are
never used to repair an old archive row.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.archive_io import open_jsonl_text
from poly_weather.market_microstructure import (
    CrossBucketMassIndex,
    L2ChurnIndex,
    build_cross_bucket_mass_index,
)
from poly_weather.market_regime import (
    MarketRegimeStateMachine,
    RegimeState,
    RegimeTransition,
    ThresholdTier,
)
from poly_weather.no_forward import wilson_interval
from poly_weather.polymarket_status import (
    load_quality_windows,
    market_record_is_analysis_eligible,
)
from poly_weather.shadow_orders import BookSnapshot, ShadowSide, TradeEvent

ZERO = Decimal("0")
ONE = Decimal("1")
FORENSIC_VERSION = "quiet-window-v2-zero-fill-forensics-v1"
COUNTERFACTUAL_MINUTES = (5, 15, 30, 60)
SMALL_SIZE_USD = (Decimal("5"), Decimal("10"), Decimal("20"), Decimal("50"))
CROSS_BUCKET_TOLERANCE_MINUTES = (1, 2, 5, 10)

INVALID_RULE_PROVENANCE = "INVALID_RULE_PROVENANCE"
NO_LATER_HEALTHY_BOOK = "NO_LATER_HEALTHY_BOOK"
NEVER_TOUCHED = "NEVER_TOUCHED"
TOUCHED_QUEUE_NOT_CLEARED = "TOUCHED_QUEUE_NOT_CLEARED"
UNKNOWN_TAPE_GAP = "UNKNOWN_TAPE_GAP"
CANCELLED_BEFORE_LATER_TOUCH = "CANCELLED_BEFORE_LATER_TOUCH"
TRUE_ZERO_OPPOSITE_TRADE = "TRUE_ZERO_OPPOSITE_TRADE"
OTHER_WITH_EVIDENCE = "OTHER_WITH_EVIDENCE"


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = quantile * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"sample_count": 0, "mean": None, "p50": None, "p90": None}
    return {
        "sample_count": len(values),
        "mean": fmean(values),
        "p50": median(values),
        "p90": _percentile(values, 0.90),
    }


def annotate_episode_book_observations(
    episodes: Sequence[MutableMapping[str, Any]],
    pair_factory: Callable[[], Iterable[Mapping[str, Any]]],
    *,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Attach exact paired-book observation counts to reconstructed episodes.

    This is a measurement-only pass over the same local paired-book stream
    used by the replay. It does not advance a regime machine, alter an order,
    or infer a fill. Each YES/NO token frame is counted independently because
    QUIET state is token-scoped.
    """
    intervals_by_token: dict[
        str, list[tuple[datetime, datetime, MutableMapping[str, Any]]]
    ] = defaultdict(list)
    upper = _utc(cutoff) if cutoff is not None else None
    for episode in episodes:
        episode["book_observation_count"] = 0
        token = str(episode.get("token_id") or "")
        try:
            start = _utc(episode["quiet_start_at"])
            end = _utc(episode["quiet_end_at"])
        except (KeyError, TypeError, ValueError):
            episode["book_observation_count"] = None
            continue
        if upper is not None:
            end = min(end, upper)
        if not token or end < start:
            episode["book_observation_count"] = None
            continue
        intervals_by_token[token].append((start, end, episode))

    scanned_pair_count = 0
    for pair in pair_factory():
        observed_at = pair.get("observed_at")
        if observed_at is None:
            continue
        try:
            timestamp = _utc(observed_at)
        except (TypeError, ValueError):
            continue
        if upper is not None and timestamp > upper:
            break
        scanned_pair_count += 1
        for token_side in (pair.get("yes"), pair.get("no")):
            if not isinstance(token_side, Mapping):
                continue
            token = str(token_side.get("asset_id") or "")
            for start, end, episode in intervals_by_token.get(token, ()):
                if start <= timestamp <= end:
                    count = episode.get("book_observation_count")
                    episode["book_observation_count"] = int(count or 0) + 1

    values = [
        float(count)
        for episode in episodes
        if isinstance((count := episode.get("book_observation_count")), int)
    ]
    return {
        "episode_count": len(episodes),
        "scanned_pair_count": scanned_pair_count,
        "book_observations_per_episode": _summary(values),
        "source": "same_local_paired_token_native_book_stream",
        "execution_enabled": False,
    }


def _best(rows: Any, *, bids: bool) -> Decimal | None:
    if not isinstance(rows, list):
        return None
    values: list[Decimal] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        price = _decimal(row.get("price"))
        size = _decimal(row.get("size"))
        if price is not None and size is not None and size > ZERO:
            values.append(price)
    return (max(values) if bids else min(values)) if values else None


def _event_slug(market_slug: Any) -> str | None:
    text = str(market_slug or "")
    base, separator, outcome = text.rpartition(":")
    if not separator or outcome.casefold() not in {"yes", "no"}:
        return None
    return base.split("/", 1)[0] or None


def _market_slug(market_slug: Any) -> str | None:
    text = str(market_slug or "")
    base, separator, outcome = text.rpartition(":")
    if not separator or outcome.casefold() not in {"yes", "no"}:
        return None
    return base or None


def _effective_trade_at(trade: TradeEvent) -> datetime:
    return max(trade.timestamp, trade.available_at or trade.timestamp)


def _order_end(order: Mapping[str, Any]) -> datetime:
    for key in ("last_fill_at", "cancelled_at", "expired_at"):
        if order.get(key) is not None:
            return _utc(order[key])
    return _utc(order["submitted_at"])


def _order_side(order: Mapping[str, Any]) -> ShadowSide:
    return ShadowSide(str(order.get("side") or "BUY").upper())


def _tick_aligned(price: Decimal, tick: Decimal) -> bool:
    if tick <= ZERO:
        return False
    return (price / tick) == (price / tick).to_integral_value()


def _prefer_retention_archive_paths(paths: Iterable[Path]) -> tuple[tuple[Path, ...], int]:
    """Use the mutable JSONL over its completed-gzip duplicate per date."""
    selected: dict[Path, Path] = {}
    duplicates = 0
    for path in sorted({Path(value) for value in paths}):
        current = selected.get(path.parent)
        if current is None:
            selected[path.parent] = path
        elif current.name == "events.jsonl":
            duplicates += 1
        elif path.name == "events.jsonl":
            selected[path.parent] = path
            duplicates += 1
        else:
            duplicates += 1
    return tuple(sorted(selected.values())), duplicates


@dataclass(frozen=True, slots=True)
class RuleProvenance:
    """Last archived rule values known no later than an order submission."""

    tick_size: Decimal | None = None
    tick_observed_at: datetime | None = None
    tick_source: str = "UNKNOWN"
    min_order_size: Decimal | None = None
    min_order_size_observed_at: datetime | None = None
    min_order_size_source: str = "UNKNOWN"

    @property
    def valid(self) -> bool:
        return bool(
            self.tick_size is not None
            and self.tick_size > ZERO
            and self.min_order_size is not None
            and self.min_order_size > ZERO
        )

    def as_dict(self, *, limit_price: Decimal | None = None) -> dict[str, Any]:
        return {
            "status": "VALID_ARCHIVED" if self.valid else "UNKNOWN_OR_INVALID_ARCHIVED_RULE",
            "tick_size": self.tick_size,
            "tick_observed_at": self.tick_observed_at,
            "tick_source": self.tick_source,
            "min_order_size": self.min_order_size,
            "min_order_size_observed_at": self.min_order_size_observed_at,
            "min_order_size_source": self.min_order_size_source,
            "limit_tick_aligned": (
                _tick_aligned(limit_price, self.tick_size)
                if limit_price is not None and self.tick_size is not None
                else None
            ),
            "current_rule_backfill_used": False,
        }


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    timestamp: datetime
    event_id: str | None
    market_id: str | None
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    healthy: bool
    # Archived Gamma market metadata can carry a contemporaneous rule, but it
    # is not a book observation and therefore must never count as a quote-path
    # observation or maker touch.
    is_book_observation: bool = True
    tick_size: Decimal | None = None
    min_order_size: Decimal | None = None
    tick_source: str = "UNKNOWN"
    min_order_size_source: str = "UNKNOWN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp))


@dataclass(slots=True)
class QuietLifecycleCollector:
    """Keep only QUIET episode endpoints and order-window transition evidence."""

    orders: Sequence[Mapping[str, Any]]
    station_timezones: Mapping[str, str] = field(default_factory=dict)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    transitions_by_order: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _open_episodes: dict[tuple[str, str, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    _orders_by_key: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = field(
        init=False
    )

    def __post_init__(self) -> None:
        self._orders_by_key = defaultdict(list)
        for order in self.orders:
            tier = str(order.get("threshold_tier") or order.get("_tier") or "")
            station = str(order.get("station_id") or "").upper()
            day = str(order.get("market_day") or "")
            token = str(order.get("token_id") or "")
            self._orders_by_key[(tier, station, day, token)].append(order)

    @staticmethod
    def _transition_dict(transition: RegimeTransition) -> dict[str, Any]:
        metrics = transition.metrics
        return {
            "timestamp": transition.timestamp,
            "previous_state": transition.previous_state.value,
            "state": transition.state.value,
            "reason": transition.reason,
            "violations": list(transition.violations),
            "coverage_reasons": list(transition.coverage_reasons),
            "true_violations": list(transition.true_violations),
            "best_bid": metrics.best_bid if metrics is not None else None,
            "best_ask": metrics.best_ask if metrics is not None else None,
            "spread": metrics.spread if metrics is not None else None,
            "metric_status": dict(metrics.metric_status) if metrics is not None else {},
        }

    @staticmethod
    def _end_kind(transition: RegimeTransition) -> str:
        if transition.reason == "soft_external_information_update":
            return "soft_policy_cancel"
        if transition.coverage_reasons:
            return "coverage_flicker"
        if transition.true_violations:
            return "true_instability"
        if transition.reason == "new_external_information":
            return "new_external_information"
        if transition.reason == "predictable_release_approaching":
            return "pre_release_policy"
        return transition.reason

    @staticmethod
    def _price_band(value: Decimal | None) -> str:
        if value is None:
            return "UNKNOWN"
        if value < Decimal("0.10"):
            return "<0.10"
        if value < Decimal("0.30"):
            return "0.10-0.30"
        if value < Decimal("0.70"):
            return "0.30-0.70"
        if value < Decimal("0.90"):
            return "0.70-0.90"
        return ">=0.90"

    def observe(
        self,
        tier: ThresholdTier,
        snapshot: BookSnapshot,
        _machine: MarketRegimeStateMachine,
        transition: RegimeTransition,
    ) -> None:
        station = str(snapshot.station_id or "").upper()
        day = str(snapshot.market_day or "")
        token = snapshot.token_id
        key = (tier.value, station, day, token)
        transition_row = self._transition_dict(transition)
        for order in self._orders_by_key.get(key, ()):
            submitted = _utc(order["submitted_at"])
            ended = _order_end(order)
            if submitted <= transition.timestamp <= ended:
                self.transitions_by_order[str(order["order_id"])].append(transition_row)
        if (
            transition.previous_state is not RegimeState.QUIET
            and transition.state is RegimeState.QUIET
        ):
            local_hour = None
            timezone_name = self.station_timezones.get(station)
            if timezone_name:
                try:
                    local_hour = transition.timestamp.astimezone(ZoneInfo(timezone_name)).hour
                except (KeyError, ValueError):
                    local_hour = None
            self._open_episodes[key] = {
                "threshold_tier": tier.value,
                "station_id": station,
                "market_day": day,
                "token_id": token,
                "quiet_start_at": transition.timestamp,
                "quiet_end_at": None,
                "entry_best_bid": transition.metrics.best_bid if transition.metrics else None,
                "entry_price_band": self._price_band(
                    transition.metrics.best_bid if transition.metrics else None
                ),
                "local_hour": local_hour,
                "end_reason": None,
                "end_kind": None,
                "end_violations": [],
                "end_coverage_reasons": [],
                "end_true_violations": [],
            }
        elif (
            transition.previous_state is RegimeState.QUIET
            and transition.state is not RegimeState.QUIET
        ):
            episode = self._open_episodes.pop(key, None)
            if episode is not None:
                episode.update(
                    {
                        "quiet_end_at": transition.timestamp,
                        "end_reason": transition.reason,
                        "end_kind": self._end_kind(transition),
                        "end_violations": list(transition.violations),
                        "end_coverage_reasons": list(transition.coverage_reasons),
                        "end_true_violations": list(transition.true_violations),
                    }
                )
                self.episodes.append(episode)

    def finalize(self, *, cutoff: datetime) -> None:
        point = _utc(cutoff)
        for episode in self._open_episodes.values():
            episode.update(
                {
                    "quiet_end_at": point,
                    "end_reason": "analysis_cutoff",
                    "end_kind": "analysis_cutoff",
                }
            )
            self.episodes.append(episode)
        self._open_episodes.clear()

    def order_context(self, order: Mapping[str, Any]) -> dict[str, Any]:
        tier = str(order.get("threshold_tier") or order.get("_tier") or "")
        station = str(order.get("station_id") or "").upper()
        day = str(order.get("market_day") or "")
        token = str(order.get("token_id") or "")
        submitted = _utc(order["submitted_at"])
        matching = [
            row
            for row in self.episodes
            if (
                row["threshold_tier"],
                row["station_id"],
                row["market_day"],
                row["token_id"],
            )
            == (tier, station, day, token)
            and row["quiet_start_at"] <= submitted <= row["quiet_end_at"]
        ]
        episode = matching[-1] if matching else None
        transitions = self.transitions_by_order.get(str(order["order_id"]), [])
        end_transition = transitions[-1] if transitions else None
        return {
            "quiet_start_at": episode.get("quiet_start_at") if episode else None,
            "quiet_end_at": episode.get("quiet_end_at") if episode else None,
            "quiet_end_reason": episode.get("end_reason") if episode else None,
            "quiet_end_kind": episode.get("end_kind") if episode else None,
            "quiet_end_coverage_reasons": (
                episode.get("end_coverage_reasons", []) if episode else []
            ),
            "quiet_end_true_violations": (
                episode.get("end_true_violations", []) if episode else []
            ),
            "transition_count_during_order": len(transitions),
            "last_transition_during_order": end_transition,
        }

    def summary(self) -> dict[str, Any]:
        groups: dict[str, dict[str, dict[str, Any]]] = {
            "threshold_tier": defaultdict(dict),
            "station": defaultdict(dict),
            "local_hour": defaultdict(dict),
            "entry_price_band": defaultdict(dict),
        }
        end_counts: Counter[str] = Counter()
        end_reason_counts: Counter[str] = Counter()
        coverage_reason_counts: Counter[str] = Counter()
        true_violation_counts: Counter[str] = Counter()
        duration_by_group: dict[tuple[str, str], list[float]] = defaultdict(list)
        for episode in self.episodes:
            start = episode.get("quiet_start_at")
            end = episode.get("quiet_end_at")
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                continue
            duration = max(0.0, (end - start).total_seconds())
            end_counts[str(episode.get("end_kind") or "UNKNOWN")] += 1
            end_reason_counts[str(episode.get("end_reason") or "UNKNOWN")] += 1
            coverage_reason_counts.update(
                str(reason) for reason in (episode.get("end_coverage_reasons") or ())
            )
            true_violation_counts.update(
                str(reason) for reason in (episode.get("end_true_violations") or ())
            )
            labels = {
                "threshold_tier": str(episode.get("threshold_tier") or "UNKNOWN"),
                "station": str(episode.get("station_id") or "UNKNOWN"),
                "local_hour": str(episode.get("local_hour"))
                if episode.get("local_hour") is not None
                else "UNKNOWN",
                "entry_price_band": str(episode.get("entry_price_band") or "UNKNOWN"),
            }
            for dimension, label in labels.items():
                duration_by_group[(dimension, label)].append(duration)
        for (dimension, label), values in duration_by_group.items():
            groups[dimension][label] = _summary(values)
        return {
            "episode_count": len(self.episodes),
            "duration_seconds": _summary(
                [
                    max(0.0, (row["quiet_end_at"] - row["quiet_start_at"]).total_seconds())
                    for row in self.episodes
                    if isinstance(row.get("quiet_start_at"), datetime)
                    and isinstance(row.get("quiet_end_at"), datetime)
                ]
            ),
            "book_observations_per_episode": _summary(
                [
                    float(row["book_observation_count"])
                    for row in self.episodes
                    if isinstance(row.get("book_observation_count"), int)
                ]
            ),
            "end_kind_counts": dict(sorted(end_counts.items())),
            "end_reason_counts": dict(sorted(end_reason_counts.items())),
            "end_coverage_reason_counts": dict(sorted(coverage_reason_counts.items())),
            "end_true_violation_counts": dict(sorted(true_violation_counts.items())),
            "by_threshold_tier": dict(sorted(groups["threshold_tier"].items())),
            "by_station": dict(sorted(groups["station"].items())),
            "by_local_hour": dict(sorted(groups["local_hour"].items())),
            "by_entry_price_band": dict(sorted(groups["entry_price_band"].items())),
            "episodes": self.episodes,
        }


def _rule_at(quotes: Sequence[QuoteObservation], submitted_at: datetime) -> RuleProvenance:
    tick = tick_at = minimum = minimum_at = None
    tick_source = minimum_source = "UNKNOWN"
    for quote in quotes:
        if quote.timestamp > submitted_at:
            break
        if quote.tick_size is not None and quote.tick_size > ZERO:
            tick = quote.tick_size
            tick_at = quote.timestamp
            tick_source = quote.tick_source
        if quote.min_order_size is not None and quote.min_order_size > ZERO:
            minimum = quote.min_order_size
            minimum_at = quote.timestamp
            minimum_source = quote.min_order_size_source
    return RuleProvenance(
        tick_size=tick,
        tick_observed_at=tick_at,
        tick_source=tick_source,
        min_order_size=minimum,
        min_order_size_observed_at=minimum_at,
        min_order_size_source=minimum_source,
    )


def _quote_touch(quote: QuoteObservation, side: ShadowSide, limit: Decimal) -> bool:
    executable = quote.best_ask if side is ShadowSide.BUY else quote.best_bid
    return bool(
        executable is not None
        and (executable <= limit if side is ShadowSide.BUY else executable >= limit)
    )


def _nearest_distance(
    quotes: Sequence[QuoteObservation], side: ShadowSide, limit: Decimal
) -> Decimal | None:
    values = [
        quote.best_ask if side is ShadowSide.BUY else quote.best_bid
        for quote in quotes
    ]
    usable = [value for value in values if value is not None]
    return min((abs(value - limit) for value in usable), default=None)


def _matching_trades(
    trades: Sequence[TradeEvent],
    *,
    token_id: str,
    start: datetime,
    end: datetime,
) -> tuple[TradeEvent, ...]:
    return tuple(
        trade
        for trade in trades
        if trade.asset_id == token_id and start < _effective_trade_at(trade) <= end
    )


def _queue_progress(
    order: Mapping[str, Any],
    trades: Sequence[TradeEvent],
    *,
    strict_through: bool = False,
    reject_same_second_ambiguity: bool = False,
) -> dict[str, Any]:
    side = _order_side(order)
    limit = Decimal(str(order["limit_price"]))
    queue_remaining = (
        Decimal(str(order.get("better_level_shares") or "0"))
        + Decimal(str(order.get("volume_ahead") or "0"))
    )
    order_remaining = Decimal(str(order.get("remaining_shares") or order["requested_shares"]))
    eligible_side = ShadowSide.SELL if side is ShadowSide.BUY else ShadowSide.BUY
    ordered = sorted(
        trades,
        key=lambda row: (_effective_trade_at(row), row.timestamp, row.sequence is None, row.sequence or 0),
    )
    eligible_volume = ZERO
    strict_volume = ZERO
    filled_shares = ZERO
    ambiguity_count = 0
    index = 0
    while index < len(ordered):
        effective_at = _effective_trade_at(ordered[index])
        group: list[TradeEvent] = []
        while index < len(ordered) and _effective_trade_at(ordered[index]) == effective_at:
            group.append(ordered[index])
            index += 1
        ambiguous = len(group) > 1 and any(row.sequence is None for row in group)
        if ambiguous:
            ambiguity_count += 1
        for trade in group:
            price_ok = trade.price <= limit if side is ShadowSide.BUY else trade.price >= limit
            strict_ok = trade.price < limit if side is ShadowSide.BUY else trade.price > limit
            if trade.side is not eligible_side or not price_ok:
                continue
            eligible_volume += trade.size
            if strict_ok:
                strict_volume += trade.size
            if strict_through and not strict_ok:
                continue
            if reject_same_second_ambiguity and ambiguous:
                continue
            consumed = min(queue_remaining, trade.size)
            queue_remaining -= consumed
            available = trade.size - consumed
            if available > ZERO and order_remaining > ZERO:
                taken = min(available, order_remaining)
                filled_shares += taken
                order_remaining -= taken
    return {
        "eligible_opposite_trade_volume": eligible_volume,
        "strict_trade_through_volume": strict_volume,
        "queue_remaining_shares": queue_remaining,
        "modelled_available_shares": filled_shares,
        "queue_full_fill_possible": order_remaining <= ZERO,
        "same_second_ambiguity_count": ambiguity_count,
    }


def _tape_known_for_lifecycle(
    index: L2ChurnIndex | None,
    *,
    token_id: str,
    start: datetime,
    end: datetime,
) -> tuple[bool, str]:
    if index is None:
        return True, "not_requested"
    statuses = index.status_by_asset.get(token_id, ())
    if not statuses:
        return False, "no_token_l2_status"
    timestamps = index.status_times_by_asset.get(token_id, ())
    start_position = bisect_right(timestamps, start) - 1
    if start_position < 0 or not statuses[start_position][1]:
        return False, statuses[start_position][2] if start_position >= 0 else "before_first_l2_status"
    for at, known, reason in statuses[start_position + 1 :]:
        if at > end:
            break
        if not known:
            return False, reason
    return True, "continuous_l2_status"


def _counterfactual_touch(
    *,
    quotes: Sequence[QuoteObservation],
    order: Mapping[str, Any],
    cutoff: datetime | None,
) -> dict[str, Any]:
    submitted = _utc(order["submitted_at"])
    side = _order_side(order)
    limit = Decimal(str(order["limit_price"]))
    output: dict[str, Any] = {}
    for minutes in COUNTERFACTUAL_MINUTES:
        end = submitted + timedelta(minutes=minutes)
        if cutoff is not None:
            end = min(end, cutoff)
        eligible = [
            quote
            for quote in quotes
            if submitted < quote.timestamp <= end and quote.healthy
        ]
        touched = [quote for quote in eligible if _quote_touch(quote, side, limit)]
        output[str(minutes)] = {
            "horizon_end": end,
            "healthy_book_count": len(eligible),
            "touch_observed": bool(touched),
            "first_touch_at": touched[0].timestamp if touched else None,
            "counterfactual_only": True,
            "does_not_extend_live_timeout": True,
        }
    return output


def audit_order_path(
    order: Mapping[str, Any],
    *,
    quotes: Sequence[QuoteObservation],
    trades: Sequence[TradeEvent] = (),
    rule_provenance: RuleProvenance | None = None,
    tape_known: bool = True,
    tape_status_reason: str = "not_requested",
    cutoff: datetime | None = None,
    cross_bucket_taker_volume: Decimal = ZERO,
) -> dict[str, Any]:
    """Explain one historical zero-fill order without inventing a fill."""
    submitted = _utc(order["submitted_at"])
    ended = _order_end(order)
    side = _order_side(order)
    limit = Decimal(str(order["limit_price"]))
    requested = Decimal(str(order["requested_shares"]))
    rule = rule_provenance or _rule_at(quotes, submitted)
    sorted_quotes = tuple(sorted(quotes, key=lambda row: row.timestamp))
    prior = [quote for quote in sorted_quotes if quote.timestamp <= submitted]
    submit_quote = prior[-1] if prior else None
    active_quotes = [
        quote
        for quote in sorted_quotes
        if quote.is_book_observation and submitted < quote.timestamp <= ended
    ]
    healthy_quotes = [quote for quote in active_quotes if quote.healthy]
    touches = [quote for quote in healthy_quotes if _quote_touch(quote, side, limit)]
    later_end = ended + timedelta(minutes=30)
    if cutoff is not None:
        later_end = min(later_end, cutoff)
    post_cancel_quotes = [
        quote
        for quote in sorted_quotes
        if quote.is_book_observation
        and ended < quote.timestamp <= later_end
        and quote.healthy
    ]
    post_cancel_touches = [
        quote for quote in post_cancel_quotes if _quote_touch(quote, side, limit)]
    active_trades = _matching_trades(
        tuple(trades), token_id=str(order["token_id"]), start=submitted, end=ended
    )
    queue = _queue_progress(order, active_trades)
    conservative_queue = _queue_progress(
        order,
        active_trades,
        strict_through=True,
        reject_same_second_ambiguity=True,
    )
    lifetime_seconds = max(0.0, (ended - submitted).total_seconds())
    if not rule.valid:
        primary_reason = INVALID_RULE_PROVENANCE
    elif not healthy_quotes:
        primary_reason = NO_LATER_HEALTHY_BOOK
    elif touches and not tape_known:
        primary_reason = UNKNOWN_TAPE_GAP
    elif touches and queue["eligible_opposite_trade_volume"] <= ZERO:
        primary_reason = TRUE_ZERO_OPPOSITE_TRADE
    elif touches and not queue["queue_full_fill_possible"]:
        primary_reason = TOUCHED_QUEUE_NOT_CLEARED
    elif not touches and post_cancel_touches and order.get("cancelled_at") is not None:
        primary_reason = CANCELLED_BEFORE_LATER_TOUCH
    elif not touches:
        primary_reason = NEVER_TOUCHED
    else:
        primary_reason = OTHER_WITH_EVIDENCE
    mechanical_reason = (
        NO_LATER_HEALTHY_BOOK
        if not healthy_quotes
        else UNKNOWN_TAPE_GAP
        if touches and not tape_known
        else TRUE_ZERO_OPPOSITE_TRADE
        if touches and queue["eligible_opposite_trade_volume"] <= ZERO
        else TOUCHED_QUEUE_NOT_CLEARED
        if touches and not queue["queue_full_fill_possible"]
        else CANCELLED_BEFORE_LATER_TOUCH
        if not touches and post_cancel_touches and order.get("cancelled_at") is not None
        else NEVER_TOUCHED
        if not touches
        else OTHER_WITH_EVIDENCE
    )
    executable = quote = None
    if submit_quote is not None:
        executable = submit_quote.best_ask if side is ShadowSide.BUY else submit_quote.best_bid
        quote = submit_quote.best_bid if side is ShadowSide.BUY else submit_quote.best_ask
    post_bids = [quote.best_bid for quote in post_cancel_quotes if quote.best_bid is not None]
    post_asks = [quote.best_ask for quote in post_cancel_quotes if quote.best_ask is not None]
    healthy_bids = [quote.best_bid for quote in healthy_quotes if quote.best_bid is not None]
    healthy_asks = [quote.best_ask for quote in healthy_quotes if quote.best_ask is not None]
    return {
        "order_id": str(order["order_id"]),
        "threshold_tier": str(order.get("threshold_tier") or order.get("_tier") or ""),
        "scope": {
            "event_id": order.get("event_id"),
            "market_id": order.get("market_id"),
            "token_id": order.get("token_id"),
            "station_id": order.get("station_id"),
            "market_day": order.get("market_day"),
        },
        "submitted_at": submitted,
        "lifecycle_end_at": ended,
        "lifecycle_seconds": lifetime_seconds,
        "window_short_context_under_five_minutes": lifetime_seconds < 5 * 60,
        "state": order.get("state"),
        "cancel_reason": order.get("cancel_reason"),
        "side": side.value,
        "limit_price": limit,
        "requested_shares": requested,
        "requested_usd": _decimal(order.get("requested_usd")),
        "legacy_recorded_rules": {
            "tick_size": order.get("tick_size"),
            "min_order_size": order.get("min_order_size"),
            "treated_as_historical_provenance": False,
        },
        "rule_provenance": rule.as_dict(limit_price=limit),
        "submit_book": {
            "snapshot_at": submit_quote.timestamp if submit_quote else None,
            "best_bid": submit_quote.best_bid if submit_quote else None,
            "best_ask": submit_quote.best_ask if submit_quote else None,
            "spread": (
                submit_quote.best_ask - submit_quote.best_bid
                if submit_quote is not None
                and submit_quote.best_bid is not None
                and submit_quote.best_ask is not None
                else None
            ),
            "same_side_quote": quote,
            "opposite_executable_quote": executable,
        },
        "queue_at_submission": {
            "better_level_shares": _decimal(order.get("better_level_shares")),
            "volume_ahead": _decimal(order.get("volume_ahead")),
            "initial_queue_ahead": (
                _decimal(order.get("better_level_shares")) or ZERO
            )
            + (_decimal(order.get("volume_ahead")) or ZERO),
        },
        "book_path": {
            "later_book_count": len(active_quotes),
            "later_healthy_book_count": len(healthy_quotes),
            "later_best_bid_low": min(healthy_bids) if healthy_bids else None,
            "later_best_bid_high": max(healthy_bids) if healthy_bids else None,
            "later_best_ask_low": min(healthy_asks) if healthy_asks else None,
            "later_best_ask_high": max(healthy_asks) if healthy_asks else None,
            "nearest_distance_to_limit": _nearest_distance(healthy_quotes, side, limit),
            "touch_observed": bool(touches),
            "touch_observation_count": len(touches),
            "first_touch_at": touches[0].timestamp if touches else None,
            "last_touch_at": touches[-1].timestamp if touches else None,
            "observed_touch_span_seconds": (
                max(0.0, (touches[-1].timestamp - touches[0].timestamp).total_seconds())
                if len(touches) > 1
                else 0.0
                if touches
                else None
            ),
        },
        "tape_path": {
            "status": "OK" if tape_known else UNKNOWN_TAPE_GAP,
            "status_reason": tape_status_reason,
            "same_bucket_canonical_taker_volume": sum(
                (trade.size for trade in active_trades if trade.side is (ShadowSide.SELL if side is ShadowSide.BUY else ShadowSide.BUY)),
                start=ZERO,
            ),
            "same_bucket_eligible_taker_volume": queue["eligible_opposite_trade_volume"],
            "cross_bucket_canonical_taker_volume": cross_bucket_taker_volume,
            "same_second_ambiguity_count": queue["same_second_ambiguity_count"],
        },
        "queue_path": queue,
        "conservative_trade_through_path": conservative_queue,
        "post_cancel_30m": {
            "horizon_end": later_end,
            "healthy_book_count": len(post_cancel_quotes),
            "touch_observed": bool(post_cancel_touches),
            "first_touch_at": post_cancel_touches[0].timestamp if post_cancel_touches else None,
            "best_bid_low": min(post_bids) if post_bids else None,
            "best_ask_high": max(post_asks) if post_asks else None,
            "adverse_selection_indicator_only": (
                min(post_bids) - limit if post_bids else None
                if side is ShadowSide.BUY
                else limit - max(post_asks)
                if post_asks
                else None
            ),
            "not_pnl_or_fill_claim": True,
        },
        "counterfactual_touch_only": _counterfactual_touch(
            quotes=sorted_quotes, order=order, cutoff=cutoff
        ),
        "upper_bound_eligibility": {
            "touch_upper_bound": rule.valid and bool(touches),
            "queue_upper_bound": (
                rule.valid
                and bool(touches)
                and tape_known
                and bool(queue["queue_full_fill_possible"])
            ),
            "conservative_fill": (
                rule.valid
                and bool(touches)
                and tape_known
                and bool(conservative_queue["queue_full_fill_possible"])
            ),
        },
        "primary_reason": primary_reason,
        "mechanical_reason": mechanical_reason,
        "execution_enabled": False,
        "lookahead_used_for_decision": False,
    }


def _clustered_bound(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    eligible = [row for row in rows if row.get("rule_provenance", {}).get("status") == "VALID_ARCHIVED"]
    success = [row for row in eligible if row.get("upper_bound_eligibility", {}).get(key)]
    raw_interval = wilson_interval(len(success), len(eligible))
    clusters: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in eligible:
        scope = row.get("scope") or {}
        clusters[(str(scope.get("station_id") or ""), str(scope.get("market_day") or ""))].append(row)
    cluster_successes = sum(
        any(item.get("upper_bound_eligibility", {}).get(key) for item in values)
        for values in clusters.values()
    )
    cluster_interval = wilson_interval(cluster_successes, len(clusters))
    return {
        "order_numerator": len(success),
        "order_denominator": len(eligible),
        "order_rate": len(success) / len(eligible) if eligible else None,
        "order_wilson_95": raw_interval,
        "station_day_numerator": cluster_successes,
        "station_day_denominator": len(clusters),
        "station_day_rate": cluster_successes / len(clusters) if clusters else None,
        "station_day_wilson_95": cluster_interval,
        "statistically_unreliable": len(clusters) < 30,
        "independent_unit": "station_id + market_day",
        "N_A_reason": (
            "no orders have contemporaneous valid tick and min-order provenance"
            if not eligible
            else None
        ),
    }


def _small_size_sensitivity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for size in SMALL_SIZE_USD:
        eligible: list[dict[str, Any]] = []
        min_rejected = 0
        for row in rows:
            rules = row.get("rule_provenance") or {}
            limit = _decimal(row.get("limit_price"))
            minimum = _decimal(rules.get("min_order_size"))
            if limit is None or limit <= ZERO or minimum is None or minimum <= ZERO:
                continue
            shares = size / limit
            if shares < minimum:
                min_rejected += 1
                continue
            # This is a fixed historical upper-bound calculation, not a new
            # order replay. Queue capacity must nevertheless be compared to
            # the declared small-size share count rather than the saved $20
            # order's share count.
            queue = row.get("queue_path") or {}
            conservative = row.get("conservative_trade_through_path") or {}
            observed_touch = bool((row.get("book_path") or {}).get("touch_observed"))
            tape_known = (row.get("tape_path") or {}).get("status") == "OK"
            queue_available = _decimal(queue.get("modelled_available_shares")) or ZERO
            conservative_available = (
                _decimal(conservative.get("modelled_available_shares")) or ZERO
            )
            eligibility = {
                "touch_upper_bound": bool(rules.get("status") == "VALID_ARCHIVED")
                and observed_touch,
                "queue_upper_bound": bool(rules.get("status") == "VALID_ARCHIVED")
                and observed_touch
                and tape_known
                and queue_available >= shares,
                "conservative_fill": bool(rules.get("status") == "VALID_ARCHIVED")
                and observed_touch
                and tape_known
                and conservative_available >= shares,
            }
            eligible.append(
                {
                    **dict(row),
                    "requested_shares_for_sensitivity": shares,
                    "upper_bound_eligibility": eligibility,
                }
            )
        output[str(size)] = {
            "quote_size_usd": size,
            "valid_rule_order_count": len(eligible),
            "below_min_order_size_count": min_rejected,
            "touch_upper_bound": _clustered_bound(eligible, "touch_upper_bound"),
            "queue_upper_bound": _clustered_bound(eligible, "queue_upper_bound"),
            "conservative_fill": _clustered_bound(eligible, "conservative_fill"),
            "sensitivity_only": True,
            "not_a_parameter_search_or_replay": True,
        }
    return output


def cross_bucket_coverage_sensitivity(
    pair_factory: Callable[[], Iterable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Measure archive coverage at fixed sync tolerances without changing policy."""
    output: dict[str, Any] = {}
    for minutes in CROSS_BUCKET_TOLERANCE_MINUTES:
        index: CrossBucketMassIndex = build_cross_bucket_mass_index(
            lambda: iter(pair_factory()),
            synchronization_tolerance=timedelta(minutes=minutes),
        )
        summary = index.summary()
        counts = summary.get("status_counts") or {}
        total = int(summary.get("observation_count") or 0)
        known = int(counts.get("OK") or 0)
        values = tuple(index.values.values())
        interval_widths = [
            float(value.upper - value.lower)
            for value in values
            if value.lower is not None and value.upper is not None
        ]
        maximum_ages = [
            float(value.maximum_age_seconds)
            for value in values
            if value.maximum_age_seconds is not None
        ]
        missing_bucket = sum(
            value.observed_bucket_count < value.expected_bucket_count
            for value in values
        )
        aged_out = sum(
            value.observed_bucket_count == value.expected_bucket_count
            and value.status != "OK"
            and value.maximum_age_seconds is not None
            and value.maximum_age_seconds > minutes * 60
            for value in values
        )
        output[str(minutes)] = {
            **summary,
            "known_count": known,
            "known_fraction": known / total if total else None,
            "missing_bucket_checkpoint_count": missing_bucket,
            "complete_but_unsynchronized_count": aged_out,
            "mass_interval_width": _summary(interval_widths),
            "maximum_age_seconds": _summary(maximum_ages),
            "diagnostic_only": True,
            "does_not_change_quiet_thresholds": True,
        }
    return output


def collect_archived_order_quotes(
    orders: Sequence[Mapping[str, Any]],
    checkpoint_paths: Sequence[Path],
    *,
    cutoff: datetime | None = None,
) -> tuple[
    dict[str, tuple[QuoteObservation, ...]],
    dict[str, str],
    dict[str, Any],
]:
    """Extract only same-token archival evidence needed for the saved cohort."""
    if not orders:
        return {}, {}, {"selected_file_count": 0, "duplicate_archive_file_count": 0}
    starts = [_utc(order["submitted_at"]) for order in orders]
    ends = [_order_end(order) + timedelta(minutes=max(COUNTERFACTUAL_MINUTES)) for order in orders]
    lower = min(starts) - timedelta(days=1)
    upper = min(max(ends), cutoff) if cutoff is not None else max(ends)
    selected, duplicates = _prefer_retention_archive_paths(checkpoint_paths)
    wanted_tokens = {str(order["token_id"]) for order in orders}
    wanted_events = {str(order.get("event_id") or "") for order in orders}
    quotes: dict[str, list[QuoteObservation]] = defaultdict(list)
    asset_event: dict[str, str] = {}
    quality_windows = load_quality_windows(
        selected[0].parents[3] / "runtime" / "polymarket_quality_windows.json"
    ) if selected else ()
    raw_rows = eligible_rows = 0
    for path in selected:
        try:
            with open_jsonl_text(path) as handle:
                for line in handle:
                    try:
                        source = json.loads(line)
                        timestamp = _utc(source["received_at"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if timestamp < lower or timestamp > upper:
                        continue
                    raw_rows += 1
                    asset_id = str(source.get("asset_id") or "")
                    event_id = _event_slug(source.get("market_slug"))
                    if event_id in wanted_events and asset_id:
                        asset_event.setdefault(asset_id, event_id)
                    if asset_id not in wanted_tokens:
                        continue
                    raw = source.get("raw") if isinstance(source.get("raw"), Mapping) else {}
                    tick_value = _decimal(source.get("tick_size") or raw.get("tick_size"))
                    min_value = _decimal(
                        source.get("min_order_size")
                        or raw.get("min_order_size")
                        or raw.get("orderMinSize")
                    )
                    bid = _best(source.get("bids"), bids=True)
                    ask = _best(source.get("asks"), bids=False)
                    healthy = bool(
                        source.get("book_complete")
                        and bid is not None
                        and ask is not None
                        and market_record_is_analysis_eligible(source, timestamp, quality_windows)
                    )
                    if healthy:
                        eligible_rows += 1
                    quotes[asset_id].append(
                        QuoteObservation(
                            timestamp=timestamp,
                            event_id=event_id,
                            market_id=_market_slug(source.get("market_slug")),
                            token_id=asset_id,
                            best_bid=bid,
                            best_ask=ask,
                            healthy=healthy,
                            tick_size=tick_value,
                            min_order_size=min_value,
                            tick_source=(
                                "checkpoint_top_level"
                                if source.get("tick_size") is not None
                                else "websocket_raw_book"
                                if raw.get("tick_size") is not None
                                else "UNKNOWN"
                            ),
                            min_order_size_source=(
                                "checkpoint_top_level"
                                if source.get("min_order_size") is not None
                                else "websocket_raw_book"
                                if raw.get("min_order_size") is not None
                                or raw.get("orderMinSize") is not None
                                else "UNKNOWN"
                            ),
                        )
                    )
        except OSError:
            continue
    # Gamma event responses are archived public market metadata. A record is
    # rule provenance only when its receipt predates the cohort's latest
    # submission; it is never quote, depth, or fill evidence. Current Gamma
    # state is intentionally not queried here.
    gamma_root = (
        selected[0].parents[3] / "raw" / "polymarket_gamma_event" if selected else None
    )
    gamma_paths: tuple[Path, ...] = ()
    gamma_duplicates = 0
    if gamma_root is not None and gamma_root.exists():
        gamma_paths, gamma_duplicates = _prefer_retention_archive_paths(
            path
            for path in gamma_root.rglob("events.jsonl*")
            if path.name in {"events.jsonl", "events.jsonl.gz"}
        )
    gamma_rule_records = 0
    gamma_rule_tokens: set[str] = set()

    def token_ids(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return ()
        else:
            parsed = value
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item) for item in parsed if item is not None)

    for path in gamma_paths:
        try:
            with open_jsonl_text(path) as handle:
                for line in handle:
                    try:
                        source = json.loads(line)
                        observed_at = _utc(source.get("fetched_at") or source["received_at"])
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if observed_at > max(starts):
                        continue
                    payload = source.get("payload")
                    market_rows = (
                        payload.get("markets", ()) if isinstance(payload, Mapping) else ()
                    )
                    for market in market_rows:
                        if not isinstance(market, Mapping):
                            continue
                        matched_tokens = wanted_tokens.intersection(
                            token_ids(market.get("clobTokenIds"))
                        )
                        if not matched_tokens:
                            continue
                        tick_value = _decimal(market.get("orderPriceMinTickSize"))
                        min_value = _decimal(market.get("orderMinSize"))
                        for asset_id in matched_tokens:
                            gamma_rule_records += 1
                            gamma_rule_tokens.add(asset_id)
                            quotes[asset_id].append(
                                QuoteObservation(
                                    timestamp=observed_at,
                                    event_id=_event_slug(market.get("slug")),
                                    market_id=str(market.get("slug") or "") or None,
                                    token_id=asset_id,
                                    best_bid=None,
                                    best_ask=None,
                                    healthy=False,
                                    is_book_observation=False,
                                    tick_size=tick_value,
                                    min_order_size=min_value,
                                    tick_source="gamma_event_archived_market_metadata",
                                    min_order_size_source="gamma_event_archived_market_metadata",
                                )
                            )
        except OSError:
            continue
    return (
        {token: tuple(sorted(rows, key=lambda row: row.timestamp)) for token, rows in quotes.items()},
        asset_event,
        {
            "selected_file_count": len(selected),
            "duplicate_archive_file_count": duplicates,
            "raw_candidate_row_count": raw_rows,
            "healthy_quote_row_count": eligible_rows,
            "gamma_rule_file_count": len(gamma_paths),
            "gamma_rule_duplicate_archive_file_count": gamma_duplicates,
            "gamma_rule_record_count": gamma_rule_records,
            "gamma_rule_token_count": len(gamma_rule_tokens),
            "cutoff": upper,
            "current_rule_backfill_used": False,
        },
    )


def audit_quiet_order_forensics(
    quiet_result: Mapping[str, Any],
    *,
    checkpoint_paths: Sequence[Path],
    trades: Iterable[TradeEvent] = (),
    l2_churn_index: L2ChurnIndex | None = None,
    lifecycle_collector: QuietLifecycleCollector | None = None,
    cross_bucket_sensitivity: Mapping[str, Any] | None = None,
    cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Audit saved v2 order records against raw token-native evidence."""
    orders: list[dict[str, Any]] = []
    for tier, model in (quiet_result.get("models") or {}).items():
        if not isinstance(model, Mapping):
            continue
        for value in model.get("orders") or ():
            if isinstance(value, Mapping):
                orders.append({**dict(value), "_tier": str(tier), "threshold_tier": str(tier)})
    analysis_cutoff = cutoff or _utc(quiet_result.get("analysis_cutoff") or datetime.now(UTC))
    quotes_by_token, asset_event, archive = collect_archived_order_quotes(
        orders, checkpoint_paths, cutoff=analysis_cutoff
    )
    all_trades = tuple(sorted(trades, key=_effective_trade_at))
    rows: list[dict[str, Any]] = []
    for order in orders:
        token = str(order["token_id"])
        submitted = _utc(order["submitted_at"])
        ended = _order_end(order)
        rule = _rule_at(quotes_by_token.get(token, ()), submitted)
        tape_known, tape_reason = _tape_known_for_lifecycle(
            l2_churn_index, token_id=token, start=submitted, end=ended
        )
        event_id = str(order.get("event_id") or "")
        cross_volume = sum(
            (
                trade.size
                for trade in all_trades
                if (
                    submitted < _effective_trade_at(trade) <= ended
                    and trade.asset_id != token
                    and asset_event.get(trade.asset_id) == event_id
                )
            ),
            start=ZERO,
        )
        row = audit_order_path(
            order,
            quotes=quotes_by_token.get(token, ()),
            trades=all_trades,
            rule_provenance=rule,
            tape_known=tape_known,
            tape_status_reason=tape_reason,
            cutoff=analysis_cutoff,
            cross_bucket_taker_volume=cross_volume,
        )
        if lifecycle_collector is not None:
            row["quiet_lifecycle"] = lifecycle_collector.order_context(order)
        rows.append(row)
    primary = Counter(str(row["primary_reason"]) for row in rows)
    mechanical = Counter(str(row["mechanical_reason"]) for row in rows)
    bounds = {
        "TOUCH_UPPER_BOUND": _clustered_bound(rows, "touch_upper_bound"),
        "QUEUE_UPPER_BOUND": _clustered_bound(rows, "queue_upper_bound"),
        "CONSERVATIVE_FILL": _clustered_bound(rows, "conservative_fill"),
    }
    lifecycle_summary = lifecycle_collector.summary() if lifecycle_collector else None
    if lifecycle_summary is not None:
        exposures = [float(row["lifecycle_seconds"]) for row in rows]
        lifecycle_summary["order_exposure_seconds"] = _summary(exposures)
        lifecycle_summary["short_order_window_count"] = sum(
            bool(row.get("window_short_context_under_five_minutes")) for row in rows
        )
        lifecycle_summary["order_count_with_reconstructed_episode"] = sum(
            bool((row.get("quiet_lifecycle") or {}).get("quiet_start_at"))
            for row in rows
        )
    return {
        "forensic_version": FORENSIC_VERSION,
        "source_strategy_version": quiet_result.get("strategy_version"),
        "source_analysis_cutoff": analysis_cutoff,
        "execution_enabled": False,
        "analysis_mode": "read_only_saved_v2_order_audit",
        "order_count": len(rows),
        "primary_reason_counts": dict(sorted(primary.items())),
        "mechanical_reason_counts": dict(sorted(mechanical.items())),
        "archive": archive,
        "upper_bounds": bounds,
        "small_size_sensitivity": _small_size_sensitivity(rows),
        "cross_bucket_coverage_sensitivity": dict(cross_bucket_sensitivity or {}),
        "quiet_lifecycle": lifecycle_summary,
        "orders": rows,
        "interpretation": {
            "touch_is_only_an_optimistic_upper_bound": True,
            "queue_uses_only_same_token_opposite_taker_trades": True,
            "l2_removals_are_not_fills": True,
            "current_rules_never_backfill_historical_orders": True,
            "counterfactual_timeouts_are_diagnostic_only": True,
            "no_threshold_or_timeout_relaxation_used": True,
            "no_parameter_search": True,
        },
    }


def render_quiet_order_forensics_report(result: Mapping[str, Any], output_path: Path | str) -> None:
    """Write a compact human-readable companion to the full JSON evidence."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bounds = result.get("upper_bounds") or {}
    lifecycle = result.get("quiet_lifecycle") or {}
    lines = [
        "# QUIET v2 zero-fill order forensics and executable upper bounds",
        "",
        "This is a read-only retrospective audit of the saved v2 cohort. It never sends an order, uses no current rule as historical evidence, and does not relax a stability threshold or extend a timeout.",
        "",
        f"- Forensic version: `{result.get('forensic_version')}`",
        f"- Source analysis cutoff: `{result.get('source_analysis_cutoff')}`",
        f"- Audited saved orders: `{result.get('order_count')}`",
        f"- Execution enabled: `{result.get('execution_enabled')}`",
        "",
        "## Primary audit outcome",
        "",
        "| Primary reason | Orders |",
        "|---|---:|",
    ]
    for reason, count in sorted((result.get("primary_reason_counts") or {}).items()):
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            "The primary reason uses the declared fail-closed priority. The mechanical reason below remains visible so missing min-order provenance does not hide whether a quote ever touched or a queue could have cleared.",
            "",
            "| Mechanical path reason | Orders |",
            "|---|---:|",
        ]
    )
    for reason, count in sorted((result.get("mechanical_reason_counts") or {}).items()):
        lines.append(f"| {reason} | {count} |")
    lines.extend(
        [
            "",
            "## Three executable upper bounds",
            "",
            "TOUCH is an optimistic quote-path upper bound. QUEUE additionally requires recorded same-token opposite taker volume to clear the saved queue model. CONSERVATIVE additionally requires strict trade-through and rejects ambiguous same-second tape ordering.",
            "",
            "| Layer | Orders (success / valid) | Order Wilson 95% | Station-days (success / valid) | Cluster Wilson 95% |",
            "|---|---|---|---|---|",
        ]
    )
    for name, value in bounds.items():
        if not isinstance(value, Mapping):
            continue
        lines.append(
            "| {name} | {order_success}/{orders} ({order_rate}) | {order_interval} | {cluster_success}/{clusters} ({cluster_rate}) | {cluster_interval} |".format(
                name=name,
                order_success=value.get("order_numerator"),
                orders=value.get("order_denominator"),
                order_rate=value.get("order_rate"),
                order_interval=value.get("order_wilson_95"),
                cluster_success=value.get("station_day_numerator"),
                clusters=value.get("station_day_denominator"),
                cluster_rate=value.get("station_day_rate"),
                cluster_interval=value.get("station_day_wilson_95"),
            )
        )
    lines.extend(
        [
            "",
            "## QUIET lifecycle evidence",
            "",
            f"- QUIET episodes observed: `{lifecycle.get('episode_count')}`",
            f"- Duration seconds (p50 / p90): `{(lifecycle.get('duration_seconds') or {}).get('p50')}` / `{(lifecycle.get('duration_seconds') or {}).get('p90')}`",
            f"- Book observations per episode (p50 / p90): `{(lifecycle.get('book_observations_per_episode') or {}).get('p50')}` / `{(lifecycle.get('book_observations_per_episode') or {}).get('p90')}`",
            f"- Saved-order exposure seconds (p50 / p90): `{(lifecycle.get('order_exposure_seconds') or {}).get('p50')}` / `{(lifecycle.get('order_exposure_seconds') or {}).get('p90')}`",
            f"- Saved orders with a reconstructed QUIET episode: `{lifecycle.get('order_count_with_reconstructed_episode')}`",
            f"- Saved orders shorter than five minutes: `{lifecycle.get('short_order_window_count')}`",
            f"- End-kind counts: `{lifecycle.get('end_kind_counts')}`",
            f"- End-reason counts: `{lifecycle.get('end_reason_counts')}`",
            f"- Coverage-flicker source counts: `{lifecycle.get('end_coverage_reason_counts')}`",
            f"- True-instability source counts: `{lifecycle.get('end_true_violation_counts')}`",
            "",
            "## Cross-bucket synchronization coverage",
            "",
            "| Sync tolerance minutes | Observations | Known fraction | Missing bucket checkpoint | Complete but aged out | Interval width p50/p90 |",
            "|---:|---:|---:|---:|---:|---|",
        ]
    )
    for minutes, value in sorted(
        (result.get("cross_bucket_coverage_sensitivity") or {}).items(), key=lambda row: int(row[0])
    ):
        interval = value.get("mass_interval_width") or {}
        lines.append(
            "| {minutes} | {count} | {known} | {missing} | {aged} | {p50} / {p90} |".format(
                minutes=minutes,
                count=value.get("observation_count"),
                known=value.get("known_fraction"),
                missing=value.get("missing_bucket_checkpoint_count"),
                aged=value.get("complete_but_unsynchronized_count"),
                p50=interval.get("p50"),
                p90=interval.get("p90"),
            )
        )
    lines.extend(
        [
            "",
            "## Per-order evidence",
            "",
            "| Tier | Station/day | Token | Submit → end minutes | Rules | Mechanical path | Touch | Eligible opposite trade volume | Queue remaining |",
            "|---|---|---|---:|---|---|---|---:|---:|",
        ]
    )
    for row in result.get("orders") or ():
        scope = row.get("scope") or {}
        rules = row.get("rule_provenance") or {}
        book = row.get("book_path") or {}
        tape = row.get("tape_path") or {}
        queue = row.get("queue_path") or {}
        lines.append(
            "| {tier} | {station}/{day} | `{token}` | {minutes:.2f} | {rules} | {reason} | {touch} | {volume} | {queue_remaining} |".format(
                tier=row.get("threshold_tier"),
                station=scope.get("station_id"),
                day=scope.get("market_day"),
                token=str(scope.get("token_id") or "")[:12],
                minutes=float(row.get("lifecycle_seconds") or 0) / 60,
                rules=rules.get("status"),
                reason=row.get("mechanical_reason"),
                touch=book.get("touch_observed"),
                volume=tape.get("same_bucket_eligible_taker_volume"),
                queue_remaining=queue.get("queue_remaining_shares"),
            )
        )
    lines.extend(
        [
            "",
            "Conclusion: this audit is a feasibility diagnosis, not a Champion/Challenger comparison and not an execution recommendation. This saved cohort has valid archived tick/min-order evidence; every upper-bound layer is zero because no live quote path touched the limit. The present constraint is therefore quote-path passivity, not an inferred queue or tape failure.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_quiet_order_forensics(result: Mapping[str, Any], output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_value(dict(result)), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CROSS_BUCKET_TOLERANCE_MINUTES",
    "FORENSIC_VERSION",
    "QuietLifecycleCollector",
    "QuoteObservation",
    "RuleProvenance",
    "annotate_episode_book_observations",
    "audit_order_path",
    "audit_quiet_order_forensics",
    "collect_archived_order_quotes",
    "cross_bucket_coverage_sensitivity",
    "render_quiet_order_forensics_report",
    "write_quiet_order_forensics",
]
