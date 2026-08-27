"""Safe archive-pass and continuous runtime for the read-only shadow strategy.

    The runtime consumes local archives and writes only a permanent shadow ledger
and status JSON.  It has no network execution client, no credentials and no
order-submission method.  ``--supervised`` is required by the CLI so an
    operator cannot mistake this diagnostic process for a trading daemon.  The
    continuous mode follows append-only local feeds with a durable cursor; it
    still has no network execution client or credentials.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from poly_weather.archive_io import jsonl_archive_paths
from poly_weather.config import load_settlement_registry
from poly_weather.market_trade_tape import (
    build_shadow_trade_events,
    load_market_ws_trades,
    parse_market_ws_trade,
)
from poly_weather.polymarket_status import (
    load_quality_windows,
    market_record_is_analysis_eligible,
    quality_window_at,
)
from poly_weather.real_no_books import archived_event_metadata, paired_book_snapshots
from poly_weather.shadow_orders import (
    BookSnapshot,
    FillModel,
    QuoteMode,
    ReplenishMode,
    ShadowLedger,
    ShadowOrder,
    ShadowOrderEngine,
    ShadowOrderRejected,
    ShadowSide,
    ShadowStrategyConfig,
    TradeEvent,
    inventory_risk_summary,
    quote_limit,
)
from poly_weather.shadow_spread_replay import (
    _row_snapshot,
    default_shadow_strategy_config,
    replay_shadow_spread,
)
from poly_weather.trade_tape_analysis import load_event_trade_tapes
from poly_weather.weather_market_join import (
    align_weather_to_snapshots,
    load_realtime_weather_observations,
)

FORBIDDEN_EXECUTION_MODULES = frozenset(
    {"web3", "eth_account", "py_clob_client", "relayer", "ccxt"}
)


def execution_dependency_scan() -> dict[str, Any]:
    """Report forbidden live-trading modules loaded by this read-only process."""
    loaded = sorted(
        name
        for name in sys.modules
        if name == "web3"
        or name == "eth_account"
        or name == "py_clob_client"
        or name == "relayer"
        or name == "ccxt"
        or any(name.startswith(f"{prefix}.") for prefix in FORBIDDEN_EXECUTION_MODULES)
    )
    return {
        "status": "clear" if not loaded else "forbidden_modules_loaded",
        "forbidden_modules": loaded,
        "execution_enabled": False,
    }


@dataclass
class ShadowCursor:
    """Atomic line/byte positions for the append-only local feeds."""

    path: Path
    sources: dict[str, dict[str, int]] = field(default_factory=dict)
    generation: str | None = None
    restart_count: int = 0
    pair_latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    pair_last_emitted: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> ShadowCursor:
        destination = Path(path)
        if not destination.exists():
            return cls(destination)
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(destination)
        rows = payload.get("sources") if isinstance(payload, Mapping) else {}
        sources: dict[str, dict[str, int]] = {}
        for key, value in (rows or {}).items():
            if not isinstance(value, Mapping):
                continue
            try:
                offset = max(0, int(value.get("offset", 0)))
                line = max(0, int(value.get("line", 0)))
            except (TypeError, ValueError):
                continue
            sources[str(key)] = {"offset": offset, "line": line}
        try:
            restart_count = max(0, int(payload.get("restart_count") or 0)) + 1
        except (TypeError, ValueError):
            restart_count = 1
        pair_latest = {
            str(key): dict(value)
            for key, value in (payload.get("pair_latest") or {}).items()
            if isinstance(value, Mapping)
        }
        pair_last_emitted = {
            str(key): str(value)
            for key, value in (payload.get("pair_last_emitted") or {}).items()
            if value
        }
        return cls(
            destination,
            sources,
            str(payload.get("generation")) if payload.get("generation") else None,
            restart_count,
            pair_latest,
            pair_last_emitted,
        )

    def position(self, path: Path) -> dict[str, int]:
        return self.sources.setdefault(str(path.resolve()), {"offset": 0, "line": 0})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "generation": self.generation,
            "restart_count": self.restart_count,
            "sources": self.sources,
            "pair_latest": self.pair_latest,
            "pair_last_emitted": self.pair_last_emitted,
            "execution_enabled": False,
        }
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def _incremental_jsonl_rows(path: Path, position: dict[str, int]) -> list[dict[str, Any]]:
    """Read only newly appended complete lines; gzip uses a line watermark."""
    if not path.exists():
        return []
    # A supervisor may rotate a plain JSONL file in place.  Never seek beyond
    # the new file and silently lose its first rows.
    if path.suffix != ".gz" and path.stat().st_size < position.get("offset", 0):
        position["offset"] = 0
        position["line"] = 0
    rows: list[dict[str, Any]] = []
    if path.suffix == ".gz":
        from poly_weather.archive_io import open_jsonl_text

        with open_jsonl_text(path) as handle:
            for index, line in enumerate(handle):
                if index < position.get("line", 0):
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                position["line"] = index + 1
        position["offset"] = path.stat().st_size
        return rows
    with path.open("rb") as handle:
        handle.seek(position.get("offset", 0))
        payload = handle.read()
        complete = payload.rsplit(b"\n", 1)
        if not payload.endswith(b"\n"):
            payload = complete[0] if len(complete) == 2 else b""
        position["offset"] += len(payload)
    for line in payload.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
        position["line"] = position.get("line", 0) + 1
    return rows


def _archive_pair_rows(
    rows: Sequence[Mapping[str, Any]],
    latest: dict[tuple[str, str], dict[str, Any]],
    last_emitted: dict[str, datetime],
    *,
    quality_windows: Sequence[Any] = (),
    minimum_interval: timedelta = timedelta(minutes=5),
    maximum_side_age: timedelta = timedelta(minutes=5),
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for source in rows:
        try:
            timestamp = datetime.fromisoformat(str(source["received_at"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            continue
        if not source.get("book_complete") or not isinstance(source.get("bids"), list) or not isinstance(source.get("asks"), list):
            continue
        analysis_eligible = market_record_is_analysis_eligible(
            dict(source), timestamp, tuple(quality_windows)
        )
        text = str(source.get("market_slug") or "")
        base, separator, outcome = text.rpartition(":")
        if not separator or outcome.casefold() not in {"yes", "no"}:
            continue
        key = (base, outcome.casefold())
        row = dict(source)
        row["_timestamp"] = timestamp
        if not analysis_eligible:
            # Keep a degraded book in the live pairer solely to cancel active
            # shadow orders.  It can never create a new order because the
            # resulting BookSnapshot is health-failed and quality-excluded.
            row["quality_excluded"] = True
            if str(row.get("upstream_status") or "normal").casefold() == "normal":
                window = quality_window_at(tuple(quality_windows), timestamp)
                row["upstream_status"] = "maintenance" if window is not None else "degraded"
        latest[key] = row
        yes = latest.get((base, "yes"))
        no = latest.get((base, "no"))
        if yes is None or no is None:
            continue
        if abs(yes["_timestamp"] - no["_timestamp"]) > maximum_side_age:
            continue
        if base in last_emitted and timestamp - last_emitted[base] < minimum_interval:
            continue
        last_emitted[base] = timestamp
        pairs.append(
            {
                "observed_at": timestamp,
                "event_slug": base.split("/", 1)[0],
                "market_slug": base,
                "yes": yes,
                "no": no,
                "side_age_seconds": abs((yes["_timestamp"] - no["_timestamp"]).total_seconds()),
                # Promote quality state from either side to the paired row so
                # BookSnapshot.health_ok can cancel active orders during a
                # maintenance/recovery row without ever creating a new one.
                "upstream_status": (
                    str(no.get("upstream_status") or yes.get("upstream_status") or "normal")
                ),
                "quality_excluded": bool(
                    no.get("quality_excluded") or yes.get("quality_excluded")
                ),
            }
        )
    return pairs


class ShadowStreamProcessor:
    """Small persistent-state adapter for one or more market-days."""

    def __init__(
        self,
        *,
        ledger: ShadowLedger,
        strategy: ShadowStrategyConfig,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.ledger = ledger
        self.strategy = strategy
        self.metadata = metadata or {}
        self.engines: dict[tuple[str, str], ShadowOrderEngine] = {}
        self.last_market_event: str | None = None
        self.last_trade_event: str | None = None
        self.last_upstream_status: str = "normal"
        self.upstream_maintenance = False
        self.fill_count = 0
        self.rebuild_count = 0
        self._restore_from_ledger()

    def _restore_from_ledger(self) -> None:
        restored: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for order in self.ledger.orders.values():
            key = (order.station_id or "unknown", order.market_day)
            engine = self.engines.setdefault(
                key,
                ShadowOrderEngine(
                    budget_usd=self.strategy.market_budget_usd,
                    max_active_orders=self.strategy.max_active_orders,
                    fill_model=FillModel.QUEUE_AWARE,
                    order_timeout=self.strategy.order_timeout,
                    market_day=order.market_day,
                    require_season_version=self.strategy.require_season_version,
                ),
            )
            if not hasattr(engine, "_orders"):
                engine._orders = {}
            engine._orders[order.order_id] = order
            engine._local_idempotency[order.idempotency_key] = order
            restored[key].append(order)
        for key, orders in restored.items():
            engine = self.engines[key]
            # The in-memory engine is intentionally ledger-less, but it still
            # needs the ledger's seen event set to reject a replayed trade or
            # touch after restart.  Otherwise a partially filled order could
            # consume the same transaction twice.
            engine._seen_event_keys.update(getattr(self.ledger, "_seen_events", set()))
            for order in sorted(orders, key=lambda value: value.submitted_at):
                for fill in sorted(order.fills, key=lambda value: value.timestamp):
                    if order.side is ShadowSide.BUY:
                        engine.inventory_shares += fill.shares
                        engine.inventory_cost_usd += fill.shares * fill.price + fill.fee_usd
                        engine.cumulative_buy_cost_usd += fill.shares * fill.price + fill.fee_usd
                        engine.fees_usd += fill.fee_usd
                    else:
                        average = engine.average_inventory_cost or Decimal("0")
                        engine.inventory_shares = max(Decimal("0"), engine.inventory_shares - fill.shares)
                        engine.inventory_cost_usd = max(
                            Decimal("0"), engine.inventory_cost_usd - fill.shares * average
                        )
                        engine.realized_pnl_usd += fill.shares * (fill.price - average) - fill.fee_usd
                        engine.fees_usd += fill.fee_usd
        self.rebuild_count = 1 if self.ledger.orders else 0

    def _engine(self, snapshot: BookSnapshot) -> ShadowOrderEngine:
        key = (snapshot.station_id or "unknown", snapshot.market_day or snapshot.timestamp.date().isoformat())
        return self.engines.setdefault(
            key,
            ShadowOrderEngine(
                budget_usd=self.strategy.market_budget_usd,
                max_active_orders=self.strategy.max_active_orders,
                fill_model=FillModel.QUEUE_AWARE,
                order_timeout=self.strategy.order_timeout,
                market_day=key[1],
                require_season_version=self.strategy.require_season_version,
            ),
        )

    def _persist(self, engine: ShadowOrderEngine) -> None:
        for order in engine.orders:
            event_key = ":".join(
                (
                    "continuous",
                    order.order_id,
                    str(order.state),
                    str(order.filled_shares),
                    str(order.last_fill_at or ""),
                    str(order.cancelled_at or ""),
                    str(order.expired_at or ""),
                    # Queue position is mutable even when state/fill count is
                    # unchanged.  Include it in the idempotent event key so a
                    # restart cannot resurrect stale volume-ahead values.
                    str(order.better_level_shares),
                    str(order.volume_ahead),
                    str(order.remaining_shares),
                )
            )
            self.ledger.save(order, event_key=event_key)

    def process_snapshot(self, snapshot: BookSnapshot) -> None:
        engine = self._engine(snapshot)
        before = sum(len(order.fills) for order in engine.orders)
        engine.process_snapshot(snapshot)
        self.last_upstream_status = snapshot.upstream_status
        self.upstream_maintenance = snapshot.upstream_status.casefold() in {
            "maintenance",
            "degraded",
            "recovery",
        } or snapshot.quality_excluded
        # A stale/maintenance/weather-invalidated book is still useful for a
        # risk-only bid-depth exit, but it can never create a new maker order.
        if not snapshot.health_ok and engine.inventory_shares > Decimal("0"):
            engine.simulate_taker_exit(snapshot, reason="health_gate_risk_exit")
        if snapshot.health_ok:
            bands = self.strategy.entry_bands.get(snapshot.station_id or "", ())
            ask = snapshot.best_ask
            lag = bool(snapshot.metadata.get("weather_market_lag"))
            improving = bool(snapshot.metadata.get("weather_improving"))
            unchanged = bool(snapshot.metadata.get("weather_unchanged"))
            worsening = bool(snapshot.metadata.get("weather_worsening"))
            candidate = (
                any(lower <= ask < upper for lower, upper in bands) if ask is not None else False
            )
            if self.strategy.trigger_strategy == "weather_market_lag":
                candidate = lag and improving
            elif self.strategy.trigger_strategy == "either":
                candidate = candidate or (lag and improving)
            if candidate and not engine.active_orders and engine.inventory_shares <= Decimal("0"):
                quote = quote_limit(snapshot, mode=self.strategy.quote_mode)
                if quote is not None:
                    try:
                        engine.submit_limit(
                            snapshot,
                            side=ShadowSide.BUY,
                            limit_price=quote,
                            size_usd=self.strategy.tranche_usd[0],
                            idempotency_key=f"{snapshot.event_id}:{snapshot.market_id}:{snapshot.timestamp.isoformat()}:continuous-entry",
                            strategy_version=self.strategy.version,
                            season_version=snapshot.season_version,
                            trigger_reason="weather_market_lag" if lag and improving else "price_band",
                        )
                    except (ShadowOrderRejected, ValueError):
                        pass
            if engine.inventory_shares > Decimal("0") and not engine.active_orders:
                # Replenishment is a separate, explicit gate from the initial
                # entry.  The number of prior BUY orders is durable in the
                # ledger, so a restart cannot reset the tranche schedule.
                buy_orders = [order for order in engine.orders if order.side is ShadowSide.BUY]
                next_tranche = len(buy_orders)
                if (
                    self.strategy.replenish_mode is not ReplenishMode.NONE
                    and next_tranche < len(self.strategy.tranche_usd)
                ):
                    target = (engine.average_inventory_cost or Decimal("0")) + self.strategy.target_rise
                    quote = quote_limit(snapshot, mode=self.strategy.quote_mode)
                    if quote is not None:
                        try:
                            engine.submit_replenishment(
                                snapshot,
                                limit_price=quote,
                                size_usd=self.strategy.tranche_usd[next_tranche],
                                mode=self.strategy.replenish_mode,
                                target_price=target,
                                prior_order_resolved=True,
                                weather_improving=improving,
                                weather_unchanged=unchanged,
                                weather_worsening=worsening,
                                spread_ok=snapshot.spread is not None,
                                book_ok=snapshot.book_complete,
                                idempotency_key=(
                                    f"{snapshot.event_id}:{snapshot.market_id}:"
                                    f"{snapshot.timestamp.isoformat()}:replenish:{next_tranche}"
                                ),
                                strategy_version=self.strategy.version,
                            )
                        except (ShadowOrderRejected, ValueError):
                            pass
                if worsening and engine.inventory_shares > Decimal("0"):
                    engine.simulate_taker_exit(snapshot, reason="weather_worsening")
                else:
                    first_entry = min(
                        (
                            fill.timestamp
                            for order in engine.orders
                            if order.side is ShadowSide.BUY
                            for fill in order.fills
                        ),
                        default=None,
                    )
                    if (
                        first_entry is not None
                        and self.strategy.max_hold is not None
                        and snapshot.timestamp - first_entry >= self.strategy.max_hold
                    ):
                        engine.simulate_taker_exit(snapshot, reason="time_exit")
                if engine.inventory_shares > Decimal("0") and not engine.active_orders:
                    filled_exit_stages = [
                        int(order.metadata["exit_stage"])
                        for order in engine.orders
                        if order.side is ShadowSide.SELL
                        and order.filled_shares > Decimal("0")
                        and str(order.metadata.get("exit_stage", "")).isdigit()
                    ]
                    stage = max(filled_exit_stages, default=-1) + 1
                    target_rise = self.strategy.exit_targets[
                        min(stage, len(self.strategy.exit_targets) - 1)
                    ]
                    target = (engine.average_inventory_cost or Decimal("0")) + target_rise
                    if snapshot.best_bid is not None and snapshot.best_bid >= target:
                        exit_quote = quote_limit(
                            snapshot, side=ShadowSide.SELL, mode=QuoteMode.BEST_BID
                        )
                        if exit_quote is not None:
                            shares = engine.inventory_shares
                            if stage == 0 and self.strategy.partial_exit_fraction < Decimal("1"):
                                shares *= self.strategy.partial_exit_fraction
                            try:
                                engine.submit_limit(
                                    snapshot,
                                    side=ShadowSide.SELL,
                                    limit_price=exit_quote,
                                    shares=shares,
                                    idempotency_key=(
                                        f"{snapshot.event_id}:{snapshot.market_id}:"
                                        f"{snapshot.timestamp.isoformat()}:exit:{stage}"
                                    ),
                                    strategy_version=self.strategy.version,
                                    season_version=snapshot.season_version,
                                    trigger_reason="target_maker_exit",
                                    metadata={"exit_stage": stage},
                                )
                            except (ShadowOrderRejected, ValueError):
                                pass
        self._persist(engine)
        self.fill_count += sum(len(order.fills) for order in engine.orders) - before
        self.last_market_event = snapshot.timestamp.isoformat()

    def process_trade(self, trade: TradeEvent) -> None:
        self.process_trades((trade,))

    def process_trades(self, trades: Sequence[TradeEvent]) -> None:
        """Process a batch without inventing an intra-second API order.

        Data API timestamps have second precision and no sequence field.  A
        same-token same-second group is therefore rejected conservatively by
        ``ShadowOrderEngine.process_trades``; WS rows with a sequence remain
        eligible.  Grouping by token avoids discarding unrelated tokens that
        merely happen to share a wall-clock second.
        """
        ordered = sorted(
            trades,
            key=lambda row: (row.asset_id, row.timestamp, row.sequence is None, row.sequence or 0),
        )
        grouped: list[list[TradeEvent]] = []
        for trade in ordered:
            if (
                grouped
                and grouped[-1][0].asset_id == trade.asset_id
                and grouped[-1][0].timestamp == trade.timestamp
            ):
                grouped[-1].append(trade)
            else:
                grouped.append([trade])
        for group in grouped:
            for engine in self.engines.values():
                before = sum(len(order.fills) for order in engine.orders)
                engine.process_trades(group, reject_ambiguous_same_second=True)
                self._persist(engine)
                self.fill_count += sum(len(order.fills) for order in engine.orders) - before
        if ordered:
            self.last_trade_event = max(
                ordered,
                key=lambda row: (row.timestamp, row.sequence is None, row.sequence or 0),
            ).timestamp.isoformat()

    def status(self) -> dict[str, Any]:
        active = [order for engine in self.engines.values() for order in engine.active_orders]
        orders = [order for engine in self.engines.values() for order in engine.orders]
        fills = [fill for order in orders for fill in order.fills]
        return {
            "active_orders": len(active),
            "active_order_ids": [order.order_id for order in active],
            "inventory_shares": str(sum((engine.inventory_shares for engine in self.engines.values()), Decimal("0"))),
            "ledger_order_count": len(self.ledger.orders),
            "fill_count": len(fills),
            "rebuild_count": self.rebuild_count,
            "last_market_event": self.last_market_event,
            "last_trade_event": self.last_trade_event,
            "last_upstream_status": self.last_upstream_status,
            "upstream_maintenance": self.upstream_maintenance,
        }


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
    dependency_scan = execution_dependency_scan()
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
        enriched["station_id"] = station or None
        enriched["market_day"] = target or None
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
    weather_observations = load_realtime_weather_observations(
        jsonl_archive_paths(root / "raw" / "weather_daemon")
    )
    replay_pairs, weather_join_reasons = align_weather_to_snapshots(
        replay_pairs, weather_observations
    )
    if config_path is None:
        strategy = default_shadow_strategy_config()
    else:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
        from poly_weather.shadow_orders import ShadowStrategyConfig

        strategy = ShadowStrategyConfig.from_mapping(payload)
    trades_dir = root / "public_trades"
    public_trade_rows = load_event_trade_tapes(trades_dir) if trades_dir.exists() else {}
    public_trade_values = [trade for rows in public_trade_rows.values() for trade in rows]
    ws_trade_result = load_market_ws_trades(
        jsonl_archive_paths(root / "raw" / "polymarket_clob_websocket"),
        quality_windows=load_quality_windows(
            root / "runtime" / "polymarket_quality_windows.json"
        ),
    )
    trade_events, trade_source_validation = build_shadow_trade_events(
        ws_trade_result.trades, public_trade_values
    )
    settled_slugs: list[str] = []
    settled_catalog = root / "settled_markets.json"
    if settled_catalog.exists():
        try:
            payload = json.loads(settled_catalog.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, Mapping):
            settled_slugs = [
                str(row["event_slug"])
                for row in payload.get("events") or ()
                if isinstance(row, Mapping) and row.get("event_slug")
            ]
    result = replay_shadow_spread(
        replay_pairs,
        trades=trade_events,
        event_metadata=metadata,
        config=strategy,
        settled_event_slugs=settled_slugs,
    )
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
    ws_summary = ws_trade_result.as_json()
    ws_summary.pop("trades", None)
    ws_summary["queue_trade_events"] = trade_source_validation["ws_shadow_trade_event_count"]
    status = {
        "schema_version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "execution_enabled": False,
        "execution_dependency_scan": dependency_scan,
        "supervised": True,
        "runtime_mode": "read_only_shadow_archive_pass",
        "strategy_version": strategy.version,
        "active_orders": len(active),
        "active_order_ids": [order.order_id for order in active],
        "inventory_shares": risk["inventory_shares"],
        "average_inventory_cost": risk["average_inventory_cost"],
        "inventory_cost_usd": risk["inventory_cost_usd"],
        "cumulative_buy_cost_usd": risk["cumulative_buy_cost_usd"],
        "active_reserved_usd": risk["active_reserved_usd"],
        "budget_used_usd": risk["budget_used_usd"],
        "available_budget_usd": risk["available_budget_usd"],
        "realized_pnl_usd": risk["realized_pnl_usd"],
        "unrealized_pnl_usd": None,
        "capital_minutes": None,
        "fill_count": len(fills),
        "ledger_order_count": len(ledger.orders),
        "persisted_this_pass": persisted,
        "restart_idempotent": True,
        "recent_rejection_reasons": result["models"]["queue_aware"].get(
            "rejection_reasons", {}
        ),
        "recent_cancel_reasons": {
            str(order.get("cancel_reason")): sum(
                1 for row in queue_orders if row.get("cancel_reason") == order.get("cancel_reason")
            )
            for order in queue_orders
            if order.get("cancel_reason")
        },
        "raw_snapshot_count": result["raw_snapshot_count"],
        "weather_join": {
            "observation_count": len(weather_observations),
            "reason_counts": weather_join_reasons,
            "strict_cutoff": "source_timestamp <= snapshot_at and received_at <= snapshot_at",
            "historical_backfill_used": False,
        },
        "trade_sources": {
            "market_ws": ws_summary,
            "data_api": {
                "trade_count": len(public_trade_values),
                "queue_trade_events": trade_source_validation["data_api_shadow_trade_event_count"],
            },
            "side_validation": trade_source_validation,
        },
        "independent_market_day_count": result["independent_market_day_count"],
        "settled_event_count": result["settled_event_count"],
        "settled_depth_overlap_count": result["settled_depth_overlap_count"],
        "settled_result_status": result["settled_result_status"],
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


def run_shadow_spread_continuous(
    *,
    data_dir: Path | str = Path("data"),
    ledger_path: Path | str = Path("data/raw/shadow_orders/shadow_orders.jsonl"),
    status_path: Path | str = Path("data/runtime/shadow_spread_status.json"),
    cursor_path: Path | str = Path("data/runtime/shadow_spread_cursor.json"),
    config_path: Path | str | None = None,
    supervised: bool = True,
    poll_seconds: float = 5.0,
    runtime_seconds: float = 0.0,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Follow local market/weather archives as a continuous read-only daemon.

    The implementation consumes append-only files rather than opening a
    websocket or an execution client.  A cursor is committed only after the
    corresponding ledger/status update, so a crash can replay the last batch
    idempotently.
    """
    if supervised is not True:
        raise ValueError("shadow runtime requires supervised=true")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if runtime_seconds < 0:
        raise ValueError("runtime_seconds cannot be negative")
    dependency_scan = execution_dependency_scan()
    root = Path(data_dir)
    strategy = (
        default_shadow_strategy_config()
        if config_path is None
        else ShadowStrategyConfig.from_mapping(json.loads(Path(config_path).read_text(encoding="utf-8")))
    )
    strategy.validate()
    ledger = ShadowLedger(ledger_path)
    cursor = ShadowCursor.load(cursor_path)
    registry_path = root / ".." / "configs" / "settlements.json"
    if not registry_path.exists():
        registry_path = Path("configs/settlements.json")
    metadata: dict[str, dict[str, Any]] = {}
    if registry_path.exists():
        registry = load_settlement_registry(registry_path)
        # Metadata is extended as new event slugs appear below.
        metadata = archived_event_metadata((), registry.specs)
    processor = ShadowStreamProcessor(ledger=ledger, strategy=strategy, metadata=metadata)
    quality_windows = load_quality_windows(root / "runtime" / "polymarket_quality_windows.json")
    policy_path = Path("configs/warming_window_no_thresholds.json")
    policy_payload = (
        json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}
    )
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for encoded_key, value in cursor.pair_latest.items():
        base, separator, outcome = encoded_key.partition("\x00")
        if not separator or not isinstance(value, Mapping):
            continue
        restored = dict(value)
        if restored.get("_timestamp"):
            try:
                restored["_timestamp"] = datetime.fromisoformat(
                    str(restored["_timestamp"])
                ).astimezone(UTC)
            except (TypeError, ValueError):
                continue
        latest[(base, outcome)] = restored
    last_emitted: dict[str, datetime] = {}
    for base, value in cursor.pair_last_emitted.items():
        try:
            last_emitted[base] = datetime.fromisoformat(value).astimezone(UTC)
        except (TypeError, ValueError):
            continue
    weather_paths = jsonl_archive_paths(root / "raw" / "weather_daemon")
    observations: list[Any] = list(load_realtime_weather_observations(weather_paths))
    weather_join_state: dict[str, Any] = {}
    # Rebuild the previous-observation state from the durable weather archive
    # before the first live batch so a restart does not flag an old observation
    # as a new weather event.
    for observation in observations:
        key = (observation.station_id, observation.product)
        prior = weather_join_state.setdefault("previous_observation", {})
        prior[key] = observation.observation_id
        weather_join_state.setdefault("previous_temperature", {})[key] = observation.temperature_f
    all_public_trades: list[Any] = []
    public_trade_mtimes: dict[Path, int] = {}
    if (root / "public_trades").exists():
        existing_tapes = load_event_trade_tapes(root / "public_trades")
        all_public_trades = [trade for rows in existing_tapes.values() for trade in rows]
        # Existing history is available for WS side validation, but should not
        # be replayed into a fresh live shadow process.  Only a later mtime
        # (an incremental collector update) becomes a new API queue feed.
        for public_path in root.joinpath("public_trades").glob("*.json"):
            try:
                public_trade_mtimes[public_path] = public_path.stat().st_mtime_ns
            except OSError:
                continue
    started = time.monotonic()
    cycles = 0
    last_error: str | None = None
    current_generation: str | None = None
    last_weather_event: str | None = None
    last_signal_event: str | None = None
    while True:
        cycles += 1
        checkpoint_rows: list[dict[str, Any]] = []
        weather_rows: list[dict[str, Any]] = []
        ws_rows: list[dict[str, Any]] = []
        signal_rows: list[dict[str, Any]] = []
        matched_trade_assets: set[str] = set()
        unmatched_ws_trade_count = 0
        api_trade_events: list[TradeEvent] = []
        accepted_ws_trade_identities: set[tuple[str, str]] = set()
        try:
            # The market supervisor may append a newly observed maintenance or
            # recovery interval while this follower is already running.  Read
            # the small quality-window file each cycle so active orders are
            # cancelled on the next paired degraded book rather than waiting
            # for a daemon restart.
            quality_windows = load_quality_windows(
                root / "runtime" / "polymarket_quality_windows.json"
            )
            public_trade_dir = root / "public_trades"
            if public_trade_dir.exists():
                changed_public = False
                for public_path in public_trade_dir.glob("*.json"):
                    try:
                        mtime = public_path.stat().st_mtime_ns
                    except OSError:
                        continue
                    if public_trade_mtimes.get(public_path) != mtime:
                        public_trade_mtimes[public_path] = mtime
                        changed_public = True
                        api_trade_events.extend(
                            _public_trade_events_from_file(
                                public_path, quality_windows=quality_windows
                            )
                        )
                if changed_public:
                    tapes = load_event_trade_tapes(public_trade_dir)
                    all_public_trades = [trade for rows in tapes.values() for trade in rows]
            checkpoint_paths = jsonl_archive_paths(root / "raw" / "polymarket_book_checkpoints")
            for path in checkpoint_paths:
                checkpoint_rows.extend(_incremental_jsonl_rows(path, cursor.position(path)))
            weather_paths = jsonl_archive_paths(root / "raw" / "weather_daemon")
            for path in weather_paths:
                weather_rows.extend(_incremental_jsonl_rows(path, cursor.position(path)))
            ws_paths = jsonl_archive_paths(root / "raw" / "polymarket_clob_websocket")
            for path in ws_paths:
                ws_rows.extend(_incremental_jsonl_rows(path, cursor.position(path)))
            signal_paths = jsonl_archive_paths(root / "raw" / "signal_snapshot")
            for path in signal_paths:
                signal_rows.extend(_incremental_jsonl_rows(path, cursor.position(path)))
            observations.extend(
                observation
                for row in weather_rows
                for observation in _parse_observation_safely(row)
            )
            if weather_rows:
                last_weather_event = max(
                    (
                        str(row.get("received_at") or row.get("source_timestamp_ms") or "")
                        for row in weather_rows
                    ),
                    default=last_weather_event,
                )
            if signal_rows:
                last_signal_event = max(
                    (
                        str(row.get("generated_at") or row.get("received_at") or "")
                        for row in signal_rows
                    ),
                    default=last_signal_event,
                )
            if checkpoint_rows:
                slugs = {
                    str(row.get("market_slug") or "").rpartition(":")[0].split("/", 1)[0]
                    for row in checkpoint_rows
                    if row.get("market_slug")
                }
                if registry_path.exists():
                    metadata.update(archived_event_metadata(sorted(slugs), registry.specs))
                pairs = _archive_pair_rows(
                    checkpoint_rows,
                    latest,
                    last_emitted,
                    quality_windows=quality_windows,
                )
                enriched_pairs: list[dict[str, Any]] = []
                for pair in pairs:
                    event_value = metadata.get(pair["event_slug"], {})
                    enriched = dict(pair)
                    enriched["station_id"] = event_value.get("station_id")
                    enriched["market_day"] = event_value.get("target_date")
                    station = str(enriched.get("station_id") or "")
                    target = str(enriched.get("market_day") or "")
                    for season in (
                        (policy_payload.get("stations") or {}).get(station, {}).get("seasons", ())
                    ):
                        if str(season.get("window_start") or "") <= target <= str(
                            season.get("window_end") or ""
                        ):
                            enriched["season_version"] = str(
                                season.get("threshold_version")
                                or policy_payload.get("policy_version")
                                or ""
                            )
                            enriched["in_season"] = True
                            break
                    enriched_pairs.append(enriched)
                aligned, _join_reasons = align_weather_to_snapshots(
                    enriched_pairs, observations, state=weather_join_state
                )
                for pair in aligned:
                    try:
                        processor.process_snapshot(_row_snapshot(pair, event_metadata=metadata))
                    except (TypeError, ValueError, KeyError) as exc:
                        last_error = f"snapshot:{type(exc).__name__}: {exc}"
            # WS rows can safely consume the queue only after a matching
            # canonical taker row establishes side semantics.  A local stream
            # without that match is retained in coverage but fail-closed here.
            cycle_trade_events: list[TradeEvent] = []
            for row in ws_rows:
                try:
                    parsed = parse_market_ws_trade(row)
                except (TypeError, ValueError):
                    continue
                matching = [
                    trade
                    for trade in all_public_trades
                    if trade.transaction_hash
                    and trade.transaction_hash == parsed.transaction_hash
                ]
                if not matching:
                    unmatched_ws_trade_count += 1
                    continue
                matched_trade_assets.add(parsed.asset_id)
                events, validation = build_shadow_trade_events([parsed], matching)
                if validation["queue_use_allowed"]:
                    cycle_trade_events.extend(events)
                    if parsed.transaction_hash:
                        accepted_ws_trade_identities.add(
                            (parsed.transaction_hash, parsed.asset_id)
                        )
            # Data API is a supplementary receipt-gated source.  A matching WS
            # hash has already been preferred above; the ledger's trade event
            # key keeps incremental file rewrites idempotent.
            for event in api_trade_events:
                if event.event_id and (event.event_id, event.asset_id) in accepted_ws_trade_identities:
                    continue
                cycle_trade_events.append(event)
            processor.process_trades(cycle_trade_events)
            supervisor_status = root / "runtime" / "market_supervisor_status.json"
            if supervisor_status.exists():
                try:
                    generation = str(json.loads(supervisor_status.read_text(encoding="utf-8")).get("generation") or "")
                except (OSError, json.JSONDecodeError):
                    generation = None
                if generation and generation != current_generation:
                    cursor.generation = generation
                    current_generation = generation
                    processor.rebuild_count += 1
        except (OSError, ValueError, TypeError, KeyError) as exc:
            last_error = f"cycle:{type(exc).__name__}: {exc}"
        runtime_status = {
            "schema_version": 2,
            "checked_at": datetime.now(UTC).isoformat(),
            "heartbeat": datetime.now(UTC).isoformat(),
            "execution_enabled": False,
            "execution_dependency_scan": dependency_scan,
            "supervised": True,
            "runtime_mode": "read_only_shadow_continuous",
            "strategy_version": strategy.version,
            "cursor_path": str(Path(cursor_path).resolve()),
            "cursor_sources": len(cursor.sources),
            "cursor": {
                "generation": cursor.generation,
                "restart_count": cursor.restart_count,
                "sources": cursor.sources,
            },
            "cycle_count": cycles,
            "last_error": last_error,
            "last_weather_event": last_weather_event,
            "last_signal_event": last_signal_event,
            "data_coverage": {
                "new_market_rows": len(checkpoint_rows),
                "new_weather_rows": len(weather_rows),
                "new_ws_trade_rows": len(ws_rows),
                "new_data_api_trade_rows": len(api_trade_events),
                "new_signal_rows": len(signal_rows),
                "matched_trade_token_count": len(matched_trade_assets),
                "unmatched_ws_trade_count": unmatched_ws_trade_count,
            },
            **processor.status(),
            "limitations": [
                "read-only local archive follower; no order or user websocket endpoint",
                "public trade prices are not quotes; unmatched WS side semantics are excluded",
                "cursor commits after ledger/status work and is safe to replay",
            ],
        }
        destination = Path(status_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(json.dumps(runtime_status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)
        # Commit the cursor only after the ledger state and status heartbeat
        # have been durably written for this batch.
        cursor.pair_latest = {
            f"{base}\x00{outcome}": {
                **row,
                "_timestamp": (
                    row["_timestamp"].isoformat()
                    if isinstance(row.get("_timestamp"), datetime)
                    else row.get("_timestamp")
                ),
            }
            for (base, outcome), row in latest.items()
        }
        cursor.pair_last_emitted = {
            base: timestamp.isoformat() for base, timestamp in last_emitted.items()
        }
        cursor.save()
        if max_cycles is not None and cycles >= max_cycles:
            return runtime_status
        if runtime_seconds > 0 and time.monotonic() - started >= runtime_seconds:
            return runtime_status
        time.sleep(poll_seconds)


def _parse_observation_safely(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Local import boundary keeps malformed weather rows fail-closed."""
    from poly_weather.weather_market_join import parse_weather_observation

    try:
        return (parse_weather_observation(row),)
    except (TypeError, ValueError, KeyError):
        return ()


def _public_trade_events_from_file(
    path: Path,
    *,
    quality_windows: Sequence[Any] = (),
) -> tuple[TradeEvent, ...]:
    """Convert a newly-written public tape into receipt-gated queue events.

    The tape's ``fetched_at`` is the earliest local availability bound.  It is
    deliberately not treated as a quote and is used only to prevent a later
    API refresh from backfilling an order before the data was observed.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, Mapping) or not payload.get("event_slug"):
        return ()
    fetched_at: datetime | None = None
    if payload.get("fetched_at"):
        try:
            fetched_at = datetime.fromisoformat(str(payload["fetched_at"]).replace("Z", "+00:00"))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
            fetched_at = fetched_at.astimezone(UTC)
        except (TypeError, ValueError):
            fetched_at = None
    events: list[TradeEvent] = []
    for row in payload.get("trades") or ():
        if not isinstance(row, Mapping):
            continue
        try:
            timestamp_value = row.get("timestamp")
            if isinstance(timestamp_value, (int, float)):
                epoch = float(timestamp_value)
                if epoch > 10**11:
                    epoch /= 1000
                timestamp = datetime.fromtimestamp(epoch, tz=UTC)
            else:
                timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                timestamp = timestamp.astimezone(UTC)
            if not market_record_is_analysis_eligible(
                {"upstream_status": "normal"}, timestamp, tuple(quality_windows)
            ):
                continue
            transaction_hash = str(
                row.get("transaction_hash") or row.get("transactionHash") or ""
            ).strip()
            if not transaction_hash:
                # A refreshed API row without a stable identity cannot be
                # replayed idempotently; keep it out of the queue model.
                continue
            events.append(
                TradeEvent(
                    timestamp=timestamp,
                    available_at=fetched_at,
                    asset_id=str(row.get("asset_id") or row.get("asset") or ""),
                    side=str(row.get("side") or ""),
                    price=Decimal(str(row.get("price"))),
                    size=Decimal(str(row.get("size"))),
                    event_id=transaction_hash,
                    source="data_api",
                )
            )
        except (ArithmeticError, TypeError, ValueError, KeyError):
            continue
    return tuple(events)
