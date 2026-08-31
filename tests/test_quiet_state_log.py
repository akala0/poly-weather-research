from datetime import UTC, datetime, timedelta

from poly_weather.quiet_state_log import append_forward_quiet_state_log

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def _record(minute: int, identifier: str) -> dict[str, object]:
    return {
        "record_id": identifier,
        "observed_at": BASE + timedelta(minutes=minute),
        "state": "QUIET",
        "execution_enabled": False,
    }


def test_forward_log_tail_bootstrap_suppresses_historical_records(tmp_path) -> None:
    ledger = tmp_path / "state.jsonl"
    cursor = tmp_path / "cursor.json"

    first = append_forward_quiet_state_log(
        [_record(0, "old")], ledger_path=ledger, cursor_path=cursor
    )
    assert first["bootstrap"] is True
    assert first["suppressed_historical_record_count"] == 1
    assert not ledger.exists()

    second = append_forward_quiet_state_log(
        [_record(0, "old"), _record(1, "new")], ledger_path=ledger, cursor_path=cursor
    )
    assert second["appended_state_record_count"] == 1
    assert "new" in ledger.read_text(encoding="utf-8")
    assert "old" not in ledger.read_text(encoding="utf-8")


def test_forward_log_preserves_same_timestamp_dedupe_cursor(tmp_path) -> None:
    ledger = tmp_path / "state.jsonl"
    cursor = tmp_path / "cursor.json"
    append_forward_quiet_state_log([], ledger_path=ledger, cursor_path=cursor)
    first = append_forward_quiet_state_log(
        [_record(1, "one"), _record(1, "two")],
        ledger_path=ledger,
        cursor_path=cursor,
        bootstrap_at_tail=False,
    )
    assert first["appended_state_record_count"] == 2
    second = append_forward_quiet_state_log(
        [_record(1, "one"), _record(1, "two")],
        ledger_path=ledger,
        cursor_path=cursor,
        bootstrap_at_tail=False,
    )
    assert second["appended_state_record_count"] == 0
