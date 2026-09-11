from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from poly_weather.adapters.polymarket import EventSnapshot
from poly_weather.domain import Market, SettlementSpec, VerificationStatus, WindowBasis
from poly_weather.market_supervisor import MarketEventSupervisor


class FakeBot:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[str, ...]]] = []

    async def subscribe_assets(
        self,
        *,
        asset_slugs: dict[str, str],
        asset_events: dict[str, str],
        event_policies: dict[str, Any] | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        del asset_events, event_policies, timeout_seconds
        self.log.append(("subscribe_books_confirmed", tuple(asset_slugs)))

    async def unsubscribe_assets(
        self,
        asset_ids: list[str] | tuple[str, ...],
        *,
        timeout_seconds: float = 30,
    ) -> None:
        del timeout_seconds
        self.log.append(("unsubscribe", tuple(asset_ids)))


def _spec() -> SettlementSpec:
    return SettlementSpec(
        key="city",
        market_slug_pattern=r"highest-temperature-in-city-on-[a-z]+-\d{1,2}-\d{4}",
        station_id="KAAA",
        station_name="City Airport",
        latitude=1,
        longitude=2,
        timezone="UTC",
        bucket_width_degrees=2,
        window_basis=WindowBasis.LOCAL_CIVIL_DAY,
        resolution_source_url="https://www.weather.gov/wrh/timeseries?site=kaaa",
        status=VerificationStatus.VERIFIED,
    )


def _event(day: int, token_prefix: str, *, station: str = "KAAA") -> EventSnapshot:
    base = f"highest-temperature-in-city-on-august-{day}-2026"
    markets = []
    for index, suffix in enumerate(("67forbelow", "68-69f", "70forhigher")):
        markets.append(
            Market.from_gamma(
                {
                    "id": f"{token_prefix}-{index}",
                    "question": "temperature bucket",
                    "slug": f"{base}-{suffix}",
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.5","0.5"]',
                    "clobTokenIds": f'["{token_prefix}-yes-{index}","{token_prefix}-no-{index}"]',
                }
            )
        )
    source = f"https://www.weather.gov/wrh/timeseries?site={station.lower()}"
    description = (
        "This market resolves using information from NOAA and the highest reading under the Temp "
        "column in Hourly Data recorded at the City Airport in degrees Fahrenheit on 24 Aug '26. "
        "The resolution source is "
        f"{source}. This market can not resolve until the first data point for the following date "
        "has been published. The resolution source measures temperatures to whole degrees "
        "Fahrenheit. After which any alterations will not be considered."
    )
    return EventSnapshot(
        request_url="https://gamma-api.polymarket.com/events",
        fetched_at=datetime.now(UTC),
        event_id=f"event-{day}",
        event_slug=base,
        title=f"Highest temperature in City on August {day}?",
        resolution_source=source,
        raw_payload={"description": description, "resolutionSource": source},
        markets=tuple(markets),
    )


def test_simulated_rollover_subscribes_and_confirms_before_old_unsubscribe(tmp_path) -> None:
    async def run() -> list[tuple[str, tuple[str, ...]]]:
        bot = FakeBot()
        supervisor = MarketEventSupervisor(
            specs=(_spec(),), bot=bot, data_dir=tmp_path  # type: ignore[arg-type]
        )
        old = _event(24, "old")
        new = _event(25, "new")
        await supervisor.reconcile({"city": old})
        await supervisor.reconcile(
            {"city": new}, closed_event_slugs={old.event_slug}
        )
        assert set(supervisor.active) == {new.event_slug}
        return bot.log

    log = asyncio.run(run())
    assert [operation for operation, _ in log] == [
        "subscribe_books_confirmed",
        "subscribe_books_confirmed",
        "unsubscribe",
    ]
    assert all(asset.startswith("new-") for asset in log[1][1])
    assert all(asset.startswith("old-") for asset in log[2][1])


def test_verification_failure_is_fail_closed(tmp_path) -> None:
    async def run() -> tuple[int, dict[str, dict[str, Any]]]:
        bot = FakeBot()
        supervisor = MarketEventSupervisor(
            specs=(_spec(),), bot=bot, data_dir=tmp_path  # type: ignore[arg-type]
        )
        await supervisor.reconcile({"city": _event(25, "bad", station="KBBB")})
        return len(bot.log), supervisor.failures

    subscriptions, failures = asyncio.run(run())
    assert subscriptions == 0
    assert failures["city"]["fail_closed"] is True
    assert "station_exact" in failures["city"]["differences"]


def test_raw_market_recovery_skips_retention_and_downstream_publication(
    tmp_path, monkeypatch
) -> None:
    def forbidden_retention(*_, **__):
        raise AssertionError("raw market recovery invoked retention")

    monkeypatch.setattr(
        "poly_weather.market_supervisor.apply_market_retention", forbidden_retention
    )
    bot = FakeBot()
    supervisor = MarketEventSupervisor(
        specs=(_spec(),),
        bot=bot,  # type: ignore[arg-type]
        data_dir=tmp_path,
        raw_collection_recovery=True,
        startup_attempt_id="attempt-test",
    )

    async def run() -> None:
        await supervisor._apply_retention()
        await supervisor.reconcile({"city": _event(25, "recovery")})

    asyncio.run(run())

    assert supervisor.retention_result == {
        "state": "disabled_raw_market_recovery",
        "mutations_attempted": False,
    }
    assert supervisor.published_count == 0
    assert supervisor.signal_update_path.exists() is False
    status = json.loads(supervisor.status_path.read_text(encoding="utf-8"))
    assert status["collection_mode"] == "raw_market_recovery"
    assert status["startup_attempt_id"] == "attempt-test"
    assert status["downstream_start_blocked"] is True
    assert status["signal_update_publication_enabled"] is False
