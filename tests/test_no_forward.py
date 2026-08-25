import json
from datetime import UTC, date, datetime

from poly_weather.domain import Market
from poly_weather.no_forward import NoForwardTracker, forward_summary, wilson_interval
from poly_weather.signal_engine import LiveSignalConfig


def _fixture() -> tuple[LiveSignalConfig, dict, dict]:
    market = Market.from_gamma(
        {
            "id": "m1",
            "question": "Will the high be 80-81F?",
            "slug": "event-80-81f",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.2", "0.8"]',
            "clobTokenIds": '["yes-1", "no-1"]',
        }
    )
    config = LiveSignalConfig(
        event_id="e1",
        event_slug="event",
        station_id="KLGA",
        timezone="America/New_York",
        target_date=date(2026, 8, 24),
        markets=(market,),
        contract_verified=True,
        contract_reason="verified",
    )
    output = {
        "event_slug": "event",
        "warming_rate_f_per_hour": 1.2,
        "hours_to_typical_peak": 3.0,
        "signals": [
            {
                "market_id": "m1",
                "market_slug": market.slug,
                "warming_window_no": True,
                "physical_margin_f": -3.0,
                "margin_tier": "< -3F",
                "no_best_ask": 0.9,
                "execution_estimates": [],
            }
        ],
    }
    books = {
        "no-1": {
            "best_bid": "0.96",
            "best_ask": "0.97",
            "bids": [{"price": "0.96", "size": "100"}],
            "asks": [{"price": "0.97", "size": "100"}],
            "book_complete": True,
        }
    }
    return config, output, books


def test_wilson_interval_uses_conservative_lower_bound() -> None:
    interval = wilson_interval(26, 26)
    assert interval is not None
    assert 0.86 < interval[0] < 0.88
    assert interval[1] == 1.0


def test_forward_tracker_records_first_trigger_and_real_book_once(tmp_path) -> None:
    config, output, books = _fixture()
    tracker = NoForwardTracker(tmp_path)
    now = datetime(2026, 8, 24, 13, tzinfo=UTC)
    tracker.observe([output], (config,), books, now)
    tracker.observe([output], (config,), books, now)

    path = next((tmp_path / "raw" / "no_forward_validation").glob("*/events.jsonl"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["record_type"] for row in rows] == ["trigger", "bid_reached_0.95"]
    assert rows[0]["no_book"]["asks"][0]["price"] == "0.97"
    assert all(row["execution_enabled"] is False for row in rows)
    assert forward_summary(tmp_path)["trigger_count"] == 1

    tracker.record_settlement(
        {"assets_ids": ["yes-1", "no-1"], "winning_asset_id": "no-1"},
        datetime(2026, 8, 25, 13, tzinfo=UTC),
    )
    summary = forward_summary(tmp_path)
    assert summary["settled_count"] == 1
    assert summary["wins"] == 1
    assert summary["win_rate"] == 1.0
