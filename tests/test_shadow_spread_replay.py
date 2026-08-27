from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.shadow_orders import ShadowStrategyConfig, TradeEvent
from poly_weather.shadow_spread_replay import (
    default_shadow_strategy_config,
    render_shadow_spread_report,
    replay_shadow_spread,
)

BASE = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def row(minute: int, *, ask: str = "0.80", bid: str = "0.70") -> dict[str, object]:
    timestamp = BASE + timedelta(minutes=minute)
    return {
        "event_slug": "event-1",
        "market_slug": "market-1",
        "observed_at": timestamp,
        "station_id": "KLAX",
        "market_day": "2026-08-26",
        "season_version": "heat-2026-v1",
        "no": {
            "_timestamp": timestamp,
            "asset_id": "no-1",
            "bids": [{"price": bid, "size": "100"}],
            "asks": [{"price": ask, "size": "100"}],
            "book_complete": True,
            "tick_size": "0.01",
            "min_order_size": "5",
        },
    }


def config() -> ShadowStrategyConfig:
    base = default_shadow_strategy_config()
    return ShadowStrategyConfig(
        version=base.version,
        quote_mode=base.quote_mode,
        fill_model=base.fill_model,
        entry_bands={"KLAX": ((Decimal("0.70"), Decimal("0.85")),)},
        target_rise=Decimal("0.05"),
        order_timeout=base.order_timeout,
        max_active_orders=base.max_active_orders,
        market_budget_usd=base.market_budget_usd,
        tranche_usd=(Decimal("20"),),
        replenish_mode=base.replenish_mode,
    )


def test_replay_is_market_day_based_and_runs_three_fill_models() -> None:
    pairs = [row(0), row(5, ask="0.81", bid="0.71"), row(10, ask="0.82", bid="0.81")]
    trades = [
        TradeEvent(BASE + timedelta(minutes=1), "no-1", "SELL", "0.69", "200", "trade-1"),
        TradeEvent(BASE + timedelta(minutes=11), "no-1", "BUY", "0.82", "200", "trade-2"),
    ]
    result = replay_shadow_spread(pairs, trades=trades, config=config())
    assert result["execution_enabled"] is False
    assert result["independent_market_day_count"] == 1
    assert set(result["models"]) == {"touch", "queue_aware", "trade_through"}
    assert set(result["trigger_comparison"]) == {"price_band", "weather_market_lag"}
    assert all(model["execution_enabled"] is False for model in result["models"].values())
    assert result["same_second_policy"].startswith("ambiguous")


def test_replay_never_uses_trade_at_entry_timestamp() -> None:
    pairs = [row(0), row(5, ask="0.81", bid="0.71")]
    trades = [TradeEvent(BASE, "no-1", "SELL", "0.69", "200", "same-time")]
    result = replay_shadow_spread(pairs, trades=trades, config=config())
    for model in result["models"].values():
        assert model["fill_count"] == 0


def test_weather_lag_trigger_is_separate_from_price_band() -> None:
    pairs = [
        row(0, ask="0.40", bid="0.30"),
        {
            **row(5, ask="0.40", bid="0.30"),
            "weather_market_lag": True,
            "weather_improving": True,
        },
    ]
    base = config()
    strategy = ShadowStrategyConfig(
        version=base.version,
        trigger_strategy="weather_market_lag",
        quote_mode=base.quote_mode,
        fill_model=base.fill_model,
        entry_bands=base.entry_bands,
        target_rise=base.target_rise,
        order_timeout=base.order_timeout,
        max_active_orders=base.max_active_orders,
        market_budget_usd=base.market_budget_usd,
        tranche_usd=base.tranche_usd,
        replenish_mode=base.replenish_mode,
    )
    result = replay_shadow_spread(pairs, config=strategy)
    assert result["strategy"]["trigger_strategy"] == "weather_market_lag"
    assert result["models"]["queue_aware"]["trigger_candidate_count"] == 1
    assert result["models"]["queue_aware"]["price_band_candidate_count"] == 0


def test_report_keeps_n_a_and_shadow_boundary(tmp_path) -> None:
    result = replay_shadow_spread([], config=config())
    path = tmp_path / "shadow_spread_strategy_report.md"
    render_shadow_spread_report(result, path)
    text = path.read_text(encoding="utf-8")
    assert "execution_enabled=false" in text
    assert "成交价不是可成交 ask/bid" in text
    assert "没有至少 30 个独立 market-day" in text


def test_replay_touch_audit_sees_later_ask_touching_buy_limit() -> None:
    pairs = [row(0, ask="0.80", bid="0.70"), row(5, ask="0.70", bid="0.69")]
    result = replay_shadow_spread(pairs, config=config())
    touch = result["models"]["touch"]
    assert touch["fill_count"] >= 1
    assert touch["touch_audit"]["touch_count"] == 1
    assert any(row["side"] == "BUY" and row["filled_shares"] != "0" for row in touch["orders"])


def test_replay_does_not_consume_trade_before_local_availability() -> None:
    # The exchange trade happened at +1, but the local archive received it at
    # +6.  An order decided at +2 must not be retroactively filled by it.
    pairs = [row(2), row(5, ask="0.81", bid="0.71")]
    trades = [
        TradeEvent(
            BASE + timedelta(minutes=1),
            "no-1",
            "SELL",
            "0.70",
            "200",
            "late-trade",
            available_at=BASE + timedelta(minutes=6),
        )
    ]
    result = replay_shadow_spread(pairs, trades=trades, config=config())
    assert result["models"]["queue_aware"]["fill_count"] == 0
