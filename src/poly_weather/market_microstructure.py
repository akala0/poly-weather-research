"""Receipt-safe microstructure diagnostics for the offline QUIET v2 replay.

The module intentionally produces regime-quality inputs only.  It never
creates an order, infers an executable quote from a midpoint, or treats an L2
size removal as proof that a shadow order filled.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from heapq import heapify, heappop
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.archive_io import open_jsonl_text
from poly_weather.shadow_orders import BookSnapshot, ShadowSide, TradeEvent

ZERO = Decimal("0")
ONE = Decimal("1")
# ``iter_paired_book_snapshots`` already enforces this same maximum age when
# it combines a YES and NO side.  Reusing the archive's declared pairing
# boundary for the *different-bucket* diagnostic avoids pretending that a
# sub-minute alignment exists in five-minute checkpoint data, without ever
# mixing a quote older than the source's own accepted side pairing.
CROSS_BUCKET_SYNC_TOLERANCE = timedelta(minutes=5)
# L2 prices in the archived CLOB are ticked at 0.001/0.01 today, but retain
# six decimal places so a future finer public tick fails closed rather than
# being rounded.  Sizes use nine fractional places for the same reason.  The
# compact integer representation prevents a multi-day full-depth replay from
# holding millions of Decimal/tuple objects in memory.
_L2_PRICE_SCALE = 1_000_000
_L2_SIZE_SCALE = 1_000_000_000
_L2_PRICE_DECIMAL_SCALE = Decimal(_L2_PRICE_SCALE)
_L2_SIZE_DECIMAL_SCALE = Decimal(_L2_SIZE_SCALE)

# Raw CLOB records are emitted with these envelope fields before the nested
# websocket payload.  The expressions are a prefilter only: a selected record
# is always decoded and validated as JSON below.  This lets a focused replay
# scan a tens-of-GiB archive without constructing Python objects for every
# unrelated subscribed token.
_RAW_ASSET_ID_RE = re.compile(r'"asset_id"\s*:\s*"([^"]+)"')
_RAW_RUN_ID_RE = re.compile(r'"run_id"\s*:\s*"([^"]+)"')
_RAW_RECEIVED_NS_RE = re.compile(r'"received_at_ns"\s*:\s*(\d+)')
_RAW_RECEIVED_AT_RE = re.compile(r'"received_at"\s*:\s*"([^"]+)"')


def _utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
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


def _l2_units(value: Decimal, scale: Decimal) -> int | None:
    scaled = value * scale
    integral = scaled.to_integral_value()
    if scaled != integral:
        return None
    return int(integral)


def _l2_decimal(units: int, scale: Decimal) -> Decimal:
    return Decimal(units) / scale


def _best(rows: Any, *, bids: bool) -> Decimal | None:
    values: list[Decimal] = []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        price = _decimal(row.get("price"))
        size = _decimal(row.get("size"))
        if price is not None and size is not None and size > ZERO:
            values.append(price)
    return (max(values) if bids else min(values)) if values else None


def _effective_trade_at(trade: TradeEvent) -> datetime:
    return max(trade.timestamp, trade.available_at or trade.timestamp)


@dataclass(frozen=True, slots=True)
class CrossBucketMassObservation:
    """One strictly contemporaneous YES-price interval across event buckets."""

    timestamp: datetime
    status: str
    lower: Decimal | None = None
    upper: Decimal | None = None
    error: Decimal | None = None
    observed_bucket_count: int = 0
    expected_bucket_count: int = 0
    maximum_age_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "lower": str(self.lower) if self.lower is not None else None,
            "upper": str(self.upper) if self.upper is not None else None,
            "error": str(self.error) if self.error is not None else None,
            "observed_bucket_count": self.observed_bucket_count,
            "expected_bucket_count": self.expected_bucket_count,
            "maximum_age_seconds": self.maximum_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class CrossBucketMassIndex:
    values: Mapping[tuple[str, datetime], CrossBucketMassObservation]
    synchronization_tolerance: timedelta
    expected_bucket_count_by_event: Mapping[str, int]

    def lookup(self, market_slug: str, timestamp: datetime) -> CrossBucketMassObservation:
        value = self.values.get((market_slug, timestamp))
        if value is not None:
            return value
        return CrossBucketMassObservation(
            timestamp=timestamp,
            status="UNKNOWN_CROSS_BUCKET_SYNC",
            expected_bucket_count=self.expected_bucket_count_by_event.get(
                market_slug.split("/", 1)[0], 0
            ),
        )

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for value in self.values.values():
            counts[value.status] += 1
        return {
            "synchronization_tolerance_seconds": self.synchronization_tolerance.total_seconds(),
            "observation_count": len(self.values),
            "status_counts": dict(sorted(counts.items())),
            "event_count": len(self.expected_bucket_count_by_event),
        }


def build_cross_bucket_mass_index(
    pair_factory: Callable[[], Iterator[Mapping[str, Any]]],
    *,
    synchronization_tolerance: timedelta = CROSS_BUCKET_SYNC_TOLERANCE,
) -> CrossBucketMassIndex:
    """Build a non-lookahead cross-bucket interval index from YES books only.

    The first pass learns the archived contract universe.  The second pass uses
    only the current bucket update and already-observed quotes, never a later
    bucket value, when assigning a mass interval to that update.
    """
    markets_by_event: dict[str, set[str]] = defaultdict(set)
    for pair in pair_factory():
        event_slug = str(pair.get("event_slug") or "")
        market_slug = str(pair.get("market_slug") or "")
        if event_slug and market_slug:
            markets_by_event[event_slug].add(market_slug)
    expected = {event: len(markets) for event, markets in markets_by_event.items()}
    latest: dict[str, dict[str, tuple[datetime, Decimal, Decimal]]] = defaultdict(dict)
    values: dict[tuple[str, datetime], CrossBucketMassObservation] = {}
    for pair in pair_factory():
        event_slug = str(pair.get("event_slug") or "")
        market_slug = str(pair.get("market_slug") or "")
        timestamp = pair.get("observed_at")
        if not event_slug or not market_slug or not isinstance(timestamp, datetime):
            continue
        timestamp = timestamp.astimezone(UTC)
        yes = pair.get("yes")
        bid = _best(yes.get("bids") if isinstance(yes, Mapping) else None, bids=True)
        ask = _best(yes.get("asks") if isinstance(yes, Mapping) else None, bids=False)
        if bid is not None and ask is not None and bid <= ask:
            latest[event_slug][market_slug] = (timestamp, bid, ask)
        expected_markets = markets_by_event.get(event_slug, set())
        current = latest.get(event_slug, {})
        rows = [current[market] for market in expected_markets if market in current]
        ages = [(timestamp - row[0]).total_seconds() for row in rows]
        synchronized = (
            bool(expected_markets)
            and len(rows) == len(expected_markets)
            and all(0 <= age <= synchronization_tolerance.total_seconds() for age in ages)
        )
        if synchronized:
            lower = sum((row[1] for row in rows), start=ZERO)
            upper = sum((row[2] for row in rows), start=ZERO)
            error = ZERO if lower <= ONE <= upper else min(abs(ONE - lower), abs(upper - ONE))
            values[(market_slug, timestamp)] = CrossBucketMassObservation(
                timestamp=timestamp,
                status="OK",
                lower=lower,
                upper=upper,
                error=error,
                observed_bucket_count=len(rows),
                expected_bucket_count=len(expected_markets),
                maximum_age_seconds=max(ages, default=0.0),
            )
        else:
            lower = sum((row[1] for row in rows), start=ZERO) if rows else None
            upper = sum((row[2] for row in rows), start=ZERO) if rows else None
            values[(market_slug, timestamp)] = CrossBucketMassObservation(
                timestamp=timestamp,
                status="UNKNOWN_CROSS_BUCKET_SYNC",
                lower=lower,
                upper=upper,
                observed_bucket_count=len(rows),
                expected_bucket_count=len(expected_markets),
                maximum_age_seconds=max(ages) if ages else None,
            )
    return CrossBucketMassIndex(
        values=values,
        synchronization_tolerance=synchronization_tolerance,
        expected_bucket_count_by_event=expected,
    )


@dataclass(frozen=True, slots=True)
class L2ChurnObservation:
    timestamp: datetime
    added: Decimal
    removed: Decimal
    traded: Decimal
    cancelled_at_level: Decimal


@dataclass(frozen=True, slots=True)
class L2ChurnIndex:
    """Level-delta diagnostics; no removal is ever converted to a fill."""

    observations_by_asset: Mapping[str, tuple[L2ChurnObservation, ...]]
    status_by_asset: Mapping[str, tuple[tuple[datetime, bool, str], ...]]
    observation_times_by_asset: Mapping[str, tuple[datetime, ...]]
    observation_prefix_by_asset: Mapping[
        str, tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...]
    ]
    status_times_by_asset: Mapping[str, tuple[datetime, ...]]
    snapshot_windows_by_asset: Mapping[
        str, Mapping[datetime, L2ChurnObservation]
    ]
    snapshot_statuses_by_asset: Mapping[str, Mapping[datetime, str]]
    window: timedelta
    raw_event_count: int
    price_change_count: int
    reconnect_or_gap_count: int
    source_file_count: int
    duplicate_archive_file_count: int
    sparse_snapshot_index: bool
    snapshot_window_observation_count: int

    def lookup(self, asset_id: str, timestamp: datetime) -> dict[str, Any]:
        point = timestamp.astimezone(UTC)
        if asset_id in self.snapshot_windows_by_asset:
            value = self.snapshot_windows_by_asset[asset_id].get(point)
            if value is None:
                return {
                    "churn_rate": None,
                    "metric_status": {"churn_rate": "UNKNOWN_TAPE_GAP"},
                    "l2_added": None,
                    "l2_removed": None,
                    "l2_traded": None,
                    "l2_cancelled_at_level": None,
                    "l2_status_reason": self.snapshot_statuses_by_asset
                    .get(asset_id, {})
                    .get(point, "snapshot_not_l2_indexed"),
                }
            added = value.added
            removed = value.removed
            traded = value.traded
            cancelled = value.cancelled_at_level
            denominator = added + removed
            churn_rate = cancelled / denominator if denominator > ZERO else ZERO
            return {
                "churn_rate": churn_rate,
                "metric_status": {"churn_rate": "OK"},
                "l2_added": added,
                "l2_removed": removed,
                "l2_traded": traded,
                "l2_cancelled_at_level": cancelled,
                "l2_status_reason": self.snapshot_statuses_by_asset
                .get(asset_id, {})
                .get(point, "book_snapshot"),
            }
        statuses = self.status_by_asset.get(asset_id, ())
        status = "UNKNOWN_TAPE_GAP"
        known = False
        if statuses:
            index = bisect_right(self.status_times_by_asset.get(asset_id, ()), point) - 1
            if index >= 0:
                _at, known, status = statuses[index]
        if not known:
            return {
                "churn_rate": None,
                "metric_status": {"churn_rate": "UNKNOWN_TAPE_GAP"},
                "l2_added": None,
                "l2_removed": None,
                "l2_traded": None,
                "l2_cancelled_at_level": None,
                "l2_status_reason": status,
            }
        times = self.observation_times_by_asset.get(asset_id, ())
        prefix = self.observation_prefix_by_asset.get(asset_id, ())
        start = point - self.window
        start_index = bisect_right(times, start)
        end_index = bisect_right(times, point)
        if not prefix:
            added = removed = traded = cancelled = ZERO
        else:
            left = prefix[start_index]
            right = prefix[end_index]
            added = right[0] - left[0]
            removed = right[1] - left[1]
            traded = right[2] - left[2]
            cancelled = right[3] - left[3]
        denominator = added + removed
        churn_rate = cancelled / denominator if denominator > ZERO else ZERO
        return {
            "churn_rate": churn_rate,
            "metric_status": {"churn_rate": "OK"},
            "l2_added": added,
            "l2_removed": removed,
            "l2_traded": traded,
            "l2_cancelled_at_level": cancelled,
            "l2_status_reason": status,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "asset_count": len(self.status_by_asset) or len(self.snapshot_windows_by_asset),
            "raw_event_count": self.raw_event_count,
            "price_change_count": self.price_change_count,
            "reconnect_or_gap_count": self.reconnect_or_gap_count,
            "source_file_count": self.source_file_count,
            "duplicate_archive_file_count": self.duplicate_archive_file_count,
            "sparse_snapshot_index": self.sparse_snapshot_index,
            "snapshot_window_observation_count": self.snapshot_window_observation_count,
            "window_seconds": self.window.total_seconds(),
        }


@dataclass(slots=True)
class _TradeLevelMatcher:
    """Consume only receipt-available same-price taker volume for diagnostics."""

    lots: dict[tuple[str, Decimal, ShadowSide], deque[tuple[datetime, Decimal]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    tolerance: timedelta = timedelta(seconds=5)

    @classmethod
    def from_trades(cls, trades: Iterable[TradeEvent]) -> _TradeLevelMatcher:
        matcher = cls()
        for trade in sorted(trades, key=_effective_trade_at):
            matcher.lots[(trade.asset_id, trade.price, trade.side)].append(
                (_effective_trade_at(trade), trade.size)
            )
        return matcher

    def consume(
        self,
        *,
        asset_id: str,
        price: Decimal,
        aggressor_side: ShadowSide,
        at: datetime,
        maximum: Decimal,
    ) -> Decimal:
        queue = self.lots.get((asset_id, price, aggressor_side))
        if not queue or maximum <= ZERO:
            return ZERO
        earliest = at - self.tolerance
        while queue and queue[0][0] < earliest:
            queue.popleft()
        consumed = ZERO
        while queue and queue[0][0] <= at and consumed < maximum:
            trade_at, available = queue.popleft()
            del trade_at
            take = min(available, maximum - consumed)
            consumed += take
            remaining = available - take
            if remaining > ZERO:
                queue.appendleft((at, remaining))
                break
        return consumed


def _book_levels(rows: Any) -> dict[tuple[str, Decimal], Decimal]:
    output: dict[tuple[str, Decimal], Decimal] = {}
    if not isinstance(rows, list):
        return output
    for side, values in (("BUY", rows),):
        del side
        for row in values:
            if not isinstance(row, Mapping):
                continue
            price = _decimal(row.get("price"))
            size = _decimal(row.get("size"))
            if price is not None and size is not None and size > ZERO:
                # The caller supplies side-specific lists; this helper is used
                # only for a temporary book rebuild below.
                output[("", price)] = size
    return output


def _row_time(row: Mapping[str, Any]) -> datetime | None:
    return _utc(row.get("received_at") or row.get("received_at_ns"))


def _retention_preferred_paths(paths: Iterable[Path]) -> tuple[tuple[Path, ...], int]:
    """Prefer the mutable JSONL when a retention gzip duplicate coexists.

    Retention writes the gzip from a completed ``events.jsonl`` and then
    removes the plain source.  A supervisor crash between those steps can
    leave both representations in one date partition.  Reading both would
    replay the same L2 deltas twice, inflate churn, and multiply an offline
    scan's cost.  The still-present plain archive is the authoritative
    superset because it may contain later appends; an immutable gzip remains
    eligible when it is the only representation for that partition.
    """
    selected: dict[Path, Path] = {}
    duplicates = 0
    for path in sorted({Path(path) for path in paths}):
        current = selected.get(path.parent)
        if current is None:
            selected[path.parent] = path
            continue
        if current.name == "events.jsonl":
            duplicates += 1
            continue
        if path.name == "events.jsonl":
            selected[path.parent] = path
            duplicates += 1
            continue
        # A non-standard duplicate has no precedence rule.  Keep the first
        # deterministic path and expose the condition in the diagnostics.
        duplicates += 1
    return tuple(sorted(selected.values())), duplicates


def _raw_header_from_line(line: str) -> tuple[int | None, str | None]:
    """Read only archive-continuity fields without decoding an unrelated row."""
    receipt_match = _RAW_RECEIVED_NS_RE.search(line)
    if receipt_match is not None:
        try:
            received_ns = int(receipt_match.group(1))
        except ValueError:
            received_ns = None
    else:
        received_match = _RAW_RECEIVED_AT_RE.search(line)
        received_at = _utc(received_match.group(1)) if received_match else None
        received_ns = (
            int(received_at.timestamp() * 1_000_000_000)
            if received_at is not None
            else None
        )
    run_match = _RAW_RUN_ID_RE.search(line)
    return received_ns, run_match.group(1) if run_match is not None else None


def _timestamp_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _timestamp_ns(value: datetime) -> int:
    utc_value = value.astimezone(UTC)
    delta = utc_value - _EPOCH
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000_000_000
        + delta.microseconds * 1_000
    )


def build_l2_churn_index(
    paths: Sequence[Path],
    *,
    asset_ids: Iterable[str],
    trades: Iterable[TradeEvent] = (),
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    window: timedelta = timedelta(minutes=5),
    gap_tolerance: timedelta = timedelta(seconds=90),
    lookup_times_by_asset: Mapping[str, Sequence[datetime]] | None = None,
) -> L2ChurnIndex:
    """Read archived ``price_change`` rows and return true L2 churn coverage.

    A full ``book`` is the only recovery point after a reconnect or archive
    gap.  Until that point the value is UNKNOWN, which deliberately prevents a
    token from entering QUIET rather than pretending its churn is zero.  A
    valid L2 state may, however, carry across sparse replay snapshots: a
    snapshot gap is not itself an archive gap, and throwing that state away
    would create a synthetic UNKNOWN coverage failure.
    """
    wanted = {str(value) for value in asset_ids}
    selected_paths, duplicate_archive_file_count = _retention_preferred_paths(paths)
    matcher = _TradeLevelMatcher.from_trades(trades)
    # Store a side as ``price_units -> size_units`` rather than a tuple of
    # Decimal objects.  Full-depth books can contain millions of live levels
    # across all subscribed weather tokens; this representation preserves the
    # archived values exactly at the declared scale while keeping the offline
    # v2 pass bounded in memory.
    levels: dict[str, tuple[dict[int, int], dict[int, int]]] = {}
    known: dict[str, bool] = defaultdict(bool)
    statuses: dict[str, list[tuple[datetime, bool, str]]] = defaultdict(list)
    current_status: dict[str, str] = defaultdict(lambda: "no_full_l2_book")
    observations: dict[str, list[L2ChurnObservation]] = defaultdict(list)
    direct_schedule: dict[str, tuple[datetime, ...]] = {}
    if lookup_times_by_asset is not None:
        for asset, values in lookup_times_by_asset.items():
            if asset not in wanted:
                continue
            normalized = tuple(
                sorted(
                    {
                        value.astimezone(UTC)
                        for value in values
                        if isinstance(value, datetime) and value.tzinfo is not None
                    }
                )
            )
            if normalized:
                direct_schedule[asset] = normalized
    sparse_snapshot_index = lookup_times_by_asset is not None
    snapshot_windows: dict[str, dict[datetime, L2ChurnObservation]] = {
        asset: {} for asset in direct_schedule
    }
    snapshot_statuses: dict[str, dict[datetime, str]] = {
        asset: {} for asset in direct_schedule
    }
    schedule_heap = [
        (_timestamp_ns(timestamp), asset, timestamp)
        for asset, timestamps in direct_schedule.items()
        for timestamp in timestamps
    ]
    heapify(schedule_heap)
    schedule_positions: dict[str, int] = defaultdict(int)
    rolling: dict[str, deque[L2ChurnObservation]] = defaultdict(deque)
    rolling_totals: dict[str, list[Decimal]] = defaultdict(
        lambda: [ZERO, ZERO, ZERO, ZERO]
    )
    last_global_ns: int | None = None
    last_run_id: str | None = None
    header_unknown_pending = False
    raw_count = 0
    price_change_count = 0
    reconnect_count = 0
    end_ns = _timestamp_ns(end_at) if end_at is not None else None
    gap_ns = int(gap_tolerance.total_seconds() * 1_000_000_000)

    def invalidate_all(at: datetime, reason: str) -> None:
        nonlocal reconnect_count
        reconnect_count += 1
        for asset in wanted:
            known[asset] = False
            current_status[asset] = reason
            if not sparse_snapshot_index:
                statuses[asset].append((at, False, reason))
        # A reconnect/gap makes all retained price levels untrustworthy.  Drop
        # them rather than allowing a later delta to reuse pre-gap depth.
        levels.clear()
        rolling.clear()
        rolling_totals.clear()

    def append_observation(asset: str, value: L2ChurnObservation) -> None:
        if not sparse_snapshot_index:
            observations[asset].append(value)
            return
        timestamps = direct_schedule.get(asset, ())
        position = schedule_positions[asset]
        if position >= len(timestamps):
            return
        # Keep a raw L2 delta only if it can still affect the next actual
        # book snapshot for this token.  Without this bound, a resolved or
        # unsubscribed token can keep receiving deltas after its final replay
        # row and make a nominally sparse index grow without limit.
        next_snapshot = timestamps[position]
        if value.timestamp <= next_snapshot - window or value.timestamp > next_snapshot:
            return
        rolling[asset].append(value)
        totals = rolling_totals[asset]
        totals[0] += value.added
        totals[1] += value.removed
        totals[2] += value.traded
        totals[3] += value.cancelled_at_level

    def prune_rolling(asset: str, before: datetime) -> None:
        values = rolling[asset]
        totals = rolling_totals[asset]
        while values and values[0].timestamp <= before:
            expired = values.popleft()
            totals[0] -= expired.added
            totals[1] -= expired.removed
            totals[2] -= expired.traded
            totals[3] -= expired.cancelled_at_level

    def flush_snapshots(limit_ns: int, *, inclusive: bool) -> None:
        if not sparse_snapshot_index:
            return
        while schedule_heap and (
            schedule_heap[0][0] < limit_ns
            or (inclusive and schedule_heap[0][0] == limit_ns)
        ):
            _scheduled_ns, asset, timestamp = heappop(schedule_heap)
            prune_rolling(asset, timestamp - window)
            snapshot_statuses[asset][timestamp] = current_status[asset]
            totals = rolling_totals[asset]
            if known[asset]:
                snapshot_windows[asset][timestamp] = L2ChurnObservation(
                    timestamp=timestamp,
                    added=totals[0],
                    removed=totals[1],
                    traded=totals[2],
                    cancelled_at_level=totals[3],
                )
            schedule_positions[asset] += 1
            timestamps = direct_schedule[asset]
            position = schedule_positions[asset]
            if position >= len(timestamps):
                rolling.pop(asset, None)
                rolling_totals.pop(asset, None)
                levels.pop(asset, None)
                known.pop(asset, None)
            else:
                next_snapshot = timestamps[position]
                # A later snapshot has its own causal five-minute window;
                # discard only obsolete *deltas*.  The reconstructed depth
                # remains valid until the archive itself signals a gap or a
                # reconnect, even when the strategy has no snapshot for a
                # while.
                prune_rolling(asset, next_snapshot - window)

    stopped_at_cutoff = False
    for path in selected_paths:
        if stopped_at_cutoff:
            break
        if not path.exists():
            continue
        try:
            handle = open_jsonl_text(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                raw_count += 1
                header_ns, header_run_id = _raw_header_from_line(line)
                if header_ns is not None:
                    if end_ns is not None and header_ns > end_ns:
                        flush_snapshots(end_ns, inclusive=True)
                        stopped_at_cutoff = True
                        break
                    # Hold same-receipt records until a later receipt arrives:
                    # a single WS message can be archived as several token
                    # rows and all of them are simultaneously available.
                    if (
                        last_global_ns is not None
                        and header_ns > last_global_ns + gap_ns
                    ):
                        # Silence beyond the declared archive tolerance is
                        # observable at the tolerance boundary itself.  Flush
                        # only strictly prior snapshots with the old state,
                        # then make later snapshots UNKNOWN instead of waiting
                        # for the eventually arriving next record to reveal
                        # the gap.
                        gap_at_ns = last_global_ns + gap_ns
                        flush_snapshots(gap_at_ns, inclusive=False)
                        invalidate_all(_timestamp_from_ns(gap_at_ns), "archive_gap")
                    flush_snapshots(header_ns, inclusive=False)
                    if header_run_id is None:
                        invalidate_all(
                            _timestamp_from_ns(header_ns),
                            "unparseable_archive_header",
                        )
                        header_unknown_pending = True
                        last_run_id = None
                    else:
                        if last_run_id is not None and header_run_id != last_run_id:
                            invalidate_all(
                                _timestamp_from_ns(header_ns),
                                "reconnect_run_id_changed",
                            )
                        last_run_id = header_run_id
                        header_unknown_pending = False
                    last_global_ns = header_ns
                elif not header_unknown_pending:
                    # We cannot place a malformed unrelated row on the clock.
                    # Invalidate from the last trustworthy receipt rather than
                    # allowing a quiet window to bridge an opaque interval.
                    invalidate_all(
                        _timestamp_from_ns(last_global_ns)
                        if last_global_ns is not None
                        else (start_at or datetime.min.replace(tzinfo=UTC)),
                        "unparseable_archive_header",
                    )
                    header_unknown_pending = True

                asset_match = _RAW_ASSET_ID_RE.search(line)
                if asset_match is None or asset_match.group(1) not in wanted:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                at = _row_time(row)
                if at is None or (end_at is not None and at > end_at):
                    continue
                if header_ns is None:
                    record_ns = _timestamp_ns(at)
                    flush_snapshots(record_ns, inclusive=False)
                    last_global_ns = record_ns
                    last_run_id = str(row.get("run_id") or "") or None
                asset = str(row.get("asset_id") or "")
                if asset not in wanted:
                    continue
                if sparse_snapshot_index and schedule_positions[asset] >= len(
                    direct_schedule.get(asset, ())
                ):
                    continue
                event_type = str(row.get("event_type") or "")
                if event_type == "book":
                    rebuilt_sides: list[dict[int, int]] = []
                    precision_supported = True
                    for rows in (row.get("bids"), row.get("asks")):
                        rebuilt: dict[int, int] = {}
                        if not isinstance(rows, list):
                            rebuilt_sides.append(rebuilt)
                            continue
                        for level in rows:
                            if not isinstance(level, Mapping):
                                continue
                            price = _decimal(level.get("price"))
                            size = _decimal(level.get("size"))
                            if price is not None and size is not None and size > ZERO:
                                price_units = _l2_units(price, _L2_PRICE_DECIMAL_SCALE)
                                size_units = _l2_units(size, _L2_SIZE_DECIMAL_SCALE)
                                if price_units is None or size_units is None:
                                    precision_supported = False
                                    break
                                rebuilt[price_units] = size_units
                        rebuilt_sides.append(rebuilt)
                        if not precision_supported:
                            break
                    if precision_supported:
                        # The tuple order is BUY/bids, SELL/asks.
                        levels[asset] = (rebuilt_sides[0], rebuilt_sides[1])
                        known[asset] = True
                        current_status[asset] = "book_snapshot"
                        if not sparse_snapshot_index:
                            statuses[asset].append((at, True, "book_snapshot"))
                    else:
                        levels.pop(asset, None)
                        known[asset] = False
                        current_status[asset] = "unsupported_l2_precision"
                        if not sparse_snapshot_index:
                            statuses[asset].append((at, False, "unsupported_l2_precision"))
                    continue
                if event_type != "price_change":
                    continue
                price_change_count += 1
                raw = row.get("raw")
                changes = raw.get("price_changes") if isinstance(raw, Mapping) else None
                if not isinstance(changes, list) or not known[asset]:
                    continue
                for change in changes:
                    if not isinstance(change, Mapping) or str(change.get("asset_id") or asset) != asset:
                        continue
                    side = str(change.get("side") or "").upper()
                    price = _decimal(change.get("price"))
                    size = _decimal(change.get("size"))
                    if side not in {"BUY", "SELL"} or price is None or size is None:
                        continue
                    price_units = _l2_units(price, _L2_PRICE_DECIMAL_SCALE)
                    size_units = _l2_units(size, _L2_SIZE_DECIMAL_SCALE)
                    if price_units is None or size_units is None:
                        levels.pop(asset, None)
                        known[asset] = False
                        current_status[asset] = "unsupported_l2_precision"
                        break
                    side_levels = levels[asset][0 if side == "BUY" else 1]
                    prior_units = side_levels.get(price_units, 0)
                    added_units = max(0, size_units - prior_units)
                    removed_units = max(0, prior_units - size_units)
                    if size_units > 0:
                        side_levels[price_units] = size_units
                    else:
                        side_levels.pop(price_units, None)
                    added = _l2_decimal(added_units, _L2_SIZE_DECIMAL_SCALE)
                    removed = _l2_decimal(removed_units, _L2_SIZE_DECIMAL_SCALE)
                    aggressor = ShadowSide.SELL if side == "BUY" else ShadowSide.BUY
                    traded = matcher.consume(
                        asset_id=asset,
                        price=price,
                        aggressor_side=aggressor,
                        at=at,
                        maximum=removed,
                    )
                    append_observation(
                        asset,
                        L2ChurnObservation(
                            timestamp=at,
                            added=added,
                            removed=removed,
                            traded=traded,
                            cancelled_at_level=max(ZERO, removed - traded),
                        ),
                    )
    if sparse_snapshot_index and last_global_ns is not None:
        flush_snapshots(last_global_ns, inclusive=True)
    # The legacy prefix index needs state transitions.  The sparse path stores
    # exactly one status per replay snapshot instead, so a frequent full-book
    # checkpoint cannot dominate the diagnostic working set.
    if not sparse_snapshot_index:
        for asset in wanted:
            if not statuses.get(asset):
                statuses[asset].append(
                    (start_at or datetime.min.replace(tzinfo=UTC), False, "no_full_l2_book")
                )
    normalized_observations: dict[str, tuple[L2ChurnObservation, ...]] = {}
    observation_times: dict[str, tuple[datetime, ...]] = {}
    observation_prefixes: dict[
        str, tuple[tuple[Decimal, Decimal, Decimal, Decimal], ...]
    ] = {}
    if not sparse_snapshot_index:
        for asset, values in observations.items():
            ordered = tuple(sorted(values, key=lambda value: value.timestamp))
            normalized_observations[asset] = ordered
            observation_times[asset] = tuple(value.timestamp for value in ordered)
            running = (ZERO, ZERO, ZERO, ZERO)
            prefix: list[tuple[Decimal, Decimal, Decimal, Decimal]] = [running]
            for value in ordered:
                running = (
                    running[0] + value.added,
                    running[1] + value.removed,
                    running[2] + value.traded,
                    running[3] + value.cancelled_at_level,
                )
                prefix.append(running)
            observation_prefixes[asset] = tuple(prefix)
    normalized_statuses = {
        key: tuple(sorted(value, key=lambda row: row[0]))
        for key, value in statuses.items()
    }
    return L2ChurnIndex(
        observations_by_asset=normalized_observations,
        status_by_asset=normalized_statuses,
        observation_times_by_asset=observation_times,
        observation_prefix_by_asset=observation_prefixes,
        status_times_by_asset={
            key: tuple(row[0] for row in value)
            for key, value in normalized_statuses.items()
        },
        snapshot_windows_by_asset=snapshot_windows if sparse_snapshot_index else {},
        snapshot_statuses_by_asset=snapshot_statuses if sparse_snapshot_index else {},
        window=window,
        raw_event_count=raw_count,
        price_change_count=price_change_count,
        reconnect_or_gap_count=reconnect_count,
        source_file_count=len(selected_paths),
        duplicate_archive_file_count=duplicate_archive_file_count,
        sparse_snapshot_index=sparse_snapshot_index,
        snapshot_window_observation_count=sum(
            len(values) for values in snapshot_windows.values()
        ),
    )


def _price_band(snapshot: BookSnapshot) -> str | None:
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    if bid is None or ask is None:
        return None
    value = (bid + ask) / Decimal("2")
    lower = (value * Decimal("5")).to_integral_value(rounding="ROUND_FLOOR") / Decimal("5")
    upper = min(ONE, lower + Decimal("0.2"))
    return f"{lower:.1f}-{upper:.1f}"


@dataclass(slots=True)
class TradeIntensityTracker:
    """Rolling strict-prior count/volume baseline by token, band and local hour."""

    station_timezones: Mapping[str, str] = field(default_factory=dict)
    minimum_samples: int = 3
    baseline_window_samples: int = 96
    history: dict[
        tuple[str, str, str, int], deque[tuple[datetime, Decimal, Decimal]]
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        if self.baseline_window_samples < self.minimum_samples:
            raise ValueError(
                "baseline_window_samples must cover minimum_samples"
            )

    def _key(self, snapshot: BookSnapshot) -> tuple[str, str, str, int] | None:
        station = (snapshot.station_id or "unknown").upper()
        band = _price_band(snapshot)
        if band is None:
            return None
        timezone_name = self.station_timezones.get(station, "UTC")
        try:
            local_hour = snapshot.timestamp.astimezone(ZoneInfo(timezone_name)).hour
        except (KeyError, ValueError):
            local_hour = snapshot.timestamp.hour
        return station, snapshot.token_id, band, local_hour

    @staticmethod
    def _ratio(actual: Decimal, baseline: Decimal) -> Decimal:
        if baseline == ZERO:
            return ZERO if actual == ZERO else Decimal("Infinity")
        return actual / baseline

    def observe(
        self,
        snapshot: BookSnapshot,
        *,
        previous_at: datetime | None,
        trades: Sequence[TradeEvent],
    ) -> dict[str, Any]:
        key = self._key(snapshot)
        if key is None or previous_at is None or snapshot.timestamp <= previous_at:
            return {
                "trade_intensity_multiple": None,
                "metric_status": {
                    "trade_intensity_multiple": "WARMUP_INSUFFICIENT_BASELINE"
                },
                "trade_intensity_baseline_samples": 0,
                "trade_intensity_status_reason": "UNKNOWN_BASELINE",
            }
        elapsed_minutes = Decimal(str((snapshot.timestamp - previous_at).total_seconds())) / Decimal("60")
        if elapsed_minutes <= ZERO:
            return {
                "trade_intensity_multiple": None,
                "metric_status": {
                    "trade_intensity_multiple": "WARMUP_INSUFFICIENT_BASELINE"
                },
                "trade_intensity_baseline_samples": 0,
                "trade_intensity_status_reason": "UNKNOWN_BASELINE",
            }
        samples = self.history.get(key)
        if samples is None:
            samples = deque(maxlen=self.baseline_window_samples)
            self.history[key] = samples
        # This tracker is fed by the replay's chronological token stream.  All
        # retained samples are therefore strict-prior observations for this
        # key.  A fixed, declared rolling window prevents an old regime from
        # becoming an unbounded baseline and keeps all three fixed profiles
        # linear in snapshot count.
        prior = samples
        count_rate = Decimal(len(trades)) / elapsed_minutes
        volume_rate = sum((trade.size for trade in trades), start=ZERO) / elapsed_minutes
        if len(prior) < self.minimum_samples:
            result = {
                "trade_intensity_multiple": None,
                "metric_status": {
                    "trade_intensity_multiple": "WARMUP_INSUFFICIENT_BASELINE"
                },
                "trade_intensity_baseline_samples": len(prior),
                "trade_intensity_status_reason": "UNKNOWN_BASELINE",
            }
        else:
            baseline_count = Decimal(str(median([float(row[1]) for row in prior])))
            baseline_volume = Decimal(str(median([float(row[2]) for row in prior])))
            intensity = max(
                self._ratio(count_rate, baseline_count),
                self._ratio(volume_rate, baseline_volume),
            )
            result = {
                "trade_intensity_multiple": intensity,
                "metric_status": {"trade_intensity_multiple": "OK"},
                "trade_intensity_baseline_samples": len(prior),
                "trade_intensity_count_rate_per_minute": count_rate,
                "trade_intensity_volume_rate_per_minute": volume_rate,
                "trade_intensity_baseline_count_rate_per_minute": baseline_count,
                "trade_intensity_baseline_volume_rate_per_minute": baseline_volume,
                "trade_intensity_status_reason": "OK",
            }
        samples.append((snapshot.timestamp, count_rate, volume_rate))
        return result


__all__ = [
    "CROSS_BUCKET_SYNC_TOLERANCE",
    "CrossBucketMassIndex",
    "CrossBucketMassObservation",
    "L2ChurnIndex",
    "L2ChurnObservation",
    "TradeIntensityTracker",
    "build_cross_bucket_mass_index",
    "build_l2_churn_index",
]
