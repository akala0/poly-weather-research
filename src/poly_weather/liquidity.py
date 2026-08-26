"""Liquidity health summaries from archived full-depth market books."""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.execution_cost import estimate_fill_price
from poly_weather.polymarket_status import (
    load_quality_windows,
    market_record_is_analysis_eligible,
)

_EVENT_CITY = re.compile(r"^highest-temperature-in-(?P<city>.+)-on-[a-z]+-\d{1,2}-\d{4}$")

DEFAULT_LIQUIDITY_WINDOWS = {
    "los-angeles": ("KLAX", "America/Los_Angeles", time(13, 0), time(15, 0)),
    "nyc": ("KLGA", "America/New_York", time(16, 0), time(17, 0)),
    "chicago": ("KORD", "America/Chicago", time(14, 0), time(17, 0)),
    "miami": ("KMIA", "America/New_York", time(14, 0), time(17, 0)),
    "atlanta": ("KATL", "America/New_York", time(14, 0), time(17, 0)),
    "dallas": ("KDAL", "America/Chicago", time(14, 0), time(17, 0)),
    "houston": ("KHOU", "America/Chicago", time(14, 0), time(17, 0)),
    "seattle": ("KSEA", "America/Los_Angeles", time(13, 0), time(16, 0)),
}


def _book_levels(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []
    decoded = json.loads(value)
    if not isinstance(decoded, list):
        return []
    return [
        (str(row["price"]), str(row["size"]))
        for row in decoded
        if isinstance(row, dict) and "price" in row and "size" in row
    ]


def liquidity_health_rows(
    rows: Iterable[tuple[Any, ...]],
) -> list[dict[str, Any]]:
    """Aggregate minute-sampled full books inside configured local windows."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for received_at, market_path, best_bid, best_ask, _bids_json, asks_json in rows:
        if not market_path or "/" not in str(market_path):
            continue
        event_slug, market_outcome = str(market_path).split("/", 1)
        match = _EVENT_CITY.fullmatch(event_slug)
        if match is None or match.group("city") not in DEFAULT_LIQUIDITY_WINDOWS:
            continue
        station_id, timezone, window_start, window_end = DEFAULT_LIQUIDITY_WINDOWS[
            match.group("city")
        ]
        timestamp = received_at
        if not isinstance(timestamp, datetime):
            timestamp = datetime.fromisoformat(str(received_at))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        local_time = timestamp.astimezone(ZoneInfo(timezone)).time().replace(tzinfo=None)
        if not window_start <= local_time <= window_end:
            continue
        if best_bid is None or best_ask is None:
            continue
        bid = Decimal(str(best_bid))
        ask = Decimal(str(best_ask))
        if ask < bid:
            continue
        asks = _book_levels(asks_json)
        estimates = {
            size: estimate_fill_price(asks, Decimal(size), "buy") for size in ("200", "1000")
        }
        market_slug, outcome = market_outcome.rsplit(":", 1)
        bucket = market_slug.removeprefix(f"{event_slug}-") + f":{outcome}"
        grouped[(station_id, f"{window_start:%H:%M}-{window_end:%H:%M}", bucket)].append(
            {
                "spread": float(ask - bid),
                "estimates": estimates,
            }
        )

    output = []
    for (station_id, window, bucket), samples in sorted(grouped.items()):
        row: dict[str, Any] = {
            "station_id": station_id,
            "window": window,
            "bucket": bucket,
            "sample_count": len(samples),
            "average_spread": statistics.fmean(sample["spread"] for sample in samples),
        }
        for size in ("200", "1000"):
            estimates = [
                sample["estimates"][size]
                for sample in samples
                if sample["estimates"][size] is not None
            ]
            row[f"slippage_{size}_usd"] = (
                statistics.fmean(float(estimate[1]) for estimate in estimates)
                if estimates
                else None
            )
            row[f"insufficient_{size}_frequency"] = statistics.fmean(
                sample["estimates"][size] is None or sample["estimates"][size][2] < 1.0
                for sample in samples
            )
        output.append(row)
    return output


def archived_liquidity_rows(
    connection: Any,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    quality_windows: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    clauses = ["event_type IN ('book', 'price_change')"]
    parameters: list[Any] = []
    if start is not None:
        clauses.append("received_at >= ?")
        parameters.append(start.astimezone(UTC))
    if end is not None:
        clauses.append("received_at < ?")
        parameters.append(end.astimezone(UTC))
    query = f"""
        SELECT received_at, market_slug, asset_id, event_type, raw_json,
               bids_json, asks_json, COALESCE(upstream_status, 'normal')
        FROM market_stream_events
        WHERE {" AND ".join(clauses)}
        ORDER BY received_at, run_id, sequence
    """
    eligible = []
    for row in connection.execute(query, parameters).fetchall():
        timestamp = row[0]
        if not isinstance(timestamp, datetime):
            timestamp = datetime.fromisoformat(str(timestamp))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if market_record_is_analysis_eligible(
            {"upstream_status": row[7]}, timestamp.astimezone(UTC), quality_windows
        ):
            eligible.append(row[:7])
    return _replay_liquidity_events(eligible)


def _decoded_levels(value: Any) -> dict[Decimal, Decimal] | None:
    if value is None:
        return None
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        return None
    return {
        Decimal(str(row["price"])): Decimal(str(row["size"]))
        for row in decoded
        if isinstance(row, dict)
        and "price" in row
        and "size" in row
        and Decimal(str(row["size"])) > 0
    }


def _replay_liquidity_events(rows: Iterable[tuple[Any, ...]]) -> list[dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    latest: dict[tuple[str, datetime], tuple[Any, ...]] = {}
    for received_at, market_slug, asset_id, event_type, raw_json, bids, asks in rows:
        if not asset_id:
            continue
        timestamp = received_at
        if not isinstance(timestamp, datetime):
            timestamp = datetime.fromisoformat(str(timestamp))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        timestamp = timestamp.astimezone(UTC)
        book = books.setdefault(str(asset_id), {"bids": {}, "asks": {}, "complete": False})
        decoded_bids = _decoded_levels(bids)
        decoded_asks = _decoded_levels(asks)
        if decoded_bids is not None and decoded_asks is not None:
            book["bids"] = decoded_bids
            book["asks"] = decoded_asks
            book["complete"] = True
        raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        if event_type == "price_change" and isinstance(raw, dict):
            changes = raw.get("price_changes")
            if isinstance(changes, list) and book["complete"]:
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    if str(change.get("asset_id") or "") != str(asset_id):
                        continue
                    side = "bids" if str(change.get("side") or "").upper() == "BUY" else "asks"
                    price = Decimal(str(change["price"]))
                    size = Decimal(str(change["size"]))
                    if size > 0:
                        book[side][price] = size
                    else:
                        book[side].pop(price, None)
        if not book["complete"] or not market_slug:
            continue
        bid_levels = book["bids"]
        ask_levels = book["asks"]
        if not bid_levels or not ask_levels:
            continue
        minute = timestamp.replace(second=0, microsecond=0)
        latest[(str(asset_id), minute)] = (
            timestamp,
            market_slug,
            str(max(bid_levels)),
            str(min(ask_levels)),
            json.dumps(
                [
                    {"price": str(price), "size": str(bid_levels[price])}
                    for price in sorted(bid_levels, reverse=True)
                ],
                separators=(",", ":"),
            ),
            json.dumps(
                [
                    {"price": str(price), "size": str(ask_levels[price])}
                    for price in sorted(ask_levels)
                ],
                separators=(",", ":"),
            ),
        )
    return liquidity_health_rows(latest.values())


def archived_liquidity_rows_from_jsonl(
    data_dir: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read minute-sampled depth directly while the DuckDB writer is running.

    DuckDB intentionally permits only one writer process.  The append-only JSONL
    archive is therefore the live-safe report source and also acts as the durable
    replay source if the database needs to be rebuilt.
    """
    start_utc = start.astimezone(UTC) if start is not None else None
    end_utc = end.astimezone(UTC) if end is not None else None
    events: list[tuple[Any, ...]] = []
    quality_windows = load_quality_windows(
        data_dir / "runtime" / "polymarket_quality_windows.json"
    )
    checkpoint_root = data_dir / "raw" / "polymarket_book_checkpoints"
    full_archive_root = data_dir / "raw" / "polymarket_clob_websocket"
    archive_root = (
        checkpoint_root if any(checkpoint_root.glob("*/events.jsonl")) else full_archive_root
    )
    for path in sorted(archive_root.glob("*/events.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    received_at = datetime.fromisoformat(str(record["received_at"]))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=UTC)
                received_at = received_at.astimezone(UTC)
                if not market_record_is_analysis_eligible(
                    record, received_at, quality_windows
                ):
                    continue
                if end_utc is not None and received_at >= end_utc:
                    continue
                if record.get("event_type") not in {"book", "price_change"}:
                    continue
                if not record.get("asset_id"):
                    continue
                if start_utc is not None and received_at < start_utc:
                    # Keep earlier same-day snapshots so later deltas can be
                    # replayed, but do not emit them as report samples.
                    market_slug = None
                else:
                    market_slug = record.get("market_slug")
                events.append(
                    (
                        received_at,
                        market_slug,
                        record.get("asset_id"),
                        record.get("event_type"),
                        record.get("raw"),
                        record.get("bids"),
                        record.get("asks"),
                    )
                )
    return _replay_liquidity_events(events)


def render_liquidity_report(rows: list[dict[str, Any]], *, output_path: Path) -> None:
    lines = [
        "# 流动性健康报告",
        "",
        "完整订单簿按 asset/minute 取每分钟最后一帧，避免高频更新资产被重复加权。",
        "所有结果仅为只读成交成本估算，`execution_enabled=False`。",
        "",
        "| 站点 | 时段 | 桶/方向 | 样本 | 平均 spread | $200 滑点 | $1000 滑点 | $200 深度不足 | $1000 深度不足 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        slippage_200 = row["slippage_200_usd"]
        slippage_1000 = row["slippage_1000_usd"]
        lines.append(
            "| {station_id} | {window} | {bucket} | {sample_count} | {spread:.3f} | "
            "{slip200} | {slip1000} | {insufficient200:.1%} | {insufficient1000:.1%} |".format(
                **row,
                spread=row["average_spread"],
                slip200=(f"{slippage_200:.3f}" if slippage_200 is not None else "N/A"),
                slip1000=(f"{slippage_1000:.3f}" if slippage_1000 is not None else "N/A"),
                insufficient200=row["insufficient_200_frequency"],
                insufficient1000=row["insufficient_1000_frequency"],
            )
        )
    if not rows:
        lines.extend(["", "当前归档中还没有带完整深度的候选窗口快照。"])
    expensive = [
        row
        for row in rows
        if row["slippage_200_usd"] is not None and row["slippage_200_usd"] > 0.05
    ]
    lines.extend(
        [
            "",
            "## 健康判断",
            "",
            f"$200 平均滑点超过 5¢ 的分组数：{len(expensive)}。",
            "该阈值用于提示策略经济性需要重算，不会触发任何订单。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
