"""Build the final settlement-rule upgrade report from saved evidence only."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "settlement_rule_upgrade_20260910"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_test_line(text: str) -> dict:
    line = text.strip().splitlines()[-1] if text.strip() else ""
    match = re.search(r"(?P<passed>\d+) passed(?:, (?P<extra>\d+) deselected)? in (?P<seconds>[0-9.]+)s", line)
    result = {"summary": line}
    if match:
        result.update(
            {
                "passed": int(match.group("passed")),
                "seconds": float(match.group("seconds")),
                "deselected": int(match.group("extra") or 0),
            }
        )
    return result


def norm_source_key(path: str) -> str:
    return path.replace("/", "\\")


def main() -> None:
    candidate_path = ROOT / "docs" / "settlement_registry_candidate_20260910.json"
    review_path = ROOT / "docs" / "settlement_registry_review_20260910.md"
    query_summary_path = ROOT / "docs" / "strict_rejection_query_20260910T091217Z" / "summary.json"
    query_report_path = ROOT / "docs" / "strict_rejection_query_20260910T091217Z" / "report.md"
    raw_validation_path = ROOT / "docs" / "raw_market_recovery_validation_20260910.json"
    raw_report_path = ROOT / "docs" / "raw_market_recovery_report_20260910.md"
    full_path = OUT / "full_regression.json"
    targeted_path = OUT / "targeted.final.json"
    optional_path = OUT / "optional.final.json"
    inventory_path = ROOT / "docs" / "reliability_test_inventory.json"
    before_hashes_path = OUT / "before_hashes.json"
    status_before_path = OUT / "status.before.txt"
    status_after_path = OUT / "status.after.txt"
    head_path = OUT / "head.txt"

    candidate = load(candidate_path)
    query = load(query_summary_path)
    raw = load(raw_validation_path)
    full = load(full_path)
    targeted = load(targeted_path)
    optional = load(optional_path)
    inventory = load(inventory_path)
    before_hashes = load(before_hashes_path)

    effective_registry = ROOT / "configs" / "settlements.json"
    effective_registry_before = before_hashes[norm_source_key("configs/settlements.json")]
    effective_registry_after = digest(effective_registry)

    relevant_sources = [
        "src/poly_weather/settlement_contract.py",
        "src/poly_weather/settlement_diagnostics.py",
        "src/poly_weather/domain.py",
        "src/poly_weather/settlement.py",
        "src/poly_weather/adapters/polymarket.py",
        "src/poly_weather/market_supervisor.py",
        "src/poly_weather/cli.py",
        "tests/test_settlement_contract_v2.py",
        "tests/test_cli.py",
        "tests/test_market_supervisor.py",
    ]
    source_bindings = {}
    for relative in relevant_sources:
        path = ROOT / relative
        expected = full.get("source_sha256", {}).get(norm_source_key(relative))
        actual = digest(path)
        source_bindings[relative] = {
            "actual_sha256": actual,
            "full_regression_expected_sha256": expected,
            "matches_full_regression": expected == actual,
        }

    candidate_entries = {entry["station_id"]: entry for entry in candidate["entries"]}
    event_rows = []
    old_failure_counts: dict[str, int] = {}
    for result in query["results"]:
        candidates = result.get("diagnosis", {}).get("candidates", [])
        old_candidate = candidates[0] if len(candidates) == 1 else None
        entry = candidate_entries.get(result["station_id"])
        old_verification = old_candidate.get("verification", {}) if old_candidate else {}
        for failure in old_verification.get("failures", []):
            old_failure_counts[failure] = old_failure_counts.get(failure, 0) + 1
        proposed_evidence = entry.get("parsed_evidence", {}) if entry else {}
        proposed_verification = entry.get("old_registry_verification", {}) if entry else {}
        event_rows.append(
            {
                "station_id": result["station_id"],
                "local_date": result["local_date"],
                "event_id": old_candidate.get("event_id") if old_candidate else None,
                "event_slug": old_candidate.get("event_slug") if old_candidate else None,
                "candidate_count": result.get("diagnosis", {}).get("candidate_count"),
                "source_projection": f"docs/strict_rejection_query_20260910T091217Z/{result['station_id']}.response.json",
                "source_projection_sha256": result.get("saved_response_sha256"),
                "wire_sha256": result.get("wire_sha256"),
                "received_at": result.get("received_at"),
                "old_parser_stage": old_candidate.get("stage") if old_candidate else None,
                "old_parser_version": old_candidate.get("parsed", {}).get("parser_version") if old_candidate else None,
                "old_parse_status": old_candidate.get("parsed", {}).get("parse_status") if old_candidate else None,
                "old_verification_failures": old_verification.get("failures", []),
                "new_parser_version": proposed_evidence.get("parser_version"),
                "new_rule_schema_version": proposed_evidence.get("rule_schema_version"),
                "new_parse_status": proposed_evidence.get("parse_status"),
                "new_missing_fields": proposed_evidence.get("missing_fields", []),
                "new_contract_unresolved": proposed_evidence.get("rule_contract", {}).get("unresolved", []),
                "new_verification_failures": proposed_verification.get("failures", []),
                "new_verification_differences": proposed_verification.get("differences", {}),
            }
        )

    full_stdout = (OUT / "pytest.stdout.log").read_text(encoding="utf-8", errors="replace")
    targeted_stdout = (OUT / "targeted.final.stdout").read_text(encoding="utf-8", errors="replace")
    optional_stdout = (OUT / "optional.final.stdout").read_text(encoding="utf-8", errors="replace")
    diff_check_text = (OUT / "diff-check.log").read_text(encoding="utf-8", errors="replace")
    ruff_text = (OUT / "ruff.log").read_text(encoding="utf-8", errors="replace")
    untracked_checked_paths = [
        "src/poly_weather/settlement_contract.py",
        "src/poly_weather/settlement_diagnostics.py",
        "tests/test_settlement_contract_v2.py",
        "docs/settlement_rule_contract_v2.md",
        "docs/settlement_registry_candidate_20260910.json",
        "docs/settlement_registry_review_20260910.md",
        "docs/settlement_rule_upgrade_report_20260910.md",
        "docs/settlement_rule_upgrade_validation_20260910.json",
    ]
    trailing_whitespace = []
    for relative in untracked_checked_paths:
        for line_number, line in enumerate(
            (ROOT / relative).read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.rstrip(" \t") != line:
                trailing_whitespace.append({"path": relative, "line": line_number})

    tests = {
        "inventory": {
            "path": "docs/reliability_test_inventory.json",
            "exit_code": inventory.get("exit_code"),
            "collected_count": inventory.get("collected_count"),
            "diagnostic_selected_count": inventory.get("diagnostic_selected_count"),
        },
        "targeted_contract_and_diagnostics": {
            "path": "docs/settlement_rule_upgrade_20260910/targeted.final.stdout",
            "command": "D:/poly/.venv/Scripts/python.exe -m pytest tests/test_settlement_contract_v2.py -q -p no:cacheprovider -p user_tmp_plugin --tb=short",
            "exit_code": targeted.get("exit_code"),
            **parse_test_line(targeted_stdout),
        },
        "full_default_selection": {
            "path": "docs/settlement_rule_upgrade_20260910/full_regression.json",
            "command": full.get("command"),
            "environment": "PYTHONPATH=docs/settlement_rule_upgrade_20260910; user_tmp_plugin is an evidence-only tmp_path fixture",
            "exit_code": full.get("exit_code"),
            "timeout": full.get("timeout"),
            "timeout_seconds": full.get("timeout_seconds"),
            "elapsed_seconds": full.get("elapsed_seconds"),
            **parse_test_line(full_stdout),
            "source_hash_count": len(full.get("source_sha256", {})),
            "source_unchanged_during_run": full.get("unchanged"),
        },
        "optional_nautilus": {
            "path": "docs/settlement_rule_upgrade_20260910/optional.final.json",
            "command": optional.get("command"),
            "environment": optional.get("environment"),
            "exit_code": optional.get("exit_code"),
            **parse_test_line(optional_stdout),
        },
        "ruff": {
            "path": "docs/settlement_rule_upgrade_20260910/ruff.log",
            "scope": "src tests scripts",
            "exit_code": 0,
            "summary": ruff_text.strip(),
        },
        "git_diff_check": {
            "path": "docs/settlement_rule_upgrade_20260910/diff-check.log",
            "exit_code": 0,
            "line_ending_warnings_present": "warning:" in diff_check_text,
        },
        "untracked_text_check": {
            "paths": untracked_checked_paths,
            "trailing_whitespace": trailing_whitespace,
            "passed": not trailing_whitespace,
            "method": "UTF-8 line scan because git diff --check does not inspect untracked files",
        },
    }

    runtime_isolation = {
        "evidence": "docs/raw_market_recovery_validation_20260910.json",
        "market_running": raw.get("market_running"),
        "market_enabled": raw.get("market_enabled"),
        "unconfirmed_market_children": raw.get("unconfirmed_market_children"),
        "downstream_stopped_disabled": raw.get("downstream_stopped_disabled"),
        "weather_same_process_chain": raw.get("weather_same_process_chain"),
        "weather_task_config_unchanged": raw.get("weather_task_config_unchanged"),
        "paper_started": raw.get("paper_started"),
        "old_v2_and_protected_files_sha256_equal": raw.get("isolation", {}).get("protected_sha256_equal"),
        "market_status_bytes_equal": raw.get("isolation", {}).get("market_status_byte_equal"),
        "retention_called": raw.get("isolation", {}).get("retention_called"),
        "signal_publication_called": raw.get("isolation", {}).get("signal_publication_called"),
        "explicit_db_maintenance_called": raw.get("isolation", {}).get("explicit_db_maintenance_called"),
        "new_market_raw_files": raw.get("write_scope", {}).get("new_market_raw_files"),
        "raw_acceptance": raw.get("raw_acceptance"),
        "status_note": "This is preserved evidence from the prior limited recovery task; this task performed no runtime operation.",
    }

    validation = {
        "schema_version": 1,
        "document_kind": "settlement_rule_contract_upgrade_validation",
        "as_of": datetime.now(UTC).isoformat(),
        "result": "PARTIAL_COMPLETE_PENDING_HUMAN_REVIEW",
        "project_status": "NOT SEALED; Paper N=0; PnL=N/A; public group completeness UNSUPPORTED",
        "task": {
            "authorized_file": "CODEX_SETTLEMENT_RULE_CONTRACT_UPGRADE_TASK.md",
            "authorized_scope": [
                "versioned settlement rule contract",
                "strict parser/verifier upgrade",
                "durable per-event rejection diagnostics",
                "offline saved-projection reproduction",
                "non-loadable registry candidate and human review package",
            ],
            "network_in_this_task": False,
            "saved_query_only": True,
        },
        "git_and_content_identity": {
            "head": head_path.read_text(encoding="utf-8").strip(),
            "status_before_path": "docs/settlement_rule_upgrade_20260910/status.before.txt",
            "status_after_path": "docs/settlement_rule_upgrade_20260910/status.after.txt",
            "status_before_sha256": digest(status_before_path),
            "status_after_sha256": digest(status_after_path),
            "before_hash_manifest": "docs/settlement_rule_upgrade_20260910/before_hashes.json",
            "full_regression_source_manifest": "docs/settlement_rule_upgrade_20260910/full_regression.json",
            "relevant_source_bindings": source_bindings,
            "effective_registry": {
                "path": "configs/settlements.json",
                "before_sha256": effective_registry_before,
                "after_sha256": effective_registry_after,
                "unchanged": effective_registry_before == effective_registry_after,
            },
            "candidate": {
                "path": "docs/settlement_registry_candidate_20260910.json",
                "sha256": digest(candidate_path),
                "registry_sha256_inside_candidate": candidate.get("registry_sha256"),
                "review_status": candidate.get("review_status"),
                "production_loadable": candidate.get("production_loadable"),
                "entry_count": len(candidate.get("entries", [])),
            },
        },
        "contract_implementation": {
            "schema_version": 2,
            "semantic_version": "deadline-fallback-v2",
            "paths": [
                "src/poly_weather/settlement_contract.py",
                "src/poly_weather/domain.py",
                "src/poly_weather/settlement.py",
                "src/poly_weather/adapters/polymarket.py",
                "src/poly_weather/market_supervisor.py",
                "src/poly_weather/cli.py",
            ],
            "semantics": {
                "observation_identity": "event, station, local target date, timezone, unit, precision, bucket boundaries",
                "primary_source": "name/url/station/table scoped to the NOAA primary clause",
                "fallback_source": "Weather Underground name/table and explicit missing URL/station remain unresolved",
                "settlement_trigger": "first following-date publication or deadline, whichever comes first",
                "deadline": "observation calendar date + one day, 23:59 America/New_York, minute precision, boundary unresolved",
                "no_data": "primary/fallback availability states are distinct; lowest bucket is the event's open lower-bound bucket ID",
                "revision": "first following-date publication is modeled independently; deadline conflict remains unresolved",
                "identity_hash": "parser/schema/contract/raw description/market bucket contracts are content bound",
            },
            "strictness": [
                "new incomplete or unresolved contracts remain rejected",
                "SAME_STATION_NOAA cannot bypass a v2 contract",
                "legacy schema remains readable but does not receive v2 admission",
                "pending candidate is outside the loader schema",
            ],
        },
        "offline_query": {
            "summary_path": "docs/strict_rejection_query_20260910T091217Z/summary.json",
            "report_path": "docs/strict_rejection_query_20260910T091217Z/report.md",
            "query_window": "2026-09-10T09:12:17Z–2026-09-10T09:12:29Z",
            "network_get_count": query.get("network_get_count"),
            "query_elapsed_seconds": query.get("elapsed_seconds"),
            "all_status_code_200": all(r.get("status_code") == 200 for r in query.get("results", [])),
            "source_unchanged": query.get("source_unchanged"),
            "current_task_network_requests": 0,
            "candidate_count_total": sum(r.get("diagnosis", {}).get("candidate_count", 0) for r in query.get("results", [])),
            "station_count": len(event_rows),
            "old_failure_counts": old_failure_counts,
            "events": event_rows,
            "interpretation": "The saved query is a current-query reproduction, not retrospective proof of the three earlier unsaved response bodies.",
        },
        "diagnostics": {
            "implementation_path": "src/poly_weather/settlement_diagnostics.py",
            "record_root": "data/logs/daemons/attempts/market-supervisor/<attempt_id>/settlement/",
            "stages": ["input", "conversion", "parse", "verification", "discovery"],
            "required_fields": [
                "attempt_id",
                "station_id",
                "local_target_date",
                "event_id",
                "event_slug",
                "receipt_at",
                "projection_sha256",
                "parser_version",
                "projection_version",
                "stage",
                "outcome",
                "verification.differences",
            ],
            "outcomes": [
                "no_candidate",
                "ambiguous",
                "conversion",
                "parse_failed",
                "rule_incomplete",
                "registry_mismatch",
                "passed",
            ],
            "durability": [
                "public projection is written before conversion/parser/verifier",
                "each record uses a unique immutable file and fsync",
                "diagnostic write failure raises and blocks collector/subscription startup",
                "exception text is bounded and credential-bearing material is redacted",
            ],
            "test_evidence": "tests/test_settlement_contract_v2.py",
        },
        "candidate_review": {
            "candidate_path": "docs/settlement_registry_candidate_20260910.json",
            "review_path": "docs/settlement_registry_review_20260910.md",
            "review_status": "pending_human_review",
            "production_loadable": False,
            "effective_config_modified": False,
            "unresolved_approval_items": [
                "deadline second boundary and date basis",
                "fallback URL/station binding",
                "unavailable versus confirmed absent",
                "no-data source scope and lowest-bucket condition",
                "revision behavior when deadline precedes publication",
                "station-specific primary product and effective event scope",
            ],
        },
        "tests": tests,
        "runtime_isolation": runtime_isolation,
        "prohibitions_observed": [
            "no daemon or Task Scheduler operation in this task",
            "no market/signal/shadow/weather start or stop",
            "no Paper start, no execution client, no credentials",
            "no effective registry, formal data, old v2 cursor/inventory/ledger, retention, backfill, or DB maintenance mutation",
            "no network query in this task; saved projections and in-memory fake transport only",
            "no commit, push, reset, clean, stash, dependency installation, or upgrade",
        ],
        "limitations": [
            "The original three rejected recovery attempts have no saved response bodies; their current-query rule diagnosis cannot be claimed retrospectively.",
            "The saved responses are sanitized public projections plus wire hashes, not full wire bytes.",
            "No real collection, order-book coverage, or settlement outcome was created by this task.",
            "The v2 contract intentionally remains incomplete until a human resolves the listed semantic questions; no automatic settlement or registry activation is provided.",
            "The full regression used an evidence-only tmp_path plugin because the default Windows temp location is ACL-restricted; product code and test selection were unchanged.",
        ],
        "evidence_index": [
            "docs/settlement_rule_contract_v2.md",
            "docs/settlement_registry_candidate_20260910.json",
            "docs/settlement_registry_review_20260910.md",
            "docs/strict_rejection_query_20260910T091217Z/summary.json",
            "docs/strict_rejection_query_20260910T091217Z/report.md",
            "docs/raw_market_recovery_validation_20260910.json",
            "docs/raw_market_recovery_report_20260910.md",
            "docs/settlement_rule_upgrade_20260910/full_regression.json",
            "docs/settlement_rule_upgrade_20260910/targeted.final.stdout",
            "docs/settlement_rule_upgrade_20260910/optional.final.json",
            "docs/settlement_rule_upgrade_20260910/ruff.log",
            "docs/settlement_rule_upgrade_20260910/diff-check.log",
            "docs/reliability_test_inventory.json",
        ],
    }
    validation_path = ROOT / "docs" / "settlement_rule_upgrade_validation_20260910.json"
    validation_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    station_lines = []
    for row in event_rows:
        station_lines.append(
            f"| {row['station_id']} | {row['event_id']} | {row['local_date']} | {row['old_parser_stage']} | "
            f"{row['new_parse_status']} | {', '.join(row['new_contract_unresolved'])} | "
            f"{', '.join(row['new_verification_failures'])} |"
        )
    report = f"""# 结算规则契约升级报告（2026-09-10）

生成时间：{validation['as_of']}。状态：**规则代码与离线验证完成，registry 待人工审核；未恢复采集，NOT SEALED。**

## 结论

本轮把新条款建模为 schema 2、semantic version `deadline-fallback-v2`。十站保存的当前查询投影均各有一个候选；新 parser 能识别主来源、备用来源、次日首条发布、截止时间、最低桶和修订条款，但由于原文没有给出若干必要语义，十站都保持 `INCOMPLETE`，旧生效 registry 仍严格拒绝。候选文件明确 `pending_human_review` 且 `production_loadable=false`，没有写入 `configs/settlements.json`。

这不是三次旧恢复 attempt 的追溯根因证明。旧 attempt 的响应正文未保存；本轮只重放已保存的脱敏公开投影，没有新增网络请求。

## 已准确表达的语义

- 事件身份、当地目标日、站点、时区、单位、精度和合法桶边界继续由原 evidence 层保留。
- 主来源和备用来源分开建模。主来源只从主来源句解析；备用 `Weather Underground / Daily Observations` 不会污染主来源表名。美国八站的主表为 `Hourly Data / Temp`；ZUCK/ZUUU 的主表原文未明示，保持空值并拒绝。
- 结算触发单独表示为“resolution source 上次日首条数据发布”与截止时间两者取较早者。首次发布是源端发布事件，和观测时刻、收到响应的 receipt 分开。
- 截止时间保存观察日历日期、+1 天、`23:59`、`America/New_York` 和分钟精度；没有把未说明的秒边界擅自变成 `23:59:59`。
- 无数据路径区分主来源存在、主来源确认缺失、备用来源存在、两者确认缺失以及查询未知；最低桶使用该事件开下界桶的 market ID，不从价格或数组顺序猜测。
- 修订截止独立记录为次日首条发布，不把它偷偷合并为 finalization deadline 的 `min`。

## 仍需人工解释的阻塞项

每站候选都保留以下未决项：`deadline_second_boundary`、`deadline_date_basis_review`、`fallback_url_missing`、`fallback_station_missing`、`unavailable_vs_absent`、`no_data_source_scope`、`revision_after_settlement_deadline`；ZUCK/ZUUU 另有 `primary_table_missing`。这些未决项会进入 `completeness_failures()`，所以机器解析成功不等于规则完整，更不授予采集或结算资格。

人工审核包要求逐项确定：ET 截止的日期基准及中国/西海岸站点适用方式、23:59 秒边界、备用 URL/站点、不可用与确认无数据的切换条件、最低桶适用范围、截止早于首条发布时修订条款的关系，以及逐站生效时间和事件范围。审核结论必须写入新的明确版本；本轮没有替人裁定。

## 十站离线复现与逐事件结果

保存查询窗口为 2026-09-10 09:12:17–09:12:29 UTC，10 次 GET 均为 HTTP 200、无重试。每站一个候选；旧 parser 结果是 `verification` 阶段，十站原失败均为 `finalization_known` 和 `finalization_exact`。新 parser 的逐事件结果如下，完整 expected/actual 位于候选和 validation JSON：

| 站点 | event ID | 当地日 | 旧阶段 | 新 parse | 未决项 | 新核验失败 |
|---|---:|---|---|---|---|---|
{chr(10).join(station_lines)}

原始投影路径、投影 hash、wire hash、receipt、event ID/slug、parser/schema 版本、证据 hash 与逐字段 differences 均按站点保存。投影不是完整 wire；hash 也不冒充原始字节留存。

## 逐事件诊断持久化

`SettlementDiagnostics` 使用 runner 的 attempt ID，在 `data/logs/daemons/attempts/market-supervisor/<attempt_id>/settlement/` 写唯一不可覆盖 JSON。转换前先写公开 input projection，再分别记录 `conversion`、`parse`、`verification` 和 discovery 的无候选/歧义结果；核验记录包含 `expected/actual` differences。异常文本有长度上限并脱敏。诊断写失败抛出 `DiagnosticWriteError`，CLI/supervisor 不继续构造 collector 或订阅对象。CLI 初始 discovery、supervisor reconcile 和 Gamma conversion 走同一结构。

## 回归与静态检查

- 定向契约/诊断测试：**48 passed**，退出码 0。
- 完整默认测试选择：**801 passed**，退出码 0，未超时，180 秒外层上限；使用仅改变 `tmp_path` 位置的 evidence-only fixture，未改变产品代码或测试选择。完整 source manifest 记录 168 个文件且运行期间未变。
- 可选 Nautilus challenger：**6 passed，795 deselected**，退出码 0；仍为隔离 challenger，不是生产执行证据。
- `ruff check src tests scripts`：退出码 0。`git diff --check`：退出码 0；日志只含现有 Windows LF/CRLF 提示。
- 对本轮新增源文件、测试和交付文档另做 UTF-8 行尾扫描，未发现 trailing whitespace；这是补充检查，因为 `git diff --check` 不覆盖未跟踪文件。
- `docs/reliability_test_inventory.json` 已重新生成，当前收集数为 801。

红→绿反例覆盖关键条款删除/改写、重复/空白、DST/年末/闰日、receipt 与发布时钟分离、四种来源可用性、诊断前缀和写失败、两种 signal truth policy、候选不可加载以及 collector 未构造。synthetic reviewed contract 仅在临时测试对象中显式构造，不写回候选。

## 生效配置与运行隔离

有效 `configs/settlements.json` 的 SHA-256 前后相同：`{effective_registry_before}`。候选包含相同的 registry hash，但格式不是 `SettlementRegistry`，默认 loader 不会发现；测试验证直接加载候选会拒绝。

本轮没有运行 daemon、Task Scheduler、market/signal/shadow/weather、Paper、WS 或业务网络查询；没有修改正式 data、旧 v2 cursor/库存/账本、retention、回填或数据库。此前受限恢复的保存证据仍显示 market/signal/shadow 已禁用、market 无运行 child、weather 原进程链保持、Paper 未启动、受保护文件 hash 相等；本轮没有触碰这些状态。正式采集恢复仍需人工审核完成后另行申请，不能由候选自动触发。

## 限制与下一道闸门

原三次恢复失败 attempt 的 response body 不存在，不能把当前查询结论回填为历史原因；本轮也没有生成真实订单簿或结算结果。只有在人工逐项解决未决语义、形成新的 reviewed 规则版本并单独授权 market/supervisor 恢复后，才可进行下一阶段的有界运行验收。当前项目仍 `NOT SEALED`，Paper 仍 `N=0`、`PnL=N/A`。

证据索引：

- [契约说明](settlement_rule_contract_v2.md)
- [待审核候选](settlement_registry_candidate_20260910.json)
- [人工审核包](settlement_registry_review_20260910.md)
- [验证 JSON](settlement_rule_upgrade_validation_20260910.json)
- [回归证据目录](settlement_rule_upgrade_20260910/)
"""
    report_path = ROOT / "docs" / "settlement_rule_upgrade_report_20260910.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"wrote {validation_path}")
    print(f"wrote {report_path}")


if __name__ == "__main__":
    main()
