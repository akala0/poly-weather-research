import json
from datetime import UTC, datetime
from pathlib import Path

from poly_weather.real_no_books import analyze_real_no_books, paired_book_snapshots


def _row(timestamp: str, outcome: str, bid: str, ask: str) -> dict[str, object]:
    return {
        "received_at": timestamp,
        "market_slug": f"event/event-80-81f:{outcome}",
        "book_complete": True,
        "bids": [{"price": bid, "size": "1000"}],
        "asks": [{"price": ask, "size": "1000"}],
        "last_trade_price": bid,
    }


def test_real_book_pairing_never_uses_future_side(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    rows = [
        _row("2026-08-24T12:00:00+00:00", "Yes", "0.03", "0.04"),
        _row("2026-08-24T12:01:00+00:00", "No", "0.96", "0.97"),
        _row("2026-08-24T12:10:00+00:00", "Yes", "0.04", "0.05"),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    pairs = paired_book_snapshots([path])

    assert len(pairs) == 1
    assert pairs[0]["observed_at"] == datetime(2026, 8, 24, 12, 1, tzinfo=UTC)
    result = analyze_real_no_books(pairs)
    assert result["complement_gap_max"] == 0.0
