"""Retrospective attribution of market-stream records to official status windows."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poly_weather.polymarket_status import UpstreamQualityWindow, quality_window_at


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _overlap_seconds(
    start: datetime,
    end: datetime,
    window: UpstreamQualityWindow,
) -> float:
    overlap_start = max(start, window.start_at)
    overlap_end = min(end, window.end_at or end)
    return max(0.0, (overlap_end - overlap_start).total_seconds())


def audit_reconnect_rows(
    rows: Sequence[Mapping[str, Any]],
    windows: Sequence[UpstreamQualityWindow],
) -> dict[str, Any]:
    exact: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for row in rows:
        if row.get("record_type") == "legacy_unattributed_summary":
            start = _utc(row["previous_started_at"])
            end = _utc(row["previous_last_event_at"])
            overlaps = [
                {
                    "incident_id": window.incident_id,
                    "title": window.title,
                    "overlap_seconds": _overlap_seconds(start, end, window),
                }
                for window in windows
                if _overlap_seconds(start, end, window) > 0
            ]
            legacy.append(
                {
                    "reconnect_count": int(row.get("reconnect_count") or 0),
                    "previous_run_id": row.get("previous_run_id"),
                    "interval_start": start.isoformat(),
                    "interval_end": end.isoformat(),
                    "overlapping_official_windows": overlaps,
                    "attribution": (
                        "unrecoverable_exact_times; interval overlaps official window"
                        if overlaps
                        else "unrecoverable_exact_times; no official window overlap"
                    ),
                }
            )
            continue
        if not row.get("observed_at"):
            continue
        observed_at = _utc(row["observed_at"])
        window = quality_window_at(list(windows), observed_at)
        exact.append(
            {
                "observed_at": observed_at.isoformat(),
                "error": row.get("error"),
                "incident_id": window.incident_id if window else None,
                "incident_title": window.title if window else None,
            }
        )
    attributed = sum(row["incident_id"] is not None for row in exact)
    return {
        "exact_reconnect_count": len(exact),
        "officially_attributed_reconnect_count": attributed,
        "unattributed_exact_reconnect_count": len(exact) - attributed,
        "legacy_unattributed_reconnect_count": sum(
            row["reconnect_count"] for row in legacy
        ),
        "exact_rows": exact,
        "legacy_summaries": legacy,
    }


def load_reconnect_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def audit_archive_paths(
    paths: Sequence[Path],
    windows: Sequence[UpstreamQualityWindow],
) -> dict[str, Any]:
    total = 0
    in_window = 0
    explicitly_tagged = 0
    legacy_untagged = 0
    malformed = 0
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                total += 1
                try:
                    row = json.loads(line)
                    received_at = _utc(row["received_at"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    malformed += 1
                    continue
                window = quality_window_at(list(windows), received_at)
                if window is None:
                    continue
                in_window += 1
                if str(row.get("upstream_status") or "normal").casefold() != "normal":
                    explicitly_tagged += 1
                else:
                    legacy_untagged += 1
    return {
        "path_count": len(paths),
        "record_count": total,
        "records_in_official_windows": in_window,
        "explicitly_tagged_records": explicitly_tagged,
        "legacy_untagged_records_excluded_by_time_dimension": legacy_untagged,
        "malformed_record_count": malformed,
    }


def render_maintenance_audit(result: Mapping[str, Any], output_path: Path) -> None:
    lines = [
        "# Polymarket 上游维护窗口审计",
        "",
        "官方状态按组件解析；只有影响 CLOB WebSocket 的窗口默认从深度分析排除。",
        "维护期间仍持续采集，标记不会停止 WebSocket。",
        "",
        "## 官方质量窗口",
        "",
        "| 事件 | 开始 UTC | 结束 UTC | 状态 | 组件 | 最新更新 |",
        "|---|---|---|---|---|---|",
    ]
    for window in result["windows"]:
        lines.append(
            f"| {window['title']} | {window['start_at']} | "
            f"{window['end_at'] or '仍在进行'} | {window['status']} | "
            f"{', '.join(window['affected_components'])} | "
            f"{window.get('latest_update_message') or 'N/A'} |"
        )
    reconnects = result["reconnects"]
    lines.extend(
        [
            "",
            "## 重连归因",
            "",
            f"- 有精确时间的重连：{reconnects['exact_reconnect_count']}；"
            f"对上官方窗口：{reconnects['officially_attributed_reconnect_count']}；"
            f"无法归因：{reconnects['unattributed_exact_reconnect_count']}。",
            f"- 旧版仅保留汇总、无逐次时间的重连："
            f"{reconnects['legacy_unattributed_reconnect_count']}。",
        ]
    )
    unattributed_exact = [
        row for row in reconnects["exact_rows"] if row["incident_id"] is None
    ]
    if unattributed_exact:
        lines.append(
            "- 精确时间已知但官方无法归因的区间："
            f"{unattributed_exact[0]['observed_at']}–"
            f"{unattributed_exact[-1]['observed_at']}；"
            "保留为未归因上游异常，不擅自延长官方维护窗口。"
        )
    for row in reconnects["legacy_summaries"]:
        overlap = ", ".join(
            f"{item['title']} 重叠 {item['overlap_seconds'] / 60:.1f} 分钟"
            for item in row["overlapping_official_windows"]
        ) or "无官方窗口重叠"
        lines.append(
            f"- legacy run `{row['previous_run_id']}` 覆盖 "
            f"{row['interval_start']}–{row['interval_end']}：{overlap}；"
            "因逐次时间永久缺失，不能把 28 次强行归因。"
        )
    lines.extend(["", "## 归档标记覆盖", ""])
    for name, row in result["archives"].items():
        lines.append(
            f"- {name}: 官方窗口内 {row['records_in_official_windows']} 条；"
            f"显式标记 {row['explicitly_tagged_records']}；"
            f"旧记录按时间维度默认排除 {row['legacy_untagged_records_excluded_by_time_dimension']}。"
        )
    lines.extend(
        [
            "",
            "## 分析口径",
            "",
            "历史未带字段的记录不会被修改；分析读取时使用持久化质量窗口回溯排除。"
            "这既保留原始证据，也避免把维护期盘口当作正常流动性。",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
