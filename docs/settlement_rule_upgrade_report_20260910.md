# 结算规则契约升级报告（2026-09-10）

生成时间：2026-09-10T10:21:00.321597+00:00。状态：**规则代码与离线验证完成，registry 待人工审核；未恢复采集，NOT SEALED。**

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
| KLGA | 987074 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KORD | 987248 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KLAX | 987929 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KMIA | 987247 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KATL | 987076 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KDAL | 987075 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KHOU | 987250 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| KSEA | 987928 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| ZUCK | 986731 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline, primary_table_missing | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |
| ZUUU | 986734 | 2026-09-10 | verification | incomplete | deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline, primary_table_missing | parse_complete, finalization_exact, rule_reviewed, rule_resolved, rule_deadline_exact, rule_fallback_exact, rule_fallback_condition_exact, rule_lowest_bucket_market_id_exact, rule_no_data_action_exact, rule_no_data_condition_exact, rule_primary_exact, rule_publication_date_offset_days_exact, rule_publication_source_exact, rule_revision_source_exact, rule_revision_trigger_exact, rule_schema_version_exact, rule_semantic_version_exact, rule_settlement_trigger_exact, rule_unresolved_exact |

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

有效 `configs/settlements.json` 的 SHA-256 前后相同：`c22d8ab8e0ce05f65ef4c5a526c698b14bf2e4421c8c6d8d9f962472e19f70dd`。候选包含相同的 registry hash，但格式不是 `SettlementRegistry`，默认 loader 不会发现；测试验证直接加载候选会拒绝。

本轮没有运行 daemon、Task Scheduler、market/signal/shadow/weather、Paper、WS 或业务网络查询；没有修改正式 data、旧 v2 cursor/库存/账本、retention、回填或数据库。此前受限恢复的保存证据仍显示 market/signal/shadow 已禁用、market 无运行 child、weather 原进程链保持、Paper 未启动、受保护文件 hash 相等；本轮没有触碰这些状态。正式采集恢复仍需人工审核完成后另行申请，不能由候选自动触发。

## 限制与下一道闸门

原三次恢复失败 attempt 的 response body 不存在，不能把当前查询结论回填为历史原因；本轮也没有生成真实订单簿或结算结果。只有在人工逐项解决未决语义、形成新的 reviewed 规则版本并单独授权 market/supervisor 恢复后，才可进行下一阶段的有界运行验收。当前项目仍 `NOT SEALED`，Paper 仍 `N=0`、`PnL=N/A`。

证据索引：

- [契约说明](settlement_rule_contract_v2.md)
- [待审核候选](settlement_registry_candidate_20260910.json)
- [人工审核包](settlement_registry_review_20260910.md)
- [验证 JSON](settlement_rule_upgrade_validation_20260910.json)
- [回归证据目录](settlement_rule_upgrade_20260910/)
