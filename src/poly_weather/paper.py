from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from poly_weather.domain import MarketPricePoint, PaperAction, PaperDecision


class PaperPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_net_edge: Decimal = Field(default=Decimal("0.03"), ge=0, le=1)
    fee_bps: int = Field(default=0, ge=0, le=10_000)
    slippage_bps: int = Field(default=50, ge=0, le=10_000)
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

    yes_edge = model_probability - price_point.price
    no_edge = price_point.price - model_probability
    if yes_edge >= no_edge:
        candidate_action = PaperAction.BUY_YES
        raw_edge = yes_edge
    else:
        candidate_action = PaperAction.BUY_NO
        raw_edge = no_edge
    estimated_cost = Decimal(policy.fee_bps + policy.slippage_bps) / Decimal(10_000)
    net_edge = raw_edge - estimated_cost
    if net_edge < policy.min_net_edge:
        reasons.append("net edge is below policy minimum")

    action = PaperAction.SKIP if reasons else candidate_action
    return PaperDecision(
        event_id=event_id,
        event_slug=event_slug,
        market_id=market_id,
        market_slug=market_slug,
        token_id=price_point.token_id,
        decision_time=decision_time,
        price_time=price_point.timestamp,
        yes_price=price_point.price,
        model_probability=model_probability,
        raw_edge=raw_edge,
        estimated_cost=estimated_cost,
        net_edge=net_edge,
        action=action,
        notional_usd=Decimal(0) if action is PaperAction.SKIP else policy.max_notional_usd,
        reasons=tuple(reasons) if reasons else ("all paper risk gates passed",),
        signal_contract_verified=signal_contract_verified,
        calibration_sample_count=calibration_sample_count,
        no_lookahead=no_lookahead,
    )
