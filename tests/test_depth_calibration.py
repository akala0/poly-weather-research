import json
from datetime import UTC, datetime

from poly_weather.depth_calibration import replay_books_at_or_before


def test_depth_replay_is_strictly_no_lookahead_and_applies_deltas(tmp_path) -> None:
    archive = (
        tmp_path
        / "raw"
        / "polymarket_clob_websocket"
        / "2026-08-24"
        / "events.jsonl"
    )
    archive.parent.mkdir(parents=True)
    rows = [
        {
            "received_at": "2026-08-24T20:00:00+00:00",
            "event_type": "book",
            "asset_id": "yes",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "raw": {},
        },
        {
            "received_at": "2026-08-24T20:01:00+00:00",
            "event_type": "price_change",
            "asset_id": "yes",
            "bids": None,
            "asks": None,
            "raw": {
                "price_changes": [
                    {"asset_id": "yes", "side": "SELL", "price": "0.50", "size": "0"},
                    {"asset_id": "yes", "side": "SELL", "price": "0.60", "size": "100"},
                ]
            },
        },
    ]
    archive.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    before_delta = datetime(2026, 8, 24, 20, 0, 30, tzinfo=UTC)
    at_delta = datetime(2026, 8, 24, 20, 1, tzinfo=UTC)

    snapshots = replay_books_at_or_before(
        tmp_path,
        {"yes": [before_delta, at_delta]},
    )

    assert snapshots[("yes", before_delta)] is not None
    assert snapshots[("yes", before_delta)].asks[0][0] == "0.50"
    assert snapshots[("yes", at_delta)] is not None
    assert snapshots[("yes", at_delta)].asks[0][0] == "0.60"
