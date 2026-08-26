import json
from datetime import UTC, datetime
from pathlib import Path

from poly_weather.domain import SettlementSpec
from poly_weather.polymarket_status import UpstreamQualityWindow, persist_quality_windows
from poly_weather.real_no_books import (
    analyze_real_no_books,
    archived_event_metadata,
    paired_book_snapshots,
)


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


def test_real_book_pairing_excludes_legacy_rows_by_official_window(tmp_path: Path) -> None:
    archive = (
        tmp_path
        / "raw"
        / "polymarket_book_checkpoints"
        / "2026-08-26"
        / "events.jsonl"
    )
    archive.parent.mkdir(parents=True)
    persist_quality_windows(
        tmp_path / "runtime" / "polymarket_quality_windows.json",
        (
            UpstreamQualityWindow(
                incident_id="maintenance",
                title="CLOB maintenance",
                incident_type="maintenance",
                start_at=datetime(2026, 8, 26, 4, tzinfo=UTC),
                end_at=datetime(2026, 8, 26, 7, tzinfo=UTC),
                affected_components=("Clob Websocket",),
                affects_market_data=True,
                affects_trading=True,
                status="completed",
                source_url="https://status.polymarket.com/maintenance",
                default_excluded=True,
            ),
        ),
    )
    rows = [
        _row("2026-08-26T05:00:00+00:00", "Yes", "0.03", "0.04"),
        _row("2026-08-26T05:00:01+00:00", "No", "0.96", "0.97"),
    ]
    archive.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    assert paired_book_snapshots([archive]) == []
    assert len(
        paired_book_snapshots([archive], exclude_upstream_degraded=False)
    ) == 1


def test_archived_event_metadata_uses_slug_and_registry_not_rotating_state() -> None:
    specs = (
        SettlementSpec(
            key="la",
            market_slug_pattern=(
                r"highest-temperature-in-los-angeles-on-[a-z]+-\d{1,2}-\d{4}"
            ),
            station_id="KLAX",
            timezone="America/Los_Angeles",
        ),
    )

    result = archived_event_metadata(
        [
            "highest-temperature-in-los-angeles-on-august-24-2026",
            "not-a-registered-event",
        ],
        specs,
    )

    assert result == {
        "highest-temperature-in-los-angeles-on-august-24-2026": {
            "station_id": "KLAX",
            "timezone": "America/Los_Angeles",
            "target_date": "2026-08-24",
        }
    }
