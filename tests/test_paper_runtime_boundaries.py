import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from poly_weather.paper_account import PaperLedger
from poly_weather.paper_spread_runtime import (
    PaperSpreadProcessor,
    PaperStrategyConfig,
    replay_paper_spread,
    run_paper_spread_continuous,
)
from poly_weather.shadow_orders import BookSnapshot, ShadowOrderState, ShadowSide, TradeEvent

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)


def snapshot(*, at: datetime = BASE, bid: str = "0.75", ask: str = "0.80", metadata: dict | None = None) -> BookSnapshot:
    source_at = at - timedelta(minutes=1)
    received_at = at - timedelta(seconds=30)
    return BookSnapshot(
        timestamp=at,
        event_id="event",
        market_id="market",
        token_id="token",
        station_id="KLAX",
        market_day="2026-09-02",
        season_version="season-v1",
        bids=((bid, "100"),),
        asks=((ask, "100"),),
        metadata={
            "weather_join_status": "aligned",
            "weather_observation_id": f"weather-{source_at.isoformat()}",
            "weather_source_timestamp": source_at.isoformat(),
            "weather_received_at": received_at.isoformat(),
            "weather_observation_new": True,
            "weather_market_lag": True,
            "weather_improving": True,
            "weather_worsening": False,
            "weather_unchanged": False,
            **(metadata or {}),
        },
    )


def processor(tmp_path) -> PaperSpreadProcessor:
    paper = PaperSpreadProcessor(
        ledger=PaperLedger(tmp_path / "paper.jsonl"),
        strategy=PaperStrategyConfig.load("configs/paper_spread_strategy_v1.json"),
    )
    paper.set_supervisor_evidence(
        generation="fixture-generation",
        active_event_ids={"event"},
        integrity="verified",
        as_of=BASE,
    )
    paper.set_quality_evidence(
        integrity="verified", refreshed_at=BASE, window_hash="fixture-quality"
    )
    return paper


def fill_first(paper: PaperSpreadProcessor) -> None:
    assert paper.process_snapshot(snapshot()) is not None
    paper.process_trade(TradeEvent(BASE + timedelta(minutes=1), "token", ShadowSide.SELL, "0.75", "200", "entry"))


def test_paper_continuous_first_start_tail_bootstraps_without_scoring_history(tmp_path) -> None:
    root = tmp_path / "data"
    archive = root / "raw" / "polymarket_book_checkpoints" / "2026-09-02" / "events.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        "\n".join(
            json.dumps(
                {
                    "received_at": BASE.isoformat(),
                    "market_slug": f"event/market:{outcome}",
                    "asset_id": asset,
                    "bids": [{"price": "0.75", "size": "100"}],
                    "asks": [{"price": "0.80", "size": "100"}],
                    "book_complete": True,
                }
            )
            for outcome, asset in (("Yes", "yes"), ("No", "token"))
        )
        + "\n",
        encoding="utf-8",
    )
    status = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "paper-status.json",
        cursor_path=root / "runtime" / "paper-cursor.json",
        strategy_config_path="configs/paper_spread_strategy_v1.json",
        max_cycles=1,
        poll_seconds=0.01,
    )
    assert status["cursor_bootstrap_mode"] == "tail_of_existing_archives"
    assert status["data_coverage"]["new_market_rows"] == 0
    assert status["active_orders"] == 0


def test_paper_continuous_rejects_unverified_ws_trade(tmp_path) -> None:
    root = tmp_path / "data"
    checkpoint = root / "raw" / "polymarket_book_checkpoints" / "events" / "events.jsonl"
    websocket = root / "raw" / "polymarket_clob_websocket" / "trades" / "events.jsonl"
    for path in (checkpoint, websocket):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    cursor_path = root / "runtime" / "cursor.json"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text(
        json.dumps(
            {
                "sources": {
                    str(websocket.resolve()): {"offset": 0, "line": 0},
                    str(checkpoint.resolve()): {"offset": 0, "line": 0},
                }
            }
        ),
        encoding="utf-8",
    )
    websocket.write_text(
        json.dumps(
            {
                "run_id": "run",
                "sequence": 1,
                "received_at": BASE.isoformat(),
                "source_timestamp_ms": int(BASE.timestamp() * 1000),
                "event_type": "last_trade_price",
                "asset_id": "token",
                "market_id": "market",
                "market_slug": "event/market:No",
                "upstream_status": "normal",
                "last_trade_price": "0.75",
                "raw": {
                    "market": "market",
                    "asset_id": "token",
                    "price": "0.75",
                    "size": "200",
                    "side": "SELL",
                    "event_type": "last_trade_price",
                    "timestamp": int(BASE.timestamp() * 1000),
                    "transaction_hash": "unmatched",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    status = run_paper_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders" / "paper.jsonl",
        status_path=root / "runtime" / "paper-status.json",
        cursor_path=cursor_path,
        strategy_config_path="configs/paper_spread_strategy_v1.json",
        max_cycles=1,
        poll_seconds=0.01,
    )
    assert status["data_coverage"]["new_ws_trade_rows"] == 1
    assert status["data_coverage"]["accepted_queue_trade_rows"] == 0
    assert status["data_coverage"]["unmatched_ws_trade_count"] == 1


def test_paper_replay_uses_event_clock_not_wall_clock(tmp_path) -> None:
    at = datetime(2020, 1, 1, 12, tzinfo=UTC)
    status = replay_paper_spread(
        [snapshot(at=at)],
        ledger_path=tmp_path / "paper.jsonl",
        strategy_config_path="configs/paper_spread_strategy_v1.json",
    )
    assert status["checked_at"] == at.isoformat()
    assert status["overdue_orders"] == 0


def test_weather_worsening_cancels_and_releases_buy_reservation(tmp_path) -> None:
    paper = processor(tmp_path)
    order = paper.process_snapshot(snapshot())
    assert order is not None
    worsening = snapshot(at=BASE + timedelta(minutes=1), metadata={"weather_worsening": True})
    assert paper.process_snapshot(worsening) is None
    assert order.state is ShadowOrderState.CANCELLED
    assert paper.account.buy_reserved_usd == Decimal("0")
    # A later otherwise-eligible confirmation cannot use worsening as a
    # temporary cancellation before silently replenishing the same portfolio.
    assert paper.process_snapshot(
        snapshot(at=BASE + timedelta(minutes=2), metadata={"weather_worsening": False})
    ) is None
    assert len(paper.ledger.orders) == 1


def test_partial_exit_is_not_advanced_until_order_fully_fills(tmp_path) -> None:
    paper = processor(tmp_path)
    fill_first(paper)
    order = paper._submit_exit_if_eligible(snapshot(at=BASE + timedelta(minutes=2), bid="0.80", ask="0.85"))
    assert order is not None
    paper.process_trade(TradeEvent(BASE + timedelta(minutes=3), "token", ShadowSide.BUY, "0.85", "101", "partial-exit"))
    state = next(iter(paper.states.values()))
    assert 0 not in state.completed_exit_stages
    assert state.exit_stage_filled_shares[0] > Decimal("0")


def test_windows_paper_runner_arguments_are_supervised_and_isolated() -> None:
    """Inspect the command template only; never execute the PowerShell runner."""

    script = Path("scripts/windows/poly-weather-daemon-runner.ps1").read_text(encoding="utf-8")
    marker = '"paper-spread-engine" {'
    block = script[script.index(marker) : script.index("    }", script.index(marker))]
    assert '"paper-spread-engine", "--supervised", "--runtime", "0", "--data-dir", $dataDir' in block
    assert '"--strategy-config", (Join-Path $ProjectRoot "configs\\paper_spread_strategy_v1.json")' in block
    assert '"--ledger", (Join-Path $dataDir "raw\\shadow_orders\\paper_spread_v1_orders.jsonl")' in block
    assert '"--status", (Join-Path $dataDir "runtime\\paper_spread_v1_status.json")' in block
    assert '"--cursor", (Join-Path $dataDir "runtime\\paper_spread_v1_cursor.json")' in block
    assert "--once" not in block
