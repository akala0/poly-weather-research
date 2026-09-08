"""Tests for the optional, offline-only Nautilus conformance challenger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from poly_weather.nautilus_conformance import (
    PROJECT_ROOT,
    ConformanceClassification,
    NautilusFixtureRejected,
    VerifiedTrade,
    _reversed_same_second_evidence,
    _run_local_shadow,
    _run_nautilus_sandbox,
    _touch_case,
    adapt_nautilus_fixture,
    canonical_timeline,
    fixture_manifest,
    forbidden_execution_dependency_scan,
    run_offline_conformance,
    sandbox_config_hash,
    validate_verified_trade,
    write_conformance_result,
)
from poly_weather.shadow_orders import BookSnapshot, TradeEvent


def _snapshot() -> BookSnapshot:
    timestamp = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)
    return BookSnapshot(
        timestamp=timestamp,
        event_id="event-a",
        market_id="market-a",
        token_id="token-a",
        bids=((Decimal("0.74"), Decimal("5")), (Decimal("0.73"), Decimal("20"))),
        asks=((Decimal("0.76"), Decimal("50")),),
        station_id="KLAX",
        market_day="2026-09-04",
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("1"),
        season_version="test-season-v1",
        metadata={"book_received_at": timestamp.isoformat(), "outcome": "NO"},
    )


def _verified_trades() -> tuple[VerifiedTrade, ...]:
    timestamp = _snapshot().timestamp + timedelta(seconds=1)
    return (
        VerifiedTrade(
            TradeEvent(
                timestamp=timestamp,
                available_at=timestamp + timedelta(seconds=1),
                asset_id="token-a",
                side="SELL",
                price=Decimal("0.74"),
                size=Decimal("5"),
                event_id="trade-a",
                source="public_tape",
                sequence=1,
            ),
            receipt_verified=True,
            public_tape_verified=True,
            gap_free=True,
        ),
        VerifiedTrade(
            TradeEvent(
                timestamp=timestamp,
                available_at=timestamp + timedelta(seconds=2),
                asset_id="token-a",
                side="SELL",
                price=Decimal("0.74"),
                size=Decimal("5"),
                event_id="trade-b",
                source="public_tape",
                sequence=2,
            ),
            receipt_verified=True,
            public_tape_verified=True,
            gap_free=True,
        ),
    )


def test_conformance_import_is_lazy_and_forbidden_execution_scan_is_clear() -> None:
    code = (
        "import json, sys; "
        "import poly_weather.nautilus_conformance as challenger; "
        "print(json.dumps({'optional_loaded': 'nautilus_trader' in sys.modules, "
        "'scan': challenger.forbidden_execution_dependency_scan()['status']}))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(completed.stdout) == {"optional_loaded": False, "scan": "clear"}
    assert forbidden_execution_dependency_scan()["status"] == "clear"


def test_optional_nautilus_dependency_python_range_and_lock_are_pinned() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.12,<3.15"
    assert pyproject["project"]["optional-dependencies"]["nautilus-eval"] == [
        "nautilus-trader==2.0.0rc4"
    ]

    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.12, <3.15"' in lock_text
    assert 'name = "nautilus-trader"\nversion = "2.0.0rc4"' in lock_text
    assert 'specifier = "==2.0.0rc4"' in lock_text


def test_manifest_is_deterministic_token_native_and_rejects_unsequenced_trade() -> None:
    snapshot = _snapshot()
    trades = _verified_trades()
    first = fixture_manifest(snapshot, trades)
    second = fixture_manifest(snapshot, trades)
    assert first["fixture_hash"] == second["fixture_hash"]
    assert first["snapshot"]["token_id"] == "token-a"
    assert first["snapshot"]["bids"][0] == ["0.74", "5"]

    invalid = VerifiedTrade(
        TradeEvent(
            timestamp=trades[0].trade.timestamp,
            available_at=trades[0].trade.available_at,
            asset_id="token-a",
            side="SELL",
            price=Decimal("0.74"),
            size=Decimal("1"),
            event_id="missing-sequence",
        ),
        receipt_verified=True,
        public_tape_verified=True,
        gap_free=True,
    )
    with pytest.raises(NautilusFixtureRejected, match="unsequenced"):
        validate_verified_trade(snapshot, invalid)


def test_conformance_writer_only_allows_explicit_non_data_output(tmp_path: Path) -> None:
    result = {
        "official_score": False,
        "challenger_only": True,
        "execution_enabled": False,
        "fixture_hash": "fixture",
    }
    with pytest.raises(ValueError, match="formal data root"):
        write_conformance_result(result, PROJECT_ROOT / "data" / "must-not-exist.json")

    destination = tmp_path / "nautilus-conformance.json"
    written = write_conformance_result(result, destination)
    assert written["official_score"] is False
    assert json.loads(destination.read_text(encoding="utf-8"))["challenger_only"] is True


@pytest.mark.nautilus_eval
def test_optional_nautilus_sandbox_fixture_and_matrix_are_isolated() -> None:
    pytest.importorskip("nautilus_trader")
    snapshot = _snapshot()
    trades = _verified_trades()
    fixture = adapt_nautilus_fixture(snapshot, trades)

    assert str(fixture.sandbox_config.venue) == "POLYMARKET"
    assert str(fixture.sandbox_config.account_type) == "CASH"
    assert str(fixture.sandbox_config.oms_type) == "NETTING"
    assert str(fixture.sandbox_config.book_type) == "L2_MBP"
    assert fixture.sandbox_config.trade_execution is True
    assert fixture.sandbox_config.queue_position is True
    assert fixture.sandbox_config.liquidity_consumption is True
    assert fixture.sandbox_config.bar_execution is False
    assert int(fixture.trade_ticks[0].ts_event) < int(fixture.trade_ticks[0].ts_init)
    assert len(fixture.book_deltas) == 3

    report = run_offline_conformance(
        snapshot,
        trades,
        config_hash="paper-config-hash-for-test",
        order_shares=Decimal("10"),
    )
    assert report["official_score"] is False
    assert report["challenger_only"] is True
    assert report["execution_enabled"] is False
    assert report["nautilus_version"] == "2.0.0rc4"
    assert report["config_hash"] == "paper-config-hash-for-test"
    assert report["dependency_scan"]["status"] == "clear"
    assert len(report["matrix"]) == 18
    classifications = {row["classification"] for row in report["matrix"]}
    assert ConformanceClassification.MATCH.value in classifications
    assert ConformanceClassification.NAUTILUS_LIMITATION.value in classifications
    assert ConformanceClassification.UNSUPPORTED.value in classifications
    assert report["matrix"][2]["classification"] == ConformanceClassification.NAUTILUS_LIMITATION.value


@pytest.mark.nautilus_eval
def test_nautilus_touch_case_contains_real_touch_without_trade() -> None:
    pytest.importorskip("nautilus_trader")
    touch = _touch_case(_snapshot(), shares=Decimal("10"))

    assert touch["input"]["public_trade_count"] == 0
    assert Decimal(touch["input"]["later_best_ask"]) <= Decimal(touch["input"]["maker_limit"])
    assert touch["local"]["fills"] == []
    assert len(touch["local_touch_counterfactual"]["fills"]) == 1
    assert touch["nautilus"]["fills"] == []
    later_event_ns = int(
        datetime.fromisoformat(touch["input"]["timeline"][-1]["event_timestamp"])
        .astimezone(UTC)
        .timestamp()
        * 1_000_000_000
    )
    assert any(
        row["kind"] == "book_callback" and row["event_timestamp_ns"] == later_event_ns
        for row in touch["nautilus"]["observed_trace"]
    )


@pytest.mark.nautilus_eval
def test_nautilus_local_and_native_share_availability_timeline() -> None:
    pytest.importorskip("nautilus_trader")
    snapshot = _snapshot()
    trades = _verified_trades()
    expected = [row.as_dict() for row in canonical_timeline(snapshot, trades)]
    fixture = adapt_nautilus_fixture(snapshot, trades)
    local = _run_local_shadow(snapshot, trades, shares=Decimal("10"))
    native = _run_nautilus_sandbox(fixture, shares=Decimal("10"))

    assert [row.as_dict() for row in fixture.timeline] == expected
    assert local["timeline"] == expected
    assert native["timeline"] == expected
    # rc4 does not offer the strategy a TradeTick subscription API here, so
    # it cannot prove callback-level receipt scheduling. The report must call
    # this a limitation rather than manufacture a MATCH.
    report = run_offline_conformance(snapshot, trades, order_shares=Decimal("10"))
    row = next(
        item
        for item in report["matrix"]
        if item["case"] == "availability_timeline_and_sequence_limitation"
    )
    assert row["classification"] == ConformanceClassification.NAUTILUS_LIMITATION.value
    assert row["inputs"]["canonical_receipt_order_sequences"] == [2, 1]


@pytest.mark.nautilus_eval
def test_nautilus_sequence_limitation_is_not_reported_as_match() -> None:
    pytest.importorskip("nautilus_trader")
    evidence = _reversed_same_second_evidence(
        _snapshot(), _verified_trades()[0].trade, shares=Decimal("10")
    )

    assert evidence["input"]["canonical_receipt_order_sequences"] == [2, 1]
    assert evidence["native_tick_has_sequence"] is False
    report = run_offline_conformance(_snapshot(), _verified_trades(), order_shares=Decimal("10"))
    row = next(
        item
        for item in report["matrix"]
        if item["case"] == "availability_timeline_and_sequence_limitation"
    )
    assert row["classification"] != ConformanceClassification.MATCH.value
    assert row["classification"] == ConformanceClassification.NAUTILUS_LIMITATION.value


@pytest.mark.nautilus_eval
def test_nautilus_report_hashes_actual_active_venue_config() -> None:
    pytest.importorskip("nautilus_trader")
    fixture = adapt_nautilus_fixture(_snapshot(), _verified_trades())
    native = _run_nautilus_sandbox(fixture, shares=Decimal("10"))

    assert native["active_venue_config"] == dict(fixture.sandbox_config_payload)
    assert native["active_venue_config_hash"] == sandbox_config_hash(
        fixture.sandbox_config_payload
    )
    mutations = {
        "venue": "OTHER_VENUE",
        "starting_balance_usdc": "201",
        "account_type": "MARGIN",
        "oms_type": "HEDGING",
        "book_type": "L1_MBP",
        "trade_execution": False,
        "queue_position": False,
        "liquidity_consumption": False,
        "bar_execution": True,
        "fee_model": "other",
        "execution_enabled": True,
        "fill_model.name": "other",
        "fill_model.prob_fill_on_limit": 1.0,
        "fill_model.prob_slippage": 1.0,
        "fill_model.random_seed": 1,
        "fill_model.policy": "other",
    }
    original_hash = sandbox_config_hash(fixture.sandbox_config_payload)
    for field, value in mutations.items():
        payload = deepcopy(dict(fixture.sandbox_config_payload))
        if field.startswith("fill_model."):
            payload["fill_model"][field.rpartition(".")[2]] = value
        else:
            payload[field] = value
        assert sandbox_config_hash(payload) != original_hash


@pytest.mark.nautilus_eval
def test_nautilus_matrix_classifications_are_backed_by_trace_assertions() -> None:
    pytest.importorskip("nautilus_trader")
    report = run_offline_conformance(_snapshot(), _verified_trades(), order_shares=Decimal("10"))

    assert len(report["matrix"]) == 18
    for row in report["matrix"]:
        assert set(
            ("inputs", "local_trace", "native_trace", "compared_fields", "classification", "reason", "assertions")
        ).issubset(row)
        assert row["assertions"]
        if row["classification"] == ConformanceClassification.UNSUPPORTED.value:
            assert row["native_trace"]["native_trace_available"] is False
        if row["classification"] == ConformanceClassification.NAUTILUS_LIMITATION.value:
            assert any(
                value in (False, 0, "N/A") for value in row["assertions"].values()
            ), row["case"]
