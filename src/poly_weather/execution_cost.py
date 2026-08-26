"""Read-only order-book depth execution cost estimates."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from poly_weather.fees import (
    FEE_PRECISION_USDC,
    LiquidityRole,
    configured_fee_rate,
    trading_fee_usdc,
)

BookLevel = tuple[Decimal | float | str, Decimal | float | str]
FillEstimate = tuple[Decimal, Decimal, float]


@dataclass(frozen=True, slots=True)
class ExecutionCostEstimate:
    average_fill_price: Decimal
    slippage_vs_top: Decimal
    filled_fraction: float
    filled_usd: Decimal
    filled_shares: Decimal
    fee_usdc: Decimal
    fee_per_share: Decimal
    liquidity_role: LiquidityRole
    fee_rate: Decimal


def estimate_execution_cost(
    book_side: Iterable[BookLevel],
    size_usd: Decimal | float | str,
    side: Literal["buy", "sell"],
    *,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    market_category: str = "weather",
    fee_rate: Decimal | float | str | None = None,
) -> ExecutionCostEstimate | None:
    """Walk depth and separately estimate slippage and official protocol fee."""
    requested_usd = Decimal(str(size_usd))
    if requested_usd <= 0:
        raise ValueError("size_usd must be positive")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    role = LiquidityRole(liquidity_role)
    rate = configured_fee_rate(market_category, override=fee_rate)
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
    filled_usd = Decimal(0)
    filled_shares = Decimal(0)
    unrounded_fee = Decimal(0)
    for price, shares_available in ordered:
        level_usd = price * shares_available
        take_usd = min(remaining_usd, level_usd)
        take_shares = take_usd / price
        filled_usd += take_usd
        filled_shares += take_shares
        unrounded_fee += trading_fee_usdc(
            take_shares,
            price,
            liquidity_role=role,
            market_category=market_category,
            fee_rate=rate,
            round_to_protocol_precision=False,
        )
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
    fee = unrounded_fee.quantize(FEE_PRECISION_USDC, rounding=ROUND_HALF_UP)
    return ExecutionCostEstimate(
        average_fill_price=average_fill_price,
        slippage_vs_top=max(Decimal(0), slippage_vs_top),
        filled_fraction=float(filled_usd / requested_usd),
        filled_usd=filled_usd,
        filled_shares=filled_shares,
        fee_usdc=fee,
        fee_per_share=fee / filled_shares,
        liquidity_role=role,
        fee_rate=rate,
    )


def estimate_execution_by_shares(
    book_side: Iterable[BookLevel],
    share_count: Decimal | float | str,
    side: Literal["buy", "sell"],
    *,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    market_category: str = "weather",
    fee_rate: Decimal | float | str | None = None,
) -> ExecutionCostEstimate | None:
    """Walk a book for an exact share quantity instead of a USD notional.

    This is needed to test whether the shares bought at entry can also be
    liquidated from the opposite side of the same archived book.  It is a
    depth-cost diagnostic, not a claim that a future exit book will be equal to
    the entry-time book.
    """
    requested_shares = Decimal(str(share_count))
    if requested_shares <= 0:
        raise ValueError("share_count must be positive")
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    role = LiquidityRole(liquidity_role)
    rate = configured_fee_rate(market_category, override=fee_rate)
    levels = [
        (Decimal(str(price)), Decimal(str(size)))
        for price, size in book_side
        if Decimal(str(price)) > 0 and Decimal(str(size)) > 0
    ]
    if not levels:
        return None
    ordered = sorted(levels, key=lambda level: level[0], reverse=side == "sell")
    top_price = ordered[0][0]
    remaining_shares = requested_shares
    filled_usd = Decimal(0)
    filled_shares = Decimal(0)
    unrounded_fee = Decimal(0)
    for price, shares_available in ordered:
        take_shares = min(remaining_shares, shares_available)
        filled_usd += price * take_shares
        filled_shares += take_shares
        unrounded_fee += trading_fee_usdc(
            take_shares,
            price,
            liquidity_role=role,
            market_category=market_category,
            fee_rate=rate,
            round_to_protocol_precision=False,
        )
        remaining_shares -= take_shares
        if remaining_shares <= 0:
            break
    if filled_shares <= 0:
        return None
    average_fill_price = filled_usd / filled_shares
    slippage_vs_top = (
        average_fill_price - top_price
        if side == "buy"
        else top_price - average_fill_price
    )
    fee = unrounded_fee.quantize(FEE_PRECISION_USDC, rounding=ROUND_HALF_UP)
    return ExecutionCostEstimate(
        average_fill_price=average_fill_price,
        slippage_vs_top=max(Decimal(0), slippage_vs_top),
        filled_fraction=float(filled_shares / requested_shares),
        filled_usd=filled_usd,
        filled_shares=filled_shares,
        fee_usdc=fee,
        fee_per_share=fee / filled_shares,
        liquidity_role=role,
        fee_rate=rate,
    )


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
    estimate = estimate_execution_cost(book_side, size_usd, side)
    if estimate is None:
        return None
    return (
        estimate.average_fill_price,
        estimate.slippage_vs_top,
        estimate.filled_fraction,
    )
