from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from poly_weather.domain import (
    BucketForecast,
    BucketProbability,
    Market,
    TemperatureBucket,
)

_BELOW = re.compile(r"-(?P<upper>\d+)forbelow$")
_RANGE = re.compile(r"-(?P<lower>\d+)-(?P<upper>\d+)f$")
_HIGHER = re.compile(r"-(?P<lower>\d+)forhigher$")


def market_temperature_bucket(market: Market) -> TemperatureBucket:
    slug = market.slug.lower()
    lower: int | None
    upper: int | None
    if match := _BELOW.search(slug):
        lower, upper = None, int(match.group("upper"))
    elif match := _RANGE.search(slug):
        lower, upper = int(match.group("lower")), int(match.group("upper"))
    elif match := _HIGHER.search(slug):
        lower, upper = int(match.group("lower")), None
    else:
        raise ValueError(f"unsupported temperature bucket slug: {market.slug}")

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
        market_probability=yes_probability,
    )


def _sort_key(bucket: TemperatureBucket) -> int:
    return -10_000 if bucket.lower_f is None else bucket.lower_f


def validate_bucket_partition(buckets: Sequence[TemperatureBucket]) -> tuple[TemperatureBucket, ...]:
    ordered = tuple(sorted(buckets, key=_sort_key))
    if len(ordered) < 2:
        raise ValueError("a bucket forecast requires at least two markets")
    if ordered[0].lower_f is not None or ordered[-1].upper_f is not None:
        raise ValueError("temperature buckets must include open lower and upper tails")
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.upper_f is None or right.lower_f is None or left.upper_f + 1 != right.lower_f:
            raise ValueError("temperature buckets must be contiguous and non-overlapping")
    return ordered


def build_bucket_forecast(
    *,
    markets: Iterable[Market],
    member_highs_f: Sequence[Decimal],
    target_date: date,
    ensemble_model: str,
    pseudocount: Decimal = Decimal("0.5"),
    calibration_applied: bool = False,
    calibration_sample_count: int = 0,
    calibration_bias_f: float | None = None,
    calibration_basis: str | None = None,
    tradeable: bool = False,
    tradeable_reason: str = "settlement mapping has not been verified",
) -> BucketForecast:
    if pseudocount < 0:
        raise ValueError("pseudocount cannot be negative")
    if not member_highs_f:
        raise ValueError("ensemble contains no daily highs")
    buckets = validate_bucket_partition(tuple(market_temperature_bucket(market) for market in markets))
    rounded = [int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) for value in member_highs_f]
    counts = [sum(bucket.contains(value) for value in rounded) for bucket in buckets]
    if sum(counts) != len(rounded):
        raise ValueError("bucket partition did not classify every ensemble member")
    denominator = Decimal(len(rounded)) + pseudocount * Decimal(len(buckets))
    normalized = [
        (Decimal(count) + pseudocount) / denominator
        for count in counts
    ]
    normalized[-1] = Decimal("1") - sum(normalized[:-1], start=Decimal("0"))
    probabilities = []
    for bucket, count, probability in zip(buckets, counts, normalized, strict=True):
        edge = None
        if bucket.market_probability is not None:
            edge = probability - bucket.market_probability
        probabilities.append(
            BucketProbability(
                bucket=bucket,
                member_count=count,
                probability=probability,
                edge_vs_market=edge,
            )
        )
    return BucketForecast(
        generated_at=datetime.now(UTC),
        target_date=target_date,
        ensemble_model=ensemble_model,
        sample_size=len(rounded),
        rounding="ROUND_HALF_UP to whole degrees Fahrenheit",
        pseudocount=pseudocount,
        calibration_applied=calibration_applied,
        calibration_sample_count=calibration_sample_count,
        calibration_bias_f=calibration_bias_f,
        calibration_basis=calibration_basis,
        tradeable=tradeable,
        tradeable_reason=tradeable_reason,
        probabilities=tuple(probabilities),
    )
