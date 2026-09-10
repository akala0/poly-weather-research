"""Stage 2 production-entry counterexamples, using only temporary evidence."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_market_trade_tape import _row
from test_paper_seal_blockers import (
    STRATEGY_PATH,
    _runtime_base,
    _seed_cycle_inputs,
    _write_public_tape,
    _write_ws_trade,
    fill_trade,
    processor,
    restarted,
    snapshot,
    submit_first,
)
from test_paper_trade_evidence import public_match
from test_public_trade_collection import _FakeClient, _trade
from test_reliability_storage import coverage_fixture

from poly_weather.market_trade_tape import parse_market_ws_trade
from poly_weather.paper_spread_runtime import run_paper_spread_continuous
from poly_weather.public_trade_collection import collect_depth_event_trades
from poly_weather.shadow_runtime import ShadowCursor, _public_trade_events_from_file
from poly_weather.trade_tape_analysis import load_event_trade_tapes


def test_f03_distinct_clocks_and_duplicate_receipt(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    started, received, committed, written = [at + timedelta(hours=n) for n in (1, 2, 3, 4)]
    ticks = iter([started, received, committed, written])
    client = _FakeClient(_trade("token", at, "tx"))
    root = tmp_path / "tapes"
    collect_depth_event_trades(coverage, client=client, output_dir=root, now=at,
                              clock=lambda: next(ticks))
    tape = json.loads((root / "event-1.json").read_text())
    row = tape["trades"][0]
    assert row["request_started_at"] == started.isoformat()
    assert row["response_received_at"] == row["first_seen_at"] == received.isoformat()
    assert row["receipt_committed_at"] == row["available_at"] == committed.isoformat()
    assert tape["file_written_at"] == written.isoformat()
    collect_depth_event_trades([replace(coverage[0], end_at=at + timedelta(minutes=1))],
                              client=client, output_dir=root, clock=lambda: written)
    assert json.loads((root / "event-1.json").read_text())["trades"][0] == row


def test_f03_legacy_missing_receipt_remains_historical_only(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    root.mkdir()
    row = _trade("token", at, "tx").as_json()
    tape = root / "event-1.json"
    tape.write_text(json.dumps({"event_slug": "event-1", "fetched_at": at.isoformat(),
                                "trades": [row]}))
    collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                              output_dir=root, clock=lambda: at + timedelta(hours=1))
    assert load_event_trade_tapes(root)["event-1"][0].available_at is None
    assert _public_trade_events_from_file(tape) == ()


def test_f03_failure_retry_and_late_sibling_freeze_old_receipt(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    root = tmp_path / "tapes"
    first_trade = _trade("token", at, "tx-a")
    collect_depth_event_trades(coverage, client=_FakeClient(first_trade), output_dir=root,
                              clock=lambda: at + timedelta(hours=1))
    tape = root / "event-1.json"
    original = json.loads(tape.read_text())["trades"][0]
    advanced = [replace(coverage[0], end_at=at + timedelta(minutes=1))]

    class RetryClient:
        failed = False

        def market_trades(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("fixture request failure")
            return [first_trade, _trade("token", at, "tx-b")]

    client = RetryClient()
    collect_depth_event_trades(advanced, client=client, output_dir=root,
                              clock=lambda: at + timedelta(hours=2))
    collect_depth_event_trades(advanced, client=client, output_dir=root,
                              clock=lambda: at + timedelta(hours=3))
    rows = json.loads(tape.read_text())["trades"]
    assert rows[0] == original
    assert rows[1]["first_seen_at"] == (at + timedelta(hours=3)).isoformat()
    assert rows[1]["receipt_provenance"] == "durable_public_receipt_v1"


def test_f03_backwards_response_clock_does_not_commit(tmp_path):
    at, coverage = coverage_fixture(tmp_path)
    ticks = iter([at + timedelta(hours=1), at])
    root = tmp_path / "tapes"
    with pytest.raises(ValueError, match="backwards"):
        collect_depth_event_trades(coverage, client=_FakeClient(_trade("token", at, "tx")),
                                  output_dir=root, clock=lambda: next(ticks))
    assert not (root / "event-1.json").exists()
    assert not (root / ".depth_trade_cursor.json").exists()


@pytest.mark.parametrize("quality", ["degraded", "maintenance", "unknown"])
def test_f01_pending_restart_cannot_upgrade_quality(tmp_path, quality):
    paper = processor(tmp_path)
    ws = parse_market_ws_trade(_row(status=quality))
    paper._record_pending_ws_trade(ws)
    for _ in range(2):
        paper = restarted(tmp_path / "paper.jsonl")
        paper._resolve_pending_ws_trades_from_public_tape([public_match(ws)])
        assert paper.pending_ws_trade_evidence
        restored = paper._pending_ws_row(next(iter(paper.pending_ws_trade_evidence.values())))
        assert restored.upstream_status == quality
        assert restored.run_id == ws.run_id


def test_f01_old_pending_missing_quality_is_unknown(tmp_path):
    paper = processor(tmp_path)
    _, details = paper._ws_trade_evidence(parse_market_ws_trade(_row()))
    details.pop("upstream_status")
    assert paper._pending_ws_row(details).upstream_status == "unknown"


def test_f02_late_evidence_invalidates_old_consumption_without_rewriting(tmp_path):
    from paper_model_support import model_trade

    paper = processor(tmp_path)
    submit_first(paper)
    # Isolated old model-consumption fixture, not current ingress eligibility.
    prior = fill_trade()
    assert model_trade(paper, prior)
    ledger = tmp_path / "paper.jsonl"
    prefix = ledger.read_bytes()
    before = next(iter(paper.ledger.orders.values())).as_dict()
    paper = restarted(ledger)
    sibling = replace(prior, event_id="late-sibling", sequence=2)
    assert paper.process_trade(sibling) == ()
    assert ledger.read_bytes().startswith(prefix)
    assert next(iter(paper.ledger.orders.values())).as_dict() == before
    assert paper.trade_evidence_counts["UNKNOWN_TRADE_GROUP_INVALIDATED"] == 1
    paper = restarted(ledger)
    assert paper.process_trade(sibling) == ()
    assert paper.trade_evidence_counts["UNKNOWN_TRADE_GROUP_INVALIDATED"] == 1
    assert paper.status()["paper_score_eligible"] is False


def test_f01_new_healthy_observation_does_not_rewrite_bad_pending(tmp_path):
    paper = processor(tmp_path)
    bad = parse_market_ws_trade(_row(status="degraded"))
    evidence_id = paper._record_pending_ws_trade(bad)
    original = dict(paper.pending_ws_trade_evidence[evidence_id])
    healthy = replace(bad, upstream_status="normal", run_id="new-run")
    paper._record_pending_ws_trade(healthy)
    paper = restarted(tmp_path / "paper.jsonl")
    paper._resolve_pending_ws_trades_from_public_tape([public_match(bad)])
    assert paper.pending_ws_trade_evidence[evidence_id]["upstream_status"] == original["upstream_status"]
    assert paper.pending_ws_trade_evidence[evidence_id]["run_id"] == original["run_id"]


@pytest.mark.parametrize("restart_between", [False, True])
def test_f02_split_unsequenced_group_never_consumes_queue(tmp_path, restart_between):
    paper = processor(tmp_path)
    submit_first(paper)
    for index, size in enumerate(("50", "51")):
        trade = replace(fill_trade(size=size), sequence=None, event_id=f"split-{index}")
        assert paper.process_trade(trade) == ()
        if restart_between:
            paper = restarted(tmp_path / "paper.jsonl")
        order = next(iter(paper.ledger.orders.values()))
        assert order.volume_ahead == 100
        assert order.filled_shares == 0


def follower_fixture(tmp_path):
    root = tmp_path / "isolated"
    book, cursor_path = _seed_cycle_inputs(root)
    rows = [json.loads(line) for line in book.read_text().splitlines()]
    for row in rows:
        row["asset_id"] = "checkpoint-" + row["asset_id"]
    book.write_text("".join(json.dumps(row) + "\n" for row in rows))
    at = _runtime_base()
    ledger = root / "paper.jsonl"
    paper = processor(tmp_path, ledger_path=ledger)
    paper.process_snapshot(snapshot(at=at))
    kwargs = dict(data_dir=root, ledger_path=ledger, status_path=root / "status.json",
                  cursor_path=cursor_path, checkpoint_path=root / "checkpoint.json",
                  strategy_config_path=STRATEGY_PATH, bootstrap_at_tail=False,
                  max_cycles=2, poll_seconds=0.01)
    return root, at + timedelta(seconds=1), ledger, kwargs


@pytest.mark.parametrize("quality", ["degraded", "maintenance", "unknown", None])
def test_f01_native_follower_two_polls_restart_preserves_quality(tmp_path, quality):
    root, at, ledger, kwargs = follower_fixture(tmp_path)
    ws = _write_ws_trade(root, transaction_hash="quality", at=at)
    row = json.loads(ws.read_text())
    row["upstream_status"] = quality
    row["upstream_incident_id"] = "fixture-incident"
    ws.write_text(json.dumps(row) + "\n")
    _write_public_tape(root, transaction_hash="quality", at=at)
    cursor = ShadowCursor.load(kwargs["cursor_path"])
    cursor.position(ws)
    cursor.save()
    for _ in range(2):
        run_paper_spread_continuous(**kwargs)
        paper = restarted(ledger)
        assert next(iter(paper.ledger.orders.values())).filled_shares == 0
        assert len(paper.pending_ws_trade_evidence) == 1
        evidence = next(iter(paper.pending_ws_trade_evidence.values()))
        assert evidence["upstream_status"] == (quality or "unknown")
        assert evidence["upstream_incident_id"] == "fixture-incident"
        assert evidence["run_id"] == "fixture"


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("sequence", [None, 1])
def test_f02_native_follower_batch_file_switch_restart(tmp_path, split, sequence):
    root, at, ledger, kwargs = follower_fixture(tmp_path)
    tape = _write_public_tape(root, transaction_hash="a", at=at)
    payload = json.loads(tape.read_text())
    first = payload["trades"][0]
    first.pop("sequence", None)
    if sequence is not None:
        first["sequence"] = sequence
    first["size"] = "50"
    second = {**first, "transaction_hash": "b", "size": "51"}
    if sequence is not None:
        second["sequence"] = sequence + 1
    if not split:
        payload["trades"].append(second)
    tape.write_text(json.dumps(payload))
    polls = 0

    def after_poll(stage):
        nonlocal polls
        if stage == "lifecycle_processing":
            polls += 1
            if split and polls == 1:
                (tape.parent / "later.json").write_text(json.dumps({**payload, "trades": [second]}))

    run_paper_spread_continuous(**kwargs, _fault_injector=after_poll)
    run_paper_spread_continuous(**kwargs)
    paper = restarted(ledger)
    order = next(iter(paper.ledger.orders.values()))
    assert order.filled_shares == 0
    assert order.volume_ahead == 100
    assert (paper.trade_evidence_counts["UNKNOWN_TRADE_SEQUENCE"]
            + paper.trade_evidence_counts["UNKNOWN_TRADE_GROUP_COMPLETENESS"]) > 0
    assert len(paper.trade_time_groups) == 1
    assert len(next(iter(paper.trade_time_groups.values()))) == 2
