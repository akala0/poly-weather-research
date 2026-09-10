"""Strictly read-only paper spread runtime built on the isolated paper ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from poly_weather.archive_io import jsonl_archive_paths
from poly_weather.config import load_settlement_registry
from poly_weather.market_trade_tape import (
    MarketWsTrade,
    build_shadow_trade_events,
    parse_market_ws_trade,
)
from poly_weather.paper_account import (
    PAPER_LEDGER_KIND,
    PAPER_LEDGER_SCHEMA_VERSION,
    PaperLedger,
    PaperLedgerIntegrityError,
    record_account_action,
    restore_paper_account,
)
from poly_weather.polymarket_status import (
    excluded_market_data_window_overlaps,
    load_quality_windows,
    quality_window_at,
)
from poly_weather.real_no_books import archived_event_metadata
from poly_weather.runtime_safety import (
    StatusIntegrityError,
    atomic_json_write,
    read_chain_status,
    read_json_with_fallback,
    read_status,
)
from poly_weather.shadow_orders import (
    ZERO,
    BookSnapshot,
    FillModel,
    QuoteMode,
    ShadowFill,
    ShadowOrder,
    ShadowOrderEngine,
    ShadowOrderReason,
    ShadowOrderRejected,
    ShadowOrderState,
    ShadowSide,
    StationDayBudgetMode,
    TokenPortfolioKey,
    TradeEvent,
    _decimal,
    quote_limit,
)
from poly_weather.shadow_runtime import (
    ShadowCursor,
    _archive_pair_rows,
    _incremental_jsonl_rows,
    _parse_observation_safely,
    _public_trade_events_from_file,
    execution_dependency_scan,
)
from poly_weather.trade_evidence import exact_decimal_text as _exact_decimal_text
from poly_weather.trade_tape_analysis import load_event_trade_tapes
from poly_weather.weather_market_join import align_weather_to_snapshots

# The local paired-book stream emits on a five-minute cadence. A risk exit can
# only use a native bid snapshot at or inside this boundary; it may never make
# a cached quote look current by replacing its timestamp.
DEFAULT_RISK_EXIT_SNAPSHOT_AGE = timedelta(minutes=5)


# Decimal division of a repeating fractional share count can differ from the
# original tick price by a final context digit. This is many orders smaller
# than the CLOB's $0.01 price grid and is used only to avoid rejecting an
# exactly-on-tick threshold because of arithmetic representation noise.
PRICE_COMPARISON_EPSILON = Decimal("0.000000000000000001")


@dataclass(frozen=True, slots=True)
class PaperWeatherEvidence:
    """The single production/fixture weather contract for Paper V1.

    ``align_weather_to_snapshots`` owns these nested metadata fields. No
    fixture-only ``weather_observed_at`` or ``weather_receipt_eligible`` is
    accepted: eligibility is recomputed from the timestamp chain here.
    """

    observation_id: str
    source_timestamp: datetime
    received_at: datetime
    observation_new: bool
    improving: bool
    worsening: bool
    unchanged: bool
    market_lag: bool

    @property
    def ordering_key(self) -> tuple[datetime, datetime, str]:
        return (self.source_timestamp, self.received_at, self.observation_id)

    @classmethod
    def parse(cls, snapshot: BookSnapshot) -> tuple[PaperWeatherEvidence | None, str]:
        metadata = snapshot.metadata
        if not isinstance(metadata, Mapping):
            return None, "weather_metadata_missing"
        if str(metadata.get("weather_join_status") or "") != "aligned":
            return None, "weather_join_not_aligned"
        observation_id = metadata.get("weather_observation_id")
        source = metadata.get("weather_source_timestamp")
        received = metadata.get("weather_received_at")
        if not observation_id or source is None or received is None:
            return None, "weather_metadata_incomplete"
        try:
            source_at = datetime.fromisoformat(str(source).replace("Z", "+00:00"))
            received_at = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
            if source_at.tzinfo is None or received_at.tzinfo is None:
                return None, "weather_metadata_timestamp_naive"
            source_at = source_at.astimezone(UTC)
            received_at = received_at.astimezone(UTC)
        except (TypeError, ValueError):
            return None, "weather_metadata_timestamp_invalid"
        if not (source_at <= received_at <= snapshot.timestamp):
            return None, "weather_receipt_chain_invalid"
        return (
            cls(
                observation_id=str(observation_id),
                source_timestamp=source_at,
                received_at=received_at,
                observation_new=bool(metadata.get("weather_observation_new")),
                improving=bool(metadata.get("weather_improving")),
                worsening=bool(metadata.get("weather_worsening")),
                unchanged=bool(metadata.get("weather_unchanged")),
                market_lag=bool(metadata.get("weather_market_lag")),
            ),
            "weather_evidence_valid",
        )


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=5
        ).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _receipt_visible_by(available_at: Any, as_of: datetime) -> bool:
    """Return true only for a receipt already visible to Paper.

    A public-trade tape may be re-read after a follower restart, so its file
    timestamp is never a substitute for a timezone-aware receipt timestamp.
    """

    return (
        isinstance(available_at, datetime)
        and available_at.tzinfo is not None
        and available_at.astimezone(UTC) <= as_of
    )


@dataclass(frozen=True, slots=True)
class PaperTranche:
    usd: Decimal
    gate: str


@dataclass(frozen=True, slots=True)
class PaperExitStage:
    rise: Decimal
    fraction_of_initial_shares: Decimal


@dataclass(frozen=True, slots=True)
class PaperStrategyConfig:
    schema_version: int
    version: str
    trigger_strategy: str
    initial_cash_usd: Decimal
    quote_mode: QuoteMode
    fill_model: FillModel
    entry_bands: Mapping[str, tuple[tuple[Decimal, Decimal], ...]]
    tranches: tuple[PaperTranche, ...]
    exits: tuple[PaperExitStage, ...]
    order_timeout: timedelta
    max_hold: timedelta
    risk_exit_snapshot_age: timedelta
    station_day_budget_mode: StationDayBudgetMode
    config_path: Path
    config_sha256: str

    @classmethod
    def load(cls, path: Path | str) -> PaperStrategyConfig:
        destination = Path(path).resolve()
        raw = json.loads(destination.read_text(encoding="utf-8"))
        normalized = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if raw.get("execution_enabled") is not False:
            raise ValueError("paper config must set execution_enabled=false")
        tranches = tuple(PaperTranche(_decimal(row["usd"]), str(row["gate"])) for row in raw["tranche_plan"])
        exits = tuple(PaperExitStage(_decimal(row["rise"]), _decimal(row["fraction_of_initial_shares"])) for row in raw["exit_plan"])
        entry_bands = {
            str(station): tuple((_decimal(lower), _decimal(upper)) for lower, upper in bands)
            for station, bands in raw["entry_bands"].items()
        }
        config = cls(
            schema_version=int(raw["schema_version"]),
            version=str(raw["version"]),
            trigger_strategy=str(raw["trigger_strategy"]),
            initial_cash_usd=_decimal(raw["initial_cash_usd"]),
            quote_mode=QuoteMode(str(raw["quote_mode"])),
            fill_model=FillModel(str(raw["fill_model"])),
            entry_bands=entry_bands,
            tranches=tranches,
            exits=exits,
            order_timeout=timedelta(seconds=int(raw["order_timeout_seconds"])),
            max_hold=timedelta(seconds=int(raw["max_hold_seconds"])),
            risk_exit_snapshot_age=timedelta(
                seconds=int(
                    raw.get(
                        "risk_exit_snapshot_age_seconds",
                        int(DEFAULT_RISK_EXIT_SNAPSHOT_AGE.total_seconds()),
                    )
                )
            ),
            station_day_budget_mode=StationDayBudgetMode(str(raw["station_day_budget_mode"])),
            config_path=destination,
            config_sha256=hashlib.sha256(normalized).hexdigest(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if (
            self.schema_version != 3
            or self.version != "paper-spread-v1-account-200"
            or self.trigger_strategy != "weather_market_lag_in_band"
            or self.initial_cash_usd != Decimal("200")
            or self.quote_mode is not QuoteMode.BEST_BID
            or self.fill_model is not FillModel.QUEUE_AWARE
            or self.station_day_budget_mode is not StationDayBudgetMode.CUMULATIVE_BUY_COST
        ):
            raise ValueError("unexpected paper strategy identity")
        if self.entry_bands != {
            "KLAX": ((Decimal("0.70"), Decimal("0.85")),),
            "KLGA": ((Decimal("0.50"), Decimal("0.70")),),
        }:
            raise ValueError("paper strategy requires frozen station entry bands")
        if tuple(row.gate for row in self.tranches) != (
            "initial",
            "new_weather_confirmation",
            "conditional_dip",
            "second_new_weather_confirmation",
        ):
            raise ValueError("paper strategy requires frozen tranche gates")
        if tuple(row.usd for row in self.tranches) != (Decimal("20"), Decimal("30"), Decimal("50"), Decimal("100")):
            raise ValueError("paper strategy requires frozen 20+30+50+100 tranches")
        if tuple(row.rise for row in self.exits) != (Decimal("0.05"), Decimal("0.10"), Decimal("0.13"), Decimal("0.20")):
            raise ValueError("paper strategy requires frozen exit rises")
        if tuple(row.fraction_of_initial_shares for row in self.exits) != (
            Decimal("0.25"),
            Decimal("0.25"),
            Decimal("0.25"),
            Decimal("0.25"),
        ):
            raise ValueError("paper exit fractions must each equal one quarter")
        if (
            self.order_timeout != timedelta(seconds=900)
            or self.max_hold != timedelta(seconds=7200)
            or self.risk_exit_snapshot_age != DEFAULT_RISK_EXIT_SNAPSHOT_AGE
        ):
            raise ValueError("paper timeouts must match the frozen strategy")


@dataclass
class PaperPortfolioState:
    tranche_index: int = 0
    filled_tranches: set[int] = field(default_factory=set)
    consumed_observations: set[str] = field(default_factory=set)
    last_consumed_source_timestamp: datetime | None = None
    last_consumed_received_at: datetime | None = None
    last_consumed_observation_id: str | None = None
    cumulative_bought_shares: Decimal = ZERO
    position_opened_at: datetime | None = None
    exit_stage_filled_shares: dict[int, Decimal] = field(default_factory=dict)
    completed_exit_stages: set[int] = field(default_factory=set)
    exit_stage_attempts: dict[int, int] = field(default_factory=dict)
    exit_stage_dust: set[int] = field(default_factory=set)
    stranded: bool = False
    closed: bool = False
    close_reason: str | None = None


def _paper_mutation_boundary(default: Any):
    """Turn a failed durable transition into a process-local Paper HALT.

    The order engine mutates its live object immediately before it persists a
    snapshot.  Retrying that object after an ``OSError`` would make its memory
    state an unaudited authority, so the only safe response is to freeze this
    processor and let startup reconciliation inspect the append-only facts.
    """

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        def wrapped(self: PaperSpreadProcessor, *args: Any, **kwargs: Any) -> Any:
            try:
                return method(self, *args, **kwargs)
            except (OSError, PaperLedgerIntegrityError) as exc:
                self._persistence_failure(method.__name__, exc)
                return default

        return wrapped

    return decorate


class PaperSpreadProcessor:
    """Paper-v1 strategy controller; all market data is supplied by the caller."""

    def __init__(self, *, ledger: PaperLedger, strategy: PaperStrategyConfig) -> None:
        self.ledger = ledger
        self.strategy = strategy
        self.recovery = ledger.reconcile_startup(initial_cash_usd=strategy.initial_cash_usd)
        self.account = restore_paper_account(ledger, initial_cash_usd=strategy.initial_cash_usd)
        self.engines: dict[TokenPortfolioKey, ShadowOrderEngine] = {}
        self.states: dict[TokenPortfolioKey, PaperPortfolioState] = {}
        self.latest_snapshots: dict[TokenPortfolioKey, BookSnapshot] = {}
        self.station_day_buy_cost: dict[tuple[str, str], Decimal] = {}
        self.trade_evidence_counts: dict[str, int] = defaultdict(int)
        self.pending_ws_trade_evidence: dict[str, dict[str, Any]] = {}
        self._seen_trade_identities: set[str] = set()
        self.trade_observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.trade_time_groups: dict[tuple[str, datetime], list[dict[str, Any]]] = defaultdict(list)
        self.supervisor_generation: str | None = None
        self.supervisor_active_events: set[str] = set()
        self.supervisor_integrity = "unreadable"
        self.quality_integrity = "unreadable"
        self.quality_last_refresh_at: datetime | None = None
        self.quality_window_hash: str | None = None
        self.quality_windows: tuple[Any, ...] = ()
        self.quality_affected_active_orders = 0
        self.checkpoint_integrity = "not_present"
        # A new isolated ledger starts at a committed tail boundary with no
        # known gap. Any observed gap or ambiguous tape immediately changes
        # this to ``unknown`` and blocks readiness until resolved.
        self.feed_continuity = "verified"
        self.new_orders_blocked_reasons: set[str] = set()
        self.business_readiness: dict[str, Any] | None = None
        self.last_risk_exit_book_age_seconds: float | None = None
        self.capital_time_usd_seconds = ZERO
        self._capital_clock_at: datetime | None = None
        self.config_mismatch = self._has_config_mismatch()
        self.halted = self.account.halted
        self.halt_reason = self.account.halt_reason
        self.fatal_persistence_failure = False
        self._restore_strategy_state()
        if any(key.startswith("trade:") for key in ledger._seen_events):
            # Old keys omit side/receipt and do not prove cross-source uniqueness.
            self._halt("legacy_paper_trade_identity_requires_audit")
        if self.ledger.invalid:
            self._halt("paper_ledger_integrity_failure")

    @property
    def is_halted(self) -> bool:
        return self.halted or self.account.halted or self.ledger.invalid or self.fatal_persistence_failure

    def _mutations_blocked(self) -> bool:
        """Reject every external state transition once Paper has halted."""

        return self.is_halted

    def _has_config_mismatch(self) -> bool:
        expected = self.strategy.config_sha256
        return any(
            order.metadata.get("config_sha256") not in {None, expected}
            for order in self.ledger.orders.values()
        ) or any(
            details.get("config_sha256") not in {None, expected}
            for row in self.ledger.decisions
            if isinstance((details := row.get("details")), Mapping)
        )

    def _restore_strategy_state(self) -> None:
        for row in self.ledger.decisions:
            details = row.get("details", {})
            if not isinstance(details, Mapping):
                self._halt("invalid_persisted_strategy_decision")
                continue
            decision = str(row.get("decision") or "")
            if decision == "trade_identity_observation_v2":
                self.trade_observations[str(details["group"])].append(dict(details))
                self._index_time_group(details)
                continue
            if decision == "capital_clock":
                self.capital_time_usd_seconds = _decimal(details["capital_time_usd_seconds"])
                if details.get("as_of"):
                    self._capital_clock_at = datetime.fromisoformat(
                        str(details["as_of"])
                    ).astimezone(UTC)
                continue
            if decision == "trade_evidence_pending":
                evidence_id = str(details.get("evidence_id") or "")
                if evidence_id:
                    self.pending_ws_trade_evidence[evidence_id] = dict(details)
                continue
            if decision == "trade_evidence_resolved":
                evidence_id = str(details.get("evidence_id") or "")
                if evidence_id:
                    self.pending_ws_trade_evidence.pop(evidence_id, None)
                continue
            if decision == "trade_evidence_unknown":
                code = str(details.get("reason_code") or "UNKNOWN_TRADE_EVIDENCE")
                self.trade_evidence_counts[code] += int(details.get("count") or 1)
                continue
            if decision == "trade_evidence_duplicate":
                self.trade_evidence_counts["duplicate_trade"] += int(details.get("count") or 1)
                continue
            if decision == "supervisor_evidence":
                generation = str(details.get("generation") or "") or None
                active_ids = details.get("active_event_ids")
                if isinstance(active_ids, list):
                    self.supervisor_generation = generation
                    self.supervisor_active_events = {str(value) for value in active_ids if value}
                continue
            if decision == "quality_evidence":
                self.quality_window_hash = str(details.get("window_hash") or "") or None
                refreshed_at = details.get("refreshed_at")
                if refreshed_at:
                    try:
                        self.quality_last_refresh_at = datetime.fromisoformat(
                            str(refreshed_at)
                        ).astimezone(UTC)
                    except (TypeError, ValueError):
                        self._halt("invalid_persisted_quality_evidence")
                continue
            if decision == "quality_active_orders_affected":
                self.quality_affected_active_orders = int(
                    details.get("affected_active_orders") or 0
                )
                continue
            if decision == "feed_continuity":
                self.feed_continuity = str(details.get("state") or "unknown")
                continue
            key_data = details.get("portfolio_key")
            if not key_data:
                continue
            key = TokenPortfolioKey(**key_data)
            state = self.states.setdefault(key, PaperPortfolioState())
            if decision == "tranche_submitted":
                state.tranche_index = max(state.tranche_index, int(details["tranche_index"]) + 1)
            elif decision == "tranche_filled":
                state.filled_tranches.add(int(details["tranche_index"]))
            elif decision == "observation_consumed":
                state.consumed_observations.add(str(details["observation_id"]))
                for attribute, field_name in (
                    ("last_consumed_source_timestamp", "source_timestamp"),
                    ("last_consumed_received_at", "received_at"),
                ):
                    if details.get(field_name):
                        try:
                            setattr(
                                state,
                                attribute,
                                datetime.fromisoformat(str(details[field_name])).astimezone(UTC),
                            )
                        except (TypeError, ValueError):
                            self._halt("invalid_persisted_weather_evidence")
                state.last_consumed_observation_id = str(details.get("observation_id") or "") or None
            elif decision == "cumulative_bought_shares":
                state.cumulative_bought_shares = _decimal(details["cumulative_bought_shares"])
            elif decision == "position_opened":
                opened_at = details.get("position_opened_at")
                if opened_at:
                    state.position_opened_at = datetime.fromisoformat(str(opened_at)).astimezone(UTC)
            elif decision == "portfolio_closed":
                state.closed = True
                state.close_reason = str(details.get("reason") or "closed")
            elif decision == "exit_stage_fill":
                stage_index = int(details["stage_index"])
                state.exit_stage_filled_shares[stage_index] = (
                    state.exit_stage_filled_shares.get(stage_index, ZERO)
                    + _decimal(details["shares"])
                )
            elif decision == "exit_stage_filled":
                state.completed_exit_stages.add(int(details["stage_index"]))
            elif decision == "exit_stage_attempt":
                stage_index = int(details["stage_index"])
                state.exit_stage_attempts[stage_index] = max(
                    state.exit_stage_attempts.get(stage_index, 0), int(details["attempt"])
                )
            elif decision == "exit_stage_dust":
                state.exit_stage_dust.add(int(details["stage_index"]))
            elif decision == "stranded":
                state.stranded = True
        for key, position in self.account.positions.items():
            if position.stranded:
                self.states.setdefault(key, PaperPortfolioState()).stranded = True
        # Decisions are useful audit evidence, but a crash can occur after a
        # durable order/fill snapshot and before its corresponding strategy
        # decision.  Order facts are the authority for all economic state, so
        # rebuild the derivable strategy state before reading any new input.
        # A decision claiming *more* than the order facts is not repairable.
        derived_buys: dict[TokenPortfolioKey, Decimal] = defaultdict(lambda: ZERO)
        derived_opened_at: dict[TokenPortfolioKey, datetime] = {}
        derived_filled_tranches: dict[TokenPortfolioKey, set[int]] = defaultdict(set)
        derived_attempts: dict[TokenPortfolioKey, dict[int, int]] = defaultdict(dict)
        derived_exit_fills: dict[TokenPortfolioKey, dict[int, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: ZERO)
        )
        derived_observations: dict[TokenPortfolioKey, list[PaperWeatherEvidence]] = defaultdict(list)
        for order in self.ledger.orders.values():
            key = order.portfolio_key
            state = self._state(key)
            if order.side is ShadowSide.BUY:
                tranche_index = next(
                    (
                        index
                        for index, tranche in enumerate(self.strategy.tranches)
                        if order.trigger_reason == f"paper:{tranche.gate}"
                    ),
                    None,
                )
                if tranche_index is not None:
                    state.tranche_index = max(state.tranche_index, tranche_index + 1)
                try:
                    source_value = order.metadata.get("source_timestamp")
                    received_value = order.metadata.get("received_at")
                    observation_value = order.metadata.get("observation_id")
                    if source_value and received_value and observation_value:
                        source_at = datetime.fromisoformat(str(source_value)).astimezone(UTC)
                        received_at = datetime.fromisoformat(str(received_value)).astimezone(UTC)
                        derived_observations[key].append(
                            PaperWeatherEvidence(
                                observation_id=str(observation_value),
                                source_timestamp=source_at,
                                received_at=received_at,
                                observation_new=True,
                                improving=True,
                                worsening=False,
                                unchanged=False,
                                market_lag=False,
                            )
                        )
                except (TypeError, ValueError):
                    self._halt("invalid_persisted_weather_evidence")
            else:
                stage_value = order.metadata.get("exit_stage")
                if stage_value is not None:
                    try:
                        stage_index = int(stage_value)
                        attempt = int(order.metadata.get("exit_attempt") or 0)
                    except (TypeError, ValueError):
                        self._halt("invalid_persisted_exit_stage")
                    else:
                        derived_attempts[key][stage_index] = max(
                            derived_attempts[key].get(stage_index, 0), attempt
                        )
                        derived_exit_fills[key][stage_index] += sum(
                            (fill.shares for fill in order.fills), start=ZERO
                        )
            for fill in order.fills:
                if order.side is ShadowSide.BUY:
                    derived_buys[key] += fill.shares
                    derived_opened_at[key] = min(
                        derived_opened_at.get(key, fill.timestamp), fill.timestamp
                    )
                    if tranche_index is not None:
                        derived_filled_tranches[key].add(tranche_index)
            if key not in self.engines:
                self.engines[key] = ShadowOrderEngine(
                    ledger=self.ledger,
                    budget_usd=self.strategy.initial_cash_usd,
                    max_active_orders=1,
                    fill_model=self.strategy.fill_model,
                    order_timeout=self.strategy.order_timeout,
                    portfolio_key=key,
                    budget_mode=self.strategy.station_day_budget_mode,
                )
            if order.side is ShadowSide.BUY:
                station = order.station_id
                if station:
                    day_key = (station, key.market_day)
                    fill_cost = sum((fill.shares * fill.price + fill.fee_usd for fill in order.fills), start=ZERO)
                    self.station_day_buy_cost[day_key] = self.station_day_buy_cost.get(day_key, ZERO) + fill_cost
        for key, value in derived_buys.items():
            state = self._state(key)
            if state.cumulative_bought_shares > value:
                self._halt(
                    "strategy_state_exceeds_durable_buy_fills",
                    details={"portfolio_key": key.as_dict()},
                )
                continue
            state.cumulative_bought_shares = value
            state.filled_tranches.update(derived_filled_tranches[key])
            if state.position_opened_at is None:
                state.position_opened_at = derived_opened_at.get(key)
            elif key in derived_opened_at and state.position_opened_at != derived_opened_at[key]:
                self._halt(
                    "strategy_position_open_time_mismatch",
                    details={"portfolio_key": key.as_dict()},
                )
            for stage_index, attempt in derived_attempts[key].items():
                state.exit_stage_attempts[stage_index] = max(
                    state.exit_stage_attempts.get(stage_index, 0), attempt
                )
            for stage_index, shares in derived_exit_fills[key].items():
                persisted = state.exit_stage_filled_shares.get(stage_index, ZERO)
                if persisted > shares:
                    self._halt(
                        "strategy_exit_fill_exceeds_durable_order_fills",
                        details={"portfolio_key": key.as_dict(), "stage_index": stage_index},
                    )
                    continue
                state.exit_stage_filled_shares[stage_index] = shares
            observations = sorted(derived_observations[key], key=lambda item: item.ordering_key)
            if observations:
                state.consumed_observations.update(item.observation_id for item in observations)
                latest = observations[-1]
                prior = (
                    state.last_consumed_source_timestamp,
                    state.last_consumed_received_at,
                    state.last_consumed_observation_id or "",
                )
                if prior[0] is None or latest.ordering_key > prior:
                    state.last_consumed_source_timestamp = latest.source_timestamp
                    state.last_consumed_received_at = latest.received_at
                    state.last_consumed_observation_id = latest.observation_id
        for key, engine in self.engines.items():
            active = tuple(engine.active_orders)
            if len(active) > 1:
                self._halt(
                    "multiple_eligible_active_orders_recovered",
                    details={
                        "portfolio_key": key.as_dict(),
                        "order_ids": [order.order_id for order in active],
                    },
                )

        # A process may die after an order fill has become durable but before
        # the derived ``exit_stage_filled`` / ``portfolio_closed`` decisions
        # are appended.  Those decisions are not an independent authority:
        # reconstruct only what the immutable order facts prove.  In
        # particular, never infer completion from a planned dollar amount.
        for key, state in self.states.items():
            engine = self.engines.get(key)
            if engine is None:
                continue
            for stage_index, filled_shares in state.exit_stage_filled_shares.items():
                if not 0 <= stage_index < len(self.strategy.exits):
                    self._halt(
                        "strategy_exit_stage_out_of_range",
                        details={"portfolio_key": key.as_dict(), "stage_index": stage_index},
                    )
                    continue
                if stage_index in state.completed_exit_stages:
                    continue
                if stage_index == len(self.strategy.exits) - 1:
                    completed = engine.inventory_shares <= ZERO and filled_shares > ZERO
                else:
                    completed = filled_shares >= self._exit_stage_target(engine, state, stage_index)
                if completed:
                    state.completed_exit_stages.add(stage_index)
            if (
                state.cumulative_bought_shares > ZERO
                and engine.inventory_shares <= ZERO
                and not engine.active_orders
                and not state.stranded
            ):
                state.closed = True
                state.close_reason = state.close_reason or "recovered_zero_inventory"

    def _engine(self, snapshot: BookSnapshot) -> ShadowOrderEngine:
        key = snapshot.portfolio_key
        engine = self.engines.get(key)
        if engine is None:
            engine = ShadowOrderEngine(
                ledger=self.ledger, budget_usd=self.strategy.initial_cash_usd,
                max_active_orders=1, fill_model=self.strategy.fill_model,
                order_timeout=self.strategy.order_timeout, portfolio_key=key,
                budget_mode=self.strategy.station_day_budget_mode,
            )
            self.engines[key] = engine
        return engine

    def _state(self, key: TokenPortfolioKey) -> PaperPortfolioState:
        return self.states.setdefault(key, PaperPortfolioState())

    def _station_day_available(self, snapshot: BookSnapshot) -> Decimal:
        if snapshot.station_id is None:
            return ZERO
        used = self.station_day_buy_cost.get((snapshot.station_id, snapshot.market_day or snapshot.timestamp.date().isoformat()), ZERO)
        return max(ZERO, self.strategy.initial_cash_usd - used)

    def _record_decision(self, *, event_key: str, decision: str, details: Mapping[str, Any]) -> bool:
        """Persist one non-economic Paper fact or stop the current transition.

        A decision often accompanies a durable order/account transition.  It
        must not be treated as an optional audit write: once its append fails,
        callers cannot safely continue mutating the in-memory controller.
        """

        if self._mutations_blocked():
            return False
        try:
            self.ledger.record_decision(event_key=event_key, decision=decision, details=details)
        except (OSError, PaperLedgerIntegrityError) as exc:
            self._halt(
                "paper_decision_persistence_failed",
                details={"decision": decision, "error": str(exc)},
            )
            # Decorated public entries convert this to a halted no-op.  The
            # re-raise is essential: do not let later state mutations run in
            # the same call after a failed append.
            raise
        return True

    def _persistence_failure(self, operation: str, exc: BaseException) -> None:
        """Freeze economic mutation after any failed ledger/status transition."""

        self._halt(
            "paper_transition_persistence_failed",
            details={"operation": operation, "error": f"{type(exc).__name__}: {exc}"},
        )

    def _halt(self, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.account.halt(reason)
        try:
            self.ledger.record_discrepancy(code=reason, details=dict(details or {}))
        except (OSError, PaperLedgerIntegrityError):
            # No durable halt fact means a follower cannot truthfully resume;
            # callers must terminate instead of accepting another input cycle.
            self.fatal_persistence_failure = True

    @staticmethod
    def _stable_order_id(logical_identity: str) -> str:
        return "paper-" + hashlib.sha256(logical_identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _terminal_transition_id(order: ShadowOrder) -> str | None:
        if order.cancelled_at is not None:
            return f"cancel:{order.order_id}:{order.cancelled_at.isoformat()}"
        if order.expired_at is not None:
            return f"expire:{order.order_id}:{order.expired_at.isoformat()}"
        return None

    def _advance_capital_clock(self, as_of: datetime) -> bool:
        if self._mutations_blocked():
            return False
        timestamp = as_of.astimezone(UTC)
        if self._capital_clock_at is not None and timestamp < self._capital_clock_at:
            # A cursor-transaction failure can leave a later lifecycle fact
            # durable while its earlier source rows must be replayed.  Do not
            # turn that recovery path into a false HALT or use the older row
            # to mutate capital time.  The caller treats it as stale and
            # performs no economic transition.
            self.trade_evidence_counts["OUT_OF_ORDER_PAPER_EVENT_CLOCK"] += 1
            return False
        if self._capital_clock_at is not None:
            exposure = (
                self.account.inventory_cost_usd
                + self.account.stranded_inventory_cost_usd
                + self.account.buy_reserved_usd
            )
            self.capital_time_usd_seconds += exposure * Decimal(
                str((timestamp - self._capital_clock_at).total_seconds())
            )
        self._capital_clock_at = timestamp
        self._record_decision(
            event_key=f"capital-clock:{timestamp.isoformat()}",
            decision="capital_clock",
            details={
                "as_of": timestamp.isoformat(),
                "capital_time_usd_seconds": str(self.capital_time_usd_seconds),
                "execution_enabled": False,
            },
        )
        return not self._mutations_blocked()

    @_paper_mutation_boundary(None)
    def set_business_readiness(self, evidence: Mapping[str, Any]) -> None:
        if self._mutations_blocked():
            return
        self.business_readiness = dict(evidence)
        if evidence.get("input_evidence_ready") is True:
            self.new_orders_blocked_reasons.discard("business_readiness_unverified")
        else:
            self.new_orders_blocked_reasons.add("business_readiness_unverified")

    @_paper_mutation_boundary(())
    def set_supervisor_evidence(
        self,
        *,
        generation: str | None,
        active_event_ids: set[str] | None,
        integrity: str,
        as_of: datetime,
    ) -> tuple[str, ...]:
        """Apply verified active-set removal before accepting new snapshots."""

        if self._mutations_blocked():
            return ()
        self.supervisor_integrity = integrity
        if integrity != "verified" or active_event_ids is None:
            self.new_orders_blocked_reasons.add("supervisor_evidence_unreadable")
            self._record_decision(
                event_key=f"supervisor-evidence-unreadable:{as_of.astimezone(UTC).isoformat()}",
                decision="supervisor_evidence",
                details={
                    "generation": generation,
                    "active_event_ids": sorted(self.supervisor_active_events),
                    "integrity": integrity,
                    "as_of": as_of.astimezone(UTC).isoformat(),
                    "execution_enabled": False,
                },
            )
            return ()
        # A cursor can be stale or absent after an unclean stop.  Restored
        # portfolio IDs are therefore part of the prior active set: the first
        # verified supervisor read must still close a portfolio that vanished
        # while this follower was down.
        prior = set(self.supervisor_active_events) | {
            key.event_id for key in self.engines
        }
        removed = prior - active_event_ids
        self.supervisor_generation = generation
        self.supervisor_active_events = set(active_event_ids)
        self.new_orders_blocked_reasons.discard("supervisor_evidence_unreadable")
        active_hash = _active_event_hash(self.supervisor_active_events)
        self._record_decision(
            event_key=f"supervisor-evidence:{generation or 'none'}:{active_hash}",
            decision="supervisor_evidence",
            details={
                "generation": generation,
                "active_event_ids": sorted(self.supervisor_active_events),
                "active_event_hash": active_hash,
                "integrity": "verified",
                "as_of": as_of.astimezone(UTC).isoformat(),
                "execution_enabled": False,
            },
        )
        for event_id in sorted(removed):
            for key in tuple(self.engines):
                if key.event_id == event_id:
                    self.close_portfolio_key(key, reason="supervisor_active_set_removed", as_of=as_of)
        return tuple(sorted(removed))

    @_paper_mutation_boundary(None)
    def set_quality_evidence(
        self,
        *,
        integrity: str,
        refreshed_at: datetime,
        window_hash: str | None,
        windows: Sequence[Any] = (),
    ) -> None:
        if self._mutations_blocked():
            return
        self.quality_integrity = integrity
        self.quality_last_refresh_at = refreshed_at.astimezone(UTC)
        self.quality_window_hash = window_hash
        self.quality_windows = tuple(windows) if integrity == "verified" else ()
        if integrity == "verified":
            self.new_orders_blocked_reasons.discard("quality_evidence_unreadable")
        else:
            self.new_orders_blocked_reasons.add("quality_evidence_unreadable")
        self._record_decision(
            event_key=(
                f"quality-evidence:{integrity}:{window_hash or 'none'}"
                if integrity == "verified"
                else f"quality-evidence-unreadable:{refreshed_at.astimezone(UTC).isoformat()}"
            ),
            decision="quality_evidence",
            details={
                "integrity": integrity,
                "window_hash": window_hash,
                "refreshed_at": refreshed_at.astimezone(UTC).isoformat(),
                "execution_enabled": False,
            },
        )

    @_paper_mutation_boundary(0)
    def apply_quality_windows(self, windows: Sequence[Any], *, as_of: datetime) -> int:
        """Cancel exposure that spans a newly discovered excluded window.

        The local quality document is evidence available at this poll, not a
        reason to rewrite old fills. An active maker order spanning an
        excluded market-data interval becomes unsafe prospectively and is
        cancelled through the same terminal-account path.
        """

        if self._mutations_blocked():
            return 0
        affected = 0
        point = as_of.astimezone(UTC)
        for key, engine in tuple(self.engines.items()):
            active = engine.active_orders
            if not active:
                continue
            earliest_submission = min(order.submitted_at for order in active)
            crossed = any(
                bool(getattr(window, "default_excluded", True))
                and bool(getattr(window, "affects_market_data", True))
                and earliest_submission <= window.start_at <= point
                for window in windows
            )
            if crossed:
                affected += len(active)
                self.close_portfolio_key(
                    key, reason="quality_window_discovered", as_of=point
                )
        self.quality_affected_active_orders = affected
        if affected:
            self._record_decision(
                event_key=f"quality-active-order-close:{point.isoformat()}",
                decision="quality_active_orders_affected",
                details={
                    "affected_active_orders": affected,
                    "as_of": point.isoformat(),
                    "execution_enabled": False,
                },
            )
        return affected

    def _quality_excludes(self, timestamp: datetime) -> bool:
        return quality_window_at(tuple(self.quality_windows), timestamp) is not None

    def _record_trade_evidence(
        self,
        *,
        code: str,
        event_key: str,
        details: Mapping[str, Any],
        count: int = 1,
    ) -> None:
        """Persist unknown/duplicate tape evidence so restarts remain conservative."""

        if self.ledger.event_seen(event_key):
            return
        self.trade_evidence_counts[code] += count
        decision = "trade_evidence_duplicate" if code == "duplicate_trade" else "trade_evidence_unknown"
        self._record_decision(
            event_key=event_key,
            decision=decision,
            details={
                **dict(details),
                "reason_code": code,
                "count": count,
                "execution_enabled": False,
            },
        )

    @staticmethod
    def _ws_trade_evidence(parsed: Any) -> tuple[str, dict[str, Any]]:
        details = {
            "asset_id": str(parsed.asset_id),
            "market_id": str(parsed.market_id),
            "transaction_hash": str(parsed.transaction_hash or "") or None,
            "event_id": str(parsed.event_id),
            "source_timestamp": parsed.source_timestamp.astimezone(UTC).isoformat(),
            "received_at": parsed.received_at.astimezone(UTC).isoformat(),
            "price": _exact_decimal_text(parsed.price),
            "size": _exact_decimal_text(parsed.size),
            "side": str(parsed.side),
            "sequence": parsed.sequence,
            "evidence_schema_version": 2,
            "upstream_status": getattr(parsed, "upstream_status", "unknown"),
            "upstream_incident_id": getattr(parsed, "upstream_incident_id", None),
            "run_id": getattr(parsed, "run_id", None),
            "source": getattr(parsed, "source", "market_ws"),
            "market_slug": getattr(parsed, "market_slug", None),
        }
        encoded = json.dumps(details, sort_keys=True, separators=(",", ":"))
        details.update(source_timestamp_text=getattr(parsed, "source_timestamp_text", None),
                       receipt_timestamp_text=getattr(parsed, "receipt_timestamp_text", None))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), details

    def _record_pending_ws_trade(self, parsed: Any, *, match_reason: str | None = None) -> str:
        """Durably retain an unresolved WebSocket/public-tape ambiguity."""

        evidence_id, details = self._ws_trade_evidence(parsed)
        if self._mutations_blocked():
            return evidence_id
        if match_reason is not None:
            self._record_trade_evidence(
                code=match_reason, event_key=f"trade-match-result:{evidence_id}:{match_reason}",
                details={"evidence_id": evidence_id, "match_reason": match_reason},
            )
        event_key = f"trade-public-match-pending:{evidence_id}"
        if self.ledger.event_seen(event_key):
            self.pending_ws_trade_evidence.setdefault(
                evidence_id, {"evidence_id": evidence_id, **details}
            )
            return evidence_id
        self._record_decision(
            event_key=event_key,
            decision="trade_evidence_pending",
            details={
                "evidence_id": evidence_id,
                "reason_code": "UNKNOWN_TRADE_PUBLIC_MATCH_PENDING",
                **details,
                "execution_enabled": False,
            },
        )
        if self.ledger.event_seen(event_key):
            self.pending_ws_trade_evidence[evidence_id] = {
                "evidence_id": evidence_id,
                **details,
            }
        return evidence_id

    def _resolve_pending_ws_trade(self, evidence_id: str, *, resolved_by: str,
                                  validated_at: str | None = None) -> None:
        if self._mutations_blocked():
            return
        pending = self.pending_ws_trade_evidence.get(evidence_id)
        if pending is None:
            return
        event_key = f"trade-public-match-resolved:{evidence_id}"
        if not self.ledger.event_seen(event_key):
            self._record_decision(
                event_key=event_key,
                decision="trade_evidence_resolved",
                details={
                    "evidence_id": evidence_id,
                    "reason_code": "UNKNOWN_TRADE_PUBLIC_MATCH_RESOLVED",
                    "resolved_by": resolved_by,
                    "validated_at": validated_at,
                    "execution_enabled": False,
                },
            )
        if self.ledger.event_seen(event_key):
            self.pending_ws_trade_evidence.pop(evidence_id, None)

    def _resolve_pending_ws_trades_from_public_tape(
        self, public_trades: Sequence[Any], *, as_of: datetime | None = None,
    ) -> None:
        pending = tuple(self.pending_ws_trade_evidence.items())
        visible = [trade for trade in public_trades
                   if _receipt_visible_by(trade.available_at, as_of or datetime.now(UTC))]
        _, validation = build_shadow_trade_events(
            [self._pending_ws_row(details) for _, details in pending], visible,
            quality_windows=self.quality_windows,
        )
        for (evidence_id, _details), result in zip(pending, validation["matches"], strict=True):
            if result["allowed"]:
                self._resolve_pending_ws_trade(
                    evidence_id, resolved_by="public_trade_tape", validated_at=result["validated_at"],
                )

    @staticmethod
    def _pending_ws_row(details: Mapping[str, Any]) -> MarketWsTrade:
        return MarketWsTrade(
            asset_id=str(details["asset_id"]), market_id=str(details["market_id"]),
            market_slug=details.get("market_slug"), price=Decimal(str(details["price"])),
            size=Decimal(str(details["size"])), side=ShadowSide(str(details["side"])),
            source_timestamp=datetime.fromisoformat(str(details["source_timestamp"])),
            received_at=datetime.fromisoformat(str(details["received_at"])),
            transaction_hash=details.get("transaction_hash"), sequence=details.get("sequence"),
            run_id=details.get("run_id"), upstream_status=str(details.get("upstream_status") or "unknown"),
            upstream_incident_id=details.get("upstream_incident_id"),
            source=str(details.get("source") or "market_ws"),
            source_timestamp_text=details.get("source_timestamp_text"),
            receipt_timestamp_text=details.get("receipt_timestamp_text"),
        )

    def _set_feed_continuity(self, state: str, *, as_of: datetime) -> None:
        if self._mutations_blocked():
            return
        normalized = "verified" if state == "verified" else "unknown"
        if self.feed_continuity == normalized:
            return
        self.feed_continuity = normalized
        self._record_decision(
            event_key=f"feed-continuity:{normalized}:{as_of.astimezone(UTC).isoformat()}",
            decision="feed_continuity",
            details={
                "state": normalized,
                "as_of": as_of.astimezone(UTC).isoformat(),
                "execution_enabled": False,
            },
        )

    def _evidence_details(
        self,
        evidence: PaperWeatherEvidence | None,
        *,
        decision_timestamp: datetime,
        reason: str,
        allowed: bool,
    ) -> dict[str, Any]:
        return {
            "observation_id": evidence.observation_id if evidence is not None else None,
            "source_timestamp": evidence.source_timestamp.isoformat() if evidence is not None else None,
            "received_at": evidence.received_at.isoformat() if evidence is not None else None,
            "decision_timestamp": decision_timestamp.astimezone(UTC).isoformat(),
            "gate": "paper_weather_evidence",
            "allowed": allowed,
            "reason_code": reason,
            "strategy_version": self.strategy.version,
            "config_hash": self.strategy.config_sha256,
            "execution_enabled": False,
        }

    @staticmethod
    def _strictly_newer(
        evidence: PaperWeatherEvidence,
        state: PaperPortfolioState,
    ) -> bool:
        if state.last_consumed_source_timestamp is None or state.last_consumed_received_at is None:
            return True
        prior = (
            state.last_consumed_source_timestamp,
            state.last_consumed_received_at,
            state.last_consumed_observation_id or "",
        )
        return evidence.ordering_key > prior

    @_paper_mutation_boundary(None)
    def _paper_buy(
        self,
        snapshot: BookSnapshot,
        *,
        tranche_index: int,
        evidence: PaperWeatherEvidence | None,
        gate_reason: str,
    ) -> ShadowOrder | None:
        key, state, engine = snapshot.portfolio_key, self._state(snapshot.portfolio_key), self._engine(snapshot)
        tranche = self.strategy.tranches[tranche_index]
        quote = quote_limit(snapshot, side=ShadowSide.BUY, mode=self.strategy.quote_mode)
        logical_key = f"paper-tranche:{key.identifier}:{tranche_index}:{snapshot.timestamp.isoformat()}"
        decision_details = {
            "portfolio_key": key.as_dict(),
            "tranche_index": tranche_index,
            "gate": tranche.gate,
            "config_sha256": self.strategy.config_sha256,
            "strategy_version": self.strategy.version,
            **self._evidence_details(
                evidence,
                decision_timestamp=snapshot.timestamp,
                reason=gate_reason,
                allowed=False,
            ),
        }
        rejection: str | None = None
        if self.is_halted or state.stranded or state.closed:
            rejection = "paper_halted_or_portfolio_closed"
        elif self.supervisor_integrity != "verified":
            rejection = "supervisor_evidence_unreadable"
        elif snapshot.event_id not in self.supervisor_active_events:
            rejection = "event_not_active_in_verified_supervisor"
        elif self.quality_integrity != "verified":
            rejection = "quality_evidence_unreadable"
        elif self.new_orders_blocked_reasons:
            # Preserve the explicit supervisor/quality causes above. The
            # remaining blocks are independent cursor/policy/checkpoint
            # evidence failures and intentionally share the generic code.
            rejection = "upstream_evidence_unreadable"
        elif self.feed_continuity != "verified":
            rejection = "feed_continuity_unknown"
        elif quote is None or not snapshot.health_ok:
            rejection = "halted_or_health_or_quote"
        elif tranche.usd > self.account.available_cash_usd or tranche.usd > self._station_day_available(snapshot):
            rejection = "global_or_station_day_budget"
        if rejection is not None:
            self._record_decision(
                event_key=logical_key,
                decision="tranche_rejected",
                details={**decision_details, "reason_code": rejection},
            )
            return None
        order_id = self._stable_order_id(logical_key)
        transition_id = f"submit:{order_id}"
        reserved_notional = tranche.usd / quote * quote
        reserve_details = {
            "portfolio_key": key.as_dict(),
            "order_id": order_id,
            "notional_usd": str(reserved_notional),
        }
        self.ledger.begin_transition(
            transition_id=transition_id,
            event="reserve_buy",
            details=reserve_details,
            before_account=self.account.as_dict(),
        )
        try:
            order = engine.submit_limit(
                snapshot,
                side=ShadowSide.BUY,
                limit_price=quote,
                size_usd=tranche.usd,
                idempotency_key=logical_key,
                order_id=order_id,
                strategy_version=self.strategy.version,
                trigger_reason=f"paper:{tranche.gate}",
                metadata={
                    "paper_v1": True,
                    "config_sha256": self.strategy.config_sha256,
                    "observation_id": evidence.observation_id if evidence else None,
                    "source_timestamp": evidence.source_timestamp.isoformat() if evidence else None,
                    "received_at": evidence.received_at.isoformat() if evidence else None,
                    "supervisor_generation": self.supervisor_generation,
                },
            )
        except (ShadowOrderRejected, ValueError) as exc:
            self.ledger.abort_transition(transition_id=transition_id, reason=type(exc).__name__)
            self._record_decision(
                event_key=logical_key,
                decision="tranche_rejected",
                details={**decision_details, "reason_code": type(exc).__name__},
            )
            return None
        try:
            record_account_action(
                self.ledger,
                self.account,
                event_key=transition_id,
                event="reserve_buy",
                details=reserve_details,
            )
        except (PaperLedgerIntegrityError, ValueError) as exc:
            self._halt("paper_submit_account_transition_failed", details={"error": str(exc)})
            return None
        state.tranche_index = max(state.tranche_index, tranche_index + 1)
        if order.station_id:
            self.station_day_buy_cost.setdefault((order.station_id, order.market_day), ZERO)
        self._record_decision(
            event_key=logical_key,
            decision="tranche_submitted",
            details={
                **decision_details,
                **self._evidence_details(
                    evidence,
                    decision_timestamp=snapshot.timestamp,
                    reason=gate_reason,
                    allowed=True,
                ),
            },
        )
        if evidence is not None:
            self._consume_observation(snapshot, state, evidence)
        return order

    def _weather_gate(
        self,
        evidence: PaperWeatherEvidence | None,
        state: PaperPortfolioState,
        *,
        required_improving: bool,
    ) -> tuple[bool, str]:
        if evidence is None:
            return False, "weather_evidence_missing"
        if evidence.observation_id in state.consumed_observations:
            return False, "weather_observation_already_consumed"
        if not evidence.observation_new:
            return False, "weather_observation_not_new"
        if not self._strictly_newer(evidence, state):
            return False, "weather_observation_not_strictly_newer"
        if required_improving and not evidence.improving:
            return False, "weather_not_improving"
        if evidence.worsening:
            return False, "weather_worsening"
        return True, "weather_confirmation_accepted"

    def _consume_observation(
        self,
        snapshot: BookSnapshot,
        state: PaperPortfolioState,
        evidence: PaperWeatherEvidence,
    ) -> None:
        state.consumed_observations.add(evidence.observation_id)
        state.last_consumed_source_timestamp = evidence.source_timestamp
        state.last_consumed_received_at = evidence.received_at
        state.last_consumed_observation_id = evidence.observation_id
        self._record_decision(
            event_key=f"observation:{snapshot.portfolio_key.identifier}:{evidence.observation_id}",
            decision="observation_consumed",
            details={
                "portfolio_key": snapshot.portfolio_key.as_dict(),
                "observation_id": evidence.observation_id,
                "source_timestamp": evidence.source_timestamp.isoformat(),
                "received_at": evidence.received_at.isoformat(),
                "decision_timestamp": snapshot.timestamp.isoformat(),
                "gate": "paper_weather_evidence",
                "reason_code": "weather_observation_consumed",
                "strategy_version": self.strategy.version,
                "config_hash": self.strategy.config_sha256,
                "execution_enabled": False,
            },
        )

    @_paper_mutation_boundary(None)
    def process_snapshot(self, snapshot: BookSnapshot) -> ShadowOrder | None:
        """Apply one token-native book using only production join metadata."""

        if self._mutations_blocked():
            return None
        if not self._advance_capital_clock(snapshot.timestamp):
            return None
        engine, state = self._engine(snapshot), self._state(snapshot.portfolio_key)
        self.latest_snapshots[snapshot.portfolio_key] = snapshot
        evidence, evidence_reason = PaperWeatherEvidence.parse(snapshot)
        if evidence is None:
            self.trade_evidence_counts[evidence_reason] += 1
        if state.stranded or state.closed or self.is_halted:
            return None
        if self._quality_excludes(snapshot.timestamp):
            self.close_portfolio(
                snapshot, reason="quality_window_active", as_of=snapshot.timestamp
            )
            self._record_tranche_rejection(
                snapshot, state.tranche_index, evidence, "quality_window_active"
            )
            return None
        if evidence is not None and evidence.worsening:
            self.close_portfolio(snapshot, reason="weather_worsening", as_of=snapshot.timestamp)
            return None
        if not snapshot.health_ok:
            reason = (
                "season_or_rule_invalid"
                if not snapshot.in_season or not snapshot.warming_valid or not snapshot.season_version
                else "health_gate"
            )
            self.close_portfolio(snapshot, reason=reason, as_of=snapshot.timestamp)
            return None
        before = {
            order.order_id: (order.state, order.remaining_shares, order.cancelled_at, order.expired_at)
            for order in engine.orders
        }
        engine.process_snapshot(snapshot)
        self._sync_terminal_orders(engine, before)
        exit_order = self._submit_exit_if_eligible(snapshot)
        if exit_order is not None:
            return exit_order
        index = state.tranche_index
        if index >= len(self.strategy.tranches) or engine.active_orders:
            return None
        if index == 0:
            if (
                evidence is None
                or not evidence.market_lag
                or not evidence.improving
                or not self._within_entry_band(snapshot)
            ):
                self._record_tranche_rejection(snapshot, index, evidence, evidence_reason)
                return None
            return self._paper_buy(
                snapshot,
                tranche_index=index,
                evidence=evidence,
                gate_reason="initial_weather_market_lag",
            )
        prior_filled = bool(engine.inventory_shares)
        prior_terminated = not any(
            order.side is ShadowSide.BUY and order.is_active for order in engine.orders
        )
        if not prior_filled or not prior_terminated:
            return None
        if index == 1 and 0 in state.filled_tranches:
            allowed, reason = self._weather_gate(evidence, state, required_improving=True)
            if allowed:
                return self._paper_buy(
                    snapshot,
                    tranche_index=index,
                    evidence=evidence,
                    gate_reason=reason,
                )
            self._record_tranche_rejection(snapshot, index, evidence, reason)
            return None
        if index == 2:
            latest_buy = max(
                (
                    fill
                    for order in engine.orders
                    if order.side is ShadowSide.BUY
                    for fill in order.fills
                ),
                key=lambda fill: fill.timestamp,
                default=None,
            )
            quote = quote_limit(snapshot, side=ShadowSide.BUY, mode=self.strategy.quote_mode)
            stable_weather = evidence is not None and (evidence.unchanged or evidence.improving) and not evidence.worsening
            if latest_buy and quote is not None and quote <= latest_buy.price - snapshot.tick_size * 2 and stable_weather:
                return self._paper_buy(
                    snapshot,
                    tranche_index=index,
                    evidence=evidence,
                    gate_reason="conditional_dip_confirmed",
                )
            self._record_tranche_rejection(snapshot, index, evidence, "conditional_dip_not_confirmed")
            return None
        if index == 3 and 2 in state.filled_tranches:
            allowed, reason = self._weather_gate(evidence, state, required_improving=True)
            average = self._account_average_cost(snapshot.portfolio_key)
            if (
                allowed
                and snapshot.best_bid is not None
                and average is not None
                and self._price_reaches_target(snapshot.best_bid, average)
            ):
                return self._paper_buy(
                    snapshot,
                    tranche_index=index,
                    evidence=evidence,
                    gate_reason=reason,
                )
            self._record_tranche_rejection(
                snapshot,
                index,
                evidence,
                reason if not allowed else "fourth_tranche_bid_below_average_cost",
            )
            return None
        self._submit_exit_if_eligible(snapshot)
        return None

    def _account_average_cost(self, key: TokenPortfolioKey) -> Decimal | None:
        return self.account.positions.get(key, None).average_cost if key in self.account.positions else None

    @staticmethod
    def _price_reaches_target(price: Decimal, target: Decimal) -> bool:
        return price >= target or target - price <= PRICE_COMPARISON_EPSILON

    @staticmethod
    def _exit_target_price(*, average: Decimal, rise: Decimal, tick_size: Decimal) -> Decimal:
        """Round a required exit threshold *up* to the native price grid.

        A price target between ticks must not be rounded down: doing so would
        turn an intended profit threshold into a lower executable threshold.
        The tiny tolerance is limited to Decimal representation noise from
        fractional-share average-cost arithmetic (for example, 0.88 plus a
        final 1e-28 digit), not a market-price tolerance.
        """

        raw = average + rise
        units = (raw / tick_size).to_integral_value(rounding=ROUND_FLOOR)
        floor_price = units * tick_size
        if raw - floor_price <= PRICE_COMPARISON_EPSILON:
            return floor_price
        return (units + 1) * tick_size

    def _within_entry_band(self, snapshot: BookSnapshot) -> bool:
        ask = snapshot.best_ask
        return ask is not None and any(
            lower <= ask <= upper
            for lower, upper in self.strategy.entry_bands.get(snapshot.station_id or "", ())
        )

    def _record_tranche_rejection(
        self,
        snapshot: BookSnapshot,
        tranche_index: int,
        evidence: PaperWeatherEvidence | None,
        reason: str,
    ) -> None:
        key = snapshot.portfolio_key
        event_key = f"paper-tranche-reject:{key.identifier}:{tranche_index}:{snapshot.timestamp.isoformat()}"
        self._record_decision(
            event_key=event_key,
            decision="tranche_rejected",
            details={
                "portfolio_key": key.as_dict(),
                "tranche_index": tranche_index,
                "gate": self.strategy.tranches[tranche_index].gate,
                "config_sha256": self.strategy.config_sha256,
                **self._evidence_details(
                    evidence,
                    decision_timestamp=snapshot.timestamp,
                    reason=reason,
                    allowed=False,
                ),
            },
        )

    def _sync_terminal_orders(
        self,
        engine: ShadowOrderEngine,
        before: Mapping[str, tuple[ShadowOrderState, Decimal, datetime | None, datetime | None]],
    ) -> None:
        """Make every snapshot/sweep terminal BUY release the same residual."""

        for order in engine.orders:
            previous = before.get(order.order_id)
            if previous is None or previous[0] not in {ShadowOrderState.RESTING, ShadowOrderState.PARTIALLY_FILLED}:
                continue
            if order.is_active or order.side is not ShadowSide.BUY:
                continue
            residual = order.remaining_shares * order.limit_price
            if residual <= ZERO:
                continue
            transition_id = self._terminal_transition_id(order)
            if transition_id is None:
                self._halt("terminal_order_missing_timestamp", details={"order_id": order.order_id})
                continue
            try:
                record_account_action(
                    self.ledger,
                    self.account,
                    event_key=transition_id,
                    event="release_buy",
                    details={
                        "portfolio_key": order.portfolio_key.as_dict(),
                        "order_id": order.order_id,
                        "notional_usd": str(residual),
                    },
                )
            except (PaperLedgerIntegrityError, ValueError) as exc:
                self._halt("terminal_buy_release_failed", details={"order_id": order.order_id, "error": str(exc)})

    @staticmethod
    def _trade_identity(trade: TradeEvent) -> str:
        return json.dumps(
            (
                str(trade.event_id or ""),
                str(trade.transaction_hash or trade.event_id or ""),
                trade.asset_id,
                trade.timestamp.isoformat(),
                str(trade.side),
                _exact_decimal_text(trade.price),
                _exact_decimal_text(trade.size),
            ), separators=(",", ":"),
        )

    @staticmethod
    def _durable_trade_event_key(trade: TradeEvent) -> str | None:
        """Match the queue engine's persisted trade idempotency key.

        This lets a restarted Paper processor label a replayed public trade
        as duplicate evidence before it can be grouped as a fresh event.  The
        key deliberately excludes transport source so a matching Data API and
        WebSocket tape row cannot consume queue twice.
        """

        if not trade.event_id:
            return None
        return "paper-trade-v2:" + hashlib.sha256(
            PaperSpreadProcessor._trade_identity(trade).encode("utf-8")
        ).hexdigest()

    def _observe_trade_identity(self, trade: TradeEvent, *, ambiguous: bool = False) -> bool:
        """Persist source aliases before use; only the order save consumes them."""
        group = self._trade_group(trade)
        identity = self._trade_identity(trade)
        observations = self.trade_observations[group]
        receipt = trade.available_at.isoformat() if trade.available_at else None
        strong_row_id = bool(trade.transaction_hash and trade.event_id != trade.transaction_hash)
        conflict = ambiguous or any(
            row["identity"] != identity
            or (row["sequence"] is not None and trade.sequence is not None
                and row["sequence"] != trade.sequence)
            or (not strong_row_id and row["source"] == trade.source
                and row["available_at"] != receipt)
            for row in observations
        )
        details = {"group": group, "identity": identity, "source": trade.source,
                   "sequence": trade.sequence,
                   "available_at": receipt,
                   "conflict": conflict, "execution_enabled": False}
        event_key = "paper-trade-observation-v2:" + hashlib.sha256(
            json.dumps(details, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not self.ledger.event_seen(event_key):
            self._record_decision(event_key=event_key, decision="trade_identity_observation_v2",
                                  details=details)
            observations.append(details)
            self._index_time_group(details)
        if conflict or any(row.get("conflict") for row in observations):
            self._record_trade_evidence(
                code="UNKNOWN_TRADE_IDENTITY_CONFLICT", event_key=f"identity-conflict:{group}",
                details={"group": group},
            )
            return False
        return True

    def _index_time_group(self, observation: Mapping[str, Any]) -> None:
        """Rebuild membership from the existing append-only identity journal."""
        identity = json.loads(str(observation["identity"]))
        timestamp = datetime.fromisoformat(identity[3]).astimezone(UTC).replace(microsecond=0)
        self.trade_time_groups[(identity[2], timestamp)].append(dict(observation))

    def _time_group_is_unknown(self, trade: TradeEvent) -> bool:
        timestamp = trade.timestamp.astimezone(UTC).replace(microsecond=0)
        key = (trade.asset_id, timestamp)
        rows = self.trade_time_groups[key]
        by_sequence: dict[int, set[str]] = defaultdict(set)
        sequenced_identities: set[str] = set()
        for row in rows:
            if row["sequence"] is not None:
                by_sequence[row["sequence"]].add(row["identity"])
                sequenced_identities.add(row["identity"])
        unknown = any(row["identity"] not in sequenced_identities for row in rows) or any(
            len(identities) > 1 for identities in by_sequence.values()
        )
        if not unknown:
            return False
        self._record_trade_evidence(
            code="UNKNOWN_TRADE_SEQUENCE",
            event_key=f"unknown-trade-sequence:{key[0]}:{timestamp.isoformat()}",
            details={"token_id": key[0], "timestamp": timestamp.isoformat(),
                     "group_complete": False, "reason": "persistent_unsequenced_or_conflicting_group"},
        )
        # Old candidates may have consumed an apparently-singleton group.
        # Append an invalidation, never rewrite their economic facts.
        self._invalidate_time_group_consumptions(key[0], timestamp)
        return True

    def _invalidate_time_group_consumptions(self, token_id: str, timestamp: datetime) -> None:
        for row in self.trade_time_groups[(token_id, timestamp)]:
            durable_key = "paper-trade-v2:" + hashlib.sha256(row["identity"].encode("utf-8")).hexdigest()
            if self.ledger.event_seen(durable_key):
                self._record_trade_evidence(
                    code="UNKNOWN_TRADE_GROUP_INVALIDATED",
                    event_key=f"invalidated-time-group:{durable_key}",
                    details={"durable_trade_key": durable_key, "group_complete": False},
                )

    @staticmethod
    def _trade_group(trade: TradeEvent) -> str:
        return json.dumps([trade.transaction_hash or trade.event_id, trade.asset_id,
                           trade.event_id if trade.transaction_hash and trade.event_id != trade.transaction_hash else None])

    def _record_fills(
        self,
        *,
        key: TokenPortfolioKey,
        engine: ShadowOrderEngine,
        fills: Sequence[ShadowFill],
    ) -> None:
        for fill in fills:
            order = next(order for order in engine.orders if order.order_id == fill.order_id)
            if order.side is ShadowSide.BUY:
                details = {
                    "portfolio_key": key.as_dict(),
                    "order_id": order.order_id,
                    "fill_id": fill.fill_id,
                    "shares": str(fill.shares),
                    "price": str(fill.price),
                    "reserved_notional_usd": str(fill.shares * order.limit_price),
                    "fee_usd": str(fill.fee_usd),
                }
                try:
                    record_account_action(
                        self.ledger,
                        self.account,
                        event_key=f"fill:{fill.fill_id}",
                        event="fill_buy",
                        details=details,
                    )
                except (PaperLedgerIntegrityError, ValueError) as exc:
                    self._halt("paper_buy_fill_account_transition_failed", details={"fill_id": fill.fill_id, "error": str(exc)})
                    return
                state = self._state(key)
                tranche_index = next(
                    (
                        index
                        for index, tranche in enumerate(self.strategy.tranches)
                        if order.trigger_reason == f"paper:{tranche.gate}"
                    ),
                    None,
                )
                if tranche_index is not None:
                    state.filled_tranches.add(tranche_index)
                    self._record_decision(
                        event_key=f"tranche-filled:{fill.fill_id}",
                        decision="tranche_filled",
                        details={
                            "portfolio_key": key.as_dict(),
                            "tranche_index": tranche_index,
                            "fill_id": fill.fill_id,
                            "execution_enabled": False,
                        },
                    )
                state.cumulative_bought_shares += fill.shares
                self._record_decision(
                    event_key=f"cumulative-bought:{fill.fill_id}",
                    decision="cumulative_bought_shares",
                    details={
                        "portfolio_key": key.as_dict(),
                        "fill_id": fill.fill_id,
                        "cumulative_bought_shares": str(state.cumulative_bought_shares),
                        "execution_enabled": False,
                    },
                )
                if state.position_opened_at is None:
                    state.position_opened_at = fill.timestamp
                    self._record_decision(
                        event_key=f"position-opened:{key.identifier}",
                        decision="position_opened",
                        details={
                            "portfolio_key": key.as_dict(),
                            "position_opened_at": fill.timestamp.isoformat(),
                            "execution_enabled": False,
                        },
                    )
                station = order.station_id
                if station:
                    day_key = (station, key.market_day)
                    self.station_day_buy_cost[day_key] = self.station_day_buy_cost.get(day_key, ZERO) + fill.shares * fill.price + fill.fee_usd
            else:
                try:
                    record_account_action(
                        self.ledger,
                        self.account,
                        event_key=f"fill:{fill.fill_id}",
                        event="fill_sell",
                        details={
                            "portfolio_key": key.as_dict(),
                            "order_id": order.order_id,
                            "fill_id": fill.fill_id,
                            "shares": str(fill.shares),
                            "price": str(fill.price),
                            "fee_usd": str(fill.fee_usd),
                        },
                    )
                except (PaperLedgerIntegrityError, ValueError) as exc:
                    self._halt("paper_sell_fill_account_transition_failed", details={"fill_id": fill.fill_id, "error": str(exc)})
                    return
                stage_value = order.metadata.get("exit_stage")
                if stage_value is None:
                    continue
                state = self._state(key)
                stage_index = int(stage_value)
                state.exit_stage_filled_shares[stage_index] = state.exit_stage_filled_shares.get(stage_index, ZERO) + fill.shares
                self._record_decision(
                    event_key=f"exit-stage-fill:{fill.fill_id}",
                    decision="exit_stage_fill",
                    details={
                        "portfolio_key": key.as_dict(),
                        "stage_index": stage_index,
                        "shares": str(fill.shares),
                        "fill_id": fill.fill_id,
                        "execution_enabled": False,
                    },
                )
                target = self._exit_stage_target(engine, state, stage_index)
                if stage_index == len(self.strategy.exits) - 1:
                    completed = engine.inventory_shares <= ZERO
                else:
                    completed = state.exit_stage_filled_shares[stage_index] >= target
                if completed and stage_index not in state.completed_exit_stages:
                    state.completed_exit_stages.add(stage_index)
                    self._record_decision(
                        event_key=f"exit-stage-complete:{key.identifier}:{stage_index}",
                        decision="exit_stage_filled",
                        details={
                            "portfolio_key": key.as_dict(),
                            "stage_index": stage_index,
                            "execution_enabled": False,
                        },
                    )

    @_paper_mutation_boundary(())
    def process_trade(self, trade: TradeEvent) -> tuple[ShadowFill, ...]:
        return self.process_trades((trade,))

    @_paper_mutation_boundary(())
    def process_trades(self, trades: Sequence[TradeEvent]) -> tuple[ShadowFill, ...]:
        """Journal public observations; unknown completeness never authorizes effects.

        Neither the public API nor the archived WS transport sequence proves
        token/second group closure. There is intentionally no boolean/JSON
        escape hatch. A future completeness protocol needs its own reviewed
        producer and verifier before this entry can invoke the model kernel.
        """
        if self._mutations_blocked():
            return ()
        counts = Counter((trade.event_id, trade.asset_id, trade.source) for trade in trades)
        for trade in trades:
            self._observe_trade_identity(
                trade, ambiguous=counts[(trade.event_id, trade.asset_id, trade.source)] > 1,
            )
        for trade in trades:
            if self._mutations_blocked():
                return ()
            self._time_group_is_unknown(trade)
            timestamp = trade.timestamp.astimezone(UTC).replace(microsecond=0)
            self._record_trade_evidence(
                code="UNKNOWN_TRADE_GROUP_COMPLETENESS",
                event_key=f"unproven-time-group:{trade.asset_id}:{timestamp.isoformat()}",
                details={"token_id": trade.asset_id, "timestamp": timestamp.isoformat(),
                         "group_complete": False, "ordering_is_not_closure": True,
                         "support_state": "UNSUPPORTED_GROUP_COMPLETENESS"},
            )
            self._invalidate_time_group_consumptions(trade.asset_id, timestamp)
        return ()

    @_paper_mutation_boundary(())
    def _process_ordered_model_trades(self, trades: Sequence[TradeEvent]) -> tuple[ShadowFill, ...]:
        """Non-authoritative economic kernel, not a public-evidence admission API.

        Kept for isolated lower-layer accounting/recovery conformance tests.
        Current production ingress never calls this kernel: complete-group
        evidence is unavailable. Model fixtures cannot establish eligibility.
        """

        if self._mutations_blocked():
            return ()
        grouped: dict[tuple[str, datetime], list[TradeEvent]] = defaultdict(list)
        # Preflight the whole batch so a later sibling cannot invalidate an
        # already consumed earlier row in this same batch.
        observation_counts = Counter((trade.event_id, trade.asset_id, trade.source) for trade in trades)
        admitted = [trade for trade in trades if self._observe_trade_identity(
            trade, ambiguous=observation_counts[(trade.event_id, trade.asset_id, trade.source)] > 1
        )]
        for trade in admitted:
            if self._time_group_is_unknown(trade):
                continue
            group_key = self._trade_group(trade)
            if any(row.get("conflict") for row in self.trade_observations[group_key]):
                continue
            trade = replace(trade, consumption_key=self._durable_trade_event_key(trade))
            identity = self._trade_identity(trade)
            if identity in self._seen_trade_identities:
                self._record_trade_evidence(
                    code="duplicate_trade",
                    event_key=f"duplicate-trade:{identity}",
                    details={"trade_identity": identity},
                )
                continue
            durable_key = self._durable_trade_event_key(trade)
            if durable_key is None:
                self._record_trade_evidence(
                    code="UNKNOWN_TRADE_IDENTITY",
                    event_key=f"unknown-trade-identity:{identity}",
                    details={"trade_identity": identity},
                )
                continue
            matching = [
                engine
                for key, engine in self.engines.items()
                if key.token_id == trade.asset_id
            ]
            if (
                durable_key is not None
                and len(matching) == 1
                and matching[0].ledger is not None
                and matching[0].ledger.event_seen(durable_key)
            ):
                self._record_trade_evidence(
                    code="duplicate_trade",
                    event_key=f"duplicate-durable-trade:{durable_key}",
                    details={"trade_identity": identity, "durable_trade_key": durable_key},
                )
                continue
            self._seen_trade_identities.add(identity)
            grouped[(trade.asset_id, trade.timestamp)].append(trade)
        fills: list[ShadowFill] = []
        for (token_id, timestamp), group in sorted(grouped.items(), key=lambda item: item[0]):
            if len(group) > 1 and any(trade.sequence is None for trade in group):
                self._record_trade_evidence(
                    code="UNKNOWN_TRADE_SEQUENCE",
                    event_key=f"unknown-trade-sequence:{token_id}:{timestamp.isoformat()}",
                    details={
                        "token_id": token_id,
                        "timestamp": timestamp.isoformat(),
                    },
                    count=len(group),
                )
                continue
            matching = [(key, engine) for key, engine in self.engines.items() if key.token_id == token_id]
            if len(matching) != 1:
                if matching:
                    self._halt("ambiguous_trade_token_portfolio_route", details={"token_id": token_id})
                continue
            key, engine = matching[0]
            eligible_active = tuple(engine.active_orders)
            if len(eligible_active) > 1:
                self._halt(
                    "multiple_eligible_active_orders_for_trade",
                    details={
                        "portfolio_key": key.as_dict(),
                        "token_id": token_id,
                        "order_ids": [order.order_id for order in eligible_active],
                    },
                )
                return tuple(fills)
            ordered = tuple(sorted(group, key=lambda trade: trade.sequence or 0))
            before = {
                order.order_id: (order.state, order.remaining_shares, order.cancelled_at, order.expired_at)
                for order in engine.orders
            }
            # Dependency receipt, not exchange time, determines whether this
            # evidence arrived before the resting order's timeout.
            available = max((trade.available_at for trade in ordered if trade.available_at), default=None)
            if available is not None:
                for order in tuple(engine.active_orders):
                    if available - order.submitted_at >= self.strategy.order_timeout:
                        engine.expire(order.order_id, timestamp=available, reason=ShadowOrderReason.TIMEOUT.value)
            new = engine.process_trades(ordered, reject_ambiguous_same_second=True)
            self._record_fills(key=key, engine=engine, fills=new)
            if self._mutations_blocked():
                return tuple(fills)
            self._sync_terminal_orders(engine, before)
            fills.extend(new)
        return tuple(fills)

    @_paper_mutation_boundary(())
    def sweep_lifecycle(
        self,
        *,
        as_of: datetime,
        closed_event_ids: set[str] | None = None,
    ) -> tuple[ShadowOrder, ...]:
        """Run lifecycle from an explicit continuous/replay event clock."""

        if self._mutations_blocked():
            return ()
        if not self._advance_capital_clock(as_of):
            return ()
        expired: list[ShadowOrder] = []
        closed_event_ids = closed_event_ids or set()
        for key, engine in tuple(self.engines.items()):
            state = self._state(key)
            snapshot = self.latest_snapshots.get(key)
            if key.event_id in closed_event_ids and not state.stranded and not state.closed:
                self.close_portfolio_key(
                    key, reason="market_closed_or_unsubscribed", as_of=as_of
                )
            elif (
                state.position_opened_at is not None
                and not state.stranded
                and not state.closed
                and as_of.astimezone(UTC) - state.position_opened_at >= self.strategy.max_hold
            ):
                if snapshot is not None:
                    self.close_portfolio(snapshot, reason="max_hold", as_of=as_of)
                else:
                    self.close_portfolio_key(key, reason="max_hold_no_snapshot", as_of=as_of)
            if self._mutations_blocked():
                return tuple(expired)
            before = {
                order.order_id: (order.state, order.remaining_shares, order.cancelled_at, order.expired_at)
                for order in engine.orders
            }
            for order in tuple(engine.active_orders):
                if as_of.astimezone(UTC) - order.submitted_at < self.strategy.order_timeout:
                    continue
                updated = engine.expire(order.order_id, timestamp=as_of, reason=ShadowOrderReason.TIMEOUT.value)
                expired.append(updated)
            self._sync_terminal_orders(engine, before)
        return tuple(expired)

    def _mark_closed(self, key: TokenPortfolioKey, *, reason: str) -> None:
        state = self._state(key)
        state.closed = True
        state.close_reason = reason
        self._record_decision(
            event_key=f"portfolio-closed:{key.identifier}:{reason}",
            decision="portfolio_closed",
            details={
                "portfolio_key": key.as_dict(),
                "reason": reason,
                "execution_enabled": False,
            },
        )

    def _strand(self, key: TokenPortfolioKey, *, reason: str) -> None:
        try:
            record_account_action(
                self.ledger,
                self.account,
                event_key=f"strand:{key.identifier}:{reason}",
                event="strand",
                details={"portfolio_key": key.as_dict(), "reason": reason},
            )
        except (PaperLedgerIntegrityError, ValueError) as exc:
            self._halt("paper_strand_transition_failed", details={"error": str(exc)})
            return
        self._state(key).stranded = True
        self._record_decision(
            event_key=f"strand-decision:{key.identifier}:{reason}",
            decision="stranded",
            details={"portfolio_key": key.as_dict(), "reason": reason, "execution_enabled": False},
        )

    def _risk_exit_eligible(
        self, snapshot: BookSnapshot, *, as_of: datetime, key: TokenPortfolioKey
    ) -> bool:
        age_seconds = (as_of.astimezone(UTC) - snapshot.timestamp).total_seconds()
        self.last_risk_exit_book_age_seconds = age_seconds
        if snapshot.portfolio_key != key or age_seconds < 0:
            return False
        if age_seconds > self.strategy.risk_exit_snapshot_age.total_seconds():
            self.trade_evidence_counts["STALE_RISK_EXIT_BOOK"] += 1
            return False
        if not snapshot.health_ok or not snapshot.bids:
            return False
        overlap = excluded_market_data_window_overlaps(
            tuple(self.quality_windows),
            start_at=snapshot.timestamp,
            end_at=as_of,
        )
        if overlap is not None:
            self.trade_evidence_counts["RISK_EXIT_QUALITY_INTERVAL_OVERLAP"] += 1
            return False
        if bool(snapshot.metadata.get("feed_gap")) or str(
            snapshot.metadata.get("feed_continuity") or "verified"
        ).casefold() not in {"verified", "continuous"}:
            self.trade_evidence_counts["UNKNOWN_FEED_GAP"] += 1
            return False
        snapshot_generation = snapshot.metadata.get("supervisor_generation")
        if (
            snapshot_generation is not None
            and self.supervisor_generation is not None
            and str(snapshot_generation) != self.supervisor_generation
        ):
            self.trade_evidence_counts["GENERATION_BOUNDARY_RISK_EXIT"] += 1
            return False
        return True

    @_paper_mutation_boundary(None)
    def close_portfolio_key(self, key: TokenPortfolioKey, *, reason: str, as_of: datetime) -> None:
        if self._mutations_blocked():
            return
        engine = self.engines.get(key)
        if engine is None:
            return
        snapshot = self.latest_snapshots.get(key)
        before = {
            order.order_id: (order.state, order.remaining_shares, order.cancelled_at, order.expired_at)
            for order in engine.orders
        }
        for order in tuple(engine.active_orders):
            engine.cancel(order.order_id, timestamp=as_of, reason=reason)
        self._sync_terminal_orders(engine, before)
        if self._mutations_blocked():
            return
        if engine.inventory_shares <= ZERO:
            self._mark_closed(key, reason=reason)
        elif snapshot is None:
            self._strand(key, reason=reason)
        else:
            self.close_portfolio(snapshot, reason=reason, as_of=as_of)

    @_paper_mutation_boundary(None)
    def close_portfolio(self, snapshot: BookSnapshot, *, reason: str, as_of: datetime | None = None) -> None:
        """Cancel maker exposure then use only a contemporaneous native bid."""

        if self._mutations_blocked():
            return
        timestamp = (as_of or snapshot.timestamp).astimezone(UTC)
        engine, key = self._engine(snapshot), snapshot.portfolio_key
        before = {
            order.order_id: (order.state, order.remaining_shares, order.cancelled_at, order.expired_at)
            for order in engine.orders
        }
        for order in tuple(engine.active_orders):
            engine.cancel(order.order_id, timestamp=timestamp, reason=reason)
        self._sync_terminal_orders(engine, before)
        if self._mutations_blocked():
            return
        if engine.inventory_shares <= ZERO:
            self._mark_closed(key, reason=reason)
            return
        if self._risk_exit_eligible(snapshot, as_of=timestamp, key=key):
            before_fills = {fill.fill_id for order in engine.orders for fill in order.fills}
            fills = engine.simulate_taker_exit(snapshot, reason=reason)
            new = tuple(fill for fill in fills if fill.fill_id not in before_fills)
            self._record_fills(key=key, engine=engine, fills=new)
            if engine.inventory_shares <= ZERO:
                self._mark_closed(key, reason=reason)
                return
        # Missing or insufficient depth deliberately leaves PnL unpriced;
        # current historic cost remains occupied in the account.
        self._strand(key, reason=reason)

    def _exit_stage_target(
        self,
        engine: ShadowOrderEngine,
        state: PaperPortfolioState,
        stage_index: int,
    ) -> Decimal:
        if stage_index == len(self.strategy.exits) - 1:
            return engine.inventory_shares
        # ``tick_size`` is a *price* increment.  It is not evidence of a
        # token-share increment, so using it to round shares would strand
        # artificial dust and violate the final-stage liquidation contract.
        # The archive supplies only a minimum order size; preserve Decimal
        # shares exactly and classify only a genuinely sub-minimum residual.
        return state.cumulative_bought_shares * self.strategy.exits[stage_index].fraction_of_initial_shares

    @_paper_mutation_boundary(None)
    def _submit_exit_if_eligible(self, snapshot: BookSnapshot) -> ShadowOrder | None:
        """Submit one stable exit stage attempt for its unsold residual only."""

        if self._mutations_blocked():
            return None
        engine, state = self._engine(snapshot), self._state(snapshot.portfolio_key)
        average = self._account_average_cost(snapshot.portfolio_key)
        if average is None or engine.active_orders or engine.inventory_shares <= ZERO:
            return None
        for index, stage in enumerate(self.strategy.exits):
            if index in state.completed_exit_stages:
                continue
            price_target = self._exit_target_price(
                average=average,
                rise=stage.rise,
                tick_size=snapshot.tick_size,
            )
            if snapshot.best_bid is None or snapshot.best_bid < price_target:
                continue
            target = self._exit_stage_target(engine, state, index)
            already_filled = state.exit_stage_filled_shares.get(index, ZERO)
            if index == len(self.strategy.exits) - 1:
                residual = engine.inventory_shares
            else:
                residual = min(engine.inventory_shares, max(ZERO, target - already_filled))
            if residual <= ZERO:
                if index != len(self.strategy.exits) - 1:
                    state.completed_exit_stages.add(index)
                continue
            if residual < snapshot.min_order_size:
                if index not in state.exit_stage_dust:
                    state.exit_stage_dust.add(index)
                    self._record_decision(
                        event_key=f"exit-dust:{snapshot.portfolio_key.identifier}:{index}:{target}",
                        decision="exit_stage_dust",
                        details={
                            "portfolio_key": snapshot.portfolio_key.as_dict(),
                            "stage_index": index,
                            "target_shares": str(target),
                            "residual_shares": str(residual),
                            "reason_code": "exit_dust_or_min_order_unexecutable",
                            "execution_enabled": False,
                        },
                    )
                return None
            attempt = state.exit_stage_attempts.get(index, 0) + 1
            logical_identity = f"paper-exit:{snapshot.portfolio_key.identifier}:{index}"
            attempt_identity = f"{logical_identity}:attempt:{attempt}"
            try:
                order = engine.submit_maker_exit(
                    snapshot,
                    target_price=price_target,
                    shares=residual,
                    idempotency_key=attempt_identity,
                    strategy_version=self.strategy.version,
                    trigger_reason=f"paper_exit_stage_{index}",
                    metadata={
                        "paper_v1": True,
                        "exit_stage": index,
                        "exit_stage_identity": logical_identity,
                        "exit_attempt": attempt,
                        "exit_target_shares": str(target),
                        "config_sha256": self.strategy.config_sha256,
                    },
                )
            except (ShadowOrderRejected, ValueError):
                return None
            state.exit_stage_attempts[index] = attempt
            self._record_decision(
                event_key=f"exit-attempt:{attempt_identity}",
                decision="exit_stage_attempt",
                details={
                    "portfolio_key": snapshot.portfolio_key.as_dict(),
                    "stage_index": index,
                    "attempt": attempt,
                    "logical_identity": logical_identity,
                    "attempt_identity": attempt_identity,
                    "target_shares": str(target),
                    "residual_shares": str(residual),
                    "execution_enabled": False,
                },
            )
            return order
        return None

    def _trade_evidence_summary(self) -> dict[str, Any]:
        consumed = {
            key: value for order in self.ledger.orders.values()
            for key, value in order.metadata.get("paper_trade_consumptions_v2", {}).items()
        }
        observations = [row for rows in self.trade_observations.values() for row in rows]
        sibling_groups = Counter((row.get("transaction_hash"), row["asset_id"])
                                 for row in consumed.values() if row.get("transaction_hash"))
        return {
            "scope": "durable distinct evidence records; raw arrivals are reported per follower cycle",
            "source_alias_count": len(observations),
            "economic_candidate_count": len({row["identity"] for row in observations}),
            "consumed_economic_event_count": len(consumed),
            "duplicate_evidence_record_count": self.trade_evidence_counts.get("duplicate_trade", 0),
            "legal_sibling_event_count": sum(count for count in sibling_groups.values() if count > 1),
            "pending_observation_count": len(self.pending_ws_trade_evidence),
            "time_group_count": len(self.trade_time_groups),
            "group_completeness": "unproven" if self.trade_time_groups else "no_observations",
            "conflict_group_count": sum(any(row["conflict"] for row in rows)
                                        for rows in self.trade_observations.values()),
            "queue_shares_consumed": str(sum((Decimal(row["queue_shares"]) for row in consumed.values()), ZERO)),
            "fill_shares_consumed": str(sum((Decimal(row["fill_shares"]) for row in consumed.values()), ZERO)),
        }

    def status(
        self,
        *,
        as_of: datetime | None = None,
        cursor: ShadowCursor | None = None,
        score_started_at: str | None = None,
        git_commit: str | None = None,
        last_error: str | None = None,
        cursor_integrity: str = "verified",
        status_integrity: str = "verified",
    ) -> dict[str, Any]:
        active = [order for engine in self.engines.values() for order in engine.active_orders]
        checked_at = (as_of or datetime.now(UTC)).astimezone(UTC)
        overdue = sum(checked_at - order.submitted_at >= self.strategy.order_timeout for order in active)
        scan = execution_dependency_scan()
        reasons: list[str] = []
        if self.is_halted:
            reasons.append("halted")
        if any(row.get("conflict") for rows in self.trade_observations.values() for row in rows):
            reasons.append("unknown_trade_identity_conflict")
        if self.trade_evidence_counts.get("UNKNOWN_TRADE_SEQUENCE", 0):
            reasons.append("unknown_trade_group_order_or_completeness")
        if self.trade_time_groups:
            reasons.append("unproven_trade_group_completeness")
        if self.config_mismatch:
            reasons.append("config_mismatch")
        if overdue:
            reasons.append("overdue_orders")
        if scan["status"] != "clear":
            reasons.append("forbidden_execution_modules")
        if self.ledger.invalid:
            reasons.append("paper_ledger_invalid")
        if self.recovery.get("unfinished_transitions"):
            reasons.append("unfinished_transition")
        if cursor_integrity != "verified":
            reasons.append("cursor_integrity_failure")
        if status_integrity != "verified":
            reasons.append("status_integrity_failure")
        if self.checkpoint_integrity in {"failed", "ahead_of_ledger"}:
            reasons.append("checkpoint_integrity_failure")
        if self.supervisor_integrity != "verified":
            reasons.append("supervisor_evidence_unreadable")
        if self.quality_integrity != "verified":
            reasons.append("quality_evidence_unreadable")
        if self.feed_continuity != "verified":
            reasons.append("feed_continuity_unknown")
        if self.pending_ws_trade_evidence:
            reasons.append("unresolved_ws_public_trade_evidence")
        if self.last_risk_exit_book_age_seconds is not None and (
            self.last_risk_exit_book_age_seconds < 0
            or self.last_risk_exit_book_age_seconds
            > self.strategy.risk_exit_snapshot_age.total_seconds()
        ):
            reasons.append("stale_snapshot_risk_exit")
        reasons.extend(sorted(self.new_orders_blocked_reasons - {"quality_evidence_unreadable", "supervisor_evidence_unreadable"}))
        oldest = min((order.submitted_at for order in active), default=None)
        fills = [fill for engine in self.engines.values() for order in engine.orders for fill in order.fills]
        orders = [order for engine in self.engines.values() for order in engine.orders]
        token_detail = {
            key.identifier: {
                "shares": str(position.shares),
                "cost_usd": str(position.cost_usd),
                "stranded": position.stranded,
            }
            for key, position in self.account.positions.items()
        }
        station_detail = {
            f"{station}:{day}": {"cumulative_buy_cost_usd": str(cost)}
            for (station, day), cost in self.station_day_buy_cost.items()
        }
        utilized = (
            self.account.inventory_cost_usd
            + self.account.stranded_inventory_cost_usd
            + self.account.buy_reserved_usd
        )
        active_quality_window = quality_window_at(tuple(self.quality_windows), checked_at)
        current_risk_book_ages = [
            (checked_at - snapshot.timestamp).total_seconds()
            for key, snapshot in self.latest_snapshots.items()
            if self.engines.get(key) is not None
            and self.engines[key].inventory_shares > ZERO
        ]
        observed_upstream_statuses = sorted(
            {snapshot.upstream_status for snapshot in self.latest_snapshots.values()}
        )
        recovery = {**self.ledger.recovery_status(), **self.recovery}
        return {
            "execution_enabled": False,
            "trade_identity_version": 2,
            "trade_evidence_summary": self._trade_evidence_summary(),
            "business_readiness": self.business_readiness,
            "strategy_version": self.strategy.version,
            "config_path": str(self.strategy.config_path),
            "config_sha256": self.strategy.config_sha256,
            "ledger_schema_version": PAPER_LEDGER_SCHEMA_VERSION,
            "ledger_kind": PAPER_LEDGER_KIND,
            "strategy_identity": {
                "schema_version": self.strategy.schema_version,
                "trigger_strategy": self.strategy.trigger_strategy,
                "quote_mode": str(self.strategy.quote_mode),
                "fill_model": str(self.strategy.fill_model),
            },
            "score_started_at": score_started_at,
            "git_commit": git_commit,
            "account": self.account.as_dict(),
            "capital_utilization": {
                "definition": "(inventory_cost_usd + stranded_inventory_cost_usd + buy_reserved_usd) / initial_cash_usd",
                "utilized_usd": str(utilized),
                "fraction": str(utilized / self.account.initial_cash_usd),
                "capital_time_usd_seconds": str(self.capital_time_usd_seconds),
                "capital_time_usd_minutes": str(self.capital_time_usd_seconds / Decimal("60")),
                "clock_as_of": self._capital_clock_at.isoformat() if self._capital_clock_at else None,
            },
            "active_orders": len(active),
            "oldest_active_order_age_seconds": None if oldest is None else int((checked_at - oldest).total_seconds()),
            "overdue_orders": overdue,
            "stranded_positions": sum(state.stranded for state in self.states.values()),
            "market_closed_positions": sum(state.closed for state in self.states.values()),
            "execution_dependency_scan": scan,
            "halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "fatal_persistence_failure": self.fatal_persistence_failure,
            "discrepancy_count": len(self.ledger.discrepancies) + len(self.ledger.legacy_invalid_records),
            "last_error": last_error,
            "config_mismatch": self.config_mismatch,
            "ledger_integrity": "failed" if self.ledger.invalid else "verified",
            "recovery": recovery,
            "checkpoint_integrity": self.checkpoint_integrity,
            "cursor_integrity": cursor_integrity,
            "status_integrity": status_integrity,
            "upstream_quality": {
                "integrity": self.quality_integrity,
                "last_refresh_at": self.quality_last_refresh_at.isoformat()
                if self.quality_last_refresh_at
                else None,
                "window_hash": self.quality_window_hash,
                "active_at_checked_at": active_quality_window is not None,
                "active_window": (
                    None
                    if active_quality_window is None
                    else {
                        "incident_id": active_quality_window.incident_id,
                        "incident_type": active_quality_window.incident_type,
                        "status": active_quality_window.status,
                        "default_excluded": active_quality_window.default_excluded,
                    }
                ),
                "affected_active_orders": self.quality_affected_active_orders,
                "observed_snapshot_upstream_statuses": observed_upstream_statuses,
            },
            "supervisor": {
                "integrity": self.supervisor_integrity,
                "generation": self.supervisor_generation,
                "active_event_count": len(self.supervisor_active_events),
            },
            "feed_continuity": self.feed_continuity,
            "unresolved_ws_public_trade_evidence": {
                "count": len(self.pending_ws_trade_evidence),
                "evidence_ids": sorted(self.pending_ws_trade_evidence),
            },
            "trade_evidence_counts": dict(sorted(self.trade_evidence_counts.items())),
            "risk_exit": {
                "max_snapshot_age_seconds": int(self.strategy.risk_exit_snapshot_age.total_seconds()),
                "last_book_age_seconds": self.last_risk_exit_book_age_seconds,
                "current_inventory_book_age_seconds": (
                    max(current_risk_book_ages) if current_risk_book_ages else None
                ),
            },
            "paper_score_eligible": not reasons,
            "paper_score_ineligible_reasons": reasons,
            "round_trip_count": sum(engine.round_trip_count for engine in self.engines.values()),
            "order_count": len(orders),
            "fill_count": len(fills),
            "partial_fill_count": sum(0 < order.filled_shares < order.requested_shares for order in orders),
            "turnover_usd": str(sum((fill.shares * fill.price for fill in fills), start=ZERO)),
            "token_portfolios": token_detail,
            "station_day": station_detail,
            "checked_at": checked_at.isoformat(),
            "cursor": None if cursor is None else {
                "restart_count": cursor.restart_count,
                "generation": cursor.generation,
                "sources": cursor.sources,
            },
        }


def _load_quality_windows_verified(path: Path) -> tuple[tuple[Any, ...], str, str | None]:
    """Reload a local quality file as a complete JSON document or fail closed."""

    try:
        raw = path.read_bytes()
        if not raw:
            raise ValueError("quality window file is empty")
        json.loads(raw.decode("utf-8"))
        windows = tuple(load_quality_windows(path))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return (), "unreadable", None
    return windows, "verified", hashlib.sha256(raw).hexdigest()


def _active_event_hash(event_ids: set[str]) -> str:
    return hashlib.sha256("\n".join(sorted(event_ids)).encode("utf-8")).hexdigest()


def _load_paper_supervisor_cursor(
    path: Path,
) -> tuple[str | None, set[str], str]:
    """Load only Paper-owned active-set evidence, never a v2 cursor field."""

    if not path.exists():
        return None, set(), "missing"
    try:
        payload, integrity, _source = read_json_with_fallback(path)
        active_ids = {
            str(value)
            for value in (payload.get("active_event_ids") or ())
            if value
        }
        declared_hash = str(payload.get("active_event_hash") or "")
        if (
            integrity != "verified"
            or payload.get("execution_enabled") is not False
            or not declared_hash
            or declared_hash != _active_event_hash(active_ids)
        ):
            raise StatusIntegrityError("paper supervisor cursor failed integrity validation")
        return str(payload.get("generation") or "") or None, active_ids, "verified"
    except (StatusIntegrityError, OSError, TypeError, ValueError):
        return None, set(), "failed"


def _save_paper_supervisor_cursor(
    path: Path,
    *,
    generation: str | None,
    active_event_ids: set[str],
    ledger_frontier: int,
) -> None:
    """Persist isolated active-set evidence without changing v2 cursor schema."""

    atomic_json_write(
        path,
        {
            "schema_version": 1,
            "execution_enabled": False,
            "generation": generation,
            "active_event_ids": sorted(active_event_ids),
            "active_event_hash": _active_event_hash(active_event_ids),
            "ledger_frontier": ledger_frontier,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _closed_event_ids_from_local_rows(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    """Use only archived close/resolution flags visible at the local receipt."""

    closed: set[str] = set()
    terminal = {"closed", "resolved", "inactive", "cancelled", "settled"}
    for row in rows:
        event_id = str(row.get("event_id") or row.get("event_slug") or "")
        if not event_id:
            continue
        status = str(
            row.get("market_status")
            or row.get("status")
            or row.get("event_status")
            or ""
        ).casefold()
        if bool(row.get("closed")) or bool(row.get("resolved")) or status in terminal:
            closed.add(event_id)
    return closed


def _paper_checkpoint_payload(
    processor: PaperSpreadProcessor,
    *,
    cursor: ShadowCursor,
    score_started_at: str,
    run_id: str,
    clean_exit: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ledger_kind": PAPER_LEDGER_KIND,
        "execution_enabled": False,
        "strategy_version": processor.strategy.version,
        "config_hash": processor.strategy.config_sha256,
        "score_started_at": score_started_at,
        "run_id": run_id,
        "clean_exit": clean_exit,
        "committed_transition_frontier": processor.ledger.frontier,
        "account": processor.account.as_dict(),
        "orders": [order.as_dict() for order in processor.ledger.orders.values()],
        "portfolio_state": {
            key.identifier: {
                "tranche_index": state.tranche_index,
                "filled_tranches": sorted(state.filled_tranches),
                "consumed_observations": sorted(state.consumed_observations),
                "last_consumed_source_timestamp": state.last_consumed_source_timestamp.isoformat()
                if state.last_consumed_source_timestamp
                else None,
                "last_consumed_received_at": state.last_consumed_received_at.isoformat()
                if state.last_consumed_received_at
                else None,
                "last_consumed_observation_id": state.last_consumed_observation_id,
                "cumulative_bought_shares": str(state.cumulative_bought_shares),
                "exit_stage_filled_shares": {
                    str(index): str(value) for index, value in state.exit_stage_filled_shares.items()
                },
                "completed_exit_stages": sorted(state.completed_exit_stages),
                "exit_stage_attempts": {str(index): value for index, value in state.exit_stage_attempts.items()},
                "exit_stage_dust": sorted(state.exit_stage_dust),
                "position_opened_at": state.position_opened_at.isoformat()
                if state.position_opened_at
                else None,
                "stranded": state.stranded,
                "closed": state.closed,
                "close_reason": state.close_reason,
            }
            for key, state in processor.states.items()
        },
        "station_day_cumulative_cost": {
            f"{station}:{day}": str(cost)
            for (station, day), cost in processor.station_day_buy_cost.items()
        },
        "source_cursor": cursor.sources,
        # This is the cursor-committed weather/public-tape join boundary.  It
        # belongs in the recovery artifact as well as the cursor so an audit
        # can prove which non-position source state was eligible at this
        # frontier.  The cursor remains the sole authority for restart.
        "paper_cursor_state": cursor.paper_state,
        "supervisor_generation": processor.supervisor_generation,
        "supervisor_active_event_ids": sorted(processor.supervisor_active_events),
        "supervisor_active_event_hash": _active_event_hash(processor.supervisor_active_events),
        "supervisor_integrity": processor.supervisor_integrity,
        "quality_integrity": processor.quality_integrity,
        "quality_window_hash": processor.quality_window_hash,
        "feed_continuity": processor.feed_continuity,
        "capital_time_usd_seconds": str(processor.capital_time_usd_seconds),
        "recovery": processor.ledger.recovery_status(),
    }


def _encode_paper_join_state(state: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Serialize the join's tuple-keyed state into the atomic Paper cursor."""

    encoded: dict[str, dict[str, str]] = {}
    for name in ("previous_observation", "previous_temperature", "previous_ask"):
        source = state.get(name, {})
        if not isinstance(source, Mapping):
            raise ValueError(f"paper weather join state {name} is not a mapping")
        values: dict[str, str] = {}
        for key, value in source.items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise ValueError("paper weather join state has an invalid key")
            values[f"{key[0]}\x00{key[1]}"] = str(value)
        encoded[name] = values
    return encoded


def _decode_paper_join_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Restore only a complete, cursor-committed weather join baseline."""

    output: dict[str, Any] = {
        "previous_observation": {},
        "previous_temperature": {},
        "previous_ask": {},
    }
    for name in output:
        source = payload.get(name, {})
        if not isinstance(source, Mapping):
            raise ValueError(f"paper weather join cursor {name} is not a mapping")
        for encoded, value in source.items():
            first, separator, second = str(encoded).partition("\x00")
            if not separator or not first or not second:
                raise ValueError("paper weather join cursor key is invalid")
            key = (first, second)
            if name == "previous_observation":
                output[name][key] = str(value)
            else:
                output[name][key] = _decimal(value)
    return output


def _visible_weather_observations(
    weather_paths: Sequence[Path], cursor: ShadowCursor
) -> list[Any]:
    """Return exactly the weather evidence at or before the committed cursor."""

    observations = [
        observation
        for source in weather_paths
        for row in _cursor_visible_jsonl_rows(
            source, cursor.existing_position(source)
        )
        for observation in _parse_observation_safely(row)
    ]
    return sorted(
        observations,
        key=lambda item: (
            item.source_timestamp.astimezone(UTC),
            item.received_at.astimezone(UTC),
            item.observation_id,
        ),
    )


def _rebuild_paper_join_state_from_cursor(
    *,
    weather_paths: Sequence[Path],
    checkpoint_paths: Sequence[Path],
    cursor: ShadowCursor,
) -> tuple[list[Any], dict[str, Any]]:
    """Build a conservative first-run baseline from only committed prefixes.

    This recovery is deliberately a baseline reconstruction, not a strategy
    replay.  In particular it never opens Paper orders and never reads a byte
    beyond a committed source position.
    """

    observations = _visible_weather_observations(weather_paths, cursor)
    state: dict[str, Any] = {
        "previous_observation": {},
        "previous_temperature": {},
        "previous_ask": {},
    }
    for observation in observations:
        identity = (str(observation.station_id), str(observation.product))
        state["previous_observation"][identity] = str(observation.observation_id)
        if observation.temperature_f is not None:
            state["previous_temperature"][identity] = _decimal(observation.temperature_f)

    # Preserve the last native ask that was actually visible at the tail.  A
    # missing/invalid raw row is ignored rather than substituted with a trade,
    # midpoint, or opposite-token value.
    latest_asks: dict[tuple[str, str], tuple[datetime, Decimal]] = {}
    for source in checkpoint_paths:
        for row in _cursor_visible_jsonl_rows(
            source, cursor.existing_position(source)
        ):
            try:
                received_at = datetime.fromisoformat(
                    str(row.get("received_at") or row.get("observed_at")).replace("Z", "+00:00")
                )
                if received_at.tzinfo is None:
                    continue
                received_at = received_at.astimezone(UTC)
                asset_id = str(row.get("asset_id") or row.get("token_id") or "")
                market_slug = str(row.get("market_slug") or "")
                event_id = str(row.get("event_id") or row.get("event_slug") or market_slug.split("/", 1)[0])
                asks = row.get("asks")
                if not asset_id or not event_id or not isinstance(asks, list):
                    continue
                prices = [
                    _decimal(level.get("price"))
                    for level in asks
                    if isinstance(level, Mapping) and level.get("price") is not None
                ]
                if not prices:
                    continue
                key = (event_id, asset_id)
                candidate = (received_at, min(prices))
                if key not in latest_asks or candidate[0] >= latest_asks[key][0]:
                    latest_asks[key] = candidate
            except (TypeError, ValueError):
                continue
    state["previous_ask"] = {
        key: price for key, (_received_at, price) in latest_asks.items()
    }
    return observations, state


def _paper_public_trade_file_state(
    path: Path, events: Sequence[TradeEvent]
) -> dict[str, Any]:
    """Fingerprint a whole-file public tape without trusting mtime alone."""

    payload = path.read_bytes()
    available = sorted(
        value.available_at.astimezone(UTC).isoformat()
        for value in events
        if value.available_at is not None
    )
    identities = sorted(
        PaperSpreadProcessor._durable_trade_event_key(value)
        or PaperSpreadProcessor._trade_identity(value)
        for value in events
    )
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "trade_count": len(events),
        "last_available_at": available[-1] if available else None,
        "watermark_hash": hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest(),
    }


def _cursor_visible_jsonl_rows(
    path: Path, position: Mapping[str, int | bool] | None
) -> list[dict[str, Any]]:
    """Read only the committed prefix of one Paper input archive.

    This intentionally does not reuse `_incremental_jsonl_rows`: that helper
    advances its supplied watermark as it reads, while restart reconstruction
    must make the cursor boundary—not the current file tail—the sole visible
    weather history.
    """

    if position is not None and position.get("archive_position_schema") == 2:
        from poly_weather.archive_position import read_positioned_rows

        return read_positioned_rows(path, position, committed_only=True)[0]
    if position is None or not path.exists() or bool(position.get("skip_existing_gzip")):
        return []
    rows: list[dict[str, Any]] = []
    if path.suffix == ".gz":
        from poly_weather.archive_io import open_jsonl_text

        # A compressed source is immutable once cursor-visible.  If a later
        # retention rewrite made it shorter, a line number no longer denotes
        # the same evidence prefix; returning no baseline is safer than
        # treating a replacement file as historical Paper input.
        expected_size = max(0, int(position.get("offset", 0)))
        if expected_size and path.stat().st_size < expected_size:
            return []
        line_limit = max(0, int(position.get("line", 0)))
        with open_jsonl_text(path) as handle:
            for index, line in enumerate(handle):
                if index >= line_limit:
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
        return rows
    limit = max(0, int(position.get("offset", 0)))
    try:
        if path.stat().st_size < limit:
            # A rotated file cannot be reconstructed from an old byte offset.
            # Do not read its replacement rows as if they were already
            # committed; the next staged cycle will process it from zero.
            return []
        payload = path.read_bytes()[:limit]
    except OSError:
        return []
    for line in payload.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _archive_receipt_visible(row: Mapping[str, Any], cutoff: datetime) -> bool:
    from poly_weather.weather_provenance import weather_receipt_time

    return weather_receipt_time(row) <= cutoff


def run_paper_spread_continuous(
    *,
    data_dir: Path | str = Path("data"),
    ledger_path: Path | str = Path("data/raw/shadow_orders/paper_spread_v1_orders.jsonl"),
    status_path: Path | str = Path("data/runtime/paper_spread_v1_status.json"),
    cursor_path: Path | str = Path("data/runtime/paper_spread_v1_cursor.json"),
    strategy_config_path: Path | str,
    supervised: bool = True,
    poll_seconds: float = 5.0,
    runtime_seconds: float = 0.0,
    max_cycles: int | None = None,
    bootstrap_at_tail: bool = True,
    checkpoint_path: Path | str | None = None,
    _fault_injector: Callable[[str], None] | None = None,
    _ledger_factory: Callable[[Path | str], PaperLedger] | None = None,
) -> dict[str, Any]:
    """Follow only new local book rows, preserving a forward scoring boundary."""
    if supervised is not True:
        raise ValueError("paper runtime requires supervised=true")
    if poll_seconds <= 0 or runtime_seconds < 0:
        raise ValueError("invalid paper runtime timing")
    root = Path(data_dir)
    strategy = PaperStrategyConfig.load(strategy_config_path)
    ledger = (_ledger_factory or PaperLedger)(ledger_path)
    cursor_destination = Path(cursor_path)
    paper_supervisor_cursor_path = cursor_destination.with_name(
        f"{cursor_destination.stem}_paper_supervisor.json"
    )
    cursor_existed = cursor_destination.exists()
    score_started_at: str | None = None
    run_id = uuid4().hex
    paper_status_integrity = "verified"
    if Path(status_path).exists():
        try:
            prior_status, _integrity, _source = read_json_with_fallback(Path(status_path))
            score_started_at = str(prior_status.get("score_started_at") or "") or None
        except (StatusIntegrityError, OSError, TypeError, ValueError):
            paper_status_integrity = "failed"
    try:
        cursor = ShadowCursor.load(cursor_destination)
    except RuntimeError as exc:
        payload = {
            "execution_enabled": False,
            "state": "halted",
            "paper_score_eligible": False,
            "halt_reason": "cursor_integrity_failure",
            "last_error": str(exc),
        }
        atomic_json_write(status_path, payload)
        return payload
    checkpoint_paths = jsonl_archive_paths(root / "raw" / "polymarket_book_checkpoints")
    weather_paths = jsonl_archive_paths(root / "raw" / "weather_daemon")
    ws_paths = jsonl_archive_paths(root / "raw" / "polymarket_clob_websocket")
    bootstrap_count = 0
    if bootstrap_at_tail and not cursor_existed:
        bootstrap_paths = [*checkpoint_paths, *weather_paths, *ws_paths]
        bootstrap_count = cursor.bootstrap_at_tail(bootstrap_paths)
        cursor.save()
    registry_path = root / ".." / "configs" / "settlements.json"
    if not registry_path.exists():
        registry_path = Path("configs/settlements.json")
    metadata: dict[str, dict[str, Any]] = {}
    if registry_path.exists():
        metadata = archived_event_metadata((), load_settlement_registry(registry_path).specs)
    quality_path = root / "runtime" / "polymarket_quality_windows.json"
    quality_windows: tuple[Any, ...] = ()
    checkpoint_destination = (
        Path(checkpoint_path)
        if checkpoint_path is not None
        else Path(status_path).with_name("paper_spread_v1_recovery_checkpoint.json")
    )
    policy_path = Path("configs/warming_window_no_thresholds.json")
    policy_payload: dict[str, Any] = {}
    paper_cursor_state_integrity = "verified"
    cursor_paper_state = cursor.paper_state
    try:
        encoded_join_state = cursor_paper_state.get("weather_join_state")
        if encoded_join_state is None:
            observations, weather_join_state = _rebuild_paper_join_state_from_cursor(
                weather_paths=weather_paths,
                checkpoint_paths=checkpoint_paths,
                cursor=cursor,
            )
        elif not isinstance(encoded_join_state, Mapping):
            raise ValueError("paper weather join state is not an object")
        else:
            observations = _visible_weather_observations(weather_paths, cursor)
            weather_join_state = _decode_paper_join_state(encoded_join_state)
        raw_public_trade_state = cursor_paper_state.get("public_trade_files")
        if raw_public_trade_state is None:
            public_trade_file_state = {}
        elif isinstance(raw_public_trade_state, Mapping):
            public_trade_file_state = dict(raw_public_trade_state)
        else:
            raise ValueError("paper public trade file state is not an object")
    except (TypeError, ValueError):
        # Never replace a corrupt state document by reading the current file
        # tails.  Rebuilding from its committed prefixes is safe, but new
        # entries remain blocked until a clean durable commit succeeds.
        observations, weather_join_state = _rebuild_paper_join_state_from_cursor(
            weather_paths=weather_paths,
            checkpoint_paths=checkpoint_paths,
            cursor=cursor,
        )
        public_trade_file_state = {}
        paper_cursor_state_integrity = "failed"
    all_public_trades: list[Any] = []
    public_trade_dir = root / "public_trades"
    if public_trade_dir.exists():
        all_public_trades = [
            trade for rows in load_event_trade_tapes(public_trade_dir).values() for trade in rows
        ]
    processor = PaperSpreadProcessor(ledger=ledger, strategy=strategy)
    if paper_cursor_state_integrity != "verified":
        processor.checkpoint_integrity = "paper_cursor_state_failed"
        processor.new_orders_blocked_reasons.add("paper_cursor_state_integrity_failure")
    paper_cursor_generation, paper_cursor_active_events, paper_cursor_integrity = (
        _load_paper_supervisor_cursor(paper_supervisor_cursor_path)
    )
    if paper_cursor_integrity == "verified":
        # This is evidence only. A fresh verified supervisor read is still
        # required before new entries; its active-set diff handles removals
        # that occurred while the Paper follower was down.
        processor.supervisor_generation = paper_cursor_generation
        processor.supervisor_active_events = set(paper_cursor_active_events)
    elif paper_cursor_integrity == "failed":
        processor.checkpoint_integrity = "paper_supervisor_cursor_failed"
        processor.new_orders_blocked_reasons.add("paper_supervisor_cursor_integrity_failure")
        # We cannot prove whether a restored event was unsubscribed during the
        # missing/corrupt evidence interval. Release maker reservations and
        # strand any inventory instead of treating absence as a clean feed.
        for key in tuple(processor.engines):
            processor.close_portfolio_key(
                key,
                reason="paper_supervisor_cursor_integrity_failure",
                as_of=datetime.now(UTC),
            )
    if checkpoint_destination.exists():
        try:
            checkpoint, checkpoint_read_integrity, _source = read_json_with_fallback(
                checkpoint_destination
            )
            frontier = int(checkpoint.get("committed_transition_frontier") or 0)
            if checkpoint_read_integrity != "verified" or checkpoint.get("execution_enabled") is not False:
                processor.checkpoint_integrity = "failed"
                processor.new_orders_blocked_reasons.add("checkpoint_integrity_failure")
            elif frontier > ledger.frontier:
                processor.checkpoint_integrity = "ahead_of_ledger"
                processor._halt(
                    "checkpoint_ahead_of_ledger",
                    details={"checkpoint_frontier": frontier, "ledger_frontier": ledger.frontier},
                )
            elif frontier < ledger.frontier:
                # The append-only ledger is authority; a behind checkpoint is
                # rebuilt below and never causes a backward replay.
                processor.checkpoint_integrity = "behind_ledger_rebuilt"
            else:
                processor.checkpoint_integrity = "verified"
            if score_started_at is None:
                score_started_at = str(checkpoint.get("score_started_at") or "") or None
            if checkpoint.get("clean_exit") is False:
                processor.recovery["unclean_prior_run"] = True
        except (StatusIntegrityError, OSError, TypeError, ValueError):
            # A valid ledger is sufficient to reconstruct state. Keep scoring
            # ineligible until the next verified checkpoint is emitted.
            processor.checkpoint_integrity = "failed"
            processor.new_orders_blocked_reasons.add("checkpoint_integrity_failure")
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for encoded_key, value in cursor.pair_latest.items():
        base, separator, outcome = encoded_key.partition("\x00")
        if not separator or not isinstance(value, Mapping):
            continue
        restored = dict(value)
        if restored.get("_timestamp"):
            try:
                restored["_timestamp"] = datetime.fromisoformat(str(restored["_timestamp"])).astimezone(UTC)
            except (TypeError, ValueError):
                continue
        latest[(base, outcome)] = restored
    last_emitted: dict[str, datetime] = {}
    for base, value in cursor.pair_last_emitted.items():
        try:
            last_emitted[base] = datetime.fromisoformat(value).astimezone(UTC)
        except (TypeError, ValueError):
            continue
    started = time.monotonic()
    started_at = datetime.now(UTC)
    score_started_at = score_started_at or started_at.isoformat()
    cycles = 0
    last_error: str | None = None
    supervisor_active_events: set[str] | None = (
        set(paper_cursor_active_events)
        if paper_cursor_integrity == "verified"
        else None
    )
    supervisor_integrity = "unreadable"
    previous_chain = None
    while True:
        cycles += 1
        staged_cursor = copy.deepcopy(cursor)
        staged_latest = copy.deepcopy(latest)
        staged_last_emitted = dict(last_emitted)
        staged_observations = list(observations)
        staged_weather_join_state = copy.deepcopy(weather_join_state)
        staged_public_trade_file_state = copy.deepcopy(public_trade_file_state)
        cycle_failed = False
        last_error = None
        checkpoint_rows: list[dict[str, Any]] = []
        weather_rows: list[dict[str, Any]] = []
        ws_rows: list[dict[str, Any]] = []
        accepted_trade_events: list[TradeEvent] = []
        _validation: dict[str, Any] = {}
        unmatched_ws_trade_count = 0
        quality_excluded_ws_trade_count = 0
        affected_quality_orders = 0
        api_trade_events: list[TradeEvent] = []
        cycle_as_of = datetime.now(UTC)
        from poly_weather.business_readiness import paper_readiness

        chain = read_chain_status(root, now=cycle_as_of, previous=previous_chain)
        previous_chain = chain
        operational = {name: chain[name].get("health_ready") is True
                       for name in ("market", "supervisor", "weather")}
        progress_ready = all(chain[name].get("business_ready") is True
                             for name in ("market", "supervisor", "weather"))
        processor.set_business_readiness(paper_readiness(
            as_of=cycle_as_of, scope="cycle:no_snapshot", operational=operational,
            evidence={"progress": progress_ready}, group_completeness="unsupported"))
        def inject_fault(stage: str) -> None:
            if _fault_injector is not None:
                _fault_injector(stage)
        try:
            policy_payload = (
                json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
            )
            processor.new_orders_blocked_reasons.discard("season_policy_unreadable")
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            policy_payload = {}
            processor.new_orders_blocked_reasons.add("season_policy_unreadable")
        quality_windows, quality_integrity, quality_hash = _load_quality_windows_verified(
            quality_path
        )
        processor.set_quality_evidence(
            integrity=quality_integrity,
            refreshed_at=cycle_as_of,
            window_hash=quality_hash,
            windows=quality_windows,
        )
        affected_quality_orders = (
            processor.apply_quality_windows(quality_windows, as_of=cycle_as_of)
            if quality_integrity == "verified"
            else 0
        )
        closed_event_ids: set[str] = set()
        supervisor_status = root / "runtime" / "market_supervisor_status.json"
        try:
            payload = read_status(supervisor_status, stale_after_seconds=360,
                                  now=cycle_as_of, dependency_state="not_required")
            if payload.get("health_ready") is not True:
                raise StatusIntegrityError("supervisor operational health gate failed")
            active = {
                str(row.get("event_id"))
                for row in payload.get("active_events", ())
                if isinstance(row, Mapping) and row.get("event_id")
            }
            prior_active = supervisor_active_events
            if prior_active is not None:
                closed_event_ids.update(prior_active - active)
            supervisor_active_events = active
            supervisor_integrity = "verified"
            generation = str(payload.get("generation") or "") or None
            staged_cursor.generation = generation or staged_cursor.generation
            processor.set_supervisor_evidence(
                generation=generation,
                active_event_ids=active,
                integrity="verified",
                as_of=cycle_as_of,
            )
        except (StatusIntegrityError, OSError, TypeError, ValueError) as exc:
            supervisor_integrity = "unreadable"
            processor.set_supervisor_evidence(
                generation=None,
                active_event_ids=None,
                integrity="unreadable",
                as_of=cycle_as_of,
            )
            last_error = f"supervisor_status_integrity_failure:{type(exc).__name__}"
        try:
            if public_trade_dir.exists():
                # Re-scan local public tape every cycle. Canonical trade keys in
                # the Paper ledger are the durable watermark, so a receipt that
                # arrived during follower downtime is replayed once rather than
                # being discarded merely because its file mtime is unchanged.
                # The persisted fingerprint is audit state, not permission to
                # skip a recovered active order.
                for source in sorted(public_trade_dir.glob("*.json")):
                    raw_public_payload = json.loads(source.read_text(encoding="utf-8"))
                    if not isinstance(raw_public_payload, Mapping):
                        raise ValueError("public trade file is not an object")
                    events = _public_trade_events_from_file(
                        source, quality_windows=quality_windows
                    )
                    api_trade_events.extend(
                        event
                        for event in events
                        if _receipt_visible_by(event.available_at, cycle_as_of)
                    )
                    staged_public_trade_file_state[str(source.resolve())] = (
                        _paper_public_trade_file_state(source, events)
                    )
                current_public_paths = {
                    str(source.resolve()) for source in public_trade_dir.glob("*.json")
                }
                staged_public_trade_file_state = {
                    key: value
                    for key, value in staged_public_trade_file_state.items()
                    if key in current_public_paths
                }
                all_public_trades = [
                    trade
                    for rows in load_event_trade_tapes(public_trade_dir).values()
                    for trade in rows
                    if _receipt_visible_by(trade.available_at, cycle_as_of)
                ]
            for source in checkpoint_paths:
                checkpoint_rows.extend(
                    _incremental_jsonl_rows(source, staged_cursor.position(source),
                                            visible=lambda row, cutoff=cycle_as_of: _archive_receipt_visible(row, cutoff))
                )
            for source in weather_paths:
                weather_rows.extend(
                    _incremental_jsonl_rows(source, staged_cursor.position(source),
                                            visible=lambda row, cutoff=cycle_as_of: _archive_receipt_visible(row, cutoff))
                )
            for source in ws_paths:
                ws_rows.extend(_incremental_jsonl_rows(source, staged_cursor.position(source),
                                                      visible=lambda row, cutoff=cycle_as_of: _archive_receipt_visible(row, cutoff)))
            staged_observations.extend(
                observation
                for row in weather_rows
                for observation in _parse_observation_safely(row)
            )
            inject_fault("source_conversion")
            if checkpoint_rows:
                locally_closed = _closed_event_ids_from_local_rows(checkpoint_rows)
                closed_event_ids.update(locally_closed)
                for key in tuple(processor.engines):
                    if key.event_id in locally_closed:
                        processor.close_portfolio_key(
                            key, reason="archived_closed_or_resolved", as_of=cycle_as_of
                        )
                slugs = {
                    str(row.get("market_slug") or "").rpartition(":")[0].split("/", 1)[0]
                    for row in checkpoint_rows
                    if row.get("market_slug")
                }
                if registry_path.exists():
                    metadata.update(archived_event_metadata(sorted(slugs), load_settlement_registry(registry_path).specs))
                pairs = _archive_pair_rows(
                    checkpoint_rows,
                    staged_latest,
                    staged_last_emitted,
                    quality_windows=quality_windows,
                )
                enriched_pairs: list[dict[str, Any]] = []
                for pair in pairs:
                    enriched = dict(pair)
                    event_value = metadata.get(pair["event_slug"], {})
                    enriched["station_id"] = event_value.get("station_id")
                    enriched["market_day"] = event_value.get("target_date")
                    station = str(enriched.get("station_id") or "")
                    target = str(enriched.get("market_day") or "")
                    for season in (policy_payload.get("stations") or {}).get(station, {}).get("seasons", ()):
                        if str(season.get("window_start") or "") <= target <= str(season.get("window_end") or ""):
                            enriched["season_version"] = str(season.get("threshold_version") or policy_payload.get("policy_version") or "")
                            enriched["in_season"] = True
                            break
                    enriched_pairs.append(enriched)
                aligned, _ = align_weather_to_snapshots(
                    enriched_pairs,
                    staged_observations,
                    state=staged_weather_join_state,
                )
                for row in aligned:
                    snapshot = BookSnapshot.from_mapping(row)
                    weather_evidence, _ = PaperWeatherEvidence.parse(snapshot)
                    processor.set_business_readiness(paper_readiness(
                        as_of=cycle_as_of, scope=snapshot.portfolio_key.identifier,
                        operational=operational,
                        evidence={"progress": progress_ready,
                                  "active_set": processor.supervisor_integrity == "verified"
                                  and snapshot.event_id in processor.supervisor_active_events,
                                  "weather": weather_evidence is not None,
                                  "rules": row.get("settlement_verified") is True,
                                  "season": bool(snapshot.season_version) and snapshot.in_season,
                                  "quality": processor.quality_integrity == "verified"
                                  and not processor._quality_excludes(snapshot.timestamp),
                                  "archive_continuity": processor.feed_continuity == "verified",
                                  "account_ledger": not processor.is_halted
                                  and processor.checkpoint_integrity == "verified"},
                        group_completeness="unsupported"))
                    inject_fault("snapshot_conversion")
                    processor.process_snapshot(snapshot)
                    inject_fault("snapshot_processing")
                    if str(snapshot.metadata.get("feed_continuity") or "verified").casefold() not in {
                        "verified",
                        "continuous",
                    }:
                        processor._set_feed_continuity("unknown", as_of=snapshot.timestamp)
            parsed_ws_rows: list[MarketWsTrade] = []
            for row in ws_rows:
                try:
                    parsed = parse_market_ws_trade(row)
                except (TypeError, ValueError):
                    continue
                if parsed.received_at > cycle_as_of:
                    # Retaining a source cursor beyond an impossible local
                    # receipt time would permanently skip forward evidence.
                    # Fail the full staged cycle so the row is retried.
                    raise ValueError("websocket receipt is later than cycle clock")
                if quality_window_at(tuple(quality_windows), parsed.received_at) is not None:
                    quality_excluded_ws_trade_count += 1
                    continue
                parsed_ws_rows.append(parsed)
            # Validate as a batch to detect siblings, but authorize per row.
            # Include durable pending evidence so a later API-only poll cannot
            # bypass an earlier quantity/time conflict.
            pending_rows = [processor._pending_ws_row(details)
                            for details in processor.pending_ws_trade_evidence.values()]
            evidence_rows = list(parsed_ws_rows)
            evidence_ids = {processor._ws_trade_evidence(row)[0] for row in evidence_rows}
            for pending in pending_rows:
                if processor._ws_trade_evidence(pending)[0] not in evidence_ids:
                    evidence_rows.append(pending)
            accepted, _validation = build_shadow_trade_events(evidence_rows, all_public_trades,
                                                              quality_windows=quality_windows)
            for row, result in zip(evidence_rows, _validation["matches"], strict=True):
                if result["allowed"]:
                    evidence_id, _details = processor._ws_trade_evidence(row)
                    processor._resolve_pending_ws_trade(evidence_id, resolved_by="public_trade_tape",
                                                        validated_at=result["validated_at"])
                else:
                    unmatched_ws_trade_count += 1
                    processor._record_pending_ws_trade(row, match_reason=result["reason"])
            observed_hashes = {row.transaction_hash for row in evidence_rows}
            accepted_trade_events = [event for event in accepted if event.source == "market_ws"
                                     and _receipt_visible_by(event.available_at, cycle_as_of)]
            accepted_trade_events.extend(event for event in api_trade_events
                                         if event.event_id not in observed_hashes)
            if accepted_trade_events:
                processor.process_trades(accepted_trade_events)
            inject_fault("trade_processing")
            processor._set_feed_continuity(
                "verified"
                if processor.trade_evidence_counts.get("UNKNOWN_TRADE_SEQUENCE", 0) == 0
                and processor.trade_evidence_counts.get("UNKNOWN_FEED_GAP", 0) == 0
                and not processor.pending_ws_trade_evidence
                else "unknown",
                as_of=cycle_as_of,
            )
            processor.sweep_lifecycle(as_of=cycle_as_of, closed_event_ids=closed_event_ids)
            inject_fault("lifecycle_processing")
        except (OSError, TypeError, ValueError, KeyError) as exc:
            cycle_failed = True
            last_error = f"cycle:{type(exc).__name__}: {exc}"
        if processor.is_halted:
            cycle_failed = True
            last_error = last_error or f"paper_halted:{processor.halt_reason or 'unknown'}"
        status = processor.status(
            as_of=cycle_as_of,
            cursor=cursor if cycle_failed else staged_cursor,
            score_started_at=score_started_at,
            git_commit=_git_commit(),
            last_error=last_error,
            cursor_integrity="verified",
            status_integrity=(
                "failed"
                if paper_status_integrity == "failed"
                or paper_cursor_state_integrity != "verified"
                or supervisor_integrity != "verified"
                or processor.quality_integrity != "verified"
                else "verified"
            ),
        )
        status.update(
            {
                "schema_version": 1,
                "state": "halted" if processor.is_halted else "running",
                "run_id": run_id,
                "pid": os.getpid(),
                "started_at": started_at.isoformat(),
                "heartbeat": datetime.now(UTC).isoformat(),
                "runtime_mode": "read_only_paper_continuous",
                "cursor_path": str(cursor_destination.resolve()),
                "paper_supervisor_cursor_path": str(paper_supervisor_cursor_path.resolve()),
                "paper_supervisor_cursor_integrity": paper_cursor_integrity,
                "paper_cursor_state_integrity": paper_cursor_state_integrity,
                "cursor_integrity": "verified",
                "cursor_bootstrap_mode": "tail_of_existing_archives" if bootstrap_at_tail and not cursor_existed else "resumed_or_explicit_replay",
                "cursor_bootstrap_source_count": bootstrap_count,
                "cycle_count": cycles,
                "last_error": last_error,
                "data_coverage": {
                    "new_market_rows": len(checkpoint_rows),
                    "new_weather_rows": len(weather_rows),
                    "new_ws_trade_rows": len(ws_rows),
                    "new_data_api_trade_rows": len(api_trade_events),
                    "matched_trade_rows": len(accepted_trade_events),
                    "accepted_queue_trade_rows": 0,
                    "group_completeness_support": "UNSUPPORTED_GROUP_COMPLETENESS",
                    "group_completeness": "unproven",
                    "trade_validation": _validation,
                    "unmatched_ws_trade_count": unmatched_ws_trade_count,
                    "quality_excluded_ws_trade_count": quality_excluded_ws_trade_count,
                    "quality_affected_active_orders": affected_quality_orders,
                },
            }
        )
        if cycle_failed:
            status["cursor_commit_state"] = "not_committed_cycle_failure"
            try:
                atomic_json_write(status_path, status)
            except OSError:
                processor._halt("paper_cycle_status_persistence_failed")
                status["state"] = "halted"
                status["paper_score_eligible"] = False
                status["fatal_persistence_failure"] = processor.fatal_persistence_failure
                return status
            if processor.is_halted:
                status["state"] = "halted"
                status["paper_score_eligible"] = False
                status["fatal_persistence_failure"] = processor.fatal_persistence_failure
                return status
            if max_cycles is not None and cycles >= max_cycles:
                return status
            if runtime_seconds > 0 and time.monotonic() - started >= runtime_seconds:
                return status
            time.sleep(poll_seconds)
            continue
        staged_cursor.pair_latest = {
            f"{base}\x00{outcome}": {
                **row,
                "_timestamp": row["_timestamp"].isoformat()
                if isinstance(row.get("_timestamp"), datetime)
                else row.get("_timestamp"),
            }
            for (base, outcome), row in staged_latest.items()
        }
        staged_cursor.pair_last_emitted = {
            base: timestamp.isoformat() for base, timestamp in staged_last_emitted.items()
        }
        staged_cursor.paper_state = {
            "schema_version": 1,
            "weather_join_state": _encode_paper_join_state(staged_weather_join_state),
            "public_trade_files": staged_public_trade_file_state,
        }
        status["cursor_commit_state"] = "pending_durable_commit"
        try:
            atomic_json_write(status_path, status)
            _save_paper_supervisor_cursor(
                paper_supervisor_cursor_path,
                generation=processor.supervisor_generation,
                active_event_ids=processor.supervisor_active_events,
                ledger_frontier=processor.ledger.frontier,
            )
            atomic_json_write(
                checkpoint_destination,
                _paper_checkpoint_payload(
                    processor,
                    cursor=staged_cursor,
                    score_started_at=score_started_at,
                    run_id=run_id,
                    clean_exit=False,
                ),
            )
            staged_cursor.save()
        except (OSError, PaperLedgerIntegrityError, StatusIntegrityError, TypeError, ValueError) as exc:
            last_error = f"cycle_commit:{type(exc).__name__}: {exc}"
            processor._halt(
                "paper_cycle_commit_persistence_failed", details={"error": last_error}
            )
            status["state"] = "halted"
            status["paper_score_eligible"] = False
            status["last_error"] = last_error
            status["cursor_commit_state"] = "not_committed_persistence_failure"
            status["fatal_persistence_failure"] = processor.fatal_persistence_failure
            # A failed commit is not a retryable clean state.  A later process
            # must reconstruct from the ledger rather than this in-memory
            # follower continuing to consume input under an unknown frontier.
            return status
        cursor = staged_cursor
        latest = staged_latest
        last_emitted = staged_last_emitted
        observations = staged_observations
        weather_join_state = staged_weather_join_state
        public_trade_file_state = staged_public_trade_file_state
        if max_cycles is not None and cycles >= max_cycles:
            try:
                atomic_json_write(
                    checkpoint_destination,
                    _paper_checkpoint_payload(
                        processor,
                        cursor=cursor,
                        score_started_at=score_started_at,
                        run_id=run_id,
                        clean_exit=True,
                    ),
                )
            except OSError as exc:
                processor._persistence_failure("clean_exit_checkpoint", exc)
                status.update(
                    {
                        "state": "halted",
                        "paper_score_eligible": False,
                        "last_error": f"clean_exit_checkpoint:{type(exc).__name__}: {exc}",
                        "fatal_persistence_failure": processor.fatal_persistence_failure,
                    }
                )
            return status
        if runtime_seconds > 0 and time.monotonic() - started >= runtime_seconds:
            try:
                atomic_json_write(
                    checkpoint_destination,
                    _paper_checkpoint_payload(
                        processor,
                        cursor=cursor,
                        score_started_at=score_started_at,
                        run_id=run_id,
                        clean_exit=True,
                    ),
                )
            except OSError as exc:
                processor._persistence_failure("clean_exit_checkpoint", exc)
                status.update(
                    {
                        "state": "halted",
                        "paper_score_eligible": False,
                        "last_error": f"clean_exit_checkpoint:{type(exc).__name__}: {exc}",
                        "fatal_persistence_failure": processor.fatal_persistence_failure,
                    }
                )
            return status
        time.sleep(poll_seconds)


def replay_paper_spread(
    snapshots: Sequence[BookSnapshot],
    *,
    ledger_path: Path | str,
    strategy_config_path: Path | str,
) -> dict[str, Any]:
    """Replay with event timestamps only; never import wall-clock lifecycle time."""
    processor = PaperSpreadProcessor(
        ledger=PaperLedger(ledger_path), strategy=PaperStrategyConfig.load(strategy_config_path)
    )
    for snapshot in sorted(snapshots, key=lambda row: row.timestamp):
        processor.sweep_lifecycle(as_of=snapshot.timestamp)
        processor.process_snapshot(snapshot)
    as_of = max((row.timestamp for row in snapshots), default=datetime(1970, 1, 1, tzinfo=UTC))
    return processor.status(as_of=as_of)
