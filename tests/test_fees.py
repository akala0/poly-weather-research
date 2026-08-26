import asyncio
from decimal import Decimal

import httpx

from poly_weather.execution_cost import estimate_execution_cost
from poly_weather.fees import (
    LiquidityRole,
    fee_per_share,
    fetch_market_fee_details,
    fetch_token_fee_rate_bps,
    trading_fee_usdc,
)


def test_weather_taker_fee_peaks_at_half_and_is_symmetric() -> None:
    assert trading_fee_usdc(100, "0.50") == Decimal("1.25000")
    assert trading_fee_usdc(100, "0.10") == Decimal("0.45000")
    assert trading_fee_usdc(100, "0.90") == Decimal("0.45000")
    assert trading_fee_usdc(100, "0.000001") == Decimal("0.00000")
    assert trading_fee_usdc(100, "0.999999") == Decimal("0.00000")


def test_maker_fee_is_zero() -> None:
    assert (
        trading_fee_usdc(100, "0.50", liquidity_role=LiquidityRole.MAKER)
        == Decimal(0)
    )


def test_no_fee_uses_no_tokens_own_execution_price() -> None:
    no_price = Decimal("0.82")
    stale_yes_complement = Decimal("0.75")
    assert fee_per_share(no_price) == Decimal("0.007380")
    assert fee_per_share(no_price) != fee_per_share(stale_yes_complement)


def test_depth_estimate_separates_slippage_and_fee() -> None:
    estimate = estimate_execution_cost(
        [("0.50", "100"), ("0.60", "100")], "80", "buy"
    )
    assert estimate is not None
    assert estimate.average_fill_price > Decimal("0.50")
    assert estimate.slippage_vs_top > 0
    assert estimate.fee_usdc > 0
    assert estimate.liquidity_role is LiquidityRole.TAKER


def test_public_fee_rate_lookup_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fee-rate"
        assert request.url.params["token_id"] == "no-token"
        return httpx.Response(200, request=request, json={"base_fee": 500})

    async def lookup() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_token_fee_rate_bps(client, "no-token")

    assert asyncio.run(lookup()) == 500


def test_clob_market_info_is_authoritative_curve_check() -> None:
    token_id = "no-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/clob-markets/condition"
        return httpx.Response(
            200,
            request=request,
            json={
                "mbf": 1000,
                "tbf": 1000,
                "fd": {"r": 0.05, "e": 1, "to": True},
                "t": [{"t": token_id, "o": "No"}],
            },
        )

    async def lookup() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_market_fee_details(client, "condition", token_id)

    assert asyncio.run(lookup()) == {
        "rate": Decimal("0.05"),
        "exponent": 1,
        "taker_only": True,
        "maker_base_fee_bps": 1000,
        "taker_base_fee_bps": 1000,
    }
