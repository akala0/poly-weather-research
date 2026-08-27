import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.public_trade_collection import (
    collect_depth_event_trades,
    discover_depth_event_coverage,
)


def _trade(asset: str, timestamp: datetime, tx: str) -> PublicTrade:
    return PublicTrade(
        proxy_wallet="wallet",
        asset_id=asset,
        condition_id="condition-1",
        event_slug="event-1",
        market_slug="event-1/market-1",
        outcome="No",
        side="SELL",
        size=Decimal("3"),
        price=Decimal("0.70"),
        timestamp=timestamp,
        transaction_hash=tx,
    )


def _checkpoint(at: datetime, *, asset: str, condition: str = "condition-1") -> dict[str, object]:
    return {
        "received_at": at.isoformat(),
        "market_slug": "event-1/market-1:No",
        "market_id": condition,
        "asset_id": asset,
        "event_type": "book",
        "bids": [],
        "asks": [],
    }


class _FakeClient:
    def __init__(self, trade: PublicTrade) -> None:
        self.trade = trade
        self.calls: list[tuple[tuple[str, ...], datetime, datetime]] = []

    def market_trades(self, *, market_ids, start, end, taker_only=True):
        assert taker_only is True
        self.calls.append((tuple(market_ids), start, end))
        return [self.trade]


def test_discover_depth_coverage_groups_market_condition_and_tokens(tmp_path) -> None:
    path = tmp_path / "2026-08-27" / "events.jsonl"
    path.parent.mkdir()
    start = datetime(2026, 8, 27, 0, tzinfo=UTC)
    rows = [_checkpoint(start, asset="no-token"), _checkpoint(start + timedelta(minutes=2), asset="yes-token")]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    coverage = discover_depth_event_coverage([path])

    assert len(coverage) == 1
    assert coverage[0].event_slug == "event-1"
    assert coverage[0].condition_ids == ("condition-1",)
    assert coverage[0].asset_ids == ("no-token", "yes-token")
    assert coverage[0].snapshot_count == 2
    assert coverage[0].start_at == start
    assert coverage[0].end_at == start + timedelta(minutes=2)


def test_collection_is_incremental_deduplicated_and_reports_intersection(tmp_path) -> None:
    start = datetime(2026, 8, 27, 0, tzinfo=UTC)
    checkpoint = tmp_path / "events.jsonl"
    checkpoint.write_text(
        "\n".join(
            [
                json.dumps(_checkpoint(start, asset="no-token")),
                json.dumps(_checkpoint(start + timedelta(minutes=2), asset="no-token")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    coverage = discover_depth_event_coverage([checkpoint])
    trade = _trade("no-token", start + timedelta(seconds=1), "tx-1")
    client = _FakeClient(trade)

    first = collect_depth_event_trades(
        coverage, client=client, output_dir=tmp_path / "trades", now=start + timedelta(hours=1)
    )
    assert first["events"][0]["collection_status"] == "collected_nonzero"
    assert first["events"][0]["trade_asset_intersection_count"] == 1
    assert len(client.calls) == 1

    second = collect_depth_event_trades(
        coverage, client=client, output_dir=tmp_path / "trades", now=start + timedelta(hours=2)
    )
    assert len(client.calls) == 1, "unchanged coverage must not refetch the full tape"
    payload = json.loads((tmp_path / "trades" / "event-1.json").read_text(encoding="utf-8"))
    assert payload["trade_count"] == 1
    assert second["events"][0]["collection_status"] == "collected_nonzero"

    checkpoint.write_text(
        "\n".join(
            [
                json.dumps(_checkpoint(start, asset="no-token")),
                json.dumps(_checkpoint(start + timedelta(minutes=3), asset="new-token")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    advanced = discover_depth_event_coverage([checkpoint])
    collect_depth_event_trades(
        advanced, client=client, output_dir=tmp_path / "trades", now=start + timedelta(hours=3)
    )
    assert len(client.calls) == 2
    assert client.calls[-1][1] == start + timedelta(minutes=2) - timedelta(seconds=2)
    payload = json.loads((tmp_path / "trades" / "event-1.json").read_text(encoding="utf-8"))
    assert payload["trade_count"] == 1
    assert len(payload["unmatched_snapshot_assets"]) == 1
