import json
from datetime import UTC, datetime
from decimal import Decimal

from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    ReplenishMode,
    ShadowLedger,
    ShadowOrderState,
    ShadowSide,
    ShadowStrategyConfig,
    TradeEvent,
)
from poly_weather.shadow_runtime import (
    ShadowCursor,
    ShadowStreamProcessor,
    _archive_pair_rows,
    _public_trade_events_from_file,
    run_shadow_spread_continuous,
    run_shadow_spread_once,
)
from poly_weather.shadow_spread_replay import default_shadow_strategy_config


def test_shadow_runtime_requires_supervision_and_persists_status(tmp_path) -> None:
    try:
        run_shadow_spread_once(
            data_dir=tmp_path,
            ledger_path=tmp_path / "raw" / "shadow_orders.jsonl",
            status_path=tmp_path / "runtime" / "status.json",
            supervised=False,
        )
    except ValueError as exc:
        assert "supervised" in str(exc)
    else:
        raise AssertionError("unsupervised shadow runtime must fail closed")
    status_path = tmp_path / "runtime" / "status.json"
    status = run_shadow_spread_once(
        data_dir=tmp_path,
        ledger_path=tmp_path / "raw" / "shadow_orders.jsonl",
        status_path=status_path,
        supervised=True,
    )
    assert status["execution_enabled"] is False
    assert status["restart_idempotent"] is True
    assert "average_inventory_cost" in status
    assert status["unrealized_pnl_usd"] is None
    assert json.loads(status_path.read_text(encoding="utf-8"))["supervised"] is True
    assert status["execution_dependency_scan"]["status"] == "clear"


def test_shadow_cursor_recovers_from_malformed_positions(tmp_path) -> None:
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps(
            {
                "restart_count": "bad",
                "sources": {
                    "good": {"offset": "4", "line": "2"},
                    "bad": {"offset": "not-an-int", "line": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    cursor = ShadowCursor.load(path)
    assert cursor.restart_count == 1
    assert cursor.sources == {"good": {"offset": 4, "line": 2}}


def test_shadow_continuous_cursor_is_idempotent_and_read_only(tmp_path, monkeypatch) -> None:
    from runtime_health_support import seed_test_chain

    root = tmp_path / "data"
    seed_test_chain(root, monkeypatch)
    path = root / "raw" / "polymarket_book_checkpoints" / "2026-08-27" / "events.jsonl"
    path.parent.mkdir(parents=True)
    rows = []
    for outcome, asset in (("Yes", "yes"), ("No", "no")):
        rows.append(
            {
                "received_at": "2026-08-27T12:00:00+00:00",
                "event_type": "book",
                "market_id": "condition",
                "market_slug": f"event/market:{outcome}",
                "asset_id": asset,
                "best_bid": "0.40",
                "best_ask": "0.50",
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.50", "size": "100"}],
                "book_complete": True,
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    status_path = root / "runtime" / "status.json"
    cursor_path = root / "runtime" / "cursor.json"
    ledger_path = root / "raw" / "shadow_orders.jsonl"
    first = run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=status_path,
        cursor_path=cursor_path,
        supervised=True,
        runtime_seconds=0.01,
        max_cycles=1,
    )
    second = run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=ledger_path,
        status_path=status_path,
        cursor_path=cursor_path,
        supervised=True,
        runtime_seconds=0.01,
        max_cycles=1,
    )
    assert first["execution_enabled"] is False
    assert first["started_at"]
    assert first["cursor"]["sources"]
    assert second["cycle_count"] == 1
    assert second["cursor"]["restart_count"] >= 1
    assert second["data_coverage"]["new_market_rows"] == 0


def test_continuous_tail_bootstrap_skips_existing_archive_history(tmp_path) -> None:
    root = tmp_path / "data"
    path = root / "raw" / "polymarket_book_checkpoints" / "2026-08-27" / "events.jsonl"
    path.parent.mkdir(parents=True)
    row = {
        "received_at": "2026-08-27T12:00:00+00:00",
        "market_slug": "event/market:Yes",
        "asset_id": "yes",
        "book_complete": True,
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    cursor_path = root / "runtime" / "cursor.json"

    status = run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        supervised=True,
        max_cycles=1,
        runtime_seconds=0.01,
        bootstrap_at_tail=True,
    )

    saved = json.loads(cursor_path.read_text(encoding="utf-8"))
    position = saved["sources"][str(path.resolve())]
    assert status["data_coverage"]["new_market_rows"] == 0
    assert status["cursor"]["bootstrap_mode"] == "tail_of_existing_archives"
    assert status["cursor"]["bootstrap_source_count"] == 1
    assert position["offset"] == path.stat().st_size


def test_shadow_continuous_restores_pair_context_after_restart(tmp_path, monkeypatch) -> None:
    from runtime_health_support import seed_test_chain

    root = tmp_path / "data"
    seed_test_chain(root, monkeypatch)
    path = root / "raw" / "polymarket_book_checkpoints" / "2026-08-27" / "events.jsonl"
    path.parent.mkdir(parents=True)
    rows = [
        {
            "received_at": "2026-08-27T12:00:00+00:00",
            "event_type": "book",
            "market_id": "condition",
            "market_slug": "event/market:Yes",
            "asset_id": "yes",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "book_complete": True,
        },
        {
            "received_at": "2026-08-27T12:00:00+00:00",
            "event_type": "book",
            "market_id": "condition",
            "market_slug": "event/market:No",
            "asset_id": "no",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "book_complete": True,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    cursor_path = root / "runtime" / "cursor.json"
    run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        supervised=True,
        max_cycles=1,
        runtime_seconds=0.01,
    )
    new_no = {**rows[1], "received_at": "2026-08-27T12:05:00+00:00"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(new_no) + "\n")
    # The persisted pair context lets the newly appended No side pair with
    # the prior Yes side after restart.
    status = run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=cursor_path,
        supervised=True,
        max_cycles=1,
        runtime_seconds=0.01,
    )
    saved = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert "event/market\u0000no" in saved["pair_latest"]
    assert status["data_coverage"]["new_market_rows"] == 1


def test_pairer_promotes_quality_state_to_cancel_active_orders() -> None:
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    rows = [
        {
            "received_at": at.isoformat(),
            "market_slug": "event/market:Yes",
            "asset_id": "yes",
            "market_id": "condition",
            "book_complete": True,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "upstream_status": "maintenance",
        },
        {
            "received_at": at.isoformat(),
            "market_slug": "event/market:No",
            "asset_id": "no",
            "market_id": "condition",
            "book_complete": True,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.50", "size": "100"}],
            "upstream_status": "maintenance",
        },
    ]
    pairs = _archive_pair_rows(rows, {}, {})
    assert pairs[0]["upstream_status"] == "maintenance"
    assert pairs[0]["quality_excluded"] is True


def test_continuous_persists_queue_position_changes(tmp_path) -> None:
    ledger_path = tmp_path / "ledger.jsonl"
    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(ledger_path), strategy=default_shadow_strategy_config()
    )
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    snapshot = BookSnapshot(
        timestamp=at,
        event_id="event",
        market_id="market",
        token_id="no",
        station_id="KLAX",
        market_day="2026-08-27",
        season_version="heat-v1",
        bids=(("0.70", "10"),),
        asks=(("0.80", "100"),),
    )
    processor.process_snapshot(snapshot)
    processor.process_trade(
        TradeEvent(at.replace(minute=at.minute + 1), "no", ShadowSide.SELL, "0.70", "5", "tx")
    )
    restored = ShadowLedger(ledger_path)
    order = next(iter(restored.orders.values()))
    assert order.volume_ahead == Decimal("5")


def test_continuous_skips_ambiguous_same_second_api_trades(tmp_path) -> None:
    strategy = default_shadow_strategy_config()
    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(tmp_path / "ledger.jsonl"), strategy=strategy
    )
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    processor.process_snapshot(
        BookSnapshot(
            timestamp=at,
            event_id="event",
            market_id="market",
            token_id="no",
            station_id="KLAX",
            market_day="2026-08-27",
            season_version="heat-v1",
            bids=(("0.70", "10"),),
            asks=(("0.80", "100"),),
        )
    )
    processor.process_trades(
        (
            TradeEvent(at.replace(minute=at.minute + 1), "no", ShadowSide.SELL, "0.70", "5"),
            TradeEvent(at.replace(minute=at.minute + 1), "no", ShadowSide.SELL, "0.70", "5"),
        )
    )
    engine = next(
        engine for key, engine in processor.engines.items() if key.token_id == "no"
    )
    assert engine.inventory_shares == Decimal("0")
    assert not any(order.fills for order in engine.orders)


def test_processor_replenishes_only_after_fill_and_exits_in_legs(tmp_path) -> None:
    base = default_shadow_strategy_config()
    strategy = ShadowStrategyConfig(
        version=base.version,
        quote_mode=base.quote_mode,
        fill_model=FillModel.QUEUE_AWARE,
        entry_bands={"KLAX": ((Decimal("0.70"), Decimal("0.85")),)},
        target_rise=Decimal("0.10"),
        order_timeout=base.order_timeout,
        max_active_orders=base.max_active_orders,
        market_budget_usd=Decimal("40"),
        tranche_usd=(Decimal("20"), Decimal("20")),
        replenish_mode=ReplenishMode.CONFIRMATION,
        exit_targets=(Decimal("0.05"), Decimal("0.10")),
        partial_exit_fraction=Decimal("0.50"),
    )
    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(tmp_path / "ledger.jsonl"), strategy=strategy
    )
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    first = BookSnapshot(
        timestamp=at,
        event_id="event",
        market_id="market",
        token_id="no",
        station_id="KLAX",
        market_day="2026-08-27",
        season_version="heat-v1",
        bids=(("0.70", "10"),),
        asks=(("0.80", "100"),),
    )
    processor.process_snapshot(first)
    processor.process_trade(
        TradeEvent(at.replace(minute=at.minute + 1), "no", ShadowSide.SELL, "0.70", "100", "entry")
    )
    engine = processor.engines[first.portfolio_key]
    assert engine.inventory_shares > 0

    processor.process_snapshot(
        BookSnapshot(
            timestamp=at.replace(minute=at.minute + 2),
            event_id="event",
            market_id="market",
            token_id="no",
            station_id="KLAX",
            market_day="2026-08-27",
            season_version="heat-v1",
            bids=(("0.70", "10"),),
            asks=(("0.75", "100"),),
            metadata={"weather_improving": True},
        )
    )
    assert sum(order.side is ShadowSide.BUY for order in engine.orders) == 2

    processor.process_trade(
        TradeEvent(at.replace(minute=at.minute + 3), "no", ShadowSide.SELL, "0.70", "100", "entry-2")
    )
    processor.process_snapshot(
        BookSnapshot(
            timestamp=at.replace(minute=at.minute + 4),
            event_id="event",
            market_id="market",
            token_id="no",
            station_id="KLAX",
            market_day="2026-08-27",
            season_version="heat-v1",
            bids=(("0.75", "100"),),
            asks=(("0.80", "100"),),
        )
    )
    sell_orders = [order for order in engine.orders if order.side is ShadowSide.SELL]
    assert len(sell_orders) == 1
    assert sell_orders[0].requested_shares < engine.inventory_shares
    processor.process_trade(
        TradeEvent(at.replace(minute=at.minute + 5), "no", ShadowSide.BUY, "0.75", "100", "exit-1")
    )
    processor.process_snapshot(
        BookSnapshot(
            timestamp=at.replace(minute=at.minute + 6),
            event_id="event",
            market_id="market",
            token_id="no",
            station_id="KLAX",
            market_day="2026-08-27",
            season_version="heat-v1",
            bids=(("0.80", "100"),),
            asks=(("0.85", "100"),),
        )
    )
    assert any(order.side is ShadowSide.SELL and order.state is ShadowOrderState.RESTING for order in engine.orders)


def test_continuous_records_supervisor_generation(tmp_path, monkeypatch) -> None:
    from runtime_health_support import seed_test_chain

    root = tmp_path / "data"
    supervisor_path = root / "runtime" / "market_supervisor_status.json"
    supervisor_path.parent.mkdir(parents=True)
    supervisor_path.write_text(json.dumps({"generation": 41}), encoding="utf-8")
    seed_test_chain(root, monkeypatch)
    status = run_shadow_spread_continuous(
        data_dir=root,
        ledger_path=root / "raw" / "shadow_orders.jsonl",
        status_path=root / "runtime" / "status.json",
        cursor_path=root / "runtime" / "cursor.json",
        supervised=True,
        max_cycles=1,
        runtime_seconds=0.01,
    )
    assert status["cursor"]["generation"] == "41"


def test_public_trade_increment_is_receipt_gated_and_requires_identity(tmp_path) -> None:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "event_slug": "event",
                "fetched_at": "2026-08-27T12:05:00+00:00",
                "trades": [
                    {
                        "asset_id": "no",
                        "timestamp": "2026-08-27T12:01:00+00:00",
                        "side": "SELL",
                        "price": "0.70",
                        "size": "5",
                        "transaction_hash": "tx-1",
                        "available_at": "2026-08-27T12:05:00+00:00",
                    },
                    {
                        "asset_id": "no",
                        "timestamp": "2026-08-27T12:02:00+00:00",
                        "side": "SELL",
                        "price": "0.70",
                        "size": "5",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    events = _public_trade_events_from_file(path)
    assert len(events) == 1
    assert events[0].available_at == datetime(2026, 8, 27, 12, 5, tzinfo=UTC)


def test_continuous_hot_token_load_keeps_inventory_and_queue_state_isolated(tmp_path) -> None:
    ledger_path = tmp_path / "shadow_orders_v2.jsonl"
    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(ledger_path), strategy=default_shadow_strategy_config()
    )
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)

    def snapshot(token_id: str, market_id: str) -> BookSnapshot:
        return BookSnapshot(
            timestamp=at,
            event_id="event",
            market_id=market_id,
            token_id=token_id,
            station_id="KLAX",
            market_day="2026-08-27",
            season_version="heat-v1",
            bids=(("0.70", "10"),),
            asks=(("0.80", "100"),),
        )

    first = snapshot("token-a", "market-a")
    second = snapshot("token-b", "market-b")
    processor.process_snapshot(first)
    # This represents the supervisor publishing a second bucket after the
    # first one is already live.  It must create a second token portfolio.
    processor.process_snapshot(second)
    processor.process_trade(
        TradeEvent(at.replace(minute=1), "token-a", ShadowSide.SELL, "0.70", "100", "a-fill")
    )

    status = processor.status()
    engine_a = processor.engines[first.portfolio_key]
    engine_b = processor.engines[second.portfolio_key]
    assert engine_a.inventory_shares > Decimal("0")
    assert engine_b.inventory_shares == Decimal("0")
    assert status["portfolio_count"] == 2
    assert status["station_day_budgets"][0]["portfolio_count"] == 2

    restarted = ShadowStreamProcessor(
        ledger=ShadowLedger(ledger_path), strategy=default_shadow_strategy_config()
    )
    assert restarted.engines[first.portfolio_key].inventory_shares == engine_a.inventory_shares
    assert restarted.engines[second.portfolio_key].inventory_shares == Decimal("0")
    assert restarted.halted is False


def test_runtime_legacy_ledger_halts_without_rewriting_archival_v1_file(tmp_path) -> None:
    path = tmp_path / "shadow_orders_v1.jsonl"
    payload = {
        "schema_version": 1,
        "record_type": "order",
        "order": {
            "order_id": "legacy-order",
            "idempotency_key": "legacy-key",
            "event_id": "event",
            "market_id": "market",
            "token_id": "token",
            "station_id": "KLAX",
            "market_day": "2026-08-27",
            "side": "BUY",
            "state": "RESTING",
            "submitted_at": "2026-08-27T12:00:00+00:00",
            "limit_price": "0.70",
            "requested_shares": "10",
            "requested_usd": "7",
            "maker_assumption": True,
            "fill_model": "queue_aware",
            "tick_size": "0.01",
            "min_order_size": "5",
            "better_level_shares": "0",
            "volume_ahead": "0",
            "remaining_shares": "10"
        }
    }
    original = json.dumps(payload) + "\n"
    path.write_text(original, encoding="utf-8")

    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(path), strategy=default_shadow_strategy_config()
    )

    assert processor.halted is True
    assert processor.halt_reason == "legacy_ledger_schema_requires_token_scoped_v2_path"
    assert path.read_text(encoding="utf-8") == original


def test_persisted_token_discrepancy_keeps_runtime_halted_after_restart(tmp_path) -> None:
    path = tmp_path / "shadow_orders_v2.jsonl"
    ledger = ShadowLedger(path)
    ledger.record_discrepancy(code="sell_exceeds_same_token_inventory", details={"token_id": "token-b"})

    processor = ShadowStreamProcessor(
        ledger=ShadowLedger(path), strategy=default_shadow_strategy_config()
    )

    assert processor.halted is True
    assert processor.halt_reason == "sell_exceeds_same_token_inventory"
    assert processor.discrepancies
