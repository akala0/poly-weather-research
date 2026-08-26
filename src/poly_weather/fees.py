"""Polymarket protocol fee estimates for read-only research.

The protocol charges the token that actually trades. For example, buying NO at
0.82 uses ``p=0.82``; substituting ``1-YES`` is only valid if that is genuinely
the NO token's execution price, which historical last-trade data cannot prove.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from types import MappingProxyType

import httpx

FEE_PRECISION_USDC = Decimal("0.00001")
MARKET_CATEGORY_TAKER_FEE_RATES = MappingProxyType(
    {
        "crypto": Decimal("0.07"),
        "sports": Decimal("0.05"),
        "finance": Decimal("0.04"),
        "politics": Decimal("0.04"),
        "economics": Decimal("0.05"),
        "culture": Decimal("0.05"),
        "weather": Decimal("0.05"),
        "other": Decimal("0.05"),
        "mentions": Decimal("0.04"),
        "tech": Decimal("0.04"),
        "geopolitics": Decimal("0"),
    }
)


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


def _fee_details(payload: object, *, token_id: str) -> dict[str, object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("fd"), dict):
        raise ValueError("CLOB market info has no fd fee details")
    tokens = payload.get("t")
    if not isinstance(tokens, list) or token_id not in {
        str(row.get("t")) for row in tokens if isinstance(row, dict)
    }:
        raise ValueError("CLOB market info does not contain the requested token")
    details = payload["fd"]
    return {
        "rate": Decimal(str(details["r"])),
        "exponent": int(details["e"]),
        "taker_only": bool(details["to"]),
        "maker_base_fee_bps": int(payload.get("mbf") or 0),
        "taker_base_fee_bps": int(payload.get("tbf") or 0),
    }


def configured_fee_rate(
    market_category: str = "weather", *, override: Decimal | float | str | None = None
) -> Decimal:
    if override is not None:
        rate = Decimal(str(override))
    else:
        try:
            rate = MARKET_CATEGORY_TAKER_FEE_RATES[market_category.casefold()]
        except KeyError as exc:
            raise ValueError(f"unknown Polymarket fee category: {market_category}") from exc
    if not Decimal(0) <= rate <= Decimal(1):
        raise ValueError("fee rate must be between zero and one")
    return rate


def trading_fee_usdc(
    shares: Decimal | float | str,
    token_price: Decimal | float | str,
    *,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    market_category: str = "weather",
    fee_rate: Decimal | float | str | None = None,
    round_to_protocol_precision: bool = True,
) -> Decimal:
    """Return ``shares * feeRate * p * (1-p)`` for the traded token.

    Research estimates default to taker because all existing executable paths
    consume the order book. Makers pay zero. Protocol amounts are rounded to
    five USDC decimals; callers may disable rounding while aggregating book
    levels and round the final total once.
    """
    quantity = Decimal(str(shares))
    price = Decimal(str(token_price))
    role = LiquidityRole(liquidity_role)
    if quantity < 0:
        raise ValueError("shares cannot be negative")
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError("token price must be between zero and one")
    if role is LiquidityRole.MAKER or quantity == 0:
        return Decimal(0)
    rate = configured_fee_rate(market_category, override=fee_rate)
    amount = quantity * rate * price * (Decimal(1) - price)
    if round_to_protocol_precision:
        return amount.quantize(FEE_PRECISION_USDC, rounding=ROUND_HALF_UP)
    return amount


def fee_per_share(
    token_price: Decimal | float | str,
    *,
    liquidity_role: LiquidityRole | str = LiquidityRole.TAKER,
    market_category: str = "weather",
    fee_rate: Decimal | float | str | None = None,
) -> Decimal:
    """Return the unrounded analytical fee for one share at its own price."""
    return trading_fee_usdc(
        Decimal(1),
        token_price,
        liquidity_role=liquidity_role,
        market_category=market_category,
        fee_rate=fee_rate,
        round_to_protocol_precision=False,
    )


async def fetch_token_fee_rate_bps(
    client: httpx.AsyncClient, token_id: str, *, base_url: str = "https://clob.polymarket.com"
) -> int:
    """Read the public CLOB ``GET /fee-rate`` value for offline verification."""
    response = await client.get(f"{base_url.rstrip('/')}/fee-rate", params={"token_id": token_id})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or isinstance(payload.get("base_fee"), bool):
        raise ValueError("CLOB fee-rate response has no integer base_fee")
    base_fee = int(payload["base_fee"])
    if base_fee < 0:
        raise ValueError("CLOB base_fee cannot be negative")
    return base_fee


async def fetch_market_fee_details(
    client: httpx.AsyncClient,
    condition_id: str,
    token_id: str,
    *,
    base_url: str = "https://clob.polymarket.com",
) -> dict[str, object]:
    """Read V2 market ``fd`` rate/exponent; ``base_fee`` alone is not feeRate."""
    response = await client.get(f"{base_url.rstrip('/')}/clob-markets/{condition_id}")
    response.raise_for_status()
    return _fee_details(response.json(), token_id=token_id)
