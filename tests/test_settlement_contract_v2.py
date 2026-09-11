"""Saved public projection and synthetic counterexamples; never network or data writes."""

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from poly_weather.adapters.polymarket import GammaClient
from poly_weather.cli import app
from poly_weather.config import load_settlement_registry
from poly_weather.domain import RuleParseStatus, SettlementEvidence
from poly_weather.market_supervisor import MarketEventSupervisor, _city_query, discover_event
from poly_weather.settlement import (
    parse_settlement_evidence,
    verify_settlement_evidence,
    verify_signal_contract,
)
from poly_weather.settlement_contract import (
    RuleDeadline,
    publication_deadline_order,
    source_availability_path,
)
from poly_weather.settlement_diagnostics import DiagnosticWriteError, SettlementDiagnostics

ROOT = Path(__file__).resolve().parents[1]
SAVED = ROOT / "docs/strict_rejection_query_20260910T091217Z"
SPECS = load_settlement_registry(ROOT / "configs/settlements.json").specs


def payload(spec):
    return json.loads((SAVED / f"{spec.station_id}.response.json").read_text(encoding="utf-8"))


def client_for(data):
    return GammaClient(
        client=httpx.Client(
            base_url="https://gamma-api.polymarket.com",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=data, request=request)
            ),
        )
    )


def event_for(spec, data=None):
    with client_for(payload(spec) if data is None else data) as gamma:
        return discover_event(gamma, spec, date(2026, 9, 10))


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.station_id)
def test_ten_saved_projections_contract_and_both_policies_reject(spec):
    event = event_for(spec)
    evidence = parse_settlement_evidence(event, registry_spec=spec)
    c = evidence.rule_contract
    assert evidence.parser_version == "3" and evidence.rule_schema_version == 2
    assert evidence.parse_status == RuleParseStatus.INCOMPLETE
    assert c.primary.name == "NOAA Timeseries"
    if spec.station_id in {"ZUCK", "ZUUU"}:
        assert c.primary.table is None
    else:
        assert c.primary.table == "Hourly Data / Temp"
    assert c.fallback.name == "Weather Underground" and c.fallback.table == "Daily Observations"
    assert c.fallback.url is None and c.fallback.station_id is None
    assert c.deadline.timezone == "America/New_York" and c.deadline.clock == "23:59"
    assert c.deadline.precision == "minute" and c.deadline.boundary == "unresolved"
    assert c.settlement_trigger == "earlier_of_first_next_date_publication_and_deadline"
    assert c.revision_trigger == "first_next_date_publication"
    assert c.no_data_action == "lowest_temperature_bucket"
    assert c.lowest_bucket_market_id == next(
        b.market_id for b in evidence.buckets if b.lower_f is None
    )
    for policy in ("same_station_noaa", "official_resolution_source"):
        from poly_weather.domain import SignalTruthPolicy

        reviewed = spec.model_copy(update={"signal_truth_policy": SignalTruthPolicy(policy)})
        result = verify_signal_contract(evidence, reviewed)
        assert not result.passed and "rule_resolved" in result.failures
        assert "rule_reviewed" in result.failures
        assert result.differences["rule_deadline_exact"]["actual"]["clock"] == "23:59"


@pytest.mark.parametrize(
    "fragment,replacement,missing",
    [
        ("11:59 PM ET", "10:59 PM ET", "deadline_missing_or_nonunique"),
        ("11:59 PM ET", "11:59 PM UTC", "deadline_missing_or_nonunique"),
        ("day following the observation date", "observation date", "deadline_missing_or_nonunique"),
        ("whichever comes first", "whichever comes last", "settlement_missing_or_nonunique"),
        (
            "Weather Underground Daily Observations",
            "NOAA Hourly Data",
            "fallback_missing_or_nonunique",
        ),
        ("lowest bracket", "highest bracket", "no_data_missing_or_nonunique"),
        (
            "Revisions to temperatures",
            "Corrections to temperatures",
            "revision_missing_or_nonunique",
        ),
    ],
)
def test_clause_mutation_never_gains_admission(fragment, replacement, missing):
    spec = SPECS[0]
    event = event_for(spec)
    altered = event.model_copy(
        update={
            "raw_payload": event.raw_payload
            | {"description": event.raw_payload["description"].replace(fragment, replacement)}
        }
    )
    evidence = parse_settlement_evidence(altered, registry_spec=spec)
    assert missing in evidence.rule_contract.unresolved
    assert not verify_settlement_evidence(evidence, spec).passed
    assert (
        evidence.evidence_sha256
        != parse_settlement_evidence(event, registry_spec=spec).evidence_sha256
    )


def test_duplicate_clause_and_format_and_bucket_order():
    spec = SPECS[0]
    event = event_for(spec)
    original = parse_settlement_evidence(event, registry_spec=spec)
    clause = original.rule_contract.clauses["settlement"][2]
    duplicate = event.model_copy(
        update={
            "raw_payload": event.raw_payload
            | {"description": event.raw_payload["description"] + " " + clause}
        }
    )
    assert (
        "settlement_missing_or_nonunique"
        in parse_settlement_evidence(duplicate, registry_spec=spec).rule_contract.unresolved
    )
    formatted = event.model_copy(
        update={
            "raw_payload": event.raw_payload
            | {"description": event.raw_payload["description"].upper()},
            "markets": tuple(reversed(event.markets)),
        }
    )
    changed = parse_settlement_evidence(formatted, registry_spec=spec)
    assert changed.rule_contract.semantics() == original.rule_contract.semantics()
    assert changed.evidence_sha256 != original.evidence_sha256


def test_synthetic_reviewed_contract_only_complete_explicit_match_passes():
    # Synthetic interpretation fixture, not an approval of real text/candidate.
    spec = SPECS[0]
    evidence = parse_settlement_evidence(event_for(spec), registry_spec=spec)
    c = evidence.rule_contract
    c = c.model_copy(
        update={
            "unresolved": (),
            "fallback": c.fallback.model_copy(
                update={"url": "https://example.test/KLGA", "station_id": "KLGA"}
            ),
            "deadline": c.deadline.model_copy(update={"boundary": "exact_instant_test_only"}),
        }
    )
    e = evidence.model_copy(
        update={"rule_contract": c, "parse_status": RuleParseStatus.COMPLETE, "missing_fields": ()}
    )
    s = spec.model_copy(
        update={
            "rule_contract": c,
            "rule_review_status": "reviewed",
            "finalization_rule": "publication_or_deadline",
        }
    )
    assert verify_settlement_evidence(e, s).passed
    assert not verify_settlement_evidence(
        e, s.model_copy(update={"rule_review_status": "pending_human_review"})
    ).passed
    assert not verify_settlement_evidence(
        e.model_copy(update={"rule_contract": c.model_copy(update={"revision_trigger": None})}), s
    ).passed
    assert not verify_settlement_evidence(e.model_copy(update={"parser_version": "2"}), s).passed
    old = evidence.model_dump(mode="json")
    for key in ("rule_contract", "rule_schema_version"):
        old.pop(key)
    old["parser_version"] = "2"
    loaded = SettlementEvidence.model_validate(old)
    assert loaded.rule_contract is None and not verify_settlement_evidence(loaded, spec).passed


@pytest.mark.parametrize(
    "day,next_day,offset",
    [
        (date(2026, 1, 31), date(2026, 2, 1), -5),
        (date(2026, 12, 31), date(2027, 1, 1), -5),
        (date(2028, 2, 28), date(2028, 2, 29), -5),
        (date(2026, 7, 1), date(2026, 7, 2), -4),
        (date(2026, 3, 7), date(2026, 3, 8), -4),
        (date(2026, 11, 1), date(2026, 11, 2), -5),
    ],
)
def test_deadline_calendar_dst_not_server_date(day, next_day, offset):
    instant = RuleDeadline().nominal_local_time(day)
    assert instant.date() == next_day and instant.utcoffset().total_seconds() == offset * 3600
    assert instant.hour == 23 and instant.minute == 59


@pytest.mark.parametrize("hour,order", [(1, "before"), (23, "before"), (24, "after")])
def test_publication_receipt_and_boundary_not_conflated(hour, order):
    deadline = RuleDeadline()
    instant = datetime(2026, 9, 12 if hour == 24 else 11, 5 if hour == 24 else hour, tzinfo=UTC)
    assert publication_deadline_order(instant, deadline, date(2026, 9, 10)) == "UNKNOWN"
    exact = deadline.model_copy(update={"boundary": "test_only"})
    assert publication_deadline_order(instant, exact, date(2026, 9, 10)) == order
    assert (
        publication_deadline_order(
            exact.nominal_local_time(date(2026, 9, 10)), exact, date(2026, 9, 10)
        )
        == "equal"
    )
    assert publication_deadline_order(None, exact, date(2026, 9, 10)) == "UNKNOWN"


@pytest.mark.parametrize(
    "primary,fallback,expected",
    [
        ("present", "unknown", "primary"),
        ("confirmed_absent", "present", "fallback_candidate"),
        ("confirmed_absent", "confirmed_absent", "no_data_candidate_review_required"),
        ("http_failure", "present", "unknown_primary"),
        ("confirmed_absent", "http_failure", "unknown_fallback"),
    ],
)
def test_no_data_states_are_not_prices(primary, fallback, expected):
    assert source_availability_path(primary, fallback) == expected


def test_cli_real_conversion_all_ten_durable_rejections_no_collector(tmp_path, monkeypatch):
    mapping = {
        f"highest temperature in {_city_query(s)} on September 10": payload(s) for s in SPECS
    }
    client = httpx.Client(
        base_url="https://gamma-api.polymarket.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=mapping[request.url.params["q"]], request=request
            )
        ),
    )
    monkeypatch.setattr("poly_weather.cli.GammaClient", lambda: GammaClient(client=client))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, 12, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("poly_weather.cli.datetime", Frozen)

    def forbidden(*args, **kwargs):
        pytest.fail("collector constructed")

    monkeypatch.setattr("poly_weather.cli.MarketWebSocketBot", forbidden)
    result = CliRunner().invoke(
        app,
        [
            "market-supervisor",
            "--startup-attempt-id",
            "fixture-ten",
            "--data-dir",
            str(tmp_path),
            "--config",
            str(ROOT / "configs/settlements.json"),
        ],
    )
    assert result.exit_code == 2, result.output
    rows = [json.loads(p.read_text()) for p in tmp_path.rglob("settlement/*.json")]
    finals = [r for r in rows if r.get("outcome") == "rule_incomplete"]
    assert len(finals) == 10 and {r["station_id"] for r in finals} == {s.station_id for s in SPECS}
    for r in finals:
        assert any(
            i["stage"] == "input" and i["projection_sha256"] == r["projection_sha256"] for i in rows
        )
        assert r["attempt_id"] == "fixture-ten" and r["verification"]["differences"]


def test_conversion_and_parse_failure_prefix_and_write_failure(tmp_path, monkeypatch):
    spec = SPECS[0]
    diagnostics = SettlementDiagnostics(tmp_path, "fault")
    diagnostics.begin(spec, date(2026, 9, 10))
    data = payload(spec)
    data["events"][0]["markets"][0]["outcomes"] = "not-json"
    with client_for(data) as gamma:
        gamma.event_observer = diagnostics.observe
        with pytest.raises(ValueError):
            gamma.search_markets_page(query="fixture")
    rows = [json.loads(p.read_text()) for p in diagnostics.root.glob("*.json")]
    assert any(r["stage"] == "input" for r in rows) and any(
        r["stage"] == "conversion" for r in rows
    )
    event = event_for(spec).model_copy(update={"markets": ()})
    assert diagnostics.evaluate(event, spec) == (None, None)
    assert any(
        json.loads(p.read_text()).get("outcome") == "parse_failed"
        for p in diagnostics.root.glob("*.json")
    )
    prefix = {p.name: p.read_bytes() for p in diagnostics.root.glob("*.json")}

    def fail(*a, **kw):
        raise DiagnosticWriteError("disk full fixture")

    monkeypatch.setattr(diagnostics, "write", fail)
    with pytest.raises(DiagnosticWriteError):
        diagnostics.evaluate(event_for(spec), spec)
    assert all((diagnostics.root / name).read_bytes() == body for name, body in prefix.items())


def test_supervisor_uses_same_diagnosis_no_subscription(tmp_path):
    class Bot:
        async def subscribe_assets(self, **kwargs):
            pytest.fail("unreviewed subscription")

    supervisor = MarketEventSupervisor(
        specs=(SPECS[0],),
        bot=Bot(),
        data_dir=tmp_path,
        raw_collection_recovery=True,
        startup_attempt_id="cycle",
    )
    asyncio.run(supervisor.reconcile({SPECS[0].key: event_for(SPECS[0])}))
    assert supervisor.metrics.verified == 0
    rows = [json.loads(p.read_text()) for p in supervisor.diagnostics.root.glob("*.json")]
    assert any(r.get("outcome") == "rule_incomplete" for r in rows)


@pytest.mark.parametrize("kind", ["empty", "ambiguous", "write_failed", "conversion_after_prefix"])
def test_cli_discovery_outcomes_and_diagnostic_failure_stop(tmp_path, monkeypatch, kind):
    spec = SPECS[0]
    event = event_for(spec)
    data = payload(spec)
    candidate = next(e for e in data["events"] if str(e["id"]) == event.event_id)
    if kind == "empty":
        data = {"events": []}
    elif kind == "ambiguous":
        data = {"events": [candidate, candidate | {"id": "duplicate-event"}]}
    elif kind == "conversion_after_prefix":
        bad = json.loads(json.dumps(candidate))
        bad["id"] = "invalid-next-event"
        bad["markets"][0]["outcomes"] = "broken-json"
        data = {"events": [candidate, bad]}
    monkeypatch.setattr("poly_weather.cli.GammaClient", lambda: client_for(data))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 10, 12, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("poly_weather.cli.datetime", Frozen)

    def forbidden(*args, **kwargs):
        pytest.fail("collector constructed")

    monkeypatch.setattr("poly_weather.cli.MarketWebSocketBot", forbidden)
    if kind == "write_failed":

        def fail(*a, **kw):
            raise DiagnosticWriteError("injected write failure")

        monkeypatch.setattr(SettlementDiagnostics, "write", fail)
    result = CliRunner().invoke(
        app,
        [
            "market-supervisor",
            "--startup-attempt-id",
            "failure-test",
            "--data-dir",
            str(tmp_path),
            "--config",
            str(ROOT / "configs/settlements.json"),
        ],
    )
    assert result.exit_code == (2 if kind == "empty" else 4), result.output
    rows = [json.loads(p.read_text()) for p in tmp_path.rglob("settlement/*.json")]
    if kind == "empty":
        assert len([r for r in rows if r.get("outcome") == "no_candidate"]) == 10
    elif kind == "ambiguous":
        assert any(r.get("outcome") == "ambiguous" for r in rows)
    elif kind == "conversion_after_prefix":
        assert len([r for r in rows if r["stage"] == "input"]) == 2
        assert any(r["stage"] == "conversion" for r in rows)


def test_receipt_and_future_rule_do_not_rewrite_prior_identity(tmp_path):
    spec = SPECS[0]
    event = event_for(spec)
    prior = parse_settlement_evidence(event, registry_spec=spec)
    later = event.model_copy(update={"fetched_at": datetime(2030, 1, 1, tzinfo=UTC)})
    assert (
        parse_settlement_evidence(later, registry_spec=spec).evidence_sha256
        == prior.evidence_sha256
    )
    changed = later.model_copy(
        update={
            "raw_payload": later.raw_payload
            | {"description": later.raw_payload["description"].replace("11:59 PM", "10:59 PM")}
        }
    )
    assert (
        parse_settlement_evidence(changed, registry_spec=spec).evidence_sha256
        != prior.evidence_sha256
    )
    diagnostics = SettlementDiagnostics(tmp_path, "immutable-prefix")
    diagnostics.evaluate(event, spec)
    prefix = {p.name: p.read_bytes() for p in diagnostics.root.glob("*.json")}
    diagnostics.begin(spec, date(2026, 9, 10))
    diagnostics.evaluate(changed, spec)
    assert all((diagnostics.root / name).read_bytes() == body for name, body in prefix.items())


def test_pending_review_document_not_registry_input():
    from pydantic import ValidationError

    candidate = ROOT / "docs/settlement_registry_candidate_20260910.json"
    data = json.loads(candidate.read_text(encoding="utf-8"))
    assert data["review_status"] == "pending_human_review"
    assert all(
        r["review_status"] == "pending_human_review" and r["reviewer"] is None
        for r in data["entries"]
    )
    with pytest.raises(ValidationError):
        load_settlement_registry(candidate)


def test_event_snapshot_conversion_failure_has_input_and_conversion_record(tmp_path):
    spec = SPECS[0]
    data = payload(spec)
    data["events"][0]["resolutionSource"] = {"invalid": "not a URL string"}
    for market in data["events"][0]["markets"]:
        market["resolutionSource"] = str(spec.resolution_source_url)
    diagnostics = SettlementDiagnostics(tmp_path, "snapshot-conversion")
    diagnostics.begin(spec, date(2026, 9, 10))
    with client_for(data) as gamma:
        gamma.event_observer = diagnostics.observe
        with pytest.raises(ValueError):
            gamma.search_markets_page(query="fixture")
    rows = [json.loads(p.read_text()) for p in diagnostics.root.glob("*.json")]
    assert any(r["stage"] == "input" for r in rows)
    assert any(
        r["stage"] == "conversion" and r["exception"]["type"] == "ValidationError" for r in rows
    )


def test_semantic_whitespace_keeps_contract_but_not_source_hash():
    spec = SPECS[0]
    event = event_for(spec)
    old = parse_settlement_evidence(event, registry_spec=spec)
    changed = event.model_copy(
        update={
            "raw_payload": event.raw_payload
            | {"description": event.raw_payload["description"].replace(" ", " \n ")}
        }
    )
    new = parse_settlement_evidence(changed, registry_spec=spec)
    assert new.rule_contract.semantics() == old.rule_contract.semantics()
    assert new.evidence_sha256 != old.evidence_sha256


@pytest.mark.parametrize("name", ["settlement", "revision", "fallback", "no_data"])
def test_deleting_required_clause_keeps_rejection(name):
    spec = SPECS[0]
    event = event_for(spec)
    prior = parse_settlement_evidence(event, registry_spec=spec)
    clause = prior.rule_contract.clauses[name][2]
    changed = event.model_copy(
        update={
            "raw_payload": event.raw_payload
            | {"description": event.raw_payload["description"].replace(clause, "")}
        }
    )
    evidence = parse_settlement_evidence(changed, registry_spec=spec)
    assert f"{name}_missing_or_nonunique" in evidence.missing_fields
    assert not verify_settlement_evidence(evidence, spec).passed
