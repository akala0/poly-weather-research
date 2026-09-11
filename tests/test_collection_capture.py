import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from poly_weather.cli import app
from poly_weather.collection_capture import (
    CapturePlan,
    CaptureStore,
    SnapshotBoundary,
    run_capture,
    safe_root,
    verify_segment,
)
from poly_weather.collection_identity import identify_events
from poly_weather.config import load_settlement_registry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/settlements.json"
SPEC = load_settlement_registry(CONFIG).specs[0]
RAW = json.loads(
    (ROOT / "docs/strict_rejection_query_20260910T091217Z/KLGA.response.json").read_text()
)["events"][0]
IDENTITY = identify_events([(RAW, SPEC, date(2026, 9, 10))], max_tokens=512)


def plan(**changes):
    return CapturePlan.model_validate(
        {
            "selections": [
                {"station_id": "KLGA", "target_date": "2026-09-10", "event_slug": RAW["slug"]}
            ],
            "runtime_seconds": 1,
            "max_tokens": 64,
            "max_bytes": 8 * 1024**2,
            "min_free_bytes": 1024**2,
            **changes,
        }
    )


def full(token=None):
    binding = token or IDENTITY.bindings[0]
    return {
        "event_type": "book",
        "asset_id": binding.token_id,
        "market": binding.condition_id,
        "timestamp": "1789031549000",
        "bids": [{"price": "0.4", "size": "10"}],
        "asks": [{"price": "0.5", "size": "10"}],
    }


def delta():
    b = IDENTITY.bindings[0]
    return {
        "event_type": "price_change",
        "market": b.condition_id,
        "price_changes": [{"asset_id": b.token_id, "side": "BUY", "price": "0.4", "size": "8"}],
    }


def records(root):
    path = next(root.rglob("frames.capture.jsonl"))
    return path, [json.loads(line) for line in path.read_text().splitlines()]


def test_disabled_entry_has_no_side_effects(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "market-capture",
            "--plan",
            str(tmp_path / "absent"),
            "--capture-root",
            str(tmp_path / "capture"),
        ],
    )
    assert result.exit_code != 0
    assert "capture disabled" in result.output
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("relative", ["", "data", "data/raw", "data/runtime"])
def test_formal_paths_block_before_any_write(relative):
    with pytest.raises(ValueError):
        safe_root(ROOT / relative)


def test_storage_raw_bytes_checksum_no_legacy_schema_and_restart(tmp_path):
    root = tmp_path / "capture"
    store = CaptureStore(root, plan())
    raw = b"\xffnot JSON\n\x00"
    store.wire("public_market_frame", raw, receipt_ns=1234567890123456789, epoch=1)
    store.finish("complete")
    path, rows = records(root)
    row = rows[0]
    assert base64.b64decode(row["record"]["payload_bytes_base64"]) == raw
    assert row["record"]["receipt_ns"] == 1234567890123456789
    assert not {"event_type", "raw", "no", "yes", "asset_id", "received_at"} & row.keys()
    assert verify_segment(path, max_bytes=plan().max_bytes)["sequence"] == 1
    before = path.read_bytes()
    second = CaptureStore(root, plan())
    second.finish("complete")
    assert path.read_bytes() == before
    assert not list(root.rglob("*.duckdb"))
    assert not list(root.rglob("events.jsonl"))
    assert not list(root.rglob("market_supervisor_status.json"))
    assert not list(root.rglob("polymarket_ws_status.json"))
    assert not list(root.rglob("signal_engine_status.json"))
    assert not list(root.rglob("shadow_spread_status*.json"))
    assert not list(root.rglob("paper*_status.json"))
    assert not list(root.rglob("active_set*"))
    assert not list(root.rglob("signal_config_update.json"))


def test_marked_root_without_runs_directory_is_repaired_without_data_migration(tmp_path):
    root = tmp_path / "capture"
    root.mkdir()
    (root / "capture-root.json").write_text(
        json.dumps({"domain": "isolated-public-market-capture/v1"}) + "\n"
    )
    store = CaptureStore(root, plan())
    store.finish("complete")
    assert (root / "runs").is_dir()
    assert len(list((root / "runs").iterdir())) == 1


def test_root_file_is_rejected_before_any_capture_write(tmp_path):
    root = tmp_path / "capture"
    root.write_text("existing")
    with pytest.raises(ValueError, match="capture_root_not_directory"):
        CaptureStore(root, plan())
    assert root.read_text() == "existing"


def test_existing_capture_root_rejects_unrelated_files_and_run_entries(tmp_path):
    root = tmp_path / "capture"
    store = CaptureStore(root, plan())
    store.finish("complete")
    (root / "unrelated.db").write_bytes(b"do not absorb")
    with pytest.raises(ValueError, match="capture_root_schema_conflict"):
        CaptureStore(root, plan())
    (root / "unrelated.db").unlink()
    run = next((root / "runs").iterdir())
    (run / "unrelated.status.json").write_text("{}")
    with pytest.raises(ValueError, match="capture_run_schema_conflict"):
        CaptureStore(root, plan())


@pytest.mark.parametrize("bad", ["truncate", "edit", "missing_result", "failed"])
def test_prior_conflict_blocks_without_rewriting(tmp_path, bad):
    root = tmp_path / "capture"
    store = CaptureStore(root, plan())
    store.wire("public_market_frame", "{}", receipt_ns=123, epoch=1)
    store.finish("failed" if bad == "failed" else "complete")
    path, _ = records(root)
    if bad == "truncate":
        path.write_bytes(path.read_bytes()[:-1])
    if bad == "edit":
        path.write_bytes(path.read_bytes().replace(b"123", b"124"))
    if bad == "missing_result":
        (path.parent / "result.capture.json").unlink()
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises((ValueError, KeyError)):
        CaptureStore(root, plan())
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_write_failure_halts_and_never_advances_frontier(tmp_path, monkeypatch):
    store = CaptureStore(tmp_path / "capture", plan())
    store.wire("public_market_frame", "{}", receipt_ns=1, epoch=1)
    prior = store.sequence, store.previous

    def fail(_):
        raise OSError("injected fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr("poly_weather.collection_capture.os.fsync", fail)
        with pytest.raises(OSError):
            store.wire("public_market_frame", "{}", receipt_ns=2, epoch=1)
    assert (store.sequence, store.previous) == prior
    with pytest.raises(OSError):
        store.append("later", {})
    store.finish("failed", error_type="OSError")
    with pytest.raises(ValueError):
        CaptureStore(tmp_path / "capture", plan())


@pytest.mark.parametrize(
    "bad", ["missing_side", "unknown_token", "wrong_condition", "bad_depth", "unknown_format"]
)
def test_structural_unknown_invalidates_initial_snapshot(bad):
    boundary = SnapshotBoundary(IDENTITY)
    assert boundary.observe(json.dumps(full()))["initialized_tokens"]
    row = full()
    if bad == "missing_side":
        row.pop("asks")
    if bad == "unknown_token":
        row["asset_id"] = "42"
    if bad == "wrong_condition":
        row["market"] = "0x" + "0" * 64
    if bad == "bad_depth":
        row["asks"][0]["size"] = "NaN"
    if bad == "unknown_format":
        row = {"topic": "market", "payload": row}
    out = boundary.observe(json.dumps(row))
    assert not out["initialized_tokens"] and out["healthy_l2"] is False


def test_reconnection_requires_new_full_even_after_old_full():
    boundary = SnapshotBoundary(IDENTITY)
    assert boundary.observe(json.dumps(full()))["initialized_tokens"]
    boundary.reset()
    assert boundary.observe(json.dumps(delta()))["reasons"] == ["delta_before_full_snapshot"]
    empty = full()
    empty.update(bids=[], asks=[])
    assert boundary.observe(json.dumps(empty))["initialized_tokens"]
    assert boundary.observe(json.dumps(delta()))["healthy_l2"] is False


def test_actual_recorder_fake_wire_reconnect_rechecks_identity_and_preserves_unknown(tmp_path):
    fetched = []
    sent = []
    epochs = []

    async def fetch(slug):
        fetched.append(slug)
        return json.dumps(RAW).encode(), 1789031549000000000

    @asynccontextmanager
    async def connection(url, **kwargs):
        index = len(epochs)
        epochs.append(url)

        class WS:
            def __init__(self):
                self.count = 0

            async def send(self, value):
                sent.append(value)

            async def recv(self):
                self.count += 1
                if index == 0:
                    if self.count == 1:
                        return json.dumps(full())
                    raise RuntimeError("fake lost connection")
                if self.count == 1:
                    return json.dumps(delta())
                if self.count == 2:
                    return b"unknown binary format"
                if self.count == 3:
                    return json.dumps(full())
                await asyncio.sleep(5)

        yield WS()

    root = tmp_path / "capture"
    result = asyncio.run(
        run_capture(
            enabled=True,
            root=root,
            plan=plan(),
            registry_path=CONFIG,
            fetcher=fetch,
            connector=connection,
        )
    )
    path, rows = records(root)
    assert len(fetched) == len(sent) == len(epochs) == 2
    assert result["frames"] == 4 and result["connection_failures"] == 1
    observations = [r["record"] for r in rows if r["kind"] == "structural_observation"]
    assert observations[1]["reasons"] == ["delta_before_full_snapshot"]
    assert not observations[2]["initialized_tokens"]
    assert observations[3]["initialized_tokens"]
    assert all(r["healthy_l2"] is False for r in observations)
    assert verify_segment(path, max_bytes=plan().max_bytes)["sequence"] == len(rows)
    assert all(
        not r["record"]["strategy_admitted"] for r in rows if r["kind"] == "identity_and_rules"
    )


def test_invalid_identity_saved_before_rejection_and_no_subscription(tmp_path):
    bad = json.loads(json.dumps(RAW))
    bad["markets"][0]["conditionId"] = None

    async def fetch(slug):
        return json.dumps(bad).encode(), 1789031549000000000

    def connection(*args, **kwargs):
        pytest.fail("must not subscribe")

    root = tmp_path / "capture"
    with pytest.raises(ValueError):
        asyncio.run(
            run_capture(
                enabled=True,
                root=root,
                plan=plan(),
                registry_path=CONFIG,
                fetcher=fetch,
                connector=connection,
            )
        )
    _, rows = records(root)
    assert rows[0]["kind"] == "public_event_response"
    assert rows[-1]["kind"] == "failure"


def test_capture_envelopes_do_not_enter_actual_shared_consumers(tmp_path):
    from poly_weather.archive_io import jsonl_archive_paths
    from poly_weather.complement_pair import pair_snapshots_from_mapping
    from poly_weather.depth_calibration import replay_books_at_or_before
    from poly_weather.information_clock import load_external_information_events
    from poly_weather.liquidity import archived_liquidity_rows_from_jsonl
    from poly_weather.market_trade_tape import load_market_ws_trades
    from poly_weather.public_trade_collection import discover_depth_event_coverage
    from poly_weather.quiet_window_strategy import _as_snapshot
    from poly_weather.real_no_books import iter_paired_book_snapshots
    from poly_weather.shadow_orders import BookSnapshot
    from poly_weather.shadow_runtime import _archive_pair_rows
    from poly_weather.shadow_spread_replay import paired_row_to_book_snapshot
    from poly_weather.weather_market_join import load_realtime_weather_observations

    root = tmp_path / "capture"
    store = CaptureStore(root, plan())
    store.wire("public_market_frame", json.dumps(full()), receipt_ns=1789031549000000000, epoch=1)
    store.finish("complete")
    path, rows = records(root)
    for source in [
        "polymarket_book_checkpoints",
        "polymarket_clob_websocket",
        "weather_daemon",
        "signal_snapshot",
        "polymarket_gamma_event",
        "settlement_evidence",
    ]:
        assert jsonl_archive_paths(root / "raw" / source) == []
    assert _archive_pair_rows(rows, {}, {}) == []
    assert list(iter_paired_book_snapshots([path], rule_index={})) == []
    assert discover_depth_event_coverage([path]) == ()
    assert load_realtime_weather_observations([path]) == ()
    assert not load_market_ws_trades([path]).trades
    for converter in [
        BookSnapshot.from_mapping,
        paired_row_to_book_snapshot,
        _as_snapshot,
        pair_snapshots_from_mapping,
    ]:
        with pytest.raises((ValueError, KeyError, TypeError)):
            converter(rows[0])
    # Actual directory entry points have no capture inputs, including fallback readers.
    assert not archived_liquidity_rows_from_jsonl(data_dir=root)
    assert not load_external_information_events(root)
    assert replay_books_at_or_before(data_dir=root, requests={}) == {}
