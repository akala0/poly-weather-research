"""Safe one-pass runtime for the read-only shadow strategy.

The runtime consumes local archives and writes only a permanent shadow ledger
and status JSON.  It has no network execution client, no credentials and no
order-submission method.  ``--supervised`` is required by the CLI so an
operator cannot mistake this diagnostic process for a trading daemon.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poly_weather.archive_io import jsonl_archive_paths
from poly_weather.config import load_settlement_registry
from poly_weather.real_no_books import archived_event_metadata, paired_book_snapshots
from poly_weather.shadow_orders import (
    ShadowLedger,
    ShadowOrder,
    ShadowOrderEngine,
    inventory_risk_summary,
)
from poly_weather.shadow_spread_replay import (
    default_shadow_strategy_config,
    replay_shadow_spread,
)


def run_shadow_spread_once(
    *,
    data_dir: Path | str = Path("data"),
    ledger_path: Path | str = Path("data/raw/shadow_orders/shadow_orders.jsonl"),
    status_path: Path | str = Path("data/runtime/shadow_spread_status.json"),
    config_path: Path | str | None = None,
    supervised: bool = True,
) -> dict[str, Any]:
    """Replay the local archive once and persist idempotent shadow state."""
    if supervised is not True:
        raise ValueError("shadow runtime requires supervised=true")
    root = Path(data_dir)
    ledger = ShadowLedger(ledger_path)
    checkpoint_paths = jsonl_archive_paths(root / "raw" / "polymarket_book_checkpoints")
    pairs = paired_book_snapshots(checkpoint_paths)
    registry_path = root / ".." / "configs" / "settlements.json"
    if not registry_path.exists():
        registry_path = Path("configs/settlements.json")
    metadata = {}
    if pairs and registry_path.exists():
        registry = load_settlement_registry(registry_path)
        metadata = archived_event_metadata(
            sorted({str(pair["event_slug"]) for pair in pairs}), registry.specs
        )
    replay_pairs: list[dict[str, Any]] = []
    policy_path = Path("configs/warming_window_no_thresholds.json")
    policy_payload = (
        json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    )
    for pair in pairs:
        event_value = metadata.get(str(pair.get("event_slug") or ""), {})
        station = str(event_value.get("station_id") or "")
        target = str(event_value.get("target_date") or "")
        enriched = dict(pair)
        for season in (policy_payload.get("stations") or {}).get(station, {}).get("seasons", ()):
            start = str(season.get("window_start") or "")
            end = str(season.get("window_end") or "")
            if start and end and start <= target <= end:
                enriched["season_version"] = str(
                    season.get("threshold_version") or policy_payload.get("policy_version") or ""
                )
                enriched["in_season"] = True
                break
        replay_pairs.append(enriched)
    if config_path is None:
        strategy = default_shadow_strategy_config()
    else:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        from poly_weather.shadow_orders import ShadowStrategyConfig

        strategy = ShadowStrategyConfig.from_mapping(payload)
    result = replay_shadow_spread(replay_pairs, event_metadata=metadata, config=strategy)
    queue_orders = result["models"]["queue_aware"].get("orders") or []
    persisted = 0
    for payload in queue_orders:
        order = ShadowOrder.from_dict(payload)
        if ledger.by_idempotency(order.idempotency_key) is None:
            ledger.save(order, event_key=f"runtime:{order.order_id}")
            persisted += 1
    # Reconstructing the local engine is safe: it reads only the append-only
    # ledger, and no network/execution state is consulted.
    ledger_engine = ShadowOrderEngine(
        ledger=ledger,
        budget_usd=strategy.market_budget_usd,
        require_season_version=False,
    )
    risk = inventory_risk_summary(ledger_engine)
    active = [order for order in ledger.orders.values() if order.is_active]
    fills = [fill for order in ledger.orders.values() for fill in order.fills]
    status = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "execution_enabled": False,
        "supervised": True,
        "runtime_mode": "read_only_shadow_archive_pass",
        "strategy_version": strategy.version,
        "active_orders": len(active),
        "active_order_ids": [order.order_id for order in active],
        "inventory_shares": risk["inventory_shares"],
        "average_inventory_cost": risk["average_inventory_cost"],
        "inventory_cost_usd": risk["inventory_cost_usd"],
        "active_reserved_usd": risk["active_reserved_usd"],
        "available_budget_usd": risk["available_budget_usd"],
        "realized_pnl_usd": risk["realized_pnl_usd"],
        "unrealized_pnl_usd": None,
        "capital_minutes": None,
        "fill_count": len(fills),
        "ledger_order_count": len(ledger.orders),
        "persisted_this_pass": persisted,
        "restart_idempotent": True,
        "recent_rejection_reasons": [],
        "raw_snapshot_count": result["raw_snapshot_count"],
        "independent_market_day_count": result["independent_market_day_count"],
        "models": {
            key: {
                "shadow_order_count": value["shadow_order_count"],
                "fill_count": value["fill_count"],
                "full_fill_rate": value["full_fill_rate"],
            }
            for key, value in result["models"].items()
        },
        "limitations": [
            "archive pass only; it never calls a Polymarket execution endpoint",
            "public trade prices are not quotes and touch fills are an upper bound",
        ],
    }
    destination = Path(status_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return status
