# 未决规则证据审查（2026-09-11）

本轮结论：十站旧投影仍不通过完整结算核验，但八类未决项不应一律成为隔离盘口留存的必要条件。备用源绑定/切换、无数据范围、截止与修订冲突，确实影响依赖它们的策略与结算判断；分钟秒界只影响边界敏感操作。观察日期标签已可从文本提取，文字 URL 和固定展示表名则混入了本地实现要求。**不修改 unresolved，不更新 registry，不授予信号/Paper 或恢复资格。**

范围是当前脏树源码、已保存的 2026-09-10 投影及本轮官方文档阅读。原三次失败 attempt 响应缺失；今天事件规则、进程状态未查询。旧 48/801/6 passed 仅是前轮证据，本轮仅运行内存纯解析/转换诊断。项目仍 NOT SEALED；Paper 正式 N=0、PnL=N/A。

## 证据口径

机器证据见 [审查索引](D:/poly/docs/rule_admission_review_evidence_20260911.json)（Windows 正确路径链接以下采用 D:/）。候选 [settlement_registry_candidate_20260910.json](D:/poly/docs/settlement_registry_candidate_20260910.json) 的 SHA-256 为 `9fbd948400c20f3a4bce268e24136dcb90998e8f01e65ca92f7fc2afd242e4c2`；生效配置 SHA-256 为 `c22d8ab8e0ce05f65ef4c5a526c698b14bf2e4421c8c6d8d9f962472e19f70dd`。

下表 S-* 均使用自己的 response.json，字符区间针对 JSON 解码后的 `events[0].description`，0 起始、右开区间，不是文件字节偏移。逐项 R-* 保存文本、路径、hash 关联、判定及建议。50 个已有 clause 区间逐一与原文本相等；新增 primary 区间从 description 起点到备用句之前。保存内容是公开字段投影，原 wire 字节未保存，wire hash 不等于 wire 本体。

| 证据 | event ID / 观察日 | 主站/产品 | 本次离线检查 |
| --- | --- | --- | --- |
| [S-KLGA](D:/poly/docs/strict_rejection_query_20260910T091217Z/KLGA.response.json) | 987074 / 2026-09-10 | KLGA / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KORD](D:/poly/docs/strict_rejection_query_20260910T091217Z/KORD.response.json) | 987248 / 2026-09-10 | KORD / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KLAX](D:/poly/docs/strict_rejection_query_20260910T091217Z/KLAX.response.json) | 987929 / 2026-09-10 | KLAX / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KMIA](D:/poly/docs/strict_rejection_query_20260910T091217Z/KMIA.response.json) | 987247 / 2026-09-10 | KMIA / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KATL](D:/poly/docs/strict_rejection_query_20260910T091217Z/KATL.response.json) | 987076 / 2026-09-10 | KATL / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KDAL](D:/poly/docs/strict_rejection_query_20260910T091217Z/KDAL.response.json) | 987075 / 2026-09-10 | KDAL / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KHOU](D:/poly/docs/strict_rejection_query_20260910T091217Z/KHOU.response.json) | 987250 / 2026-09-10 | KHOU / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-KSEA](D:/poly/docs/strict_rejection_query_20260910T091217Z/KSEA.response.json) | 987928 / 2026-09-10 | KSEA / Hourly Data / Temp | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-ZUCK](D:/poly/docs/strict_rejection_query_20260910T091217Z/ZUCK.response.json) | 986731 / 2026-09-10 | ZUCK / Temp 列、表未明示 | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |
| [S-ZUUU](D:/poly/docs/strict_rejection_query_20260910T091217Z/ZUUU.response.json) | 986734 / 2026-09-10 | ZUUU / Temp 列、表未明示 | 11 markets、22 tokens；站内唯一、二元 YES/NO 对齐、condition 非空唯一；完整核验拒绝 |

这些检查只验证投影内部关系；不证明链上推导正确、当前可订阅、跨事件完整覆盖、WS 连续或可交易。诊断构造 EventSnapshot 时使用固定合成 receipt，绝不将它写成真实前向首次可见时间。

## 能力依赖矩阵

“可”只表示该项未知本身不阻断，仍须独立身份/receipt/质量等资格；不是当前运行授权。“现拒”指现有完整 verifier/策略政策保持不变。诊断温度与纯盘口特征分列在格内。

| 未决项 | 收集证据/原始盘口 | 生成诊断特征 | 策略信号 | Paper 建仓 | Paper 退出 | 自动结算/官方结果核验 |
| --- | --- | --- | --- | --- | --- | --- |
| 秒边界 | 可 | 盘口可；截止分钟内顺序 UNKNOWN | 现拒；边界敏感策略依赖，远离边界仅可证明不敏感 | 现拒 | 已有资格路径保留；精确截止驱动退出依赖秒界 | 临界判定依赖；记录官方结果原事实可 |
| 日期基准 | 可，站点/目标日身份冲突仍拒 | 盘口可；日高/距截止依赖正确日标签和产品日界 | 现拒；日级信号依赖 | 现拒 | 日界/到期退出依赖；同 token 风险减仓概念上不必重算结算 | 依赖日标签，不固定 ET 偏移 |
| 备用 URL | 可 | 盘口可；备用数据取得需可信定位 | 现拒；用备用特征者依赖 | 现拒 | 不借新隔离数据升级旧仓；既有退出资格不变 | 自动取备用需定位；不必要求 URL 写在市场原句 |
| 备用站点 | 可，主身份须明确 | 盘口可；备用温度归属依赖绑定 | 现拒；用备用者依赖 | 现拒 | 同 token 身份不可省；旧仓不迁移 | 备用结算依赖绑定 |
| unavailable/absent | 可，错误本身也是证据 | 盘口可；只能诊断可用性类别，不能推定切换 | 现拒；切换相关依赖 | 现拒 | 源失效可以触发保守保护，但不能伪造温度/结果/成交 | 切换/最低桶判断必需 |
| no_data 来源范围 | 可 | 盘口可；no-data winner 特征 UNKNOWN | 现拒；涉及无数据收益路径者依赖 | 现拒 | 不把最低桶当退出价格；真实 bid 必须独立合格 | 最低桶自动判定必需 |
| revision/deadline | 可 | 盘口可；数据“不可修订”标签未知 | 现拒；依赖温度终局者受影响 | 现拒 | 已有同 token 风险退出概念可独立；终局驱动退出依赖 | deadline 先到分支必需 |
| 中国主表 | 可，NOAA site/机场/事件身份可另验 | 盘口可；用于日高的采样/日界/QC 特征需补证 | 现拒；中国站既有季节/精度门也不放宽 | 现拒 | 不套美国表，不新增旧仓数据入口 | 温度独立重算依赖具体产品；原结果事实可留存 |

Paper 退出不是“无条件获准”：当前 `_risk_exit_eligible` 还调用 `snapshot.health_ok`，其中包含 settlement_verified、天气/季节/完整簿等现有门。本表区分能力的逻辑必要性与当前实现，**不提议本主题修改 Paper 退出政策**。将来若讨论风险退出，须独立证明同 token 新鲜真实 bid、身份、质量区间、库存份额及连续性，不能以新采集 run 清空库存或借默认值通过。

价差策略可在结算前平仓，因此不是每个未决细节都必然影响每笔价格操作；但结算源切换/修订会影响温度解释、尾部跳空和市场可交易时间。不能以“只赚价差”解除身份和风险闸门。现阶段不实现基于“歧义不敏感”的策略豁免。

## 八类判断的理由和可反驳情景

1. **分钟精度**：文本给 23:59，不给精确秒/包含关系。建议仅在分析中保留可能边界区间，例如从 23:59:00 到下一分钟起点的保守包络，并在两端也不擅判等号；它不是新结算条款。23:59:30 的首条发布无法唯一排序；23:58:00 在所列分钟解释下均早于截止，只证明该比较不敏感。当前 publication_deadline_order 在 boundary 未决时一律 UNKNOWN，是偏保守实现；无需为了存盘口修改它。

2. **日期标签与 instant**：原文“observation date”结合 title/slug 明示 2026-09-10，合理标签解释为 D+1 的 ET 23:59。代码已经提取这层，不应要求人工创造另一日期事实。仍需区分 NOAA 页面按哪个站点当地日取“all times on this day”，registry 时区只是本地关联值，不能冒充源端产品日界说明。若平台另有相反上下文，才形成真正冲突。所有站一律列 date_basis_review 不能自动证明源原文模糊。

3. **备用 URL 与站点**：主机场上下文和 WU Daily Observations 名称可识别意图，未给确切 WU 页面/站点是现有事实。URL 是数据获取定位，不等同权威语义。合格补证链应由原市场机场与日期 → 权威机场标识/别名（尤其 ZUCK 等代码映射）→ WU 自身标识、机场坐标与产品页面元数据 → 日期、单位、产品版本及当时 receipt 组成。只用 registry、城市同名、URL 拼接或相近坐标均不够。本轮没有完整 WU 映射链，故备用温度资格仍 UNKNOWN；但它不影响已经独立识别的 CLOB token 留存。

4. **不可用的权威**：本机网络失败是本机观测，未发布是源发布状态，页面空值可能是过滤/时间窗，源声明无数据是有来源的事实；四者不能折叠。当前 source_availability_path 对 http_failure/not_published/page_empty 都回 unknown_primary；“confirmed_absent”仍是调用方声明，函数没有验证其权威、receipt 或截止。正式来源切换/最低桶须依据市场条款与有权威的裁定/澄清及源事实；研究脚本不能凭本地失败行使这个权力。

5. **无数据范围**：主源无值、备用有值的情景直接区分“先备用”与“任一主缺数即最低桶”。文本上下文倾向前者，但没有明确源集合、切换瞬间和晚到数据处置。主备都无值也须证明是同观察日、合格时间与可接受无数据状态。保留 no_data_source_scope。

6. **独立修订条款**：若首条次日发布早于 deadline，两个截止可一致；若 deadline 先到且次日首条在以后发布，文本给出的修订接受期会延伸到名义结算触发之后。更正可能把最高温从一个桶移到另一个桶。结算流程存在 oracle 提议/争议阶段，不等于文字中的 resolve instant 与实际链上终局完全一致；不能自行用“已经结算”或 min 合并消除问题。主备来源切换后哪个“first datapoint”生效也需说明。

7. **主表缺失**：八个美国事件逐个都有 Hourly Data/Show Hourly Data 句，既有 parser 可提取，不新增缺口。重庆/成都逐个都给 NOAA、机场名、timeseries?site=zuck/zuuu、Temp 列、Celsius 和指定日期；不是“什么来源都不知道”。但缺 Hourly Data 句，不能证明采用美国同一采样表、日界/QC。固定要求展示表名才允许 token 采集过强；同源温度结算重算仍需产品证据。

8. **人工审核的限度**：审核可确认代码正确表达已取得事实、批准政策和生效边界，不能补造秒界、WU 站点、无数据权威或修订规则。官方说明市场规则决定结算，并存在 Additional context 澄清机制；这只支持应保存规则版本/澄清来源，不解决本十站具体争议。[官方 Resolution](https://docs.polymarket.com/concepts/resolution)

## 日历/DST 诊断（实际运行，仅名义分钟）

现有 RuleDeadline.nominal_local_time 的字符串输出包含 :00 是分钟起点表示，不是本报告断言精确结算发生在该秒。ET 使用 America/New_York。

| 观察标签 D | D+1 的名义 ET 分钟 | UTC | 中国时间 |
| --- | --- | --- | --- |
| 2026-09-10 | 2026-09-11T23:59:00-04:00 | 2026-09-12T03:59:00+00:00 | 2026-09-12T11:59:00+08:00 |
| 2026-01-10 | 2026-01-11T23:59:00-05:00 | 2026-01-12T04:59:00+00:00 | 2026-01-12T12:59:00+08:00 |
| 2026-03-07 | 2026-03-08T23:59:00-04:00 | 2026-03-09T03:59:00+00:00 | 2026-03-09T11:59:00+08:00 |
| 2026-10-31 | 2026-11-01T23:59:00-05:00 | 2026-11-02T04:59:00+00:00 | 2026-11-02T12:59:00+08:00 |
| 2026-12-31 | 2027-01-01T23:59:00-05:00 | 2027-01-02T04:59:00+00:00 | 2027-01-02T12:59:00+08:00 |
| 2028-02-28 | 2028-02-29T23:59:00-05:00 | 2028-03-01T04:59:00+00:00 | 2028-03-01T12:59:00+08:00 |

中国例：9 月 10 日 00:00 Asia/Shanghai 对应 ET 9 月 9 日 12:00；若先转换再取 ET 日期+1，会错算成 9 月 10 日截止，比标签解释早一天。西海岸例：9 月 10 日 23:30 PDT 是 ET 9 月 11 日 02:30，但 observation label 仍是 9 月 10 日；其名义截止是当地 9 月 11 日 20:59。春秋 DST 示例说明不能固定 UTC−5，也不能将“加一个日历日”写成对一个已换区 instant 简单加 24 小时。这些例子证明类型区分必要，不替源端做边界裁定。

## 逐站八项审查

以下 80 行逐站审查；美国的 primary_table_missing 行明确“不适用”，不虚增为 80 个实际 unresolved。每项理由和能力解释还受上面的矩阵限制；R-* 的完整 source_text 和代码依据在 JSON。

### KLGA / 987074

源：[docs/strict_rejection_query_20260910T091217Z/KLGA.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KLGA.response.json)，SHA-256 `ec8eea754f50a75b2cab010bb60b9833a8cc990b044304aaf48efadad359f999`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KLGA-deadline_second_boundary | description[546:599]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KLGA-deadline_date_basis_review | description[546:599]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/New_York，按标签 2026-09-10，名义截止对应 9 月 11 日 23:59 东部夏令时间。 |
| fallback_url_missing / R-KLGA-fallback_url_missing | description[490:688]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KLGA-fallback_station_missing | description[490:688]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=klga 已明示，WU KLGA 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KLGA-unavailable_vs_absent | description[490:688]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KLGA-no_data_source_scope | description[690:855]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KLGA-revision_after_settlement_deadline | description[1370:1575]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KLGA-primary_table_missing | description[0:490]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KORD / 987248

源：[docs/strict_rejection_query_20260910T091217Z/KORD.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KORD.response.json)，SHA-256 `cbd38493c8160a4e1d1f0ca64ef0b2be756106cde77ae5a72f58fe5fd4621105`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KORD-deadline_second_boundary | description[556:609]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KORD-deadline_date_basis_review | description[556:609]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/Chicago，按标签 2026-09-10，名义截止对应 9 月 11 日 22:59 中部夏令时间。 |
| fallback_url_missing / R-KORD-fallback_url_missing | description[500:698]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KORD-fallback_station_missing | description[500:698]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=kord 已明示，WU KORD 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KORD-unavailable_vs_absent | description[500:698]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KORD-no_data_source_scope | description[700:865]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KORD-revision_after_settlement_deadline | description[1380:1585]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KORD-primary_table_missing | description[0:500]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KLAX / 987929

源：[docs/strict_rejection_query_20260910T091217Z/KLAX.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KLAX.response.json)，SHA-256 `ac1dade45f31b9a0448e83f498a0b652a4a2362bfd568227357a58528864d35b`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KLAX-deadline_second_boundary | description[562:615]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KLAX-deadline_date_basis_review | description[562:615]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/Los_Angeles，按标签 2026-09-10，名义截止对应 9 月 11 日 20:59 太平洋夏令时间。 |
| fallback_url_missing / R-KLAX-fallback_url_missing | description[506:704]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KLAX-fallback_station_missing | description[506:704]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=klax 已明示，WU KLAX 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KLAX-unavailable_vs_absent | description[506:704]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KLAX-no_data_source_scope | description[706:871]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KLAX-revision_after_settlement_deadline | description[1386:1591]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KLAX-primary_table_missing | description[0:506]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KMIA / 987247

源：[docs/strict_rejection_query_20260910T091217Z/KMIA.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KMIA.response.json)，SHA-256 `c8bdbeb92f7ddf1103eb6b547b826b4b3d49bb3ecde06fdc40d956d135c49d0d`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KMIA-deadline_second_boundary | description[547:600]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KMIA-deadline_date_basis_review | description[547:600]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/New_York，按标签 2026-09-10，名义截止对应 9 月 11 日 23:59 东部夏令时间。 |
| fallback_url_missing / R-KMIA-fallback_url_missing | description[491:689]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KMIA-fallback_station_missing | description[491:689]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=kmia 已明示，WU KMIA 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KMIA-unavailable_vs_absent | description[491:689]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KMIA-no_data_source_scope | description[691:856]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KMIA-revision_after_settlement_deadline | description[1371:1576]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KMIA-primary_table_missing | description[0:491]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KATL / 987076

源：[docs/strict_rejection_query_20260910T091217Z/KATL.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KATL.response.json)，SHA-256 `f8ff4b1816d7bc8ffdc9e2c906f90dfb761e2c0652965fddcfdf1affc622493d`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KATL-deadline_second_boundary | description[569:622]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KATL-deadline_date_basis_review | description[569:622]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/New_York，按标签 2026-09-10，名义截止对应 9 月 11 日 23:59 东部夏令时间。 |
| fallback_url_missing / R-KATL-fallback_url_missing | description[513:711]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KATL-fallback_station_missing | description[513:711]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=katl 已明示，WU KATL 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KATL-unavailable_vs_absent | description[513:711]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KATL-no_data_source_scope | description[713:878]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KATL-revision_after_settlement_deadline | description[1393:1598]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KATL-primary_table_missing | description[0:513]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KDAL / 987075

源：[docs/strict_rejection_query_20260910T091217Z/KDAL.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KDAL.response.json)，SHA-256 `fac2c6a7b4479281af83c08d9cc1e217aa7647d760ac7a910d3c4a481d688449`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KDAL-deadline_second_boundary | description[546:599]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KDAL-deadline_date_basis_review | description[546:599]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/Chicago，按标签 2026-09-10，名义截止对应 9 月 11 日 22:59 中部夏令时间。 |
| fallback_url_missing / R-KDAL-fallback_url_missing | description[490:688]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KDAL-fallback_station_missing | description[490:688]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=kdal 已明示，WU KDAL 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KDAL-unavailable_vs_absent | description[490:688]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KDAL-no_data_source_scope | description[690:855]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KDAL-revision_after_settlement_deadline | description[1370:1575]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KDAL-primary_table_missing | description[0:490]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KHOU / 987250

源：[docs/strict_rejection_query_20260910T091217Z/KHOU.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KHOU.response.json)，SHA-256 `417cf726f4c580d8fb47f3b9291b366b46e48824c96aff1383aad92acc74c738`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KHOU-deadline_second_boundary | description[553:606]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KHOU-deadline_date_basis_review | description[553:606]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/Chicago，按标签 2026-09-10，名义截止对应 9 月 11 日 22:59 中部夏令时间。 |
| fallback_url_missing / R-KHOU-fallback_url_missing | description[497:695]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KHOU-fallback_station_missing | description[497:695]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=khou 已明示，WU KHOU 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KHOU-unavailable_vs_absent | description[497:695]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KHOU-no_data_source_scope | description[697:862]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KHOU-revision_after_settlement_deadline | description[1377:1582]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KHOU-primary_table_missing | description[0:497]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### KSEA / 987928

源：[docs/strict_rejection_query_20260910T091217Z/KSEA.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/KSEA.response.json)，SHA-256 `298399f4ec39a4ac43b31fc8e992aee9065b85c6eef0e4ad8a0852b28d10a003`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-KSEA-deadline_second_boundary | description[565:618]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-KSEA-deadline_date_basis_review | description[565:618]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 America/Los_Angeles，按标签 2026-09-10，名义截止对应 9 月 11 日 20:59 太平洋夏令时间。 |
| fallback_url_missing / R-KSEA-fallback_url_missing | description[509:707]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-KSEA-fallback_station_missing | description[509:707]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=ksea 已明示，WU KSEA 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-KSEA-unavailable_vs_absent | description[509:707]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-KSEA-no_data_source_scope | description[709:874]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-KSEA-revision_after_settlement_deadline | description[1389:1594]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-KSEA-primary_table_missing | description[0:509]；机场/NOAA URL/Temp 主段 | 本事件确有 Hourly Data/Show Hourly Data，当前 parser 没有 primary_table_missing。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 此项不适用：原文足以识别 Hourly Data / Temp；不等于主源实际可用或数据无修订。 | 不新增未决项；保留本站主条款证据，实际可用性另按源事实判断。 |

### ZUCK / 986731

源：[docs/strict_rejection_query_20260910T091217Z/ZUCK.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/ZUCK.response.json)，SHA-256 `c782201f7bb4b7c1273c1ec7befe7fab820fad2ec837f6a802f37b3c1c34f086`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-ZUCK-deadline_second_boundary | description[471:524]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-ZUCK-deadline_date_basis_review | description[471:524]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 Asia/Shanghai，按标签 2026-09-10，名义截止对应 9 月 12 日 11:59 中国时间。 |
| fallback_url_missing / R-ZUCK-fallback_url_missing | description[415:613]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-ZUCK-fallback_station_missing | description[415:613]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=zuck 已明示，WU ZUCK 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-ZUCK-unavailable_vs_absent | description[415:613]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-ZUCK-no_data_source_scope | description[615:780]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-ZUCK-revision_after_settlement_deadline | description[1288:1493]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-ZUCK-primary_table_missing | description[0:415]；机场/NOAA URL/Temp 主段 | 当前提取只认 NOAA + Hourly Data 字样；Temp/URL 另有证据。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 本地精确表名要求可能过强；https://www.weather.gov/wrh/timeseries?site=zuck、机场名及 Temp 已给，具体日高数据产品仍需补证。 | 区分站点/URL/列已识别与产品采样/时区/QC 未证明；不得把美国表名套中国。 |

### ZUUU / 986734

源：[docs/strict_rejection_query_20260910T091217Z/ZUUU.response.json](D:/poly/docs/strict_rejection_query_20260910T091217Z/ZUUU.response.json)，SHA-256 `6370d65f92d6f9c43dbc6cf96d43eab2e191f0694bf33032e05bfe5dd9787a49`。

| 项目 / 证据 ID | 原文定位 | 代码为何 UNKNOWN | 必要能力及反例 | 判定 | 最小补证或变更 |
| --- | --- | --- | --- | --- | --- |
| deadline_second_boundary / R-ZUUU-deadline_second_boundary | description[470:523]；deadline | parse_rule_contract 无条件列入；RuleDeadline.boundary=unresolved，文本只到分钟。 | 自动截止/临界退出需精确边界；23:59:30 发布在分钟起点与终点解释下结论不同。 | 源精度确实有限；要求所有能力都具秒级截止过强。 | 保留 23:59；只为边界敏感能力请求包含关系说明。原始盘口留存不依赖该秒。 |
| deadline_date_basis_review / R-ZUUU-deadline_date_basis_review | description[470:523]；deadline | parser 已写 observation_calendar_date/+1/ET，仍对所有站无条件加 review。 | 跨日结算需正确标签；把站点午夜转 ET 再取日期可错一天。 | 文本支持观察日期标签 D 的次日 ET；不是所有站都有源冲突。具体产品日界仍需独立证据。 | 标签 D、站点日界、ET instant 分离；候选标签计算可复核，不把复核当秒边界或日高产品已证明。 本站 Asia/Shanghai，按标签 2026-09-10，名义截止对应 9 月 12 日 11:59 中国时间。 |
| fallback_url_missing / R-ZUUU-fallback_url_missing | description[414:612]；fallback | 只抽 WU 名称/表，url=None；completeness 强制源 URL。 | 自动备用数据取得需可重放定位；同名城市页面可能是另一机场或 PWS。 | 源未写 URL 是事实；把文字 URL 作为 token 采集必要条件过强。 | 另存有权威站点映射支持的获取 URL、产品版本、receipt；不可声称其来自原条款。 |
| fallback_station_missing / R-ZUUU-fallback_station_missing | description[414:612]；fallback | 备用句无站号，parser 不继承主站；completeness 又产生 fallback_station_id_missing。 | 依赖备用温度的信号/结算需同机场绑定；城市同名不保证相同站。 | 精确 WU 站点绑定缺证；主机场上下文有支持，但不能自动等同 WU 标识。 | 逐站证明原文机场→权威机场 ID→WU airport/history 元数据→日期/单位/产品；链缺一环仍 UNKNOWN。 本站 NOAA site=zuuu 已明示，WU ZUUU 对应关系未由本轮源证据证明。 |
| unavailable_vs_absent / R-ZUUU-unavailable_vs_absent | description[414:612]；fallback | 识别 unavailable 条件，但没有可决定该事实的证据契约。 | 切换到备用的判断必需；本机 TLS 失败时 NOAA 仍可能有值。 | 源端触发事实/裁定语义未充分限定；不是网络异常类别名可补足。 | 保留 transport failure、未发布、空值、源声明无数据四态；正式切换需权威规则和源事实，不由本地失败触发。 |
| no_data_source_scope / R-ZUUU-no_data_source_scope | description[614:779]；no_data | no data 未列明确来源集合与检查顺序，parser 有意留 scope_unresolved。 | 最低桶/依赖无数据处置的风险判断必需；主源无值而备用有高温时最低桶与备用结果相反。 | 真实适用范围歧义；上下文倾向先备用仍不足以当自动裁定。 | 请求主备都无数据是否为必要条件、时间窗及可接受证据；不通过硬编码顺序解除。 |
| revision_after_settlement_deadline / R-ZUUU-revision_after_settlement_deadline | description[1287:1492]；revision | settlement 是首条发布或 deadline 较早者；revision 仍只写首条发布。 | 结算终局/修订稳定性判断必需；deadline 已过而首条未发期间更正是否有效？ | 真实条件性语义冲突/未定义顺序；非简单 parser 缺词。 | 保留两条时钟；平台说明 deadline 先到的更正、主备发布各如何处理，不改 revision=min。 |
| primary_table_missing / R-ZUUU-primary_table_missing | description[0:414]；机场/NOAA URL/Temp 主段 | 当前提取只认 NOAA + Hourly Data 字样；Temp/URL 另有证据。 | 同产品日高/温度特征需产品定义；显示逐小时与高频序列峰值可不同。 | 本地精确表名要求可能过强；https://www.weather.gov/wrh/timeseries?site=zuuu、机场名及 Temp 已给，具体日高数据产品仍需补证。 | 区分站点/URL/列已识别与产品采样/时区/QC 未证明；不得把美国表名套中国。 |

## 具体补证需求（本轮不请求业务数据或联系平台）

- 平台语义问题包：指明上述十个 event ID、原 clause/hash，询问 23:59 包含关系、观察标签与源日界、NOAA unavailable 的权威判据、最低桶所需来源集合、deadline 先于首条发布时的修订与备用源发布规则。优先使用已有官方 Additional context/明确规则说明；没有说明就 UNKNOWN，不要求用户凭直觉裁定。
- 备用绑定问题包：逐站权威机场→WU 标识→Daily Observations 的可复现关联，特别中国代码/机场映射。现有证据不足，不拼造 URL。
- 中国主产品问题包：对应 site 页面/官方源码中 Temp 产品的采样、日界、单位、QC/修订与小时表关系；不是要求平台必须使用英文表名。
- 如需核实今天事件，另申请一份有界公开查询：最多十站各一个指定当地日发现响应；最多两个明确争议 event 详情（总请求最多 12，单请求 15 秒/2 MiB、总 180 秒，无重试/分页/WS），先核实实际 adapter 参数和目的，按 attempt/receipt 保存投影及 hash。超限/截断记 UNKNOWN。WU/NOAA 产品查询是另一个定点证据包，不混入此额度，也不继承上一轮已用完授权。
- registry 审批、补证查询、隔离采集实现、部署/运维恢复分别授权。不得因本审查完成启动 market 或 Paper。

本轮只提出安全分层的必要条件；具体实现和消费者隔离见 [设计文件](D:/poly/docs/collection_admission_layering_design_20260911.md)。

