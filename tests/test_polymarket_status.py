import asyncio
import html
from datetime import UTC, datetime

import httpx

from poly_weather.polymarket_status import (
    PolymarketStatusClient,
    UpstreamQualityWindow,
    market_record_is_analysis_eligible,
    merge_quality_windows,
    parse_status_atom,
)


def _feed(content: str, *, incident_id: str = "incident-1") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>tag:status.polymarket.com,2005:Maintenance/{incident_id}</id>
    <title>Scheduled CLOB maintenance</title>
    <published>2026-08-26T04:00:00.000+00:00</published>
    <link href="https://status.polymarket.com/{incident_id}" />
    <content type="html">{html.escape(content)}</content>
  </entry>
</feed>"""


def _content(*, completed: bool = False) -> str:
    ending = (
        "<p><small>Aug <var data-var='date'> 26</var>, "
        "<var data-var='time'>07:42:00</var> GMT+0</small><br />"
        "<strong>Completed</strong> - Maintenance completed.</p>"
        if completed
        else "<p><small>Aug <var data-var='date'> 26</var>, "
        "<var data-var='time'>06:56:19</var> GMT+0</small><br />"
        "<strong>Identified</strong> - Expected resolution is 7:30am UTC.</p>"
    )
    return (
        "<p><strong>Type:</strong> Maintenance</p>"
        "<p><strong>Affected Components:</strong> Clob Websocket, Trading API (CLOB)</p>"
        + ending
    )


def test_status_parser_uses_component_and_explicit_active_state() -> None:
    windows = parse_status_atom(_feed(_content()), active_ids={"incident-1"})

    assert len(windows) == 1
    window = windows[0]
    assert window.status == "active"
    assert window.end_at is None
    assert window.affects_market_data is True
    assert window.affects_trading is True
    assert window.default_excluded is True
    assert window.latest_update_at == datetime(2026, 8, 26, 6, 56, 19, tzinfo=UTC)
    assert window.latest_update_message == "Expected resolution is 7:30am UTC."


def test_completed_and_unknown_windows_do_not_become_permanently_active() -> None:
    completed = parse_status_atom(_feed(_content(completed=True)))[0]
    unknown = parse_status_atom(_feed(_content(), incident_id="unknown"))[0]

    assert completed.status == "completed"
    assert completed.end_at == datetime(2026, 8, 26, 7, 42, tzinfo=UTC)
    assert completed.default_excluded is True
    assert unknown.status == "unknown"
    assert unknown.default_excluded is False


def test_quality_windows_merge_and_exclude_legacy_rows() -> None:
    completed = parse_status_atom(_feed(_content(completed=True)))[0]
    older = UpstreamQualityWindow(
        incident_id="older",
        title="Older",
        incident_type="maintenance",
        start_at=datetime(2026, 8, 13, 4, 30, tzinfo=UTC),
        end_at=datetime(2026, 8, 13, 5, 0, tzinfo=UTC),
        affected_components=("Clob Websocket",),
        affects_market_data=True,
        affects_trading=False,
        status="completed",
        source_url="https://status.polymarket.com/older",
        default_excluded=True,
    )
    merged = merge_quality_windows((older,), (completed,))

    assert [row.incident_id for row in merged] == ["older", "incident-1"]
    assert not market_record_is_analysis_eligible(
        {}, datetime(2026, 8, 26, 5, tzinfo=UTC), merged
    )
    assert market_record_is_analysis_eligible(
        {}, datetime(2026, 8, 26, 8, tzinfo=UTC), merged
    )


def test_status_client_probes_only_verified_summary_and_atom_endpoints() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/v2/summary.json":
            return httpx.Response(
                200,
                request=request,
                json={
                    "page": {"status": "SOMEMAJOROUTAGE"},
                    "activeMaintenances": [{"id": "incident-1"}],
                },
            )
        return httpx.Response(200, request=request, text=_feed(_content()))

    async def run() -> None:
        async_client = httpx.AsyncClient(
            base_url="https://status.polymarket.com",
            transport=httpx.MockTransport(handler),
        )
        client = PolymarketStatusClient(client=async_client)
        try:
            snapshot = await client.fetch()
            assert snapshot.upstream_maintenance is True
        finally:
            await async_client.aclose()

    asyncio.run(run())
    assert requested == ["/api/v2/summary.json", "/history.atom"]
