import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from poly_weather.domain import SettlementSpec
from poly_weather.polymarket_status import UpstreamQualityWindow, persist_quality_windows
from poly_weather.real_no_books import (
    analyze_real_no_books,
    archived_event_metadata,
    archived_market_rule_index,
    iter_paired_book_snapshots,
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


def test_real_book_pairing_reads_retention_gzip(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl.gz"
    rows = [
        _row("2026-08-24T12:00:00+00:00", "Yes", "0.03", "0.04"),
        _row("2026-08-24T12:01:00+00:00", "No", "0.96", "0.97"),
    ]
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(row) for row in rows) + "\n")

    assert len(paired_book_snapshots([path])) == 1


def test_streaming_pairer_merges_paths_chronologically_without_raw_payload(tmp_path: Path) -> None:
    early = tmp_path / "early.jsonl"
    late = tmp_path / "late.jsonl"
    early.write_text(
        "\n".join(
            (
                json.dumps(_row("2026-08-24T12:00:00+00:00", "Yes", "0.03", "0.04")),
                json.dumps(_row("2026-08-24T12:01:00+00:00", "No", "0.96", "0.97")),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    late.write_text(
        "\n".join(
            (
                json.dumps(_row("2026-08-24T12:10:00+00:00", "Yes", "0.04", "0.05")),
                json.dumps(_row("2026-08-24T12:11:00+00:00", "No", "0.95", "0.96")),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    pairs = list(iter_paired_book_snapshots([late, early]))

    assert [pair["observed_at"] for pair in pairs] == [
        datetime(2026, 8, 24, 12, 1, tzinfo=UTC),
        datetime(2026, 8, 24, 12, 11, tzinfo=UTC),
    ]
    assert "raw" not in pairs[0]["yes"]
    assert "raw" not in pairs[0]["no"]


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


def test_paired_books_merge_only_previously_archived_gamma_rules(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    books = data_root / "raw" / "polymarket_book_checkpoints" / "2026-08-28"
    gamma = data_root / "raw" / "polymarket_gamma_event" / "2026-08-28"
    books.mkdir(parents=True)
    gamma.mkdir(parents=True)
    token_yes = "yes-token"
    token_no = "no-token"
    timestamp = "2026-08-28T12:00:00+00:00"
    rows = [
        _row(timestamp, "Yes", "0.39", "0.41") | {"asset_id": token_yes},
        _row("2026-08-28T12:00:01+00:00", "No", "0.59", "0.61")
        | {"asset_id": token_no},
    ]
    archive = books / "events.jsonl"
    archive.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    gamma_record = {
        "fetched_at": "2026-08-28T11:59:00+00:00",
        "payload": {
            "markets": [
                {
                    "clobTokenIds": json.dumps([token_yes, token_no]),
                    "orderPriceMinTickSize": "0.01",
                    "orderMinSize": "5",
                }
            ]
        },
    }
    (gamma / "events.jsonl").write_text(
        json.dumps(gamma_record) + "\n", encoding="utf-8"
    )

    index = archived_market_rule_index([archive])
    assert index[token_yes][0]["min_order_size"] == "5"
    pair = paired_book_snapshots([archive])[0]
    assert pair["yes"]["rule_provenance"]["min_order_size"] == "5"
    assert (
        pair["yes"]["rule_provenance"]["min_order_size_source"]
        == "gamma_event_archived_market_metadata"
    )


def test_archived_tick_change_is_effective_only_after_its_receipt(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    books = data_root / "raw" / "polymarket_book_checkpoints" / "2026-08-28"
    gamma = data_root / "raw" / "polymarket_gamma_event" / "2026-08-28"
    books.mkdir(parents=True)
    gamma.mkdir(parents=True)
    archive = books / "events.jsonl"
    rows = [
        _row("2026-08-28T12:00:00+00:00", "Yes", "0.39", "0.41")
        | {"asset_id": "yes-token"},
        _row("2026-08-28T12:00:01+00:00", "No", "0.59", "0.61")
        | {"asset_id": "no-token"},
        _row("2026-08-28T12:10:00+00:00", "Yes", "0.39", "0.41")
        | {"asset_id": "yes-token"},
        _row("2026-08-28T12:10:01+00:00", "No", "0.59", "0.61")
        | {"asset_id": "no-token"},
    ]
    archive.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    def gamma_record(fetched_at: str, tick: str) -> str:
        return json.dumps(
            {
                "fetched_at": fetched_at,
                "payload": {
                    "markets": [
                        {
                            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
                            "orderPriceMinTickSize": tick,
                            "orderMinSize": "5",
                        }
                    ]
                },
            }
        )

    (gamma / "events.jsonl").write_text(
        "\n".join(
            (
                gamma_record("2026-08-28T11:59:00+00:00", "0.01"),
                gamma_record("2026-08-28T12:05:00+00:00", "0.001"),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    pairs = paired_book_snapshots([archive])
    assert [pair["yes"]["rule_provenance"]["tick_size"] for pair in pairs] == [
        "0.01",
        "0.001",
    ]
