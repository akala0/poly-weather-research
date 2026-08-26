from datetime import UTC, datetime
from decimal import Decimal

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.trade_tape_analysis import analyze_trade_tape_staleness


def _trade(timestamp: int, *, outcome: str, asset: str, price: str) -> PublicTrade:
    return PublicTrade(
        proxy_wallet="wallet",
        asset_id=asset,
        condition_id="condition",
        event_slug="event",
        market_slug="market",
        outcome=outcome,
        side="BUY",
        size=Decimal("10"),
        price=Decimal(price),
        timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
        transaction_hash=f"tx-{timestamp}-{outcome}",
    )


def test_trade_tape_staleness_is_strictly_no_lookahead() -> None:
    first = int(datetime(2026, 8, 10, 12, tzinfo=UTC).timestamp())
    catalog = {
        "events": [
            {
                "event_slug": "event",
                "target_date": "2026-08-10",
                "timezone": "UTC",
                "markets": [],
            }
        ]
    }
    histories = {
        "event": {
            "markets": [
                {
                    "market_slug": "market",
                    "yes_token_id": "yes",
                    "history": [
                        {"t": first + 60, "p": 0.40},
                        {"t": first + 180, "p": 0.45},
                    ],
                }
            ]
        }
    }
    trades = {
        "event": [
            _trade(first, outcome="Yes", asset="yes", price="0.40"),
            _trade(first + 120, outcome="Yes", asset="yes", price="0.45"),
            _trade(first + 200, outcome="Yes", asset="yes", price="0.90"),
            _trade(first + 130, outcome="No", asset="no", price="0.55"),
        ]
    }

    result = analyze_trade_tape_staleness(
        catalog,
        histories_by_event=histories,
        trades_by_event=trades,
        elimination_times={
            ("event", "market"): datetime.fromtimestamp(first + 125, tz=UTC)
        },
    )

    samples = result["sample_rows"]
    assert samples[0]["last_yes_trade_at"] == datetime.fromtimestamp(
        first, tz=UTC
    ).isoformat()
    assert samples[1]["last_yes_trade_at"] == datetime.fromtimestamp(
        first + 120, tz=UTC
    ).isoformat()
    assert all(
        datetime.fromisoformat(row["last_yes_trade_at"])
        <= datetime.fromisoformat(row["sample_at"])
        for row in samples
    )
    market = result["market_rows"][0]
    assert market["post_elimination_trade_count"] == 2
    assert market["post_elimination_no_trade_count"] == 1
    assert result["physically_eliminated_market_count"] == 1
