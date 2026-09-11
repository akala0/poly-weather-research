# 采集准入分层设计（2026-09-11，仅提案）

> 状态注记（2026-09-11）：本文保留设计冻结时点的“未实现/未验收”表述，作为范围基线。最小实现现已落在 `src/poly_weather/collection_identity.py`、`src/poly_weather/collection_capture.py` 和 CLI 的默认关闭 `market-capture` 入口；临时验证结果与仍未完成的异步入口验收见 [实现记录](D:/poly/docs/collection_capture_implementation_20260911.md)。这不构成正式 market/supervisor 恢复授权。

**可以提出最小分层方案，但当前代码没有安全启用条件。** 推荐独立物理目录 + 不兼容旧消费者的 capture 数据格式 + 独立 collection membership/status + 无 DuckDB/策略发布的原始留存路径。先实现并在临时 fixture 验证身份门和隔离，再单独申请运维恢复。本轮不实施、不改 verifier/registry、不启动任何链路。

规则结论见 [逐站审查](D:/poly/docs/unresolved_rule_evidence_review_20260911.md)，诊断命令、结果和源码指纹见 [JSON 证据](D:/poly/docs/rule_admission_review_evidence_20260911.json)。以下名称中 collection-qualified、strategy-approved、capture profile 是提议概念，**不是现有生产字段或可执行命令**。

## 当前实际调用图及耦合

```text
Gamma search_markets_page → 转换前公开 projection/receipt → Market.from_gamma
  → discover_event: registry slug pattern + target suffix + 候选唯一
  → CLI market-supervisor 或后续 reconcile:
       parse_settlement_evidence → verify_settlement_evidence
       拒绝: 逐项 reasons/expected/actual → 不新增订阅
       通过: event_asset_maps → subscribe/initial assets → BookState
       → Raw JSONL + Checkpoint JSONL + market_stream.duckdb
       → supervisor.active_events + status + signal_config_update
          → signal config 重新取事件/parse/hash/verify → signal raw/DB/state
          → Paper continuous: status/active set + rules/readiness/quality/account

旁路:
CLI market-stream → 直接映射并订阅
  (完整 verifier 仅决定 EventArchivePolicy，不阻断 token 订阅)
  → 同样的正式 raw/checkpoint/DB/status

目录/DB独立读者:
raw/checkpoints → real_no_books / shadow pairer → v2、QUIET、complement
               → Paper conversion/readiness、研究、public trade discovery
raw/WS         → signal、tape match、microstructure、depth calibration、maintenance
raw/Gamma      → tick/min-size rule index、information clock、quiet forensics
market_stream_events → liquidity_report / warehouse counts
```

| 闸门/边界 | 输入 → 输出及调用方 | 当前事实/风险 |
| --- | --- | --- |
| Gamma 转换 | 公共 payload → EventSnapshot/Market；C06、C10 | outcomes 与 prices 长度验证存在；token/condition 的完整关系不由 Domain validator 保证。转换前保留投影，转换错误另记。 |
| 发现 | spec/目标日/search → 唯一候选；[market_supervisor.py:71](D:/poly/src/poly_weather/market_supervisor.py:71) | 匹配 slug 和 suffix；依赖上游 active search 参数，未独立证明所有身份关系或分页完整性。发现歧义必须继续拒绝。 |
| 初始 supervisor | parse/verify → 通过事件或 exit 2；C01 | 十站旧投影仍完整核验失败。--raw-collection-recovery 不放宽这个门。 |
| 后续 reconcile | 新 event → parse/同一 verify → subscribe；C02 | 相同完整规则门，但已 active 的 slug 在核验前跳过，不是持续逐版本核验。 |
| 循环证据 | today/tomorrow → discovered[spec.key]；C03 | 后一天覆盖同一 key 的前一天；已有 event detail 仅检查 closed。可能存在已保存 input 无最终核验结果、同 slug 变更未处理；源码风险，未做完整循环故障复现。新模式不能沿用这处覆盖。 |
| 直接 market-stream | exact slugs → token map → bot；C04 | **核验失败仍可订阅**，只是没有已核验时间窗 policy；没有隔离 namespace，也没有 raw recovery 参数。不是可用的恢复捷径。纠正旧报告中“该入口做核验”容易造成的阻断误读。 |
| token mapping | event.markets → 两个 token→slug 字典；C05 | D-ID 诊断：缺 condition 仍返回22 tokens；重复 token 覆盖成 No 后21；一个未对齐市场被跳过后20。不是仅源码推测。 |
| rule hash | parser/schema/description/event/bucket → evidence hash；C08 | 不显式绑定 clobTokenIds/conditionId/outcome 完整映射，不能拿现有 rule hash 代替 identity manifest hash。 |
| WS/L2 | assets_ids → book/price_change → 本地状态；C12、C14、C39 | 收到 token/condition 尚无完整 allowlist 绑定校验；缺 bids/asks 可能被默认为空并标 complete；重连路径保留旧 BookState.complete。这些为源码风险，未执行 WS/故障试验。 |
| 持久化 | StreamRecord → raw/checkpoint/DB；C11、C13 | raw mode 仍用正式 source 名、DB、状态路径；标准记录无完整 rule admission；09–20 以外有每小时裁剪，即使 raw recovery。 |
| 发布 | active set → signal update/status；C15 | raw recovery 禁发 signal update，status 仍含 active_events；published business position 为0时不能用它充作策略发布进度。collection 成功不等于 strategy active。 |
| 运维保护 | runner attempt / mutex / raw flags；C32–34 | 当前磁盘代码保留实际 null exit、未确认退出阻断重试；raw mode 不跑 retention/显式 DB 维护。未运行 runner 验证，旧内存 runner 不会因磁盘改变立即更新。 |

官方资料支持事件容纳市场、市场有 condition 与 YES/NO token 身份；并区分 enableOrderBook。它不证明十站当前映射或事件规则一致。[Markets & Events](https://docs.polymarket.com/concepts/markets-events)
公开 Market Stream 文档以 token 订阅，列出 book/price_change；协议层没有要求本地结算契约必须完整，因而“结算未决不必阻断原始订阅”是结合本地代码作出的架构判断。[Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)
本轮文档已重定向到新版 topic/tokenIds 示意，当前代码使用旧 assets_ids/type wire；未请求 WS，不能由文档示例声称当前 wire 兼容。未来真实恢复前须在单独授权范围验证，未确认格式只留原文、不可标健康 L2。

## 所有现有消费者的隔离审查

审查范围：src/poly_weather 下生产 Python 的目录枚举、共享 archive reader、market DB 表名与消费者调用；Windows runner/status 路径；既有 consumer/receipt/archive/health 契约。AST 检索得到 72 条定位记录（实际数量见 JSON），并沿下表调用方检查。这里的“所有”指仓库已识别生产路径，不涵盖用户任意外部 SQL/另行编写脚本或 .claude 副本运行。没有读取正式归档/DB或执行消费者。

| 读取方 | 实际路径、函数/行号 | 旧 UNKNOWN 可能怎样流失 | 推荐隔离的实际挡点/未来断言 |
| --- | --- | --- | --- |
| signal 常驻/非 supervised | CLI _build_signal_configs:4528、signal_engine:4656；SignalEngine.__init__:448、_bootstrap_market_books:647、_ingest_market:599 | 配置有 parse/hash/verify；行输入没有逐条 settlement gate。market_resolved 分支在 asset allowlist 之前；同 token 新行可能影响旧配置/NoForward 结果。 | 不产生 signal_config_update、polymarket_ws_status、formal raw；capture 行无旧 event_type/raw/received_at；比较 signal 状态、NoForward、DB均无新效果。 |
| v2 continuous / once / replay | shadow_runtime:832/1046/1169/1244；_archive_pair_rows:238；CLI analyze_shadow_spread:3145/3187/3196 | 按目录读 checkpoint/weather/WS/signal 和 public_trades，不只读 supervisor 事件。配对未上提 settlement false；shared converter 默认 true。上游 business gate 存在，不能代替每行规则资格。 | 无这些 source 目录及 cursor 输入；显式误传 capture 行也拒绝/无 snapshot，旧 ledger/cursor/库存不动。 |
| QUIET | CLI analyze_quiet_window:3352/3426/3624/3641；quiet_window_strategy._as_snapshot:180 | real_no_books 配对→shared converter；收到 book health flag 不代表原文完整核验。information clock 的规则事件只是信息变化。 | 不可读 capture envelope；QUIET state/store/订单/报告计数无新增隔离行。 |
| complement | CLI analyze_shadow_complement_pairs:3993/4015/4036；complement_pair.pair_snapshots_from_mapping:956 | 目录枚举，两腿都走 BookSnapshot.from_mapping 顶层缺失默认 true。独立策略账本不能保护输入来源。 | 显式两腿 fixture 都保持无准入，pair ledger 无新订单/fill；不借 pair 配平批准规则。 |
| Paper continuous | paper_spread_runtime:2821/3025/3130/3155 | 也读全部 checkpoint；转换默认 true，但 readiness 的 rules 使用 row.get(...) is True，缺失拒绝，另有 membership/progress/account/group unsupported。**未证明绕过 Paper 建仓门**。旧风险退出/生命周期仍可收到数据。 | 无正式 status/membership/raw，旧账本无任何新 mutation；建仓/退出/close/pending/cursor均比较，不只 BUY=0。 |
| Paper replay / MODEL_KERNEL | replay_paper_spread:3416、BookSnapshot.from_mapping:351 | 接受调用方准备对象；内核不是 public admission 权威，不能把其测试成功当隔离保障。 | 保持显式输入边界；capture 格式无法直接变 BookSnapshot。不得生成官方评分/forward fills。 |
| CLI 原生 NO/深度/入场/价带 | CLI analyze_real_no_book:2585、analyze_no_entry_accessibility:2636、analyze_price_band_accessibility:2758、analyze_eliminated_no_exit:4155 | jsonl_archive_paths→real_no_books._eligible_book_rows:237 / iter_paired_book_snapshots:370；主要检查深度与质量。archived_event_metadata:54 只是 registry 唯一 slug/日期关联，并非 verified gate。 | capture 源不枚举，明确 no eligible inputs；不能让已有正式报告混进隔离统计。 |
| price paths / challenger / weather join | CLI analyze_price_paths:2828、analyze_market_state_challenger:2940/3010；price_path_analysis:137、weather_market_join:158/223 | 显式路径、天气对齐和 shared snapshot；metadata 默认值不组成规则准入。 | 原始 capture 不能直接解为 market/weather row；另行诊断导出须历史/隔离标记，当前不提供导出。 |
| liquidity DB / JSONL | CLI liquidity_report:2004；liquidity.archived_liquidity_rows:120、SQL:138、JSONL fallback:255；ResearchWarehouse:68/195/867 | market_stream_events 表含行情/quality，未按结算资格过滤；read 类命令实例化 warehouse 还能创建/迁移 schema。DB counter 是记录数，不是已核验数。 | **完全不建 DB**，不调用 ResearchWarehouse，不加共享表/视图。不得仅用独立 table 名并让 counts/all SQL误算。 |
| depth calibration | depth_calibration.replay_books_at_or_before:67 | 扫 raw/WS，只按时间/对象重建历史簿，不是结算 verifier。 | 不出现旧 WS 路径/形状；同 cutoff 输出保持不变。 |
| public trade 自动发现 | CLI collect_public_trades:2398/2415；public_trade_collection:96 | 从 checkpoint 推 token，再可能发独立公开查询；新文件可扩大目标集，即使没有策略。 | 隔离行不能成为发现输入；临时 fake transport 断言新增业务请求数0，当前未运行。 |
| WS tape / microstructure / maintenance | market_trade_tape:255、market_microstructure:662、maintenance_audit:104/127；CLI audit_polymarket_maintenance:2352 | 读取显式 WS/checkpoint 列表；tape row match/quality不是 settlement 或 group closure。 | 不发现 capture；手动传入行不产生 accepted trade、baseline 或正式质量分母。 |
| rule 元数据 / forensics | real_no_books._rule_archive_paths:122/archived_market_rule_index:147；quiet_order_forensics:1189/1196/1216 | 从 checkpoint.parents[3] 推 raw/Gamma 再 rglob；rule_provenance 是 tick/minsize，不代表结算合同批准；部分紧缩转换只保留选定字段。 | 规则材料在 evidence/*.rule.json，非 events.jsonl，不放到 raw/Gamma；不能从 sibling parent 自动推到正式源。 |
| external information clock | information_clock:825/1338 | Gamma/settlement/status payload 生成 settlement_rule 事件、hash，不赋策略批准。共享目录仍可改变 EVENT/DIGESTION等状态。 | 不发布正式规则事件/状态；隔离新证据不改变原 clock 前缀。 |
| 下游派生/迁移/可选 challenger | signal_migration CLI:4515；no_forward:243、no_side_analysis:316、trade_tape_analysis:72；Nautilus 接收上层转换结果 | 读 signal_snapshot/no_forward/public tape或显式对象；不是新 raw 的直接读者，但会继承上层污染。 | 上游零输入之外，未来验收比较派生产物、经济权威与报告；独立 ledger 不替代来源隔离。 |
| status / runner / retention | runtime_safety:34；runner dependency gate:209、Get-ArgumentList:277；retention:18/159 | 固定正式状态路径；订阅/connected 与业务进度分别判断。旧进程可能仍等依赖；retention 根据 source/path，而非新 UNKNOWN bool。 | 不输出任何正式状态名/业务样本；独立 collection_health 不被链读取；禁 retention/维护/cleanup。旧 runner 接管另需运维核查。 |

关键纯函数反例（D-CONVERT，实际执行）：`_archive_pair_rows` 的两侧都有 `settlement_verified=false`，产物顶层没有该字段；分别经 shadow converter、BookSnapshot.from_mapping、QUIET converter、complement 两腿转换后均得到 true。显式给 shadow converter 顶层 false 才得到 false。**只证明资格字段丢失/默认；没有运行引擎，没有证明会产生订单/fill，更没有证明 Paper 已放行。**

共享标签方案还需修复未知字段被过滤、默认 true、旧 kernel 例外、membership/hash 全链绑定和 DB 读视图，明显超过本次最小主题。现有 status 的完整性仅证明内容未坏，不能证明每事件规则 VERIFIED。

## 三方案比较与选定范围

| 方案 | 收益 | 当前阻碍/代价 | 决定 |
| --- | --- | --- | --- |
| 保持现状 | 规则完整核验不变，无新准入 | supervisor 十站拒绝；深度仍无新恢复证据。直接 market-stream 是未经隔离的旁路。 | 本轮运行状态保持；不能作为恢复设计完成的替代。 |
| 独立隔离原始采集 | 缺不可回补的盘口先保留；不触发策略 | 必须新增身份核验、输出注入和不兼容格式、唯一 writer/资源/故障边界；当前不存在。 | **推荐仅实现这个主题**，通过临时端到端隔离验收后才可申请运维。 |
| 共享存储+标签 | 分析查找方便 | 已复现嵌套 false 被忽略；多个 glob/DB/转换旁路，需所有消费者逐行验证 admission。一次遗漏会污染状态/报告。 | 当前不可实施，不选。 |

分层只是资格分离：identity-qualified → 可订阅；durable-capture → 可留存；healthy-L2 → 独立满足快照/连续/时间/质量；research/strategy/Paper-qualified → 仍需各自证据。每箭头都不是自动授权。纯盘口诊断将来也要显式以隔离数据做诊断，不生成正式前向业绩。

## 推荐输出与身份契约（全部待实现）

**路径和格式同时隔离。** 示例根 `D:/poly/collection_quarantine/<campaign-id>/`，在正式 data 之外；路径为提案，不是已创建目录。启动参数须显式 collection root，不能复用默认 data-dir。解析真实绝对路径、父目录/文件身份、junction/symlink 后验证不与任何正式 data/raw/DB/runtime/ledger/cursor 或其父范围重叠；未知/冲突拒绝，防止大小写/链接别名指回正式目录。

只生成 `captures/<run>/<segment>.capture.jsonl`、`evidence/<id>.rule.json`、`control/collection-membership.json`、`control/collection-health.json` 和独立 attempt/commit 记录。**不生成 raw/*/events.jsonl、market_stream.duckdb、signal_config_update、market_supervisor_status 或 polymarket_ws_status；不提供指向正式文件的 link/view。** 第一阶段不建任何 DB，不发布 legacy checkpoint；需要 L2诊断快照时只放相同 capture envelope 内，标 derived，不改源报文。

capture envelope 使用独立 schema/domain，保存 `capture_schema`、`campaign/run/attempt/connection_epoch`、`receipt_ns`、局部 frame sequence、source URL/transport 类型、identity manifest hash、rule projection hash/parser/schema、UNKNOWN/reasons、payload byte hash 与 `payload_bytes_base64`。不在顶层暴露旧 reader 可直接接受的 event_type/raw/received_at/asset_id/no/yes 等字段；base64 仅为精确保存公开原文，**不是加密或脱敏**。规则文本另按 public projection 保存，并保留原字节/投影不同哈希与版本。遇不支持或坏报文先留有界原文，解析失败单列，不能伪造正常记录。

这能让固定 raw glob 和 DB 查询没有新输入，也使显式误传 capture 行不能直接成为旧 BookSnapshot/tape/settlement row。但这是依据源码的隔离设计，**不是已验证安全证明**：未来必须把 actual capture 文件/行送到上表每类实际入口，断言拒绝或零输入且无 ledger/status/报告变化。任何一条能误用则默认关闭，先修该窄入口；不得上线后补测。第一阶段不提供“解包为旧 schema”的导出器，否则重新打开旁路。

身份 manifest 必须独立于 settlement verifier：

- 绑定 discovery request/receipt/target station-day、唯一 event ID/slug/title/description 日期与机场站号；registry 仅用于有来源的 allowlist/站点关联，不借旧 verified 标记批准新条款。冲突、歧义、缺必需身份、未确认分页覆盖均拒绝对应候选，不猜 token。
- 保存 event → market ID/slug/question → condition ID → outcome index/YES/NO → distinct token IDs 的完整有序关系；全部字段非空、严格类型、长度对齐、站内/跨事件唯一/冲突检查。重复相同对象也须记录去重依据，不以 dict 后写覆盖。桶范围与日期矛盾属于身份/产品不一致拒绝；最低桶结算语义依旧 UNKNOWN。
- 以整个映射及源投影生成独立 identity hash，不能复用只含 rule/bucket 的 hash。当前十站历史投影检查通过的只是小部分关系，没有证明一个生产 identity validator 已存在。enableOrderBook 缺失不能伪称 true；只读订阅响应/有界首簿等待是可用性检查，不据此授予交易能力。
- 每个 WS asset 必须在该 connection epoch 的 allowlist 中，message.market 作为 condition ID 对照 Gamma condition；保留独立 gamma market ID，避免现有 market_id 同名混淆。未知/错 condition 或 token：保留拒绝证据，不更新合格 book，不扩大订阅集。lifecycle 消息也按身份核验。
- rule snapshot 在决策前持久化，首次真实 receipt 不刷新；后续内容变更/缺失追加新版本。station/day/token/condition 身份不再可证则撤该 collection membership 并记录；仅 settlement 语义 UNKNOWN 不升级资格，身份仍可靠可继续隔离留存。不得因相同 slug 跳过复核；每个站点日期候选有终态诊断，不能 today 被 tomorrow 覆盖。
- 关闭/退订只有相应可信 lifecycle/当前详情证据或预声明集合边界触发；保留 close/退订 sent/ack-or-timeout、最后 durable frontier。失败不声称已退订；迟到包仍留证但不能复活 membership。不把推断的 settlement deadline 当已 closed。

**L2和原始档案独立。** WS接收完成即记录真实 receipt/connection epoch；保留原始 source 时间文本和精度，精确 UTC 转换；局部 sequence 只证明本进程顺序，不证明上游未丢帧。首次 full book 两侧字段必须真实存在，空数组可作为有证据的空簿，缺字段不可等同空簿。所有增量只作用相同 token/condition 的已初始化 epoch；重连/gap/错序/持久化失败后完整性降 UNKNOWN，新的全量快照只重建其后状态、不抹历史 gap。陈旧、官方维护、本地断流、连接状态与 rule UNKNOWN 分开。不得用成交价、mid、1-p、对侧 token 或最后结算值补簿。

原始模式必须保存每个收到的合格/待诊断报文，不继承 C13 的窗外每小时策略；保持既有 WS订阅/心跳节奏，不降频。稀疏无交易不等于没连接；frame sequence、durable bytes、真实连接检查分别报告，不能把 PONG/connected 直接等同已恢复全部 L2。公共成交 group completeness 继续 UNSUPPORTED，本地连续也不证明 tape 组封闭。

## 唯一 writer、持久化与资源

复用已有经过审查的锁/写入/attempt 技术，但显式注入 collection paths；禁止仅在最后一步改输出目录。MarketWebSocketBot 构造器已经创建日志、sink/warehouse，run 又直接调 warehouse，必须在任何这些副作用前选定 capture profile，确保没有临时打开正式 DB的窗口。

campaign writer 锁应绑定规范化路径、进程创建身份和 ownership；未知旧 child 退出状态则阻断新 writer。保存 attempt ID、各阶段、实际退出码原值、stdout/stderr、child exit confirmed 与独立 runner failure code。未知退出码保留 null，不填0；Dispose不等同退出。未来部署前重查旧进程，修改磁盘脚本不当成更新旧 runner。

写序：不可变身份/规则 evidence durable → frame及hash/epoch durable → committed frontier/collection status。receipt、首次可见、commit确认时间分别保存。短写、ENOSPC、fsync失败、部分尾行或hash冲突保留原字节，停止相应 writer，不推进已确认 frontier；status 写不了时留进程退出错误，不冒称干净停止。恢复只验证可信 committed prefix，未确认尾部保持隔离，不自动截断/覆盖/删除；新 run 明示断点且不回补 receipt。Windows目录项断电持久性仍需独立认证，进程故障 fixture不证明真实断电安全。

首阶段的站点/日期/token 集合、最大事件/token数、单帧/单规则响应字节、队列容量、backpressure超时、磁盘最低余量、campaign最大写入量和运行时限必须在显式配置中有有限上限及hash；无值/无限值拒绝启动。参数先以既有峰值和临时回放测量定稿，不能凭十站历史22-token样本声称容量足够。超限先留具体证据再停止对应新增写入/准入，不自动删除/压缩旧数据，不静默丢帧后标连续。持续运行额度与后续增加站点/日期在运维包内明确。

原始输出本身没有经济 cursor；旧 v2 非零无prefix hash cursor、不明库存/订单不迁移也不清空。后续规则补证获批只能从批准且证据可见的生效边界授予未来资格；更早 capture 仍为隔离历史，不能追认 forward fills/PnL、改变已提交前缀或重新消费旧 tape。正式 promotion/export 不在本主题内。

## 下一阶段唯一最小实现主题与文件范围

主题：**“带身份门的隔离原始 market capture，默认关闭”**。不包含 signal/Paper 门改造、registry 审批、正式迁移、Single Runs 验证或系统运维。

| 提议文件/函数 | 最小变化 | 兼容性边界 |
| --- | --- | --- |
| 新 collection_identity.py（名字待实现确认） | 纯身份核验/映射manifest/reasons；复用 Market、日期/桶解析等现有纯函数，不能调用放宽 verifier | 不修改 SettlementSpec/正式完整 verifier 语义；不把独立 identity 资格命名 VERIFIED |
| 新 collection_capture.py | capture sink/独立输出profile/manifest，有限campaign协调；复用既有连接与 durable primitives | 无通用权限框架、无 DB、无旧 schema导出 |
| market_stream.py 的构造/run/_connection/_records/_prepare_for_archive/_writer | 在副作用前注入 profile、原文接收hook、identity allowlist、epoch/L2失效、完整留存及独立status；逐处拆掉capture分支的 warehouse依赖 | 默认旧模式行为保持；共享逻辑修正须所有调用方回归，不能用新模式flag掩盖旧缺陷 |
| CLI 新显式 capture 入口；market_supervisor发现/reconcile可复用的窄部分 | 每个站点+日期独立候选和诊断，初始/轮换都走同一 identity门；正式 verifier结果仅记录，不移除 reasons | 不复用 direct market-stream 的旁路，不新增 signal active；同slug规则/身份变化必须留证 |
| settlement_diagnostics.py 的显式root/observer参数 | 复用input/parse/verification reasons；补 discovery候选终态和版本/identity绑定 | 原attempt记录不覆盖；新root不得暗转正式 data |
| 新临时fixture测试/必要既有测试 | 以下 D02 反例与全入口输出比较；共享完整回归及可选套件分列 | 本轮未新增测试或改 inventory；旧 801 不覆盖未来实现 |
| Windows runner | 此主题先不改/不执行现有runner；未来需为已验收入口接入显式profile/边界 | 实现验收与实际Task接管分别授权；复用attempt安全逻辑需再回归，不能直接运行历史运维脚本 |

若发现复用 bot 必须重写大部分运行栈，停止扩大主题，先提交进一步缩小的 adapter/sink 注入差异审核；不能以“已给方案”为由偷偷实施。当前的直接 market-stream 不符合此profile，禁止作为替代命令。

## 反例验收包（全部未来设计，未运行）

先冻结候选代码/测试/profile/registry指纹，所有输出在临时目录。先纯契约，再转换，再真实入口与故障，最后共享回归。不能 mock 身份/verifier/账户门为成功；HTTP/WS/时钟允许 fake，不触碰正式 daemon。

| ID | 输入/故障 | 路径与必须比较的输出 |
| --- | --- | --- |
| T01 | 可靠映射 + 十站现有 unresolved | 初始和轮换入口写 capture/evidence，完整 verifier仍拒绝；formal raw/DB/status/config/active/ledger/cursor均无新增，六类消费者无准入 |
| T02 | condition缺失/矛盾、token重复、YES/NO交换、长度不齐、跨event冲突、发现双候选/分页未明 | identity拒绝订阅，逐event reasons、原投影先持久化；不得字典覆盖或只丢一个市场后成功 |
| T03 | 同slug规则改变、关键句缺失、身份不变/变更各一例；today/tomorrow共存 | 每候选独立terminal诊断，rule新版本/UNKNOWN保留，身份冲突撤membership；重启后版本与资格不增加 |
| T04 | delta先于full、缺一个side、真实空簿、重连后delta、错token/condition、stale/maintenance/gap | capture原文不丢；只有新epoch合格full才建L2，状态/派生snapshot逐项降级，历史gap不消失 |
| T05 | 默认glob、改data-root、显式传capture文件/行、junction/大小写别名指向正式data | 覆盖上表所有reader家族；无正式计数/报告/天气join/clock/交易目标/ledger变化，错误不能变成功零覆盖；命中一个旁路即失败 |
| T06 | 较后批准或补证，先后分别追加到同可见前缀 | 过去订单/queue/fill/PnL/hash不变；不导出/重消费隔离旧数据；approved membership只从新边界开始，此主题无promotion实现 |
| T07 | evidence/frame/fsync/frontier/status前后故障，短写、磁盘满、崩溃重启 | 比较原始字节/hash、确认prefix/sequence、receipt首次值、attempt/result、锁与残尾；不删文件、不多writer、不越过未确认尾部 |
| T08 | ExitCode null、WaitForExit异常且child活着/无法查询、启动前unresolved marker | null不可成功；child未确认退出无再次启动；实际进程退出码与runner错误分别留证（仅临时测试child） |
| T09 | 09–20窗外、午夜/DST、不同站日期、23:59:30、主无值备用有值、deadline先到 | 每个收到frame仍留存，不启每小时裁剪；秒界/revision/no-data继续UNKNOWN，能力层资格独立 |
| T10 | 已有非零v2 cursor/库存与新campaign | 旧状态bytes/经济内容均不变；不重置游标/订单/累计成本；新run不擦除旧仓 |
| T11 | 默认参数、误选正式root、超事件/token/frame/queue/disk额度 | 默认不启新路径；超限拒绝/停止且有证据，无cleanup/retention/DB维护/系统配置变化 |
| T12 | 有效旧模式样本+所有 shared callers | 完整默认回归、可选依赖回归、lint和源码/未跟踪指纹；与新反例结果分列，不能只靠flag测试或旧801 |
| T13 | 新capture健康而正式market/status不变、遗留等待runner模拟 | 不发布可唤起下游的status，fake subprocess启动清单无signal/shadow/Paper；旧runner现实状态仍留待运维只读预检 |
| T14 | 丢失/坏/不支持的wire，raw写成功而L2失败，book hash/顺序证据不足 | 原始留存成功与healthy-L2分报；订阅数/心跳不能计为完整簿恢复，public group completeness仍UNSUPPORTED |

验收不是当前恢复许可。设计基于实际旁路已能指出必要隔离挡点，但当前没有capture实现与T01–T14输出证据，因此现状为 **可交接的条件方案 / 未实现未验收**。若任何reader可从新root/schema意外消费，或任何身份冲突不能隔离，保持新路径关闭并先修最窄边界。

## 独立授权与本轮停止点

1. 下一阶段只申请上述隔离capture实现及临时验证范围。
2. 规则语义/registry审批与官方定点补证分别进行；不等待 Single Runs 才实现原始留存，也不把它混入最小主题。
3. 已验证候选再另申请 market/supervisor限定运维：先查 signal/shadow等待链与实际旧runner，再明确隔离/接管；历史“最多30分钟、累计3次child失败停止”可作为新运维包上限沿用，但本文件不触发、不续借旧授权。成功保留采集的资源额度/退出条件需新包明示。
4. weather不操作，旧v2状态保留，Paper不启动；无正式数据写入/retention/回填、无commit/push。

本轮只新增三份交付文件。正式 data未全量指纹审计，Git空输出不能证明ignored数据整体未变；本轮操作清单没有访问/写入正式归档或DB，也没有进程/任务操作。前后277个限定既存文件指纹及Git检查详见证据JSON。正式恢复仍未获本轮授权，项目 NOT SEALED。
