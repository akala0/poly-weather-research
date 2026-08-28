from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from poly_weather.complement_pair import (
    ComplementPairConfig,
    ComplementPairReplay,
    ComplementPairState,
    predefined_complement_pair_configs,
    replay_complement_pairs,
    replay_complement_pairs_streaming,
    replay_complement_pairs_streaming_grid,
)
from poly_weather.shadow_orders import BookSnapshot, TradeEvent

BASE = datetime(2026, 8, 28, 12, tzinfo=UTC)


def snapshots(
    minute: int = 0,
    *,
    market_id: str = "market-a",
    yes_token: str = "yes-a",
    no_token: str = "no-a",
    yes_bid: str = "0.40",
    yes_ask: str = "0.50",
    no_bid: str = "0.40",
    no_ask: str = "0.50",
    status: str = "normal",
) -> tuple[BookSnapshot, BookSnapshot]:
    at = BASE + timedelta(minutes=minute)
    common = {
        "timestamp": at,
        "event_id": "event-a",
        "market_id": market_id,
        "station_id": "KLAX",
        "market_day": "2026-08-28",
        "season_version": "heat-v1",
        "min_order_size": "5",
        "upstream_status": status,
    }
    return (
        BookSnapshot(
            token_id=yes_token,
            bids=((yes_bid, "100"),),
            asks=((yes_ask, "100"),),
            **common,
        ),
        BookSnapshot(
            token_id=no_token,
            bids=((no_bid, "100"),),
            asks=((no_ask, "100"),),
            **common,
        ),
    )


def config(**changes: object) -> ComplementPairConfig:
    values: dict[str, object] = {
        "pair_notional_usd": Decimal("20"),
        "station_day_budget_usd": Decimal("200"),
        "safety_buffer": Decimal("0.02"),
        "hedge_timeout": timedelta(minutes=15),
        "max_unmatched_usd": Decimal("20"),
        "max_hedge_price": Decimal("0.99"),
    }
    values.update(changes)
    return ComplementPairConfig(**values)


def fill_trade(minute: int, token: str, *, size: str = "200", price: str = "0.40") -> TradeEvent:
    return TradeEvent(
        BASE + timedelta(minutes=minute),
        token,
        "SELL",
        price,
        size,
        f"trade-{token}-{minute}",
    )


def archive_pair(minute: int) -> dict[str, object]:
    yes, no = snapshots(minute)
    common = {
        "event_slug": yes.event_id,
        "market_slug": yes.market_id,
        "station_id": yes.station_id,
        "market_day": yes.market_day,
        "season_version": yes.season_version,
        "in_season": True,
    }
    return {
        **common,
        "yes": {
            "_timestamp": yes.timestamp,
            "asset_id": yes.token_id,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "book_complete": True,
            "tick_size": "0.01",
            "min_order_size": "5",
        },
        "no": {
            "_timestamp": no.timestamp,
            "asset_id": no.token_id,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "book_complete": True,
            "tick_size": "0.01",
            "min_order_size": "5",
        },
    }


def test_actual_two_leg_fills_create_locked_pair_not_planned_quotes() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    yes, no = snapshots()
    replay.process_pair(yes, no)
    portfolio = next(iter(replay.portfolios.values()))

    assert portfolio.submitted_pair_count == 1
    assert portfolio.paired_shares == 0
    assert portfolio.state is ComplementPairState.RESTING

    replay.process_trades((fill_trade(1, "yes-a"),))
    assert portfolio.had_one_leg_fill is True
    assert portfolio.paired_shares == 0
    replay.process_trades((fill_trade(2, "no-a"),))

    assert portfolio.paired_shares == Decimal("25")
    assert portfolio.locked_edge_usd == Decimal("5.00")
    assert portfolio.state is ComplementPairState.LOCKED


def test_partial_fills_pair_only_minimum_same_shares() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    yes, no = snapshots()
    replay.process_pair(yes, no)
    replay.process_trades(
        (
            fill_trade(1, "yes-a", size="110"),
            fill_trade(2, "no-a", size="106"),
        )
    )
    portfolio = next(iter(replay.portfolios.values()))

    assert portfolio.paired_shares == Decimal("6")
    assert portfolio.unmatched_side == "yes"
    assert portfolio.unmatched_shares == Decimal("4")


def test_pair_risk_budget_is_aggregated_by_station_day_not_per_market() -> None:
    replay = ComplementPairReplay(
        config=config(station_day_budget_usd=Decimal("30")), fill_model="queue_aware"
    )
    replay.process_pair(*snapshots(market_id="market-a", yes_token="yes-a", no_token="no-a"))
    replay.process_pair(*snapshots(market_id="market-b", yes_token="yes-b", no_token="no-b"))

    portfolios = list(replay.portfolios.values())
    assert sum(portfolio.submitted_pair_count for portfolio in portfolios) == 1
    assert any("station_day_budget_exceeded" in portfolio.rejection_reasons for portfolio in portfolios)


def test_reusing_a_token_for_a_different_pair_halts_everything() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    replay.process_pair(*snapshots(market_id="market-a", yes_token="yes-a", no_token="no-a"))
    replay.process_pair(*snapshots(market_id="market-b", yes_token="yes-a", no_token="no-b"))

    assert replay.halted is True
    assert replay.halt_reason == "complement_pair_token_reused_by_other_market"
    assert all(portfolio.state is ComplementPairState.HALTED for portfolio in replay.portfolios.values())


def test_cross_token_fill_routed_into_pair_halts_immediately() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    replay.process_pair(*snapshots())
    portfolio = next(iter(replay.portfolios.values()))

    portfolio.process_trade(fill_trade(1, "unrelated-token"))

    assert portfolio.halted is True
    assert portfolio.state is ComplementPairState.HALTED
    assert portfolio.halt_reason == "cross_token_trade_routed_to_complement_pair"


def test_maintenance_cancels_both_unfilled_legs_and_never_submits_more() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    replay.process_pair(*snapshots())
    portfolio = next(iter(replay.portfolios.values()))
    replay.process_pair(*snapshots(1, status="maintenance"))

    assert not portfolio.active_orders
    assert portfolio.submitted_pair_count == 1
    assert all(
        order.cancel_reason in {"upstream_maintenance", "complement_pair_health_gate"}
        for order in (*portfolio.yes_engine.orders, *portfolio.no_engine.orders)
    )


def test_hedge_cost_cap_refuses_expensive_missing_leg_then_unwinds_real_bid() -> None:
    replay = ComplementPairReplay(
        config=config(hedge_timeout=timedelta(seconds=1)), fill_model="queue_aware"
    )
    yes, no = snapshots()
    replay.process_pair(yes, no)
    replay.process_trades((fill_trade(1, "yes-a"),))
    # The missing NO ask is above the fee-inclusive remaining pair-cost cap.
    replay.process_pair(*snapshots(2, no_ask="0.80", yes_bid="0.30", no_bid="0.20"))
    portfolio = next(iter(replay.portfolios.values()))

    assert portfolio.no_engine.inventory_shares == 0
    assert portfolio.yes_engine.inventory_shares == 0
    assert portfolio.unwind_fill_count == 1
    assert portfolio.unwind_pnl_usd < 0
    assert all(
        fill.source != "hedge_taker_depth"
        for order in portfolio.no_engine.orders
        for fill in order.fills
    )


def test_bounded_taker_hedge_records_actual_ask_cost_and_taker_fee() -> None:
    replay = ComplementPairReplay(
        config=config(hedge_timeout=timedelta(seconds=1)), fill_model="queue_aware"
    )
    replay.process_pair(*snapshots())
    replay.process_trades((fill_trade(1, "yes-a"),))
    replay.process_pair(*snapshots(2, no_ask="0.50", no_bid="0.49"))
    portfolio = next(iter(replay.portfolios.values()))

    assert portfolio.paired_shares == Decimal("25")
    assert portfolio.no_engine.fees_usd > 0
    assert any(
        fill.source == "hedge_taker_depth"
        for order in portfolio.no_engine.orders
        for fill in order.fills
    )
    assert portfolio.locked_edge_usd > 0


def test_replay_strictly_excludes_trade_at_decision_timestamp() -> None:
    yes, no = snapshots()
    pairs = [
        {
            "event_slug": yes.event_id,
            "market_slug": yes.market_id,
            "station_id": yes.station_id,
            "market_day": yes.market_day,
            "season_version": yes.season_version,
            "yes": {
                "_timestamp": yes.timestamp,
                "asset_id": yes.token_id,
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}],
                "book_complete": True,
                "tick_size": "0.01",
                "min_order_size": "5",
            },
            "no": {
                "_timestamp": no.timestamp,
                "asset_id": no.token_id,
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}],
                "book_complete": True,
                "tick_size": "0.01",
                "min_order_size": "5",
            },
        }
    ]

    result = replay_complement_pairs(
        pairs,
        trades=(fill_trade(0, "yes-a"), fill_trade(0, "no-a")),
        config=config(),
    )

    assert result["models"]["queue_aware"]["maker_fill_count"] == 0
    assert result["models"]["queue_aware"]["completed_pair_count"] == 0


def test_station_day_is_the_wilson_cluster_not_pair_count() -> None:
    replay = ComplementPairReplay(config=config(), fill_model="queue_aware")
    replay.process_pair(*snapshots(market_id="market-a", yes_token="yes-a", no_token="no-a"))
    replay.process_pair(*snapshots(market_id="market-b", yes_token="yes-b", no_token="no-b"))
    replay.process_trades(
        (
            fill_trade(1, "yes-a"),
            fill_trade(1, "no-a"),
            fill_trade(1, "yes-b"),
            fill_trade(1, "no-b"),
        )
    )

    summary = replay.summary()
    rate = summary["completion_given_one_leg_station_day_rate"]
    assert summary["completed_pair_count"] == 2
    assert summary["station_day_cluster_count"] == 1
    assert rate["sample_count"] == 1
    assert rate["statistically_unreliable"] is True


def test_predefined_sensitivity_grid_is_fixed_and_complete() -> None:
    configs = predefined_complement_pair_configs(config())

    assert len(configs) == 24
    assert {item.safety_buffer for item in configs} == {
        Decimal("0.01"),
        Decimal("0.02"),
        Decimal("0.03"),
    }
    assert {item.hedge_timeout.total_seconds() / 60 for item in configs} == {5, 15, 30, 60}


def test_streaming_replay_matches_finite_replay_without_retaining_all_pairs() -> None:
    pairs = [archive_pair(0), archive_pair(3)]
    trades = (fill_trade(1, "yes-a"), fill_trade(2, "no-a"))

    finite = replay_complement_pairs(pairs, trades=trades, config=config())
    streaming = replay_complement_pairs_streaming(iter(pairs), trades=trades, config=config())

    for key in ("completed_pair_count", "locked_edge_usd", "maker_fill_count"):
        assert streaming["models"]["queue_aware"][key] == finite["models"]["queue_aware"][key]


def test_streaming_replay_rejects_nonchronological_snapshot_input() -> None:
    with pytest.raises(ValueError, match="chronological"):
        replay_complement_pairs_streaming(
            iter([archive_pair(3), archive_pair(0)]), config=config()
        )


def test_streaming_grid_shares_archive_scan_semantics_with_single_config() -> None:
    pairs = [archive_pair(0), archive_pair(3)]
    trades = (fill_trade(1, "yes-a"), fill_trade(2, "no-a"))
    selected = (config(), config(safety_buffer=Decimal("0.03")))

    grid = replay_complement_pairs_streaming_grid(
        iter(pairs), trades=trades, configs=selected
    )
    single = replay_complement_pairs_streaming(
        iter(pairs), trades=trades, config=selected[0]
    )

    assert grid[0]["strategy"] == single["strategy"]
    assert grid[0]["models"]["queue_aware"]["completed_pair_count"] == single["models"][
        "queue_aware"
    ]["completed_pair_count"]
    assert grid[1]["strategy"]["safety_buffer"] == "0.03"
