from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from poly_weather.domain import AviationWeatherSnapshot, MetarReport, TafReport


def _epoch(value: object) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


class AviationWeatherClient:
    """Read-only METAR and TAF adapter for NOAA Aviation Weather Center."""

    def __init__(
        self,
        *,
        base_url: str = "https://aviationweather.gov",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AviationWeatherClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _json_list(response: httpx.Response, *, product: str) -> list[dict[str, Any]]:
        if response.status_code == 204:
            return []
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError(f"Aviation Weather {product} response is not a list of objects")
        return payload

    @staticmethod
    def _parse_metars(
        rows: list[dict[str, Any]], *, station_id: str
    ) -> tuple[MetarReport, ...]:
        return tuple(
            sorted(
                (
                    MetarReport(
                        station_id=str(row.get("icaoId") or station_id).upper(),
                        observed_at=_epoch(row["obsTime"]),
                        temperature_c=(
                            None if row.get("temp") is None else Decimal(str(row["temp"]))
                        ),
                        dewpoint_c=(
                            None if row.get("dewp") is None else Decimal(str(row["dewp"]))
                        ),
                        raw_text=str(row.get("rawOb") or ""),
                        flight_category=row.get("fltCat"),
                        raw=row,
                    )
                    for row in rows
                    if row.get("obsTime") is not None
                ),
                key=lambda report: report.observed_at,
            )
        )

    @staticmethod
    def _parse_tafs(
        rows: list[dict[str, Any]], *, station_id: str
    ) -> tuple[TafReport, ...]:
        return tuple(
            sorted(
                (
                    TafReport(
                        station_id=str(row.get("icaoId") or station_id).upper(),
                        issued_at=datetime.fromisoformat(
                            str(row["issueTime"]).replace("Z", "+00:00")
                        ),
                        valid_from=_epoch(row["validTimeFrom"]),
                        valid_to=_epoch(row["validTimeTo"]),
                        raw_text=str(row.get("rawTAF") or ""),
                        periods=tuple(
                            period for period in (row.get("fcsts") or []) if isinstance(period, dict)
                        ),
                        raw=row,
                    )
                    for row in rows
                    if row.get("issueTime")
                    and row.get("validTimeFrom") is not None
                    and row.get("validTimeTo") is not None
                ),
                key=lambda report: report.issued_at,
            )
        )

    def metars(
        self, *, station_id: str, hours: int = 6
    ) -> tuple[str, tuple[MetarReport, ...], list[dict[str, Any]]]:
        if not 1 <= hours <= 360:
            raise ValueError("hours must be between 1 and 360")
        normalized = station_id.strip().upper()
        response = self._client.get(
            "/api/data/metar",
            params={"ids": normalized, "format": "json", "hours": hours},
        )
        rows = self._json_list(response, product="METAR")
        return str(response.request.url), self._parse_metars(rows, station_id=normalized), rows

    def tafs(
        self, *, station_id: str
    ) -> tuple[str, tuple[TafReport, ...], list[dict[str, Any]]]:
        normalized = station_id.strip().upper()
        response = self._client.get(
            "/api/data/taf",
            params={"ids": normalized, "format": "json"},
        )
        rows = self._json_list(response, product="TAF")
        return str(response.request.url), self._parse_tafs(rows, station_id=normalized), rows

    def snapshot(
        self,
        *,
        station_id: str,
        metar_hours: int = 6,
    ) -> tuple[str, str, AviationWeatherSnapshot]:
        normalized = station_id.strip().upper()
        metar_url, metars, metar_raw = self.metars(station_id=normalized, hours=metar_hours)
        taf_url, tafs, taf_raw = self.tafs(station_id=normalized)
        return (
            metar_url,
            taf_url,
            AviationWeatherSnapshot(
                source="NOAA Aviation Weather Center",
                station_id=normalized,
                fetched_at=datetime.now(UTC),
                metars=metars,
                tafs=tafs,
                metar_raw=metar_raw,
                taf_raw=taf_raw,
            ),
        )
