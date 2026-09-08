from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.paper_account import PaperLedger
from poly_weather.paper_spread_runtime import PaperSpreadProcessor, PaperStrategyConfig
from poly_weather.shadow_orders import BookSnapshot, ShadowOrderState, ShadowSide, TradeEvent

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)


def config() -> PaperStrategyConfig:
    return PaperStrategyConfig.load("configs/paper_spread_strategy_v1.json")


def snapshot(*, at: datetime = BASE, bid: str = "0.75", ask: str = "0.80", metadata: dict | None = None) -> BookSnapshot:
    source_at = at - timedelta(minutes=1)
    received_at = at - timedelta(seconds=30)
    return BookSnapshot(
        timestamp=at,
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=((bid, "100"),),
        asks=((ask, "100"),),
        metadata={
            "weather_join_status": "aligned",
            "weather_observation_id": f"weather-{source_at.isoformat()}",
            "weather_source_timestamp": source_at.isoformat(),
            "weather_received_at": received_at.isoformat(),
            "weather_observation_new": True,
            "weather_market_lag": True,
            "weather_improving": True,
            "weather_worsening": False,
            "weather_unchanged": False,
            **(metadata or {}),
        },
    )


def processor(tmp_path) -> PaperSpreadProcessor:
    paper = PaperSpreadProcessor(ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=config())
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    return paper


def fill_first(processor: PaperSpreadProcessor) -> None:
    order = processor.process_snapshot(snapshot())
    assert order is not None
    processor.process_trade(TradeEvent(BASE + timedelta(minutes=1), "token", ShadowSide.SELL, "0.75", "200", "entry"))


def test_initial_tranche_requires_lag_improving_and_no_ask_band(tmp_path) -> None:
    paper = processor(tmp_path)
    assert paper.process_snapshot(snapshot(metadata={"weather_market_lag": False})) is None
    assert paper.process_snapshot(snapshot(metadata={"weather_improving": False})) is None
    assert paper.process_snapshot(snapshot(ask="0.90")) is None
    assert paper.process_snapshot(snapshot()) is not None


def test_rejected_confirmation_is_not_consumed(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    rejected = snapshot(
        at=BASE + timedelta(minutes=2),
        metadata={
            "weather_improving": False,
        },
    )
    assert paper.process_snapshot(rejected) is None
    state = next(iter(paper.states.values()))
    assert rejected.metadata["weather_observation_id"] not in state.consumed_observations


def test_fourth_tranche_requires_actual_third_fill(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    second = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(minutes=2),
            metadata={
                "weather_observation_id": "weather-confirmation-1",
            },
        ),
    )
    assert second is not None
    paper.process_trade(
        TradeEvent(BASE + timedelta(minutes=3), "token", ShadowSide.SELL, "0.75", "200", "second")
    )
    third = paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=4), bid="0.73", ask="0.78"),
    )
    assert third is not None
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=20))
    fourth = paper.process_snapshot(
        snapshot(
            at=BASE + timedelta(minutes=21),
            metadata={
                "weather_observation_id": "weather-confirmation-2",
            },
        ),
    )
    assert fourth is None


def test_max_hold_closes_with_contemporaneous_native_bid(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    paper.process_snapshot(snapshot(at=BASE + timedelta(hours=2, minutes=1)))
    paper.sweep_lifecycle(as_of=BASE + timedelta(hours=2, minutes=1, seconds=1))
    state = next(iter(paper.states.values()))
    assert state.closed is True
    assert state.close_reason == "max_hold"
    assert next(iter(paper.account.positions.values())).shares == Decimal("0")


def test_closed_event_with_contemporaneous_native_bid_risk_exits(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=2), closed_event_ids={"event"})
    state = next(iter(paper.states.values()))
    assert state.closed is True
    assert paper.account.stranded_inventory_cost_usd == Decimal("0")
    assert next(iter(paper.account.positions.values())).shares == Decimal("0")


def test_closed_event_without_native_bid_strands_and_leaves_pnl_unpriced(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    key = next(iter(paper.engines))
    # The closing fact is current, but the only locally available native book
    # has no executable bid.  Do not substitute a last trade or cached quote.
    paper.latest_snapshots[key] = BookSnapshot(
        timestamp=BASE + timedelta(minutes=2),
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=(),
        asks=(("0.80", "100"),),
        book_complete=False,
    )

    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=2), closed_event_ids={"event"})

    state = next(iter(paper.states.values()))
    assert state.stranded is True
    assert state.closed is False
    assert paper.account.stranded_inventory_cost_usd > Decimal("0")
    assert paper.account.realized_pnl_usd == Decimal("0")


def test_exit_releases_cash_without_resetting_station_day_cumulative_buy_cost(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    day_key = ("KLAX", "2026-09-02")
    cumulative_before_exit = paper.station_day_buy_cost[day_key]

    paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=2), closed_event_ids={"event"})

    assert paper.account.buy_reserved_usd == Decimal("0")
    assert paper.account.positions[next(iter(paper.account.positions))].shares == Decimal("0")
    assert paper.station_day_buy_cost[day_key] == cumulative_before_exit == Decimal("20")
def test_lifecycle_sweep_expires_without_later_token_snapshot_and_releases_residual(tmp_path) -> None:
    paper = processor(tmp_path)
    order = paper.process_snapshot(snapshot())
    assert order is not None
    paper.process_trade(TradeEvent(BASE + timedelta(minutes=1), "token", ShadowSide.SELL, "0.75", "101", "queue-only"))
    expired = paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=15))
    assert expired == (order,)
    assert order.state is ShadowOrderState.EXPIRED
    # One opposite-taker share crossed the 100-share queue.  Timeout releases
    # only the $19.25 unfilled BUY reservation; the filled inventory/cost is
    # retained and a second sweep cannot release it again.
    assert order.filled_shares == Decimal("1")
    assert paper.account.buy_reserved_usd == Decimal("0")
    assert paper.account.inventory_cost_usd == Decimal("0.75")
    assert paper.account.cash_usd == Decimal("199.25")
    assert paper.sweep_lifecycle(as_of=BASE + timedelta(minutes=16)) == ()
    assert paper.account.cash_usd == Decimal("199.25")


def test_confirmation_is_receipt_ordered_consumed_once_and_requires_prior_fill(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    stale = snapshot(
        at=BASE + timedelta(minutes=2),
        metadata={
            "weather_observation_id": f"weather-{(BASE - timedelta(minutes=1)).isoformat()}",
            "weather_source_timestamp": (BASE - timedelta(minutes=1)).isoformat(),
            "weather_received_at": (BASE - timedelta(seconds=30)).isoformat(),
            "weather_unchanged": True,
        },
    )
    assert paper.process_snapshot(stale) is None
    eligible = snapshot(
        at=BASE + timedelta(minutes=3), metadata={"weather_unchanged": True}
    )
    assert paper.process_snapshot(eligible) is not None
    assert paper.process_snapshot(eligible) is None


def test_conditional_dip_requires_two_ticks_from_latest_actual_fill(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    confirmation = snapshot(
        at=BASE + timedelta(minutes=2),
        metadata={"weather_observation_id": "weather-confirmation", "weather_unchanged": True},
    )
    second = paper.process_snapshot(confirmation)
    assert second is not None
    paper.process_trade(TradeEvent(BASE + timedelta(minutes=3), "token", ShadowSide.SELL, "0.75", "200", "entry-2"))
    one_tick = snapshot(at=BASE + timedelta(minutes=4), bid="0.74", ask="0.79", metadata={"weather_unchanged": True})
    assert paper.process_snapshot(one_tick) is None
    two_ticks = snapshot(at=BASE + timedelta(minutes=5), bid="0.73", ask="0.78", metadata={"weather_unchanged": True})
    assert paper.process_snapshot(two_ticks) is not None


def test_close_without_native_bid_strands_inventory_and_keeps_cost(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    closing = BookSnapshot(
        timestamp=BASE + timedelta(minutes=2), event_id="event", market_id="market", token_id="token",
        station_id="KLAX", market_day="2026-09-02", season_version="season-v1", bids=(), asks=(("0.80", "100"),),
        book_complete=False,
    )
    paper.close_portfolio(closing, reason="market_resolved")
    assert paper.account.stranded_inventory_cost_usd > Decimal("0")
    assert paper.status()["stranded_positions"] == 1


def test_exit_stages_use_initial_share_quarters_without_oversell(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    engine = next(iter(paper.engines.values()))
    state = next(iter(paper.states.values()))
    assert state.cumulative_bought_shares == engine.inventory_shares
    first = paper._submit_exit_if_eligible(snapshot(at=BASE + timedelta(minutes=2), bid="0.80", ask="0.85"))
    assert first is not None
    assert first.requested_shares == state.cumulative_bought_shares * Decimal("0.25")
    paper.process_trade(TradeEvent(BASE + timedelta(minutes=3), "token", ShadowSide.BUY, "0.85", "200", "exit"))
    state.completed_exit_stages.add(0)
    later = paper._submit_exit_if_eligible(snapshot(at=BASE + timedelta(minutes=4), bid="0.85", ask="0.90"))
    assert later is not None
    assert later.requested_shares <= engine.inventory_shares


def test_paper_status_reports_frozen_identity_and_eligibility(tmp_path) -> None:
    paper = PaperSpreadProcessor(ledger=PaperLedger(tmp_path / "paper.jsonl"), strategy=config())
    status = paper.status()
    assert status["execution_enabled"] is False
    assert len(status["config_sha256"]) == 64
    assert status["account"]["initial_cash_usd"] == "200"
    assert status["paper_score_eligible"] is False
    assert "supervisor_evidence_unreadable" in status["paper_score_ineligible_reasons"]
