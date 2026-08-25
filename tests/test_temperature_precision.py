from __future__ import annotations

from decimal import Decimal

import httpx

from poly_weather.adapters.aviation_weather import parse_metar_report
from poly_weather.adapters.nws import NwsClient
from poly_weather.temperature import celsius_to_fahrenheit, round_whole_degree


def test_celsius_conversion_rounds_only_after_conversion_at_boundaries() -> None:
    assert celsius_to_fahrenheit(Decimal("25.5")) == Decimal("77.9")
    assert round_whole_degree(celsius_to_fahrenheit(Decimal("25.5"))) == Decimal("78")
    assert celsius_to_fahrenheit(Decimal("25.55")) == Decimal("77.990")
    assert round_whole_degree(celsius_to_fahrenheit(Decimal("25.55"))) == Decimal("78")
    assert celsius_to_fahrenheit(Decimal("-0.5")) == Decimal("31.1")
    assert round_whole_degree(celsius_to_fahrenheit(Decimal("-0.5"))) == Decimal("31")


def test_metar_prefers_tenths_t_group_over_integer_body() -> None:
    report = parse_metar_report(
        {
            "icaoId": "KLGA",
            "obsTime": 1_787_385_600,
            "temp": 26,
            "dewp": 15,
            "rawOb": "METAR KLGA 221200Z 18005KT 10SM CLR 26/15 A2992 RMK AO2 T02550150",
        },
        station_id="KLGA",
    )
    assert report.temperature_c == Decimal("25.5")
    assert report.dewpoint_c == Decimal("15")
    assert report.temperature_source == "metar_remarks_t_group"
    assert report.temperature_precision_degraded is False


def test_metar_marks_precision_degraded_when_t_group_is_missing() -> None:
    report = parse_metar_report(
        {
            "icaoId": "ZUCK",
            "obsTime": 1_787_385_600,
            "temp": 26,
            "dewp": 15,
            "rawOb": "METAR ZUCK 221200Z 18003MPS CAVOK 26/15 Q1012 NOSIG",
        },
        station_id="ZUCK",
    )
    assert report.temperature_c == Decimal("26")
    assert report.temperature_source == "metar_body_or_api_whole_degree"
    assert report.temperature_precision_degraded is True
    assert report.raw["temperature_precision_degraded"] is True


def test_nws_preserves_decimal_temperature_precision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "properties": {
                    "timestamp": "2026-08-24T12:00:00Z",
                    "temperature": {"value": 25.55, "unitCode": "wmoUnit:degC"},
                }
            },
        )

    with httpx.Client(
        base_url="https://api.weather.gov", transport=httpx.MockTransport(handler)
    ) as client:
        with NwsClient(client=client) as nws:
            observation = nws.latest_observation("KLGA")
    assert observation.temperature_c == Decimal("25.55")
    assert observation.temperature_precision_degraded is False
