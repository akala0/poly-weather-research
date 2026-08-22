from decimal import Decimal

import pytest
from pydantic import ValidationError

from poly_weather.domain import Market, SettlementSpec, VerificationStatus


def test_gamma_market_decodes_json_encoded_arrays() -> None:
    market = Market.from_gamma(
        {
            "id": "42",
            "question": "Will the temperature exceed 90°F?",
            "slug": "temperature-over-90",
            "active": True,
            "closed": False,
            "endDate": "2026-08-23T00:00:00Z",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.42", "0.58"]',
            "clobTokenIds": '["yes-token", "no-token"]',
        }
    )

    assert market.market_id == "42"
    assert market.outcomes == ("Yes", "No")
    assert market.outcome_prices == (Decimal("0.42"), Decimal("0.58"))
    assert market.clob_token_ids == ("yes-token", "no-token")


def test_unverified_settlement_is_fail_closed() -> None:
    spec = SettlementSpec(
        key="seed",
        market_slug_pattern="weather",
        status=VerificationStatus.UNVERIFIED,
    )

    with pytest.raises(ValueError, match="not verified"):
        spec.assert_tradeable()


def test_verified_settlement_requires_complete_source_contract() -> None:
    with pytest.raises(ValidationError, match="incomplete"):
        SettlementSpec(
            key="incomplete",
            market_slug_pattern="weather",
            status=VerificationStatus.VERIFIED,
        )

