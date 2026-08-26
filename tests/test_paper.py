from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.domain import MarketPricePoint, PaperAction
from poly_weather.paper import PaperPolicy, make_paper_decision


def test_paper_decision_chooses_yes_after_costs() -> None:
    decision_time = datetime(2026, 8, 22, 15, tzinfo=UTC)
    result = make_paper_decision(
        event_id="event-1",
        event_slug="weather-event",
        market_id="market-1",
        market_slug="bucket-80-81",
        price_point=MarketPricePoint(
            token_id="yes-1",
            timestamp=decision_time - timedelta(minutes=5),
            price=Decimal("0.40"),
        ),
        no_price_point=MarketPricePoint(
            token_id="no-1",
            timestamp=decision_time - timedelta(minutes=5),
            price=Decimal("0.61"),
        ),
        decision_time=decision_time,
        model_probability=Decimal("0.50"),
        signal_contract_verified=True,
        calibration_sample_count=31,
        policy=PaperPolicy(assumed_slippage_bps=50),
    )

    assert result.action is PaperAction.BUY_YES
    assert result.raw_edge == Decimal("0.10")
    assert result.estimated_fee == Decimal("0.0120")
    assert result.estimated_slippage == Decimal("0.002")
    assert result.net_edge == Decimal("0.0860")
    assert result.no_lookahead is True


def test_paper_decision_fails_closed_on_future_price_and_unverified_contract() -> None:
    decision_time = datetime(2026, 8, 22, 15, tzinfo=UTC)
    result = make_paper_decision(
        event_id="event-1",
        event_slug="weather-event",
        market_id="market-1",
        market_slug="bucket-80-81",
        price_point=MarketPricePoint(
            token_id="yes-1",
            timestamp=decision_time + timedelta(seconds=1),
            price=Decimal("0.40"),
        ),
        no_price_point=MarketPricePoint(
            token_id="no-1",
            timestamp=decision_time + timedelta(seconds=1),
            price=Decimal("0.60"),
        ),
        decision_time=decision_time,
        model_probability=Decimal("0.80"),
        signal_contract_verified=False,
        calibration_sample_count=0,
        policy=PaperPolicy(),
    )

    assert result.action is PaperAction.SKIP
    assert result.no_lookahead is False
    assert "price point is later than decision time" in result.reasons


def test_paper_no_side_uses_own_token_price_and_fails_closed_when_missing() -> None:
    decision_time = datetime(2026, 8, 22, 15, tzinfo=UTC)
    common = {
        "event_id": "event-1",
        "event_slug": "weather-event",
        "market_id": "market-1",
        "market_slug": "bucket-80-81",
        "price_point": MarketPricePoint(
            token_id="yes-1",
            timestamp=decision_time,
            price=Decimal("0.25"),
        ),
        "decision_time": decision_time,
        "model_probability": Decimal("0.10"),
        "signal_contract_verified": True,
        "calibration_sample_count": 31,
        "policy": PaperPolicy(assumed_slippage_bps=0),
    }
    missing = make_paper_decision(**common)
    assert missing.action is PaperAction.SKIP
    assert "1-YES substitution is forbidden" in " ".join(missing.reasons)

    result = make_paper_decision(
        **common,
        no_price_point=MarketPricePoint(
            token_id="no-1",
            timestamp=decision_time,
            price=Decimal("0.82"),
        ),
    )
    assert result.action is PaperAction.BUY_NO
    assert result.token_id == "no-1"
    assert result.selected_token_price == Decimal("0.82")
    assert result.estimated_fee == Decimal("0.007380")
