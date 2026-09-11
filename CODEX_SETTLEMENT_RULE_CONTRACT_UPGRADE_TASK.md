# Codex 任务：结算规则契约升级与逐事件拒绝留证

日期：2026-09-10。状态：待实施。本文件不是配置批准、运维恢复或 Paper 启动授权。

## 1. 目标及已知证据

修复现有解析器与规则模型无法完整表达新结算条款的问题，并补齐逐事件拒绝证据。先完成版本化契约和离线验证，再交付待人工审核的 registry 候选；不得直接让正式配置通过。

依据 `docs/strict_rejection_query_20260910T091217Z/report.md`：2026-09-10 09:12:17–09:12:29 UTC 的 10 次 GET 各发现一个候选，十站均无解析异常，现有解析状态 COMPLETE，但核验均失败于 `finalization_known`、`finalization_exact`。解析器返回 unknown，registry 预期 first_next_day_observation。

这是该查询时点的可复现不匹配，不是原三次恢复 attempt 的根因追溯证明。此前响应未保存，不可补造。

新条款包含：次日首条数据或截止时间取较早者、截止时缺 NOAA 数据后的备用来源、仍无数据的最低桶处置、修订截止。**这些是不同语义，不得只加正则并映射回旧枚举。**

原始查询保存的是公开字段投影及 wire hash，不是完整 wire 字节。已有原响应/投影解析一致性记录可支持现有读取字段的复现；新增解析若依赖投影未保留字段，应报告证据缺失，不能制造该字段。

## 2. 必读与权限

完整阅读 `AGENTS.md`、`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`，再阅读：

- 当前诊断报告、查询目录下的 summary 和十站 response/result；
- `docs/raw_market_recovery_report_20260910.md` 与对应 validation；
- `src/poly_weather/domain.py`、`settlement.py`、`config.py`、相关 CLI/discovery 调用；
- `configs/settlements.json` 及现有 settlement/config/CLI/signal 测试；
- `CURRENT_CONCLUSIONS.md`、相关 source/health 契约和第三方归属。

开始时记录 HEAD、全部 dirty/untracked 状态，针对本任务文件建立内容指纹。保留 `.claude/`、既有修改、历史报告与指纹；每次编辑前重读，发现并行漂移先协调，不覆盖。

本任务允许修改：规则 domain/parser/verifier、必要配置读取兼容代码、逐事件诊断入口、相关测试和本轮文档；允许使用全新临时目录和内存 fake transport 验证真实转换链。

禁止：

- 修改正式 `configs/settlements.json`、生产模板/默认策略/阈值，自动切换 registry 或把候选标成已人工核验。
- 启用/启动/停止现有 market、signal、shadow、weather 或 Task Scheduler；不运行正式 engine/collector/恢复脚本。
- 修改正式 data、旧 v2 cursor/库存/账本、旧成绩或历史规则身份；不 retention、回填或数据库维护。
- 网络业务查询、WS、凭据/钱包/签名/订单接口；本轮使用已保存证据，缺失另列有界查询申请，不继承上次 10 次 GET 授权。
- 更改网络/代理/系统、安装升级依赖、commit/push、reset/clean/stash。

market、signal/shadow 的禁用状态及 weather 原进程均保持不动。Nautilus 仍为隔离 challenger。项目 NOT SEALED，Paper N=0、PnL=N/A，不启动 Paper。

## 3. C01：先定义契约，不先改正则

在 `docs/settlement_rule_contract_v2.md` 中逐站建立“原条款定位 → 结构化字段 → 时间/来源作用域 → 核验方式 → 歧义”表。命名可按现有架构确定，不凭本文字段示意臆造源端接口。

契约必须分别表达：

| 语义 | 必须保留的内容 |
| --- | --- |
| 观察日与站点 | event identity、当地目标日、station、观察时区、单位、精度、桶边界 |
| 主来源 | 名称、URL/站点绑定、使用的数据表/产品 |
| 备用来源 | 名称、URL/站点/表格及切换前提；不能污染主来源 observation_table |
| 结算触发 | following-date first publication 与 deadline 的较早者；所依赖来源和数据日期 |
| 截止时间 | 日期基准、相对天数、当地钟点、IANA timezone，不能只存一个字符串标签 |
| 无数据处置 | 主来源缺失、备用源缺失及最终最低桶处置之间的有序条件 |
| 修订截止 | 独立表达 first-next-day-publication 条款，不能简化为 ignores_late_revisions=true |
| 证据与版本 | parser/schema/规则语义版本、条款位置、事件范围、内容 hash、审核状态 |

特别要求：

1. ET 使用 `America/New_York`，不得固定 UTC−5。观察日属于哪个日期基准、次日如何计算，须由原条款支持；中国站和美国西海岸不能直接按服务器日期平移。
2. “11:59 PM”未说明秒级边界时，保留原精度和边界不确定性，不擅自改成 23:59:59、24:00 或包含整分钟。可表达不确定性，但不授予执行资格。
3. “次日首条已发布”是发布事件，不是我们收到数据，也不是值的观测时间。观察时间、源端发布、首次本地 receipt 分开。
4. 结算截止早于次日首条发布时，修订条款如何适用可能存在歧义；不能直接把修订截止也写成 min。保留两项字面语义、冲突和待审核结论；不擅自裁定真实结算。
5. 缺主来源、切备用源、备用源仍缺失三个条件不能用一个 no_data bool 合并。source unavailable、HTTP failure、数据确实不存在也不是同一件事。
6. 最低桶由该 event 合法桶顺序/边界确定，不按数组首元素、YES/NO 价格或猜测温度选择。本轮仅建模条款，不实现自动结算或持仓平仓。
7. 不要求十站规则完全相同；逐站、单位、来源和文本核对，不能以一站模板覆盖全部。

对有明确证据的字段继续实现；对歧义字段使用显式 unresolved/review-required，使相关完整性/核验保持拒绝。不得为让十站全绿而选择有利解释。

## 4. C02：版本化解析与严格核验

沿用现有 domain/config/parser 框架，最小增量建模，不引入通用规则引擎或全项目重构。

- 主备来源按条款作用域解析，不全局搜索 Daily Observations 后赋给主来源。
- 识别新条款各个子句及关系；缺失、重复矛盾、多个无法唯一解释的匹配都显式失败。
- `parse_status=COMPLETE` 必须覆盖该规则版本全部必要语义，不能在 finalization unknown 或新关键条款未解析时仍报告完整。
- `verify_settlement_evidence` 逐字段比较人工审核预期；来源层级、表格、时区、deadline、fallback、无数据处置、修订截止均进入检查，输出具名 reasons 与 expected/actual。
- 审查 `verify_signal_contract` 的 `SAME_STATION_NOAA` 路径及其它消费者。观测来源策略可以不同，但不能绕过结算规则的新语义校验或把 known 枚举当完整匹配。保留策略意图，不放宽闸门。
- 新语义须有独立版本/身份；不得映射为旧 `FIRST_NEXT_DAY_OBSERVATION` 后继续使用原 hash。证据内容变化应导致相应规则身份变化。
- 旧数据保持可读取或明确标记历史不受支持；读兼容不等于当前准入。缺新字段不能自动补成新规则、verified 或 realtime。
- 不用 registry 预期反向补齐源文本中缺失的结算语义，防止“用答案验证答案”。逐项说明现有 registry-assisted 解析边界。
- 定位所有序列化、历史恢复、报告、校验缓存调用方；版本变化不能让旧 verified 缓存给新规则授权。

新规则与旧正式 registry 不匹配时继续拒绝，是本轮预期行为。代码完成不要求正式 CLI 启动成功。

## 5. C03：逐事件诊断在核验结果前留存

修复初始 discovery 只保留 rejected_count 的证据缺口，并检查循环 discovery 是否共用同一诊断结构。

每个候选记录：attempt ID、站点、当地目标日、event ID/slug、源证据投影 hash、观察 receipt、转换/解析/核验阶段、parser/规则版本、每个 failure 的 expected/actual、脱敏异常类型/原因。

必须区分：无候选、候选歧义、转换失败、解析失败、规则不完整、与 registry 不匹配、完整通过。总计与逐事件结果应可对账，不能吞掉异常后仅加 rejected_count。

复用现有 attempt 日志/manifest；记录顺序应保证处理候选时先留必要输入证据，再做解析核验。诊断写失败须可见且不得继续创建 market child 业务采集对象，不能静默缺证后声称可诊断。

只保存必要公开字段投影及来源引用，不复制敏感 header、凭据或无限响应正文。字段 hash 不等于保存原始字节；明确投影版本与覆盖范围。测试只能写临时目录；不通过正式启动验证留证路径。

如果必须修改 runner 才能可靠关联诊断，限定于该证据契约，不改启动授权、重试上限、ownership latch、retention 或下游隔离。

## 6. C04：registry 候选与人工审核包

仅新增 `docs/settlement_registry_candidate_20260910.json` 与 `docs/settlement_registry_review_20260910.md`，不修改任何生效配置。

- 候选逐站记录旧值、新提议、条款证据 ID、语义版本、事件/查询日期范围和未决歧义。
- 候选顶层和逐条明确 pending human review，不带会被生产直接当作 VERIFIED 的默认状态；默认配置读取路径不得自动发现或加载此文件。
- 将机器验证的“字段解析准确”与人工批准的“该规则作为预期配置生效”分开。
- 测试如需要 reviewed spec，仅在 tmp_path/内存显式构造并标注测试用途，不把测试审批写回候选。
- 文本换版不重写历史版本、旧前向样本或现有账本；未来生效点与允许事件范围须人工确认。
- 审核包列出最低必要批准项，特别是 deadline 日期/精度、备用源条件、最低桶、revision 与 finalization 的冲突；不能用一条“同意更新”掩盖未决语义。

## 7. 修复前反例及最小验收矩阵

先保存当前代码反例，再实现修复；不能只改测试期望迎合结果。使用已保存十站投影，经现有 Gamma 转换→parser→verifier→CLI 初始化诊断的实际链路，外部 transport 为内存 fixture，采集对象必须断言未构造。

至少覆盖：

1. 十站当前投影复现旧 finalization 两项失败；记录每站原结果，不将合成输入叫真实 wire。
2. 新 parser 正确保留主备来源、deadline、fallback、无数据规则、修订条款；主来源不被备用表名污染。
3. 新规则对旧 registry 仍拒绝；pending 候选不能获得生产准入。
4. 明确无歧义的测试 reviewed spec 完整匹配通过；未决歧义仍拒绝，不强求真实十站全部通过。
5. 逐项删除/改写关键条款，改 deadline/时区/日期基准、交换主备来源、移除最低桶或修订条款均可检测；证据减少不能提升资格。
6. 同义格式/大小写/空白变化保持语义；不同语义不能被宽松 regex 合并。
7. 次日首条早于、晚于、等于截止；首条缺失；边界精度不充分保持 UNKNOWN。
8. DST 冬夏偏移、月末/年末/闰日、中国站与美国站日期差异；使用 timezone-aware 时间，不读真实 now 影响历史 fixture。
9. 主来源有数据、主来源无数据且备用可用、两者均缺、来源无法查询四种情况分别建模；无数据结算不成为模型成交价。
10. 结算触发与修订截止独立；晚到 receipt/修订/未来规则追加不改变过去已确认规则版本。
11. 旧 schema 加载、旧规则回归、unknown 拒绝、新规则缓存/证据 hash 失效；不迁移正式档案。
12. 两种 signal truth policy 均不绕过新契约；已有 CLI、supervisor、研究消费者采用一致判定。
13. 每个拒绝都有 attempt/event 对应证据，转换/解析异常与核验失败分开；部分成功后异常、诊断写失败均不丢失已确认前缀或误记成功。
14. 正式配置、raw data、旧 v2 状态不变；没有网络请求、collector/daemon 启动或执行客户端。

矩阵逐行写需求、代码路径、测试名、实际断言、真实投影/合成输入、结果和限制。普通 regex 测试不能替代完整生产转换链。

## 8. 回归与交付

定向测试、完整默认套件、可选套件分别报告。沿用既有 `.venv`，完整 pytest 设置 180 秒外层上限和堆栈输出；只处理本次测试进程树，不杀现有 Python。超时或 skipped 如实列明；此前 753 passed 不能替代新候选回归。

Ruff 检查候选源码/tests/scripts；PowerShell 若修改则解析及相关故障测试；按现有生成器更新测试 inventory，不能手改计数或改写旧指纹。用候选内容 hash 绑定本轮结果。文档/JSON 单独校验；`git diff --check` 不覆盖未跟踪文件，应另查。

交付文件：

- `docs/settlement_rule_contract_v2.md`；
- `docs/settlement_registry_candidate_20260910.json`；
- `docs/settlement_registry_review_20260910.md`；
- `docs/settlement_rule_upgrade_report_20260910.md`；
- `docs/settlement_rule_upgrade_validation_20260910.json`（含测试结果、候选指纹和证据索引）。

最终报告先说明：哪些语义已准确表达、哪些仍需人工解释、正式旧 registry 是否仍拒绝、新候选是否尚未批准。再列红→绿证据、回归结果、兼容性、实际修改路径和禁止操作声明。

状态终点：**规则代码与离线验证完成，registry 待人工审核；未恢复采集，NOT SEALED。** 如歧义未解决，写局部完成和具体阻塞。不得自动更新 registry、请求新数据、启用任务或进入下一轮恢复。
