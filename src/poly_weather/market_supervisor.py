from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from poly_weather.adapters.polymarket import EventSnapshot, GammaClient
from poly_weather.domain import SettlementSpec
from poly_weather.market_stream import EventArchivePolicy, MarketWebSocketBot
from poly_weather.retention import RetentionConfig, apply_market_retention, disk_capacity_status
from poly_weather.settlement import parse_settlement_evidence, verify_settlement_evidence
from poly_weather.storage import RawEventArchive


@dataclass(slots=True)
class ActiveEvent:
    settlement_key: str
    event_id: str
    event_slug: str
    target_date: str
    evidence_sha256: str
    asset_ids: tuple[str, ...]
    subscribed_at: str
    books_confirmed_at: str


@dataclass(slots=True)
class SupervisorMetrics:
    state: str = "starting"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    cycles: int = 0
    discovered: int = 0
    verified: int = 0
    skipped: int = 0
    hot_subscriptions: int = 0
    hot_unsubscriptions: int = 0
    last_cycle_at: str | None = None
    last_error: str | None = None


def event_asset_maps(event: EventSnapshot) -> tuple[dict[str, str], dict[str, str]]:
    slugs: dict[str, str] = {}
    events: dict[str, str] = {}
    for market in event.markets:
        if len(market.outcomes) != len(market.clob_token_ids):
            continue
        for outcome, token_id in zip(market.outcomes, market.clob_token_ids, strict=True):
            slugs[token_id] = f"{event.event_slug}/{market.slug}:{outcome}"
            events[token_id] = event.event_slug
    if not slugs:
        raise ValueError(f"event {event.event_slug} has no aligned CLOB token identifiers")
    return slugs, events


def _city_query(spec: SettlementSpec) -> str:
    match = re.search(r"highest-temperature-in-(.+?)-on-", spec.market_slug_pattern)
    if not match:
        raise ValueError(f"cannot derive city search term from {spec.market_slug_pattern!r}")
    city = match.group(1).replace("\\", "").replace("-", " ")
    return "New York City" if city == "nyc" else city.title()


def discover_event(
    gamma: GammaClient,
    spec: SettlementSpec,
    target_date: date,
) -> EventSnapshot | None:
    """Discover one exact active event from Gamma public-search, without slug guessing."""
    city = _city_query(spec)
    query = f"highest temperature in {city} on {target_date.strftime('%B')} {target_date.day}"
    page = gamma.search_markets_page(query=query, limit=50)
    suffix = target_date.strftime("-%B-%d-%Y").lower().replace("-0", "-")
    candidates = [
        event
        for event in page.events
        if re.fullmatch(spec.market_slug_pattern, event.event_slug)
        and event.event_slug.endswith(suffix)
    ]
    if len(candidates) > 1:
        raise ValueError(f"ambiguous Gamma discovery for {spec.key}: {len(candidates)} events")
    return candidates[0] if candidates else None


class MarketEventSupervisor:
    """Fail-closed daily event rotation layered over one live market WebSocket."""

    def __init__(
        self,
        *,
        specs: tuple[SettlementSpec, ...],
        bot: MarketWebSocketBot,
        data_dir: Path,
        discovery_interval_seconds: float = 300.0,
    ) -> None:
        self.specs = specs
        self.bot = bot
        self.data_dir = data_dir
        self.discovery_interval_seconds = discovery_interval_seconds
        self.metrics = SupervisorMetrics()
        self.active: dict[str, ActiveEvent] = {}
        self.failures: dict[str, dict[str, Any]] = {}
        self.status_path = data_dir / "runtime" / "market_supervisor_status.json"
        self.signal_update_path = data_dir / "runtime" / "signal_config_update.json"
        self.archive = RawEventArchive(data_dir / "raw")
        self.retention_config = RetentionConfig()
        self.retention_result: dict[str, Any] = {}

    async def reconcile(
        self,
        events_by_spec: dict[str, EventSnapshot],
        *,
        closed_event_slugs: set[str] | None = None,
    ) -> None:
        """Verify and subscribe replacements before removing closed predecessors."""
        now = datetime.now(UTC)
        for spec in self.specs:
            event = events_by_spec.get(spec.key)
            if event is None or event.event_slug in self.active:
                continue
            self.metrics.discovered += 1
            self.archive.append(
                source="polymarket_gamma_event",
                fetched_at=event.fetched_at,
                request_url=event.request_url,
                payload=event.raw_payload,
            )
            try:
                evidence = parse_settlement_evidence(event, registry_spec=spec)
                verification = verify_settlement_evidence(evidence, spec)
            except Exception as exc:
                self._record_failure(spec.key, event.event_slug, f"parse error: {exc}")
                continue
            evidence_payload = {
                "evidence": evidence.model_dump(mode="json"),
                "verification": verification.model_dump(mode="json"),
            }
            self.archive.append(
                source="settlement_evidence",
                fetched_at=now,
                request_url=f"model://settlement-evidence/{event.event_slug}",
                payload=evidence_payload,
            )
            if not verification.passed:
                self._record_failure(
                    spec.key,
                    event.event_slug,
                    verification.reason,
                    differences=list(verification.failures),
                )
                continue
            asset_slugs, asset_events = event_asset_maps(event)
            try:
                await self.bot.subscribe_assets(
                    asset_slugs=asset_slugs,
                    asset_events=asset_events,
                    event_policies={
                        event.event_slug: EventArchivePolicy(
                            timezone=spec.timezone or "UTC",
                            target_date=evidence.target_date,
                        )
                    },
                )
            except Exception as exc:
                self._record_failure(spec.key, event.event_slug, f"book confirmation failed: {exc}")
                continue
            confirmed = datetime.now(UTC).isoformat()
            self.active[event.event_slug] = ActiveEvent(
                settlement_key=spec.key,
                event_id=event.event_id,
                event_slug=event.event_slug,
                target_date=str(evidence.target_date),
                evidence_sha256=evidence.evidence_sha256,
                asset_ids=tuple(asset_slugs),
                subscribed_at=now.isoformat(),
                books_confirmed_at=confirmed,
            )
            self.metrics.verified += 1
            self.metrics.hot_subscriptions += 1
            self.failures.pop(spec.key, None)

        # Old target days overlap the new day. They are removed only after Gamma
        # marks the event closed, and only after all new subscriptions above have
        # received authoritative book snapshots.
        for event_slug in sorted(closed_event_slugs or set()):
            binding = self.active.get(event_slug)
            if binding is None:
                continue
            await self.bot.unsubscribe_assets(list(binding.asset_ids))
            del self.active[event_slug]
            self.metrics.hot_unsubscriptions += 1
        self._publish_signal_update()
        self._write_status()

    def _record_failure(
        self,
        settlement_key: str,
        event_slug: str,
        reason: str,
        *,
        differences: list[str] | None = None,
    ) -> None:
        self.metrics.skipped += 1
        self.failures[settlement_key] = {
            "event_slug": event_slug,
            "reason": reason,
            "differences": differences or [],
            "failed_at": datetime.now(UTC).isoformat(),
            "fail_closed": True,
        }

    def _publish_signal_update(self) -> None:
        self.signal_update_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generation": time.time_ns(),
            "updated_at": datetime.now(UTC).isoformat(),
            "execution_enabled": False,
            "retention": self.retention_result,
            **disk_capacity_status(
                self.data_dir,
                warning_gb=self.retention_config.disk_warning_gb,
                projected_trimmed_gb_per_day=(
                    self.retention_config.projected_trimmed_gb_per_day
                ),
            ),
            "events": [asdict(item) for item in self.active.values()],
        }
        self._atomic_json(self.signal_update_path, payload)

    def _write_status(self) -> None:
        payload = {
            **asdict(self.metrics),
            "pid": __import__("os").getpid(),
            "updated_at": datetime.now(UTC).isoformat(),
            "active_events": [asdict(item) for item in self.active.values()],
            "verification_failures": self.failures,
            "state_path": str(self.status_path.resolve()),
            "signal_update_path": str(self.signal_update_path.resolve()),
            "execution_enabled": False,
        }
        self._atomic_json(self.status_path, payload)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{__import__('os').getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    async def run(self, *, runtime_seconds: float = 0) -> None:
        self.metrics.state = "running"
        self._write_status()
        deadline = time.monotonic() + runtime_seconds if runtime_seconds > 0 else None
        try:
            while not self.bot.stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                self.metrics.cycles += 1
                self.metrics.last_cycle_at = datetime.now(UTC).isoformat()
                discovered: dict[str, EventSnapshot] = {}
                closed: set[str] = set()
                try:
                    self.retention_result = await asyncio.to_thread(
                        apply_market_retention,
                        self.data_dir,
                        config=self.retention_config,
                    )
                    with GammaClient() as gamma:
                        for spec in self.specs:
                            local_today = datetime.now(ZoneInfo(spec.timezone or "UTC")).date()
                            for target in (local_today, local_today + timedelta(days=1)):
                                event = await asyncio.to_thread(discover_event, gamma, spec, target)
                                if event is not None:
                                    discovered[spec.key] = event
                        for slug in tuple(self.active):
                            snapshot = await asyncio.to_thread(gamma.event_by_slug, slug)
                            if bool(snapshot.raw_payload.get("closed")):
                                closed.add(slug)
                    await self.reconcile(discovered, closed_event_slugs=closed)
                    self.metrics.last_error = None
                except Exception as exc:
                    self.metrics.last_error = f"{type(exc).__name__}: {exc}"
                self._write_status()
                delay = self.discovery_interval_seconds
                if deadline is not None:
                    delay = min(delay, max(0.0, deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(self.bot.stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self.metrics.state = "stopped"
            self._write_status()
