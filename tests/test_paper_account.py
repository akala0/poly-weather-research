from decimal import Decimal

import pytest

from poly_weather.paper_account import (
    PaperAccount,
    PaperLedger,
    record_account_action,
    restore_paper_account,
)
from poly_weather.shadow_orders import TokenPortfolioKey

KEY = TokenPortfolioKey("event", "market", "token", "2026-09-02")
OTHER_KEY = TokenPortfolioKey("event", "market-2", "token-2", "2026-09-02")


def action(ledger: PaperLedger, account: PaperAccount, key: str, event: str, **details: str) -> None:
    payload = dict(details)
    if event != "release_buy" and event != "halt":
        payload["portfolio_key"] = KEY.as_dict()
    record_account_action(ledger, account, event_key=key, event=event, details=payload)


def test_global_account_reserves_cash_and_conserves_value() -> None:
    account = PaperAccount()
    account.reserve_buy(KEY, "20")
    assert account.buy_reserved_usd == Decimal("20")
    assert account.available_cash_usd == Decimal("180")
    account.fill_buy(
        KEY,
        shares="10",
        price="0.70",
        reserved_notional_usd="7",
        fee_usd="0.01",
    )
    assert account.cash_usd == Decimal("192.99")
    assert account.buy_reserved_usd == Decimal("13")
    assert account.inventory_cost_usd == Decimal("7.0")
    account.release_buy("13")
    account.fill_sell(KEY, shares="10", price="0.80", fee_usd="0.02")
    assert account.cash_usd == Decimal("200.97")
    assert account.inventory_cost_usd == Decimal("0")
    assert account.realized_pnl_usd == Decimal("1.0")
    assert account.fees_usd == Decimal("0.03")
    assert account.equity_usd == Decimal("200.97")


def test_global_account_blocks_second_token_when_cash_reserved() -> None:
    account = PaperAccount()
    account.reserve_buy(KEY, "150")
    assert account.can_reserve_buy("51") is False
    assert account.can_reserve_buy("50") is True
    account.reserve_buy(OTHER_KEY, "50")
    assert account.available_cash_usd == Decimal("0")


def test_account_rejects_sell_above_same_token_inventory() -> None:
    account = PaperAccount()
    account.reserve_buy(KEY, "5")
    account.fill_buy(
        KEY,
        shares="10",
        price="0.50",
        reserved_notional_usd="5",
        fee_usd="0",
    )

    with pytest.raises(ValueError, match="invalid_sell"):
        account.fill_sell(KEY, shares="10.000000000000000001", price="0.60", fee_usd="0")


def test_account_keeps_stranded_cost_occupied() -> None:
    account = PaperAccount()
    account.reserve_buy(KEY, "20")
    account.fill_buy(KEY, shares="20", price="0.75", reserved_notional_usd="15", fee_usd="0")
    account.release_buy("5")
    account.strand(KEY)
    position = account.positions[KEY]
    assert position.stranded is True
    assert account.inventory_cost_usd == Decimal("0")
    assert account.stranded_inventory_cost_usd == Decimal("15.00")
    assert account.available_cash_usd == Decimal("185")
    with pytest.raises(ValueError, match="invalid_sell"):
        account.fill_sell(KEY, shares="1", price="0.70", fee_usd="0")


def test_paper_ledger_restores_account_events_and_idempotency(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl")
    account = PaperAccount()
    action(ledger, account, "reserve", "reserve_buy", notional_usd="20")
    action(
        ledger,
        account,
        "fill",
        "fill_buy",
        shares="10",
        price="0.70",
        reserved_notional_usd="7",
        fee_usd="0",
    )
    action(ledger, account, "release", "release_buy", notional_usd="13")
    action(ledger, account, "sell", "fill_sell", shares="10", price="0.80", fee_usd="0")
    action(ledger, account, "sell", "fill_sell", shares="10", price="0.80", fee_usd="0")

    restored = restore_paper_account(PaperLedger(ledger.path))
    assert restored.as_dict() == account.as_dict()
    assert restored.cash_usd == Decimal("201.00")



def test_discrepancy_is_durable_halt(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.jsonl")
    ledger.record_discrepancy(code="paper_account_conservation_failed", details={"source": "fixture"})
    restored = restore_paper_account(PaperLedger(ledger.path))
    assert restored.halted is True
    assert restored.halt_reason == "paper_ledger_integrity_failure"


def test_paper_ledger_rejects_v2_records_and_halts(tmp_path) -> None:
    path = tmp_path / "paper.jsonl"
    path.write_text('{"schema_version": 2, "record_type": "order"}\n', encoding="utf-8")
    ledger = PaperLedger(path)
    account = restore_paper_account(ledger)
    assert ledger.legacy_invalid_records
    assert account.halted is True
    assert account.halt_reason == "paper_ledger_integrity_failure"
