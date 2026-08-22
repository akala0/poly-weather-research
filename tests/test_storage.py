import json
from datetime import UTC, datetime

from poly_weather.domain import Market
from poly_weather.storage import CatalogStore, RawEventArchive


def test_archive_and_catalog_preserve_source_data(tmp_path) -> None:
    fetched_at = datetime(2026, 8, 22, 12, tzinfo=UTC)
    payload = {
        "id": "weather-1",
        "question": "Weather question",
        "slug": "weather-question",
        "active": True,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.25", "0.75"]',
    }
    raw_path = RawEventArchive(tmp_path / "raw").append(
        source="polymarket_gamma_markets",
        fetched_at=fetched_at,
        request_url="https://example.test/markets",
        payload=[payload],
    )

    archived = json.loads(raw_path.read_text(encoding="utf-8"))
    assert archived["payload"] == [payload]

    with CatalogStore(tmp_path / "catalog.sqlite3") as catalog:
        assert catalog.upsert_markets([Market.from_gamma(payload)], observed_at=fetched_at) == 1
        catalog.record_page(
            source="polymarket_gamma_markets",
            fetched_at=fetched_at,
            request_url="https://example.test/markets",
            item_count=1,
            candidate_count=1,
            raw_path=raw_path,
        )
        assert catalog.market_count() == 1

