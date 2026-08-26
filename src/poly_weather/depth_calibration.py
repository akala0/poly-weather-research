"""Replay archived order books and calibrate historical ``p`` price optimism."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.archive_io import jsonl_archive_paths, open_jsonl_text
from poly_weather.execution_cost import estimate_execution_cost
from poly_weather.polymarket_status import (
    load_quality_windows,
    market_record_is_analysis_eligible,
)


@dataclass(frozen=True)
class DepthSnapshot:
    observed_at: datetime
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]


def _snapshot(state: Mapping[str, Any]) -> DepthSnapshot | None:
    if not state.get("complete") or not state.get("bids") or not state.get("asks"):
        return None
    bids = state["bids"]
    asks = state["asks"]
    return DepthSnapshot(
        observed_at=state["observed_at"],
        bids=tuple((str(price), str(bids[price])) for price in sorted(bids, reverse=True)),
        asks=tuple((str(price), str(asks[price])) for price in sorted(asks)),
    )


def replay_books_at_or_before(
    data_dir: Path,
    requests: Mapping[str, Sequence[datetime]],
) -> dict[tuple[str, datetime], DepthSnapshot | None]:
    """Return exact local books using only archive records at or before each cutoff."""
    cutoffs = {
        asset_id: sorted({cutoff.astimezone(UTC) for cutoff in values})
        for asset_id, values in requests.items()
    }
    positions = {asset_id: 0 for asset_id in cutoffs}
    states: dict[str, dict[str, Any]] = {}
    output: dict[tuple[str, datetime], DepthSnapshot | None] = {}
    quality_windows = load_quality_windows(
        data_dir / "runtime" / "polymarket_quality_windows.json"
    )

    def capture_before(asset_id: str, timestamp: datetime) -> None:
        asset_cutoffs = cutoffs[asset_id]
        position = positions[asset_id]
        while position < len(asset_cutoffs) and asset_cutoffs[position] < timestamp:
            cutoff = asset_cutoffs[position]
            output[(asset_id, cutoff)] = _snapshot(states.get(asset_id, {}))
            position += 1
        positions[asset_id] = position

    archive_root = data_dir / "raw" / "polymarket_clob_websocket"
    for path in jsonl_archive_paths(archive_root):
        with open_jsonl_text(path) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    asset_id = str(row.get("asset_id") or "")
                    timestamp = datetime.fromisoformat(str(row["received_at"])).astimezone(UTC)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not market_record_is_analysis_eligible(row, timestamp, quality_windows):
                    continue
                if asset_id not in cutoffs:
                    continue
                capture_before(asset_id, timestamp)
                if positions[asset_id] >= len(cutoffs[asset_id]):
                    continue
                state = states.setdefault(
                    asset_id,
                    {"bids": {}, "asks": {}, "complete": False, "observed_at": timestamp},
                )
                bids = row.get("bids")
                asks = row.get("asks")
                if isinstance(bids, list) and isinstance(asks, list):
                    state["bids"] = {
                        Decimal(str(level["price"])): Decimal(str(level["size"]))
                        for level in bids
                        if Decimal(str(level["size"])) > 0
                    }
                    state["asks"] = {
                        Decimal(str(level["price"])): Decimal(str(level["size"]))
                        for level in asks
                        if Decimal(str(level["size"])) > 0
                    }
                    state["complete"] = True
                if row.get("event_type") == "price_change" and state["complete"]:
                    raw = row.get("raw")
                    changes = raw.get("price_changes") if isinstance(raw, dict) else None
                    if isinstance(changes, list):
                        for change in changes:
                            if not isinstance(change, dict):
                                continue
                            if str(change.get("asset_id") or "") != asset_id:
                                continue
                            side = (
                                "bids"
                                if str(change.get("side") or "").upper() == "BUY"
                                else "asks"
                            )
                            price = Decimal(str(change["price"]))
                            size = Decimal(str(change["size"]))
                            if size > 0:
                                state[side][price] = size
                            else:
                                state[side].pop(price, None)
                state["observed_at"] = timestamp

    for asset_id, asset_cutoffs in cutoffs.items():
        for cutoff in asset_cutoffs[positions[asset_id] :]:
            output[(asset_id, cutoff)] = _snapshot(states.get(asset_id, {}))
    return output


def _catalog_market_for_bucket(event: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    for market in event.get("markets") or []:
        lower = market.get("lower_f")
        upper = market.get("upper_f")
        expected = (
            f"≤{upper}°F"
            if lower is None
            else f"≥{lower}°F"
            if upper is None
            else f"{lower}-{upper}°F"
        )
        if expected == label:
            return market
    raise ValueError(f"no catalog market matches bucket {label!r}")


def build_depth_cost_calibration(
    analysis: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    data_dir: Path,
    sizes_usd: Sequence[Decimal] = (Decimal("50"), Decimal("200"), Decimal("1000")),
) -> dict[str, Any]:
    """Compare historical p-proxy P&L with executable archived depth estimates."""
    events = {
        (str(event["station_id"]), str(event["target_date"])): event
        for event in catalog.get("events") or []
    }
    prepared: list[dict[str, Any]] = []
    requests: dict[str, list[datetime]] = {}
    for trade in analysis.get("trades") or []:
        event = events.get((str(trade["station_id"]), str(trade["date"])))
        if event is None:
            continue
        market = _catalog_market_for_bucket(event, str(trade["bucket"]))
        timezone = ZoneInfo(str(event["timezone"]))
        entry = datetime.fromisoformat(f"{trade['date']}T{trade['time']}:00").replace(
            tzinfo=timezone
        ).astimezone(UTC)
        exit_value = str(trade["exit"])
        exit_cutoff = None if exit_value == "settlement" else datetime.fromisoformat(exit_value)
        asset_id = str(market["yes_token_id"])
        requests.setdefault(asset_id, []).append(entry)
        if exit_cutoff is not None:
            requests[asset_id].append(exit_cutoff)
        prepared.append(
            {
                "trade": trade,
                "event": event,
                "market": market,
                "asset_id": asset_id,
                "entry": entry,
                "exit": exit_cutoff,
            }
        )
    snapshots = replay_books_at_or_before(data_dir, requests)
    records: list[dict[str, Any]] = []
    for item in prepared:
        trade = item["trade"]
        entry_book = snapshots.get((item["asset_id"], item["entry"]))
        exit_book = (
            snapshots.get((item["asset_id"], item["exit"]))
            if item["exit"] is not None
            else None
        )
        for size_usd in sizes_usd:
            entry_fill = (
                estimate_execution_cost(entry_book.asks, size_usd, "buy")
                if entry_book is not None
                else None
            )
            exit_fill = (
                estimate_execution_cost(exit_book.bids, size_usd, "sell")
                if exit_book is not None
                else None
            )
            fully_executable = entry_fill is not None and entry_fill.filled_fraction >= 1.0
            if item["exit"] is not None:
                fully_executable = (
                    fully_executable
                    and exit_fill is not None
                    and exit_fill.filled_fraction >= 1.0
                )
            depth_pnl = None
            if fully_executable and entry_fill is not None:
                exit_price = (
                    exit_fill.average_fill_price - exit_fill.fee_per_share
                    if exit_fill is not None
                    else Decimal("1")
                    if trade["physical_bucket_won"]
                    else Decimal("0")
                )
                depth_pnl = (
                    exit_price
                    - entry_fill.average_fill_price
                    - entry_fill.fee_per_share
                )
            proxy_pnl = Decimal(str(trade["pnl_per_share_p_proxy"]))
            records.append(
                {
                    "date": trade["date"],
                    "station_id": trade["station_id"],
                    "time": trade["time"],
                    "bucket": trade["bucket"],
                    "size_usd": float(size_usd),
                    "fully_executable": fully_executable,
                    "entry_fill": float(entry_fill.average_fill_price) if entry_fill else None,
                    "entry_taker_fee_usdc": float(entry_fill.fee_usdc)
                    if entry_fill
                    else None,
                    "entry_filled_fraction": entry_fill.filled_fraction if entry_fill else 0.0,
                    "exit_fill": float(exit_fill.average_fill_price) if exit_fill else None,
                    "exit_taker_fee_usdc": float(exit_fill.fee_usdc) if exit_fill else None,
                    "exit_filled_fraction": exit_fill.filled_fraction if exit_fill else 0.0,
                    "p_proxy_pnl": float(proxy_pnl),
                    "depth_pnl": float(depth_pnl) if depth_pnl is not None else None,
                    "p_proxy_optimism": (
                        float(proxy_pnl - depth_pnl) if depth_pnl is not None else None
                    ),
                }
            )
    summaries = []
    for size_usd in sizes_usd:
        selected = [row for row in records if row["size_usd"] == float(size_usd)]
        executable = [row for row in selected if row["fully_executable"]]
        summaries.append(
            {
                "size_usd": float(size_usd),
                "candidate_trade_count": len(selected),
                "fully_executable_count": len(executable),
                "depth_insufficient_frequency": (
                    1 - len(executable) / len(selected) if selected else None
                ),
                "mean_p_proxy_pnl": (
                    fmean(row["p_proxy_pnl"] for row in executable) if executable else None
                ),
                "mean_depth_pnl": (
                    fmean(row["depth_pnl"] for row in executable) if executable else None
                ),
                "mean_p_proxy_optimism": (
                    fmean(row["p_proxy_optimism"] for row in executable)
                    if executable
                    else None
                ),
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "proxy_trade_count": len(analysis.get("trades") or []),
        "catalog_matched_trade_count": len(prepared),
        "summaries": summaries,
        "records": records,
        "execution_enabled": False,
    }


def render_depth_cost_calibration(result: Mapping[str, Any], output_path: Path) -> None:
    lines = [
        "# p 代理与真实执行成本校准",
        "",
        "按严格无前视订单簿重放估算入场吃 ask、退出吃 bid；不产生真实订单。",
        "",
        "| 仓位 | 候选交易 | 可完整执行 | 深度不足 | p代理平均P&L | 深度平均P&L | p代理乐观偏差 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["summaries"]:
        def value(
            name: str,
            *,
            percent: bool = False,
            current: Mapping[str, Any] = row,
        ) -> str:
            item = current[name]
            if item is None:
                return "N/A"
            return f"{item:.1%}" if percent else f"{item:.3f}"

        lines.append(
            f"| ${row['size_usd']:.0f} | {row['candidate_trade_count']} | "
            f"{row['fully_executable_count']} | "
            f"{value('depth_insufficient_frequency', percent=True)} | "
            f"{value('mean_p_proxy_pnl')} | {value('mean_depth_pnl')} | "
            f"{value('mean_p_proxy_optimism')} |"
        )
    if not any(row["fully_executable_count"] for row in result["summaries"]):
        lines.extend(
            [
                "",
                "当前 p 代理回测日期与完整深度归档日期没有可执行重叠样本，因此不输出伪造的成本修正。",
                "持续归档并在新市场结算后重跑本命令即可自动得到比较。",
            ]
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
