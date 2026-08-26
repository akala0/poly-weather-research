from datetime import UTC, datetime
from decimal import Decimal

import httpx

from poly_weather.adapters.polymarket_data import (
    PolymarketDataClient,
    PublicTrade,
    last_trade_at_or_before,
)


def _row(timestamp: int, *, suffix: str) -> dict[str, object]:
    return {
        "proxyWallet": f"wallet-{suffix}",
        "asset": "yes-token",
        "conditionId": "condition",
        "eventSlug": "event",
        "slug": "market",
        "outcome": "Yes",
        "side": "BUY",
        "size": "2.5",
        "price": "0.42",
        "timestamp": timestamp,
        "transactionHash": f"tx-{suffix}",
    }


def test_trade_client_bisects_at_page_cap_and_uses_taker_rows() -> None:
    calls: list[tuple[int, int, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        taker_only = request.url.params["takerOnly"]
        calls.append((start, end, taker_only))
        rows = [_row(1, suffix="a"), _row(9, suffix="b")] if (start, end) == (0, 10) else [
            _row(start, suffix=f"{start}-{end}")
        ]
        return httpx.Response(200, request=request, json=rows)

    http_client = httpx.Client(
        base_url="https://data-api.polymarket.com",
        transport=httpx.MockTransport(handler),
    )
    client = PolymarketDataClient(
        client=http_client, request_pause_seconds=0, page_limit=2
    )
    trades = client.event_trades(
        event_id="1",
        start=datetime.fromtimestamp(0, tz=UTC),
        end=datetime.fromtimestamp(10, tz=UTC),
    )

    assert calls == [(0, 10, "true"), (0, 5, "true"), (6, 10, "true")]
    assert [trade.timestamp.timestamp() for trade in trades] == [0, 6]


def test_last_trade_at_or_before_never_uses_future_execution() -> None:
    def trade(timestamp: int) -> PublicTrade:
        return PublicTrade(
            proxy_wallet="wallet",
            asset_id="asset",
            condition_id="condition",
            event_slug="event",
            market_slug="market",
            outcome="Yes",
            side="BUY",
            size=Decimal("1"),
            price=Decimal("0.5"),
            timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
            transaction_hash=f"tx-{timestamp}",
        )

    selected = last_trade_at_or_before(
        [trade(100), trade(200)],
        asset_id="asset",
        cutoff=datetime.fromtimestamp(150, tz=UTC),
    )

    assert selected is not None
    assert selected.timestamp == datetime.fromtimestamp(100, tz=UTC)
