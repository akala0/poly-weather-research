from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
import typer
from pydantic import ValidationError

from poly_weather.adapters.aviation_weather import AviationWeatherClient
from poly_weather.adapters.clob import ClobClient
from poly_weather.adapters.historical_weather import (
    NceiDailySummariesClient,
    OpenMeteoPreviousRunsClient,
)
from poly_weather.adapters.nws import NwsClient
from poly_weather.adapters.open_meteo import OpenMeteoEnsembleClient
from poly_weather.adapters.polymarket import GammaClient, is_weather_market
from poly_weather.calibration import fit_bias_calibration, rolling_origin_evaluate
from poly_weather.config import load_settlement_registry
from poly_weather.domain import CalibrationSample, TruthKind, VerificationStatus
from poly_weather.market_stream import MarketWebSocketBot
from poly_weather.modeling import build_bucket_forecast
from poly_weather.monitoring import MonitorThresholds, build_monitor_snapshot
from poly_weather.paper import PaperPolicy, make_paper_decision
from poly_weather.research_store import ResearchWarehouse
from poly_weather.settlement import (
    parse_settlement_evidence,
    verify_settlement_evidence,
    verify_signal_contract,
)
from poly_weather.signal_engine import (
    LiveSignalConfig,
    LiveSignalEngine,
    build_live_calibration,
)
from poly_weather.storage import CatalogStore, RawEventArchive
from poly_weather.weather_stream import WeatherDaemon, WeatherStation

app = typer.Typer(
    no_args_is_help=True,
    help="Research, replay, and non-executable paper decisions for Polymarket weather markets.",
)

DEFAULT_CONFIG = Path("configs/settlements.example.json")
DEFAULT_DATA_DIR = Path("data")


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _event_by_slug_with_retry(
    gamma: GammaClient,
    event_slug: str,
    *,
    attempts: int = 5,
) -> object:
    for attempt in range(attempts):
        try:
            return gamma.event_by_slug(event_slug)
        except httpx.HTTPError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(4.0, 0.5 * (2**attempt)))
    raise RuntimeError("unreachable event retry state")


def _parse_aware_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter(f"{label} must include a UTC offset or Z")
    return parsed


def _interruptible_sleep(seconds: float) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0:
        chunk = min(30.0, remaining)
        time.sleep(chunk)
        remaining -= chunk


@app.command("validate-settlements")
def validate_settlements(
    config: Annotated[Path, typer.Option("--config", help="Settlement registry JSON file.")] = DEFAULT_CONFIG,
) -> None:
    """Validate schema and report which entries are allowed downstream."""
    try:
        registry = load_settlement_registry(config)
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"Invalid settlement registry: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    verified = [spec.key for spec in registry.specs if spec.status is VerificationStatus.VERIFIED]
    _emit(
        {
            "schema_version": registry.schema_version,
            "total": len(registry.specs),
            "verified": verified,
            "unverified": [
                spec.key for spec in registry.specs if spec.status is VerificationStatus.UNVERIFIED
            ],
            "disabled": [
                spec.key for spec in registry.specs if spec.status is VerificationStatus.DISABLED
            ],
            "tradeable_count": len(verified),
        }
    )


@app.command("discover-markets")
def discover_markets(
    pages: Annotated[int, typer.Option(min=1, max=100, help="Maximum pages to fetch.")] = 1,
    page_size: Annotated[int, typer.Option(min=1, max=50, help="Events per search page.")] = 20,
    query: Annotated[str, typer.Option(help="Gamma public-search query.")] = "highest temperature",
    data_dir: Annotated[Path, typer.Option(help="Archive and catalog directory.")] = DEFAULT_DATA_DIR,
) -> None:
    """Search public Gamma events, archive responses, and retain weather markets."""
    archive = RawEventArchive(data_dir / "raw")
    candidate_total = 0
    fetched_total = 0
    page_total = 0
    with GammaClient() as client, CatalogStore(data_dir / "catalog.sqlite3") as catalog:
        for page in client.iter_search_pages(query=query, pages=pages, page_size=page_size):
            page_total += 1
            fetched_total += len(page.markets)
            candidates = tuple(market for market in page.markets if is_weather_market(market))
            candidate_total += len(candidates)
            raw_path = archive.append(
                source="polymarket_gamma_search",
                fetched_at=page.fetched_at,
                request_url=page.request_url,
                payload=page.raw_payload,
            )
            catalog.upsert_markets(candidates, observed_at=page.fetched_at)
            catalog.record_page(
                source="polymarket_gamma_search",
                fetched_at=page.fetched_at,
                request_url=page.request_url,
                item_count=len(page.markets),
                candidate_count=len(candidates),
                raw_path=raw_path,
            )
        catalog_total = catalog.market_count()
    _emit(
        {
            "mode": "read_only",
            "query": query,
            "pages_fetched": page_total,
            "nested_markets_fetched": fetched_total,
            "weather_candidates_seen": candidate_total,
            "weather_markets_in_catalog": catalog_total,
            "data_dir": str(data_dir.resolve()),
        }
    )


@app.command("nws-latest")
def nws_latest(
    station_id: Annotated[str, typer.Argument(help="Four-letter NWS station identifier.")],
    data_dir: Annotated[Path, typer.Option(help="Archive directory.")] = DEFAULT_DATA_DIR,
) -> None:
    """Fetch and archive the latest public NWS station observation."""
    fetched_at = datetime.now(UTC)
    with NwsClient() as client:
        observation = client.latest_observation(station_id)
    raw_path = RawEventArchive(data_dir / "raw").append(
        source="nws_latest_observation",
        fetched_at=fetched_at,
        request_url=f"https://api.weather.gov/stations/{observation.station_id}/observations/latest",
        payload=observation.raw,
    )
    _emit(
        {
            "mode": "read_only",
            "station_id": observation.station_id,
            "timestamp": observation.timestamp,
            "temperature_c": observation.temperature_c,
            "raw_path": str(raw_path.resolve()),
        }
    )


@app.command("aviation-snapshot")
def aviation_snapshot(
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    metar_hours: Annotated[int, typer.Option("--metar-hours", min=1, max=360)] = 6,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Archive NOAA Aviation Weather METARs and the current TAF for one airport."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if not spec.station_id:
        typer.echo("Registry entry needs an ICAO station_id.", err=True)
        raise typer.Exit(code=2)
    with AviationWeatherClient() as client:
        metar_url, taf_url, snapshot = client.snapshot(
            station_id=spec.station_id,
            metar_hours=metar_hours,
        )
    archive = RawEventArchive(data_dir / "raw")
    metar_path = archive.append(
        source="noaa_aviation_metar",
        fetched_at=snapshot.fetched_at,
        request_url=metar_url,
        payload=snapshot.metar_raw,
    )
    taf_path = archive.append(
        source="noaa_aviation_taf",
        fetched_at=snapshot.fetched_at,
        request_url=taf_url,
        payload=snapshot.taf_raw,
    )
    latest_metar = snapshot.metars[-1] if snapshot.metars else None
    latest_taf = snapshot.tafs[-1] if snapshot.tafs else None
    periods = latest_taf.periods if latest_taf else ()
    weather_strings = [
        str(period.get("wxString") or "") for period in periods if period.get("wxString")
    ]
    ceiling_bases = [
        int(cloud["base"])
        for period in periods
        for cloud in (period.get("clouds") or [])
        if isinstance(cloud, dict)
        and cloud.get("cover") in {"BKN", "OVC", "VV"}
        and cloud.get("base") is not None
    ]
    taf_temperature_entries = sum(
        len(period.get("temp") or []) for period in periods if isinstance(period, dict)
    )
    latest_temperature_f = None
    if latest_metar and latest_metar.temperature_c is not None:
        latest_temperature_f = (
            latest_metar.temperature_c * Decimal(9) / Decimal(5) + Decimal(32)
        )
    _emit(
        {
            "source": snapshot.source,
            "station_id": snapshot.station_id,
            "fetched_at": snapshot.fetched_at,
            "metar_count": len(snapshot.metars),
            "latest_metar_at": latest_metar.observed_at if latest_metar else None,
            "latest_metar_temperature_f": latest_temperature_f,
            "latest_metar_flight_category": latest_metar.flight_category if latest_metar else None,
            "latest_metar_raw": latest_metar.raw_text if latest_metar else None,
            "taf_count": len(snapshot.tafs),
            "latest_taf_issued_at": latest_taf.issued_at if latest_taf else None,
            "latest_taf_valid_from": latest_taf.valid_from if latest_taf else None,
            "latest_taf_valid_to": latest_taf.valid_to if latest_taf else None,
            "taf_has_precipitation": any(
                token in weather for weather in weather_strings for token in ("RA", "SN", "DZ")
            ),
            "taf_has_thunderstorm": any("TS" in weather for weather in weather_strings),
            "taf_min_ceiling_ft": min(ceiling_bases) if ceiling_bases else None,
            "taf_has_explicit_temperature_guidance": taf_temperature_entries > 0,
            "signal_role": {
                "metar": "same-station observation cross-check",
                "taf": "qualitative short-horizon risk feature; not a daily-high forecast",
            },
            "metar_raw_path": str(metar_path.resolve()),
            "taf_raw_path": str(taf_path.resolve()),
        }
    )


@app.command("forecast-buckets")
def forecast_buckets(
    event_slug: Annotated[str, typer.Argument(help="Exact Polymarket event slug.")],
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    target_date_text: Annotated[str, typer.Argument(help="Local settlement date (YYYY-MM-DD).")],
    config: Annotated[Path, typer.Option("--config", help="Settlement registry JSON file.")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option(help="Archive directory.")] = DEFAULT_DATA_DIR,
    pseudocount_text: Annotated[str, typer.Option("--pseudocount", help="Non-negative Dirichlet smoothing per bucket.")] = "0.5",
    calibration_lead_days: Annotated[int, typer.Option(min=0, max=7)] = 1,
    min_calibration_samples: Annotated[int, typer.Option(min=2)] = 30,
) -> None:
    """Build a research-only GEFS probability distribution for an event's buckets."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if spec.latitude is None or spec.longitude is None or spec.timezone is None:
        typer.echo("Settlement entry needs latitude, longitude, and timezone for forecasting.", err=True)
        raise typer.Exit(code=2)
    try:
        target_date = date.fromisoformat(target_date_text)
    except ValueError as exc:
        typer.echo("Target date must use YYYY-MM-DD format.", err=True)
        raise typer.Exit(code=2) from exc
    try:
        pseudocount = Decimal(pseudocount_text)
        if pseudocount < 0:
            raise ValueError
    except (ValueError, ArithmeticError) as exc:
        typer.echo("Pseudocount must be a non-negative decimal.", err=True)
        raise typer.Exit(code=2) from exc

    local_today = datetime.now(ZoneInfo(spec.timezone)).date()
    forecast_days = (target_date - local_today).days + 1
    if forecast_days < 1 or forecast_days > 10:
        typer.echo("Target date must be within the current 10-day GEFS ensemble window.", err=True)
        raise typer.Exit(code=2)

    archive = RawEventArchive(data_dir / "raw")
    with GammaClient() as gamma:
        event = gamma.event_by_slug(event_slug)
    event_raw_path = archive.append(
        source="polymarket_gamma_event",
        fetched_at=event.fetched_at,
        request_url=event.request_url,
        payload=event.raw_payload,
    )

    with OpenMeteoEnsembleClient() as weather:
        forecast_url, ensemble = weather.temperature_forecast(
            latitude=spec.latitude,
            longitude=spec.longitude,
            timezone=spec.timezone,
            forecast_days=forecast_days,
        )
    ensemble_raw_path = archive.append(
        source="open_meteo_gefs",
        fetched_at=ensemble.fetched_at,
        request_url=forecast_url,
        payload=ensemble.raw,
    )

    source_matches = (
        spec.resolution_source_url is not None
        and event.resolution_source == str(spec.resolution_source_url)
    )
    slug_matches = re.fullmatch(spec.market_slug_pattern, event.event_slug) is not None
    tradeable = (
        spec.status is VerificationStatus.VERIFIED and source_matches and slug_matches
    )
    if spec.status is not VerificationStatus.VERIFIED:
        reason = "settlement registry entry is unverified"
    elif not slug_matches:
        reason = "event slug does not exactly match the verified settlement rule"
    elif not source_matches:
        reason = "event resolution source differs from the verified settlement rule"
    else:
        reason = "verified settlement, slug, and resolution source"

    raw_member_highs = ensemble.daily_highs(target_date)
    calibration_applied = False
    calibration_sample_count = 0
    calibration_bias_f = None
    calibration_basis = None
    calibrated_member_highs = raw_member_highs
    warehouse_path = data_dir / "research.duckdb"
    if warehouse_path.exists() and spec.station_id:
        with ResearchWarehouse(warehouse_path) as warehouse:
            prior_samples = [
                sample
                for sample in warehouse.samples(
                    station_id=spec.station_id,
                    lead_days=calibration_lead_days,
                    model=ensemble.model,
                )
                if sample.target_date < target_date
            ]
        calibration_sample_count = len(prior_samples)
        if calibration_sample_count >= min_calibration_samples:
            fitted = fit_bias_calibration(prior_samples)
            calibration_bias_f = fitted.bias_f
            calibrated_member_highs = tuple(
                value + Decimal(str(fitted.bias_f)) for value in raw_member_highs
            )
            calibration_applied = True
            calibration_basis = "Open-Meteo Previous Runs + same-station NOAA NCEI daily truth"

    bucket_forecast = build_bucket_forecast(
        markets=event.markets,
        member_highs_f=calibrated_member_highs,
        target_date=target_date,
        ensemble_model=ensemble.model,
        pseudocount=pseudocount,
        calibration_applied=calibration_applied,
        calibration_sample_count=calibration_sample_count,
        calibration_bias_f=calibration_bias_f,
        calibration_basis=calibration_basis,
        tradeable=tradeable,
        tradeable_reason=reason,
    )
    forecast_payload = bucket_forecast.model_dump(mode="json")
    forecast_payload.update(
        {
            "event_id": event.event_id,
            "event_slug": event.event_slug,
            "event_title": event.title,
            "settlement_key": spec.key,
            "event_raw_path": str(event_raw_path.resolve()),
            "ensemble_raw_path": str(ensemble_raw_path.resolve()),
        }
    )
    snapshot_path = archive.append(
        source="bucket_forecast",
        fetched_at=bucket_forecast.generated_at,
        request_url=f"model://bucket-forecast/{event.event_slug}",
        payload=forecast_payload,
    )
    forecast_payload["snapshot_path"] = str(snapshot_path.resolve())
    _emit(forecast_payload)


@app.command("inspect-settlement")
def inspect_settlement(
    event_slug: Annotated[str, typer.Argument(help="Exact Polymarket event slug.")],
    settlement_key: Annotated[str, typer.Argument(help="Registry entry to compare against.")],
    config: Annotated[Path, typer.Option("--config", help="Settlement registry JSON file.")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option(help="Archive directory.")] = DEFAULT_DATA_DIR,
) -> None:
    """Parse resolution evidence and apply the fail-closed verification checklist."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    with GammaClient() as gamma:
        event = gamma.event_by_slug(event_slug)
    archive = RawEventArchive(data_dir / "raw")
    event_path = archive.append(
        source="polymarket_gamma_event",
        fetched_at=event.fetched_at,
        request_url=event.request_url,
        payload=event.raw_payload,
    )
    evidence = parse_settlement_evidence(event, registry_spec=spec)
    strict_verification = verify_settlement_evidence(evidence, spec)
    signal_verification = verify_signal_contract(evidence, spec)
    payload = {
        "evidence": evidence.model_dump(mode="json"),
        "verification": strict_verification.model_dump(mode="json"),
        "signal_contract_verification": signal_verification.model_dump(mode="json"),
        "event_raw_path": str(event_path.resolve()),
    }
    evidence_path = archive.append(
        source="settlement_evidence",
        fetched_at=datetime.now(UTC),
        request_url=f"model://settlement-evidence/{event.event_slug}",
        payload=payload,
    )
    payload["evidence_path"] = str(evidence_path.resolve())
    _emit(payload)


@app.command("backfill-calibration")
def backfill_calibration(
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    start_date_text: Annotated[str, typer.Argument(help="First target date (YYYY-MM-DD).")],
    end_date_text: Annotated[str, typer.Argument(help="Last target date (YYYY-MM-DD).")],
    lead_days: Annotated[int, typer.Option(min=0, max=7, help="Fixed forecast lead in days.")] = 1,
    model: Annotated[str, typer.Option(help="Open-Meteo model identifier.")] = "gfs_seamless",
    config: Annotated[Path, typer.Option("--config", help="Settlement registry JSON file.")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option(help="Research data directory.")] = DEFAULT_DATA_DIR,
) -> None:
    """Join fixed-lead historical forecasts to same-station NOAA daily truth."""
    try:
        start_date = date.fromisoformat(start_date_text)
        end_date = date.fromisoformat(end_date_text)
    except ValueError as exc:
        typer.echo("Dates must use YYYY-MM-DD format.", err=True)
        raise typer.Exit(code=2) from exc
    if end_date < start_date or (end_date - start_date).days > 366:
        typer.echo("Date range must be ordered and no longer than 367 days.", err=True)
        raise typer.Exit(code=2)
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if None in (spec.latitude, spec.longitude, spec.timezone) or not spec.ncei_station_id:
        typer.echo("Registry entry needs coordinates, timezone, and ncei_station_id.", err=True)
        raise typer.Exit(code=2)

    with OpenMeteoPreviousRunsClient() as forecast_client:
        forecast_url, forecasts = forecast_client.daily_highs(
            station_id=spec.station_id or settlement_key,
            latitude=spec.latitude,
            longitude=spec.longitude,
            timezone=spec.timezone,
            start_date=start_date,
            end_date=end_date,
            lead_days=lead_days,
            model=model,
        )
    with NceiDailySummariesClient() as truth_client:
        truth_url, truth = truth_client.daily_highs(
            station_id=spec.ncei_station_id,
            start_date=start_date,
            end_date=end_date,
        )

    archive = RawEventArchive(data_dir / "raw")
    forecast_path = archive.append(
        source="open_meteo_previous_runs",
        fetched_at=forecasts.fetched_at,
        request_url=forecast_url,
        payload=forecasts.raw,
    )
    truth_path = archive.append(
        source="noaa_ncei_daily_summaries",
        fetched_at=truth.fetched_at,
        request_url=truth_url,
        payload=truth.raw,
    )
    forecast_by_date = {point.local_date: point.value_f for point in forecasts.values}
    truth_by_date = {point.local_date: point.value_f for point in truth.values}
    joined_dates = sorted(set(forecast_by_date) & set(truth_by_date))
    ingested_at = datetime.now(UTC)
    samples = [
        CalibrationSample(
            station_id=spec.station_id or settlement_key,
            target_date=day,
            lead_days=lead_days,
            model=model,
            forecast_high_f=forecast_by_date[day],
            observed_high_f=truth_by_date[day],
            forecast_source=forecasts.source,
            truth_source=truth.source,
            truth_kind=TruthKind.NOAA_SAME_STATION_DAILY_FINAL.value,
            ingested_at=ingested_at,
        )
        for day in joined_dates
    ]
    with ResearchWarehouse(data_dir / "research.duckdb") as warehouse:
        written = warehouse.upsert_samples(samples)
        parquet_path = warehouse.export_samples_parquet(
            data_dir / "research" / "calibration_samples.parquet"
        )
        status = warehouse.status()
    _emit(
        {
            "settlement_key": settlement_key,
            "station_id": spec.station_id,
            "ncei_station_id": spec.ncei_station_id,
            "lead_days": lead_days,
            "model": model,
            "joined_samples": written,
            "forecast_only_dates": sorted(str(day) for day in set(forecast_by_date) - set(truth_by_date)),
            "truth_only_dates": sorted(str(day) for day in set(truth_by_date) - set(forecast_by_date)),
            "truth_kind": TruthKind.NOAA_SAME_STATION_DAILY_FINAL.value,
            "settlement_eligible": False,
            "forecast_raw_path": str(forecast_path.resolve()),
            "truth_raw_path": str(truth_path.resolve()),
            "parquet_path": str(parquet_path.resolve()),
            "warehouse": status,
        }
    )


@app.command("evaluate-calibration")
def evaluate_calibration(
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    lead_days: Annotated[int, typer.Option(min=0, max=7)] = 1,
    model: Annotated[str, typer.Option()] = "gfs_seamless",
    min_train_size: Annotated[int, typer.Option(min=2)] = 14,
    test_size: Annotated[int, typer.Option(min=1)] = 7,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Compare raw and bias-corrected forecasts using rolling-origin folds."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    station_id = spec.station_id or settlement_key
    with ResearchWarehouse(data_dir / "research.duckdb") as warehouse:
        samples = warehouse.samples(
            station_id=station_id,
            lead_days=lead_days,
            model=model,
        )
        try:
            evaluation = rolling_origin_evaluate(
                samples,
                min_train_size=min_train_size,
                test_size=test_size,
            )
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        run_id = warehouse.record_evaluation(evaluation)
    payload = evaluation.model_dump(mode="json")
    payload.update(
        {
            "run_id": run_id,
            "truth_kind": TruthKind.NOAA_SAME_STATION_DAILY_FINAL.value,
            "settlement_eligible": False,
        }
    )
    _emit(payload)


@app.command("noaa-daily-high")
def noaa_daily_high(
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    target_date_text: Annotated[str, typer.Argument(help="Local date (YYYY-MM-DD).")],
    as_of_text: Annotated[
        str | None,
        typer.Option("--as-of", help="Optional no-lookahead cutoff with UTC offset."),
    ] = None,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Compute a fast same-station daily high from NOAA/NWS observations."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
        target_date = date.fromisoformat(target_date_text)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if not spec.station_id or not spec.timezone:
        typer.echo("Registry entry needs station_id and timezone.", err=True)
        raise typer.Exit(code=2)
    as_of = _parse_aware_datetime(as_of_text, label="as-of") if as_of_text else None
    with NwsClient() as client:
        request_url, observed = client.daily_high(
            station_id=spec.station_id,
            target_date=target_date,
            timezone=spec.timezone,
            as_of=as_of,
        )
    raw_path = RawEventArchive(data_dir / "raw").append(
        source="noaa_nws_daily_high",
        fetched_at=observed.fetched_at,
        request_url=request_url,
        payload=observed.raw,
    )
    payload = observed.model_dump(mode="json")
    payload.update(
        {
            "whole_degree_value_f": observed.value_f.quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            ),
            "signal_source_policy": spec.signal_truth_policy.value,
            "waits_for_wunderground": False,
            "registry_signal_policy_enabled": spec.signal_truth_policy.value
            == "same_station_noaa",
            "raw_path": str(raw_path.resolve()),
        }
    )
    payload.pop("raw", None)
    _emit(payload)


@app.command("backfill-prices")
def backfill_prices(
    event_slug: Annotated[str, typer.Argument(help="Exact Polymarket event slug.")],
    start_text: Annotated[str, typer.Argument(help="ISO-8601 start with UTC offset.")],
    end_text: Annotated[str, typer.Argument(help="ISO-8601 end with UTC offset.")],
    fidelity_minutes: Annotated[int, typer.Option("--fidelity", min=1)] = 60,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Archive official CLOB Yes-token history for every binary event market."""
    start = _parse_aware_datetime(start_text, label="start")
    end = _parse_aware_datetime(end_text, label="end")
    if end <= start:
        typer.echo("End must be later than start.", err=True)
        raise typer.Exit(code=2)
    archive = RawEventArchive(data_dir / "raw")
    with GammaClient() as gamma:
        event = gamma.event_by_slug(event_slug)
    event_path = archive.append(
        source="polymarket_gamma_event",
        fetched_at=event.fetched_at,
        request_url=event.request_url,
        payload=event.raw_payload,
    )
    token_markets = {}
    for market in event.markets:
        if len(market.outcomes) != len(market.clob_token_ids):
            continue
        for outcome, token_id in zip(market.outcomes, market.clob_token_ids, strict=True):
            if outcome.casefold() == "yes":
                token_markets[token_id] = market
                break
    if not token_markets:
        typer.echo("Event has no aligned binary Yes-token identifiers.", err=True)
        raise typer.Exit(code=2)

    archive_paths = []
    point_count = 0
    empty_tokens = []
    token_ids = tuple(token_markets)
    with ClobClient() as clob, ResearchWarehouse(data_dir / "research.duckdb") as warehouse:
        for offset in range(0, len(token_ids), 20):
            batch = clob.batch_price_history(
                token_ids=token_ids[offset : offset + 20],
                start=start,
                end=end,
                fidelity_minutes=fidelity_minutes,
            )
            batch_path = archive.append(
                source="polymarket_clob_price_history",
                fetched_at=batch.fetched_at,
                request_url=batch.request_url,
                payload=batch.raw,
            )
            archive_paths.append(str(batch_path.resolve()))
            for series in batch.series:
                market = token_markets[series.token_id]
                if not series.points:
                    empty_tokens.append(series.token_id)
                point_count += warehouse.upsert_price_points(
                    event_id=event.event_id,
                    event_slug=event.event_slug,
                    market_id=market.market_id,
                    market_slug=market.slug,
                    outcome="Yes",
                    source=series.source,
                    points=series.points,
                    ingested_at=batch.fetched_at,
                )
        warehouse_status = warehouse.status()
    _emit(
        {
            "mode": "read_only_history",
            "event_id": event.event_id,
            "event_slug": event.event_slug,
            "yes_tokens": len(token_ids),
            "points_upserted": point_count,
            "empty_token_count": len(empty_tokens),
            "start": start,
            "end": end,
            "fidelity_minutes": fidelity_minutes,
            "no_lookahead_ready": True,
            "event_raw_path": str(event_path.resolve()),
            "price_raw_paths": archive_paths,
            "warehouse": warehouse_status,
        }
    )


@app.command("paper-decision")
def paper_decision(
    event_slug: Annotated[str, typer.Argument(help="Exact Polymarket event slug.")],
    market_slug: Annotated[str, typer.Argument(help="Exact binary market slug.")],
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    model_probability_text: Annotated[str, typer.Argument(help="Model probability for Yes.")],
    decision_time_text: Annotated[str, typer.Argument(help="ISO-8601 decision time.")],
    calibration_sample_count: Annotated[int, typer.Option(min=0)] = 0,
    min_net_edge_text: Annotated[str, typer.Option("--min-net-edge")] = "0.03",
    fee_bps: Annotated[int, typer.Option(min=0, max=10_000)] = 0,
    slippage_bps: Annotated[int, typer.Option(min=0, max=10_000)] = 50,
    max_price_age_minutes: Annotated[int, typer.Option(min=1)] = 90,
    max_notional_text: Annotated[str, typer.Option("--max-notional")] = "25",
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Create and persist a non-executable decision from a historical price point."""
    decision_time = _parse_aware_datetime(decision_time_text, label="decision-time")
    try:
        model_probability = Decimal(model_probability_text)
        policy = PaperPolicy(
            min_net_edge=Decimal(min_net_edge_text),
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            max_price_age=timedelta(minutes=max_price_age_minutes),
            max_notional_usd=Decimal(max_notional_text),
        )
    except (ArithmeticError, ValueError, ValidationError) as exc:
        typer.echo(f"Invalid paper policy or probability: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not Decimal(0) <= model_probability <= Decimal(1):
        typer.echo("Model probability must be between 0 and 1.", err=True)
        raise typer.Exit(code=2)

    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    with GammaClient() as gamma:
        event = gamma.event_by_slug(event_slug)
    market = next((item for item in event.markets if item.slug == market_slug), None)
    if market is None:
        typer.echo("Market slug is not part of the event.", err=True)
        raise typer.Exit(code=2)
    try:
        yes_index = next(
            index for index, outcome in enumerate(market.outcomes) if outcome.casefold() == "yes"
        )
        yes_token = market.clob_token_ids[yes_index]
    except (StopIteration, IndexError) as exc:
        typer.echo("Market has no aligned Yes token.", err=True)
        raise typer.Exit(code=2) from exc

    evidence = parse_settlement_evidence(event, registry_spec=spec)
    strict_verification = verify_settlement_evidence(evidence, spec)
    signal_verification = verify_signal_contract(evidence, spec)
    with ResearchWarehouse(data_dir / "research.duckdb") as warehouse:
        price_point = warehouse.price_at_or_before(
            token_id=yes_token,
            decision_time=decision_time,
            max_age=policy.max_price_age,
        )
        if price_point is None:
            typer.echo(
                "No non-future price exists inside the allowed age; run backfill-prices first.",
                err=True,
            )
            raise typer.Exit(code=2)
        decision = make_paper_decision(
            event_id=event.event_id,
            event_slug=event.event_slug,
            market_id=market.market_id,
            market_slug=market.slug,
            price_point=price_point,
            decision_time=decision_time,
            model_probability=model_probability,
            signal_contract_verified=signal_verification.tradeable,
            calibration_sample_count=calibration_sample_count,
            policy=policy,
        )
        decision_id = warehouse.record_paper_decision(decision)
    payload = decision.model_dump(mode="json")
    payload.update(
        {
            "decision_id": decision_id,
            "mode": "paper_only_no_order_path",
            "signal_source_policy": spec.signal_truth_policy.value,
            "signal_contract_verification": signal_verification.model_dump(mode="json"),
            "strict_resolution_verification": strict_verification.model_dump(mode="json"),
        }
    )
    _emit(payload)


@app.command("monitor")
def monitor(
    event_slug: Annotated[str, typer.Argument(help="Exact Polymarket event slug.")],
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    cycles: Annotated[
        int,
        typer.Option(min=0, help="Number of cycles; 0 runs until interrupted."),
    ] = 0,
    interval_seconds: Annotated[
        int,
        typer.Option("--interval", min=60, max=3600, help="Seconds between cycle starts."),
    ] = 60,
    taf_refresh_minutes: Annotated[
        int,
        typer.Option(min=10, max=120, help="TAF refresh cadence."),
    ] = 10,
    metar_hours: Annotated[int, typer.Option(min=1, max=24)] = 2,
    max_nws_age_minutes: Annotated[float, typer.Option(min=1)] = 30,
    max_metar_age_minutes: Annotated[float, typer.Option(min=1)] = 70,
    max_source_delta_f_text: Annotated[str, typer.Option("--max-source-delta-f")] = "2",
    max_market_spread_text: Annotated[str, typer.Option("--max-market-spread")] = "0.10",
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Continuously archive read-only NOAA weather and public CLOB quote health."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
        thresholds = MonitorThresholds(
            max_nws_age_minutes=max_nws_age_minutes,
            max_metar_age_minutes=max_metar_age_minutes,
            max_source_delta_f=Decimal(max_source_delta_f_text),
            max_market_spread=Decimal(max_market_spread_text),
        )
    except (KeyError, ArithmeticError, ValueError, ValidationError) as exc:
        typer.echo(f"Invalid monitor configuration: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if not spec.station_id:
        typer.echo("Registry entry needs an ICAO station_id.", err=True)
        raise typer.Exit(code=2)

    archive = RawEventArchive(data_dir / "raw")
    with GammaClient() as gamma:
        event = gamma.event_by_slug(event_slug)
    event_path = archive.append(
        source="polymarket_gamma_event",
        fetched_at=event.fetched_at,
        request_url=event.request_url,
        payload=event.raw_payload,
    )
    evidence = parse_settlement_evidence(event, registry_spec=spec)
    signal_verification = verify_signal_contract(evidence, spec)
    token_market_slugs = {}
    for market in event.markets:
        if len(market.outcomes) != len(market.clob_token_ids):
            continue
        for outcome, token_id in zip(market.outcomes, market.clob_token_ids, strict=True):
            if outcome.casefold() == "yes":
                token_market_slugs[token_id] = market.slug
                break
    if not token_market_slugs:
        typer.echo("Event has no aligned binary Yes-token identifiers.", err=True)
        raise typer.Exit(code=2)

    completed_cycles = 0
    cached_tafs = ()
    cached_taf_path: str | None = None
    last_taf_fetch: datetime | None = None
    collection_exceptions = (httpx.HTTPError, ValueError, ValidationError, KeyError)
    try:
        with (
            NwsClient() as nws_client,
            AviationWeatherClient() as aviation_client,
            ClobClient() as clob_client,
            ResearchWarehouse(data_dir / "research.duckdb") as warehouse,
        ):
            while cycles == 0 or completed_cycles < cycles:
                cycle_started = time.monotonic()
                cycle_number = completed_cycles + 1
                errors: dict[str, str] = {}
                raw_paths = {"event": str(event_path.resolve())}

                nws_observation = None
                try:
                    nws_observation = nws_client.latest_observation(spec.station_id)
                    nws_path = archive.append(
                        source="nws_latest_observation",
                        fetched_at=datetime.now(UTC),
                        request_url=(
                            f"https://api.weather.gov/stations/{spec.station_id}/observations/latest"
                        ),
                        payload=nws_observation.raw,
                    )
                    raw_paths["nws"] = str(nws_path.resolve())
                except collection_exceptions as exc:
                    errors["nws"] = str(exc)

                metars = ()
                try:
                    metar_url, metars, metar_raw = aviation_client.metars(
                        station_id=spec.station_id,
                        hours=metar_hours,
                    )
                    metar_path = archive.append(
                        source="noaa_aviation_metar",
                        fetched_at=datetime.now(UTC),
                        request_url=metar_url,
                        payload=metar_raw,
                    )
                    raw_paths["metar"] = str(metar_path.resolve())
                except collection_exceptions as exc:
                    errors["metar"] = str(exc)

                now = datetime.now(UTC)
                taf_due = (
                    last_taf_fetch is None
                    or (now - last_taf_fetch).total_seconds() >= taf_refresh_minutes * 60
                )
                if taf_due:
                    try:
                        taf_url, cached_tafs, taf_raw = aviation_client.tafs(
                            station_id=spec.station_id
                        )
                        taf_path = archive.append(
                            source="noaa_aviation_taf",
                            fetched_at=datetime.now(UTC),
                            request_url=taf_url,
                            payload=taf_raw,
                        )
                        cached_taf_path = str(taf_path.resolve())
                        last_taf_fetch = datetime.now(UTC)
                    except collection_exceptions as exc:
                        errors["taf"] = str(exc)
                if cached_taf_path:
                    raw_paths["taf"] = cached_taf_path

                quotes = ()
                try:
                    quote_batch = clob_client.batch_quotes(token_ids=tuple(token_market_slugs))
                    quotes = quote_batch.quotes
                    quote_path = archive.append(
                        source="polymarket_clob_quotes",
                        fetched_at=quote_batch.fetched_at,
                        request_url=quote_batch.request_url,
                        payload=quote_batch.raw,
                    )
                    raw_paths["clob"] = str(quote_path.resolve())
                except collection_exceptions as exc:
                    errors["clob"] = str(exc)

                snapshot = build_monitor_snapshot(
                    captured_at=datetime.now(UTC),
                    station_id=spec.station_id,
                    event_id=event.event_id,
                    event_slug=event.event_slug,
                    signal_contract_verified=signal_verification.tradeable,
                    nws=nws_observation,
                    metars=metars,
                    tafs=cached_tafs,
                    quotes=quotes,
                    token_market_slugs=token_market_slugs,
                    thresholds=thresholds,
                    collection_errors=errors,
                    raw_paths=raw_paths,
                )
                snapshot_path = archive.append(
                    source="monitor_snapshot",
                    fetched_at=snapshot.captured_at,
                    request_url=f"monitor://{event.event_slug}/{cycle_number}",
                    payload=snapshot.model_dump(mode="json"),
                )
                snapshot = snapshot.model_copy(
                    update={
                        "raw_paths": {
                            **snapshot.raw_paths,
                            "monitor": str(snapshot_path.resolve()),
                        }
                    }
                )
                snapshot_id = warehouse.record_monitor_snapshot(snapshot)
                completed_cycles += 1
                payload = snapshot.model_dump(mode="json")
                payload.update(
                    {
                        "snapshot_id": snapshot_id,
                        "cycle": completed_cycles,
                        "taf_refreshed": taf_due and "taf" not in errors,
                        "mode": "continuous_read_only_no_execution",
                    }
                )
                _emit(payload)

                if cycles != 0 and completed_cycles >= cycles:
                    break
                elapsed = time.monotonic() - cycle_started
                _interruptible_sleep(interval_seconds - elapsed)
    except KeyboardInterrupt:
        _emit(
            {
                "status": "stopped_by_user",
                "completed_cycles": completed_cycles,
                "mode": "continuous_read_only_no_execution",
            }
        )


@app.command("market-stream")
def market_stream(
    event_slugs: Annotated[
        list[str], typer.Argument(help="One or more exact Polymarket event slugs.")
    ],
    runtime_seconds: Annotated[
        float,
        typer.Option("--runtime", min=0, help="0 runs until Ctrl+C."),
    ] = 0,
    silence_timeout_seconds: Annotated[
        float,
        typer.Option("--silence-timeout", min=15, max=300),
    ] = 45,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Run the public event-driven Polymarket CLOB WebSocket daemon."""
    with GammaClient() as gamma:
        events = [gamma.event_by_slug(event_slug) for event_slug in event_slugs]
    archive = RawEventArchive(data_dir / "raw")
    event_paths = [
        archive.append(
            source="polymarket_gamma_event",
            fetched_at=event.fetched_at,
            request_url=event.request_url,
            payload=event.raw_payload,
        )
        for event in events
    ]
    asset_slugs = {
        token_id: f"{event.event_slug}/{market.slug}:{outcome}"
        for event in events
        for market in event.markets
        if len(market.outcomes) == len(market.clob_token_ids)
        for outcome, token_id in zip(market.outcomes, market.clob_token_ids, strict=True)
    }
    asset_events = {
        token_id: event.event_slug
        for event in events
        for market in event.markets
        if len(market.outcomes) == len(market.clob_token_ids)
        for token_id in market.clob_token_ids
    }
    if not asset_slugs:
        typer.echo("Event has no aligned CLOB token identifiers.", err=True)
        raise typer.Exit(code=2)
    bot = MarketWebSocketBot(
        asset_slugs=asset_slugs,
        asset_events=asset_events,
        data_dir=data_dir,
        silence_timeout_seconds=silence_timeout_seconds,
    )
    try:
        metrics = asyncio.run(bot.run(runtime_seconds=runtime_seconds))
    except KeyboardInterrupt:
        typer.echo("Market stream interrupted by user.", err=True)
        return
    payload = asdict(metrics)
    payload.update(
        {
            "event_ids": [event.event_id for event in events],
            "event_slugs": [event.event_slug for event in events],
            "asset_count": len(asset_slugs),
            "event_raw_paths": [str(path.resolve()) for path in event_paths],
            "status_path": str(bot.status_path.resolve()),
            "mode": "public_websocket_read_only_no_execution",
        }
    )
    _emit(payload)


@app.command("weather-stream")
def weather_stream(
    settlement_keys: Annotated[
        list[str], typer.Argument(help="One or more settlement registry entry keys.")
    ],
    runtime_seconds: Annotated[
        float,
        typer.Option("--runtime", min=0, help="0 runs until Ctrl+C."),
    ] = 0,
    observation_interval_seconds: Annotated[
        float,
        typer.Option("--observation-interval", min=60, max=3600),
    ] = 60,
    taf_interval_seconds: Annotated[
        float,
        typer.Option("--taf-interval", min=600, max=7200),
    ] = 600,
    forecast_interval_seconds: Annotated[
        float,
        typer.Option("--forecast-interval", min=3600, max=21_600),
    ] = 10_800,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Run the independent async NOAA weather collection daemon."""
    registry = load_settlement_registry(config)
    specs = []
    for settlement_key in settlement_keys:
        try:
            spec = registry.by_key(settlement_key)
        except KeyError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        if not spec.station_id:
            typer.echo(f"Registry entry {spec.key!r} needs an ICAO station_id.", err=True)
            raise typer.Exit(code=2)
        specs.append(spec)
    stations = [
        WeatherStation(
            station_id=spec.station_id or "",
            latitude=spec.latitude,
            longitude=spec.longitude,
            timezone=spec.timezone,
        )
        for spec in specs
    ]
    daemon = WeatherDaemon(
        stations=stations,
        data_dir=data_dir,
        observation_interval_seconds=observation_interval_seconds,
        taf_interval_seconds=taf_interval_seconds,
        forecast_interval_seconds=forecast_interval_seconds,
    )
    try:
        metrics = asyncio.run(daemon.run(runtime_seconds=runtime_seconds))
    except KeyboardInterrupt:
        typer.echo("Weather stream interrupted by user.", err=True)
        return
    payload = asdict(metrics)
    payload.update(
        {
            "settlement_keys": [spec.key for spec in specs],
            "station_ids": [station.station_id for station in stations],
            "status_path": str(daemon.status_path.resolve()),
            "mode": "async_public_weather_read_only_no_execution",
        }
    )
    _emit(payload)


@app.command("stream-status")
def stream_status(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Read daemon heartbeat files without contacting external services."""
    statuses = {}
    for name, filename in (
        ("market", "polymarket_ws_status.json"),
        ("weather", "weather_daemon_status.json"),
        ("signal", "signal_engine_status.json"),
    ):
        path = data_dir / "runtime" / filename
        if not path.exists():
            statuses[name] = {"state": "not_started", "status_path": str(path.resolve())}
            continue
        try:
            statuses[name] = json.loads(path.read_text(encoding="utf-8"))
            statuses[name]["status_path"] = str(path.resolve())
        except (OSError, json.JSONDecodeError) as exc:
            statuses[name] = {
                "state": "unreadable",
                "error": str(exc),
                "status_path": str(path.resolve()),
            }
    _emit(statuses)


@app.command("signal-engine")
def signal_engine(
    market_specs: Annotated[
        list[str],
        typer.Option(
            "--market",
            help="Repeat EVENT_SLUG=SETTLEMENT_KEY for each monitored city.",
        ),
    ],
    runtime_seconds: Annotated[
        float,
        typer.Option("--runtime", min=0, help="0 runs until Ctrl+C."),
    ] = 0,
    interval_seconds: Annotated[
        float,
        typer.Option("--interval", min=0.1, max=10),
    ] = 0.25,
    min_net_edge_text: Annotated[str, typer.Option("--min-net-edge")] = "0.03",
    cost_buffer_text: Annotated[str, typer.Option("--cost-buffer")] = "0.01",
    calibration_lead_days: Annotated[
        int,
        typer.Option(
            "--calibration-lead-days",
            min=0,
            max=7,
            help="Historical forecast lead used by the live credibility gate.",
        ),
    ] = 0,
    min_calibration_samples: Annotated[
        int,
        typer.Option(
            "--min-calibration-samples",
            min=2,
            help="Minimum training history before walk-forward validation.",
        ),
    ] = 30,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Run deterministic live probability and read-only edge calculations."""
    registry = load_settlement_registry(config)
    parsed_specs: list[tuple[str, str]] = []
    for value in market_specs:
        event_slug, separator, settlement_key = value.partition("=")
        if not separator or not event_slug or not settlement_key:
            typer.echo(f"Invalid --market value: {value!r}", err=True)
            raise typer.Exit(code=2)
        parsed_specs.append((event_slug, settlement_key))
    configs = []
    with GammaClient() as gamma:
        for event_slug, settlement_key in parsed_specs:
            try:
                spec = registry.by_key(settlement_key)
            except KeyError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=2) from exc
            if not spec.station_id or not spec.timezone:
                typer.echo(f"Settlement {spec.key!r} lacks station or timezone.", err=True)
                raise typer.Exit(code=2)
            event = _event_by_slug_with_retry(gamma, event_slug)
            evidence = parse_settlement_evidence(event, registry_spec=spec)
            if evidence.target_date is None:
                typer.echo(f"Could not parse target date for {event_slug!r}.", err=True)
                raise typer.Exit(code=2)
            verification = verify_signal_contract(evidence, spec)
            contract_verified = (
                spec.status is VerificationStatus.VERIFIED and verification.tradeable
            )
            contract_reason = (
                verification.reason
                if contract_verified
                else f"registry is not verified; {verification.reason}"
            )
            prior_samples: list[CalibrationSample] = []
            warehouse_path = data_dir / "research.duckdb"
            if warehouse_path.exists():
                with ResearchWarehouse(warehouse_path) as warehouse:
                    prior_samples = [
                        sample
                        for sample in warehouse.samples(
                            station_id=spec.station_id,
                            lead_days=calibration_lead_days,
                            model="gfs_seamless",
                        )
                        if sample.target_date < evidence.target_date
                    ]
            calibration = build_live_calibration(
                prior_samples,
                station_id=spec.station_id,
                lead_days=calibration_lead_days,
                min_samples=min_calibration_samples,
            )
            configs.append(
                LiveSignalConfig(
                    event_id=event.event_id,
                    event_slug=event.event_slug,
                    station_id=spec.station_id,
                    timezone=spec.timezone,
                    target_date=evidence.target_date,
                    markets=event.markets,
                    contract_verified=contract_verified,
                    contract_reason=contract_reason,
                    calibration=calibration,
                )
            )
    engine = LiveSignalEngine(
        configs=tuple(configs),
        data_dir=data_dir,
        interval_seconds=interval_seconds,
        min_net_edge=Decimal(min_net_edge_text),
        cost_buffer=Decimal(cost_buffer_text),
    )
    try:
        metrics = asyncio.run(engine.run(runtime_seconds=runtime_seconds))
    except KeyboardInterrupt:
        typer.echo("Signal engine interrupted by user.", err=True)
        return
    payload = asdict(metrics)
    payload.update(
        {
            "events": [item.event_slug for item in configs],
            "status_path": str(engine.status_path.resolve()),
            "state_path": str(engine.state_path.resolve()),
            "mode": "deterministic_read_only_no_execution_no_model",
        }
    )
    _emit(payload)
