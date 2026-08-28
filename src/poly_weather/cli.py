from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import duckdb
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
from poly_weather.adapters.open_meteo import OpenMeteoDeterministicClient
from poly_weather.adapters.polymarket import GammaClient, is_weather_market
from poly_weather.adapters.polymarket_data import PolymarketDataClient
from poly_weather.archive_io import jsonl_archive_paths
from poly_weather.calibration import (
    evaluate_bucket_skill,
    fit_bias_calibration,
    learn_model_weights,
    rolling_origin_evaluate,
)
from poly_weather.certainty_report import (
    download_iem_asos,
    render_certainty_summary_report,
    station_certainty_summary,
)
from poly_weather.complement_pair import (
    default_complement_pair_config,
    predefined_complement_pair_configs,
    render_complement_pair_report,
    replay_complement_pairs_streaming,
    replay_complement_pairs_streaming_grid,
    write_complement_pair_result,
)
from poly_weather.config import load_settlement_registry
from poly_weather.depth_calibration import (
    build_depth_cost_calibration,
    render_depth_cost_calibration,
)
from poly_weather.domain import CalibrationSample, TruthKind, VerificationStatus
from poly_weather.entry_accessibility import (
    analyze_no_entry_accessibility,
    current_rule_rows,
    current_tail_rule_targets,
    render_no_entry_accessibility_report,
)
from poly_weather.fees import (
    configured_fee_rate,
    fetch_market_fee_details,
    fetch_token_fee_rate_bps,
)
from poly_weather.high_frequency_audit import (
    build_high_frequency_reanalysis,
    render_high_frequency_reanalysis,
)
from poly_weather.intraday_reversal import load_iem_asos_csv
from poly_weather.liquidity import (
    archived_liquidity_rows,
    archived_liquidity_rows_from_jsonl,
    render_liquidity_report,
)
from poly_weather.maintenance_audit import (
    audit_archive_paths,
    audit_reconnect_rows,
    load_reconnect_rows,
    render_maintenance_audit,
)
from poly_weather.market_stream import EventArchivePolicy, MarketWebSocketBot
from poly_weather.market_supervisor import (
    MarketEventSupervisor,
    discover_event,
    event_asset_maps,
)
from poly_weather.market_trade_tape import (
    build_shadow_trade_events,
    load_market_ws_trades,
)
from poly_weather.modeling import (
    DEFAULT_MULTI_MODEL_WEIGHTS,
    blend_multi_model_forecasts,
    build_bucket_forecast,
)
from poly_weather.monitoring import MonitorThresholds, build_monitor_snapshot
from poly_weather.no_forward import forward_summary, render_forward_report
from poly_weather.no_side_analysis import (
    audit_no_proxy_distortion,
    load_history_directory,
    render_no_proxy_audit,
)
from poly_weather.paper import PaperPolicy, make_paper_decision
from poly_weather.polymarket_status import (
    PolymarketStatusClient,
    load_quality_overrides,
    load_quality_windows,
    merge_quality_windows,
    persist_quality_windows,
)
from poly_weather.precision_audit import (
    download_iem_precision_rows,
    precision_comparison,
    render_precision_audit,
    temperature_observations_from_precision_rows,
)
from poly_weather.price_band_accessibility import (
    analyze_price_band_accessibility,
    render_price_band_accessibility_report,
)
from poly_weather.price_path_analysis import (
    CONTROL_BANDS,
    PRIMARY_BANDS,
    analyze_price_paths,
    compact_no_book_pairs,
    load_archived_weather_observations,
    render_price_path_report,
)
from poly_weather.public_trade_collection import (
    collect_depth_event_trades,
    discover_depth_event_coverage,
)
from poly_weather.real_no_books import (
    analyze_eliminated_no_exit,
    analyze_real_no_books,
    archived_event_metadata,
    iter_paired_book_snapshots,
    paired_book_snapshots,
    render_eliminated_exit_report,
    render_real_no_report,
)
from poly_weather.research_store import ResearchWarehouse
from poly_weather.settlement import (
    parse_settlement_evidence,
    verify_settlement_evidence,
    verify_signal_contract,
)
from poly_weather.shadow_orders import ShadowStrategyConfig
from poly_weather.shadow_runtime import run_shadow_spread_continuous, run_shadow_spread_once
from poly_weather.shadow_spread_replay import (
    default_shadow_strategy_config,
    render_shadow_spread_report,
    replay_shadow_spread,
    write_shadow_spread_result,
)
from poly_weather.signal_engine import (
    LiveSignalConfig,
    LiveSignalEngine,
    build_live_calibration,
)
from poly_weather.signal_migration import migrate_signal_snapshots_from_jsonl
from poly_weather.storage import CatalogStore, RawEventArchive
from poly_weather.temperature import celsius_to_fahrenheit
from poly_weather.trade_tape_analysis import (
    analyze_trade_tape_staleness,
    load_event_trade_tapes,
    physical_elimination_times,
    render_trade_tape_report,
)
from poly_weather.warming_policy import (
    WarmingThresholdRegistry,
    build_heat_season_policy_analysis,
    policy_document,
    render_heat_season_policy_report,
)
from poly_weather.weather_market_join import (
    align_weather_to_snapshots,
    load_realtime_weather_observations,
)
from poly_weather.weather_stream import WeatherDaemon, WeatherStation
from poly_weather.wrh_backfill import (
    WrhBackfillRequest,
    backfill_wrh_history,
    render_wrh_backfill_report,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Research, replay, and non-executable paper decisions for Polymarket weather markets.",
)

DEFAULT_CONFIG = Path("configs/settlements.json")
DEFAULT_DATA_DIR = Path("data")
DEFAULT_WARMING_POLICY = Path("configs/warming_window_no_thresholds.json")


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _read_json_with_retry(path: Path, *, attempts: int = 5) -> dict[str, Any]:
    for attempt in range(attempts):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"{path} does not contain a JSON object")
            return value
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable JSON retry state")


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


def _parse_date(value: str, *, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{label} must use YYYY-MM-DD") from exc


def _interruptible_sleep(seconds: float) -> None:
    remaining = max(0.0, seconds)
    while remaining > 0:
        chunk = min(30.0, remaining)
        time.sleep(chunk)
        remaining -= chunk


@app.command("validate-settlements")
def validate_settlements(
    config: Annotated[
        Path, typer.Option("--config", help="Settlement registry JSON file.")
    ] = DEFAULT_CONFIG,
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
    data_dir: Annotated[
        Path, typer.Option(help="Archive and catalog directory.")
    ] = DEFAULT_DATA_DIR,
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
        latest_temperature_f = celsius_to_fahrenheit(latest_metar.temperature_c)
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
    config: Annotated[
        Path, typer.Option("--config", help="Settlement registry JSON file.")
    ] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option(help="Archive directory.")] = DEFAULT_DATA_DIR,
    calibration_lead_days: Annotated[int, typer.Option(min=1, max=7)] = 1,
    min_calibration_samples: Annotated[int, typer.Option(min=2)] = 30,
) -> None:
    """Build a deterministic, calibration-based distribution for event buckets."""
    registry = load_settlement_registry(config)
    try:
        spec = registry.by_key(settlement_key)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if spec.latitude is None or spec.longitude is None or spec.timezone is None:
        typer.echo(
            "Settlement entry needs latitude, longitude, and timezone for forecasting.", err=True
        )
        raise typer.Exit(code=2)
    try:
        target_date = date.fromisoformat(target_date_text)
    except ValueError as exc:
        typer.echo("Target date must use YYYY-MM-DD format.", err=True)
        raise typer.Exit(code=2) from exc
    local_today = datetime.now(ZoneInfo(spec.timezone)).date()
    forecast_days = (target_date - local_today).days + 1
    if target_date >= local_today and forecast_days > 10:
        typer.echo("Target date must be within the current 10-day forecast window.", err=True)
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

    if target_date < local_today:
        with OpenMeteoPreviousRunsClient() as weather:
            forecast_url, historical = weather.daily_highs(
                station_id=spec.station_id or settlement_key,
                latitude=spec.latitude,
                longitude=spec.longitude,
                timezone=spec.timezone,
                start_date=target_date,
                end_date=target_date,
                lead_days=calibration_lead_days,
            )
        matching = [point.value_f for point in historical.values if point.local_date == target_date]
        if not matching:
            typer.echo("Deterministic historical forecast is unavailable.", err=True)
            raise typer.Exit(code=1)
        deterministic_high = Decimal(str(matching[0]))
        forecast_model = historical.model or "gfs_seamless"
        deterministic_raw_path = archive.append(
            source="open_meteo_previous_runs",
            fetched_at=historical.fetched_at,
            request_url=forecast_url,
            payload=historical.raw,
        )
    else:
        with OpenMeteoDeterministicClient() as weather:
            forecast_url, deterministic = weather.temperature_forecast(
                latitude=spec.latitude,
                longitude=spec.longitude,
                timezone=spec.timezone,
                forecast_days=forecast_days,
            )
        deterministic_high = deterministic.daily_high(target_date)
        forecast_model = deterministic.model
        deterministic_raw_path = archive.append(
            source="open_meteo_deterministic",
            fetched_at=deterministic.fetched_at,
            request_url=forecast_url,
            payload=deterministic.raw,
        )

    source_matches = spec.resolution_source_url is not None and event.resolution_source == str(
        spec.resolution_source_url
    )
    slug_matches = re.fullmatch(spec.market_slug_pattern, event.event_slug) is not None
    tradeable = spec.status is VerificationStatus.VERIFIED and source_matches and slug_matches
    if spec.status is not VerificationStatus.VERIFIED:
        reason = "settlement registry entry is unverified"
    elif not slug_matches:
        reason = "event slug does not exactly match the verified settlement rule"
    elif not source_matches:
        reason = "event resolution source differs from the verified settlement rule"
    else:
        reason = "verified settlement, slug, and resolution source"

    calibration_applied = False
    calibration_sample_count = 0
    calibration_bias_f = None
    calibration_basis = None
    residual_std_f = None
    calibrated_high = deterministic_high
    warehouse_path = data_dir / "research.duckdb"
    if warehouse_path.exists() and spec.station_id:
        with ResearchWarehouse(warehouse_path) as warehouse:
            prior_samples = [
                sample
                for sample in warehouse.samples(
                    station_id=spec.station_id,
                    lead_days=calibration_lead_days,
                    model=forecast_model,
                )
                if sample.target_date < target_date
            ]
        calibration_sample_count = len(prior_samples)
        if calibration_sample_count >= 2:
            fitted = fit_bias_calibration(prior_samples)
            residual_std_f = fitted.residual_std_f
            if calibration_sample_count >= min_calibration_samples:
                calibration_bias_f = fitted.bias_f
                calibrated_high += Decimal(str(fitted.bias_f))
                calibration_applied = True
                calibration_basis = (
                    "Open-Meteo Previous Runs lead>=1 + same-station NOAA NCEI daily truth"
                )
    if residual_std_f is None:
        typer.echo("At least two no-lookahead calibration samples are required.", err=True)
        raise typer.Exit(code=1)

    bucket_forecast = build_bucket_forecast(
        markets=event.markets,
        deterministic_high_f=calibrated_high,
        residual_std_f=residual_std_f,
        target_date=target_date,
        forecast_model=forecast_model,
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
            "deterministic_raw_path": str(deterministic_raw_path.resolve()),
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
    config: Annotated[
        Path, typer.Option("--config", help="Settlement registry JSON file.")
    ] = DEFAULT_CONFIG,
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
    start_date_text: Annotated[
        str | None, typer.Argument(help="First target date (YYYY-MM-DD).")
    ] = None,
    end_date_text: Annotated[
        str | None, typer.Argument(help="Last target date (YYYY-MM-DD).")
    ] = None,
    start_date_option: Annotated[
        str | None, typer.Option("--start-date", help="First target date (YYYY-MM-DD).")
    ] = None,
    end_date_option: Annotated[
        str | None, typer.Option("--end-date", help="Last target date (YYYY-MM-DD).")
    ] = None,
    lead_days: Annotated[int, typer.Option(min=1, max=7, help="Fixed forecast lead in days.")] = 1,
    model: Annotated[str, typer.Option(help="Open-Meteo model identifier.")] = "gfs_seamless",
    multi_model: Annotated[
        bool,
        typer.Option("--multi-model", help="Backfill GFS, ICON, and GEM deterministic forecasts."),
    ] = False,
    config: Annotated[
        Path, typer.Option("--config", help="Settlement registry JSON file.")
    ] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option(help="Research data directory.")] = DEFAULT_DATA_DIR,
) -> None:
    """Join fixed-lead historical forecasts to same-station NOAA daily truth."""
    if start_date_text and start_date_option and start_date_text != start_date_option:
        typer.echo("Positional start date and --start-date disagree.", err=True)
        raise typer.Exit(code=2)
    if end_date_text and end_date_option and end_date_text != end_date_option:
        typer.echo("Positional end date and --end-date disagree.", err=True)
        raise typer.Exit(code=2)
    start_date_value = start_date_option or start_date_text
    end_date_value = end_date_option or end_date_text
    if not start_date_value or not end_date_value:
        typer.echo("Both start and end dates are required.", err=True)
        raise typer.Exit(code=2)
    try:
        start_date = date.fromisoformat(start_date_value)
        end_date = date.fromisoformat(end_date_value)
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

    forecast_high_f_by_model_by_date: dict[date, dict[str, float]] = {}
    model_weights: dict[str, float] | None = None
    with OpenMeteoPreviousRunsClient() as forecast_client:
        if multi_model:
            forecast_url, forecasts_by_model = forecast_client.get_multi_model_ensemble(
                station_id=spec.station_id or settlement_key,
                latitude=spec.latitude,
                longitude=spec.longitude,
                timezone=spec.timezone,
                start_date=start_date,
                end_date=end_date,
                lead_days=lead_days,
            )
            values_by_model = {
                model_name: {point.local_date: point.value_f for point in series.values}
                for model_name, series in forecasts_by_model.items()
            }
            common_forecast_dates = set.intersection(
                *(set(values) for values in values_by_model.values())
            )
            forecast_high_f_by_model_by_date = {
                day: {
                    model_name: values_by_model[model_name][day] for model_name in values_by_model
                }
                for day in common_forecast_dates
            }
            model_weights = dict(DEFAULT_MULTI_MODEL_WEIGHTS)
            forecast_by_date = {
                day: blend_multi_model_forecasts(values, model_weights)
                for day, values in forecast_high_f_by_model_by_date.items()
            }
            forecasts = next(iter(forecasts_by_model.values()))
            stored_model = "multi_model_blend"
        else:
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
            forecast_by_date = {point.local_date: point.value_f for point in forecasts.values}
            stored_model = model
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
    truth_by_date = {point.local_date: point.value_f for point in truth.values}
    joined_dates = sorted(set(forecast_by_date) & set(truth_by_date))
    ingested_at = datetime.now(UTC)
    samples = [
        CalibrationSample(
            station_id=spec.station_id or settlement_key,
            target_date=day,
            lead_days=lead_days,
            model=stored_model,
            forecast_high_f=forecast_by_date[day],
            observed_high_f=truth_by_date[day],
            forecast_source=forecasts.source,
            truth_source=truth.source,
            truth_kind=TruthKind.NOAA_SAME_STATION_DAILY_FINAL.value,
            ingested_at=ingested_at,
            forecast_high_f_by_model=forecast_high_f_by_model_by_date.get(day),
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
            "model": stored_model,
            "multi_model": multi_model,
            "model_weights": model_weights,
            "joined_samples": written,
            "forecast_only_dates": sorted(
                str(day) for day in set(forecast_by_date) - set(truth_by_date)
            ),
            "truth_only_dates": sorted(
                str(day) for day in set(truth_by_date) - set(forecast_by_date)
            ),
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
    lead_days: Annotated[int, typer.Option(min=1, max=7)] = 1,
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


@app.command("evaluate-bucket-skill")
def evaluate_bucket_skill_command(
    settlement_key: Annotated[str, typer.Argument(help="Settlement registry entry key.")],
    start_date_text: Annotated[
        str | None, typer.Option("--start-date", help="Optional first target date.")
    ] = None,
    end_date_text: Annotated[
        str | None, typer.Option("--end-date", help="Optional last target date.")
    ] = None,
    lead_days: Annotated[int, typer.Option(min=1, max=7)] = 1,
    model: Annotated[str, typer.Option()] = "multi_model_blend",
    min_train_size: Annotated[int, typer.Option(min=2)] = 30,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Evaluate true 2°F bucket probability with walk-forward calibration."""
    try:
        start_date = date.fromisoformat(start_date_text) if start_date_text else None
        end_date = date.fromisoformat(end_date_text) if end_date_text else None
    except ValueError as exc:
        typer.echo("Dates must use YYYY-MM-DD format.", err=True)
        raise typer.Exit(code=2) from exc
    if start_date and end_date and end_date < start_date:
        typer.echo("Date range must be ordered.", err=True)
        raise typer.Exit(code=2)
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
    samples = [
        sample
        for sample in samples
        if (start_date is None or sample.target_date >= start_date)
        and (end_date is None or sample.target_date <= end_date)
    ]
    try:
        evaluation = evaluate_bucket_skill(samples, min_train_size=min_train_size)
        learned_weights = (
            learn_model_weights(samples)
            if samples and all(sample.forecast_high_f_by_model for sample in samples)
            else None
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _emit(
        {
            "settlement_key": settlement_key,
            "station_id": station_id,
            "lead_days": lead_days,
            "model": model,
            "start_date": start_date,
            "end_date": end_date,
            "learned_weights_full_sample_diagnostic": learned_weights,
            **evaluation,
        }
    )


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
            "whole_degree_value_f": observed.value_f.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            "signal_source_policy": spec.signal_truth_policy.value,
            "waits_for_wunderground": False,
            "registry_signal_policy_enabled": spec.signal_truth_policy.value == "same_station_noaa",
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
    liquidity_role: Annotated[str, typer.Option(help="taker (default) or maker")] = "taker",
    market_category: Annotated[str, typer.Option(help="Fee schedule category.")] = "weather",
    fee_rate_text: Annotated[
        str | None,
        typer.Option("--fee-rate", help="Optional offline-verified fee-rate override."),
    ] = None,
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
            liquidity_role=liquidity_role,
            market_category=market_category,
            fee_rate_override=(Decimal(fee_rate_text) if fee_rate_text is not None else None),
            assumed_slippage_bps=slippage_bps,
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
        no_index = next(
            index for index, outcome in enumerate(market.outcomes) if outcome.casefold() == "no"
        )
        no_token = market.clob_token_ids[no_index]
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
        no_price_point = warehouse.price_at_or_before(
            token_id=no_token,
            decision_time=decision_time,
            max_age=policy.max_price_age,
        )
        decision = make_paper_decision(
            event_id=event.event_id,
            event_slug=event.event_slug,
            market_id=market.market_id,
            market_slug=market.slug,
            price_point=price_point,
            no_price_point=no_price_point,
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


@app.command("check-fee-rate")
def check_fee_rate(
    token_id: Annotated[str, typer.Argument(help="Public CLOB token ID.")],
    condition_id: Annotated[
        str | None,
        typer.Option("--condition-id", help="Condition ID for authoritative V2 fd parameters."),
    ] = None,
    market_category: Annotated[str, typer.Option(help="Configured fee category.")] = "weather",
) -> None:
    """Compare configured research fees with public CLOB /fee-rate metadata."""

    async def fetch() -> tuple[int, dict[str, object] | None]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=30),
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
        ) as client:
            base_fee = await fetch_token_fee_rate_bps(client, token_id)
            details = (
                await fetch_market_fee_details(client, condition_id, token_id)
                if condition_id is not None
                else None
            )
            return base_fee, details

    try:
        configured = configured_fee_rate(market_category)
        base_fee_bps, details = asyncio.run(fetch())
    except (httpx.HTTPError, ValueError) as exc:
        typer.echo(f"Fee-rate verification failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    observed_rate = details.get("rate") if details is not None else None
    exponent = details.get("exponent") if details is not None else None
    taker_only = details.get("taker_only") if details is not None else None
    _emit(
        {
            "token_id": token_id,
            "market_category": market_category,
            "configured_taker_fee_rate": str(configured),
            "clob_base_fee_bps": base_fee_bps,
            "base_fee_note": (
                "order/fee-bearing parameter; not the p*(1-p) curve rate"
            ),
            "clob_fee_rate": str(observed_rate) if observed_rate is not None else None,
            "clob_fee_exponent": exponent,
            "clob_taker_only": taker_only,
            "matches_configured_curve": (
                observed_rate == configured and exponent == 1 and taker_only is True
                if details is not None
                else None
            ),
            "sources": [
                "GET https://clob.polymarket.com/fee-rate?token_id=...",
                "GET https://clob.polymarket.com/clob-markets/{condition_id}"
                if condition_id is not None
                else None,
            ],
            "read_only": True,
        }
    )


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
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
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
    registry = load_settlement_registry(config)
    event_policies: dict[str, EventArchivePolicy] = {}
    for event in events:
        for spec in registry.specs:
            if spec.status is not VerificationStatus.VERIFIED:
                continue
            if re.fullmatch(spec.market_slug_pattern, event.event_slug) is None:
                continue
            evidence = parse_settlement_evidence(event, registry_spec=spec)
            verification = verify_settlement_evidence(evidence, spec)
            if verification.passed and evidence.target_date is not None and spec.timezone:
                event_policies[event.event_slug] = EventArchivePolicy(
                    timezone=spec.timezone,
                    target_date=evidence.target_date,
                )
            break
    if not asset_slugs:
        typer.echo("Event has no aligned CLOB token identifiers.", err=True)
        raise typer.Exit(code=2)
    bot = MarketWebSocketBot(
        asset_slugs=asset_slugs,
        asset_events=asset_events,
        event_policies=event_policies,
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


@app.command("market-supervisor")
def market_supervisor(
    runtime_seconds: Annotated[
        float,
        typer.Option("--runtime", min=0, help="0 runs until Ctrl+C."),
    ] = 0,
    discovery_interval_seconds: Annotated[
        float,
        typer.Option("--discovery-interval", min=30),
    ] = 300,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Run fail-closed daily discovery plus hot WebSocket event rotation."""
    registry = load_settlement_registry(config)
    specs = tuple(spec for spec in registry.specs if spec.status is VerificationStatus.VERIFIED)
    if not specs:
        typer.echo("No verified settlement templates are configured.", err=True)
        raise typer.Exit(code=2)
    initial: dict[str, Any] = {}
    with GammaClient() as gamma:
        for spec in specs:
            target = datetime.now(ZoneInfo(spec.timezone or "UTC")).date()
            event = discover_event(gamma, spec, target)
            if event is None:
                continue
            evidence = parse_settlement_evidence(event, registry_spec=spec)
            verification = verify_settlement_evidence(evidence, spec)
            if verification.passed:
                initial[spec.key] = event
    if not initial:
        typer.echo("No current events passed strict settlement verification.", err=True)
        raise typer.Exit(code=2)
    asset_slugs: dict[str, str] = {}
    asset_events: dict[str, str] = {}
    initial_policies: dict[str, EventArchivePolicy] = {}
    specs_by_key = {spec.key: spec for spec in specs}
    for settlement_key, event in initial.items():
        event_slugs, event_assets = event_asset_maps(event)
        asset_slugs.update(event_slugs)
        asset_events.update(event_assets)
        spec = specs_by_key[settlement_key]
        evidence = parse_settlement_evidence(event, registry_spec=spec)
        if evidence.target_date is not None and spec.timezone:
            initial_policies[event.event_slug] = EventArchivePolicy(
                timezone=spec.timezone,
                target_date=evidence.target_date,
            )
    bot = MarketWebSocketBot(
        asset_slugs=asset_slugs,
        asset_events=asset_events,
        event_policies=initial_policies,
        data_dir=data_dir,
    )
    supervisor = MarketEventSupervisor(
        specs=specs,
        bot=bot,
        data_dir=data_dir,
        discovery_interval_seconds=discovery_interval_seconds,
    )

    async def run_both() -> None:
        bot_task = asyncio.create_task(
            bot.run(runtime_seconds=runtime_seconds), name="supervised-market-stream"
        )
        try:
            while bot.metrics.state not in {"connected", "failed", "stopped"}:
                await asyncio.sleep(0.05)
            if bot.metrics.state != "connected":
                raise RuntimeError(f"market stream did not connect: {bot.metrics.state}")
            await supervisor.reconcile(initial)
            supervisor_task = asyncio.create_task(
                supervisor.run(runtime_seconds=runtime_seconds), name="market-event-supervisor"
            )
            await bot_task
            if not supervisor_task.done():
                supervisor_task.cancel()
            await asyncio.gather(supervisor_task, return_exceptions=True)
        finally:
            bot.stop_event.set()
            await asyncio.gather(bot_task, return_exceptions=True)

    try:
        asyncio.run(run_both())
    except KeyboardInterrupt:
        typer.echo("Market supervisor interrupted by user.", err=True)
        return
    _emit(
        {
            "supervisor": asdict(supervisor.metrics),
            "market": asdict(bot.metrics),
            "active_events": [asdict(item) for item in supervisor.active.values()],
            "status_path": str(supervisor.status_path.resolve()),
            "execution_enabled": False,
        }
    )


@app.command("liquidity-report")
def liquidity_report(
    start_text: Annotated[
        str | None,
        typer.Option("--start", help="Optional inclusive ISO-8601 timestamp with offset."),
    ] = None,
    end_text: Annotated[
        str | None,
        typer.Option("--end", help="Optional exclusive ISO-8601 timestamp with offset."),
    ] = None,
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Markdown report destination."),
    ] = Path("data/liquidity_report.md"),
    source: Annotated[
        str,
        typer.Option(help="auto, duckdb, or jsonl; auto is safe while the daemon runs."),
    ] = "auto",
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Summarize spread, depth slippage, and partial fills from archived books."""
    start = _parse_aware_datetime(start_text, label="start") if start_text else None
    end = _parse_aware_datetime(end_text, label="end") if end_text else None
    if start is not None and end is not None and end <= start:
        raise typer.BadParameter("end must be later than start")
    if source not in {"auto", "duckdb", "jsonl"}:
        raise typer.BadParameter("source must be auto, duckdb, or jsonl")
    database_path = data_dir / "market_stream.duckdb"
    source_used = source
    if source == "jsonl":
        rows = archived_liquidity_rows_from_jsonl(
            data_dir,
            start=start,
            end=end,
        )
    else:
        try:
            with ResearchWarehouse(database_path) as warehouse:
                rows = archived_liquidity_rows(
                    warehouse.connection,
                    start=start,
                    end=end,
                    quality_windows=load_quality_windows(
                        data_dir / "runtime" / "polymarket_quality_windows.json"
                    ),
                )
            source_used = "duckdb"
        except duckdb.IOException:
            if source == "duckdb":
                raise
            rows = archived_liquidity_rows_from_jsonl(
                data_dir,
                start=start,
                end=end,
            )
            source_used = "jsonl"
    render_liquidity_report(rows, output_path=output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "source": source_used,
            "group_count": len(rows),
            "sample_count": sum(int(row["sample_count"]) for row in rows),
            "groups_over_5c_slippage_at_200": sum(
                row["slippage_200_usd"] is not None and row["slippage_200_usd"] > 0.05
                for row in rows
            ),
            "execution_enabled": False,
        }
    )


@app.command("multi-city-certainty-report")
def multi_city_certainty_report(
    start_date_text: Annotated[str, typer.Option("--start-date")] = "2024-06-01",
    end_date_text: Annotated[str, typer.Option("--end-date")] = "2026-08-23",
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh")] = True,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/multi_city_certainty_report.md"
    ),
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Download IEM observations and compare intraday certainty across cities."""
    start_date = _parse_date(start_date_text, label="start date")
    end_date = _parse_date(end_date_text, label="end date")
    if end_date < start_date:
        raise typer.BadParameter("end date must not precede start date")
    registry = load_settlement_registry(config)
    rows = []
    downloads = []
    for spec in registry.specs:
        if spec.status is not VerificationStatus.VERIFIED or not spec.station_id or not spec.timezone:
            continue
        csv_path = data_dir / "hourly_obs" / f"{spec.station_id}.csv"
        if refresh or not csv_path.exists():
            downloads.append(
                download_iem_asos(
                    station_id=spec.station_id,
                    timezone=spec.timezone,
                    start_date=start_date,
                    end_date=end_date,
                    output_path=csv_path,
                )
            )
        rows.append(station_certainty_summary(spec, csv_path))
    render_certainty_summary_report(rows, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "station_count": len(rows),
            "downloads": downloads,
            "rows": rows,
            "no_lookahead": True,
        }
    )


@app.command("high-frequency-weather-reanalysis")
def high_frequency_weather_reanalysis(
    start_date_text: Annotated[str, typer.Option("--start-date")] = "2024-06-01",
    end_date_text: Annotated[str, typer.Option("--end-date")] = "2026-08-22",
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/high_frequency_weather_reanalysis.md"
    ),
    json_path: Annotated[Path, typer.Option("--json-output")] = Path(
        "data/high_frequency_weather_reanalysis.json"
    ),
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Compare IEM hourly features with post-hoc WRH high-frequency history."""
    start_date = _parse_date(start_date_text, label="start date")
    end_date = _parse_date(end_date_text, label="end date")
    if end_date < start_date:
        raise typer.BadParameter("end date must not precede start date")
    registry = load_settlement_registry(config)
    specs = [
        spec
        for spec in registry.specs
        if spec.status is VerificationStatus.VERIFIED and spec.station_id and spec.timezone
    ]
    result = build_high_frequency_reanalysis(
        specs,
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
    )
    render_high_frequency_reanalysis(result, output_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "json_path": str(json_path.resolve()),
            "station_count": len(result["stations"]),
            "collection_mode": result["collection_mode"],
            "strict_no_lookahead_eligible": False,
            "execution_enabled": False,
        }
    )


@app.command("derive-warming-window-policy")
def derive_warming_window_policy(
    start_date_text: Annotated[str, typer.Option("--start-date")] = "2024-06-01",
    end_date_text: Annotated[str, typer.Option("--end-date")] = "2026-08-22",
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/warming_window_threshold_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/warming_window_threshold_analysis.json"
    ),
    policy_path: Annotated[Path, typer.Option("--policy-output")] = Path(
        "configs/warming_window_no_thresholds.json"
    ),
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Derive station heat-season NO thresholds from post-hoc WRH history."""
    start_date = _parse_date(start_date_text, label="start date")
    end_date = _parse_date(end_date_text, label="end date")
    if end_date < start_date:
        raise typer.BadParameter("end date must not precede start date")
    registry = load_settlement_registry(config)
    specs = [
        spec
        for spec in registry.specs
        if spec.status is VerificationStatus.VERIFIED and spec.station_id and spec.timezone
    ]
    result = build_heat_season_policy_analysis(
        specs,
        data_dir=data_dir,
        start_date=start_date,
        end_date=end_date,
    )
    render_heat_season_policy_report(result, output_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    policy = policy_document(result)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        json.dumps(policy, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "policy_path": str(policy_path.resolve()),
            "station_count": len(result["stations"]),
            "recommended_profile": result["recommended_profile"],
            "collection_mode": result["collection_mode"],
            "strict_no_lookahead_eligible": False,
            "execution_enabled": False,
        }
    )


@app.command("execution-cost-calibration")
def execution_cost_calibration(
    analysis_path: Annotated[
        Path,
        typer.Option("--analysis", help="market_lag_analysis.json path."),
    ] = Path("data/market_lag_analysis.json"),
    catalog_path: Annotated[
        Path,
        typer.Option("--catalog", help="settled_markets.json path."),
    ] = Path("data/settled_markets.json"),
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Markdown report destination."),
    ] = Path("data/execution_cost_calibration.md"),
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Compare p-proxy trades with strict no-lookahead archived order-book fills."""
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    result = build_depth_cost_calibration(analysis, catalog, data_dir=data_dir)
    render_depth_cost_calibration(result, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "proxy_trade_count": result["proxy_trade_count"],
            "catalog_matched_trade_count": result["catalog_matched_trade_count"],
            "fully_executable_by_size": {
                f"${row['size_usd']:.0f}": row["fully_executable_count"]
                for row in result["summaries"]
            },
            "execution_enabled": False,
        }
    )


@app.command("audit-no-proxy")
def audit_no_proxy(
    catalog_path: Annotated[Path, typer.Option("--catalog")] = Path(
        "data/settled_markets.json"
    ),
    histories_dir: Annotated[Path, typer.Option("--histories-dir")] = Path(
        "data/historical_prices"
    ),
    observations_dir: Annotated[Path, typer.Option("--observations-dir")] = Path(
        "data/hourly_obs"
    ),
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/no_proxy_audit_report.md"
    ),
) -> None:
    """Audit stale 1-p contradictions; never treats p as executable NO quotes."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    histories = load_history_directory(histories_dir)
    stations = {str(event["station_id"]) for event in catalog.get("events") or []}
    events = catalog.get("events") or []
    first_date = min(date.fromisoformat(str(event["target_date"])) for event in events)
    last_date = max(date.fromisoformat(str(event["target_date"])) for event in events)
    timezone_by_station = {
        str(event["station_id"]): str(event["timezone"]) for event in events
    }
    observations = {}
    for station in stations:
        precision_rows = download_iem_precision_rows(
            station_id=station,
            timezone=timezone_by_station[station],
            start_date=first_date,
            end_date=last_date,
        )
        observations[station] = temperature_observations_from_precision_rows(
            precision_rows, station_id=station
        )
    result = audit_no_proxy_distortion(
        catalog,
        histories_by_event=histories,
        observations_by_station=observations,
    )
    render_no_proxy_audit(result, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "event_count": result["event_count"],
            "contradiction_point_count": result["contradiction_point_count"],
            "contradiction_share_of_eliminated_points": result[
                "contradiction_share_of_eliminated_points"
            ],
            "historical_no_backtest_usable": False,
            "execution_enabled": False,
        }
    )


@app.command("sync-polymarket-status")
def sync_polymarket_status(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Persist official component-level status windows for default analysis exclusion."""

    async def fetch() -> object:
        client = PolymarketStatusClient()
        try:
            return await client.fetch()
        finally:
            await client.close()

    snapshot = asyncio.run(fetch())
    path = data_dir / "runtime" / "polymarket_quality_windows.json"
    windows = merge_quality_windows(
        merge_quality_windows(load_quality_windows(path), load_quality_overrides()),
        snapshot.windows,
    )
    persist_quality_windows(path, windows)
    _emit(
        {
            "quality_windows_path": str(path.resolve()),
            "page_status": snapshot.page_status,
            "upstream_maintenance": snapshot.upstream_maintenance,
            "active_market_data_windows": [
                row.as_json() for row in snapshot.active_market_data_windows
            ],
            "window_count": len(windows),
            "execution_enabled": False,
        }
    )


@app.command("audit-polymarket-maintenance")
def audit_polymarket_maintenance(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/polymarket_maintenance_audit.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/polymarket_maintenance_audit.json"
    ),
) -> None:
    """Attribute reconnects and archived rows to official CLOB status windows."""
    windows = load_quality_windows(
        data_dir / "runtime" / "polymarket_quality_windows.json"
    )
    market_windows = tuple(window for window in windows if window.affects_market_data)
    reconnects = audit_reconnect_rows(
        load_reconnect_rows(data_dir / "runtime" / "polymarket_ws_reconnects.jsonl"),
        market_windows,
    )
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "windows": [window.as_json() for window in market_windows],
        "reconnects": reconnects,
        "archives": {
            "full_market_stream": audit_archive_paths(
                jsonl_archive_paths(
                    data_dir / "raw" / "polymarket_clob_websocket"
                ),
                market_windows,
            ),
            "depth_checkpoints": audit_archive_paths(
                jsonl_archive_paths(
                    data_dir / "raw" / "polymarket_book_checkpoints"
                ),
                market_windows,
            ),
        },
        "execution_enabled": False,
    }
    render_maintenance_audit(result, output_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "officially_attributed_reconnect_count": reconnects[
                "officially_attributed_reconnect_count"
            ],
            "legacy_unattributed_reconnect_count": reconnects[
                "legacy_unattributed_reconnect_count"
            ],
            "execution_enabled": False,
        }
    )


@app.command("collect-public-trades")
def collect_public_trades(
    catalog_path: Annotated[Path, typer.Option("--catalog")] = Path(
        "data/settled_markets.json"
    ),
    histories_dir: Annotated[Path, typer.Option("--histories-dir")] = Path(
        "data/historical_prices"
    ),
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path(
        "data/public_trades"
    ),
    depth_dir: Annotated[Path, typer.Option("--depth-dir")] = Path(
        "data/raw/polymarket_book_checkpoints"
    ),
    cursor_path: Annotated[Path | None, typer.Option("--cursor")] = None,
    auto_discover: Annotated[
        bool,
        typer.Option(
            "--auto-discover/--catalog-only",
            help="Discover events/tokens from the depth archive before using the settled catalog.",
        ),
    ] = True,
) -> None:
    """Archive canonical public taker-side executions incrementally.

    When a depth archive is present, event/condition/token scope is discovered
    from it and a durable cursor prevents a full historical refetch.  The
    legacy settled-catalog mode remains available with ``--catalog-only``.
    """
    checkpoint_paths = jsonl_archive_paths(depth_dir) if auto_discover else []
    coverages = discover_depth_event_coverage(checkpoint_paths) if checkpoint_paths else ()
    if coverages:
        with PolymarketDataClient() as client:
            audit = collect_depth_event_trades(
                coverages,
                client=client,
                output_dir=output_dir,
                cursor_path=cursor_path,
            )
        _emit({**audit, "execution_enabled": False})
        return
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    histories = load_history_directory(histories_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    with PolymarketDataClient() as client:
        for event in catalog.get("events") or ():
            event_slug = str(event["event_slug"])
            history = histories[event_slug]
            timestamps = [
                int(point["t"])
                for market in history.get("markets") or ()
                for point in market.get("history") or ()
            ]
            if not timestamps:
                continue
            start = datetime.fromtimestamp(0, tz=UTC)
            end = datetime.fromtimestamp(max(timestamps), tz=UTC) + timedelta(days=1)
            trades = client.event_trades(
                event_id=str(event["event_id"]),
                start=start,
                end=end,
                taker_only=True,
            )
            payload = {
                "fetched_at": datetime.now(UTC).isoformat(),
                "source": "https://data-api.polymarket.com/trades",
                "event_id": str(event["event_id"]),
                "event_slug": event_slug,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "taker_only": True,
                "tape_semantics": (
                    "one taker-side row per execution; takerOnly=false is intentionally "
                    "avoided because participant mirrors cannot be losslessly deduplicated"
                ),
                "trade_count": len(trades),
                "trades": [trade.as_json() for trade in trades],
            }
            destination = output_dir / f"{event_slug}.json"
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            summaries.append(
                {
                    "event_slug": event_slug,
                    "trade_count": len(trades),
                    "path": str(destination.resolve()),
                }
            )
    _emit(
        {
            "event_count": len(summaries),
            "trade_count": sum(row["trade_count"] for row in summaries),
            "events": summaries,
            "execution_enabled": False,
        }
    )


@app.command("analyze-public-trades")
def analyze_public_trades(
    catalog_path: Annotated[Path, typer.Option("--catalog")] = Path(
        "data/settled_markets.json"
    ),
    histories_dir: Annotated[Path, typer.Option("--histories-dir")] = Path(
        "data/historical_prices"
    ),
    trades_dir: Annotated[Path, typer.Option("--trades-dir")] = Path(
        "data/public_trades"
    ),
    observations_dir: Annotated[Path, typer.Option("--observations-dir")] = Path(
        "data/hourly_obs"
    ),
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/public_trade_tape_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/public_trade_tape_analysis.json"
    ),
) -> None:
    """Measure prices-history staleness against actual prior public executions."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    stations = {str(event["station_id"]) for event in catalog.get("events") or ()}
    observations = {
        station: load_iem_asos_csv(
            observations_dir / f"{station}.csv", station_id=station
        )
        for station in stations
        if (observations_dir / f"{station}.csv").exists()
    }
    result = analyze_trade_tape_staleness(
        catalog,
        histories_by_event=load_history_directory(histories_dir),
        trades_by_event=load_event_trade_tapes(trades_dir),
        elimination_times=physical_elimination_times(catalog, observations),
    )
    render_trade_tape_report(result, output_path)
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "event_count": result["event_count"],
            "prices_history_sample_count": result["prices_history_sample_count"],
            "last_trade_age_p90_minutes": result["last_trade_age_p90_minutes"],
            "execution_enabled": False,
        }
    )


@app.command("audit-temperature-precision")
def audit_temperature_precision(
    start_date_text: Annotated[str, typer.Option("--start-date")] = "2024-06-01",
    end_date_text: Annotated[str, typer.Option("--end-date")] = "2026-08-20",
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/temperature_precision_audit.md"
    ),
) -> None:
    """Compare IEM tmpf with METAR remarks T-group precision for KLGA and ZUCK."""
    start_date = _parse_date(start_date_text, label="start date")
    end_date = _parse_date(end_date_text, label="end date")
    stations = (
        ("KLGA", "America/New_York"),
        ("ZUCK", "Asia/Shanghai"),
    )
    results = []
    for station_id, timezone in stations:
        rows = download_iem_precision_rows(
            station_id=station_id,
            timezone=timezone,
            start_date=start_date,
            end_date=end_date,
        )
        results.append(precision_comparison(rows, station_id=station_id))
    render_precision_audit(results, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "start_date": start_date,
            "end_date": end_date,
            "stations": results,
            "execution_enabled": False,
        }
    )


@app.command("analyze-real-no-books")
def analyze_real_no_book_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/real_no_book_report.md"
    ),
) -> None:
    """Analyze executable NO quotes from archived full-depth checkpoints."""
    paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    unfiltered_pairs = paired_book_snapshots(paths, exclude_upstream_degraded=False)
    pairs = paired_book_snapshots(paths)
    unfiltered_result = analyze_real_no_books(unfiltered_pairs)
    result = analyze_real_no_books(pairs)
    result["upstream_degraded_pairs_excluded"] = max(
        0, len(unfiltered_pairs) - len(pairs)
    )
    result["unfiltered_summary"] = {
        key: unfiltered_result[key]
        for key in (
            "paired_snapshot_count",
            "usable_snapshot_count",
            "complement_gap_max",
            "complement_gap_over_2c_rate",
            "complement_gap_over_2c_wilson_low",
            "complement_gap_over_2c_wilson_high",
            "mean_no_ask_minus_proxy",
            "mean_no_bid_minus_proxy",
            "size_summaries",
        )
    }
    render_real_no_report(result, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "paired_snapshot_count": result["paired_snapshot_count"],
            "usable_snapshot_count": result["usable_snapshot_count"],
            "complement_gap_max": result["complement_gap_max"],
            "proxy_comparable_count": result["proxy_comparable_count"],
            "historical_settled_depth_overlap_count": 0,
            "execution_enabled": False,
        }
    )


@app.command("analyze-no-entry-accessibility")
def analyze_no_entry_accessibility_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[Path, typer.Option("--warming-policy")] = (
        DEFAULT_WARMING_POLICY
    ),
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/no_entry_accessibility_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/no_entry_accessibility_analysis.json"
    ),
) -> None:
    """Explain high-NO entry failures using only real NO depth and public reads."""
    checkpoint_paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    unfiltered_pairs = paired_book_snapshots(
        checkpoint_paths, exclude_upstream_degraded=False
    )
    pairs = paired_book_snapshots(checkpoint_paths)
    registry = load_settlement_registry(config)
    metadata = archived_event_metadata(
        sorted({str(pair["event_slug"]) for pair in pairs}), registry.specs
    )
    policy_payload = json.loads(warming_policy_path.read_text(encoding="utf-8"))
    typical_peak_minutes = {
        station_id: int(seasons[0]["typical_peak_minutes"])
        for station_id, value in (policy_payload.get("stations") or {}).items()
        if isinstance(value, dict)
        and isinstance(seasons := value.get("seasons"), list)
        and seasons
        and isinstance(seasons[0], dict)
        and seasons[0].get("typical_peak_minutes") is not None
    }
    no_rows = [
        pair["no"]
        for pair in pairs
        if isinstance(pair.get("no"), dict) and pair["no"].get("asset_id")
    ]
    no_asset_ids = tuple(sorted({str(row["asset_id"]) for row in no_rows}))
    market_ids = tuple(sorted({str(row["market_id"]) for row in no_rows if row.get("market_id")}))
    quote_times = [
        timestamp
        for pair in pairs
        if (timestamp := _parse_aware_datetime(str(pair["no"]["_timestamp"]), label="NO book"))
    ]
    if not quote_times or not no_asset_ids or not market_ids:
        raise typer.BadParameter("no eligible NO book records found in checkpoints")
    public_start = min(quote_times) - timedelta(days=7)
    public_end = max(quote_times) + timedelta(seconds=1)
    price_points_by_asset = {asset_id: [] for asset_id in no_asset_ids}
    with ClobClient() as clob:
        for start in range(0, len(no_asset_ids), 20):
            batch = clob.batch_price_history(
                token_ids=no_asset_ids[start : start + 20],
                start=public_start,
                end=public_end,
                fidelity_minutes=1,
            )
            for series in batch.series:
                price_points_by_asset[series.token_id] = list(series.points)
    trades_by_asset: dict[str, list[Any]] = {asset_id: [] for asset_id in no_asset_ids}
    with PolymarketDataClient() as trade_client:
        for start in range(0, len(market_ids), 20):
            for trade in trade_client.market_trades(
                market_ids=market_ids[start : start + 20],
                start=public_start,
                end=public_end,
                taker_only=True,
            ):
                if trade.asset_id in trades_by_asset:
                    trades_by_asset[trade.asset_id].append(trade)
    rule_targets = current_tail_rule_targets(
        pairs, event_metadata=metadata, as_of=datetime.now(UTC)
    )
    books_by_asset = {}
    rule_errors: dict[str, str] = {}
    with ClobClient() as clob:
        for target in rule_targets:
            asset_id = str(target["asset_id"])
            try:
                books_by_asset[asset_id] = clob.order_book(token_id=asset_id)
            except (httpx.HTTPError, ValueError) as exc:
                rule_errors[asset_id] = f"{type(exc).__name__}: {exc}"
    rules = current_rule_rows(
        rule_targets, books_by_asset=books_by_asset, errors_by_asset=rule_errors
    )
    result = analyze_no_entry_accessibility(
        pairs,
        event_metadata=metadata,
        typical_peak_minutes_by_station=typical_peak_minutes,
        price_points_by_asset=price_points_by_asset,
        trades_by_asset=trades_by_asset,
        current_rules=rules,
    )
    result["quality_window_pairs_excluded"] = max(0, len(unfiltered_pairs) - len(pairs))
    result["public_source_window"] = {
        "start": public_start.isoformat(),
        "end": public_end.isoformat(),
        "price_history_fidelity_minutes": 1,
        "price_history_asset_count": len(no_asset_ids),
        "trade_market_count": len(market_ids),
        "taker_only": True,
    }
    render_no_entry_accessibility_report(result, output_path)
    serializable = {key: value for key, value in result.items() if key != "records"}
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "tail_book_snapshot_count": result["tail_book_snapshot_count"],
            "quality_window_pairs_excluded": result["quality_window_pairs_excluded"],
            "execution_enabled": False,
        }
    )


@app.command("analyze-price-band-accessibility")
def analyze_price_band_accessibility_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[Path, typer.Option("--warming-policy")] = (
        DEFAULT_WARMING_POLICY
    ),
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/price_band_accessibility_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/price_band_accessibility_analysis.json"
    ),
) -> None:
    """Compare true NO ask bands with the equivalent YES-sell depth path."""
    checkpoint_paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    unfiltered_pairs = paired_book_snapshots(
        checkpoint_paths, exclude_upstream_degraded=False
    )
    pairs = paired_book_snapshots(checkpoint_paths)
    if not pairs:
        raise typer.BadParameter("no eligible paired order-book records found in checkpoints")
    registry = load_settlement_registry(config)
    metadata = archived_event_metadata(
        sorted({str(pair["event_slug"]) for pair in pairs}), registry.specs
    )
    policy_payload = json.loads(warming_policy_path.read_text(encoding="utf-8"))
    typical_peak_minutes = {
        station_id: int(seasons[0]["typical_peak_minutes"])
        for station_id, value in (policy_payload.get("stations") or {}).items()
        if isinstance(value, dict)
        and isinstance(seasons := value.get("seasons"), list)
        and seasons
        and isinstance(seasons[0], dict)
        and seasons[0].get("typical_peak_minutes") is not None
    }
    result = analyze_price_band_accessibility(
        pairs,
        event_metadata=metadata,
        typical_peak_minutes_by_station=typical_peak_minutes,
    )
    result["quality_window_pairs_excluded"] = max(
        0, len(unfiltered_pairs) - len(pairs)
    )
    result["quality_window"] = (
        "official maintenance/failure plus measured recovery windows loaded by paired_book_snapshots"
    )
    render_price_band_accessibility_report(result, output_path)
    serializable = {key: value for key, value in result.items() if key != "records"}
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "paired_snapshot_count": result["paired_snapshot_count"],
            "analyzable_snapshot_count": result["analyzable_snapshot_count"],
            "quality_window_pairs_excluded": result["quality_window_pairs_excluded"],
            "data_cutoff": result["data_cutoff"],
            "execution_enabled": False,
        }
    )


@app.command("analyze-price-paths")
def analyze_price_paths_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[Path, typer.Option("--warming-policy")] = (
        DEFAULT_WARMING_POLICY
    ),
    settled_catalog_path: Annotated[
        Path, typer.Option("--settled-catalog", help="Settled event catalog for overlap accounting.")
    ] = Path("data/settled_markets.json"),
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/price_path_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/price_path_analysis.json"
    ),
) -> None:
    """Measure target price paths after fully executable $200 NO entries."""
    checkpoint_paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    unfiltered_pair_count = len(
        paired_book_snapshots(
            checkpoint_paths, exclude_upstream_degraded=False
        )
    )
    pairs = paired_book_snapshots(checkpoint_paths)
    pairs = compact_no_book_pairs(pairs)
    if not pairs:
        raise typer.BadParameter("no eligible paired order-book records found in checkpoints")
    registry = load_settlement_registry(config)
    metadata = archived_event_metadata(
        sorted({str(pair["event_slug"]) for pair in pairs}), registry.specs
    )
    station_ids = sorted({str(value["station_id"]) for value in metadata.values()})
    weather_paths = jsonl_archive_paths(data_dir / "raw" / "weather_daemon")
    observations = load_archived_weather_observations(
        weather_paths, station_ids=station_ids
    )
    policy_payload = json.loads(warming_policy_path.read_text(encoding="utf-8"))
    typical_peak_minutes = {
        station_id: int(seasons[0]["typical_peak_minutes"])
        for station_id, value in (policy_payload.get("stations") or {}).items()
        if isinstance(value, dict)
        and isinstance(seasons := value.get("seasons"), list)
        and seasons
        and isinstance(seasons[0], dict)
        and seasons[0].get("typical_peak_minutes") is not None
    }
    settled_slugs: list[str] = []
    settlement_ends: dict[str, datetime] = {}
    if settled_catalog_path.exists():
        settled_payload = json.loads(settled_catalog_path.read_text(encoding="utf-8"))
        for event in settled_payload.get("events") or ():
            event_slug = str(event.get("event_slug") or "")
            if not event_slug:
                continue
            settled_slugs.append(event_slug)
            end_at = _parse_aware_datetime(str(event.get("end_date")), label="settlement end") if event.get("end_date") else None
            if end_at is not None:
                settlement_ends[event_slug] = end_at
    result = analyze_price_paths(
        pairs,
        event_metadata=metadata,
        observations_by_station=observations,
        typical_peak_minutes_by_station=typical_peak_minutes,
        settled_event_slugs=settled_slugs,
        settlement_end_by_event=settlement_ends,
        entry_bands_by_station={
            station: (
                (PRIMARY_BANDS[station],)
                if station in PRIMARY_BANDS
                else CONTROL_BANDS
            )
            for station in station_ids
        },
    )
    result["quality_window_pairs_excluded"] = max(
        0, unfiltered_pair_count - result["paired_snapshot_count"]
    )
    result["quality_window"] = (
        "official maintenance/failure plus measured recovery windows loaded by paired_book_snapshots"
    )
    render_price_path_report(result, output_path)
    serializable = {key: value for key, value in result.items() if key != "records"}
    analysis_path.parent.mkdir(parents=True, exist_ok=True)
    analysis_path.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "paired_snapshot_count": result["paired_snapshot_count"],
            "usable_entry_count": result["usable_entry_count"],
            "settled_depth_event_count": result["settlement_overlap"][
                "settled_depth_event_count"
            ],
            "quality_window_pairs_excluded": result["quality_window_pairs_excluded"],
            "data_cutoff": result["data_cutoff"],
            "execution_enabled": False,
        }
    )


@app.command("analyze-shadow-spread")
def analyze_shadow_spread_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[
        Path, typer.Option("--warming-policy")
    ] = DEFAULT_WARMING_POLICY,
    strategy_config_path: Annotated[
        Path | None, typer.Option("--strategy-config", help="Versioned shadow strategy JSON.")
    ] = None,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/shadow_spread_strategy_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/shadow_spread_strategy_analysis.json"
    ),
    global_capital_usd: Annotated[str, typer.Option("--global-capital-usd")] = "200",
) -> None:
    """Replay finite post-only shadow orders; never submits an order."""
    checkpoint_paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    pairs = paired_book_snapshots(checkpoint_paths)
    if not pairs:
        raise typer.BadParameter("no eligible paired order-book records found in checkpoints")
    registry = load_settlement_registry(config)
    metadata = archived_event_metadata(
        sorted({str(pair["event_slug"]) for pair in pairs}), registry.specs
    )
    if strategy_config_path is not None:
        strategy_payload = json.loads(strategy_config_path.read_text(encoding="utf-8"))
        strategy = ShadowStrategyConfig.from_mapping(strategy_payload)
    else:
        strategy = default_shadow_strategy_config()
    policy_versions: dict[tuple[str, str], str] = {}
    policy_windows: dict[tuple[str, str], bool] = {}
    if warming_policy_path.exists():
        policy_payload = json.loads(warming_policy_path.read_text(encoding="utf-8"))
        for event_value in metadata.values():
            station = str(event_value.get("station_id") or "")
            target = str(event_value.get("target_date") or "")
            seasons = (policy_payload.get("stations") or {}).get(station, {}).get("seasons", ())
            for season in seasons:
                start = str(season.get("window_start") or "")
                end = str(season.get("window_end") or "")
                if start and end and start <= target <= end:
                    policy_versions[(station, target)] = str(
                        season.get("threshold_version") or policy_payload.get("policy_version") or ""
                    )
                    policy_windows[(station, target)] = True
                    break
    replay_pairs: list[dict[str, object]] = []
    for pair in pairs:
        event_value = metadata.get(str(pair.get("event_slug") or ""), {})
        station = str(event_value.get("station_id") or "")
        target = str(event_value.get("target_date") or "")
        enriched = dict(pair)
        enriched["station_id"] = station or None
        enriched["market_day"] = target or None
        if (version := policy_versions.get((station, target))):
            enriched["season_version"] = version
            enriched["in_season"] = policy_windows[(station, target)]
        replay_pairs.append(enriched)
    weather_paths = jsonl_archive_paths(data_dir / "raw" / "weather_daemon")
    weather_observations = load_realtime_weather_observations(weather_paths)
    replay_pairs, weather_join_reasons = align_weather_to_snapshots(
        replay_pairs, weather_observations
    )
    trades_dir = data_dir / "public_trades"
    public_trade_rows = load_event_trade_tapes(trades_dir) if trades_dir.exists() else {}
    public_trade_values = [trade for rows in public_trade_rows.values() for trade in rows]
    ws_trade_result = load_market_ws_trades(
        jsonl_archive_paths(data_dir / "raw" / "polymarket_clob_websocket"),
        quality_windows=load_quality_windows(data_dir / "runtime" / "polymarket_quality_windows.json"),
    )
    trade_events, trade_source_validation = build_shadow_trade_events(
        ws_trade_result.trades, public_trade_values
    )
    settled_slugs: list[str] = []
    settled_catalog = data_dir / "settled_markets.json"
    if settled_catalog.exists():
        payload = json.loads(settled_catalog.read_text(encoding="utf-8"))
        settled_slugs = [
            str(row["event_slug"])
            for row in payload.get("events") or ()
            if row.get("event_slug")
        ]
    result = replay_shadow_spread(
        replay_pairs,
        trades=trade_events,
        event_metadata=metadata,
        config=strategy,
        global_capital_usd=Decimal(global_capital_usd),
        settled_event_slugs=settled_slugs,
    )
    result["quality_window"] = (
        "paired_book_snapshots default exclusion: official maintenance/failure plus measured recovery"
    )
    result["weather_join"] = {
        "observation_count": len(weather_observations),
        "reason_counts": weather_join_reasons,
        "strict_cutoff": "source_timestamp <= snapshot_at and received_at <= snapshot_at",
        "historical_backfill_used": False,
    }
    ws_summary = ws_trade_result.as_json()
    ws_summary.pop("trades", None)
    result["trade_sources"] = {
        "market_ws": ws_summary | {
            "queue_trade_events": trade_source_validation["ws_shadow_trade_event_count"]
        },
        "data_api": {
            "trade_count": len(public_trade_values),
            "queue_trade_events": trade_source_validation["data_api_shadow_trade_event_count"],
        },
        "side_validation": trade_source_validation,
        "cross_source_dedupe": "transaction_hash first; same-second rows without sequence are skipped",
    }
    render_shadow_spread_report(result, output_path)
    write_shadow_spread_result(result, analysis_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "independent_market_day_count": result["independent_market_day_count"],
            "shadow_order_count": result["models"]["queue_aware"]["shadow_order_count"],
            "fill_count": result["models"]["queue_aware"]["fill_count"],
            "execution_enabled": False,
        }
    )


@app.command("shadow-spread-engine")
def shadow_spread_engine_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    ledger_path: Annotated[Path, typer.Option("--ledger")] = Path(
        "data/raw/shadow_orders/shadow_orders_v2_token_scoped.jsonl"
    ),
    status_path: Annotated[Path, typer.Option("--status")] = Path(
        "data/runtime/shadow_spread_status_v2_token_scoped.json"
    ),
    strategy_config_path: Annotated[
        Path | None, typer.Option("--strategy-config")
    ] = None,
    supervised: Annotated[
        bool, typer.Option("--supervised", help="Required explicit read-only supervision flag.")
    ] = False,
    once: Annotated[
        bool,
        typer.Option("--once", help="Run one finite archive pass instead of the continuous follower."),
    ] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds")] = 5.0,
    runtime_seconds: Annotated[float, typer.Option("--runtime")] = 0.0,
    cursor_path: Annotated[Path, typer.Option("--cursor")] = Path(
        "data/runtime/shadow_spread_cursor_v2_token_scoped.json"
    ),
    replay_existing: Annotated[
        bool,
        typer.Option(
            "--replay-existing",
            help="Explicitly replay existing archives; the continuous default begins at current tails.",
        ),
    ] = False,
) -> None:
    """Run a supervised, read-only shadow follower (or explicit ``--once`` pass)."""
    if not supervised:
        raise typer.BadParameter("--supervised is required; this command never executes orders")
    if once:
        status = run_shadow_spread_once(
            data_dir=data_dir,
            ledger_path=ledger_path,
            status_path=status_path,
            config_path=str(strategy_config_path) if strategy_config_path else None,
            supervised=True,
        )
    else:
        status = run_shadow_spread_continuous(
            data_dir=data_dir,
            ledger_path=ledger_path,
            status_path=status_path,
            cursor_path=cursor_path,
            config_path=str(strategy_config_path) if strategy_config_path else None,
            supervised=True,
            poll_seconds=poll_seconds,
            runtime_seconds=runtime_seconds,
            bootstrap_at_tail=not replay_existing,
        )
    _emit({**status, "status_path": str(status_path.resolve()), "execution_enabled": False})


@app.command("analyze-shadow-complement-pairs")
def analyze_shadow_complement_pairs_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[
        Path, typer.Option("--warming-policy")
    ] = DEFAULT_WARMING_POLICY,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/complement_pair_strategy_report.md"
    ),
    analysis_path: Annotated[Path, typer.Option("--analysis-output")] = Path(
        "data/complement_pair_strategy_analysis.json"
    ),
    sensitivity_path: Annotated[Path, typer.Option("--sensitivity-output")] = Path(
        "data/complement_pair_strategy_sensitivity.json"
    ),
    sensitivity_grid: Annotated[
        bool,
        typer.Option("--sensitivity-grid/--no-sensitivity-grid"),
    ] = True,
) -> None:
    """Replay isolated YES+NO maker pairs; it never creates an exchange order."""
    checkpoint_paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    event_slugs: set[str] = set()
    target_asset_ids: set[str] = set()
    paired_snapshot_count = 0
    # Freeze one analysis vintage before any of the multiple sensitivity passes
    # begin. The live checkpoint collector may append while this command runs.
    analysis_cutoff = datetime.now(UTC)
    # First pass retains only identities.  A full paired book has real depth
    # on two sides, so holding every historical pair in memory is unnecessary
    # and unsafe on a live collector host.
    for pair in iter_paired_book_snapshots(checkpoint_paths):
        observed_at = pair.get("observed_at")
        if isinstance(observed_at, datetime) and observed_at > analysis_cutoff:
            break
        paired_snapshot_count += 1
        event_slugs.add(str(pair["event_slug"]))
        for side in (pair.get("yes"), pair.get("no")):
            if isinstance(side, dict) and side.get("asset_id"):
                target_asset_ids.add(str(side["asset_id"]))
    if not paired_snapshot_count:
        raise typer.BadParameter("no eligible paired order-book records found in checkpoints")
    registry = load_settlement_registry(config)
    metadata = archived_event_metadata(sorted(event_slugs), registry.specs)
    policy_versions: dict[tuple[str, str], str] = {}
    policy_windows: dict[tuple[str, str], bool] = {}
    if warming_policy_path.exists():
        policy_payload = json.loads(warming_policy_path.read_text(encoding="utf-8"))
        for event_value in metadata.values():
            station = str(event_value.get("station_id") or "")
            target = str(event_value.get("target_date") or "")
            for season in (policy_payload.get("stations") or {}).get(station, {}).get(
                "seasons", ()
            ):
                start = str(season.get("window_start") or "")
                end = str(season.get("window_end") or "")
                if start and end and start <= target <= end:
                    policy_versions[(station, target)] = str(
                        season.get("threshold_version")
                        or policy_payload.get("policy_version")
                        or ""
                    )
                    policy_windows[(station, target)] = True
                    break
    def replay_pair_stream() -> Any:
        for pair in iter_paired_book_snapshots(checkpoint_paths):
            observed_at = pair.get("observed_at")
            if isinstance(observed_at, datetime) and observed_at > analysis_cutoff:
                break
            event_value = metadata.get(str(pair.get("event_slug") or ""), {})
            station = str(event_value.get("station_id") or "")
            target = str(event_value.get("target_date") or "")
            yield {
                **pair,
                "station_id": station or None,
                "market_day": target or None,
                "season_version": policy_versions.get((station, target), "unknown"),
                "in_season": policy_windows.get((station, target), False),
            }

    public_trade_rows = (
        load_event_trade_tapes(data_dir / "public_trades")
        if (data_dir / "public_trades").exists()
        else {}
    )
    public_trade_values = [
        trade
        for rows in public_trade_rows.values()
        for trade in rows
        if trade.asset_id in target_asset_ids
    ]
    ws_trade_result = load_market_ws_trades(
        jsonl_archive_paths(data_dir / "raw" / "polymarket_clob_websocket"),
        quality_windows=load_quality_windows(
            data_dir / "runtime" / "polymarket_quality_windows.json"
        ),
        asset_ids=target_asset_ids,
    )
    trade_events, trade_source_validation = build_shadow_trade_events(
        ws_trade_result.trades, public_trade_values
    )
    selected_config = default_complement_pair_config()
    scenarios = predefined_complement_pair_configs(selected_config)
    scenario_results: list[dict[str, Any]] = []
    if sensitivity_grid:
        # The full grid is a fixed 24-config diagnostic.  A single shared
        # archive scan avoids moving the vintage boundary between scenarios;
        # observed memory for all 24 configs is bounded by the read-only
        # replay summaries (well below the host's available memory).  Keep the
        # batch expression explicit so a constrained host can lower it without
        # changing the predeclared scenario set.
        batch_size = 24
        for start in range(0, len(scenarios), batch_size):
            scenario_results.extend(
                replay_complement_pairs_streaming_grid(
                    replay_pair_stream(),
                    trades=trade_events,
                    configs=scenarios[start : start + batch_size],
                )
            )
        result = scenario_results[
            next(index for index, scenario in enumerate(scenarios) if scenario == selected_config)
        ]
    else:
        result = replay_complement_pairs_streaming(
            replay_pair_stream(), trades=trade_events, config=selected_config
        )
    result["quality_window"] = (
        "paired_book_snapshots default exclusion: official maintenance/failure plus measured recovery"
    )
    result["analysis_cutoff"] = analysis_cutoff.isoformat()
    result["trade_sources"] = {
        "target_asset_count": len(target_asset_ids),
        "market_ws_trade_count": len(ws_trade_result.trades),
        "market_ws_filtered_other_asset_count": ws_trade_result.filtered_asset_count,
        "data_api_trade_count": len(public_trade_values),
        "side_validation": trade_source_validation,
        "cross_source_dedupe": "transaction_hash first; same-second rows without sequence are skipped",
    }
    render_complement_pair_report(result, output_path)
    write_complement_pair_result(result, analysis_path)
    sensitivity: list[dict[str, object]] = []
    if sensitivity_grid:
        for scenario_result in scenario_results:
            scenario = scenario_result["strategy"]
            sensitivity.append(
                {
                    "strategy": scenario,
                    "touch": scenario_result["models"]["touch"],
                    "queue_aware": scenario_result["models"]["queue_aware"],
                    "trade_through": scenario_result["models"]["trade_through"],
                    "execution_enabled": False,
                }
            )
        sensitivity_path.parent.mkdir(parents=True, exist_ok=True)
        sensitivity_path.write_text(
            json.dumps(sensitivity, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    queue_model = result["models"]["queue_aware"]
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "analysis_path": str(analysis_path.resolve()),
            "sensitivity_path": str(sensitivity_path.resolve()) if sensitivity_grid else None,
            "pair_candidate_count": queue_model["pair_candidate_count"],
            "submitted_pair_count": queue_model["submitted_pair_count"],
            "completed_pair_count": queue_model["completed_pair_count"],
            "execution_enabled": False,
        }
    )


@app.command("analyze-eliminated-no-exit")
def analyze_eliminated_no_exit_command(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/eliminated_no_exit_report.md"
    ),
) -> None:
    """Measure real NO bid exit depth after irreversible physical elimination."""
    registry = load_settlement_registry(config)
    paths = jsonl_archive_paths(data_dir / "raw" / "polymarket_book_checkpoints")
    unfiltered_pairs = paired_book_snapshots(paths, exclude_upstream_degraded=False)
    pairs = paired_book_snapshots(paths)
    archived_slugs = sorted({str(pair["event_slug"]) for pair in unfiltered_pairs})
    all_metadata = archived_event_metadata(archived_slugs, registry.specs)
    metadata = {
        slug: value
        for slug, value in all_metadata.items()
        if value["station_id"] in {"KLAX", "KLGA"}
    }
    dates_by_station: dict[str, list[date]] = {}
    timezone_by_station: dict[str, str] = {}
    for value in metadata.values():
        station_id = value["station_id"]
        dates_by_station.setdefault(station_id, []).append(
            date.fromisoformat(value["target_date"])
        )
        timezone_by_station[station_id] = value["timezone"]
    observations = {}
    for station_id, targets in dates_by_station.items():
        rows = download_iem_precision_rows(
            station_id=station_id,
            timezone=timezone_by_station[station_id],
            start_date=min(targets),
            end_date=max(targets) + timedelta(days=1),
        )
        observations[station_id] = temperature_observations_from_precision_rows(
            rows, station_id=station_id
        )
    unfiltered_result = analyze_eliminated_no_exit(
        unfiltered_pairs,
        event_metadata=metadata,
        observations_by_station=observations,
    )
    result = analyze_eliminated_no_exit(
        pairs,
        event_metadata=metadata,
        observations_by_station=observations,
    )
    result["upstream_degraded_pairs_excluded"] = max(
        0, len(unfiltered_pairs) - len(pairs)
    )
    result["unfiltered_eliminated_market_count"] = unfiltered_result[
        "eliminated_market_count"
    ]
    result["unfiltered_summary"] = {
        key: unfiltered_result[key]
        for key in (
            "eliminated_market_count",
            "bid_0_95_reached_rate",
            "bid_0_98_reached_rate",
            "depth_within_15m_coverage_rate",
            "sell_200_at_0_97_within_15m_rate",
        )
    }
    render_eliminated_exit_report(result, output_path)
    _emit(
        {
            "report_path": str(output_path.resolve()),
            "eliminated_market_count": result["eliminated_market_count"],
            "stations": result["stations"],
            "archived_event_count": len(archived_slugs),
            "matched_scope_event_count": len(metadata),
            "settled_event_count": 0,
            "partial_forward_day": True,
            "execution_enabled": False,
        }
    )


@app.command("no-forward-report")
def no_forward_report(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[
        Path, typer.Option("--warming-policy")
    ] = DEFAULT_WARMING_POLICY,
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Markdown report destination."),
    ] = Path("data/no_forward_validation_report.md"),
) -> None:
    """Summarize real-book forward NO triggers without execution or p proxies."""
    registry = load_settlement_registry(config)
    warming_policy = WarmingThresholdRegistry.from_path(warming_policy_path)
    station_timezones = {
        str(spec.station_id): spec.timezone
        for spec in registry.specs
        if spec.station_id and spec.timezone
    }
    summary = forward_summary(
        data_dir,
        warming_policy=warming_policy,
        station_timezones=station_timezones,
    )
    render_forward_report(summary, output_path)
    _emit({**summary, "report_path": str(output_path.resolve())})


@app.command("wrh-backfill")
def wrh_backfill(
    settlement_keys: Annotated[
        list[str], typer.Argument(help="One or more settlement registry entry keys.")
    ],
    start_date_text: Annotated[
        str | None,
        typer.Option("--start-date", help="First local date (YYYY-MM-DD)."),
    ] = None,
    end_date_text: Annotated[
        str | None,
        typer.Option("--end-date", help="Last local date (YYYY-MM-DD)."),
    ] = None,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    output_path: Annotated[Path, typer.Option("--output")] = Path(
        "data/wrh_backfill_report.md"
    ),
) -> None:
    """Batch WRH history into an isolated post-hoc QC archive and database."""
    if bool(start_date_text) != bool(end_date_text):
        typer.echo("Provide both --start-date and --end-date, or neither.", err=True)
        raise typer.Exit(code=2)
    explicit_dates = start_date_text is not None and end_date_text is not None
    if explicit_dates:
        try:
            requested_start = date.fromisoformat(str(start_date_text))
            requested_end = date.fromisoformat(str(end_date_text))
        except ValueError as exc:
            typer.echo("Dates must use YYYY-MM-DD format.", err=True)
            raise typer.Exit(code=2) from exc
        if requested_end < requested_start:
            typer.echo("End date cannot precede start date.", err=True)
            raise typer.Exit(code=2)
        if (requested_end - requested_start).days + 1 > 30:
            typer.echo("WRH backfill cannot exceed 30 calendar days.", err=True)
            raise typer.Exit(code=2)
    registry = load_settlement_registry(config)
    requests: list[WrhBackfillRequest] = []
    for settlement_key in settlement_keys:
        try:
            spec = registry.by_key(settlement_key)
        except KeyError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        if not spec.station_id or not spec.timezone:
            typer.echo(
                f"Registry entry {spec.key!r} needs station_id and timezone.",
                err=True,
            )
            raise typer.Exit(code=2)
        if explicit_dates:
            start_date = requested_start
            end_date = requested_end
        else:
            previous_local_day = datetime.now(ZoneInfo(spec.timezone)).date() - timedelta(
                days=1
            )
            start_date = previous_local_day
            end_date = previous_local_day
        requests.append(
            WrhBackfillRequest(
                station_id=spec.station_id,
                timezone=spec.timezone,
                start_date=start_date,
                end_date=end_date,
            )
        )
    try:
        result = asyncio.run(backfill_wrh_history(requests, data_dir=data_dir))
    except (httpx.HTTPError, ValueError) as exc:
        typer.echo(f"WRH backfill failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    render_wrh_backfill_report(result, output_path)
    _emit({**result, "report_path": str(output_path.resolve())})


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
    ] = 120,
    metar_interval_seconds: Annotated[
        float,
        typer.Option("--metar-interval", min=60, max=7200),
    ] = 900,
    international_observation_interval_seconds: Annotated[
        float,
        typer.Option("--international-observation-interval", min=60, max=7200),
    ] = 1800,
    taf_interval_seconds: Annotated[
        float,
        typer.Option("--taf-interval", min=600, max=7200),
    ] = 3600,
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
            nws_api_enabled=spec.nws_api_enabled,
            open_meteo_enabled=spec.open_meteo_enabled,
        )
        for spec in specs
    ]
    model_weights_by_station: dict[str, dict[str, float]] = {}
    warehouse_path = data_dir / "research.duckdb"
    if warehouse_path.exists():
        with ResearchWarehouse(warehouse_path) as warehouse:
            for station in stations:
                samples = warehouse.samples(
                    station_id=station.station_id,
                    lead_days=1,
                    model="multi_model_blend",
                )
                if samples and all(sample.forecast_high_f_by_model for sample in samples):
                    model_weights_by_station[station.station_id] = learn_model_weights(samples)
    for station in stations:
        model_weights_by_station.setdefault(
            station.station_id,
            dict(DEFAULT_MULTI_MODEL_WEIGHTS),
        )
    daemon = WeatherDaemon(
        stations=stations,
        data_dir=data_dir,
        observation_interval_seconds=observation_interval_seconds,
        metar_interval_seconds=metar_interval_seconds,
        international_observation_interval_seconds=(
            international_observation_interval_seconds
        ),
        taf_interval_seconds=taf_interval_seconds,
        forecast_interval_seconds=forecast_interval_seconds,
        model_weights_by_station=model_weights_by_station,
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
            "model_weights_by_station": model_weights_by_station,
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
        ("supervisor", "market_supervisor_status.json"),
        ("weather", "weather_daemon_status.json"),
        ("signal", "signal_engine_status.json"),
        # v1 used a station-day-wide inventory scope and is evidence only.
        # Keep it out of the live status surface so an operator cannot mistake
        # it for the token-scoped read-only shadow daemon.
        ("shadow", "shadow_spread_status_v2_token_scoped.json"),
    ):
        path = data_dir / "runtime" / filename
        if not path.exists():
            statuses[name] = {"state": "not_started", "status_path": str(path.resolve())}
            continue
        try:
            statuses[name] = _read_json_with_retry(path)
            statuses[name]["status_path"] = str(path.resolve())
            updated_text = statuses[name].get("updated_at")
            if updated_text:
                updated_at = datetime.fromisoformat(str(updated_text)).astimezone(UTC)
                age_seconds = (datetime.now(UTC) - updated_at).total_seconds()
                stale_after = 300 if name == "weather" else 120
                if age_seconds > stale_after:
                    statuses[name]["reported_state"] = statuses[name].get("state")
                    statuses[name]["state"] = "stale"
                    statuses[name]["stale_age_seconds"] = round(age_seconds, 1)
        except (OSError, json.JSONDecodeError) as exc:
            statuses[name] = {
                "state": "unreadable",
                "error": str(exc),
                "status_path": str(path.resolve()),
            }
    legacy_shadow_status = data_dir / "runtime" / "shadow_spread_status_v1_legacy_read_only.json"
    statuses["shadow"]["legacy_status_path"] = str(legacy_shadow_status.resolve())
    statuses["shadow"]["legacy_status"] = "superseded_v1_read_only"
    _emit(statuses)


@app.command("migrate-signal-schema")
def migrate_signal_schema(
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
    target_path: Annotated[
        Path | None,
        typer.Option("--target", help="Independent candidate DuckDB path."),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace an existing candidate, never the online DB."),
    ] = False,
) -> None:
    """Vectorize all raw signal JSONL into the normalized candidate schema."""
    source_root = data_dir / "raw" / "signal_snapshot"
    paths = sorted(
        (*source_root.glob("*/events.jsonl"), *source_root.glob("*/events.jsonl.gz"))
    )
    destination = target_path or data_dir / "signal_stream.candidate.duckdb"
    report = migrate_signal_snapshots_from_jsonl(
        paths,
        target_path=destination,
        source_database=data_dir / "signal_stream.duckdb",
        replace=replace,
        protected_tree=data_dir / "raw" / "no_forward_validation",
    )
    _emit(report)


def _build_signal_configs(
    parsed_specs: list[tuple[str, str]],
    *,
    registry: Any,
    data_dir: Path,
    calibration_lead_days: int,
    min_calibration_samples: int,
    expected_sha_by_slug: dict[str, str] | None = None,
) -> tuple[LiveSignalConfig, ...]:
    configs: list[LiveSignalConfig] = []
    with GammaClient() as gamma:
        for event_slug, settlement_key in parsed_specs:
            spec = registry.by_key(settlement_key)
            if not spec.station_id or not spec.timezone:
                raise ValueError(f"Settlement {spec.key!r} lacks station or timezone")
            event = _event_by_slug_with_retry(gamma, event_slug)
            evidence = parse_settlement_evidence(event, registry_spec=spec)
            if evidence.target_date is None:
                raise ValueError(f"Could not parse target date for {event_slug!r}")
            expected_sha = (expected_sha_by_slug or {}).get(event_slug)
            if expected_sha is not None and evidence.evidence_sha256 != expected_sha:
                # A resolved/edited prior-day event must fail closed without
                # preventing every other independently verified city from loading.
                typer.echo(
                    f"Skipping {event_slug!r}: settlement evidence changed",
                    err=True,
                )
                continue
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
                            model="multi_model_blend",
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
    return tuple(configs)


@app.command("signal-engine")
def signal_engine(
    market_specs: Annotated[
        list[str] | None,
        typer.Option(
            "--market",
            help="Repeat EVENT_SLUG=SETTLEMENT_KEY for each monitored city.",
        ),
    ] = None,
    supervised: Annotated[
        bool,
        typer.Option(
            "--supervised",
            help="Load verified events from market-supervisor and hot-reload generations.",
        ),
    ] = False,
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
            min=1,
            max=7,
            help="Historical forecast lead used by the live credibility gate.",
        ),
    ] = 1,
    min_calibration_samples: Annotated[
        int,
        typer.Option(
            "--min-calibration-samples",
            min=2,
            help="Minimum training history before walk-forward validation.",
        ),
    ] = 30,
    config: Annotated[Path, typer.Option("--config")] = DEFAULT_CONFIG,
    warming_policy_path: Annotated[
        Path, typer.Option("--warming-policy")
    ] = DEFAULT_WARMING_POLICY,
    data_dir: Annotated[Path, typer.Option()] = DEFAULT_DATA_DIR,
) -> None:
    """Run deterministic live probability and read-only edge calculations."""
    registry = load_settlement_registry(config)
    try:
        warming_policy = WarmingThresholdRegistry.from_path(warming_policy_path)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        typer.echo(f"Cannot load warming-window policy: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    update_path = data_dir / "runtime" / "signal_config_update.json"
    parsed_specs: list[tuple[str, str]] = []
    expected_sha_by_slug: dict[str, str] | None = None
    initial_generation: int | None = None
    for value in market_specs or ():
        event_slug, separator, settlement_key = value.partition("=")
        if not separator or not event_slug or not settlement_key:
            typer.echo(f"Invalid --market value: {value!r}", err=True)
            raise typer.Exit(code=2)
        parsed_specs.append((event_slug, settlement_key))
    if supervised:
        try:
            update_payload = _read_json_with_retry(update_path)
            event_rows = update_payload["events"]
            if not isinstance(event_rows, list) or not event_rows:
                raise ValueError("supervisor config contains no verified events")
            parsed_specs = [
                (str(item["event_slug"]), str(item["settlement_key"]))
                for item in event_rows
            ]
            expected_sha_by_slug = {
                str(item["event_slug"]): str(item["evidence_sha256"])
                for item in event_rows
            }
            initial_generation = int(update_payload["generation"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            typer.echo(f"Cannot load supervisor config: {exc}", err=True)
            raise typer.Exit(code=2) from exc
    if not parsed_specs:
        typer.echo("Provide --market or use --supervised.", err=True)
        raise typer.Exit(code=2)
    try:
        configs = _build_signal_configs(
            parsed_specs,
            registry=registry,
            data_dir=data_dir,
            calibration_lead_days=calibration_lead_days,
            min_calibration_samples=min_calibration_samples,
            expected_sha_by_slug=expected_sha_by_slug,
        )
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    def load_supervisor_events(events: list[dict[str, Any]]) -> tuple[LiveSignalConfig, ...]:
        pairs = [(str(item["event_slug"]), str(item["settlement_key"])) for item in events]
        expected = {
            str(item["event_slug"]): str(item["evidence_sha256"]) for item in events
        }
        return _build_signal_configs(
            pairs,
            registry=registry,
            data_dir=data_dir,
            calibration_lead_days=calibration_lead_days,
            min_calibration_samples=min_calibration_samples,
            expected_sha_by_slug=expected,
        )
    engine = LiveSignalEngine(
        configs=tuple(configs),
        data_dir=data_dir,
        interval_seconds=interval_seconds,
        min_net_edge=Decimal(min_net_edge_text),
        cost_buffer=Decimal(cost_buffer_text),
        config_update_path=update_path,
        config_loader=load_supervisor_events,
        warming_policy=warming_policy,
    )
    if initial_generation is not None:
        engine._last_config_generation = initial_generation
        engine.metrics.config_generation = initial_generation
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
