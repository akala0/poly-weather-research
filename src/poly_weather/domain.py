from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DISABLED = "disabled"


class WindowBasis(StrEnum):
    LOCAL_CIVIL_DAY = "local_civil_day"
    LOCAL_STANDARD_DAY = "local_standard_day"
    EXPLICIT_UTC = "explicit_utc"


class SignalTruthPolicy(StrEnum):
    OFFICIAL_RESOLUTION_SOURCE = "official_resolution_source"
    SAME_STATION_NOAA = "same_station_noaa"


class SettlementSpec(BaseModel):
    """The exact external facts needed to reproduce a market settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    venue: str = "polymarket"
    market_slug_pattern: str = Field(min_length=1)
    station_id: str | None = None
    station_name: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str | None = None
    metric: str = "daily_high_temperature"
    unit: str = "fahrenheit"
    bucket_width_degrees: int = Field(default=2, gt=0)
    rounding_precision_degrees: Decimal = Field(default=Decimal("1"), gt=0)
    finalization_rule: str = "first_next_day_observation"
    ignores_late_revisions: bool = True
    window_basis: WindowBasis | None = None
    resolution_source_url: HttpUrl | None = None
    ncei_station_id: str | None = None
    signal_truth_policy: SignalTruthPolicy = SignalTruthPolicy.OFFICIAL_RESOLUTION_SOURCE
    nws_api_enabled: bool = True
    open_meteo_enabled: bool = True
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    notes: str = ""

    @model_validator(mode="after")
    def validate_verified_spec(self) -> SettlementSpec:
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown IANA timezone: {self.timezone}") from exc

        if self.status is VerificationStatus.VERIFIED:
            required = {
                "station_id": self.station_id,
                "station_name": self.station_name,
                "latitude": self.latitude,
                "longitude": self.longitude,
                "timezone": self.timezone,
                "window_basis": self.window_basis,
                "resolution_source_url": self.resolution_source_url,
            }
            missing = [name for name, value in required.items() if value is None or value == ""]
            if missing:
                raise ValueError("verified settlement spec is incomplete: " + ", ".join(missing))
        return self

    def assert_tradeable(self) -> None:
        if self.status is not VerificationStatus.VERIFIED:
            raise ValueError(f"settlement spec {self.key!r} is not verified")


class SettlementRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    specs: tuple[SettlementSpec, ...]

    @model_validator(mode="after")
    def unique_keys(self) -> SettlementRegistry:
        keys = [spec.key for spec in self.specs]
        if len(keys) != len(set(keys)):
            raise ValueError("settlement spec keys must be unique")
        return self

    def by_key(self, key: str) -> SettlementSpec:
        for spec in self.specs:
            if spec.key == key:
                return spec
        raise KeyError(f"unknown settlement key: {key}")


def _json_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise ValueError("expected a JSON list")
        return decoded
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError("expected a list or JSON-encoded list")


class Market(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market_id: str
    question: str
    slug: str
    condition_id: str | None = None
    category: str | None = None
    description: str | None = None
    active: bool = False
    closed: bool = False
    end_date: datetime | None = None
    outcomes: tuple[str, ...] = ()
    outcome_prices: tuple[Decimal, ...] = ()
    clob_token_ids: tuple[str, ...] = ()
    resolution_source: str | None = None
    raw: dict[str, Any]

    @classmethod
    def from_gamma(cls, payload: dict[str, Any]) -> Market:
        return cls(
            market_id=str(payload["id"]),
            question=str(payload.get("question") or ""),
            slug=str(payload.get("slug") or ""),
            condition_id=payload.get("conditionId"),
            category=payload.get("category"),
            description=payload.get("description"),
            active=bool(payload.get("active", False)),
            closed=bool(payload.get("closed", False)),
            end_date=payload.get("endDate"),
            outcomes=tuple(str(item) for item in _json_list(payload.get("outcomes"))),
            outcome_prices=tuple(Decimal(str(item)) for item in _json_list(payload.get("outcomePrices"))),
            clob_token_ids=tuple(str(item) for item in _json_list(payload.get("clobTokenIds"))),
            resolution_source=payload.get("resolutionSource"),
            raw=payload,
        )

    @model_validator(mode="after")
    def aligned_outcomes(self) -> Market:
        if self.outcome_prices and len(self.outcomes) != len(self.outcome_prices):
            raise ValueError("outcomes and outcome_prices must have equal length")
        return self


class NwsObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str
    timestamp: datetime
    temperature_c: Decimal | None
    temperature_precision_degraded: bool = False
    raw: dict[str, Any]


class EnsembleForecast(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    latitude: float
    longitude: float
    timezone: str
    temperature_unit: str
    fetched_at: datetime
    times: tuple[datetime, ...]
    members: dict[str, tuple[Decimal | None, ...]]
    raw: dict[str, Any]

    @model_validator(mode="after")
    def aligned_series(self) -> EnsembleForecast:
        mismatched = [name for name, values in self.members.items() if len(values) != len(self.times)]
        if mismatched:
            raise ValueError("ensemble member lengths do not match time axis: " + ", ".join(mismatched))
        if not self.members:
            raise ValueError("ensemble response contains no temperature members")
        return self

    def daily_highs(self, target_date: date) -> tuple[Decimal, ...]:
        indices = [index for index, timestamp in enumerate(self.times) if timestamp.date() == target_date]
        if not indices:
            raise ValueError(f"forecast contains no hours for {target_date.isoformat()}")
        highs: list[Decimal] = []
        for values in self.members.values():
            available = [values[index] for index in indices if values[index] is not None]
            if available:
                highs.append(max(available))
        if not highs:
            raise ValueError(f"forecast contains no usable member values for {target_date.isoformat()}")
        return tuple(highs)


class DeterministicForecast(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    latitude: float
    longitude: float
    timezone: str
    temperature_unit: str
    fetched_at: datetime
    times: tuple[datetime, ...]
    values: tuple[Decimal | None, ...]
    raw: dict[str, Any]

    @model_validator(mode="after")
    def aligned_series(self) -> DeterministicForecast:
        if len(self.values) != len(self.times):
            raise ValueError("deterministic forecast values do not match time axis")
        return self

    def daily_high(self, target_date: date) -> Decimal:
        available = [
            value
            for timestamp, value in zip(self.times, self.values, strict=True)
            if timestamp.date() == target_date and value is not None
        ]
        if not available:
            raise ValueError(f"forecast contains no usable values for {target_date.isoformat()}")
        return max(available)


class TemperatureBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market_id: str
    market_slug: str
    label: str
    lower_f: int | None = None
    upper_f: int | None = None
    unit: str = "fahrenheit"
    market_probability: Decimal | None = None

    @model_validator(mode="after")
    def ordered_bounds(self) -> TemperatureBucket:
        if self.lower_f is not None and self.upper_f is not None and self.lower_f > self.upper_f:
            raise ValueError("temperature bucket lower bound exceeds upper bound")
        return self

    def contains(self, value_f: int) -> bool:
        return (self.lower_f is None or value_f >= self.lower_f) and (
            self.upper_f is None or value_f <= self.upper_f
        )

    @property
    def width_degrees(self) -> int | None:
        if self.lower_f is None or self.upper_f is None:
            return None
        return self.upper_f - self.lower_f + 1


class BucketProbability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: TemperatureBucket
    probability: Decimal
    edge_vs_market: Decimal | None = None


class BucketForecast(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    target_date: date
    forecast_model: str
    forecast_high_f: Decimal
    residual_std_f: float
    rounding: str
    calibration_applied: bool
    calibration_sample_count: int
    calibration_bias_f: float | None
    calibration_basis: str | None
    tradeable: bool
    tradeable_reason: str
    probabilities: tuple[BucketProbability, ...]


class RuleParseStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class FinalizationRule(StrEnum):
    FIRST_NEXT_DAY_OBSERVATION = "first_next_day_observation"
    SOURCE_FINALIZED = "source_finalized"
    UNKNOWN = "unknown"


class SettlementEvidence(BaseModel):
    """Deterministically parsed evidence; parsing alone never verifies a market."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    parser_version: str
    evidence_sha256: str
    event_id: str
    event_slug: str
    title: str
    city: str | None
    target_date: date | None
    station_id: str | None
    station_name: str | None
    timezone: str | None
    official_source_name: str | None
    official_source_url: str | None
    unit: str | None
    precision_degrees: Decimal | None
    bucket_width_degrees: int | None
    observation_table: str | None
    finalization_rule: FinalizationRule
    ignores_late_revisions: bool
    buckets: tuple[TemperatureBucket, ...]
    parse_status: RuleParseStatus
    missing_fields: tuple[str, ...]


class SettlementVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    settlement_key: str
    evidence_sha256: str
    passed: bool
    checks: dict[str, bool]
    failures: tuple[str, ...]
    tradeable: bool
    reason: str


class DailyHighPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    local_date: date
    value_f: float


class DailyHighSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    station_id: str
    model: str | None = None
    lead_days: int | None = None
    fetched_at: datetime
    values: tuple[DailyHighPoint, ...]
    raw: dict[str, Any] | list[dict[str, Any]]


class CalibrationSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str
    target_date: date
    lead_days: int
    model: str
    forecast_high_f: float
    observed_high_f: float
    forecast_source: str
    truth_source: str
    truth_kind: str
    ingested_at: datetime
    forecast_high_f_by_model: dict[str, float] | None = None

    @property
    def error_f(self) -> float:
        return self.observed_high_f - self.forecast_high_f


class BiasCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int
    bias_f: float
    residual_std_f: float
    raw_error_rms_f: float


class RollingEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str
    model: str
    lead_days: int
    folds: int
    train_min_size: int
    test_sample_count: int
    mae_raw: float
    mae_calibrated: float
    rmse_raw: float
    rmse_calibrated: float
    brier_raw: float
    brier_calibrated: float
    log_loss_raw: float
    log_loss_calibrated: float
    no_lookahead: bool = True


class TruthKind(StrEnum):
    NOAA_SAME_STATION_PROVISIONAL = "noaa_same_station_provisional"
    NOAA_SAME_STATION_DAILY_FINAL = "noaa_same_station_daily_final"


class ObservedDailyHigh(BaseModel):
    """A same-station daily high with explicit provenance and finality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    truth_kind: TruthKind
    station_id: str
    local_date: date
    timezone: str
    value_f: Decimal
    observation_count: int = Field(gt=0)
    first_observation_at: datetime
    last_observation_at: datetime
    finalized: bool
    fetched_at: datetime
    raw: dict[str, Any] | list[dict[str, Any]]


class MetarReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str
    observed_at: datetime
    temperature_c: Decimal | None
    dewpoint_c: Decimal | None
    temperature_source: str = "api_field"
    temperature_precision_degraded: bool = False
    raw_text: str
    flight_category: str | None
    raw: dict[str, Any]


class TafReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: str
    issued_at: datetime
    valid_from: datetime
    valid_to: datetime
    raw_text: str
    periods: tuple[dict[str, Any], ...]
    raw: dict[str, Any]


class AviationWeatherSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    station_id: str
    fetched_at: datetime
    metars: tuple[MetarReport, ...]
    tafs: tuple[TafReport, ...]
    metar_raw: list[dict[str, Any]]
    taf_raw: list[dict[str, Any]]


class MarketPricePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str = Field(min_length=1)
    timestamp: datetime
    price: Decimal = Field(ge=0, le=1)


class MarketPriceSeries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    token_id: str
    fetched_at: datetime
    points: tuple[MarketPricePoint, ...]


class MarketQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_id: str = Field(min_length=1)
    fetched_at: datetime
    best_bid: Decimal | None = Field(default=None, ge=0, le=1)
    best_ask: Decimal | None = Field(default=None, ge=0, le=1)
    midpoint: Decimal | None = Field(default=None, ge=0, le=1)
    spread: Decimal | None = Field(default=None, ge=0, le=1)


class MonitorStatus(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    STALE = "stale"


class MonitorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    captured_at: datetime
    station_id: str
    event_id: str
    event_slug: str
    signal_contract_verified: bool
    nws_observed_at: datetime | None
    nws_temperature_f: Decimal | None
    nws_age_minutes: float | None
    metar_observed_at: datetime | None
    metar_temperature_f: Decimal | None
    metar_age_minutes: float | None
    source_delta_f: Decimal | None
    taf_issued_at: datetime | None
    taf_valid_to: datetime | None
    taf_has_precipitation: bool
    taf_has_thunderstorm: bool
    taf_min_ceiling_ft: int | None
    token_market_slugs: dict[str, str]
    quotes: tuple[MarketQuote, ...]
    status: MonitorStatus
    reasons: tuple[str, ...]
    collection_errors: dict[str, str]
    raw_paths: dict[str, str]


class PaperAction(StrEnum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    SKIP = "skip"


class PaperDecision(BaseModel):
    """A non-executable decision record produced from a historical price snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    event_slug: str
    market_id: str
    market_slug: str
    token_id: str
    decision_time: datetime
    price_time: datetime
    yes_price: Decimal = Field(ge=0, le=1)
    model_probability: Decimal = Field(ge=0, le=1)
    raw_edge: Decimal
    estimated_cost: Decimal = Field(ge=0)
    net_edge: Decimal
    action: PaperAction
    notional_usd: Decimal = Field(ge=0)
    reasons: tuple[str, ...]
    signal_contract_verified: bool
    calibration_sample_count: int = Field(ge=0)
    no_lookahead: bool
