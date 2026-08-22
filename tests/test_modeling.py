from datetime import date
from decimal import Decimal

from poly_weather.domain import Market
from poly_weather.modeling import build_bucket_forecast, market_temperature_bucket


def _market(market_id: str, suffix: str, price: str) -> Market:
    return Market.from_gamma(
        {
            "id": market_id,
            "question": suffix,
            "slug": f"event-{suffix}",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": f'["{price}", "{Decimal(1) - Decimal(price)}"]',
        }
    )


def test_market_bucket_parser_supports_tails_and_ranges() -> None:
    below = market_temperature_bucket(_market("a", "67forbelow", "0.1"))
    middle = market_temperature_bucket(_market("b", "68-69f", "0.2"))
    above = market_temperature_bucket(_market("c", "70forhigher", "0.7"))

    assert (below.lower_f, below.upper_f) == (None, 67)
    assert (middle.lower_f, middle.upper_f) == (68, 69)
    assert (above.lower_f, above.upper_f) == (70, None)


def test_bucket_forecast_is_smoothed_and_sums_to_one() -> None:
    markets = [
        _market("a", "67forbelow", "0.1"),
        _market("b", "68-69f", "0.2"),
        _market("c", "70forhigher", "0.7"),
    ]
    forecast = build_bucket_forecast(
        markets=markets,
        member_highs_f=[Decimal("66.6"), Decimal("68.2"), Decimal("71.8")],
        target_date=date(2026, 8, 22),
        ensemble_model="gfs_seamless",
        pseudocount=Decimal("0.5"),
    )

    assert forecast.sample_size == 3
    assert forecast.calibration_applied is False
    assert [item.member_count for item in forecast.probabilities] == [1, 1, 1]
    assert sum(item.probability for item in forecast.probabilities) == Decimal("1")
    assert forecast.tradeable is False
