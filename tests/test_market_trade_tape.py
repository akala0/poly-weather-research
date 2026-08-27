import json
from datetime import UTC, datetime
from decimal import Decimal

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.market_trade_tape import (
    WS_SIDE_SEMANTICS,
    build_shadow_trade_events,
    load_market_ws_trades,
    merge_trade_sources,
    parse_market_ws_trade,
    validate_ws_side_semantics,
)


def _row(*, side: str = "SELL", rich: bool = True, status: str = "normal") -> dict[str, object]:
    raw: dict[str, object] = {
        "market": "condition",
        "asset_id": "asset",
        "price": "0.70",
        "event_type": "last_trade_price",
        "timestamp": "1787820000000",
        "transaction_hash": "tx-1",
    }
    if rich:
        raw["size"] = "4"
        raw["side"] = side
    return {
        "run_id": "run",
        "sequence": 4,
        "received_at": "2026-08-27T09:00:01+00:00",
        "source_timestamp_ms": 1787820000000,
        "event_type": "last_trade_price",
        "asset_id": "asset",
        "market_id": "condition",
        "market_slug": "event/market:No",
        "upstream_status": status,
        "last_trade_price": "0.70",
        "raw": raw,
    }


def test_rich_ws_trade_parse_keeps_source_and_receipt_times() -> None:
    trade = parse_market_ws_trade(_row())
    assert trade.asset_id == "asset"
    assert trade.side.value == "SELL"
    assert trade.size == Decimal("4")
    assert trade.source_timestamp < trade.received_at
    assert trade.transaction_hash == "tx-1"
    assert WS_SIDE_SEMANTICS in trade.as_json()["side_semantics"]
    assert trade.to_trade_event(validated_aggressor_side=False) is None


def test_price_only_ws_event_fails_closed_and_quality_window_is_excluded(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [json.dumps(_row(rich=False)), json.dumps(_row(status="maintenance"))]
        )
        + "\n",
        encoding="utf-8",
    )
    result = load_market_ws_trades([path])
    assert result.trades == ()
    assert result.skipped == 1
    assert result.quality_excluded == 1
    assert result.skip_reasons["missing_side"] == 1
    assert result.skip_reasons["upstream_quality_window"] == 1


def test_cross_source_validation_and_ws_preference() -> None:
    ws = parse_market_ws_trade(_row())
    api = PublicTrade(
        proxy_wallet="wallet",
        asset_id="asset",
        condition_id="condition",
        event_slug="event",
        market_slug="market",
        outcome="No",
        side="SELL",
        size=Decimal("4"),
        price=Decimal("0.70"),
        timestamp=datetime.fromtimestamp(1787820000, tz=UTC),
        transaction_hash="tx-1",
    )
    validation = validate_ws_side_semantics([ws], [api])
    assert validation["status"] == "validated"
    assert validation["queue_use_allowed"] is True
    merged = merge_trade_sources([ws], [api])
    assert len(merged) == 1
    assert merged[0]["source"] == "market_ws"


def test_ws_without_hash_keeps_distinct_same_second_sequences(tmp_path) -> None:
    first = _row()
    second = _row()
    first["raw"] = {**first["raw"], "transaction_hash": None}
    second["raw"] = {**second["raw"], "transaction_hash": None}
    first["sequence"] = 4
    second["sequence"] = 5
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in (first, second)) + "\n", encoding="utf-8")
    result = load_market_ws_trades([path])
    assert len(result.trades) == 2


def test_unmatched_ws_rows_fail_closed_but_api_supplement_remains() -> None:
    matched_ws = parse_market_ws_trade(_row())
    unmatched_row = _row()
    unmatched_row["raw"] = {**unmatched_row["raw"], "transaction_hash": "tx-unmatched"}
    unmatched_ws = parse_market_ws_trade(unmatched_row)
    api = PublicTrade(
        proxy_wallet="wallet",
        asset_id="asset",
        condition_id="condition",
        event_slug="event",
        market_slug="market",
        outcome="No",
        side="SELL",
        size=Decimal("4"),
        price=Decimal("0.70"),
        timestamp=datetime.fromtimestamp(1787820000, tz=UTC),
        transaction_hash="tx-2",
    )
    matched_api = PublicTrade(
        proxy_wallet="wallet",
        asset_id="asset",
        condition_id="condition",
        event_slug="event",
        market_slug="market",
        outcome="No",
        side="SELL",
        size=Decimal("4"),
        price=Decimal("0.70"),
        timestamp=datetime.fromtimestamp(1787820000, tz=UTC),
        transaction_hash="tx-1",
    )
    events, validation = build_shadow_trade_events(
        [matched_ws, unmatched_ws], [matched_api, api]
    )
    assert validation["status"] == "validated"
    assert validation["unmatched_ws_trade_count"] == 1
    assert sum(event.source == "market_ws" for event in events) == 1
    assert sum(event.source == "data_api" for event in events) == 1
