# 来源能力调查：S01 / S02

调查日期：2026-09-10；首个带时钟运行快照为 02:55:27 UTC（北京时间 10:55:27）。最终复核时间见配套证据索引。仓库 HEAD：`2c8d9b9d91504d579912985aad33ffd01bc5011a`。本轮仅调查与设计，没有实现、部署、业务 API probe 或 Paper 启动。

证据 ID 对应 [机器索引](source_recovery_evidence_20260910.json)。运行情况与授权拆分见 [恢复方案](collection_recovery_plan_20260910.md)。本报告的 supported/unknown 是调查分类，不是新增生产字段。

## 结论

- 公共 Data API 的时间窗、分页上限、token/condition、side、价格、数量、交易哈希有当前官方文档依据；所查本地旧 tape 也保存这些规范化字段。但没有得到可信的逐 fill 身份、全局撮合序列或 token/时间组闭合证明。继续 `UNSUPPORTED_GROUP_COMPLETENESS`，不能开放公共 queue consumption。[W01、W02、L01、SAMPLE02]
- 本地 durable receipt、manifest 与精确物化集合是已经实现的候选契约；三个抽查旧 tape 没有逐行 receipt/journal，不能称这些正式旧文件已获该资格。单条 WS/API 匹配、局部物化完整性、源端全组闭合是三个不同判断。[L02、L03、L04、SAMPLE02]
- 当前实际 GFS/ICON/GEM seamless 预报在保存的原始响应中也没有初始化字段；不是已有初始化字段被规范化时丢掉。两个真实样本调用现有 `forecast_vintage` 均为 `UNKNOWN_FIXED_FORECAST_VINTAGE`。[L06、SAMPLE02]
- Single Runs 和逐模型更新 metadata 是有文档支持、尚无本地真实样本验证的候选。固定 initialization 不等于当时已经发布，更不能补造旧档案首次 receipt。[W03、W06]
- 下一步优先处理原始行情恢复所需的诊断/隔离能力；forecast 缺口不应阻断仍在运行的原始天气采集。正式 N=0、PnL=N/A 沿用既有项目声明，本轮没有全量重审正式成绩；项目仍 NOT SEALED。[L05、L07、R01、R02]

## S01：需求、证据与边界

需求是：某 token、某决策可见时间组内，全部经济成员与可用于排队的顺序可证，重复、别名、迟到与重启不造成二次消费。HTTP 请求成功和本地排序都不足以满足这个需求。

| 能力 | 当前官方依据 | 真实样本观察 | 本地实现/假设 | 分类与缺口 |
| --- | --- | --- | --- | --- |
| 单笔身份 | Data API 列出 transactionHash、participant、asset、condition、side、p/q/time；所查契约未提供稳定 per-fill ID。WS 列出 transaction_hash | 9 月 3 日 WS 原始成交保存 tx hash；三个旧 API tape 保留 participant 与 tx hash | adapter 以 tx+participant+token+condition+time+side+Decimal p/q 去重；该组合不是上游唯一性证书 | 字段可用；经济唯一性 UNKNOWN，同 tx sibling 不可简单合并。[W01、W02、L01、SAMPLE02、SAMPLE04] |
| 事件排序 | 所查公共契约没有 exchange-global sequence/replay closure 保证 | WS envelope 有 run_id/sequence，wire 样本没有 exchange sequence | `MarketWebSocketBot._record` 本地递增 sequence；API 抓完按 timestamp/hash/asset 排序。API 秒级与 WS 毫秒级不能强行对齐 | 本地确定性可用；交易所同秒排序不受支持。补序号不得新消费，矛盾序号应隔离。[L01、L04、L08] |
| 分页与集合 | 官方 limit/offset 上限均 10000，offset 超限拒绝，支持 start/end 时间窗 | 旧 tape 只有汇总 fetched_ranges，没有原始逐页快照 | 满页递归二分、右窗 midpoint+1，短页结束；隐含整秒边界假设 | 文档支持分页机制；未证稳定快照、tie-break、上下界精确包含性、迟到/修正界限或源 watermark。UNSUPPORTED_GROUP_COMPLETENESS。[W01、L01、L02] |
| 可见时间 | wire time 与本地 receipt 不同，官方未承诺本地可见时间 | 抽查 API tape 无逐行 receipt；WS 有 received_at/ns | collector 在整个 `market_trades` 返回后取 response receipt；fact fsync 后取 commit bound，再 witness、manifest、tape。分页 receipt 丢失在 adapter 返回契约 | 新候选本地持久可见性有契约；旧 tape UNKNOWN_RECEIPT。逐页 receipt 是本地适配缺失，但补它不能证明组闭合。[L01、L02、L03、SAMPLE02] |
| 质量与恢复 | 连接事件/响应字段不能证明断流区间无漏单 | 所查 WS 保存 upstream_status/incident；API 旧 tape 没有完整来源质量链 | matcher 检查同 token/market、side、精确 p/q/time、receipt 与整个依赖时间区间；pending 持久保存并在 API 到达后复核 | 重连/gap/歧义/冲突/成员删减均不能提升资格；新证据仅在自身可见时生效。[L04、L05、SAMPLE04] |

官方事实与推断分开：未在上述契约找到 closure 保证，是本次证据不足；不宣称所有公共来源都不可能提供。当前文档比旧摘要明确了超 offset cap 返回 400、start/end 窗口预算等细节，但没有因此产生源端闭合证明。[W01]

### 实际生产转换链

1. `adapters/polymarket_data.py:PolymarketDataClient._page` 取得 JSON；`_range` 分页/二分；`_trades_for_query` 规范化、精确键去重、排序；`parse_public_trade` 保存源 timestamp 文本。原始页、响应 header、各页 receipt 不进入返回类型。`event_trades` 的“complete tape”注释应解释为本地抓取意图，不能作为官方完整性保证。[L01]
2. `public_trade_collection.py:_collect_depth_event_trades_locked` 在 lock 内预检旧 tape/journal，恢复待见证 fact；成功响应和异常分开记录。`ReceiptJournal.append` 持久化 fact+witness；`_receipt_rows` 以 source/response/commit 最大值确定可见性；旧行不因重试升级 receipt。[L02、L03]
3. `verify_materialized_receipts` 验证指定 journal prefix 的 manifest、完整 payload、精确去重成员及 baseline。删除成员、重复、换 event/anchor 或丢 marker 均拒绝。该证书证明“本地已经收到的这些成员被正确物化”，不证明源端没有漏发。[L02、L03]
4. `trade_tape_analysis.py:load_event_trade_tapes` 与 `shadow_runtime.py:_public_trade_events_from_file` 验证新 journal claims；历史无 receipt 行不会变成前向证据。`market_trade_tape.py:match_ws_trade` 先按 tx 找候选；候选不唯一即 UNKNOWN，再比较 token/market/side/p/q/time/receipt/quality。其 `MATCHED_COMPLETE` 仅代表一条记录匹配完整。[L04]
5. `paper_spread_runtime.py` 的 pending 记录/恢复及公共 `process_trades` 最终仍记录 UNKNOWN 组证据并返回空 fill。私有 `_process_ordered_model_trades` 是 MODEL_KERNEL，不是本轮可用旁路。v2/QUIET/complement 的既有排队结果仍是隔离的模型诊断。[L05]

### 三层判断必须分别报告

| 问题 | 本轮结果 | 不能外推什么 |
| --- | --- | --- |
| 本地 durable receipt 是否完整物化？ | 候选代码有 exact-set 验证；既有 741 passed 是历史验证。三个真实 tape 的 journal 不存在、receipt_contract 为空，未取得正式样本的肯定结论 | 不能把无 journal 的旧 tape 叫作验证成功的 durable receipt。[H01、L02、SAMPLE02] |
| WS/API 可否唯一交叉核验一条？ | 本地 matcher 可做条件式严格核验；本轮没有寻找配对样本、没有宣称 MATCHED_COMPLETE 个案。WS 样本只有毫秒时间，API 秒级身份仍可能冲突 | tx 相等不等于单笔唯一；不能截断毫秒绕过比较。[L04、SAMPLE04] |
| token/时间组全部成员及顺序是否可证？ | UNSUPPORTED_GROUP_COMPLETENESS | 短/空页、文件尾、poll 结束、后续时间戳、无重连、等待时间、HTTP 200、本地序号都不能封组。[W01、W02、L05] |

抽样范围：API 三个 Atlanta 文件完整读取，925/778/1242 行，只有规范化旧 tape，没有原始 HTTP wire；WS 仅读取 9 月 3 日首 4 MiB 和限定尾部，9 月 4 日首 4 MiB/尾 256 KiB。不是全归档、全市场或全组覆盖审计。各次读取有 range hash 和前后 stat，读取中变动则不用于一致性判断。[SAMPLE01—SAMPLE04]

### 现有参考与替代候选：有界调查结果

| 来源 | 契约/源码证据与许可 | 成本、依赖与语义差异 | 决策 |
| --- | --- | --- | --- |
| warproxxx/poly-maker | 访问 main 的 marketdata/parse.py、service.py 和 MIT LICENSE；本次历史 revision unknown。TradePrint 使用 float，缺稳定 fill ID；服务重连后取新 book | 同一公共 WS，未提供闭合证明；完整项目有执行依赖，本轮不采用 | 仅设计参考，不替代 Decimal/receipt/closure。[W08] |
| spencerfletcher/market-maker | 本地 notice 保存 revision `68f6c44730dab0772a3601072b0e00ee190f3a4c`、MIT；本次官方仓库该 queue_tracker 路径两次获取失败 | 旧调研是 Kalshi 队列诊断；不能从另一交易所的队列模型推导 Polymarket closure | 当前源码复核受限，历史结论不升级；不无界重试。[W09、H02] |
| 可选 Nautilus | 本地 notice 固定 rc4/LGPL-3.0，当前目录检查未获得可读 adapter 源文件 | challenger 与本地资格权威隔离；没有新执行能力；不再探测已知历史深度端点 | 未新增资格；完整 bundle 许可审计仍需后续。[H02、LIMIT01] |
| Polymarket 官方旧 ctf-exchange | 查到 ITrading.sol 的 OrderFilled/OrdersMatched 字段，文件 SPDX MIT；仓库标为 archived，当前部署绑定未知 | 可研究链上 fill/日志身份，但需核实部署合约、区块最终性、reorg、索引缺口、公开 RPC 限制/费用；链上顺序不自动等于 CLOB aggressor/队列顺序 | 替代证据候选，非即插即用；未发 RPC、未构造执行客户端。LICENSE URL 获取失败不改写 SPDX，也不声称完成分发审计。[W10] |

### 最小后续验证计划（本轮不执行）

- 代码授权候选：仅给 adapter 返回结果增加逐页完整 receipt、查询边界、原始页 digest、响应完成与失败分离、重试页身份和截断原因；保留 first visibility。正例多页/重复页/成功空页；反例页中异常、边界 sibling、迟到、删除成员、价格精度、重连、相同 tx 不同 fill、API/WS 冲突与重启。组闭合闸门保持关闭。[L01—L05]
- 若另获数据请求授权：仅 `GET https://data-api.polymarket.com/trades`，一个已有 condition、一个不超过 60 秒的历史窗，limit=100，offset=0/100，两遍对照，最多 4 次请求、每次 1 MiB/15 秒、无自动重试，输出到全新临时目录（未来以实际创建的绝对路径登记）。目的仅核查 wire/页重叠/边界和 receipt；不能以四次一致证明 closure。现有旧 tape 不包含 wire 时，这一步属于待样本验证，不是本轮已通过。
- 调查停止：目前来源和有界参考审查没有可靠封组依据，正式公共入口继续零 queue consumption。改变排队模型只能另立版本化研究任务。

## S02：实际天气产品与消费者

| 当前 provider/product/model | 原始字段与本地保留 | initialization / availability / valid / receipt / revision | 实际资格消费者 |
| --- | --- | --- | --- |
| NOAA WRH/Synoptic：wrh_timeseries_observation | source_payload 内 OBSERVATIONS 的 date_time、air_temp_set_1；外层 station/source_ms、received_at/ns、run_id/sequence、mode 与精度保留 | 属于观测，无模型初始化；观测时刻与本地 receipt 分开。QC/revision 应按 receipt-visible 版本选择 | weather_market_join、signal 的物理高点/升温、information clock、Paper weather gate；不能用未来 QC 回写历史。[L06、L07] |
| NOAA/NWS：latest_observation | 原始 properties/温度/时间，规范化 source_ms 与原始 payload | 观测时间不是预报初始化；首次本地 receipt 仍独立 | 同上观测 join；signal 交叉核验，不是 blended forecast。[L06、L07、W12] |
| AWC：metar | reports、obsTime、报文与 T 组精度标记保留 | observation time；校正报文身份需保留原文及 receipt，不冒充模型 run | 观测 join、signal 交叉检查与 information clock 的 SPECI/天气风险。[L06、L07、W11] |
| AWC：taf | raw 列表与 issueTime，source_ms 使用最新 issueTime | TAF 是预报报文；issue/valid period/修订与本地 receipt 分开，不是数值模式初始化 | information clock 的 TAF 变化；temperature observation join 明确拒绝此 product，signal 的高温概率选择不使用 TAF 代替三模型。[L06、L07、W11] |
| Open-Meteo：multi_model_deterministic_forecast / gfs_seamless | gfs model/time/temperature_2m/grid/timezone/unit；source_payload 与 blended 权重均保留 | initialization、源端 availability、immutable revision 均未提供于抽样响应；time 为 valid time；source_ms=null，外层 receipt 有值 | signal 的 daily-high 概率需合格 forecast；information clock 单模型 model_run 资格拒绝。[SAMPLE02、L06、L07] |
| 同上 / icon_seamless | icon 自己的同类字段 | 同上，不能借用 gfs 或外层 run_id | 同上，逐模型独立 lineage。[SAMPLE02、L06] |
| 同上 / gem_seamless | gem 自己的同类字段 | 同上；seamless 自身还可能组合 provider 内多个产品 | 同上，必须核实实际 underlying model 映射。[SAMPLE02、L06、W05] |
| 历史研究：Open-Meteo Previous Runs / 三模型 lead_days | hourly previous_dayN 后按当地日取最大值，保留 raw、model、lead_days、fetched_at | 固定 lead offset 不是整条单一 run；现在历史查询不能提供当时本地 receipt | 历史校准/skill 分析；lead_days=0 禁入严格校准，lead>=1 也不因此获得完整 fixed-vintage 资格。[L09、W04] |

原始天气采集 `_fetch_* → _event → WeatherStreamSink.write` 会在响应解析后保存真实本地接收时钟；不是网络 packet 的逐字节到达时刻，也不是 fsync 时刻。`run_id` 是采集进程身份。当前样本 `source_payload` 的 generationtime_ms 是生成响应所需耗时，不能当初始化时间。[L06、W05]

`weather_market_join.parse_weather_observation` 只接纳三种观测 product。Paper `PaperWeatherEvidence` 从 join metadata 消费 source/receipt/物理条件，不直接消费 forecast 初始化；其整体 readiness 仍可能经 signal 等依赖受到影响。`signal_engine._ingest_weather → _event_signal.visible` 对 unknown forecast 的 source_time=None 不授权概率计算；WRH 观测事实仍可保留。`information_clock._weather_daemon_events` 为各模型分别生成 identity，并把 vintage 结果放入 receipt_verified。由此不能把 forecast 缺口扩大为“所有天气原始数据必须停采”。[L07]

### 固定 run 候选及历史可救范围

| 产品 | 当前官方承诺 | 未证明的部分与结果 |
| --- | --- | --- |
| Forecast API | seamless/最新运行整合时间序列 | 现有响应未证明每一点的固定初始化 lineage；旧档案继续 UNKNOWN。[W05、SAMPLE02] |
| Historical Forecast | 历史预测时间序列，可用于模型分析 | 不自动恢复旧日当时可见运行及 receipt；不能代替 Single Runs。[W07] |
| Previous Runs | 每个 valid time 的固定提前天数值 | lead1 不是整天统一 initialization；不改当前 lead0 规则。[W04] |
| Single Runs | run 参数指定 UTC 初始化，单次 run 单独保存；文档称多数模型自 2026-04-02 归档 | initialization 与真正发布有时间差；具体 gfs/icon/gem seamless 组合、修订/immutable payload 及旧日期覆盖需样本验证。不能给旧 raw 补 hash 或 run 参数后追认。[W03] |
| Model Updates metadata | 分开 initialisation、modification、availability；存在多服务器最终一致性 | latest metadata 与某次 payload 未必同版，尤其 seamless；不能使用“等十分钟”给任意响应授信。需要明确产品/run 与 payload digest 绑定，保留同次查询 receipt。[W06] |

Open-Meteo 数据许可为 CC BY 4.0；托管免费 API 与商用使用条件须单独遵守，不能把数据许可误读成无限制免费托管服务。此轮无购买、无凭据、无调用额度消耗的业务请求。具体 endpoint 可访问性与配额适用性仍待授权验证。[W13]

未来新采集可以按每个明确产品、初始化周期、valid axis、首次 receipt、完整 source payload/hash 和修订身份保存证据；目前不能宣称已有生产实现。若相同 run 返回不同内容，保留新版本并按后来的 receipt 生效，不覆盖旧版。源端 availability 没给出时必须标 UNKNOWN；真实首次 receipt 可作为保守本地可见上界，但不能伪装成源端发布时间。

### 下一阶段最小验收设计

1. 仅一个站点、逐个 gfs/icon/gem 明确产品的 Single Runs 小样本。先核对 seamless 的 underlying model 支持，不把返回成功当逐模型 lineage 完整。另行授权时：每模型一次固定 run 和一次同 run 重取，最多 6 次；逐模型 metadata 最多 3 次，总共不超过 9 次，每次 2 MiB/20 秒，无重试/后台轮询，全新临时目录，不触碰正式 data。端点为文档列出的 `single-runs-api.open-meteo.com/v1/forecast`；metadata 的精确逐模型 URL 尚待从官方 model-updates 链接核实，未核实时不提交可运行脚本。[W03、W06]
2. 正例保存 initialization、明确产品、valid time、完整 payload、请求结束 receipt，若源端提供 availability/revision 也原样保留；缺源字段显式 UNKNOWN。每个模型各有 lineage，blend 记录依赖成员与权重版本。[L06、L07]
3. 反例至少覆盖：缺一个模型初始化、未来初始化、晚到旧 run、同 run 修订、混合模型不同 initialization、源/receipt 时钟非法、lead0/非整数 lead、metadata 与 payload run 不一致。追加未来/修订数据后，过去已提交决策前缀必须不变。[L07]
4. 验证 parser、真实 producer envelope、join/signal/information/Paper 消费链和重启可见性；合成反例仅证明拒绝契约，真实来源资格必须有原始样本。旧档案无首次 receipt/固定 run 的部分维持 UNKNOWN，历史回填仍仅事后用途。

## 文档冲突与限制

- `AGENTS.md` 的 303 tests 和旧长期运行描述是历史检查点；本轮读取到 2026-09-09 独立 741 passed 记录，未重跑全套，不再把旧 socket 超时列为当前 full-suite blocker。[H01]
- `HANDOFF.md` 的四任务 Running 不能当当前采集健康；本轮发现 weather 在采、market 启动核验失败、signal PID 复用、shadow 停止，详见恢复报告。[R01、R02]
- 旧 API 摘要/adapter “完整 tape”措辞、旧数据源“无 page 语义”概括应细化为“有分页文档、没有源端组闭合证明”；本轮不修改旧规范。[W01、L01]
- 本地样本不足以判定所有旧档案都不合格，也不足以认证完整物化。未查询数据库、未解压巨型 gzip、未进行 WS/API 对照网络实验、未验证源端全量/排序/迟到界限。[SAMPLE01—SAMPLE04、LIMIT01]
- 本轮只创建三份约定交付物；不修改产品/配置/runner/正式数据，不 commit/push。调查完成不等于采集恢复或 Paper 封板。
