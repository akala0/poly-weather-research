import re
from datetime import UTC, datetime

from poly_weather.adapters.polymarket import EventSnapshot
from poly_weather.domain import Market, SettlementSpec, VerificationStatus, WindowBasis
from poly_weather.settlement import parse_settlement_evidence, verify_settlement_evidence


def _event() -> EventSnapshot:
    markets = []
    for market_id, suffix, question in (
        ("1", "67forbelow", "Will the highest temperature in New York City be 67°F or below?"),
        ("2", "68-69f", "Will the highest temperature in New York City be between 68-69°F?"),
        ("3", "70forhigher", "Will the highest temperature in New York City be 70°F or higher?"),
    ):
        markets.append(
            Market.from_gamma(
                {
                    "id": market_id,
                    "question": question,
                    "slug": f"highest-temperature-in-nyc-on-august-22-2026-{suffix}",
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.5", "0.5"]',
                }
            )
        )
    description = (
        "This market will resolve based on the highest temperature recorded in the 'Daily Observations' table. "
        "This market will resolve to the temperature range that contains the highest temperature recorded at "
        "the LaGuardia Airport Station in degrees Fahrenheit on 22 Aug '26. "
        "The resolution source for this market will be information from Wunderground. "
        "This market can not resolve until the first data point for the following date has been published. "
        "The resolution source for this market measures temperatures to whole degrees Fahrenheit. "
        "Revisions will be considered until the first datapoint for the following date has been published, "
        "after which any alterations will not be considered."
    )
    source = "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"
    return EventSnapshot(
        request_url="https://example.test/event",
        fetched_at=datetime.now(UTC),
        event_id="event-1",
        event_slug="highest-temperature-in-nyc-on-august-22-2026",
        title="Highest temperature in NYC on August 22?",
        resolution_source=source,
        raw_payload={"description": description, "resolutionSource": source},
        markets=tuple(markets),
    )


def _spec(status: VerificationStatus) -> SettlementSpec:
    event_slug = "highest-temperature-in-nyc-on-august-22-2026"
    return SettlementSpec(
        key="nyc-2026-08-22",
        market_slug_pattern=re.escape(event_slug),
        station_id="KLGA",
        station_name="LaGuardia Airport",
        latitude=40.779,
        longitude=-73.88,
        timezone="America/New_York",
        window_basis=WindowBasis.LOCAL_CIVIL_DAY,
        resolution_source_url="https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
        status=status,
    )


def test_parse_current_polymarket_rule_wording() -> None:
    event = _event()
    evidence = parse_settlement_evidence(event, registry_spec=_spec(VerificationStatus.UNVERIFIED))

    assert evidence.parse_status.value == "complete"
    assert evidence.city == "NYC"
    assert evidence.target_date.isoformat() == "2026-08-22"
    assert evidence.station_id == "KLGA"
    assert evidence.station_name == "LaGuardia Airport"
    assert evidence.observation_table == "Daily Observations"
    assert evidence.ignores_late_revisions is True
    assert len(evidence.buckets) == 3


def test_registry_status_is_required_for_verification() -> None:
    event = _event()
    unverified = _spec(VerificationStatus.UNVERIFIED)
    evidence = parse_settlement_evidence(event, registry_spec=unverified)

    rejected = verify_settlement_evidence(evidence, unverified)
    accepted = verify_settlement_evidence(evidence, _spec(VerificationStatus.VERIFIED))

    assert rejected.tradeable is False
    assert "registry_verified" in rejected.failures
    assert accepted.tradeable is True


def test_settlement_evidence_hash_is_stable_across_market_order() -> None:
    event = _event()
    spec = _spec(VerificationStatus.VERIFIED)
    forward = parse_settlement_evidence(event, registry_spec=spec)
    reversed_order = parse_settlement_evidence(
        event.model_copy(update={"markets": tuple(reversed(event.markets))}),
        registry_spec=spec,
    )

    assert forward.evidence_sha256 == reversed_order.evidence_sha256
