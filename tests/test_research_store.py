from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from poly_weather.domain import CalibrationSample, MarketPricePoint
from poly_weather.research_store import ResearchWarehouse


def test_research_warehouse_upserts_and_exports_parquet(tmp_path) -> None:
    sample = CalibrationSample(
        station_id="KLGA",
        target_date=date(2026, 7, 1),
        lead_days=1,
        model="gfs_seamless",
        forecast_high_f=91.0,
        observed_high_f=93.0,
        forecast_source="Open-Meteo Previous Runs",
        truth_source="NOAA NCEI Daily Summaries",
        truth_kind="same_station_noaa_proxy_not_exact_wunderground",
        ingested_at=datetime.now(UTC),
    )
    parquet_path = tmp_path / "samples.parquet"
    with ResearchWarehouse(tmp_path / "research.duckdb") as warehouse:
        assert warehouse.upsert_samples([sample]) == 1
        assert warehouse.upsert_samples([sample]) == 1
        assert len(warehouse.samples(station_id="KLGA", lead_days=1, model="gfs_seamless")) == 1
        warehouse.export_samples_parquet(parquet_path)
        assert warehouse.status()["sample_count"] == 1

    assert parquet_path.exists()
    assert parquet_path.stat().st_size > 0


def test_market_price_lookup_never_uses_a_future_point(tmp_path) -> None:
    decision_time = datetime(2026, 8, 22, 12, tzinfo=UTC)
    points = [
        MarketPricePoint(
            token_id="yes-1",
            timestamp=decision_time - timedelta(minutes=10),
            price=Decimal("0.40"),
        ),
        MarketPricePoint(
            token_id="yes-1",
            timestamp=decision_time + timedelta(minutes=1),
            price=Decimal("0.90"),
        ),
    ]
    with ResearchWarehouse(tmp_path / "research.duckdb") as warehouse:
        assert warehouse.upsert_price_points(
            event_id="event-1",
            event_slug="weather-event",
            market_id="market-1",
            market_slug="bucket-1",
            outcome="Yes",
            source="Polymarket CLOB batch price history",
            points=points,
            ingested_at=datetime.now(UTC),
        ) == 2
        selected = warehouse.price_at_or_before(
            token_id="yes-1",
            decision_time=decision_time,
            max_age=timedelta(minutes=30),
        )

    assert selected is not None
    assert selected.timestamp <= decision_time
    assert selected.price == Decimal("0.4")
