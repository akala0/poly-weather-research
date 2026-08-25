import json
from datetime import UTC, datetime

from poly_weather.liquidity import (
    archived_liquidity_rows_from_jsonl,
    liquidity_health_rows,
)


def test_liquidity_health_uses_full_ask_depth_and_candidate_window() -> None:
    rows = liquidity_health_rows(
        [
            (
                datetime(2026, 8, 24, 21, 0, tzinfo=UTC),
                "highest-temperature-in-los-angeles-on-august-24-2026/"
                "highest-temperature-in-los-angeles-on-august-24-2026-78-79f:Yes",
                "0.50",
                "0.52",
                json.dumps([{"price": "0.50", "size": "1000"}]),
                json.dumps(
                    [
                        {"price": "0.52", "size": "100"},
                        {"price": "0.60", "size": "1000"},
                    ]
                ),
            )
        ]
    )

    assert len(rows) == 1
    assert rows[0]["station_id"] == "KLAX"
    assert rows[0]["average_spread"] == 0.02
    assert rows[0]["slippage_200_usd"] is not None
    assert rows[0]["slippage_200_usd"] > 0
    assert rows[0]["insufficient_1000_frequency"] == 1.0


def test_jsonl_liquidity_reader_keeps_latest_book_per_asset_minute(tmp_path) -> None:
    archive = (
        tmp_path
        / "raw"
        / "polymarket_clob_websocket"
        / "2026-08-24"
        / "events.jsonl"
    )
    archive.parent.mkdir(parents=True)
    base = {
        "asset_id": "yes-token",
        "market_slug": "highest-temperature-in-los-angeles-on-august-24-2026/"
        "highest-temperature-in-los-angeles-on-august-24-2026-78-79f:Yes",
        "best_bid": "0.50",
        "event_type": "book",
        "book_complete": True,
        "bids": [{"price": "0.50", "size": "1000"}],
        "asks": [{"price": "0.55", "size": "1000"}],
        "raw": {},
    }
    first = {**base, "received_at": "2026-08-24T21:00:01+00:00", "best_ask": "0.55"}
    latest = {
        **base,
        "received_at": "2026-08-24T21:00:59+00:00",
        "best_ask": "0.52",
        "event_type": "price_change",
        "bids": None,
        "asks": None,
        "raw": {
            "price_changes": [
                {
                    "asset_id": "yes-token",
                    "side": "SELL",
                    "price": "0.52",
                    "size": "1000",
                },
                {
                    "asset_id": "yes-token",
                    "side": "SELL",
                    "price": "0.55",
                    "size": "0",
                },
            ]
        },
    }
    future = {
        **latest,
        "received_at": "2026-08-24T21:01:00+00:00",
        "best_ask": "0.90",
    }
    archive.write_text(
        "\n".join(json.dumps(row) for row in (first, latest, future)) + "\n",
        encoding="utf-8",
    )

    rows = archived_liquidity_rows_from_jsonl(
        tmp_path,
        end=datetime(2026, 8, 24, 21, 1, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert rows[0]["sample_count"] == 1
    assert rows[0]["average_spread"] == 0.02
