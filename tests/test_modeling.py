from datetime import date
from decimal import Decimal

from poly_weather.domain import Market
from poly_weather.modeling import (
    blend_multi_model_forecasts,
    build_bucket_forecast,
    market_temperature_bucket,
    two_degree_bucket_lower,
)


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


def test_two_degree_bucket_alignment_uses_even_lower_bound() -> None:
    assert two_degree_bucket_lower(76.0) == 76
    assert two_degree_bucket_lower(77.0) == 76
    assert two_degree_bucket_lower(78.0) == 78
    assert two_degree_bucket_lower(77.5) == 78


def test_bucket_forecast_is_smoothed_and_sums_to_one() -> None:
    markets = [
        _market("a", "67forbelow", "0.1"),
        _market("b", "68-69f", "0.2"),
        _market("c", "70forhigher", "0.7"),
    ]
    forecast = build_bucket_forecast(
        markets=markets,
        deterministic_high_f=Decimal("69.0"),
        residual_std_f=1.0,
        target_date=date(2026, 8, 22),
        forecast_model="gfs_seamless",
    )

    assert forecast.forecast_high_f == Decimal("69.0")
    assert forecast.residual_std_f == 1.0
    assert forecast.calibration_applied is False
    assert sum(item.probability for item in forecast.probabilities) == Decimal("1")
    assert abs(float(forecast.probabilities[0].probability) - 0.0668072) < 1e-6
    assert abs(float(forecast.probabilities[1].probability) - 0.6246553) < 1e-6
    assert abs(float(forecast.probabilities[2].probability) - 0.3085375) < 1e-6
    assert forecast.tradeable is False


def test_multi_model_blend_uses_equal_defaults_and_normalizes_weights() -> None:
    forecasts = {"gfs": 80.0, "icon": 82.0, "gem": 84.0}

    assert blend_multi_model_forecasts(forecasts) == 82.0
    assert blend_multi_model_forecasts(
        forecasts,
        {"gfs": 2.0, "icon": 1.0, "gem": 1.0},
    ) == 81.5
