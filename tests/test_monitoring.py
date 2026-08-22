from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.domain import MarketQuote, MetarReport, MonitorStatus, NwsObservation, TafReport
from poly_weather.monitoring import MonitorThresholds, build_monitor_snapshot


def test_monitor_snapshot_is_healthy_with_fresh_agreeing_sources() -> None:
    captured = datetime(2026, 8, 22, 12, tzinfo=UTC)
    snapshot = build_monitor_snapshot(
        captured_at=captured,
        station_id="KLGA",
        event_id="event-1",
        event_slug="weather-event",
        signal_contract_verified=True,
        nws=NwsObservation(
            station_id="KLGA",
            timestamp=captured - timedelta(minutes=10),
            temperature_c=Decimal("20"),
            raw={},
        ),
        metars=(
            MetarReport(
                station_id="KLGA",
                observed_at=captured - timedelta(minutes=20),
                temperature_c=Decimal("20"),
                dewpoint_c=Decimal("15"),
                raw_text="METAR KLGA",
                flight_category="VFR",
                raw={},
            ),
        ),
        tafs=(
            TafReport(
                station_id="KLGA",
                issued_at=captured - timedelta(hours=1),
                valid_from=captured - timedelta(hours=1),
                valid_to=captured + timedelta(hours=20),
                raw_text="TAF KLGA",
                periods=(),
                raw={},
            ),
        ),
        quotes=(
            MarketQuote(
                token_id="yes-1",
                fetched_at=captured,
                best_bid=Decimal("0.40"),
                best_ask=Decimal("0.44"),
                midpoint=Decimal("0.42"),
                spread=Decimal("0.04"),
            ),
        ),
        token_market_slugs={"yes-1": "bucket-1"},
        thresholds=MonitorThresholds(),
        collection_errors={},
        raw_paths={},
    )

    assert snapshot.status is MonitorStatus.HEALTHY
    assert snapshot.nws_age_minutes == 10
    assert snapshot.metar_age_minutes == 20
    assert snapshot.source_delta_f == 0


def test_monitor_snapshot_is_stale_when_primary_noaa_collection_fails() -> None:
    snapshot = build_monitor_snapshot(
        captured_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        station_id="KLGA",
        event_id="event-1",
        event_slug="weather-event",
        signal_contract_verified=True,
        nws=None,
        metars=(),
        tafs=(),
        quotes=(),
        token_market_slugs={},
        thresholds=MonitorThresholds(),
        collection_errors={"nws": "timeout", "clob": "timeout"},
        raw_paths={},
    )

    assert snapshot.status is MonitorStatus.STALE
    assert "NWS collection failed" in snapshot.reasons
    assert "CLOB collection failed" in snapshot.reasons
