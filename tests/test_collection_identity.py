import copy
import json
from datetime import date
from pathlib import Path

import pytest

from poly_weather.collection_identity import IdentityError, identify_events
from poly_weather.config import load_settlement_registry

ROOT = Path(__file__).resolve().parents[1]
SPECS = load_settlement_registry(ROOT / "configs/settlements.json").specs


def source(spec):
    return json.loads(
        (
            ROOT
            / "docs/strict_rejection_query_20260910T091217Z"
            / f"{spec.station_id}.response.json"
        ).read_text(encoding="utf-8")
    )["events"][0]


def identify(raw, spec=SPECS[0]):
    return identify_events([(raw, spec, date(2026, 9, 10))], max_tokens=512)


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.station_id)
def test_historical_identity_is_separate_from_full_rule_approval(spec):
    result = identify(source(spec), spec)
    assert len(result.bindings) == 22
    assert len({b.token_id for b in result.bindings}) == 22
    assert result.rule_results[0]["verification"]["passed"] is False
    assert result.rule_results[0]["evidence"]["missing_fields"]
    assert result.identity_sha256 != result.projection_sha256


@pytest.mark.parametrize(
    "mutation",
    [
        "condition_missing",
        "duplicate_token",
        "duplicate_market",
        "duplicate_condition",
        "unaligned",
        "missing_id",
        "bad_date",
        "wrong_station",
        "closed",
        "nondict",
        "blank_outcome",
        "bad_token",
        "event_slug_missing",
    ],
)
def test_no_partial_or_overwriting_identity_acceptance(mutation):
    raw = source(SPECS[0])
    first, second = raw["markets"][:2]
    if mutation == "condition_missing":
        first.pop("conditionId")
    if mutation == "duplicate_token":
        second["clobTokenIds"] = first["clobTokenIds"]
    if mutation == "duplicate_market":
        raw["markets"].append(copy.deepcopy(first))
    if mutation == "duplicate_condition":
        second["conditionId"] = first["conditionId"]
    if mutation == "unaligned":
        first["clobTokenIds"] = '["123"]'
    if mutation == "missing_id":
        first["id"] = ""
    if mutation == "bad_date":
        raw["description"] = raw["description"].replace("10 Sep '26", "11 Sep '26")
    if mutation == "wrong_station":
        first["resolutionSource"] = "https://www.weather.gov/wrh/timeseries?site=klax"
    if mutation == "closed":
        first["closed"] = True
    if mutation == "nondict":
        raw["markets"].append(None)
    if mutation == "blank_outcome":
        first["outcomes"] = '["Yes", ""]'
    if mutation == "bad_token":
        first["clobTokenIds"] = '["NaN", "123"]'
    if mutation == "event_slug_missing":
        raw.pop("slug")
    with pytest.raises(IdentityError):
        identify(raw)


def test_cross_event_conflicts_and_budget_reject_whole_set():
    a, b = source(SPECS[0]), source(SPECS[1])
    b["markets"][0]["clobTokenIds"] = a["markets"][0]["clobTokenIds"]
    with pytest.raises(IdentityError):
        identify_events(
            [(a, SPECS[0], date(2026, 9, 10)), (b, SPECS[1], date(2026, 9, 10))], max_tokens=512
        )
    with pytest.raises(IdentityError):
        identify_events([(a, SPECS[0], date(2026, 9, 10))], max_tokens=21)


def test_rule_and_mapping_hashes_are_independent_and_order_stable():
    raw = source(SPECS[0])
    initial = identify(raw)
    raw["markets"].reverse()
    assert identify(raw).identity_sha256 == initial.identity_sha256
    raw["description"] += "\nAdditional unresolved text."
    changed = identify(raw)
    assert changed.identity_sha256 == initial.identity_sha256
    assert changed.projection_sha256 != initial.projection_sha256
    raw["markets"][0]["clobTokenIds"] = json.dumps(
        list(reversed(json.loads(raw["markets"][0]["clobTokenIds"])))
    )
    assert identify(raw).identity_sha256 != initial.identity_sha256
