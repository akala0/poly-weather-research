"""Read-only order-book depth execution cost estimates."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal

BookLevel = tuple[Decimal | float | str, Decimal | float | str]
FillEstimate = tuple[Decimal, Decimal, float]


def estimate_fill_price(
    book_side: Iterable[BookLevel],
    size_usd: Decimal | float | str,
    side: Literal["buy", "sell"],
) -> FillEstimate | None:
    """Estimate average price, adverse top slippage, and USD filled fraction.

    ``buy`` consumes asks from low to high; ``sell`` consumes bids from high to
    low. Level size is interpreted as shares and ``size_usd`` as quote notional.
    An empty usable book returns ``None`` rather than falling back to a top quote.
    """
    requested_usd = Decimal(str(size_usd))
    if requested_usd <= 0:
        raise ValueError("size_usd must be positive")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    levels = [
        (Decimal(str(price)), Decimal(str(size)))
        for price, size in book_side
        if Decimal(str(price)) > 0 and Decimal(str(size)) > 0
    ]
    if not levels:
        return None
    ordered = sorted(levels, key=lambda level: level[0], reverse=side == "sell")
    top_price = ordered[0][0]
    remaining_usd = requested_usd
    filled_usd = Decimal("0")
    filled_shares = Decimal("0")
    for price, shares_available in ordered:
        level_usd = price * shares_available
        take_usd = min(remaining_usd, level_usd)
        filled_usd += take_usd
        filled_shares += take_usd / price
        remaining_usd -= take_usd
        if remaining_usd <= 0:
            break
    if filled_shares <= 0:
        return None
    average_fill_price = filled_usd / filled_shares
    slippage_vs_top = (
        average_fill_price - top_price
        if side == "buy"
        else top_price - average_fill_price
    )
    return (
        average_fill_price,
        max(Decimal("0"), slippage_vs_top),
        float(filled_usd / requested_usd),
    )
