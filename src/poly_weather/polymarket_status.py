"""Machine-readable Polymarket status and market-data quality windows.

The status site is powered by Instatus.  At the time this module was added only
``/api/v2/summary.json`` and the Atom/RSS history feeds were public; the common
Statuspage incident endpoints returned 404.  We therefore combine the summary
for active-state truth with Atom for component names and completed timestamps.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

STATUS_BASE_URL = "https://status.polymarket.com"
STATUS_POLL_SECONDS = 5 * 60
MARKET_DATA_COMPONENTS = frozenset({"clob websocket"})
TRADING_COMPONENTS = frozenset({"trading api (clob)"})


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class UpstreamQualityWindow:
    incident_id: str
    title: str
    incident_type: str
    start_at: datetime
    end_at: datetime | None
    affected_components: tuple[str, ...]
    affects_market_data: bool
    affects_trading: bool
    status: str
    source_url: str
    default_excluded: bool
    latest_update_at: datetime | None = None
    latest_update_state: str | None = None
    latest_update_message: str | None = None

    def contains(self, timestamp: datetime) -> bool:
        point = timestamp.astimezone(UTC)
        return self.start_at <= point and (self.end_at is None or point < self.end_at)

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_at"] = self.start_at.isoformat()
        payload["end_at"] = self.end_at.isoformat() if self.end_at else None
        payload["latest_update_at"] = (
            self.latest_update_at.isoformat() if self.latest_update_at else None
        )
        return payload


@dataclass(frozen=True, slots=True)
class PolymarketStatusSnapshot:
    checked_at: datetime
    page_status: str
    page_message: str
    windows: tuple[UpstreamQualityWindow, ...]

    @property
    def active_market_data_windows(self) -> tuple[UpstreamQualityWindow, ...]:
        return tuple(
            window
            for window in self.windows
            if window.status == "active" and window.affects_market_data
        )

    @property
    def upstream_maintenance(self) -> bool:
        return bool(self.active_market_data_windows)


_COMPONENTS_RE = re.compile(
    r"Affected Components:</strong>\s*(.*?)</p>", re.IGNORECASE | re.DOTALL
)
_TYPE_RE = re.compile(r"Type:</strong>\s*([^<]+)", re.IGNORECASE)
_UPDATE_RE = re.compile(
    r"(?P<month>[A-Z][a-z]{2})\s*<var[^>]*>\s*(?P<day>\d{1,2})</var>,\s*"
    r"<var[^>]*>\s*(?P<clock>\d{2}:\d{2}:\d{2})</var>\s*GMT\+0.*?"
    r"<strong>(?P<state>[^<]+)</strong>\s*-\s*(?P<message>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_MONTH = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        1,
    )
}


def _plain_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _update_timestamp(match: re.Match[str], *, reference: datetime) -> datetime:
    clock = datetime.strptime(match.group("clock"), "%H:%M:%S").time()
    value = datetime(
        reference.year,
        _MONTH[match.group("month").title()],
        int(match.group("day")),
        clock.hour,
        clock.minute,
        clock.second,
        tzinfo=UTC,
    )
    if value < reference - timedelta(days=180):
        value = value.replace(year=value.year + 1)
    return value


def parse_status_atom(
    atom_text: str,
    *,
    active_ids: set[str] | None = None,
) -> tuple[UpstreamQualityWindow, ...]:
    """Parse incident windows; active IDs override misleading planned durations."""
    active_ids = active_ids or set()
    root = ET.fromstring(atom_text)
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    windows: list[UpstreamQualityWindow] = []
    for entry in root.findall("a:entry", namespace):
        raw_id = entry.findtext("a:id", default="", namespaces=namespace)
        incident_id = raw_id.rsplit("/", 1)[-1]
        title = entry.findtext("a:title", default="", namespaces=namespace).strip()
        published = entry.findtext("a:published", default="", namespaces=namespace)
        content = entry.findtext("a:content", default="", namespaces=namespace)
        link_node = entry.find("a:link", namespace)
        source_url = link_node.attrib.get("href", "") if link_node is not None else ""
        if not incident_id or not published:
            continue
        components_match = _COMPONENTS_RE.search(content)
        components = (
            tuple(
                item.strip()
                for item in re.sub(r"<[^>]+>", "", components_match.group(1)).split(",")
                if item.strip()
            )
            if components_match
            else ()
        )
        normalized = {html.unescape(item).casefold() for item in components}
        affects_market_data = bool(normalized & MARKET_DATA_COMPONENTS)
        affects_trading = bool(normalized & TRADING_COMPONENTS)
        if not (affects_market_data or affects_trading):
            continue
        type_match = _TYPE_RE.search(content)
        incident_type = html.unescape(type_match.group(1)).strip() if type_match else "Incident"
        start_at = _utc(published)
        update_matches = list(_UPDATE_RE.finditer(content))
        end_at: datetime | None = None
        completion_matches = [
            match
            for match in update_matches
            if match.group("state").strip().casefold() in {"completed", "resolved"}
        ]
        if incident_id not in active_ids and completion_matches:
            end_at = _update_timestamp(completion_matches[-1], reference=start_at)
        status = "active" if incident_id in active_ids else ("completed" if end_at else "unknown")
        latest_match = update_matches[-1] if update_matches else None
        windows.append(
            UpstreamQualityWindow(
                incident_id=incident_id,
                title=title,
                incident_type=incident_type.casefold(),
                start_at=start_at,
                end_at=end_at,
                affected_components=components,
                affects_market_data=affects_market_data,
                affects_trading=affects_trading,
                status=status,
                source_url=source_url,
                default_excluded=affects_market_data and status in {"active", "completed"},
                latest_update_at=(
                    _update_timestamp(latest_match, reference=start_at)
                    if latest_match is not None
                    else None
                ),
                latest_update_state=(
                    latest_match.group("state").strip() if latest_match is not None else None
                ),
                latest_update_message=(
                    _plain_text(latest_match.group("message"))
                    if latest_match is not None
                    else None
                ),
            )
        )
    return tuple(sorted(windows, key=lambda item: item.start_at))


def merge_quality_windows(
    existing: tuple[UpstreamQualityWindow, ...] | list[UpstreamQualityWindow],
    incoming: tuple[UpstreamQualityWindow, ...] | list[UpstreamQualityWindow],
) -> tuple[UpstreamQualityWindow, ...]:
    """Preserve history that has fallen out of the finite status feed."""
    merged = {window.incident_id: window for window in existing}
    merged.update({window.incident_id: window for window in incoming})
    return tuple(sorted(merged.values(), key=lambda item: item.start_at))


def quality_window_at(
    windows: tuple[UpstreamQualityWindow, ...] | list[UpstreamQualityWindow],
    timestamp: datetime,
) -> UpstreamQualityWindow | None:
    matches = [
        window
        for window in windows
        if window.default_excluded and window.affects_market_data and window.contains(timestamp)
    ]
    return max(matches, key=lambda item: item.start_at, default=None)


def market_record_is_analysis_eligible(
    record: dict[str, Any],
    timestamp: datetime,
    windows: tuple[UpstreamQualityWindow, ...] | list[UpstreamQualityWindow] = (),
) -> bool:
    """Default-exclude upstream-degraded rows, including legacy rows via windows."""
    explicit = str(record.get("upstream_status") or "normal").casefold()
    if explicit != "normal":
        return False
    window = quality_window_at(windows, timestamp)
    return window is None or not window.default_excluded


def load_quality_windows(path: Path) -> tuple[UpstreamQualityWindow, ...]:
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("windows", payload) if isinstance(payload, dict) else payload
    return tuple(
        UpstreamQualityWindow(
            incident_id=str(row["incident_id"]),
            title=str(row["title"]),
            incident_type=str(row["incident_type"]),
            start_at=_utc(str(row["start_at"])),
            end_at=_utc(str(row["end_at"])) if row.get("end_at") else None,
            affected_components=tuple(str(value) for value in row.get("affected_components", ())),
            affects_market_data=bool(row.get("affects_market_data")),
            affects_trading=bool(row.get("affects_trading")),
            status=str(row.get("status") or "unknown"),
            source_url=str(row.get("source_url") or ""),
            default_excluded=bool(row.get("default_excluded", True)),
            latest_update_at=(
                _utc(str(row["latest_update_at"])) if row.get("latest_update_at") else None
            ),
            latest_update_state=(
                str(row["latest_update_state"]) if row.get("latest_update_state") else None
            ),
            latest_update_message=(
                str(row["latest_update_message"])
                if row.get("latest_update_message")
                else None
            ),
        )
        for row in rows or ()
    )


def persist_quality_windows(path: Path, windows: tuple[UpstreamQualityWindow, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(UTC).isoformat(),
        "default_analysis_policy": "exclude windows where default_excluded=true",
        "windows": [window.as_json() for window in windows],
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class PolymarketStatusClient:
    def __init__(
        self,
        *,
        base_url: str = STATUS_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
            headers={"User-Agent": "poly-weather/0.1 (research; read-only)"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> PolymarketStatusSnapshot:
        summary_response, atom_response = await __import__("asyncio").gather(
            self._client.get("/api/v2/summary.json"),
            self._client.get("/history.atom"),
        )
        summary_response.raise_for_status()
        atom_response.raise_for_status()
        summary = summary_response.json()
        active_rows = tuple(summary.get("activeMaintenances") or ()) + tuple(
            summary.get("activeIncidents") or ()
        )
        active_ids = {str(row["id"]) for row in active_rows if row.get("id")}
        windows = parse_status_atom(atom_response.text, active_ids=active_ids)
        page = summary.get("page") or {}
        return PolymarketStatusSnapshot(
            checked_at=datetime.now(UTC),
            page_status=str(page.get("status") or "UNKNOWN"),
            page_message=str(page.get("statusDescription") or page.get("statusText") or ""),
            windows=windows,
        )
