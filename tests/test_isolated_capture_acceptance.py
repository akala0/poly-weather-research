"""Independent offline acceptance. Diagnostic tests are not readiness grants."""

import asyncio
import copy
import json
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date

import pytest
from test_collection_capture import CONFIG, RAW, SPEC, full, plan, records

from poly_weather import collection_capture as capture
from poly_weather.collection_identity import IdentityError, identify_events


@pytest.fixture(autouse=True)
def isolated_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


@pytest.mark.parametrize("mutation", ["question_missing", "question_day", "ambiguous_site"])
def test_identity_source_conflicts_rejected(mutation):
    raw = copy.deepcopy(RAW)
    child = raw["markets"][0]
    if mutation == "question_missing":
        child.pop("question")
    elif mutation == "question_day":
        child["question"] = child["question"].replace("September 10", "September 11")
    else:
        child["resolutionSource"] += "&site=klax"
    with pytest.raises(IdentityError):
        identify_events([(raw, SPEC, date(2026, 9, 10))], max_tokens=64)


def test_halted_store_cannot_publish_complete(tmp_path):
    store = capture.CaptureStore(tmp_path / "capture", plan())
    store.halted = True  # Exact state after append's short-write/fsync exception.
    with pytest.raises(OSError, match="halted"):
        store.finish("complete")
    assert not (store.directory / "result.capture.json").exists()
    store.handle.close()


def test_cli_transport_failure_is_nonzero_with_durable_failure(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from poly_weather.cli import app

    async def failed(slug):
        raise ConnectionError("fixture transport unavailable")

    original = capture.run_capture

    async def offline(**kwargs):
        return await original(**kwargs, fetcher=failed)

    monkeypatch.setattr(capture, "run_capture", offline)
    p = tmp_path / "plan.json"
    p.write_text(plan().model_dump_json())
    result = CliRunner().invoke(
        app,
        [
            "market-capture",
            "--enable-capture",
            "--plan",
            str(p),
            "--capture-root",
            str(tmp_path / "capture"),
            "--config",
            str(CONFIG),
        ],
    )
    assert result.exit_code == 2
    _, rows = records(tmp_path / "capture")
    assert rows[-1]["record"]["phase"] == "identity_fetch"
    assert rows[-1]["record"]["error_type"] == "ConnectionError"


@pytest.mark.parametrize("change", ["token_swap", "condition", "rules"])
def test_reconnect_revalidates_saved_response_before_second_subscription(tmp_path, change):
    calls, sent = [], []

    async def fetch(slug):
        raw = copy.deepcopy(RAW)
        if calls:
            if change == "token_swap":
                child = raw["markets"][0]
                child["clobTokenIds"] = json.dumps(
                    list(reversed(json.loads(child["clobTokenIds"])))
                )
            elif change == "condition":
                raw["markets"][0]["conditionId"] = "0x" + "a" * 64
            else:
                raw["description"] += "\nAdditional unresolved text."
        calls.append(slug)
        return json.dumps(raw).encode(), time.time_ns()

    @asynccontextmanager
    async def connector(*args, **kwargs):
        class WS:
            async def send(self, value):
                sent.append(value)

            async def recv(self):
                raise RuntimeError("fixture disconnect")

        yield WS()

    with pytest.raises(IdentityError if change != "rules" else RuntimeError):
        asyncio.run(
            capture.run_capture(
                enabled=True,
                root=tmp_path / "capture",
                plan=plan(max_connection_failures=2),
                registry_path=CONFIG,
                fetcher=fetch,
                connector=connector,
            )
        )
    _, rows = records(tmp_path / "capture")
    assert len(calls) == 2
    assert len(sent) == (2 if change == "rules" else 1)
    assert len([r for r in rows if r["kind"] == "public_event_response"]) == 2
    identities = [r["record"] for r in rows if r["kind"] == "identity_and_rules"]
    assert all(not row["strategy_admitted"] for row in identities)
    assert identities[0]["projection_sha256"] != identities[1]["projection_sha256"]


def test_diagnostic_synchronous_fsync_can_exceed_campaign_deadline(tmp_path, monkeypatch):
    """BLOCKER evidence: a successful test reproduces the missing hard time bound."""
    original = capture.os.fsync
    delayed = False

    def slow(fd):
        nonlocal delayed
        if not delayed:
            delayed = True
            time.sleep(1.2)
        return original(fd)

    monkeypatch.setattr(capture.os, "fsync", slow)

    async def fetch(slug):
        return json.dumps(RAW).encode(), time.time_ns()

    @asynccontextmanager
    async def connector(*args, **kwargs):
        class WS:
            async def send(self, value):
                pass

            async def recv(self):
                await asyncio.sleep(10)

        yield WS()

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="capture_budget_before_identity"):
        asyncio.run(
            capture.run_capture(
                enabled=True,
                root=tmp_path / "capture",
                plan=plan(runtime_seconds=1),
                registry_path=CONFIG,
                fetcher=fetch,
                connector=connector,
            )
        )
    assert time.monotonic() - start >= 1.2
    # Detection after a synchronous call returns is not a hard wall-clock bound.
    assert (
        json.loads(next((tmp_path / "capture").rglob("result.capture.json")).read_text())["outcome"]
        == "failed"
    )


def test_failed_finish_cannot_leave_restartable_success(tmp_path, monkeypatch):
    store = capture.CaptureStore(tmp_path / "capture", plan())
    store.wire("public_market_frame", json.dumps(full()), receipt_ns=123, epoch=1)
    original = capture.os.fsync

    def failed(fd):
        raise OSError("fixture finish fsync failed")

    monkeypatch.setattr(capture.os, "fsync", failed)
    with pytest.raises(OSError, match="fixture finish"):
        store.finish("complete")
    monkeypatch.setattr(capture.os, "fsync", original)
    assert (store.directory / "result.pending.capture.json").is_file()
    assert not (store.directory / "result.capture.json").exists()
    with pytest.raises(ValueError, match="capture_run_schema_conflict"):
        capture.CaptureStore(tmp_path / "capture", plan())


@pytest.fixture
def corpus(tmp_path):
    from test_collection_capture import delta

    store = capture.CaptureStore(tmp_path / "corpus", plan())
    messages = [
        full(),
        delta(),
        {**full(), "event_type": "last_trade_price", "side": "SELL", "price": "0.4", "size": "200"},
        RAW,
    ]
    for index, message in enumerate(messages):
        store.wire("public_market_frame", json.dumps(message), receipt_ns=123 + index, epoch=1)
    store.finish("complete")
    return records(tmp_path / "corpus")


def test_signal_existing_book_output_and_business_state_unchanged(tmp_path, corpus):
    from datetime import UTC, datetime

    from poly_weather.domain import Market
    from poly_weather.signal_engine import LiveSignalConfig, LiveSignalEngine

    config = LiveSignalConfig(
        event_id=RAW["id"],
        event_slug=RAW["slug"],
        station_id="KLGA",
        timezone="America/New_York",
        target_date=date(2026, 9, 10),
        markets=tuple(Market.from_gamma(m) for m in RAW["markets"]),
        contract_verified=False,
        contract_reason="unresolved",
    )
    engine = LiveSignalEngine(configs=(config,), data_dir=tmp_path / "signal")
    at = datetime(2026, 9, 10, 12, tzinfo=UTC)
    try:
        legacy = {**full(), "received_at_ns": int(at.timestamp() * 1e9), "book_complete": True}
        engine._ingest_market(legacy)
        assert engine.books[legacy["asset_id"]]["bids"]
        before = copy.deepcopy(
            (
                engine.books,
                engine.weather,
                engine._committed_input_positions,
                engine._completed_evaluations,
                engine.current_signals,
            )
        )
        output = engine._event_signal(config, at)
        db_before = engine.sink.warehouse.status()
        disk = {
            p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in (tmp_path / "signal").rglob("*")
            if p.is_file() and ".duckdb" not in p.name
        }
        for row in corpus[1]:
            engine._ingest_market(row)
            with pytest.raises(ValueError, match="UNKNOWN_WEATHER_COLLECTION_MODE"):
                engine._ingest_weather(row)
        assert before == (
            engine.books,
            engine.weather,
            engine._committed_input_positions,
            engine._completed_evaluations,
            engine.current_signals,
        )
        assert engine._event_signal(config, at) == output
        assert engine.sink.warehouse.status() == db_before
        assert disk == {
            p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in (tmp_path / "signal").rglob("*")
            if p.is_file() and ".duckdb" not in p.name
        }
    finally:
        engine.sink.close()


def test_paper_and_v2_active_order_state_reject_explicit_capture_rows(tmp_path, corpus):
    from dataclasses import replace
    from decimal import Decimal

    from test_paper_runtime_boundaries import BASE, processor, snapshot

    from poly_weather.shadow_orders import BookSnapshot, ShadowLedger, ShadowStrategyConfig
    from poly_weather.shadow_runtime import ShadowStreamProcessor

    paper = processor(tmp_path)
    snap = replace(snapshot(), token_id=full()["asset_id"])
    assert paper.process_snapshot(snap) is not None
    shadow = ShadowStreamProcessor(
        ledger=ShadowLedger(tmp_path / "shadow.jsonl"),
        strategy=ShadowStrategyConfig(entry_bands={"KLAX": ((Decimal("0.7"), Decimal("0.9")),)}),
    )
    shadow.process_snapshot(snap)
    assert any(e.active_orders for e in shadow.engines.values())
    before = copy.deepcopy(
        (paper.status(as_of=BASE), shadow.status(), paper.pending_ws_trade_evidence)
    )
    disk = {p.name: p.read_bytes() for p in tmp_path.glob("*.jsonl")}
    for row in corpus[1]:
        with pytest.raises(ValueError, match="book snapshot has no timestamp"):
            paper.process_snapshot(BookSnapshot.from_mapping(row))
        with pytest.raises(ValueError, match="book snapshot has no timestamp"):
            shadow.process_snapshot(BookSnapshot.from_mapping(row))
    assert before == (paper.status(as_of=BASE), shadow.status(), paper.pending_ws_trade_evidence)
    assert disk == {p.name: p.read_bytes() for p in tmp_path.glob("*.jsonl")}


def test_quiet_and_complement_existing_orders_unchanged(corpus):
    from test_complement_pair import config, snapshots
    from test_quiet_window_strategy import _event, _snapshot, _thresholds

    from poly_weather.complement_pair import ComplementPairReplay, pair_snapshots_from_mapping
    from poly_weather.quiet_window_strategy import (
        QuietWindowConfig,
        QuietWindowEngine,
        _as_snapshot,
    )

    quiet = QuietWindowEngine(config=QuietWindowConfig(), thresholds=_thresholds())
    quiet.process_snapshot(_snapshot(0), information_events=(_event(),))
    quiet.process_snapshot(_snapshot(1))
    assert any(e.active_orders for e in quiet.engines.values())
    pair = ComplementPairReplay(config=config(), fill_model="queue_aware")
    pair.process_pair(*snapshots())
    assert next(iter(pair.portfolios.values())).submitted_pair_count == 1
    before = copy.deepcopy(
        ([e.orders for e in quiet.engines.values()], pair.summary(snapshots()[0].timestamp))
    )
    for row in corpus[1]:
        with pytest.raises(ValueError, match="paired row has no timestamp"):
            quiet.process_snapshot(_as_snapshot(row))
        with pytest.raises(ValueError, match="requires paired yes/no book rows"):
            pair.process_pair(*pair_snapshots_from_mapping(row))
    assert before == (
        [e.orders for e in quiet.engines.values()],
        pair.summary(snapshots()[0].timestamp),
    )


def test_nonempty_depth_and_discovery_mixed_legacy_inputs(tmp_path, corpus):
    from datetime import UTC, datetime

    from poly_weather.depth_calibration import replay_books_at_or_before
    from poly_weather.public_trade_collection import discover_depth_event_coverage

    at = datetime(2026, 9, 10, 12, tzinfo=UTC)
    token = full()["asset_id"]
    root = tmp_path / "legacy"
    path = root / "raw" / "polymarket_clob_websocket" / "2026-09-10" / "events.jsonl"
    path.parent.mkdir(parents=True)
    legacy = {
        **full(),
        "received_at": at.isoformat(),
        "market_id": full()["market"],
        "market_slug": RAW["slug"] + "/market:Yes",
        "book_complete": True,
    }
    path.write_text(json.dumps(legacy) + "\n")
    expected = replay_books_at_or_before(root, {token: [at]})
    assert expected[(token, at)] is not None
    targets = discover_depth_event_coverage([path])
    assert targets
    with path.open("ab") as out:
        out.write(corpus[0].read_bytes())  # Envelopes remain intact, no legacy export.
    assert replay_books_at_or_before(root, {token: [at]}) == expected
    assert discover_depth_event_coverage([path, corpus[0]]) == targets
    assert replay_books_at_or_before(corpus[0].parents[2], {token: [at]}) == {(token, at): None}


def test_explicit_capture_file_cannot_advance_shared_input_cursor(corpus):
    from poly_weather.archive_io import ArchiveRepresentationError
    from poly_weather.shadow_runtime import _incremental_jsonl_rows
    from poly_weather.signal_engine import JsonlTail

    position = {}
    with pytest.raises(ArchiveRepresentationError, match="ISOLATED_CAPTURE_INPUT_FORBIDDEN"):
        _incremental_jsonl_rows(corpus[0], position)
    assert position == {}
    tail = JsonlTail(lambda: corpus[0])
    with pytest.raises(ArchiveRepresentationError, match="ISOLATED_CAPTURE_INPUT_FORBIDDEN"):
        tail.poll()
    assert tail.positions == {} and tail.offset == 0


def test_mixed_capture_cannot_advance_existing_cursor_or_rebuild_prefix(tmp_path, corpus):
    from poly_weather.archive_io import ArchiveRepresentationError
    from poly_weather.archive_position import read_positioned_rows
    from poly_weather.shadow_runtime import _incremental_jsonl_rows

    path = tmp_path / "events.jsonl"
    legacy = {"received_at": "2026-09-10T12:00:00+00:00", "event_type": "book", "asset_id": "token"}
    path.write_text(json.dumps(legacy) + "\n")
    position = {}
    assert _incremental_jsonl_rows(path, position) == [legacy]
    before = copy.deepcopy(position)
    with path.open("ab") as handle:
        handle.write(corpus[0].read_bytes())
    with pytest.raises(ArchiveRepresentationError, match="ISOLATED_CAPTURE_INPUT_FORBIDDEN"):
        _incremental_jsonl_rows(path, position)
    assert position == before
    assert read_positioned_rows(path, position, committed_only=True)[0] == [legacy]


@pytest.mark.parametrize("fault", ["short", "flush", "fsync", "space", "quota"])
def test_frame_persistence_faults_preserve_confirmed_prefix(tmp_path, monkeypatch, fault):
    store = capture.CaptureStore(tmp_path / "capture", plan())
    store.wire("public_market_frame", json.dumps(full()), receipt_ns=123, epoch=1)
    path = store.directory / "frames.capture.jsonl"
    prefix, sequence, checksum = path.read_bytes(), store.sequence, store.previous
    handle = store.handle

    class FaultyHandle:
        def write(self, data):
            return handle.write(data[:5] if fault == "short" else data)

        def flush(self):
            if fault == "flush":
                raise OSError("fixture flush")
            return handle.flush()

        def fileno(self):
            return handle.fileno()

    store.handle = FaultyHandle()
    if fault == "fsync":

        def fail(fd):
            raise OSError("fixture fsync")

        monkeypatch.setattr(capture.os, "fsync", fail)
    if fault == "space":
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        monkeypatch.setattr(capture.shutil, "disk_usage", lambda _: usage(100, 100, 0))
    if fault == "quota":
        store.root_bytes = store.plan.max_bytes
    with pytest.raises(OSError):
        store.wire("public_market_frame", json.dumps(full()), receipt_ns=456, epoch=1)
    assert store.halted and (store.sequence, store.previous) == (sequence, checksum)
    handle.close()
    assert path.read_bytes().startswith(prefix)
    with pytest.raises(ValueError, match="unconfirmed_prior_capture_run"):
        capture.CaptureStore(tmp_path / "capture", plan())


@pytest.mark.parametrize("fault", ["json", "http", "connect_timeout", "frames", "frame_size"])
def test_run_failures_stop_without_retry_and_save_terminal(tmp_path, fault):
    count = 0

    async def fetch(slug):
        if fault == "http":
            raise ConnectionError("fixture HTTP")
        return (b"bad json" if fault == "json" else json.dumps(RAW).encode()), 123

    @asynccontextmanager
    async def connector(*args, **kwargs):
        nonlocal count
        count += 1
        if fault == "connect_timeout":
            raise TimeoutError("fixture connect deadline")

        class WS:
            async def send(self, value):
                pass

            async def recv(self):
                return "x" * 2048 if fault == "frame_size" else json.dumps(full())

        yield WS()

    expected = {
        "json": json.JSONDecodeError,
        "http": ConnectionError,
        "connect_timeout": TimeoutError,
        "frames": OSError,
        "frame_size": OSError,
    }[fault]
    with pytest.raises(expected):
        asyncio.run(
            capture.run_capture(
                enabled=True,
                root=tmp_path / "capture",
                plan=plan(max_frames=1, max_frame_bytes=1024),
                registry_path=CONFIG,
                fetcher=fetch,
                connector=connector,
            )
        )
    assert count == (0 if fault in {"json", "http"} else 1)
    _, rows = records(tmp_path / "capture")
    assert rows[-1]["kind"] == "failure"
    if fault == "frame_size":
        import base64
        import hashlib

        evidence = next(r["record"] for r in rows if r["kind"] == "oversize_frame")
        assert evidence["payload_size"] == 2048 and evidence["truncated"]
        assert base64.b64decode(evidence["payload_prefix_base64"]) == b"x" * 1024
        assert evidence["payload_sha256"] == hashlib.sha256(b"x" * 2048).hexdigest()
    assert (
        json.loads(next((tmp_path / "capture").rglob("result.capture.json")).read_text())["outcome"]
        == "failed"
    )


def test_different_roots_share_exclusive_writer_and_cancel_releases_lock(tmp_path):
    ready = None

    async def fetch(slug):
        return json.dumps(RAW).encode(), 123

    @asynccontextmanager
    async def connector(*args, **kwargs):
        class WS:
            async def send(self, value):
                ready.set()

            async def recv(self):
                await asyncio.sleep(30)

        yield WS()

    async def run():
        nonlocal ready
        ready = asyncio.Event()
        kwargs = dict(
            enabled=True,
            plan=plan(runtime_seconds=10),
            registry_path=CONFIG,
            fetcher=fetch,
            connector=connector,
        )
        first = asyncio.create_task(capture.run_capture(root=tmp_path / "one", **kwargs))
        await asyncio.wait_for(ready.wait(), timeout=2)
        try:
            with pytest.raises(OSError):
                await capture.run_capture(root=tmp_path / "two", **kwargs)
            assert not (tmp_path / "two").exists()
        finally:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        from poly_weather.public_trade_collection import _writer_lock

        with _writer_lock(tmp_path / "poly-weather-isolated-market-capture.lock"):
            pass

    asyncio.run(run())
    result = json.loads(next((tmp_path / "one").rglob("result.capture.json")).read_text())
    assert result["outcome"] == "failed" and result["error_type"] == "CancelledError"


def test_windows_case_parent_and_junction_are_rejected(tmp_path):
    import os
    import subprocess

    from test_collection_capture import ROOT

    for root in [ROOT / "DATA", ROOT / "DaTa" / "RAW", ROOT, ROOT.parent]:
        with pytest.raises(ValueError, match="capture_root_overlaps_production"):
            capture.safe_root(root)
    if os.name != "nt":
        return  # This case records Windows junction semantics only.
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, timeout=5
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert junction.is_junction()
    with pytest.raises(ValueError, match="capture_root_link_forbidden"):
        capture.safe_root(junction / "child")
