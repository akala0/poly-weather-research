"""Public, read-only Polymarket Data API trade tape client."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from poly_weather.trade_evidence import parse_trade_timestamp


@dataclass(frozen=True, slots=True)
class PublicTrade:
    proxy_wallet: str
    asset_id: str
    condition_id: str
    event_slug: str
    market_slug: str
    outcome: str
    side: str
    size: Decimal
    price: Decimal
    timestamp: datetime
    transaction_hash: str
    # Local receipt time is optional for historical API responses.  When a
    # persisted tape supplies it, replay uses it as an availability fence so
    # a later API fetch cannot fill an earlier shadow order retroactively.
    available_at: datetime | None = None
    source_timestamp_text: str | None = None
    receipt_timestamp_text: str | None = None

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["size"] = str(self.size)
        payload["price"] = str(self.price)
        payload["timestamp"] = self.timestamp.isoformat()
        if self.available_at is not None:
            payload["available_at"] = self.available_at.isoformat()
        return payload


def parse_public_trade(row: dict[str, Any]) -> PublicTrade:
    timestamp = parse_trade_timestamp(row["timestamp"])
    if timestamp is None:
        raise ValueError("invalid or imprecise public trade timestamp")
    return PublicTrade(
        proxy_wallet=str(row.get("proxyWallet") or ""),
        asset_id=str(row["asset"]),
        condition_id=str(row["conditionId"]),
        event_slug=str(row["eventSlug"]),
        market_slug=str(row["slug"]),
        outcome=str(row["outcome"]),
        side=str(row["side"]).upper(),
        size=Decimal(str(row["size"])),
        price=Decimal(str(row["price"])),
        timestamp=timestamp,
        transaction_hash=str(row["transactionHash"]),
        source_timestamp_text=str(row["timestamp"]),
    )


def last_trade_at_or_before(
    trades: Sequence[PublicTrade],
    *,
    asset_id: str,
    cutoff: datetime,
) -> PublicTrade | None:
    """Return only executions observable by ``cutoff``; future rows are ignored."""
    cutoff_utc = cutoff.astimezone(UTC)
    eligible = [
        trade
        for trade in trades
        if trade.asset_id == asset_id and trade.timestamp <= cutoff_utc
    ]
    return max(eligible, key=lambda item: item.timestamp, default=None)


class PolymarketDataClient:
    """Unauthenticated client for the public Data API; never submits orders."""

    def __init__(
        self,
        *,
        base_url: str = "https://data-api.polymarket.com",
        client: httpx.Client | None = None,
        request_pause_seconds: float = 0.06,
        page_limit: int = 10_000,
        maximum_offset: int = 10_000,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10),
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )
        self.request_pause_seconds = request_pause_seconds
        if not 1 <= page_limit <= 10_000:
            raise ValueError("Data API trade page limit must be between 1 and 10,000")
        if maximum_offset < 0 or maximum_offset > 10_000:
            raise ValueError("Data API trade maximum offset must be between 0 and 10,000")
        self.page_limit = page_limit
        self.maximum_offset = maximum_offset

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PolymarketDataClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def event_trades(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        taker_only: bool = True,
    ) -> list[PublicTrade]:
        """Fetch a complete event tape, bisecting any range that hits the cap.

        The canonical research tape uses ``taker_only=True`` so every match is
        represented once. ``takerOnly=false`` returns participant-side mirrors
        that cannot be losslessly collapsed when one order fills many makers.
        """
        start_epoch = int(start.astimezone(UTC).timestamp())
        end_epoch = int(end.astimezone(UTC).timestamp())
        if end_epoch <= start_epoch:
            raise ValueError("trade range end must be later than start")
        return self._trades_for_query(
            query_name="eventId",
            query_value=str(event_id),
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            taker_only=taker_only,
        )

    def market_trades(
        self,
        *,
        market_ids: Sequence[str],
        start: datetime,
        end: datetime,
        taker_only: bool = True,
    ) -> list[PublicTrade]:
        """Fetch canonical taker executions for one or more condition IDs.

        The public endpoint accepts comma-separated ``market`` condition IDs.
        Callers should keep batches modest; pagination and time bisection remain
        per batch and reset offset in each child time window.
        """
        unique_market_ids = tuple(
            dict.fromkeys(market_id.strip() for market_id in market_ids if market_id.strip())
        )
        if not unique_market_ids:
            raise ValueError("at least one market id is required")
        start_epoch = int(start.astimezone(UTC).timestamp())
        end_epoch = int(end.astimezone(UTC).timestamp())
        if end_epoch <= start_epoch:
            raise ValueError("trade range end must be later than start")
        return self._trades_for_query(
            query_name="market",
            query_value=",".join(unique_market_ids),
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            taker_only=taker_only,
        )

    def _trades_for_query(
        self,
        *,
        query_name: str,
        query_value: str,
        start_epoch: int,
        end_epoch: int,
        taker_only: bool,
    ) -> list[PublicTrade]:
        rows = self._range(
            query_name=query_name,
            query_value=query_value,
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            taker_only=taker_only,
        )
        unique: dict[tuple[Any, ...], PublicTrade] = {}
        for row in rows:
            trade = parse_public_trade(row)
            # The endpoint exposes no trade-row ID. For canonical taker-only rows,
            # this full identity is the strongest lossless boundary available and
            # removes exact repeats caused by offset/window pagination.
            key = (
                trade.transaction_hash,
                trade.proxy_wallet,
                trade.asset_id,
                trade.condition_id,
                trade.timestamp,
                trade.side,
                trade.size,
                trade.price,
            )
            unique.setdefault(key, trade)
        return sorted(
            unique.values(),
            key=lambda item: (item.timestamp, item.transaction_hash, item.asset_id),
        )

    def _range(
        self,
        *,
        query_name: str,
        query_value: str,
        start_epoch: int,
        end_epoch: int,
        taker_only: bool,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            payload = self._page(
                query_name=query_name,
                query_value=query_value,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                offset=offset,
                taker_only=taker_only,
            )
            rows.extend(payload)
            if len(payload) < self.page_limit:
                return rows
            next_offset = offset + self.page_limit
            if next_offset > self.maximum_offset:
                break
            offset = next_offset
        midpoint = (start_epoch + end_epoch) // 2
        if midpoint <= start_epoch:
            raise ValueError(
                f"more than {len(rows):,} trade rows in one second; cannot paginate safely"
            )
        left = self._range(
            query_name=query_name,
            query_value=query_value,
            start_epoch=start_epoch,
            end_epoch=midpoint,
            taker_only=taker_only,
        )
        right = self._range(
            query_name=query_name,
            query_value=query_value,
            start_epoch=midpoint + 1,
            end_epoch=end_epoch,
            taker_only=taker_only,
        )
        return left + right

    def _page(
        self,
        *,
        query_name: str,
        query_value: str,
        start_epoch: int,
        end_epoch: int,
        offset: int,
        taker_only: bool,
    ) -> list[dict[str, Any]]:
        response = None
        for attempt in range(5):
            response = self._client.get(
                "/trades",
                params={
                    query_name: query_value,
                    "start": start_epoch,
                    "end": end_epoch,
                    "limit": self.page_limit,
                    "offset": offset,
                    "takerOnly": str(taker_only).lower(),
                },
            )
            if response.status_code != 429 and response.status_code < 500:
                break
            time.sleep(min(8.0, 0.5 * (2**attempt)))
        assert response is not None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Data API /trades response is not a list")
        if self.request_pause_seconds:
            time.sleep(self.request_pause_seconds)
        return [row for row in payload if isinstance(row, dict)]
