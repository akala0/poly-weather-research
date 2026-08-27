"""Incremental collection of the public Data API trade tape.

The depth archive is the source of truth for which events and condition/token
identities are in scope.  This module deliberately keeps collection metadata
next to the tape: an empty result after a successful request is different from
an event that was never requested.  Public executions are evidence for queue
consumption only; they are not historical bid/ask quotes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from poly_weather.adapters.polymarket_data import PublicTrade
from poly_weather.archive_io import open_jsonl_text


class TradeClient(Protocol):
    def market_trades(
        self,
        *,
        market_ids: Sequence[str],
        start: datetime,
        end: datetime,
        taker_only: bool = True,
    ) -> list[PublicTrade]: ...


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_from_market_slug(value: Any) -> tuple[str, str, str] | None:
    """Return event, condition-market slug and outcome from an archive identity."""
    text = str(value or "")
    base, separator, outcome = text.rpartition(":")
    if not separator or outcome.casefold() not in {"yes", "no"}:
        return None
    event_slug = base.split("/", 1)[0]
    if not event_slug:
        return None
    return event_slug, base, outcome.casefold()


@dataclass(frozen=True, slots=True)
class DepthEventCoverage:
    event_slug: str
    condition_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    start_at: datetime
    end_at: datetime
    snapshot_count: int
    assets_by_condition: Mapping[str, tuple[str, ...]]
    market_slugs: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_at"] = self.start_at.isoformat()
        payload["end_at"] = self.end_at.isoformat()
        payload["condition_ids"] = list(self.condition_ids)
        payload["asset_ids"] = list(self.asset_ids)
        payload["market_slugs"] = list(self.market_slugs)
        payload["assets_by_condition"] = {
            str(key): list(value) for key, value in self.assets_by_condition.items()
        }
        return payload


def discover_depth_event_coverage(
    checkpoint_paths: Iterable[Path],
) -> tuple[DepthEventCoverage, ...]:
    """Discover event/condition/token coverage from plain or gzip JSONL.

    Only archive receipt time is used for the collection window.  A malformed
    row is ignored (fail closed) rather than inventing an event or timestamp.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for path in checkpoint_paths:
        if not path.exists():
            continue
        try:
            handle = open_jsonl_text(path)
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    identity = _event_from_market_slug(row.get("market_slug"))
                    received = _utc(str(row["received_at"]))
                    condition_id = str(row.get("market_id") or "").strip()
                    asset_id = str(row.get("asset_id") or "").strip()
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if identity is None or not condition_id or not asset_id:
                    continue
                event_slug, market_slug, _outcome = identity
                value = grouped.setdefault(
                    event_slug,
                    {
                        "conditions": set(),
                        "assets": set(),
                        "assets_by_condition": {},
                        "market_slugs": set(),
                        "start": received,
                        "end": received,
                        "count": 0,
                    },
                )
                value["conditions"].add(condition_id)
                value["assets"].add(asset_id)
                value["assets_by_condition"].setdefault(condition_id, set()).add(asset_id)
                value["market_slugs"].add(market_slug)
                value["start"] = min(value["start"], received)
                value["end"] = max(value["end"], received)
                value["count"] += 1
    output: list[DepthEventCoverage] = []
    for event_slug, value in sorted(grouped.items()):
        output.append(
            DepthEventCoverage(
                event_slug=event_slug,
                condition_ids=tuple(sorted(value["conditions"])),
                asset_ids=tuple(sorted(value["assets"])),
                start_at=value["start"],
                end_at=value["end"],
                snapshot_count=int(value["count"]),
                assets_by_condition={
                    key: tuple(sorted(assets))
                    for key, assets in sorted(value["assets_by_condition"].items())
                },
                market_slugs=tuple(sorted(value["market_slugs"])),
            )
        )
    return tuple(output)


def public_trade_key(row: PublicTrade | Mapping[str, Any]) -> tuple[Any, ...]:
    """Strongest available identity for a canonical taker execution."""
    if isinstance(row, PublicTrade):
        return (
            row.transaction_hash,
            row.proxy_wallet,
            row.asset_id,
            row.condition_id,
            row.timestamp,
            row.side,
            row.size,
            row.price,
        )
    timestamp = row.get("timestamp")
    try:
        timestamp_value: Any = _utc(str(timestamp))
    except (TypeError, ValueError):
        timestamp_value = str(timestamp or "")
    # Decimal normalisation makes a JSON string such as ``"3.0"`` compare
    # equal to the PublicTrade value ``Decimal("3")``.
    def decimal_value(value: Any) -> Any:
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError, TypeError):
            return str(value or "")

    return (
        str(row.get("transaction_hash") or row.get("transactionHash") or ""),
        str(row.get("proxy_wallet") or row.get("proxyWallet") or ""),
        str(row.get("asset_id") or row.get("asset") or ""),
        str(row.get("condition_id") or row.get("conditionId") or ""),
        timestamp_value,
        str(row.get("side") or "").upper(),
        decimal_value(row.get("size")),
        decimal_value(row.get("price")),
    )


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_trade_cursor(path: Path | str) -> dict[str, Any]:
    payload = _read_payload(Path(path))
    return payload if isinstance(payload.get("events", {}), dict) else {"schema_version": 1, "events": {}}


def _merge_trades(
    existing: Sequence[Mapping[str, Any]],
    fetched: Sequence[PublicTrade],
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in existing:
        if not isinstance(row, Mapping):
            continue
        merged.setdefault(public_trade_key(row), dict(row))
    for trade in fetched:
        merged.setdefault(public_trade_key(trade), trade.as_json())
    rows = list(merged.values())
    rows.sort(key=lambda row: (str(row.get("timestamp") or ""), public_trade_key(row)))
    return rows


def collect_depth_event_trades(
    coverages: Sequence[DepthEventCoverage],
    *,
    client: TradeClient,
    output_dir: Path | str,
    cursor_path: Path | str | None = None,
    overlap: timedelta = timedelta(seconds=2),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Incrementally fetch and merge tapes for every archived depth event.

    ``overlap`` catches a second-resolution boundary without re-fetching the
    complete history.  Exact identity de-duplication makes the overlap safe.
    """
    destination_root = Path(output_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    cursor_file = Path(cursor_path) if cursor_path else destination_root / ".depth_trade_cursor.json"
    cursor = load_trade_cursor(cursor_file)
    events_cursor = cursor.setdefault("events", {})
    fetched_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    summaries: list[dict[str, Any]] = []
    for coverage in coverages:
        event_slug = coverage.event_slug
        path = destination_root / f"{event_slug}.json"
        old_payload = _read_payload(path)
        old_rows = old_payload.get("trades") if isinstance(old_payload.get("trades"), list) else []
        state = events_cursor.setdefault(event_slug, {})
        previous_end = None
        if state.get("watermark_end"):
            try:
                previous_end = _utc(str(state["watermark_end"]))
            except ValueError:
                previous_end = None
        # A coverage window that has not advanced is already represented by
        # the local tape.  No request is made and the status remains explicit.
        if previous_end is not None and coverage.end_at <= previous_end and old_payload:
            fetched: list[PublicTrade] = []
            status = str(old_payload.get("collection_status") or state.get("status") or "not_collected")
            request_start = previous_end
            request_end = previous_end
        else:
            request_start = coverage.start_at if previous_end is None else max(
                coverage.start_at, previous_end - overlap
            )
            request_end = coverage.end_at + timedelta(seconds=1)
            fetched = client.market_trades(
                market_ids=coverage.condition_ids,
                start=request_start,
                end=request_end,
                taker_only=True,
            )
            status = "collected_nonzero" if fetched else "collected_zero"
        rows = _merge_trades(old_rows, fetched)
        trade_assets = {str(row.get("asset_id") or "") for row in rows if row.get("asset_id")}
        intersected = sorted(set(coverage.asset_ids) & trade_assets)
        unmatched = [
            {"asset_id": asset, "reason": "no_trade_for_snapshot_token"}
            for asset in coverage.asset_ids
            if asset not in trade_assets
        ]
        payload = {
            **old_payload,
            "schema_version": 2,
            "fetched_at": fetched_at,
            "source": "https://data-api.polymarket.com/trades",
            "event_slug": event_slug,
            "condition_ids": list(coverage.condition_ids),
            "snapshot_asset_ids": list(coverage.asset_ids),
            "snapshot_token_count": len(coverage.asset_ids),
            "depth_coverage": coverage.as_json(),
            "start": request_start.isoformat() if request_start <= request_end else None,
            "end": request_end.isoformat() if request_start <= request_end else None,
            "taker_only": True,
            "tape_semantics": (
                "canonical public taker executions; execution prices are not resting bid/ask quotes"
            ),
            "collection_status": status,
            "trade_count": len(rows),
            "fetched_trade_count": len(fetched),
            "trade_asset_intersection_count": len(intersected),
            "trade_asset_intersection": intersected,
            "unmatched_snapshot_assets": unmatched,
            "trades": rows,
            "fetched_ranges": [
                *(
                    old_payload.get("fetched_ranges")
                    if isinstance(old_payload.get("fetched_ranges"), list)
                    else []
                ),
                {
                    "start": request_start.isoformat(),
                    "end": request_end.isoformat(),
                    "trade_count": len(fetched),
                },
            ]
            if fetched
            else old_payload.get("fetched_ranges", []),
        }
        _atomic_json_write(path, payload)
        state.update(
            {
                "watermark_end": coverage.end_at.isoformat(),
                "watermark_snapshot_count": coverage.snapshot_count,
                "status": status,
                "last_fetched_at": fetched_at,
                "last_fetched_trade_count": len(fetched),
            }
        )
        summaries.append(
            {
                "event_slug": event_slug,
                "path": str(path.resolve()),
                "collection_status": status,
                "snapshot_count": coverage.snapshot_count,
                "snapshot_token_count": len(coverage.asset_ids),
                "trade_count": len(rows),
                "fetched_trade_count": len(fetched),
                "trade_asset_intersection_count": len(intersected),
                "unmatched_snapshot_asset_count": len(unmatched),
            }
        )
    cursor["schema_version"] = 1
    cursor["updated_at"] = fetched_at
    _atomic_json_write(cursor_file, cursor)
    audit = {
        "schema_version": 1,
        "generated_at": fetched_at,
        "depth_event_count": len(summaries),
        "collected_zero_event_count": sum(row["collection_status"] == "collected_zero" for row in summaries),
        "not_collected_event_count": sum(row["collection_status"] == "not_collected" for row in summaries),
        "trade_count": sum(row["trade_count"] for row in summaries),
        "trade_asset_intersection_count": sum(
            row["trade_asset_intersection_count"] for row in summaries
        ),
        "events": summaries,
        "cursor_path": str(cursor_file.resolve()),
        "execution_enabled": False,
    }
    _atomic_json_write(destination_root / "depth_trade_coverage.json", audit)
    return audit


__all__ = [
    "DepthEventCoverage",
    "collect_depth_event_trades",
    "discover_depth_event_coverage",
    "load_trade_cursor",
    "public_trade_key",
]
