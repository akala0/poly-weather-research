# Codex 任务：未决规则证据审查与采集准入分层设计

日期：2026-09-11。状态：待执行；仅审查与设计，不授权实现或运维。

## 1. 本轮要作出的决定

回答两个问题：

1. schema 2 的每个 unresolved 项，究竟是源端事实缺失、真实语义冲突、本地解析不足，还是对某项能力不必要的额外要求？
2. 能否在保持规则 UNKNOWN、信号/Paper 拒绝不变的前提下，安全地识别 token 并隔离留存原始公开盘口？若能，最小设计是什么；若不能，具体障碍是什么？

不得预设“全部都可解除”或“全部都必须阻断采集”。本轮终点是一份有源码路径、逐站证据和反例设计的决策材料，不是更多待审核标签或直接启动命令。

## 2. 基线与权限

既有报告：结算契约升级后定向 48 passed、完整 801 passed、Nautilus 6 passed；十站真实投影仍 INCOMPLETE，生效 registry 未变。该计数是前轮验证，不冒充本轮重跑或独立审查结论。

上轮运维终态为 market/signal/shadow 禁用、weather 原进程保留。此处为历史交接状态，未经本轮查询不声称当前状态。原三次 attempt 响应缺失；2026-09-10 的十站查询仅证明当时规则，不能证明今天全部事件一致。

先完整阅读 `AGENTS.md`、`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`，再读：

- `docs/settlement_rule_contract_v2.md`、`docs/settlement_registry_review_20260910.md`、对应 candidate JSON；
- `docs/settlement_rule_upgrade_report_20260910.md` 与 validation JSON；
- `docs/strict_rejection_query_20260910T091217Z/report.md`、summary 及逐站 response/result；
- `docs/raw_market_recovery_report_20260910.md`、`docs/collection_recovery_plan_20260910.md`；
- `docs/reliability_consumer_audit.md`、health/archive/receipt 契约；
- 实际 domain、settlement/settlement_contract、diagnostics、Gamma adapter、CLI、market supervisor/stream、下游消费者和 runner。

允许：只读源码/既有证据、官方公开规则文档与官方源码；在临时目录或内存运行现有纯读取/解析函数进行有界诊断；创建第 7 节三份交付物。

禁止：修改产品代码、测试、规则/registry 候选、生效配置、现有规范/报告/指纹；业务 API probe、WS 或额外批量查询；联系平台、发消息或提交 issue；认证/凭据/执行接口；daemon/Task Scheduler 操作；正式 data 变更；系统/网络/依赖修改；commit/push、reset/clean/stash；启动 Paper。

本轮官方文档查阅不等于业务数据请求授权。若缺少当前事件响应或需平台解释，提交具体证据需求和最小请求计划，等待单独授权；不要复用上次已用完的查询额度。

保留脏工作树和 `.claude/`；记录 HEAD 与 relevant tracked/untracked 指纹。不得使用已有运维证据脚本作为可重复执行接口。

## 3. E01：逐项查清 unresolved 的依据

至少覆盖当前八类项：deadline_second_boundary、deadline_date_basis_review、fallback_url_missing、fallback_station_missing、unavailable_vs_absent、no_data_source_scope、revision_after_settlement_deadline，以及中国站 primary_table_missing。

逐站填写以下表格，不只复制 parser 的未决列表：

| 项目 | 原文/证据定位 | 代码为何标 UNKNOWN | 必要能力及失败反例 | 判定 | 最小补证或变更建议 |
| --- | --- | --- | --- | --- | --- |

判定至少区分：源事实确实缺失、原文冲突、解析器未提取已有信息、本地要求可能过强、现有证据已足够但仅适用于指定能力。每个判断必须有理由；人工审核状态本身不是源事实。

重点要求：

1. 分钟精度：保留 23:59 原精度，不凭空补 23:59:59。分析哪些操作真的需要秒级单点，哪些可用保守时间区间或完全不依赖该时间。区间处理仅提出方案，不修改真实规则。不同合理解释导致相同结果，也只能证明该操作对歧义不敏感，不能宣称规则已澄清。
2. 日期基准：区分观察日的日历日期标签、站点当地日期、ET 时区和 UTC instant。用中国站、西海岸、DST/跨日具体时间例说明歧义是否真实存在；ET 使用 IANA 时区，不固定 UTC−5。
3. 备用 URL/站点：区分源条款要求的权威绑定与本地获取数据需要的 URL。若可从可信关联证明绑定，列出完整证据链；若只有名称相近或 registry 猜测，不认定充分。源原文不一定必须包含实现所需的全部 URL，但本地补充必须独立标注，不能伪称源文本提供。
4. unavailable 与 absent：网络失败、未发布、页面无值、源声明无数据分别处理。明确谁有权触发 fallback/最低桶，而非自动用本地请求失败判断。
5. 修订冲突：独立保留 finalization/revision 条款，列出 deadline 早于首条发布等情景；不通过 min 合并消除冲突。可作为需要平台说明的事项，不要求用户凭直觉裁定。
6. 主表缺失：逐站分析源 URL/站点/产品能否唯一识别数据，不能用美国八站表名套中国站；缺具体展示表名不自动等于 token 身份不可信。

对于每项，明确“收集证据”“生成诊断特征”“产生策略信号”“Paper 建仓/退出”“自动结算/官方结果核验”分别是否依赖它。用户策略为价差交易，但不能据此忽略合约身份与风险退出证据。

## 4. E02：审查现有调用链与错误耦合

沿实际路径画出最小文字调用图，并列每个闸门的输入、输出、调用方：

`公开发现 → event/token 识别 → settlement 解析/核验 → 订阅 → raw/checkpoint/DB → status/active set → signal/v2/Paper/研究`。

回答：

- 初始 CLI 与 supervisor 后续 reconcile 是否以同一完整结算核验阻断订阅？
- token/condition/market/event/outcome 映射、目标日/站点与结算细节能否独立验证？不能验证的身份冲突仍拒绝订阅，不建议任意 token 采集。
- 各 active set、status、signal config、共享 DB 表被谁读取？是否存在看到“订阅成功”就当规则 verified 的路径？
- 旧消费者是否忽略新 UNKNOWN 字段、默认 missing 为正常，或直接枚举所有 raw 目录？
- 本地序列、原始 receipt、质量窗口、关闭/退订、重启恢复如何传播？原始留存不等于 book 完整，更不等于 tape 组闭合。

尤其检查 signal、v2、QUIET、complement、Paper、CLI/research 的目录枚举和 DB 读取，不只检查 Paper 一个入口。结果应包含实际函数/行号，不用理想架构代替现状。

## 5. D01：能力分层设计（仅提案）

最少对比：保持现状；独立隔离的原始采集层；共享存储但严格标签隔离。选一个最小可验证方案并说明不选其它方案的具体风险。不要新增通用权限框架或重写采集栈。

提案至少区分以下资格，名称不是已存在的生产字段：

- 可识别并订阅：event/market/condition/token/outcome 关系、站点/日期范围、唯一 writer 和资源边界有依据。
- 可留存原始证据：保留原始来源、receipt/run/sequence、规则投影与版本、UNKNOWN/reasons；不宣称已核验结算。
- 可重建健康 L2：完整快照、增量连续性、token 作用域和质量边界独立合格。
- 可用于研究/策略/Paper：各消费者原有规则/天气/季节/完整性/账户闸门仍生效，不因前面几层通过而继承。

设计硬边界：

1. 新原始采集准入不能调用放宽后的正式 verifier，不能伪造 VERIFIED 或删除 unresolved；可以提出独立最小身份核验契约，但改变准入需另行实现授权。
2. 默认关闭新路径，必须显式启用；本轮仅设计。生效 registry、正式状态语义、旧路径不静默改变。
3. 单列 collection membership 与 strategy-approved membership。不得将未核验事件写进被下游当作 verified 的 active set，也不得发布 signal 配置。
4. 若旧消费者不能可靠识别拒绝标签，优先物理隔离目录/DB/命名空间。仅加一个布尔字段不是隔离证明；路径及 schema 方案必须有所有读取方审计。
5. 规则快照及首次可见时间不可变留存；后续规则获批只从批准的生效边界起提供资格，不追认此前 forward fills/PnL。事后研究另标历史口径。
6. 重启不能丢失 UNKNOWN/规则版本，旧 v2 无 hash cursor 不迁移；原始新 run 不能消除旧经济库存/订单。
7. 保留原始 token-native 深度与质量标记，不以 last/mid/1-p/对侧 token 或最终结算值补簿。公共 trade group completeness 继续 UNSUPPORTED。
8. retention、显式 DB 维护、下游自动级联、Paper 均不因新模式开放。weather 原始采集仍独立，不为该设计停采。
9. 必须保留资源上限、唯一 writer、attempt 留证和 fail-closed 行为；“可归档”不是无限订阅所有市场。

如果任何消费者旁路或身份歧义无法安全隔离，结论为该方案暂不可实施，并给出最小必要前置修复。不得以“以后会补测试”作为当前安全证明。

## 6. D02：下一阶段最小实现与验收设计

本轮不实施，但提交可交接的范围、文件/函数、契约变化、兼容性、风险及验证顺序。重点验收场景：

1. 身份可靠但 settlement unresolved：仅进入隔离原始归档，signal/v2/QUIET/complement/Paper 均不能获得新准入。
2. token/market/outcome 不一致、重复映射或发现歧义：拒绝订阅并留具体原因。
3. 规则条款变更/丢失、UNKNOWN 降级：不丢证、不升级资格，重启后状态一致。
4. 身份合格不意味着 L2 完整；缺初始 full book、gap、错 token、陈旧数据各自标记。
5. 新隔离数据不能被旧 raw glob/共享 DB/report 意外消费；覆盖所有实际消费者。
6. 后续人工批准/规则补证不改写过去的可见前缀和评分，不重新消费旧交易。
7. 断电/写失败/短写/重启边界、attempt/唯一 writer 与 cursor 身份保持；只在未来临时 fixture 测试，不操作正式链路。
8. 跨日/DST、跨站、临界分钟/备用源冲突等情景证明资格按能力划分，而非偷偷解除规则 UNKNOWN。

验收要比较实际 raw/DB/status/active set/ledger 输出，不能只断言一个 flag 为 false。标明哪些是本轮现有函数诊断、哪些只是未来测试设计；不得宣称未运行测试已通过。

下一阶段建议必须只选一个最小实现主题；单独列 registry 审批、公开补证请求、部署/运维恢复各自授权，不合并成大任务。

## 7. 交付与完成标准

仅新增：

1. `docs/unresolved_rule_evidence_review_20260911.md`：八类未决项逐站证据/能力矩阵、确定与未知结论、必要平台问题。
2. `docs/collection_admission_layering_design_20260911.md`：现有调用图、方案对比、推荐方案、所有消费者隔离、最小实现及反例验收包。
3. `docs/rule_admission_review_evidence_20260911.json`：as-of、HEAD/候选指纹、证据 ID、来源引用、实际诊断命令/退出码、检查范围、未验证事项与操作声明。

存在同名文件时先阅读，不覆盖已有历史。官方证据记录 URL/访问时间/适用版本与准确支持的结论；本地投影记录路径/hash/字符定位，不能称完整 wire。未读到或访问失败写明，不据此证明源端不支持。

结束时核验 JSON 可解析、证据 ID 唯一、引用路径和文档格式；对新增文件单独检查，不能依赖只覆盖 tracked 文件的 diff。对比开始/结束 Git 状态，保留所有既存文件；data Git 空输出不等于 ignored 数据全量审计。

本轮无需重跑完整 pytest；引用 801 passed 注明历史证据。若运行有界纯解析诊断，单列结果，不能当作实现验收。

最终先回答：哪些未决项确实阻断策略、哪些不必阻断隔离采集、哪些本地要求需重新设计；然后明确能否提出安全的最小分层方案及其前置条件。找不到安全方案也可完成审查，但不能恢复采集。

任务完成后停止：不改代码/registry，不发业务查询、不联系平台，不启用任务或启动 Paper，不 commit/push。项目仍 NOT SEALED；规则事实不能靠人工批准补造。
