"""Deterministic settlement-rule parsing and fail-closed verification.

The parser design is adapted from YoungseokOh/polymarket-tmax-lab (MIT).
See THIRD_PARTY_NOTICES.md. This implementation targets this project's
event-plus-binary-buckets domain model and current Polymarket rule wording.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from poly_weather.adapters.polymarket import EventSnapshot
from poly_weather.domain import (
    FinalizationRule,
    RuleParseStatus,
    SettlementEvidence,
    SettlementSpec,
    SettlementVerification,
    SignalTruthPolicy,
    VerificationStatus,
)
from poly_weather.modeling import market_temperature_bucket, validate_bucket_partition
from poly_weather.settlement_contract import parse_rule_contract

PARSER_VERSION = "3"
_TITLE_RE = re.compile(
    r"^highest temperature in (?P<city>.+?) on (?P<month>[A-Za-z]+) (?P<day>\d{1,2})\?$",
    re.IGNORECASE,
)
_SLUG_DATE_RE = re.compile(
    r"-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$",
    re.IGNORECASE,
)
_RULE_DATE_RE = re.compile(
    r"\bon\s+(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})\s+'?(?P<year>\d{2,4})\b",
    re.IGNORECASE,
)
_STATION_RE = re.compile(
    r"recorded (?:at|by NOAA at) the (?P<station>.+?)(?: Station)? in degrees",
    re.IGNORECASE,
)
_WHOLE_DEGREE_RE = re.compile(
    r"measures temperatures to whole degrees (?P<unit>Fahrenheit|Celsius)",
    re.IGNORECASE,
)
_NEXT_DAY_RE = re.compile(
    r"can not resolve until the first data point for the following date has been published",
    re.IGNORECASE,
)
_FINALIZED_RE = re.compile(
    r"can not resolve(?: to [\"']?yes[\"']?)? until .*finalized",
    re.IGNORECASE,
)
_LATE_REVISION_RE = re.compile(
    r"(?:after which any alterations|revisions .* after .* finalized).*not be considered",
    re.IGNORECASE | re.DOTALL,
)


def _target_date(event: EventSnapshot, description: str) -> date | None:
    if match := _SLUG_DATE_RE.search(event.event_slug):
        try:
            return datetime.strptime(
                f"{match.group('month')} {match.group('day')} {match.group('year')}",
                "%B %d %Y",
            ).date()
        except ValueError:
            pass
    if match := _RULE_DATE_RE.search(description):
        year = match.group("year")
        if len(year) == 2:
            year = "20" + year
        try:
            return datetime.strptime(
                f"{match.group('day')} {match.group('month')} {year}",
                "%d %b %Y",
            ).date()
        except ValueError:
            return None
    return None


def _source(description: str, event_source: str | None) -> tuple[str | None, str | None]:
    # Source name belongs to the primary-source sentence, not fallback prose.
    primary = re.search(
        r"The resolution source for this market will be information from (NOAA|Wunderground)\b",
        description,
        re.I,
    )
    lowered = primary.group(1).lower() if primary else description.lower()
    if lowered == "wunderground" or (not primary and "wunderground" in lowered):
        name = "Wunderground"
    elif (
        lowered == "noaa"
        or "information from noaa" in lowered
        or "weather.gov/wrh/timeseries" in lowered
    ):
        name = "NOAA Timeseries"
    else:
        name = None
    url = event_source
    if not url:
        match = re.search(r"https?://[^\s)]+", description)
        url = match.group(0).rstrip(".,") if match else None
    return name, url


def _station_id(source_name: str | None, source_url: str | None) -> str | None:
    if not source_url:
        return None
    if source_name == "Wunderground":
        tail = urlparse(source_url).path.rstrip("/").split("/")[-1]
        return tail.upper() or None
    if source_name == "NOAA Timeseries":
        match = re.search(r"[?&]site=([A-Z0-9]{4,6})", source_url, re.IGNORECASE)
        return match.group(1).upper() if match else None
    return None


def parse_settlement_evidence(
    event: EventSnapshot,
    *,
    registry_spec: SettlementSpec | None = None,
) -> SettlementEvidence:
    """Parse an event into evidence without granting verification status."""
    raw_description = str(event.raw_payload.get("description") or "")
    description = re.sub(r"\s+", " ", raw_description)
    title_match = _TITLE_RE.match(event.title.strip())
    city = title_match.group("city").strip() if title_match else None
    source_name, source_url = _source(description, event.resolution_source)
    station_match = _STATION_RE.search(description)
    station_name = station_match.group("station").strip() if station_match else None
    station_id = _station_id(source_name, source_url)
    precision_match = _WHOLE_DEGREE_RE.search(description)
    unit = None
    precision = None
    if precision_match:
        unit = "fahrenheit" if precision_match.group("unit").lower().startswith("f") else "celsius"
        precision = Decimal("1")
    elif "degrees fahrenheit" in description.lower():
        unit = "fahrenheit"
    elif "degrees celsius" in description.lower():
        unit = "celsius"

    if _NEXT_DAY_RE.search(description):
        finalization = FinalizationRule.FIRST_NEXT_DAY_OBSERVATION
    elif _FINALIZED_RE.search(description):
        finalization = FinalizationRule.SOURCE_FINALIZED
    else:
        finalization = FinalizationRule.UNKNOWN

    parsed_buckets = []
    for market in event.markets:
        parsed_buckets.append(market_temperature_bucket(market, expected_unit=unit))
    buckets = validate_bucket_partition(tuple(parsed_buckets))
    finite_widths = {width for bucket in buckets if (width := bucket.width_degrees) is not None}
    bucket_width = next(iter(finite_widths)) if len(finite_widths) == 1 else None
    primary_table = (
        "Hourly Data / Temp"
        if source_name == "NOAA Timeseries" and "hourly data" in description.lower()
        else "Daily Observations"
        if source_name == "Wunderground" and "daily observations" in description.lower()
        else None
    )
    lowest = next((b.market_id for b in buckets if b.lower_f is None), None)
    contract = parse_rule_contract(
        raw_description,
        source_name=source_name,
        source_url=source_url,
        station_id=station_id,
        primary_table=primary_table,
        lowest_market_id=lowest,
    )
    if contract is not None:
        finalization = (
            FinalizationRule.PUBLICATION_OR_DEADLINE
            if contract.settlement_trigger
            else FinalizationRule.UNKNOWN
        )
    timezone = (
        registry_spec.timezone if registry_spec and registry_spec.station_id == station_id else None
    )
    fields = {
        "city": city,
        "target_date": _target_date(event, description),
        "station_id": station_id,
        "station_name": station_name,
        "timezone": timezone,
        "official_source_name": source_name,
        "official_source_url": source_url,
        "unit": unit,
        "precision_degrees": precision,
        "observation_table": primary_table,
        "bucket_width_degrees": bucket_width,
    }
    missing = tuple(name for name, value in fields.items() if value is None or value == "")
    if finalization is FinalizationRule.UNKNOWN:
        missing += ("finalization_rule",)
    if contract is not None:
        missing += contract.completeness_failures()
    canonical = json.dumps(
        {
            "parser_version": PARSER_VERSION,
            "rule_contract": contract.model_dump(mode="json") if contract else None,
            "event_id": event.event_id,
            "event_slug": event.event_slug,
            "title": event.title,
            "description": raw_description,
            "resolution_source": event.resolution_source,
            # Gamma does not promise a stable child-market array order. The
            # evidence is a set of contracts, so order must not alter its hash.
            "market_slugs": sorted(market.slug for market in event.markets),
            "bucket_contracts": sorted(
                (b.model_dump(mode="json", exclude={"market_probability"}) for b in buckets),
                key=lambda b: b["market_id"],
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return SettlementEvidence(
        parser_version=PARSER_VERSION,
        evidence_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        event_id=event.event_id,
        event_slug=event.event_slug,
        title=event.title,
        **fields,
        finalization_rule=finalization,
        ignores_late_revisions=bool(_LATE_REVISION_RE.search(description)),
        buckets=buckets,
        parse_status=RuleParseStatus.COMPLETE if not missing else RuleParseStatus.INCOMPLETE,
        missing_fields=missing,
        rule_contract=contract,
        rule_schema_version=2 if contract else 1,
    )


def verify_settlement_evidence(
    evidence: SettlementEvidence,
    spec: SettlementSpec,
) -> SettlementVerification:
    """Compare parsed evidence with a manually reviewed registry entry."""
    checks = {
        "parser_current": evidence.parser_version == PARSER_VERSION,
        "parse_complete": evidence.parse_status is RuleParseStatus.COMPLETE,
        "registry_verified": spec.status is VerificationStatus.VERIFIED,
        "slug_exact": re.fullmatch(spec.market_slug_pattern, evidence.event_slug) is not None,
        "station_exact": spec.station_id == evidence.station_id,
        "station_name_exact": spec.station_name == evidence.station_name,
        "timezone_exact": spec.timezone == evidence.timezone,
        "source_exact": (
            spec.resolution_source_url is not None
            and str(spec.resolution_source_url) == evidence.official_source_url
        ),
        "whole_degree_precision": evidence.precision_degrees == Decimal("1"),
        "unit_exact": evidence.unit == spec.unit,
        "bucket_width_exact": evidence.bucket_width_degrees == spec.bucket_width_degrees,
        "finalization_known": evidence.finalization_rule is not FinalizationRule.UNKNOWN,
        "finalization_exact": evidence.finalization_rule.value == spec.finalization_rule,
        "late_revision_cutoff": evidence.ignores_late_revisions,
        "late_revision_exact": evidence.ignores_late_revisions == spec.ignores_late_revisions,
    }
    differences = {}
    comparisons = {
        "parser_current": (PARSER_VERSION, evidence.parser_version),
        "parse_complete": ("complete", evidence.parse_status.value),
        "registry_verified": ("verified", spec.status.value),
        "slug_exact": (spec.market_slug_pattern, evidence.event_slug),
        "station_exact": (spec.station_id, evidence.station_id),
        "station_name_exact": (spec.station_name, evidence.station_name),
        "timezone_exact": (spec.timezone, evidence.timezone),
        "source_exact": (str(spec.resolution_source_url), evidence.official_source_url),
        "whole_degree_precision": ("1", str(evidence.precision_degrees)),
        "unit_exact": (spec.unit, evidence.unit),
        "bucket_width_exact": (spec.bucket_width_degrees, evidence.bucket_width_degrees),
        "finalization_known": ("known", evidence.finalization_rule.value),
        "finalization_exact": (spec.finalization_rule, evidence.finalization_rule.value),
        "late_revision_cutoff": (True, evidence.ignores_late_revisions),
        "late_revision_exact": (spec.ignores_late_revisions, evidence.ignores_late_revisions),
    }
    differences.update({k: {"expected": a, "actual": b} for k, (a, b) in comparisons.items()})
    if evidence.rule_contract is not None or spec.rule_contract is not None:
        actual = evidence.rule_contract.semantics() if evidence.rule_contract else {}
        expected = spec.rule_contract.semantics() if spec.rule_contract else {}
        checks["rule_reviewed"] = spec.rule_review_status == "reviewed"
        checks["rule_resolved"] = (
            bool(evidence.rule_contract) and not evidence.rule_contract.completeness_failures()
        )
        checks["rule_schema_current"] = (
            evidence.rule_schema_version == 2 and evidence.parser_version == PARSER_VERSION
        )
        differences["rule_schema_current"] = {
            "expected": {"schema": 2, "parser": PARSER_VERSION},
            "actual": {"schema": evidence.rule_schema_version, "parser": evidence.parser_version},
        }
        for field in sorted(set(actual) | set(expected)):
            checks[f"rule_{field}_exact"] = actual.get(field) == expected.get(field)
            if not checks[f"rule_{field}_exact"]:
                differences[f"rule_{field}_exact"] = {
                    "expected": expected.get(field),
                    "actual": actual.get(field),
                }
        differences["rule_reviewed"] = {"expected": "reviewed", "actual": spec.rule_review_status}
        differences["rule_resolved"] = {
            "expected": [],
            "actual": list(evidence.rule_contract.completeness_failures())
            if evidence.rule_contract
            else None,
        }
    elif evidence.rule_schema_version != 1:
        checks["rule_contract_present"] = False
        differences["rule_contract_present"] = {
            "expected": "versioned rule contract",
            "actual": None,
        }
    failures = tuple(name for name, passed in checks.items() if not passed)
    passed = not failures
    return SettlementVerification(
        settlement_key=spec.key,
        evidence_sha256=evidence.evidence_sha256,
        passed=passed,
        checks=checks,
        failures=failures,
        tradeable=passed,
        reason="all settlement evidence checks passed"
        if passed
        else "failed: " + ", ".join(failures),
        differences={k: v for k, v in differences.items() if k in failures},
    )


def verify_signal_contract(
    evidence: SettlementEvidence,
    spec: SettlementSpec,
) -> SettlementVerification:
    """Verify the tradable contract mapping under an explicit signal-source policy.

    SAME_STATION_NOAA deliberately does not wait for the resolution website's data,
    but it still requires that the market rules identify a recognized source and the
    configured airport station, local day, unit, precision, and finalization semantics.
    """
    strict = verify_settlement_evidence(evidence, spec)
    if (
        evidence.rule_contract is not None
        or spec.rule_contract is not None
        or evidence.rule_schema_version != 1
    ):
        return strict
    if spec.signal_truth_policy is SignalTruthPolicy.OFFICIAL_RESOLUTION_SOURCE:
        return strict
    checks = {
        "parser_current": evidence.parser_version == PARSER_VERSION,
        "parse_complete": evidence.parse_status is RuleParseStatus.COMPLETE,
        "slug_exact": re.fullmatch(spec.market_slug_pattern, evidence.event_slug) is not None,
        "station_exact": spec.station_id == evidence.station_id,
        "station_name_exact": spec.station_name == evidence.station_name,
        "timezone_exact": spec.timezone == evidence.timezone,
        "source_recognized": evidence.official_source_name in {"Wunderground", "NOAA Timeseries"},
        "unit_exact": evidence.unit == spec.unit,
        "whole_degree_precision": evidence.precision_degrees == Decimal("1"),
        "bucket_width_exact": evidence.bucket_width_degrees == spec.bucket_width_degrees,
        "finalization_known": evidence.finalization_rule is not FinalizationRule.UNKNOWN,
        "late_revision_cutoff": evidence.ignores_late_revisions,
        "same_station_noaa_policy": True,
    }
    failures = tuple(name for name, passed in checks.items() if not passed)
    passed = not failures
    return SettlementVerification(
        settlement_key=spec.key,
        evidence_sha256=evidence.evidence_sha256,
        passed=passed,
        checks=checks,
        failures=failures,
        tradeable=passed,
        reason=(
            "contract mapping verified for same-station NOAA signals"
            if passed
            else "failed: " + ", ".join(failures)
        ),
    )
