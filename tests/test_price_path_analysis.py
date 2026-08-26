import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.price_path_analysis import (
    ArchivedWeatherObservation,
    analyze_price_paths,
    load_archived_weather_observations,
)

BASE = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
METADATA = {
    "event": {
        "station_id": "KLAX",
        "timezone": "UTC",
        "target_date": "2026-08-26",
    }
}


def _pair(
    *,
    market: str,
    minute: int,
    ask_price: str = "0.70",
    ask_size: str = "1000",
    bid_price: str = "0.74",
    bid_size: str = "1000",
) -> dict[str, object]:
    timestamp = BASE + timedelta(minutes=minute)
    return {
        "event_slug": "event",
        "market_slug": f"event/event-{market}",
        "observed_at": timestamp,
        "no": {
            "_timestamp": timestamp,
            "asset_id": f"no-{market}",
            "asks": [{"price": ask_price, "size": ask_size}] if ask_size != "0" else [],
            "bids": [{"price": bid_price, "size": bid_size}] if bid_size != "0" else [],
        },
        "yes": {"_timestamp": timestamp, "asks": [], "bids": []},
    }


def _observation(minute: int, temperature_f: str) -> ArchivedWeatherObservation:
    timestamp = BASE + timedelta(minutes=minute)
    return ArchivedWeatherObservation(
        station_id="KLAX",
        observed_at=timestamp,
        available_at=timestamp,
        temperature_f=Decimal(temperature_f),
    )


def test_path_uses_strictly_later_real_no_bids_and_records_gap_floor() -> None:
    pairs = [
        # The entry snapshot's own 0.80 bid is already above the +5¢
        # threshold.  A strict-future implementation must ignore it and
        # wait for the later 0.76 quote (the test would otherwise pass with
        # an incorrect zero-minute hit).
        _pair(market="70-71f", minute=0, bid_price="0.80"),
        _pair(market="70-71f", minute=5, ask_size="0", bid_price="0.76"),
        _pair(market="70-71f", minute=20, ask_size="0", bid_price="0.76"),
        _pair(market="70-71f", minute=0, bid_price="0.74"),
        _pair(market="70-71f", minute=5, ask_size="0", bid_price="0.65"),
        _pair(market="70-71f", minute=10, ask_size="0", bid_price="0.05"),
        _pair(market="70-71f", minute=20, ask_size="0", bid_price="0.05"),
    ]
    # Keep the two paths in separate markets while deliberately putting the
    # second path's entry at the same timestamp as the first path.  The
    # implementation must not treat the entry snapshot's bid as a future hit.
    pairs[3]["market_slug"] = "event/event-72-73f"
    pairs[4]["market_slug"] = "event/event-72-73f"
    pairs[5]["market_slug"] = "event/event-72-73f"
    pairs[6]["market_slug"] = "event/event-72-73f"
    for row in pairs[3:]:
        row["no"]["asset_id"] = "no-gap"  # type: ignore[index]

    result = analyze_price_paths(
        pairs,
        event_metadata=METADATA,
        observations_by_station={"KLAX": [_observation(-5, "70"), _observation(10, "74")]},
        typical_peak_minutes_by_station={"KLAX": 60},
    )

    assert result["usable_entry_count"] == 2
    group = next(row for row in result["station_bands"] if row["entry_band"] == "0.70–0.85")
    plus_five = group["targets"]["0.05"]
    assert plus_five["target_rate"]["count"] == 1
    assert plus_five["zero_gap_rate"]["count"] == 1
    assert plus_five["target_200_depth_rate"]["count"] == 1
    target_row = next(
        row for row in result["records"] if row["market_slug"].endswith("70-71f")
    )
    assert target_row["outcomes"]["0.05"]["duration_minutes"] == 5.0
    assert plus_five["minimum_bid_before_collapse"]["p50"] == 0.05
    assert plus_five["intermediate_bid_rate"]["count"] == 1
    gap = next(row for row in result["records"] if row["market_slug"].endswith("72-73f"))
    assert gap["outcomes"]["0.05"]["outcome"] == "zero_gap"
    assert gap["outcomes"]["0.05"]["target_top_bid_reached"] is False


def test_incomplete_200_entry_is_excluded_and_loader_keeps_source_and_receipt(tmp_path) -> None:
    pairs = [
        _pair(market="74-75f", minute=0, ask_size="100"),
        _pair(market="74-75f", minute=20, ask_size="100"),
    ]
    result = analyze_price_paths(
        pairs,
        event_metadata=METADATA,
        observations_by_station={},
        typical_peak_minutes_by_station={},
    )
    assert result["candidate_entry_count"] == 2
    assert result["excluded_incomplete_entry_count"] == 2
    assert result["usable_entry_count"] == 0

    archive = tmp_path / "events.jsonl"
    archive.write_text(
        json.dumps(
            {
                "product": "wrh_timeseries_observation",
                "station_id": "KLAX",
                "source_timestamp_ms": int(BASE.timestamp() * 1000),
                "received_at": (BASE + timedelta(minutes=1)).isoformat(),
                "temperature_c": 20,
                "raw": {"temperature_f": "68.0"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "product": "latest_observation",
                "station_id": "KLAX",
                "source_timestamp_ms": int(BASE.timestamp() * 1000),
                "received_at": BASE.isoformat(),
                "temperature_c": 20,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    observations = load_archived_weather_observations([archive])
    assert len(observations["KLAX"]) == 1
    assert observations["KLAX"][0].observed_at == BASE
    assert observations["KLAX"][0].available_at == BASE + timedelta(minutes=1)
    assert observations["KLAX"][0].temperature_f == Decimal("68.0")
