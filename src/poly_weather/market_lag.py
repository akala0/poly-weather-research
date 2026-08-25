"""No-lookahead comparison of observed temperature certainty and CLOB price history."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from poly_weather.domain import Market, MarketPricePoint
from poly_weather.intraday_reversal import (
    TemperatureObservation,
    certainty_curve_points,
)
from poly_weather.modeling import (
    market_temperature_bucket,
    two_degree_bucket_lower,
)

_EVENT_SLUG = re.compile(
    r"^highest-temperature-in-(?P<city>nyc|los-angeles)-on-"
    r"(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$"
)
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


@dataclass(frozen=True)
class ProxyTradeResult:
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    exited_before_settlement: bool


def event_identity(event_slug: str) -> tuple[str, str, date]:
    """Return station, IANA timezone, and local target date for an exact event slug."""
    match = _EVENT_SLUG.fullmatch(event_slug)
    if match is None:
        raise ValueError(f"unsupported daily-high event slug: {event_slug}")
    city = match.group("city")
    station_id, timezone = (
        ("KLGA", "America/New_York")
        if city == "nyc"
        else ("KLAX", "America/Los_Angeles")
    )
    target_date = date(
        int(match.group("year")),
        _MONTHS[match.group("month")],
        int(match.group("day")),
    )
    return station_id, timezone, target_date


def yes_token_id(market: Market) -> str:
    try:
        yes_index = next(
            index for index, outcome in enumerate(market.outcomes) if outcome.casefold() == "yes"
        )
    except StopIteration as exc:
        raise ValueError(f"market {market.slug} has no Yes outcome") from exc
    if yes_index >= len(market.clob_token_ids):
        raise ValueError(f"market {market.slug} has no aligned Yes token")
    return market.clob_token_ids[yes_index]


def resolved_yes_market(markets: Sequence[Market]) -> Market:
    """Identify the unique binary bucket whose settled Yes outcome equals one."""
    winners: list[Market] = []
    for market in markets:
        yes_prices = [
            market.outcome_prices[index]
            for index, outcome in enumerate(market.outcomes)
            if outcome.casefold() == "yes" and index < len(market.outcome_prices)
        ]
        if yes_prices == [Decimal("1")]:
            winners.append(market)
    if len(winners) != 1:
        raise ValueError(f"expected exactly one resolved Yes bucket, found {len(winners)}")
    return winners[0]


def latest_price_at_or_before(
    points: Sequence[MarketPricePoint],
    cutoff: datetime,
) -> MarketPricePoint | None:
    """Select the newest historical price whose UTC timestamp is not after cutoff."""
    cutoff_utc = cutoff.astimezone(UTC)
    eligible = [point for point in points if point.timestamp.astimezone(UTC) <= cutoff_utc]
    return max(eligible, key=lambda point: point.timestamp) if eligible else None


def proxy_trade_pnl(
    *,
    entry_price: Decimal,
    exit_price: Decimal | None = None,
    resolved_yes: bool | None = None,
) -> ProxyTradeResult:
    """Calculate one-share P&L using a historical p point as a non-executable proxy."""
    if not Decimal("0") <= entry_price <= Decimal("1"):
        raise ValueError("entry price must be between zero and one")
    if exit_price is not None:
        if resolved_yes is not None:
            raise ValueError("provide either exit price or resolution, not both")
        if not Decimal("0") <= exit_price <= Decimal("1"):
            raise ValueError("exit price must be between zero and one")
        return ProxyTradeResult(
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=exit_price - entry_price,
            exited_before_settlement=True,
        )
    if resolved_yes is None:
        raise ValueError("resolution is required when no exit price is provided")
    settlement_price = Decimal("1") if resolved_yes else Decimal("0")
    return ProxyTradeResult(
        entry_price=entry_price,
        exit_price=settlement_price,
        pnl=settlement_price - entry_price,
        exited_before_settlement=False,
    )


def bucket_market_for_aligned_lower(markets: Sequence[Market], lower_f: int) -> Market:
    """Map an aligned two-degree bucket to the event market, including open tails."""
    matches = []
    for market in markets:
        bucket = market_temperature_bucket(market)
        if bucket.contains(lower_f) and bucket.contains(lower_f + 1):
            matches.append(market)
    if len(matches) != 1:
        raise ValueError(f"expected one market for [{lower_f},{lower_f + 1}], found {len(matches)}")
    return matches[0]


def _request_json_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    attempts: int = 5,
) -> Any:
    for attempt in range(attempts):
        try:
            response = client.request(method, url, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.5 * (2**attempt))
    raise AssertionError("unreachable retry state")


def discover_settled_events(
    *,
    min_target_date: date,
    max_target_date: date,
    minimum_events: int = 20,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Discover exact closed KLAX/KLGA daily-high events with complete CLOB tokens."""
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
        follow_redirects=True,
    )
    try:
        cursor: str | None = None
        selected: dict[str, dict[str, Any]] = {}
        for _page_number in range(50):
            params: dict[str, Any] = {
                "closed": "true",
                "limit": 100,
                "order": "endDate",
                "ascending": "false",
                "title_search": "highest temperature",
            }
            if cursor:
                params["after_cursor"] = cursor
            payload = _request_json_with_retry(
                http_client,
                "GET",
                "https://gamma-api.polymarket.com/events/keyset",
                params=params,
            )
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list):
                raise ValueError("Gamma keyset response has no events list")
            oldest_target: date | None = None
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                try:
                    _station_id, _timezone, target_date = event_identity(
                        str(raw_event.get("slug") or "")
                    )
                except (KeyError, ValueError):
                    continue
                oldest_target = (
                    target_date if oldest_target is None else min(oldest_target, target_date)
                )
                raw_markets = raw_event.get("markets") or []
                if not (
                    min_target_date <= target_date <= max_target_date
                    and raw_event.get("closed") is True
                    and isinstance(raw_markets, list)
                    and raw_markets
                ):
                    continue
                markets = [Market.from_gamma(item) for item in raw_markets if isinstance(item, dict)]
                if len(markets) != len(raw_markets) or any(
                    not market.clob_token_ids for market in markets
                ):
                    continue
                resolved_yes_market(markets)
                selected[str(raw_event["slug"])] = raw_event
            cursor = payload.get("next_cursor")
            if (
                len(selected) >= minimum_events
                and oldest_target is not None
                and oldest_target <= min_target_date
            ) or not cursor:
                break
            time.sleep(0.1)
        return sorted(
            selected.values(),
            key=lambda event: event_identity(str(event["slug"]))[2],
        )
    finally:
        if owns_client:
            http_client.close()


def settled_event_catalog(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    catalog_events = []
    for raw_event in events:
        station_id, timezone, target_date = event_identity(str(raw_event["slug"]))
        markets = [
            Market.from_gamma(item)
            for item in raw_event.get("markets") or []
            if isinstance(item, dict)
        ]
        winner = resolved_yes_market(markets)
        catalog_events.append(
            {
                "event_id": str(raw_event["id"]),
                "event_slug": str(raw_event["slug"]),
                "station_id": station_id,
                "timezone": timezone,
                "target_date": target_date.isoformat(),
                "end_date": raw_event.get("endDate"),
                "winning_market_slug": winner.slug,
                "markets": [
                    {
                        "market_id": market.market_id,
                        "market_slug": market.slug,
                        "question": market.question,
                        "outcomes": list(market.outcomes),
                        "outcome_prices": [str(value) for value in market.outcome_prices],
                        "yes_token_id": yes_token_id(market),
                        "lower_f": market_temperature_bucket(market).lower_f,
                        "upper_f": market_temperature_bucket(market).upper_f,
                        "resolved_yes": market.slug == winner.slug,
                    }
                    for market in markets
                ],
            }
        )
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "gamma_source": "https://gamma-api.polymarket.com/events/keyset",
        "event_count": len(catalog_events),
        "events": catalog_events,
    }


def download_price_histories(
    catalog: Mapping[str, Any],
    *,
    output_dir: Path,
    request_interval_seconds: float = 0.1,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Download each Yes token's p-series with retry and per-request pacing."""
    output_dir.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http_client = client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
        follow_redirects=True,
    )
    summaries = []
    try:
        for event in catalog.get("events") or []:
            market_histories = []
            for market in event["markets"]:
                token_id = market["yes_token_id"]
                payload = _request_json_with_retry(
                    http_client,
                    "GET",
                    "https://clob.polymarket.com/prices-history",
                    params={"market": token_id, "interval": "max", "fidelity": 1},
                )
                history = payload.get("history") if isinstance(payload, dict) else None
                if not isinstance(history, list):
                    raise ValueError(f"CLOB history is invalid for token {token_id}")
                market_histories.append(
                    {
                        **market,
                        "point_count": len(history),
                        "history": history,
                    }
                )
                time.sleep(request_interval_seconds)
            event_payload = {
                "event_slug": event["event_slug"],
                "station_id": event["station_id"],
                "timezone": event["timezone"],
                "target_date": event["target_date"],
                "price_field": "p",
                "price_field_note": (
                    "CLOB prices-history exposes p only; it is not a historical ask or bid/ask spread."
                ),
                "markets": market_histories,
            }
            destination = output_dir / f"{event['event_slug']}.json"
            destination.write_text(
                json.dumps(event_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summaries.append(
                {
                    "event_slug": event["event_slug"],
                    "minimum_point_count": min(
                        market["point_count"] for market in market_histories
                    ),
                    "illiquid_market_count": sum(
                        market["point_count"] < 50 for market in market_histories
                    ),
                }
            )
        return summaries
    finally:
        if owns_client:
            http_client.close()


def _price_points(raw_market: Mapping[str, Any]) -> list[MarketPricePoint]:
    return sorted(
        (
            MarketPricePoint(
                token_id=str(raw_market["yes_token_id"]),
                timestamp=datetime.fromtimestamp(int(point["t"]), tz=UTC),
                price=Decimal(str(point["p"])),
            )
            for point in raw_market.get("history") or []
        ),
        key=lambda point: point.timestamp,
    )


def _catalog_market_for_lower(
    markets: Sequence[Mapping[str, Any]], lower_f: int
) -> Mapping[str, Any]:
    matches = [
        market
        for market in markets
        if (market.get("lower_f") is None or lower_f >= int(market["lower_f"]))
        and (market.get("upper_f") is None or lower_f + 1 <= int(market["upper_f"]))
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one catalog market for [{lower_f},{lower_f + 1}]")
    return matches[0]


def _bucket_label(market: Mapping[str, Any]) -> str:
    lower_f = market.get("lower_f")
    upper_f = market.get("upper_f")
    if lower_f is None:
        return f"≤{upper_f}°F"
    if upper_f is None:
        return f"≥{lower_f}°F"
    return f"{lower_f}-{upper_f}°F"


def build_market_lag_analysis(
    catalog: Mapping[str, Any],
    *,
    histories_by_event: Mapping[str, Mapping[str, Any]],
    observations_by_station: Mapping[str, Sequence[TemperatureObservation]],
    minimum_price_points: int = 50,
) -> dict[str, Any]:
    """Join local observations and UTC p-history without using a future point."""
    physical_lookup: dict[str, dict[tuple[date, clock_time], Any]] = {}
    for station_id, observations in observations_by_station.items():
        curves = certainty_curve_points(observations)
        physical_lookup[station_id] = {
            (point.target_date, scan_time): point
            for scan_time, points in curves.items()
            for point in points
        }

    records: dict[str, dict[clock_time, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    trades: list[dict[str, Any]] = []
    excluded_market_count = 0
    excluded_event_count = 0
    missing_event_times: list[dict[str, str]] = []

    for event in catalog.get("events") or []:
        event_slug = str(event["event_slug"])
        history_payload = histories_by_event[event_slug]
        history_markets = {
            str(market["market_slug"]): market
            for market in history_payload.get("markets") or []
        }
        event_illiquid_count = sum(
            int(market.get("point_count") or 0) < minimum_price_points
            for market in history_markets.values()
        )
        excluded_market_count += event_illiquid_count
        if event_illiquid_count:
            excluded_event_count += 1
            continue

        histories = {
            market_slug: _price_points(market)
            for market_slug, market in history_markets.items()
        }
        station_id = str(event["station_id"])
        target_date = date.fromisoformat(str(event["target_date"]))
        timezone = ZoneInfo(str(event["timezone"]))
        winning_slug = str(event["winning_market_slug"])
        event_rows: list[dict[str, Any]] = []

        scan_times = sorted(
            scan_time
            for sample_date, scan_time in physical_lookup[station_id]
            if sample_date == target_date
        )
        for scan_time in scan_times:
            physical_point = physical_lookup[station_id][(target_date, scan_time)]
            physical_lower = two_degree_bucket_lower(physical_point.observed_high_f)
            physical_market = _catalog_market_for_lower(event["markets"], physical_lower)
            physical_slug = str(physical_market["market_slug"])
            cutoff = datetime.combine(target_date, scan_time, tzinfo=timezone).astimezone(UTC)
            latest = {
                market_slug: latest_price_at_or_before(points, cutoff)
                for market_slug, points in histories.items()
            }
            if any(point is None for point in latest.values()):
                missing_event_times.append(
                    {
                        "event_slug": event_slug,
                        "time": scan_time.strftime("%H:%M"),
                        "reason": "at least one bucket has no historical p point at or before cutoff",
                    }
                )
                continue
            available = {
                market_slug: point
                for market_slug, point in latest.items()
                if point is not None
            }
            prices = {
                market_slug: float(point.price)
                for market_slug, point in available.items()
            }
            market_favorite_slug = max(
                prices,
                key=lambda market_slug: (prices[market_slug], market_slug),
            )
            row = {
                "event_slug": event_slug,
                "target_date": target_date.isoformat(),
                "time": scan_time.strftime("%H:%M"),
                "physical_market_slug": physical_slug,
                "physical_bucket": _bucket_label(physical_market),
                "physical_hit": physical_slug == winning_slug,
                "market_favorite_slug": market_favorite_slug,
                "market_favorite_hit": market_favorite_slug == winning_slug,
                "true_bucket_price_p": prices[winning_slug],
                "physical_bucket_price_p": prices[physical_slug],
                "maximum_price_age_minutes": max(
                    (cutoff - point.timestamp).total_seconds() / 60.0
                    for point in available.values()
                ),
            }
            records[station_id][scan_time].append(row)
            event_rows.append(row)

        candidate_start, candidate_end = (
            (clock_time(13, 0), clock_time(15, 0))
            if station_id == "KLAX"
            else (clock_time(16, 0), clock_time(17, 0))
        )
        for row in event_rows:
            scan_time = clock_time.fromisoformat(row["time"])
            entry_price = Decimal(str(row["physical_bucket_price_p"]))
            if not candidate_start <= scan_time <= candidate_end or entry_price >= Decimal("0.85"):
                continue
            entry_slug = str(row["physical_market_slug"])
            entry_cutoff = datetime.combine(
                target_date, scan_time, tzinfo=timezone
            ).astimezone(UTC)
            day_end = datetime.combine(
                target_date, clock_time(23, 59, 59), tzinfo=timezone
            ).astimezone(UTC)
            qualifying_exits = [
                point
                for point in histories[entry_slug]
                if entry_cutoff < point.timestamp <= day_end and point.price >= Decimal("0.95")
            ]
            if qualifying_exits:
                exit_point = min(qualifying_exits, key=lambda point: point.timestamp)
                trade_result = proxy_trade_pnl(
                    entry_price=entry_price,
                    exit_price=exit_point.price,
                )
                exit_label = exit_point.timestamp.isoformat()
            else:
                trade_result = proxy_trade_pnl(
                    entry_price=entry_price,
                    resolved_yes=entry_slug == winning_slug,
                )
                exit_label = "settlement"
            trades.append(
                {
                    "date": target_date.isoformat(),
                    "station_id": station_id,
                    "time": row["time"],
                    "bucket": row["physical_bucket"],
                    "entry_price_p": float(trade_result.entry_price),
                    "exit_price_p_or_settlement": float(trade_result.exit_price),
                    "pnl_per_share_p_proxy": float(trade_result.pnl),
                    "physical_bucket_won": entry_slug == winning_slug,
                    "exit": exit_label,
                    "historical_ask": None,
                    "historical_spread": None,
                }
            )
            break

    curves_output: dict[str, list[dict[str, Any]]] = {}
    for station_id, by_time in records.items():
        curves_output[station_id] = []
        for scan_time, rows in sorted(by_time.items()):
            physical_hit_rate = fmean(row["physical_hit"] for row in rows)
            mean_physical_price = fmean(
                row["physical_bucket_price_p"] for row in rows
            )
            curves_output[station_id].append(
                {
                    "time": scan_time.strftime("%H:%M"),
                    "n": len(rows),
                    "physical_hit_rate": physical_hit_rate,
                    "market_favorite_hit_rate": fmean(
                        row["market_favorite_hit"] for row in rows
                    ),
                    "mean_true_bucket_price_p": fmean(
                        row["true_bucket_price_p"] for row in rows
                    ),
                    "mean_physical_bucket_price_p": mean_physical_price,
                    "theoretical_gap_vs_p": physical_hit_rate - mean_physical_price,
                    "median_maximum_price_age_minutes": median(
                        row["maximum_price_age_minutes"] for row in rows
                    ),
                }
            )

    trade_summary: dict[str, dict[str, Any]] = {}
    for station_id in ("KLAX", "KLGA", "ALL"):
        selected_trades = (
            trades
            if station_id == "ALL"
            else [trade for trade in trades if trade["station_id"] == station_id]
        )
        if not selected_trades:
            trade_summary[station_id] = {"trade_count": 0}
            continue
        pnl_values = [trade["pnl_per_share_p_proxy"] for trade in selected_trades]
        entry_cost = sum(trade["entry_price_p"] for trade in selected_trades)
        trade_summary[station_id] = {
            "trade_count": len(selected_trades),
            "profitable_trade_rate": fmean(value > 0 for value in pnl_values),
            "physical_bucket_win_rate": fmean(
                trade["physical_bucket_won"] for trade in selected_trades
            ),
            "mean_pnl_per_share_p_proxy": fmean(pnl_values),
            "maximum_single_trade_loss_p_proxy": min(pnl_values),
            "total_pnl_p_proxy": sum(pnl_values),
            "return_on_entry_cost_p_proxy": sum(pnl_values) / entry_cost,
        }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "price_semantics": (
            "Polymarket CLOB prices-history p point; historical ask and spread are unavailable"
        ),
        "catalog_event_count": len(catalog.get("events") or []),
        "analyzed_event_count": len(catalog.get("events") or []) - excluded_event_count,
        "excluded_event_count": excluded_event_count,
        "excluded_market_count": excluded_market_count,
        "missing_event_times": missing_event_times,
        "curves": curves_output,
        "trades": trades,
        "trade_summary": trade_summary,
    }
