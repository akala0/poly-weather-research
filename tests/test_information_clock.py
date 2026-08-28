from datetime import UTC, datetime, timedelta

from poly_weather.information_clock import (
    InformationClock,
    InformationEvent,
    deduplicate_information_events,
    iter_information_events,
    strict_information_cutoff,
)

BASE = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def test_duplicate_payload_is_dropped_but_same_time_revision_is_retained() -> None:
    rows = [
        {
            "run_id": "run-a",
            "sequence": 1,
            "received_at": (BASE + timedelta(minutes=1)).isoformat(),
            "source_timestamp_ms": int(BASE.timestamp() * 1000),
            "provider": "WRH",
            "product": "wrh_timeseries_observation",
            "station_id": "KLAX",
            "raw": {"temperature_f": "80.0"},
        },
        {
            "run_id": "run-b",
            "sequence": 2,
            "received_at": (BASE + timedelta(minutes=2)).isoformat(),
            "source_timestamp_ms": int(BASE.timestamp() * 1000),
            "provider": "WRH",
            "product": "wrh_timeseries_observation",
            "station_id": "KLAX",
            "raw": {"temperature_f": "80.0"},
        },
        {
            "run_id": "run-c",
            "sequence": 3,
            "received_at": (BASE + timedelta(minutes=3)).isoformat(),
            "source_timestamp_ms": int(BASE.timestamp() * 1000),
            "provider": "WRH",
            "product": "wrh_timeseries_observation",
            "station_id": "KLAX",
            "raw": {"temperature_f": "81.0"},
        },
    ]

    events = tuple(iter_information_events(rows, market_day_by_station={"KLAX": "2026-08-28"}))
    assert len(deduplicate_information_events(events)) == 2
    clock = InformationClock()
    accepted = clock.ingest_many(events)
    assert len(accepted) == 2
    assert accepted[0].source_at == accepted[1].source_at
    assert accepted[0].payload_hash != accepted[1].payload_hash


def test_speci_is_a_distinct_external_information_kind() -> None:
    rows = [
        {
            "source": "noaa_aviation_metar",
            "fetched_at": (BASE + timedelta(minutes=1)).isoformat(),
            "payload": [
                {
                    "icaoId": "KLGA",
                    "obsTime": BASE.timestamp(),
                    "receiptTime": (BASE + timedelta(seconds=30)).isoformat(),
                    "metarType": "SPECI",
                    "rawOb": "SPECI KLGA 280000Z 00000KT 10SM CLR 22/15 A2992",
                }
            ],
        }
    ]
    events = tuple(iter_information_events(rows, market_day_by_station={"KLGA": "2026-08-28"}))
    assert len(events) == 1
    assert events[0].kind == "speci"
    assert events[0].phase_eligible is True


def test_source_and_receipt_cutoff_is_applied_on_both_clocks() -> None:
    events = (
        InformationEvent(
            event_id="eligible",
            source="WRH",
            kind="observation",
            source_at=BASE,
            available_at=BASE + timedelta(minutes=1),
            station_id="KLAX",
            market_day="2026-08-28",
        ),
        InformationEvent(
            event_id="future-source",
            source="WRH",
            kind="observation",
            source_at=BASE + timedelta(minutes=3),
            available_at=BASE + timedelta(minutes=3),
            station_id="KLAX",
            market_day="2026-08-28",
        ),
        InformationEvent(
            event_id="future-receipt",
            source="WRH",
            kind="observation",
            source_at=BASE,
            available_at=BASE + timedelta(minutes=4),
            station_id="KLAX",
            market_day="2026-08-28",
        ),
    )
    cutoff = strict_information_cutoff(events, BASE + timedelta(minutes=2))
    assert [event.event_id for event in cutoff] == ["eligible"]


def test_missing_model_run_initialization_is_not_phase_eligible() -> None:
    row = {
        "source": "open_meteo_gefs",
        "fetched_at": (BASE + timedelta(minutes=1)).isoformat(),
        "payload": {"hourly": {"temperature_2m": [20.0]}},
    }
    event = next(iter_information_events([row]))
    assert event.kind == "model_run"
    assert event.phase_eligible is False
    clock = InformationClock()
    assert clock.ingest(event) is None
    assert clock.as_dict()["invalid_event_count"] == 1


def test_settlement_payload_context_is_recovered_without_inventing_receipt() -> None:
    row = {
        "source": "settlement_evidence",
        "fetched_at": (BASE + timedelta(minutes=1)).isoformat(),
        "payload": {
            "evidence": {
                "station_id": "KLAX",
                "target_date": "2026-08-28",
                "finalization_rule": "first_next_day_observation",
            }
        },
    }
    event = next(iter(iter_information_events([row])))
    assert event.station_id == "KLAX"
    assert event.market_day == "2026-08-28"
    assert event.phase_eligible is True
