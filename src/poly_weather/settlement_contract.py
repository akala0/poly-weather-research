"""Versioned literal settlement clauses, never an automatic settlement engine."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict


class RuleSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str | None = None
    url: str | None = None
    station_id: str | None = None
    table: str | None = None


class RuleDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    date_basis: str = "observation_calendar_date"
    offset_days: int = 1
    clock: str = "23:59"
    timezone: str = "America/New_York"
    precision: str = "minute"
    boundary: str = "unresolved"

    def nominal_local_time(self, observation_date: date) -> datetime:
        """A labelled minute, NOT an inclusive/exclusive settlement instant."""
        return datetime.combine(
            observation_date + timedelta(days=self.offset_days),
            time.fromisoformat(self.clock),
            ZoneInfo(self.timezone),
        )


class SettlementRuleContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    semantic_version: str = "deadline-fallback-v2"
    primary: RuleSource
    fallback: RuleSource
    deadline: RuleDeadline | None = None
    fallback_condition: str | None = None
    settlement_trigger: str | None = None
    publication_source: str | None = None
    publication_date_offset_days: int | None = None
    no_data_condition: str | None = None
    no_data_action: str | None = None
    lowest_bucket_market_id: str | None = None
    revision_trigger: str | None = None
    revision_source: str | None = None
    unresolved: tuple[str, ...] = ()
    clauses: dict[str, tuple[int, int, str]] = {}

    def semantics(self) -> dict:
        return self.model_dump(mode="json", exclude={"clauses"})

    def completeness_failures(self) -> tuple[str, ...]:
        failures = list(self.unresolved)
        for name in (
            "fallback_condition",
            "settlement_trigger",
            "publication_source",
            "no_data_condition",
            "no_data_action",
            "lowest_bucket_market_id",
            "revision_trigger",
            "revision_source",
        ):
            if not getattr(self, name):
                failures.append(f"{name}_missing")
        for role in ("primary", "fallback"):
            for field in ("name", "url", "station_id", "table"):
                if not getattr(getattr(self, role), field):
                    failures.append(f"{role}_{field}_missing")
        if self.deadline is None or self.deadline.boundary == "unresolved":
            failures.append("deadline_boundary_unresolved")
        if self.publication_date_offset_days is None:
            failures.append("publication_date_offset_missing")
        return tuple(dict.fromkeys(failures))


def parse_rule_contract(
    description: str,
    *,
    source_name: str | None,
    source_url: str | None,
    station_id: str | None,
    primary_table: str | None,
    lowest_market_id: str | None,
) -> SettlementRuleContract | None:
    # Do not silently fall back to legacy when any new semantic fragment survives.
    if not re.search(
        r"whichever|11:59|lowest bracket|If NOAA data|Weather Underground", description, re.I
    ):
        return None
    patterns = {
        "deadline": r"11:59\s+PM\s+ET\s+on\s+the\s+day\s+following\s+the\s+observation\s+date",
        "settlement": r"This market will resolve once the first data point for the following date has been published on the resolution source, or by 11:59 PM ET on the day following the observation date, whichever comes first\.",
        "fallback": r"If NOAA data for the observation date is unavailable by 11:59 PM ET on the day following the observation date, the Weather Underground Daily Observations table will be used as the resolution source\.",
        "no_data": r"In the event that there is no data for the observation date by 11:59 PM ET on the day following the observation date, this market will resolve to the lowest bracket\.",
        "revision": r"Revisions to temperatures recorded within this market's timeframe will be considered until the first datapoint for the following date has been published, after which any alterations will not be considered\.",
    }
    clauses = {}
    unresolved = []
    for name, pattern in patterns.items():
        pattern = re.sub(r" +", r"\\s+", pattern)
        matches = list(re.finditer(pattern, description, re.I))
        expected = 3 if name == "deadline" else 1
        if len(matches) != expected:
            unresolved.append(f"{name}_missing_or_nonunique")
        if matches:
            m = matches[0]
            clauses[name] = (m.start(), m.end(), m.group())
    # Extra trigger-bearing sentences cannot be ignored as harmless prose.
    normalized = re.sub(r"\s+", " ", description).lower()
    if normalized.count("whichever") != 1 or normalized.count("this market will resolve once") != 1:
        unresolved.append("settlement_clause_conflict")
    unresolved.extend(
        (
            "deadline_second_boundary",
            "deadline_date_basis_review",
            "fallback_url_missing",
            "fallback_station_missing",
            "unavailable_vs_absent",
            "no_data_source_scope",
            "revision_after_settlement_deadline",
        )
    )
    if not primary_table:
        unresolved.append("primary_table_missing")
    if not lowest_market_id:
        unresolved.append("lowest_bucket_missing")
    return SettlementRuleContract(
        primary=RuleSource(
            name=source_name, url=source_url, station_id=station_id, table=primary_table
        ),
        fallback=RuleSource(
            name="Weather Underground" if "fallback" in clauses else None,
            table="Daily Observations" if "fallback" in clauses else None,
        ),
        deadline=RuleDeadline() if "deadline" in clauses else None,
        fallback_condition="primary_observation_date_unavailable_at_deadline"
        if "fallback" in clauses
        else None,
        settlement_trigger="earlier_of_first_next_date_publication_and_deadline"
        if "settlement" in clauses
        else None,
        publication_source="resolution_source" if "settlement" in clauses else None,
        publication_date_offset_days=1 if "settlement" in clauses else None,
        no_data_condition="observation_date_no_data_at_deadline_source_scope_unresolved"
        if "no_data" in clauses
        else None,
        no_data_action="lowest_temperature_bucket" if "no_data" in clauses else None,
        lowest_bucket_market_id=lowest_market_id,
        revision_trigger="first_next_date_publication" if "revision" in clauses else None,
        revision_source="resolution_source" if "revision" in clauses else None,
        unresolved=tuple(unresolved),
        clauses=clauses,
    )


def source_availability_path(primary: str, fallback: str) -> str:
    """Diagnostic condition classification, never a price or settlement decision."""
    if primary == "present":
        return "primary"
    if primary != "confirmed_absent":
        return "unknown_primary"
    if fallback == "present":
        return "fallback_candidate"
    if fallback == "confirmed_absent":
        return "no_data_candidate_review_required"
    return "unknown_fallback"


def publication_deadline_order(
    publication: datetime | None, deadline: RuleDeadline, observation_date: date
) -> str:
    if publication is None or publication.utcoffset() is None or deadline.boundary == "unresolved":
        return "UNKNOWN"
    instant = deadline.nominal_local_time(observation_date)
    return "before" if publication < instant else "after" if publication > instant else "equal"
