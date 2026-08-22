from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from poly_weather.domain import (
    MarketQuote,
    MetarReport,
    MonitorSnapshot,
    MonitorStatus,
    NwsObservation,
    TafReport,
)


class MonitorThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_nws_age_minutes: float = Field(default=30, gt=0)
    max_metar_age_minutes: float = Field(default=70, gt=0)
    max_source_delta_f: Decimal = Field(default=Decimal("2"), ge=0)
    max_market_spread: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)


def _fahrenheit(value_c: Decimal | None) -> Decimal | None:
    if value_c is None:
        return None
    return value_c * Decimal(9) / Decimal(5) + Decimal(32)


def _taf_summary(taf: TafReport | None) -> tuple[bool, bool, int | None]:
    if taf is None:
        return False, False, None
    weather_strings = [
        str(period.get("wxString") or "")
        for period in taf.periods
        if period.get("wxString")
    ]
    ceiling_bases = [
        int(cloud["base"])
        for period in taf.periods
        for cloud in (period.get("clouds") or [])
        if isinstance(cloud, dict)
        and cloud.get("cover") in {"BKN", "OVC", "VV"}
        and cloud.get("base") is not None
    ]
    precipitation = any(
        token in weather for weather in weather_strings for token in ("RA", "SN", "DZ")
    )
    thunderstorm = any("TS" in weather for weather in weather_strings)
    return precipitation, thunderstorm, min(ceiling_bases) if ceiling_bases else None


def build_monitor_snapshot(
    *,
    captured_at: datetime,
    station_id: str,
    event_id: str,
    event_slug: str,
    signal_contract_verified: bool,
    nws: NwsObservation | None,
    metars: tuple[MetarReport, ...],
    tafs: tuple[TafReport, ...],
    quotes: tuple[MarketQuote, ...],
    token_market_slugs: dict[str, str],
    thresholds: MonitorThresholds,
    collection_errors: dict[str, str],
    raw_paths: dict[str, str],
) -> MonitorSnapshot:
    captured_utc = captured_at.astimezone(UTC)
    latest_metar = metars[-1] if metars else None
    latest_taf = tafs[-1] if tafs else None
    nws_temperature_f = _fahrenheit(nws.temperature_c) if nws else None
    metar_temperature_f = _fahrenheit(latest_metar.temperature_c) if latest_metar else None
    nws_age = (
        (captured_utc - nws.timestamp.astimezone(UTC)).total_seconds() / 60 if nws else None
    )
    metar_age = (
        (captured_utc - latest_metar.observed_at.astimezone(UTC)).total_seconds() / 60
        if latest_metar
        else None
    )
    source_delta = (
        abs(nws_temperature_f - metar_temperature_f)
        if nws_temperature_f is not None and metar_temperature_f is not None
        else None
    )
    precipitation, thunderstorm, min_ceiling = _taf_summary(latest_taf)

    stale_reasons: list[str] = []
    warning_reasons: list[str] = []
    if "nws" in collection_errors:
        stale_reasons.append("NWS collection failed")
    if "clob" in collection_errors:
        stale_reasons.append("CLOB collection failed")
    if "metar" in collection_errors:
        warning_reasons.append("METAR collection failed")
    if "taf" in collection_errors:
        warning_reasons.append("TAF collection failed")
    if not signal_contract_verified:
        stale_reasons.append("signal contract mapping is not verified")
    if nws is None or nws_temperature_f is None or nws_age is None:
        stale_reasons.append("NWS observation is unavailable")
    elif nws_age < 0:
        stale_reasons.append("NWS observation timestamp is in the future")
    elif nws_age > thresholds.max_nws_age_minutes:
        stale_reasons.append("NWS observation is stale")
    if latest_metar is None or metar_temperature_f is None or metar_age is None:
        warning_reasons.append("METAR cross-check is unavailable")
    elif metar_age < 0:
        warning_reasons.append("METAR timestamp is in the future")
    elif metar_age > thresholds.max_metar_age_minutes:
        warning_reasons.append("METAR cross-check is stale")
    if source_delta is not None and source_delta > thresholds.max_source_delta_f:
        warning_reasons.append("NWS and METAR temperatures disagree")
    if latest_taf is None:
        warning_reasons.append("TAF risk context is unavailable")
    elif latest_taf.valid_to.astimezone(UTC) < captured_utc:
        warning_reasons.append("TAF risk context has expired")
    if not quotes:
        stale_reasons.append("CLOB quotes are unavailable")
    else:
        incomplete = [quote for quote in quotes if quote.best_bid is None or quote.best_ask is None]
        if incomplete:
            warning_reasons.append("one or more CLOB books have no two-sided quote")
        wide = [
            quote
            for quote in quotes
            if quote.spread is not None and quote.spread > thresholds.max_market_spread
        ]
        if wide:
            warning_reasons.append("one or more CLOB spreads exceed the limit")

    if stale_reasons:
        status = MonitorStatus.STALE
    elif warning_reasons:
        status = MonitorStatus.WARNING
    else:
        status = MonitorStatus.HEALTHY
    reasons = tuple(stale_reasons + warning_reasons) or ("all monitor health gates passed",)
    return MonitorSnapshot(
        captured_at=captured_utc,
        station_id=station_id,
        event_id=event_id,
        event_slug=event_slug,
        signal_contract_verified=signal_contract_verified,
        nws_observed_at=nws.timestamp if nws else None,
        nws_temperature_f=nws_temperature_f,
        nws_age_minutes=nws_age,
        metar_observed_at=latest_metar.observed_at if latest_metar else None,
        metar_temperature_f=metar_temperature_f,
        metar_age_minutes=metar_age,
        source_delta_f=source_delta,
        taf_issued_at=latest_taf.issued_at if latest_taf else None,
        taf_valid_to=latest_taf.valid_to if latest_taf else None,
        taf_has_precipitation=precipitation,
        taf_has_thunderstorm=thunderstorm,
        taf_min_ceiling_ft=min_ceiling,
        token_market_slugs=token_market_slugs,
        quotes=quotes,
        status=status,
        reasons=reasons,
        collection_errors=collection_errors,
        raw_paths=raw_paths,
    )
