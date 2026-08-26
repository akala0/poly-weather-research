from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from poly_weather.domain import MarketPricePoint, PaperAction, PaperDecision
from poly_weather.fees import LiquidityRole, configured_fee_rate, fee_per_share


class PaperPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_net_edge: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    market_category: str = "weather"
    liquidity_role: LiquidityRole = LiquidityRole.TAKER
    fee_rate_override: Decimal | None = Field(default=None, ge=0, le=1)
    assumed_slippage_bps: int = Field(default=50, ge=0, le=10_000)
    max_price_age: timedelta = timedelta(minutes=90)
    min_calibration_samples: int = Field(default=30, ge=0)
    max_notional_usd: Decimal = Field(default=Decimal("25"), gt=0)
    require_verified_contract: bool = True


def make_paper_decision(
    *,
    event_id: str,
    event_slug: str,
    market_id: str,
    market_slug: str,
    price_point: MarketPricePoint,
    no_price_point: MarketPricePoint | None = None,
    decision_time: datetime,
    model_probability: Decimal,
    signal_contract_verified: bool,
    calibration_sample_count: int,
    policy: PaperPolicy,
) -> PaperDecision:
    """Evaluate a binary paper trade without creating an executable order."""
    decision_utc = decision_time.astimezone(price_point.timestamp.tzinfo)
    reasons: list[str] = []
    no_lookahead = price_point.timestamp <= decision_utc
    if not no_lookahead:
        reasons.append("price point is later than decision time")
    elif decision_utc - price_point.timestamp > policy.max_price_age:
        reasons.append("price point is stale")
    if policy.require_verified_contract and not signal_contract_verified:
        reasons.append("contract station/date/rounding mapping is not verified")
    if calibration_sample_count < policy.min_calibration_samples:
        reasons.append("insufficient prior calibration samples")

    if no_price_point is None:
        reasons.append("NO token own price is unavailable; 1-YES substitution is forbidden")
    elif no_price_point.timestamp > decision_utc:
        reasons.append("NO price point is later than decision time")
    elif decision_utc - no_price_point.timestamp > policy.max_price_age:
        reasons.append("NO price point is stale")

    yes_edge = model_probability - price_point.price
    no_edge = (
        Decimal(1) - model_probability - no_price_point.price
        if no_price_point is not None
        else None
    )
    if no_edge is None or yes_edge >= no_edge:
        candidate_action = PaperAction.BUY_YES
        raw_edge = yes_edge
        selected_price_point = price_point
    else:
        candidate_action = PaperAction.BUY_NO
        raw_edge = no_edge
        selected_price_point = no_price_point
    fee_rate = configured_fee_rate(
        policy.market_category, override=policy.fee_rate_override
    )
    estimated_fee = fee_per_share(
        selected_price_point.price,
        liquidity_role=policy.liquidity_role,
        market_category=policy.market_category,
        fee_rate=fee_rate,
    )
    # Paper history has no depth. Keep this explicit proxy separate from fees;
    # executable paths replace it with an actual order-book walk.
    estimated_slippage = (
        selected_price_point.price
        * Decimal(policy.assumed_slippage_bps)
        / Decimal(10_000)
    )
    estimated_cost = estimated_fee + estimated_slippage
    net_edge = raw_edge - estimated_cost
    if net_edge < policy.min_net_edge:
        reasons.append("net edge is below policy minimum")

    action = PaperAction.SKIP if reasons else candidate_action
    return PaperDecision(
        event_id=event_id,
        event_slug=event_slug,
        market_id=market_id,
        market_slug=market_slug,
        token_id=selected_price_point.token_id,
        decision_time=decision_time,
        price_time=selected_price_point.timestamp,
        yes_price=price_point.price,
        no_price=no_price_point.price if no_price_point is not None else None,
        selected_token_price=selected_price_point.price,
        model_probability=model_probability,
        raw_edge=raw_edge,
        estimated_fee=estimated_fee,
        estimated_slippage=estimated_slippage,
        estimated_cost=estimated_cost,
        net_edge=net_edge,
        liquidity_role=policy.liquidity_role.value,
        market_category=policy.market_category,
        fee_rate=fee_rate,
        action=action,
        notional_usd=Decimal(0) if action is PaperAction.SKIP else policy.max_notional_usd,
        reasons=tuple(reasons) if reasons else ("all paper risk gates passed",),
        signal_contract_verified=signal_contract_verified,
        calibration_sample_count=calibration_sample_count,
        no_lookahead=no_lookahead,
    )
