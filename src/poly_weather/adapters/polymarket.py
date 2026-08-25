from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from poly_weather.domain import Market

WEATHER_TERMS = (
    "temperature",
    "weather",
    "degrees fahrenheit",
    "degrees celsius",
    "daily high",
)


class MarketPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_url: str
    fetched_at: datetime
    raw_items: tuple[dict[str, Any], ...]
    markets: tuple[Market, ...]


class SearchPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_url: str
    fetched_at: datetime
    query: str
    page_number: int
    has_more: bool
    raw_payload: dict[str, Any]
    markets: tuple[Market, ...]
    events: tuple[EventSnapshot, ...] = ()


class EventSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_url: str
    fetched_at: datetime
    event_id: str
    event_slug: str
    title: str
    resolution_source: str | None
    raw_payload: dict[str, Any]
    markets: tuple[Market, ...]


def is_weather_market(market: Market) -> bool:
    searchable = " ".join(
        value for value in (market.question, market.description, market.category) if value
    ).lower()
    return any(term in searchable for term in WEATHER_TERMS)


class GammaClient:
    """Unauthenticated, read-only client for Polymarket's Gamma API."""

    def __init__(
        self,
        *,
        base_url: str = "https://gamma-api.polymarket.com",
        timeout: float = 20.0,
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

    def __enter__(self) -> GammaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fetch_markets_page(self, *, limit: int = 100, offset: int = 0) -> MarketPage:
        response = self._client.get(
            "/markets",
            params={"closed": "false", "limit": limit, "offset": offset},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Gamma /markets returned a non-list response")
        raw_items = tuple(item for item in payload if isinstance(item, dict))
        markets = tuple(Market.from_gamma(item) for item in raw_items)
        return MarketPage(
            request_url=str(response.request.url),
            fetched_at=datetime.now(UTC),
            raw_items=raw_items,
            markets=markets,
        )

    def iter_pages(self, *, pages: int, page_size: int) -> Iterable[MarketPage]:
        for page_number in range(pages):
            page = self.fetch_markets_page(limit=page_size, offset=page_number * page_size)
            yield page
            if len(page.raw_items) < page_size:
                break

    def search_markets_page(
        self,
        *,
        query: str,
        page_number: int = 1,
        limit: int = 50,
    ) -> SearchPage:
        response = self._client.get(
            "/public-search",
            params={
                "q": query,
                "events_status": "active",
                "limit_per_type": limit,
                "page": page_number,
                "keep_closed_markets": 0,
                "search_tags": "false",
                "search_profiles": "false",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Gamma /public-search returned a non-object response")

        raw_markets: list[dict[str, Any]] = []
        event_snapshots: list[EventSnapshot] = []
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise ValueError("Gamma search events field is not a list")
        for event in events:
            if not isinstance(event, dict):
                continue
            event_markets: list[dict[str, Any]] = []
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                enriched = dict(market)
                enriched.setdefault("category", event.get("category"))
                enriched.setdefault("description", event.get("description"))
                enriched.setdefault("resolutionSource", event.get("resolutionSource"))
                raw_markets.append(enriched)
                event_markets.append(enriched)
            if event.get("id") and event.get("slug"):
                event_snapshots.append(
                    EventSnapshot(
                        request_url=str(response.request.url),
                        fetched_at=datetime.now(UTC),
                        event_id=str(event["id"]),
                        event_slug=str(event["slug"]),
                        title=str(event.get("title") or ""),
                        resolution_source=event.get("resolutionSource"),
                        raw_payload=event,
                        markets=tuple(Market.from_gamma(item) for item in event_markets),
                    )
                )

        pagination = payload.get("pagination") or {}
        return SearchPage(
            request_url=str(response.request.url),
            fetched_at=datetime.now(UTC),
            query=query,
            page_number=page_number,
            has_more=bool(pagination.get("hasMore", False)),
            raw_payload=payload,
            markets=tuple(Market.from_gamma(item) for item in raw_markets),
            events=tuple(event_snapshots),
        )

    def iter_search_pages(
        self,
        *,
        query: str,
        pages: int,
        page_size: int,
    ) -> Iterable[SearchPage]:
        for page_number in range(1, pages + 1):
            page = self.search_markets_page(
                query=query,
                page_number=page_number,
                limit=page_size,
            )
            yield page
            if not page.has_more:
                break

    def event_by_slug(self, event_slug: str) -> EventSnapshot:
        response = self._client.get(f"/events/slug/{event_slug}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Gamma event response is not an object")
        raw_markets: list[dict[str, Any]] = []
        for market in payload.get("markets") or []:
            if not isinstance(market, dict):
                continue
            enriched = dict(market)
            enriched.setdefault("category", payload.get("category"))
            enriched.setdefault("description", payload.get("description"))
            enriched.setdefault("resolutionSource", payload.get("resolutionSource"))
            raw_markets.append(enriched)
        return EventSnapshot(
            request_url=str(response.request.url),
            fetched_at=datetime.now(UTC),
            event_id=str(payload["id"]),
            event_slug=str(payload.get("slug") or event_slug),
            title=str(payload.get("title") or ""),
            resolution_source=payload.get("resolutionSource"),
            raw_payload=payload,
            markets=tuple(Market.from_gamma(item) for item in raw_markets),
        )
