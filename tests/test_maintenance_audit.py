from datetime import UTC, datetime

from poly_weather.maintenance_audit import audit_reconnect_rows
from poly_weather.polymarket_status import UpstreamQualityWindow


def _window() -> UpstreamQualityWindow:
    return UpstreamQualityWindow(
        incident_id="maintenance",
        title="CLOB maintenance",
        incident_type="maintenance",
        start_at=datetime(2026, 8, 26, 4, tzinfo=UTC),
        end_at=datetime(2026, 8, 26, 7, 30, tzinfo=UTC),
        affected_components=("Clob Websocket",),
        affects_market_data=True,
        affects_trading=True,
        status="completed",
        source_url="https://status.polymarket.com/maintenance",
        default_excluded=True,
    )


def test_reconnect_audit_attributes_exact_rows_but_not_legacy_count() -> None:
    result = audit_reconnect_rows(
        [
            {
                "record_type": "legacy_unattributed_summary",
                "reconnect_count": 28,
                "previous_run_id": "old",
                "previous_started_at": "2026-08-25T05:25:00+00:00",
                "previous_last_event_at": "2026-08-26T04:10:00+00:00",
            },
            {
                "observed_at": "2026-08-26T04:30:00+00:00",
                "error": "silence",
            },
            {
                "observed_at": "2026-08-26T08:00:00+00:00",
                "error": "other",
            },
        ],
        [_window()],
    )

    assert result["officially_attributed_reconnect_count"] == 1
    assert result["unattributed_exact_reconnect_count"] == 1
    assert result["legacy_unattributed_reconnect_count"] == 28
    legacy = result["legacy_summaries"][0]
    assert legacy["overlapping_official_windows"][0]["overlap_seconds"] == 600
    assert "unrecoverable_exact_times" in legacy["attribution"]
