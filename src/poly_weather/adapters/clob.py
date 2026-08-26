from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from poly_weather.domain import MarketPricePoint, MarketPriceSeries, MarketQuote


class PriceHistoryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_url: str
    fetched_at: datetime
    series: tuple[MarketPriceSeries, ...]
    raw: dict[str, Any]


class QuoteBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_url: str
    fetched_at: datetime
    quotes: tuple[MarketQuote, ...]
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    """One current, public CLOB book including market-rule metadata."""

    token_id: str
    fetched_at: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    min_order_size: Decimal
    tick_size: Decimal
    last_trade_price: Decimal | None
    raw: dict[str, Any]


class ClobClient:
    """Unauthenticated read-only client for Polymarket CLOB price history."""

    def __init__(
        self,
        *,
        base_url: str = "https://clob.polymarket.com",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ClobClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def batch_price_history(
        self,
        *,
        token_ids: tuple[str, ...],
        start: datetime,
        end: datetime,
        fidelity_minutes: int = 60,
    ) -> PriceHistoryBatch:
        unique_tokens = tuple(dict.fromkeys(token.strip() for token in token_ids if token.strip()))
        if not unique_tokens:
            raise ValueError("at least one token id is required")
        if len(unique_tokens) > 20:
            raise ValueError("Polymarket batch price history accepts at most 20 token ids")
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if end_utc <= start_utc:
            raise ValueError("price history end must be later than start")
        if fidelity_minutes < 1:
            raise ValueError("fidelity_minutes must be positive")

        response = self._client.post(
            "/batch-prices-history",
            json={
                "markets": list(unique_tokens),
                "start_ts": int(start_utc.timestamp()),
                "end_ts": int(end_utc.timestamp()),
                "fidelity": fidelity_minutes,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("history"), dict):
            raise ValueError("CLOB batch price history response has no history object")
        fetched_at = datetime.now(UTC)
        parsed_series: list[MarketPriceSeries] = []
        history = payload["history"]
        for token_id in unique_tokens:
            raw_points = history.get(token_id) or []
            if not isinstance(raw_points, list):
                raise ValueError(f"CLOB history for token {token_id} is not a list")
            points: list[MarketPricePoint] = []
            for raw_point in raw_points:
                if not isinstance(raw_point, dict) or "t" not in raw_point or "p" not in raw_point:
                    raise ValueError(f"CLOB history for token {token_id} contains an invalid point")
                points.append(
                    MarketPricePoint(
                        token_id=token_id,
                        timestamp=datetime.fromtimestamp(int(raw_point["t"]), tz=UTC),
                        price=Decimal(str(raw_point["p"])),
                    )
                )
            parsed_series.append(
                MarketPriceSeries(
                    source="Polymarket CLOB batch price history",
                    token_id=token_id,
                    fetched_at=fetched_at,
                    points=tuple(sorted(points, key=lambda point: point.timestamp)),
                )
            )
        return PriceHistoryBatch(
            request_url=str(response.request.url),
            fetched_at=fetched_at,
            series=tuple(parsed_series),
            raw=payload,
        )

    def batch_quotes(self, *, token_ids: tuple[str, ...]) -> QuoteBatch:
        """Fetch public best bid and ask prices for multiple tokens."""
        unique_tokens = tuple(dict.fromkeys(token.strip() for token in token_ids if token.strip()))
        if not unique_tokens:
            raise ValueError("at least one token id is required")
        request_rows = [
            {"token_id": token_id, "side": side}
            for token_id in unique_tokens
            for side in ("BUY", "SELL")
        ]
        response = self._client.post("/prices", json=request_rows)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("CLOB prices response is not an object")
        fetched_at = datetime.now(UTC)
        quotes: list[MarketQuote] = []
        for token_id in unique_tokens:
            raw_sides = payload.get(token_id) or {}
            if not isinstance(raw_sides, dict):
                raise ValueError(f"CLOB prices for token {token_id} is not an object")
            bid = None if raw_sides.get("BUY") is None else Decimal(str(raw_sides["BUY"]))
            ask = None if raw_sides.get("SELL") is None else Decimal(str(raw_sides["SELL"]))
            midpoint = None
            spread = None
            if bid is not None and ask is not None:
                midpoint = (bid + ask) / Decimal(2)
                spread = ask - bid
                if spread < 0:
                    raise ValueError(f"CLOB best ask is below best bid for token {token_id}")
            quotes.append(
                MarketQuote(
                    token_id=token_id,
                    fetched_at=fetched_at,
                    best_bid=bid,
                    best_ask=ask,
                    midpoint=midpoint,
                    spread=spread,
                )
            )
        return QuoteBatch(
            request_url=str(response.request.url),
            fetched_at=fetched_at,
            quotes=tuple(quotes),
            raw=payload,
        )

    def order_book(self, *, token_id: str) -> OrderBookSnapshot:
        """Fetch one current public book; this method never submits an order.

        ``min_order_size`` and ``tick_size`` are current market metadata.  They
        are deliberately kept separate from the historical WebSocket archive,
        whose old frames do not reliably contain ``min_order_size``.
        """
        normalized_token = token_id.strip()
        if not normalized_token:
            raise ValueError("token_id must not be empty")
        response = self._client.get("/book", params={"token_id": normalized_token})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("CLOB order-book response is not an object")

        def levels(name: str) -> tuple[tuple[Decimal, Decimal], ...]:
            raw_levels = payload.get(name)
            if not isinstance(raw_levels, list):
                raise ValueError(f"CLOB order-book response has invalid {name}")
            parsed: list[tuple[Decimal, Decimal]] = []
            for row in raw_levels:
                if not isinstance(row, dict) or "price" not in row or "size" not in row:
                    raise ValueError(f"CLOB order-book {name} contains an invalid level")
                price = Decimal(str(row["price"]))
                size = Decimal(str(row["size"]))
                if price < 0 or price > 1 or size < 0:
                    raise ValueError(f"CLOB order-book {name} contains an invalid value")
                parsed.append((price, size))
            return tuple(parsed)

        try:
            min_order_size = Decimal(str(payload["min_order_size"]))
            tick_size = Decimal(str(payload["tick_size"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("CLOB order-book response lacks valid market rules") from exc
        if min_order_size <= 0 or tick_size <= 0:
            raise ValueError("CLOB order-book response has non-positive market rules")
        last_trade = payload.get("last_trade_price")
        last_trade_price = None if last_trade is None else Decimal(str(last_trade))
        return OrderBookSnapshot(
            token_id=str(payload.get("asset_id") or normalized_token),
            fetched_at=datetime.now(UTC),
            bids=levels("bids"),
            asks=levels("asks"),
            min_order_size=min_order_size,
            tick_size=tick_size,
            last_trade_price=last_trade_price,
            raw=payload,
        )
