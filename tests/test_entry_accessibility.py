from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.adapters.clob import OrderBookSnapshot
from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.domain import MarketPricePoint
from poly_weather.entry_accessibility import (
    analyze_no_entry_accessibility,
    current_rule_rows,
)

BASE = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _pair(
    *,
    minute: int,
    asset_id: str,
    bids: list[dict[str, str]],
    asks: list[dict[str, str]],
    last_trade: str | None,
    suffix: str,
) -> dict[str, object]:
    timestamp = BASE + timedelta(minutes=minute)
    return {
        "observed_at": timestamp,
        "event_slug": "la-event",
        "market_slug": f"la-event/la-event-{suffix}",
        "no": {
            "_timestamp": timestamp,
            "asset_id": asset_id,
            "market_id": f"condition-{asset_id}",
            "bids": bids,
            "asks": asks,
            "best_ask": "1.000" if asset_id == "b" else None,
            "last_trade_price": last_trade,
        },
    }


def _trade(asset_id: str, timestamp: datetime, price: str) -> PublicTrade:
    return PublicTrade(
        proxy_wallet="wallet",
        asset_id=asset_id,
        condition_id=f"condition-{asset_id}",
        event_slug="la-event",
        market_slug="market",
        outcome="No",
        side="BUY",
        size=Decimal("5"),
        price=Decimal(price),
        timestamp=timestamp,
        transaction_hash=f"tx-{asset_id}-{timestamp.isoformat()}",
    )


def test_entry_accessibility_uses_only_prior_p_and_trade_and_separates_empty_ask() -> None:
    pairs = [
        _pair(
            minute=0,
            asset_id="a",
            bids=[{"price": "0.990", "size": "100"}],
            asks=[
                {"price": "0.995", "size": "5"},
                {"price": "0.999", "size": "100"},
            ],
            last_trade="0.990",
            suffix="75forbelow",
        ),
        _pair(
            minute=5,
            asset_id="b",
            bids=[{"price": "0.999", "size": "100"}],
            asks=[],
            last_trade="0.999",
            suffix="77forbelow",
        ),
        _pair(
            minute=10,
            asset_id="c",
            bids=[{"price": "0.999", "size": "100"}],
            asks=[{"price": "1.000", "size": "100"}],
            last_trade="0.999",
            suffix="88-89f",
        ),
        _pair(
            minute=15,
            asset_id="d",
            bids=[{"price": "0.800", "size": "100"}],
            asks=[{"price": "0.995", "size": "100"}],
            last_trade="0.999",
            suffix="90-91f",
        ),
    ]
    metadata = {
        "la-event": {
            "station_id": "KLAX",
            "timezone": "UTC",
            "target_date": "2026-08-26",
        }
    }
    prices = {
        "a": [
            MarketPricePoint(token_id="a", timestamp=BASE - timedelta(minutes=30), price=Decimal("0.990")),
            MarketPricePoint(token_id="a", timestamp=BASE + timedelta(minutes=30), price=Decimal("0.100")),
        ],
        "b": [MarketPricePoint(token_id="b", timestamp=BASE, price=Decimal("0.999"))],
        "c": [MarketPricePoint(token_id="c", timestamp=BASE, price=Decimal("0.999"))],
    }
    trades = {
        "a": [
            _trade("a", BASE - timedelta(minutes=90), "0.990"),
            _trade("a", BASE + timedelta(minutes=30), "0.100"),
        ],
        "b": [_trade("b", BASE, "0.999")],
        "c": [_trade("c", BASE, "0.999")],
    }

    result = analyze_no_entry_accessibility(
        pairs,
        event_metadata=metadata,
        typical_peak_minutes_by_station={"KLAX": 12 * 60},
        price_points_by_asset=prices,
        trades_by_asset=trades,
    )

    first = result["records"][0]
    assert first["p"] == 0.99
    assert first["last_public_trade_at"] == (BASE - timedelta(minutes=90)).isoformat()
    assert first["ask_minus_p"] == 0.005
    assert first["frontend_source"] == "midpoint"
    assert result["records"][3]["frontend_source"] == "last_trade"
    scope = result["scope_summaries"]["KLAX"]
    assert scope["literal_no_ask"]["count"] == 1
    assert scope["ask_exactly_one"]["count"] == 1
    assert scope["reported_best_ask_one_without_depth"]["count"] == 1
    assert scope["ask_at_or_above_0999"]["count"] == 1
    buy_20 = next(row for row in scope["sizes"] if row["size_usd"] == 20.0)
    assert buy_20["entry_full_fill"]["count"] == 3
    assert buy_20["primary_reasons"]["no_resting_ask"]["count"] == 1


def test_current_rule_rows_preserves_current_rule_boundary() -> None:
    target = {
        "asset_id": "a",
        "station_id": "KLAX",
        "event_slug": "la-event",
        "market_slug": "la-event/la-event-75forbelow",
        "bucket": "≤75°F",
        "target_date": "2026-08-26",
        "observed_at": BASE.isoformat(),
    }
    book = OrderBookSnapshot(
        token_id="a",
        fetched_at=BASE,
        bids=((Decimal("0.990"), Decimal("10")),),
        asks=((Decimal("0.999"), Decimal("10")),),
        min_order_size=Decimal("5"),
        tick_size=Decimal("0.001"),
        last_trade_price=Decimal("0.999"),
        raw={},
    )

    rows = current_rule_rows([target], books_by_asset={"a": book})

    assert rows == [
        {
            **target,
            "status": "observed",
            "observed_at": BASE.isoformat(),
            "min_order_size": 5.0,
            "tick_size": 0.001,
            "top_ask": 0.999,
            "minimum_notional_at_top": 4.995,
        }
    ]
