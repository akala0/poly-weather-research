from datetime import date, datetime

from poly_weather.intraday_reversal import TemperatureObservation
from poly_weather.no_side_analysis import audit_no_proxy_distortion


def test_no_proxy_audit_is_strictly_no_lookahead_and_measures_unchanged_age() -> None:
    catalog = {
        "events": [
            {
                "event_slug": "event",
                "station_id": "KTEST",
                "timezone": "UTC",
                "target_date": date(2026, 8, 24).isoformat(),
            }
        ]
    }
    histories = {
        "event": {
            "markets": [
                {
                    "market_slug": "event-70-71f",
                    "upper_f": 71,
                    "history": [
                    {"t": 1787558400, "p": 0.20},  # 08:00 UTC
                        {"t": 1787562000, "p": 0.10},  # 09:00 UTC
                    ],
                }
            ]
        }
    }
    observations = {
        "KTEST": [
            TemperatureObservation("KTEST", datetime(2026, 8, 24, 7, 0), 70.0),
            TemperatureObservation("KTEST", datetime(2026, 8, 24, 8, 30), 80.0),
            TemperatureObservation("KTEST", datetime(2026, 8, 24, 10, 0), 99.0),
        ]
    }

    result = audit_no_proxy_distortion(
        catalog,
        histories_by_event=histories,
        observations_by_station=observations,
    )

    assert result["aligned_point_count"] == 2
    assert result["contradiction_point_count"] == 1
    case = result["cases"][0]
    assert case["observed_high_f"] == 80.0
    assert case["physical_margin_f"] == 9.0
    assert case["p_unchanged_age_minutes"] == 60.0
