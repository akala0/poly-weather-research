from decimal import Decimal

import pytest

from poly_weather.execution_cost import estimate_execution_by_shares, estimate_fill_price


def test_buy_fill_walks_asks_and_reports_top_slippage() -> None:
    estimate = estimate_fill_price(
        [(Decimal("0.50"), Decimal("100")), (Decimal("0.60"), Decimal("200"))],
        Decimal("100"),
        "buy",
    )

    assert estimate is not None
    average, slippage, filled_fraction = estimate
    assert float(average) == pytest.approx(100 / (100 + 50 / 0.60))
    assert slippage == average - Decimal("0.50")
    assert filled_fraction == 1.0


def test_sell_fill_walks_bids_from_high_to_low() -> None:
    estimate = estimate_fill_price(
        [(Decimal("0.40"), Decimal("200")), (Decimal("0.50"), Decimal("100"))],
        Decimal("75"),
        "sell",
    )

    assert estimate is not None
    average, slippage, filled_fraction = estimate
    assert float(average) == pytest.approx(75 / (100 + 25 / 0.40))
    assert slippage == Decimal("0.50") - average
    assert filled_fraction == 1.0


def test_fill_reports_partial_depth_and_empty_book() -> None:
    partial = estimate_fill_price([("0.50", "100")], "100", "buy")

    assert partial is not None
    assert partial[2] == 0.5
    assert estimate_fill_price([], "100", "buy") is None


def test_share_sized_sell_walks_bids_for_exact_entry_inventory() -> None:
    estimate = estimate_execution_by_shares(
        [("0.95", "10"), ("0.90", "10")], "15", "sell"
    )

    assert estimate is not None
    assert estimate.filled_shares == Decimal("15")
    assert estimate.filled_fraction == 1.0
    assert estimate.average_fill_price == Decimal("14.0") / Decimal("15")
    assert estimate.slippage_vs_top == Decimal("0.95") - estimate.average_fill_price
