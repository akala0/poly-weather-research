from datetime import UTC, datetime
from decimal import Decimal

from poly_weather.domain import Market, MarketPricePoint
from poly_weather.market_lag import (
    latest_price_at_or_before,
    proxy_trade_pnl,
    resolved_yes_market,
)


def _market(market_id: str, yes_price: str) -> Market:
    return Market.from_gamma(
        {
            "id": market_id,
            "slug": f"event-{market_id}-76-77f",
            "question": market_id,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{yes_price}", "{Decimal("1") - Decimal(yes_price)}"]',
            "clobTokenIds": f'["yes-{market_id}", "no-{market_id}"]',
        }
    )


def test_latest_market_price_is_strictly_no_lookahead() -> None:
    cutoff = datetime(2026, 8, 20, 17, 0, tzinfo=UTC)
    points = [
        MarketPricePoint(
            token_id="yes",
            timestamp=datetime(2026, 8, 20, 16, 59, tzinfo=UTC),
            price=Decimal("0.40"),
        ),
        MarketPricePoint(
            token_id="yes",
            timestamp=cutoff,
            price=Decimal("0.50"),
        ),
        MarketPricePoint(
            token_id="yes",
            timestamp=datetime(2026, 8, 20, 17, 1, tzinfo=UTC),
            price=Decimal("0.90"),
        ),
    ]

    selected = latest_price_at_or_before(points, cutoff)

    assert selected is not None
    assert selected.price == Decimal("0.50")


def test_proxy_trade_pnl_handles_exit_and_binary_settlement() -> None:
    exited = proxy_trade_pnl(entry_price=Decimal("0.70"), exit_price=Decimal("0.95"))
    won = proxy_trade_pnl(entry_price=Decimal("0.70"), resolved_yes=True)
    lost = proxy_trade_pnl(entry_price=Decimal("0.70"), resolved_yes=False)

    assert exited.pnl == Decimal("0.25")
    assert won.pnl == Decimal("0.30")
    assert lost.pnl == Decimal("-0.70")


def test_resolved_bucket_is_unique_yes_outcome_at_one() -> None:
    loser = _market("loser", "0")
    winner = _market("winner", "1")

    assert resolved_yes_market([loser, winner]).market_id == "winner"
