"""Real isolated follower equivalence; no CLI or operational archive access."""

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest
from runtime_health_support import seed_test_chain
from test_paper_recovery import make_processor, submit_first
from test_paper_seal_blockers import _weather_row

from poly_weather import paper_spread_runtime as runtime
from poly_weather.shadow_runtime import ShadowCursor


class ProcessCrash(BaseException):
    pass


@pytest.mark.parametrize("crash", [None, "before_cursor", "after_cursor"])
@pytest.mark.parametrize("future_append", [False, True])
def test_ar_real_follower_plain_gzip_restart_equivalence(tmp_path, monkeypatch, crash, future_append):
    base = datetime(2026, 9, 2, 12, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (base + timedelta(minutes=7)).astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    # A known isolated ledger baseline exercises nonempty order/queue/account
    # recovery; it is MODEL_KERNEL setup, not public trade admission evidence.
    initial = make_processor(tmp_path / "seed")
    initial_order = submit_first(initial)
    assert initial.account.buy_reserved_usd > 0
    ledger_prefix = initial.ledger.path.read_bytes()
    original_read = runtime._incremental_jsonl_rows
    original_observation = runtime._parse_observation_safely
    trace = []
    weather_trace = []

    def read(path, position, **kwargs):
        result = original_read(path, position, **kwargs)
        trace.extend(row["fixture_input_id"] for row in result)
        return result

    def observation(row):
        result = original_observation(row)
        weather_trace.extend(item.observation_id for item in result)
        return result

    monkeypatch.setattr(runtime, "_incremental_jsonl_rows", read)
    monkeypatch.setattr(runtime, "_parse_observation_safely", observation)

    def run(root):
        return runtime.run_paper_spread_continuous(
            data_dir=root, ledger_path=root / "ledger.jsonl", status_path=root / "status.json",
            cursor_path=root / "cursor.json", checkpoint_path=root / "checkpoint.json",
            strategy_config_path="configs/paper_spread_strategy_v1.json", bootstrap_at_tail=False,
            max_cycles=1, poll_seconds=0.001)

    results = []
    for variant in ("plain", "rotated"):
        root = tmp_path / variant
        seed_test_chain(root, monkeypatch, now=base + timedelta(minutes=7))
        (root / "ledger.jsonl").write_bytes(ledger_prefix)
        sources = [root / "raw" / name / "day" / "events.jsonl"
                   for name in ("polymarket_book_checkpoints", "weather_daemon")]
        for path in sources:
            path.parent.mkdir(parents=True)
        trace.clear()
        weather_trace.clear()
        for batch in range(2):
            at = base + timedelta(minutes=5 * batch)
            books = [{"fixture_input_id": f"book-{batch}-{side}", "received_at": at.isoformat(),
                      "market_slug": f"highest-temperature-in-los-angeles-on-september-2-2026/market:{side}",
                      "asset_id": token, "bids": [{"price": "0.75", "size": "100"}],
                      "asks": [{"price": "0.80", "size": "100"}], "book_complete": True}
                     for side, token in (("Yes", "yes"), ("No", "token"))]
            weather = _weather_row(at=at - timedelta(minutes=1), temperature_f=str(70 + batch),
                                   observation_id=f"天气-{batch}")
            weather["fixture_input_id"] = f"weather-{batch}"
            for path, rows in zip(sources, (books, [weather]), strict=True):
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            if variant == "rotated" and batch == 1:
                for path in sources:
                    path.with_suffix(".jsonl.gz").write_bytes(gzip.compress(path.read_bytes()))
                if crash:
                    save = ShadowCursor.save

                    def fail_save(cursor, root=root, save=save):
                        if cursor.path == root / "cursor.json":
                            if crash == "before_cursor":
                                raise ProcessCrash()
                            save(cursor)
                            raise ProcessCrash()
                        save(cursor)

                    with monkeypatch.context() as patch:
                        patch.setattr(ShadowCursor, "save", fail_save)
                        with pytest.raises(ProcessCrash):
                            run(root)
                else:
                    status = run(root)  # Both representations exist; consumes suffix once.
                for path in sources:
                    path.unlink()  # Only test-owned plain representation.
            status = run(root)
            assert status.get("last_error") is None
        if future_append and variant == "rotated":
            # Source-time-old, receipt-time-late revisions, plus a later vintage
            # and closed-event metadata, cannot rewrite the fixed-clock prefix.
            late = _weather_row(at=base, temperature_f="110", observation_id="late-qc")
            late.update(received_at=(base + timedelta(hours=1)).isoformat(), fixture_input_id="future-weather")
            forecast = {**late, "product": "multi_model_deterministic_forecast",
                        "raw": {"lead_days": 0, "run_initialization": (base + timedelta(hours=1)).isoformat()}}
            future_book = {**books[0], "received_at": (base + timedelta(hours=1)).isoformat(),
                           "closed": True, "fixture_input_id": "future-settlement"}
            for source, suffix in zip(sources, ([future_book], [late, forecast]), strict=True):
                archive = source.with_suffix(".jsonl.gz")
                payload = gzip.decompress(archive.read_bytes())
                payload += "".join(json.dumps(row) + "\n" for row in suffix).encode()
                archive.write_bytes(gzip.compress(payload))
        for _ in range(2):
            status = run(root)
            assert status.get("last_error") is None
            assert status["data_coverage"]["new_market_rows"] == 0
            assert status["data_coverage"]["new_weather_rows"] == 0
        cursor = ShadowCursor.load(root / "cursor.json")
        positions = sorted((value["offset"], value["line"], value["prefix_sha256"])
                           for value in cursor.sources.values())
        checkpoint = json.loads((root / "checkpoint.json").read_text())
        assert checkpoint["orders"]
        assert checkpoint["orders"][0]["order_id"] == initial_order.order_id
        results.append((positions, cursor.paper_state, status["account"], checkpoint["portfolio_state"], checkpoint["orders"]))
        assert set(trace) == {"book-0-Yes", "book-0-No", "book-1-Yes", "book-1-No", "weather-0", "weather-1"}
        assert set(weather_trace) == {"天气-0", "天气-1"}
        if not crash or variant == "plain" or crash == "after_cursor":
            assert len(trace) == 6
        else:
            assert len(trace) == 9  # Crash before acknowledgment re-reads suffix, not prefix.
        assert status["data_coverage"]["accepted_queue_trade_rows"] == 0
    assert results[0] == results[1]
