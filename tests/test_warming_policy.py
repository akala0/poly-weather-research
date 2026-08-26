from datetime import datetime

from poly_weather.warming_policy import WarmingThresholdRegistry


def _registry(*, seasons: list[dict] | None = None) -> WarmingThresholdRegistry:
    return WarmingThresholdRegistry.from_dict(
        {
            "policy_version": "policy-v1",
            "recommended_profile": "conservative",
            "stations": {
                "KLAX": {
                    "seasons": seasons
                    or [
                        {
                            "season_id": "heat_2026",
                            "threshold_version": "heat-v1",
                            "enabled": True,
                            "window_start": "2026-07-01",
                            "window_end": "2026-10-31",
                            "typical_peak_minutes": 12 * 60 + 20,
                            "profiles": {
                                "conservative": [
                                    {
                                        "time_bin": "2-4h",
                                        "max_hours_to_peak": 4.0,
                                        "required_abs_margin_f": 7.0,
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        }
    )


def test_season_boundaries_are_inclusive_and_outside_is_fail_closed() -> None:
    registry = _registry()
    for at in (
        datetime(2026, 7, 1, 9),
        datetime(2026, 10, 31, 9),
    ):
        decision = registry.decision(
            station_id="KLAX", local_at=at, physical_margin_f=-7.0
        )
        assert decision.season_id == "heat_2026"
        assert decision.physical_margin_passed is True

    for at in (
        datetime(2026, 6, 30, 9),
        datetime(2026, 11, 1, 9),
    ):
        decision = registry.decision(
            station_id="KLAX", local_at=at, physical_margin_f=-20.0
        )
        assert decision.enabled is False
        assert decision.physical_margin_passed is False
        assert decision.reason == "outside calibrated season window for station"


def test_joint_rule_changes_required_margin_with_time_to_peak() -> None:
    seasons = _registry().stations["KLAX"]["seasons"]
    seasons[0]["profiles"]["conservative"] = [
        {
            "time_bin": "0-1h",
            "max_hours_to_peak": 1.0,
            "required_abs_margin_f": 3.0,
        },
        {
            "time_bin": "2-4h",
            "max_hours_to_peak": 4.0,
            "required_abs_margin_f": 7.0,
        },
    ]
    registry = _registry(seasons=seasons)
    near_peak = registry.decision(
        station_id="KLAX",
        local_at=datetime(2026, 8, 1, 12),
        physical_margin_f=-3.0,
    )
    far_from_peak = registry.decision(
        station_id="KLAX",
        local_at=datetime(2026, 8, 1, 9),
        physical_margin_f=-3.0,
    )
    assert near_peak.physical_margin_passed is True
    assert far_from_peak.required_margin_f == 7.0
    assert far_from_peak.physical_margin_passed is False


def test_overlapping_seasons_fail_closed_instead_of_borrowing_threshold() -> None:
    base = _registry().stations["KLAX"]["seasons"][0]
    overlapping = dict(base)
    overlapping["season_id"] = "transition_2026"
    overlapping["window_start"] = "2026-10-15"
    overlapping["window_end"] = "2026-11-15"
    registry = _registry(seasons=[base, overlapping])

    decision = registry.decision(
        station_id="KLAX",
        local_at=datetime(2026, 10, 20, 9),
        physical_margin_f=-20.0,
    )
    assert decision.enabled is False
    assert decision.physical_margin_passed is False
    assert decision.reason == "overlapping calibrated season windows for station"


def test_disabled_low_resolution_station_cannot_trigger() -> None:
    season = dict(_registry().stations["KLAX"]["seasons"][0])
    season["enabled"] = False
    season["disabled_reason"] = "insufficient intraday source precision"
    registry = WarmingThresholdRegistry.from_dict(
        {
            "policy_version": "policy-v1",
            "recommended_profile": "conservative",
            "stations": {"ZUCK": {"seasons": [season]}},
        }
    )
    decision = registry.decision(
        station_id="ZUCK",
        local_at=datetime(2026, 8, 1, 9),
        physical_margin_f=-20.0,
    )
    assert decision.physical_margin_passed is False
    assert decision.reason == "insufficient intraday source precision"
