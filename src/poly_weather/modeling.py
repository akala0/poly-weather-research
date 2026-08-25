from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from statistics import NormalDist

from poly_weather.domain import (
    BucketForecast,
    BucketProbability,
    Market,
    TemperatureBucket,
)

_BELOW = re.compile(r"-(?P<upper>\d+)forbelow$")
_RANGE = re.compile(r"-(?P<lower>\d+)-(?P<upper>\d+)f$")
_HIGHER = re.compile(r"-(?P<lower>\d+)forhigher$")
_CELSIUS_EXACT = re.compile(r"-(?P<value>-?\d+)c$")
_CELSIUS_BELOW = re.compile(r"-(?P<upper>-?\d+)c(?:or)?below$")
_CELSIUS_HIGHER = re.compile(r"-(?P<lower>-?\d+)c(?:or)?higher$")
DEFAULT_MULTI_MODEL_WEIGHTS = {
    "gfs": 1.0 / 3.0,
    "icon": 1.0 / 3.0,
    "gem": 1.0 / 3.0,
}


def two_degree_bucket_lower(value_f: float | Decimal) -> int:
    """Return the even lower bound of the aligned two-degree Fahrenheit bucket."""
    numeric_value = float(value_f)
    if not math.isfinite(numeric_value):
        raise ValueError("bucket value must be finite")
    rounded_f = int(
        Decimal(str(value_f)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    return rounded_f if rounded_f % 2 == 0 else rounded_f - 1


def blend_multi_model_forecasts(
    forecasts: Mapping[str, float | Decimal],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Return a normalized weighted mean across deterministic model forecasts."""
    if not forecasts:
        raise ValueError("at least one model forecast is required")
    selected_weights = weights or {
        model: DEFAULT_MULTI_MODEL_WEIGHTS.get(model, 1.0) for model in forecasts
    }
    if set(selected_weights) != set(forecasts):
        raise ValueError("forecast and weight model keys must match")
    numeric_forecasts = {model: float(value) for model, value in forecasts.items()}
    numeric_weights = {model: float(value) for model, value in selected_weights.items()}
    if any(not math.isfinite(value) for value in numeric_forecasts.values()):
        raise ValueError("model forecasts must be finite")
    if any(not math.isfinite(value) or value < 0 for value in numeric_weights.values()):
        raise ValueError("model weights must be finite and non-negative")
    total_weight = sum(numeric_weights.values())
    if total_weight <= 0:
        raise ValueError("model weights must have a positive sum")
    return sum(
        numeric_forecasts[model] * numeric_weights[model]
        for model in numeric_forecasts
    ) / total_weight


def normal_bucket_probability(
    *,
    mean_f: float,
    std_f: float,
    lower_f: int | None,
    upper_f: int | None,
) -> float:
    """Normal probability mass inside whole-degree bucket boundaries."""
    if not math.isfinite(mean_f):
        raise ValueError("forecast mean must be finite")
    if not math.isfinite(std_f) or std_f <= 0:
        raise ValueError("forecast standard deviation must be finite and positive")
    distribution = NormalDist(mu=mean_f, sigma=std_f)
    lower_probability = (
        0.0 if lower_f is None else distribution.cdf(float(lower_f) - 0.5)
    )
    upper_probability = (
        1.0 if upper_f is None else distribution.cdf(float(upper_f) + 0.5)
    )
    return max(0.0, upper_probability - lower_probability)


def market_temperature_bucket(
    market: Market,
    *,
    expected_unit: str | None = None,
) -> TemperatureBucket:
    slug = market.slug.lower()
    lower: int | None
    upper: int | None
    unit = "fahrenheit"
    if match := _CELSIUS_BELOW.search(slug):
        lower, upper = None, int(match.group("upper"))
        unit = "celsius"
    elif match := _CELSIUS_EXACT.search(slug):
        lower = upper = int(match.group("value"))
        unit = "celsius"
    elif match := _CELSIUS_HIGHER.search(slug):
        lower, upper = int(match.group("lower")), None
        unit = "celsius"
    elif match := _BELOW.search(slug):
        lower, upper = None, int(match.group("upper"))
    elif match := _RANGE.search(slug):
        lower, upper = int(match.group("lower")), int(match.group("upper"))
    elif match := _HIGHER.search(slug):
        lower, upper = int(match.group("lower")), None
    else:
        raise ValueError(f"unsupported temperature bucket slug: {market.slug}")
    if expected_unit is not None and unit != expected_unit:
        raise ValueError(
            f"temperature bucket unit mismatch: expected {expected_unit}, got {unit}"
        )

    yes_probability = None
    for outcome, price in zip(market.outcomes, market.outcome_prices, strict=False):
        if outcome.casefold() == "yes":
            yes_probability = price
            break
    return TemperatureBucket(
        market_id=market.market_id,
        market_slug=market.slug,
        label=market.question,
        lower_f=lower,
        upper_f=upper,
        unit=unit,
        market_probability=yes_probability,
    )


def _sort_key(bucket: TemperatureBucket) -> int:
    return -10_000 if bucket.lower_f is None else bucket.lower_f


def validate_bucket_partition(
    buckets: Sequence[TemperatureBucket],
    *,
    expected_width_degrees: int | None = None,
) -> tuple[TemperatureBucket, ...]:
    ordered = tuple(sorted(buckets, key=_sort_key))
    if len(ordered) < 2:
        raise ValueError("a bucket forecast requires at least two markets")
    if ordered[0].lower_f is not None or ordered[-1].upper_f is not None:
        raise ValueError("temperature buckets must include open lower and upper tails")
    units = {bucket.unit for bucket in ordered}
    if len(units) != 1:
        raise ValueError("temperature buckets must use one unit")
    finite_widths = {
        width for bucket in ordered if (width := bucket.width_degrees) is not None
    }
    if len(finite_widths) != 1:
        raise ValueError("finite temperature buckets must have one consistent width")
    if expected_width_degrees is not None and finite_widths != {expected_width_degrees}:
        raise ValueError(
            f"temperature bucket width mismatch: expected {expected_width_degrees}, "
            f"got {sorted(finite_widths)}"
        )
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.upper_f is None or right.lower_f is None or left.upper_f + 1 != right.lower_f:
            raise ValueError("temperature buckets must be contiguous and non-overlapping")
    return ordered


def build_bucket_forecast(
    *,
    markets: Iterable[Market],
    deterministic_high_f: Decimal | float,
    residual_std_f: float,
    target_date: date,
    forecast_model: str,
    calibration_applied: bool = False,
    calibration_sample_count: int = 0,
    calibration_bias_f: float | None = None,
    calibration_basis: str | None = None,
    tradeable: bool = False,
    tradeable_reason: str = "settlement mapping has not been verified",
) -> BucketForecast:
    buckets = validate_bucket_partition(
        tuple(market_temperature_bucket(market) for market in markets)
    )
    unit = buckets[0].unit
    mean_f = float(deterministic_high_f)
    if not math.isfinite(mean_f):
        raise ValueError("deterministic forecast must be finite")
    if not math.isfinite(residual_std_f) or residual_std_f <= 0:
        raise ValueError("residual standard deviation must be finite and positive")
    distribution_mean = mean_f
    distribution_std = residual_std_f
    if unit == "celsius":
        distribution_mean = (mean_f - 32.0) * 5.0 / 9.0
        distribution_std = residual_std_f * 5.0 / 9.0
    normalized = [
        Decimal(
            str(
                normal_bucket_probability(
                    mean_f=distribution_mean,
                    std_f=distribution_std,
                    lower_f=bucket.lower_f,
                    upper_f=bucket.upper_f,
                )
            )
        )
        for bucket in buckets
    ]
    normalized[-1] = Decimal("1") - sum(normalized[:-1], start=Decimal("0"))
    probabilities = []
    for bucket, probability in zip(buckets, normalized, strict=True):
        edge = None
        if bucket.market_probability is not None:
            edge = probability - bucket.market_probability
        probabilities.append(
            BucketProbability(
                bucket=bucket,
                probability=probability,
                edge_vs_market=edge,
            )
        )
    return BucketForecast(
        generated_at=datetime.now(UTC),
        target_date=target_date,
        forecast_model=forecast_model,
        forecast_high_f=Decimal(str(deterministic_high_f)),
        residual_std_f=residual_std_f,
        rounding=(
            f"whole-degree {unit} buckets with half-degree Normal CDF boundaries; "
            "forecast conversion occurs before final rounding"
        ),
        calibration_applied=calibration_applied,
        calibration_sample_count=calibration_sample_count,
        calibration_bias_f=calibration_bias_f,
        calibration_basis=calibration_basis,
        tradeable=tradeable,
        tradeable_reason=tradeable_reason,
        probabilities=tuple(probabilities),
    )
