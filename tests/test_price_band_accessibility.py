from datetime import UTC, datetime, timedelta

from poly_weather.price_band_accessibility import (
    NO_PRIOR_ASK_LABEL,
    PRICE_BAND_LABELS,
    analyze_price_band_accessibility,
    price_band_for_ask,
)

BASE = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _pair(
    *,
    minute: int,
    asset_id: str,
    asks: list[dict[str, str]],
    suffix: str,
) -> dict[str, object]:
    timestamp = BASE + timedelta(minutes=minute)
    return {
        "observed_at": timestamp,
        "event_slug": "la-event",
        "market_slug": f"la-event/la-event-{suffix}:No",
        "no": {
            "_timestamp": timestamp,
            "asset_id": asset_id,
            "bids": [{"price": "0.30", "size": "100"}],
            "asks": asks,
        },
        "yes": {
            "_timestamp": timestamp,
            "asset_id": f"yes-{asset_id}",
            "bids": [{"price": "0.60", "size": "100"}],
            "asks": [{"price": "0.70", "size": "100"}],
        },
    }


METADATA = {
    "la-event": {
        "station_id": "KLAX",
        "timezone": "UTC",
        "target_date": "2026-08-26",
    }
}


def test_price_band_boundaries_are_true_ask_bands() -> None:
    assert price_band_for_ask("0.299") == "<0.30"
    assert price_band_for_ask("0.30") == "0.30–0.50"
    assert price_band_for_ask("0.50") == "0.50–0.70"
    assert price_band_for_ask("0.85") == "0.85–0.95"
    assert price_band_for_ask("0.95") == "0.95–0.99"
    assert price_band_for_ask("0.99") == "≥0.99"
    assert price_band_for_ask("1.00") == "≥0.99"
    assert PRICE_BAND_LABELS == (
        "<0.30",
        "0.30–0.50",
        "0.50–0.70",
        "0.70–0.85",
        "0.85–0.95",
        "0.95–0.99",
        "≥0.99",
    )


def test_empty_ask_cohort_never_uses_a_future_ask_and_paths_use_real_books() -> None:
    pairs = [
        _pair(
            minute=0,
            asset_id="a",
            asks=[{"price": "0.40", "size": "100"}],
            suffix="first",
        ),
        _pair(minute=5, asset_id="a", asks=[], suffix="empty"),
        _pair(
            minute=10,
            asset_id="a",
            asks=[{"price": "0.80", "size": "100"}],
            suffix="future",
        ),
        _pair(minute=0, asset_id="b", asks=[], suffix="no-prior"),
    ]

    result = analyze_price_band_accessibility(
        pairs,
        event_metadata=METADATA,
        typical_peak_minutes_by_station={"KLAX": 12 * 60},
    )

    records = result["records"]
    first = next(row for row in records if row["asset_id"] == "a" and row["quote_at"] == BASE.isoformat())
    empty = next(
        row
        for row in records
        if row["asset_id"] == "a" and row["cohort_source"] == "prior_true_ask"
    )
    future = next(row for row in records if row["asset_id"] == "a" and row["quote_at"] == (BASE + timedelta(minutes=10)).isoformat())
    no_prior = next(row for row in records if row["asset_id"] == "b")

    assert first["current_ask_band"] == "0.30–0.50"
    assert first["cohort_ask_band"] == "0.30–0.50"
    assert first["cohort_source"] == "current_true_ask"
    assert empty["current_ask_band"] == "no_ask"
    assert empty["cohort_ask_band"] == "0.30–0.50"
    assert empty["cohort_ask_age_minutes"] == 5.0
    assert future["cohort_ask_band"] == "0.70–0.85"
    assert no_prior["cohort_ask_band"] == NO_PRIOR_ASK_LABEL
    assert no_prior["cohort_source"] == "no_prior_true_ask"

    fills = first["fills"]["20"]
    assert fills["no_buy"]["entry"]["complete"] is True
    assert fills["yes_sell"]["entry"]["complete"] is True
    assert fills["no_buy"]["entry"]["average"] == 0.4
    assert fills["yes_sell"]["entry"]["average"] == 0.6
    assert fills["no_buy"]["round_trip_complete"] is True
    assert fills["yes_sell"]["round_trip_complete"] is True
    assert fills["no_buy"]["gross_price_move_hurdle"] == 0.0225

    summary = result["band_summaries"]["KLAX"]["0.30–0.50"]
    assert summary["current_no_ask"]["count"] == 1
    assert summary["current_no_ask"]["sample_count"] == 2
    assert summary["current_no_ask"]["wilson_low"] is not None
    size_20 = next(row for row in summary["sizes"] if row["size_usd"] == 20.0)
    assert size_20["no_buy"]["entry_full_fill"]["count"] == 1
    assert size_20["yes_sell"]["entry_full_fill"]["count"] == 2
    assert result["semantics"]["trade_price"].startswith("成交价不是可成交 ask")
