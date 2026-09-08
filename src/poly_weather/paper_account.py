"""Durable, isolated account state for the Paper V1 read-only runtime.

Paper V1 intentionally does not share a ledger with the diagnostic v2 shadow
follower. A paper record is an auditable model fact, never an exchange order.
The ledger uses a write-ahead intent/commit protocol so an interrupted
order/account transition is either reconstructed from durable order facts or
left durably halted. It has no network, credential, or execution imports.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from poly_weather.shadow_orders import (
    EXECUTION_ENABLED,
    ZERO,
    ShadowFill,
    ShadowOrder,
    TokenPortfolioKey,
    _json_value,
)

# Version 1 was an uncommitted experimental sketch. New Paper V1 ledgers
# deliberately fail closed rather than silently treating that format as an
# account authority.
PAPER_LEDGER_SCHEMA_VERSION = 2
PAPER_LEDGER_KIND = "paper_spread_v1"
_ACCOUNT_EVENTS = frozenset(
    {"reserve_buy", "release_buy", "fill_buy", "fill_sell", "strand", "halt"}
)


class PaperLedgerIntegrityError(ValueError):
    """Raised when an append-only paper ledger cannot be safely extended."""


def _decimal(value: Decimal | float | int | str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _detail_signature(value: Mapping[str, Any]) -> str:
    return json.dumps(_json_value(dict(value)), ensure_ascii=False, sort_keys=True)


def _fsync_append(path: Path, row: Mapping[str, Any]) -> None:
    """Append one JSONL record and make it durable before returning."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(_json_value(dict(row)), ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True, slots=True)
class PaperPosition:
    portfolio_key: TokenPortfolioKey
    shares: Decimal = ZERO
    cost_usd: Decimal = ZERO
    stranded: bool = False

    @property
    def average_cost(self) -> Decimal | None:
        return self.cost_usd / self.shares if self.shares else None


@dataclass
class PaperAccount:
    """One global cash account; realized PnL is gross of fees.

    Reservations are a locked subset of cash, never a second equity component.
    ``inventory_cost_usd`` and ``stranded_inventory_cost_usd`` are mutually
    exclusive cost buckets.
    """

    initial_cash_usd: Decimal = Decimal("200")
    cash_usd: Decimal = Decimal("200")
    buy_reserved_usd: Decimal = ZERO
    inventory_cost_usd: Decimal = ZERO
    fees_usd: Decimal = ZERO
    realized_pnl_usd: Decimal = ZERO
    stranded_inventory_cost_usd: Decimal = ZERO
    positions: dict[TokenPortfolioKey, PaperPosition] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str | None = None

    def __post_init__(self) -> None:
        self.initial_cash_usd = _decimal(self.initial_cash_usd)
        self.cash_usd = _decimal(self.cash_usd)
        self.buy_reserved_usd = _decimal(self.buy_reserved_usd)
        self.inventory_cost_usd = _decimal(self.inventory_cost_usd)
        self.fees_usd = _decimal(self.fees_usd)
        self.realized_pnl_usd = _decimal(self.realized_pnl_usd)
        self.stranded_inventory_cost_usd = _decimal(self.stranded_inventory_cost_usd)
        if self.initial_cash_usd <= ZERO:
            raise ValueError("initial_cash_usd must be positive")
        self.assert_conservation()

    @property
    def available_cash_usd(self) -> Decimal:
        return self.cash_usd - self.buy_reserved_usd

    @property
    def equity_usd(self) -> Decimal:
        return self.cash_usd + self.inventory_cost_usd + self.stranded_inventory_cost_usd

    def _position(self, key: TokenPortfolioKey) -> PaperPosition:
        return self.positions.get(key, PaperPosition(key))

    def can_reserve_buy(self, notional_usd: Decimal | float | int | str) -> bool:
        amount = _decimal(notional_usd)
        return not self.halted and amount > ZERO and amount <= self.available_cash_usd

    def reserve_buy(self, key: TokenPortfolioKey, notional_usd: Decimal | float | int | str) -> None:
        amount = _decimal(notional_usd)
        if not self.can_reserve_buy(amount):
            raise ValueError("paper_account_insufficient_available_cash")
        self.buy_reserved_usd += amount
        self.assert_conservation()

    def release_buy(self, notional_usd: Decimal | float | int | str) -> None:
        amount = _decimal(notional_usd)
        if amount < ZERO or amount > self.buy_reserved_usd:
            raise ValueError("paper_account_invalid_reservation_release")
        self.buy_reserved_usd -= amount
        self.assert_conservation()

    def fill_buy(
        self,
        key: TokenPortfolioKey,
        *,
        shares: Decimal | float | int | str,
        price: Decimal | float | int | str,
        reserved_notional_usd: Decimal | float | int | str,
        fee_usd: Decimal | float | int | str,
    ) -> None:
        quantity, price_value = _decimal(shares), _decimal(price)
        reserved, fee = _decimal(reserved_notional_usd), _decimal(fee_usd)
        cost = quantity * price_value
        if quantity <= ZERO or cost <= ZERO or reserved < cost or fee < ZERO:
            raise ValueError("paper_account_invalid_buy_fill")
        if reserved > self.buy_reserved_usd or cost + fee > self.cash_usd:
            raise ValueError("paper_account_buy_fill_exceeds_account")
        current = self._position(key)
        if current.stranded:
            raise ValueError("paper_account_buy_into_stranded_position")
        self.buy_reserved_usd -= reserved
        self.cash_usd -= cost + fee
        self.inventory_cost_usd += cost
        self.fees_usd += fee
        self.positions[key] = PaperPosition(key, current.shares + quantity, current.cost_usd + cost)
        self.assert_conservation()

    def fill_sell(
        self,
        key: TokenPortfolioKey,
        *,
        shares: Decimal | float | int | str,
        price: Decimal | float | int | str,
        fee_usd: Decimal | float | int | str,
    ) -> None:
        quantity, price_value, fee = _decimal(shares), _decimal(price), _decimal(fee_usd)
        current = self._position(key)
        if current.stranded or quantity <= ZERO or quantity > current.shares or fee < ZERO:
            raise ValueError("paper_account_invalid_sell_fill")
        average = current.average_cost
        if average is None:
            raise ValueError("paper_account_missing_cost_basis")
        cost = quantity * average
        proceeds = quantity * price_value
        remaining = current.shares - quantity
        self.cash_usd += proceeds - fee
        self.inventory_cost_usd -= cost
        self.fees_usd += fee
        self.realized_pnl_usd += proceeds - cost
        self.positions[key] = PaperPosition(
            key, remaining, ZERO if remaining == ZERO else current.cost_usd - cost
        )
        self.assert_conservation()

    def strand(self, key: TokenPortfolioKey) -> None:
        current = self._position(key)
        if current.shares <= ZERO or current.stranded:
            return
        self.inventory_cost_usd -= current.cost_usd
        self.stranded_inventory_cost_usd += current.cost_usd
        self.positions[key] = PaperPosition(key, current.shares, current.cost_usd, stranded=True)
        self.assert_conservation()

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def assert_conservation(self) -> None:
        if min(
            self.cash_usd,
            self.buy_reserved_usd,
            self.inventory_cost_usd,
            self.fees_usd,
            self.stranded_inventory_cost_usd,
        ) < ZERO or self.buy_reserved_usd > self.cash_usd:
            raise ValueError("paper_account_negative_or_unfunded_balance")
        expected = self.initial_cash_usd + self.realized_pnl_usd - self.fees_usd
        if self.equity_usd != expected:
            raise ValueError("paper_account_conservation_failed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "initial_cash_usd": str(self.initial_cash_usd),
            "cash_usd": str(self.cash_usd),
            "buy_reserved_usd": str(self.buy_reserved_usd),
            "available_cash_usd": str(self.available_cash_usd),
            "inventory_cost_usd": str(self.inventory_cost_usd),
            "fees_usd": str(self.fees_usd),
            "realized_pnl_usd": str(self.realized_pnl_usd),
            "stranded_inventory_cost_usd": str(self.stranded_inventory_cost_usd),
            "equity_usd": str(self.equity_usd),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "positions": [
                {
                    "portfolio_key": key.as_dict(),
                    "shares": str(position.shares),
                    "cost_usd": str(position.cost_usd),
                    "stranded": position.stranded,
                }
                for key, position in sorted(self.positions.items())
            ],
        }


@dataclass(frozen=True, slots=True)
class PaperAccountEffect:
    """A deterministic account mutation derived from durable Paper facts."""

    transition_id: str
    event: str
    details: Mapping[str, Any]
    timestamp: datetime


class PaperLedger:
    """Append-only authority with intent/commit and recovery evidence.

    The underlying order engine persists complete order snapshots. This ledger
    adds account-effect intents and commits rather than pretending independent
    file appends are atomic. At startup, order snapshots provide enough facts
    to repair a missing reserve/fill/release effect exactly once; ambiguity or
    corruption becomes a discrepancy and blocks scoring.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        persistence_fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(path)
        # The default is deliberately inert.  Tests can inject an OSError at
        # the same append boundary used in production without monkeypatching
        # around the write-ahead protocol.
        self._persistence_fault_injector = persistence_fault_injector
        self.orders: dict[str, ShadowOrder] = {}
        self.order_history: dict[str, list[ShadowOrder]] = {}
        self._idempotency: dict[str, str] = {}
        self._seen_events: set[str] = set()
        self.account_events: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.discrepancies: list[dict[str, Any]] = []
        self.legacy_invalid_records: list[dict[str, Any]] = []
        self.transition_intents: dict[str, dict[str, Any]] = {}
        self.committed_transitions: dict[str, dict[str, Any]] = {}
        self.aborted_transitions: set[str] = set()
        self.recovery_repairs: list[str] = []
        self.ledger_state = "missing"
        self.frontier = 0
        self._append_blocked = False
        self._startup_reconciled = False
        self._load()

    @property
    def invalid(self) -> bool:
        return bool(self.legacy_invalid_records or self.discrepancies or self._append_blocked)

    @property
    def unfinished_transitions(self) -> tuple[str, ...]:
        return tuple(
            transition_id
            for transition_id in self.transition_intents
            if transition_id not in self.committed_transitions
            and transition_id not in self.aborted_transitions
        )

    def _invalid(
        self,
        reason: str,
        *,
        line: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"reason": reason}
        if line is not None:
            payload["line"] = line
        if details:
            payload["details"] = _json_value(dict(details))
        self.legacy_invalid_records.append(payload)

    def _load(self) -> None:
        if not self.path.exists():
            self.ledger_state = "missing"
            return
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            self.ledger_state = "unreadable"
            self._append_blocked = True
            self._invalid("ledger_read_failed", details={"error": str(exc)})
            return
        if not raw:
            self.ledger_state = "confirmed_empty"
            return
        self.ledger_state = "verified"
        lines = raw.splitlines(keepends=True)
        for index, encoded in enumerate(lines, start=1):
            if not encoded.strip():
                self._invalid("blank_ledger_line", line=index)
                continue
            is_last = index == len(lines)
            complete_line = encoded.endswith(b"\n") or encoded.endswith(b"\r")
            try:
                row = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if is_last and not complete_line:
                    self.ledger_state = "recoverable_incomplete_tail"
                    self._append_blocked = True
                    self._invalid("truncated_final_jsonl_line", line=index, details={"error": str(exc)})
                else:
                    self.ledger_state = "corrupt"
                    self._append_blocked = True
                    self._invalid("malformed_non_tail_jsonl_line", line=index, details={"error": str(exc)})
                continue
            if not isinstance(row, Mapping):
                self._invalid("ledger_record_not_object", line=index)
                continue
            self.frontier = index
            self._load_row(dict(row), line=index)
        if self.legacy_invalid_records and self.ledger_state == "verified":
            self.ledger_state = "semantic_corruption"
            self._append_blocked = True

    def _validate_envelope(self, row: Mapping[str, Any], *, line: int) -> bool:
        try:
            schema_version = int(row.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version != PAPER_LEDGER_SCHEMA_VERSION:
            self._invalid("non_paper_or_schema_mismatch", line=line)
            return False
        if row.get("ledger_kind") != PAPER_LEDGER_KIND:
            self._invalid("non_paper_ledger_kind", line=line)
            return False
        if row.get("execution_enabled") is not False:
            self._invalid("execution_enabled_not_false", line=line)
            return False
        return True

    def _load_row(self, row: dict[str, Any], *, line: int) -> None:
        if not self._validate_envelope(row, line=line):
            return
        kind = str(row.get("record_type") or "")
        event_key = row.get("event_key")
        if event_key:
            self._seen_events.add(str(event_key))
        if kind == "order":
            try:
                order = ShadowOrder.from_dict(row["order"])
                declared = row["portfolio_key"]
                key = TokenPortfolioKey(**{name: str(value) for name, value in declared.items()})
            except (KeyError, TypeError, ValueError):
                self._invalid("invalid_paper_order", line=line)
                return
            if order.portfolio_key != key:
                self._invalid("paper_portfolio_key_mismatch", line=line)
                return
            stored = ShadowOrder.from_dict(order.as_dict())
            self.orders[stored.order_id] = stored
            self.order_history.setdefault(stored.order_id, []).append(stored)
            self._idempotency[stored.idempotency_key] = stored.order_id
            return
        if kind == "transition_intent":
            transition_id = str(row.get("transition_id") or "")
            if not transition_id:
                self._invalid("transition_intent_missing_identity", line=line)
                return
            existing = self.transition_intents.get(transition_id)
            if existing is not None and _detail_signature(existing) != _detail_signature(row):
                self._invalid("duplicate_transition_intent_conflict", line=line)
                return
            self.transition_intents[transition_id] = row
            return
        if kind == "account_event":
            transition_id = str(row.get("transition_id") or row.get("event_key") or "")
            event = str(row.get("event") or "")
            details = row.get("details")
            if not transition_id or event not in _ACCOUNT_EVENTS or not isinstance(details, Mapping):
                self._invalid("invalid_account_event", line=line)
                return
            existing = self.committed_transitions.get(transition_id)
            if existing is not None and (
                str(existing.get("event")) != event
                or _detail_signature(existing.get("details") or {}) != _detail_signature(details)
            ):
                self._invalid("duplicate_transition_commit_conflict", line=line)
                return
            normalized = dict(row)
            normalized["transition_id"] = transition_id
            self.committed_transitions[transition_id] = normalized
            self.account_events.append(normalized)
            return
        if kind == "transition_abort":
            transition_id = str(row.get("transition_id") or "")
            if not transition_id or transition_id not in self.transition_intents:
                self._invalid("invalid_transition_abort", line=line)
                return
            self.aborted_transitions.add(transition_id)
            return
        if kind == "decision":
            if not isinstance(row.get("details"), Mapping):
                self._invalid("invalid_paper_decision", line=line)
                return
            self.decisions.append(dict(row))
            return
        if kind == "discrepancy":
            self.discrepancies.append(dict(row))
            return
        self._invalid("unknown_paper_record_type", line=line)

    def _append(self, row: Mapping[str, Any]) -> None:
        if self._append_blocked:
            raise PaperLedgerIntegrityError(f"paper ledger is not appendable: {self.ledger_state}")
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector(f"append:{row.get('record_type')}")
        _fsync_append(self.path, row)
        self.frontier += 1
        if self.ledger_state == "missing":
            self.ledger_state = "verified"

    def _new_row(self, record_type: str, **payload: Any) -> dict[str, Any]:
        return {
            "schema_version": PAPER_LEDGER_SCHEMA_VERSION,
            "ledger_kind": PAPER_LEDGER_KIND,
            "record_type": record_type,
            "recorded_at": datetime.now(UTC).isoformat(),
            "execution_enabled": False,
            **_json_value(payload),
        }

    def save(self, order: ShadowOrder, *, event_key: str | None = None) -> None:
        if event_key and event_key in self._seen_events:
            return
        row = self._new_row(
            "order",
            event_key=event_key,
            portfolio_key=order.portfolio_key.as_dict(),
            order=order.as_dict(),
        )
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector("order_save")
        self._append(row)
        # Preserve the caller's live object just like ``ShadowLedger`` does;
        # only the history copy is immutable. Replacing it here would make a
        # caller retain a stale RESTING object after a later cancel/expiry.
        self.orders[order.order_id] = order
        self.order_history.setdefault(order.order_id, []).append(
            ShadowOrder.from_dict(order.as_dict())
        )
        self._idempotency[order.idempotency_key] = order.order_id
        if event_key:
            self._seen_events.add(event_key)

    def by_idempotency(self, key: str) -> ShadowOrder | None:
        order_id = self._idempotency.get(key)
        return self.orders.get(order_id) if order_id else None

    def event_seen(self, event_key: str) -> bool:
        return event_key in self._seen_events

    def active(self, *, portfolio_key: TokenPortfolioKey | None = None) -> tuple[ShadowOrder, ...]:
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

    def begin_transition(
        self,
        *,
        transition_id: str,
        event: str,
        details: Mapping[str, Any],
        before_account: Mapping[str, Any],
        before_order: Mapping[str, Any] | None = None,
    ) -> bool:
        """Persist an effect intent before mutating in-memory account state."""

        if transition_id in self.committed_transitions:
            return False
        existing = self.transition_intents.get(transition_id)
        if existing is not None:
            if str(existing.get("event")) != event or _detail_signature(
                existing.get("details") or {}
            ) != _detail_signature(details):
                self.record_discrepancy(
                    code="transition_intent_conflict", details={"transition_id": transition_id}
                )
                raise PaperLedgerIntegrityError("transition_intent_conflict")
            return False
        row = self._new_row(
            "transition_intent",
            transition_id=transition_id,
            event=event,
            details=dict(details),
            before_account=dict(before_account),
            before_order=dict(before_order) if before_order is not None else None,
            commit_state="intent",
        )
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector("transition_intent")
        self._append(row)
        self.transition_intents[transition_id] = row
        return True

    def record_account_event(
        self,
        *,
        event_key: str,
        event: str,
        details: Mapping[str, Any],
        before_account: Mapping[str, Any] | None = None,
        after_account: Mapping[str, Any] | None = None,
        recovery_repair: bool = False,
    ) -> None:
        if event not in _ACCOUNT_EVENTS:
            raise ValueError("paper_account_unknown_or_invalid_event")
        if event_key in self.committed_transitions:
            return
        if event_key not in self.transition_intents:
            self.begin_transition(
                transition_id=event_key,
                event=event,
                details=details,
                before_account=before_account or {},
            )
        row = self._new_row(
            "account_event",
            event_key=event_key,
            transition_id=event_key,
            event=event,
            details=dict(details),
            after_account=dict(after_account or {}),
            commit_state="recovery_repair" if recovery_repair else "committed",
        )
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector("account_commit")
        self._append(row)
        self.committed_transitions[event_key] = row
        self.account_events.append(row)
        self._seen_events.add(event_key)
        if recovery_repair:
            self.recovery_repairs.append(event_key)

    def abort_transition(self, *, transition_id: str, reason: str) -> None:
        """Close an intent known not to have produced its associated order."""

        if transition_id in self.committed_transitions or transition_id in self.aborted_transitions:
            return
        if transition_id not in self.transition_intents:
            raise PaperLedgerIntegrityError("cannot_abort_unknown_transition")
        row = self._new_row(
            "transition_abort", transition_id=transition_id, reason=reason, commit_state="aborted"
        )
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector("transition_abort")
        self._append(row)
        self.aborted_transitions.add(transition_id)

    def record_decision(self, *, event_key: str, decision: str, details: Mapping[str, Any]) -> None:
        if event_key in self._seen_events:
            return
        row = self._new_row(
            "decision", event_key=event_key, decision=decision, details=dict(details)
        )
        if self._persistence_fault_injector is not None:
            self._persistence_fault_injector("decision_append")
        self._append(row)
        self.decisions.append(row)
        self._seen_events.add(event_key)

    def record_discrepancy(self, *, code: str, details: Mapping[str, Any]) -> None:
        row = self._new_row("discrepancy", code=code, details=dict(details))
        if not self._append_blocked:
            if self._persistence_fault_injector is not None:
                self._persistence_fault_injector("halt_append")
            self._append(row)
        self.discrepancies.append(row)

    @staticmethod
    def _effect_from_fill(order: ShadowOrder, fill: ShadowFill) -> PaperAccountEffect:
        if order.side.value == "BUY":
            event = "fill_buy"
            details: dict[str, Any] = {
                "portfolio_key": order.portfolio_key.as_dict(),
                "order_id": order.order_id,
                "fill_id": fill.fill_id,
                "shares": str(fill.shares),
                "price": str(fill.price),
                "reserved_notional_usd": str(fill.shares * order.limit_price),
                "fee_usd": str(fill.fee_usd),
            }
        else:
            event = "fill_sell"
            details = {
                "portfolio_key": order.portfolio_key.as_dict(),
                "order_id": order.order_id,
                "fill_id": fill.fill_id,
                "shares": str(fill.shares),
                "price": str(fill.price),
                "fee_usd": str(fill.fee_usd),
            }
        return PaperAccountEffect(
            transition_id=f"fill:{fill.fill_id}",
            event=event,
            details=details,
            timestamp=fill.timestamp,
        )

    def expected_order_account_effects(self) -> tuple[PaperAccountEffect, ...]:
        """Derive the unique account effects implied by final paper orders."""

        effects: list[PaperAccountEffect] = []
        for order in sorted(self.orders.values(), key=lambda row: (row.submitted_at, row.order_id)):
            if order.side.value == "BUY":
                effects.append(
                    PaperAccountEffect(
                        transition_id=f"submit:{order.order_id}",
                        event="reserve_buy",
                        details={
                            "portfolio_key": order.portfolio_key.as_dict(),
                            "order_id": order.order_id,
                            "notional_usd": str(order.requested_shares * order.limit_price),
                        },
                        timestamp=order.submitted_at,
                    )
                )
            effects.extend(self._effect_from_fill(order, fill) for fill in order.fills)
            terminal_at = order.cancelled_at or order.expired_at
            if order.side.value == "BUY" and terminal_at is not None and order.remaining_shares > ZERO:
                prefix = "cancel" if order.cancelled_at is not None else "expire"
                effects.append(
                    PaperAccountEffect(
                        transition_id=f"{prefix}:{order.order_id}:{terminal_at.isoformat()}",
                        event="release_buy",
                        details={
                            "portfolio_key": order.portfolio_key.as_dict(),
                            "order_id": order.order_id,
                            "notional_usd": str(order.remaining_shares * order.limit_price),
                        },
                        timestamp=terminal_at,
                    )
                )
        rank = {"reserve_buy": 0, "fill_buy": 1, "fill_sell": 1, "release_buy": 2}
        return tuple(sorted(effects, key=lambda row: (row.timestamp, rank[row.event], row.transition_id)))

    @staticmethod
    def _matches_expected(actual: Mapping[str, Any], expected: PaperAccountEffect) -> bool:
        if str(actual.get("event")) != expected.event:
            return False
        details = actual.get("details")
        if not isinstance(details, Mapping):
            return False
        return all(_json_value(details.get(key)) == _json_value(value) for key, value in expected.details.items())

    def reconcile_startup(self, *, initial_cash_usd: Decimal | str = "200") -> dict[str, Any]:
        """Repair uniquely implied effects before any new market input is read."""

        if self._startup_reconciled:
            return self.recovery_status()
        self._startup_reconciled = True
        if self.invalid:
            return self.recovery_status()
        for transition_id in self.unfinished_transitions:
            intent = self.transition_intents[transition_id]
            event = str(intent.get("event") or "")
            details = intent.get("details")
            if event not in _ACCOUNT_EVENTS or not isinstance(details, Mapping):
                self.record_discrepancy(
                    code="unfinished_transition_unrecoverable",
                    details={"transition_id": transition_id},
                )
                continue
            order_id = str(details.get("order_id") or "")
            if order_id:
                order = self.orders.get(order_id)
                fill_id = str(details.get("fill_id") or "")
                terminal = transition_id.startswith(("cancel:", "expire:"))
                durable_fact_exists = (
                    order is not None
                    and (
                        not fill_id
                        or any(fill.fill_id == fill_id for fill in order.fills)
                    )
                    and (
                        not terminal
                        or order.cancelled_at is not None
                        or order.expired_at is not None
                    )
                )
                if not durable_fact_exists:
                    self.record_discrepancy(
                        code="unfinished_transition_without_durable_fact",
                        details={"transition_id": transition_id, "order_id": order_id},
                    )
                    continue
            try:
                self.record_account_event(
                    event_key=transition_id,
                    event=event,
                    details=details,
                    before_account=intent.get("before_account")
                    if isinstance(intent.get("before_account"), Mapping)
                    else {},
                    recovery_repair=True,
                )
            except (OSError, PaperLedgerIntegrityError, ValueError) as exc:
                self.record_discrepancy(
                    code="unfinished_transition_repair_failed",
                    details={"transition_id": transition_id, "error": str(exc)},
                )
        for effect in self.expected_order_account_effects():
            actual = self.committed_transitions.get(effect.transition_id)
            if actual is not None:
                if not self._matches_expected(actual, effect):
                    self.record_discrepancy(
                        code="order_account_transition_conflict",
                        details={"transition_id": effect.transition_id},
                    )
                continue
            try:
                self.begin_transition(
                    transition_id=effect.transition_id,
                    event=effect.event,
                    details=effect.details,
                    before_account={},
                )
                self.record_account_event(
                    event_key=effect.transition_id,
                    event=effect.event,
                    details=effect.details,
                    recovery_repair=True,
                )
            except (OSError, PaperLedgerIntegrityError, ValueError) as exc:
                self.record_discrepancy(
                    code="deterministic_recovery_repair_failed",
                    details={"transition_id": effect.transition_id, "error": str(exc)},
                )
        if self.invalid:
            return self.recovery_status()
        try:
            expected_effects = self.expected_order_account_effects()
            expected = _restore_from_effects(
                expected_effects, initial_cash_usd=initial_cash_usd
            )
            expected_ids = {effect.transition_id for effect in expected_effects}
            allowed_extra_events = {"strand", "halt"}
            # A ledger with no persisted paper orders is also used by the
            # small account-only unit API.  Its explicitly committed effects
            # are authoritative there; orphan-effect checks apply only when
            # there is an order-derived economic authority to reconcile.
            for row in self.account_events:
                transition_id = str(row.get("transition_id") or row.get("event_key") or "")
                if transition_id in expected_ids:
                    continue
                event = str(row.get("event") or "")
                details = row.get("details")
                if not self.orders:
                    _apply_account_event(expected, event, details)
                    continue
                if event not in allowed_extra_events or not isinstance(details, Mapping):
                    self.record_discrepancy(
                        code="unexpected_non_order_account_effect",
                        details={"transition_id": transition_id, "event": event},
                    )
                    continue
                _apply_account_event(expected, event, details)
            restored = restore_paper_account(self, initial_cash_usd=initial_cash_usd, reconcile=False)
            active_buy_reservation = sum(
                (
                    order.remaining_shares * order.limit_price
                    for order in self.orders.values()
                    if order.side.value == "BUY" and order.is_active
                ),
                start=Decimal("0"),
            )
            if expected.buy_reserved_usd != active_buy_reservation:
                self.record_discrepancy(
                    code="active_buy_reservation_mismatch",
                    details={
                        "expected_active_reservation": str(active_buy_reservation),
                        "account_reserved": str(expected.buy_reserved_usd),
                    },
                )
            if self.orders and expected.as_dict() != restored.as_dict():
                self.record_discrepancy(
                    code="paper_account_order_reconciliation_mismatch",
                    details={"expected": expected.as_dict(), "restored": restored.as_dict()},
                )
        except (KeyError, ValueError, PaperLedgerIntegrityError) as exc:
            self.record_discrepancy(
                code="paper_account_startup_reconciliation_failed", details={"error": str(exc)}
            )
        return self.recovery_status()

    def recovery_status(self) -> dict[str, Any]:
        return {
            "ledger_state": self.ledger_state,
            "ledger_frontier": self.frontier,
            "unfinished_transitions": list(self.unfinished_transitions),
            "aborted_transitions": sorted(self.aborted_transitions),
            "recovery_repairs": list(self.recovery_repairs),
            "integrity_failures": list(self.legacy_invalid_records),
            "discrepancy_count": len(self.discrepancies),
        }


def _apply_account_event(account: PaperAccount, event: str, details: Mapping[str, Any]) -> None:
    key_data = details.get("portfolio_key")
    key = TokenPortfolioKey(**key_data) if isinstance(key_data, Mapping) else None
    if event == "reserve_buy" and key:
        account.reserve_buy(key, details["notional_usd"])
    elif event == "release_buy":
        account.release_buy(details["notional_usd"])
    elif event == "fill_buy" and key:
        account.fill_buy(
            key,
            shares=details["shares"],
            price=details["price"],
            reserved_notional_usd=details["reserved_notional_usd"],
            fee_usd=details["fee_usd"],
        )
    elif event == "fill_sell" and key:
        account.fill_sell(
            key, shares=details["shares"], price=details["price"], fee_usd=details["fee_usd"]
        )
    elif event == "strand" and key:
        account.strand(key)
    elif event == "halt":
        account.halt(str(details["reason"]))
    else:
        raise ValueError("paper_account_unknown_or_invalid_event")


def _restore_from_effects(
    effects: Iterable[PaperAccountEffect], *, initial_cash_usd: Decimal | str
) -> PaperAccount:
    account = PaperAccount(initial_cash_usd=_decimal(initial_cash_usd), cash_usd=_decimal(initial_cash_usd))
    for effect in effects:
        _apply_account_event(account, effect.event, effect.details)
    return account


def restore_paper_account(
    ledger: PaperLedger,
    *,
    initial_cash_usd: Decimal | str = "200",
    reconcile: bool = True,
) -> PaperAccount:
    """Rebuild account state from committed facts, never from a checkpoint."""

    if reconcile:
        ledger.reconcile_startup(initial_cash_usd=initial_cash_usd)
    try:
        expected = ledger.expected_order_account_effects()
        if expected:
            account = _restore_from_effects(expected, initial_cash_usd=initial_cash_usd)
            expected_ids = {effect.transition_id for effect in expected}
            extra_rows = [
                row
                for row in ledger.account_events
                if str(row.get("transition_id") or row.get("event_key") or "") not in expected_ids
            ]
        else:
            account = PaperAccount(
                initial_cash_usd=_decimal(initial_cash_usd), cash_usd=_decimal(initial_cash_usd)
            )
            extra_rows = list(ledger.account_events)
        for row in extra_rows:
            _apply_account_event(account, str(row["event"]), row["details"])
    except (KeyError, TypeError, ValueError):
        account = PaperAccount(initial_cash_usd=_decimal(initial_cash_usd), cash_usd=_decimal(initial_cash_usd))
        account.halt("paper_account_restore_failed")
        return account
    if ledger.invalid:
        account.halt("paper_ledger_integrity_failure")
    return account


def record_account_action(
    ledger: PaperLedger,
    account: PaperAccount,
    *,
    event_key: str,
    event: str,
    details: Mapping[str, Any],
) -> None:
    """Write-ahead one account action and commit it exactly once."""

    if event_key in ledger.committed_transitions:
        return
    payload = dict(details)
    before = account.as_dict()
    ledger.begin_transition(
        transition_id=event_key,
        event=event,
        details=payload,
        before_account=before,
    )
    try:
        if ledger._persistence_fault_injector is not None:
            ledger._persistence_fault_injector("account_effect_apply")
        _apply_account_event(account, event, payload)
    except ValueError as exc:
        account.halt(str(exc))
        ledger.record_discrepancy(code=str(exc), details={"event": event, "details": payload})
        raise
    ledger.record_account_event(
        event_key=event_key,
        event=event,
        details=payload,
        before_account=before,
        after_account=account.as_dict(),
    )


assert EXECUTION_ENABLED is False
