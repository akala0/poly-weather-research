# Codex 任务：可靠性下一阶段——证据可用性、健康真值与文档纠偏

任务日期：2026-09-08。

当前状态：Stage 2 已形成未提交候选改动，但**项目尚未封板，禁止启动 Paper**。本任务只处理下一阶段的代码、测试与文档整改；不授予 Paper 启动、正式数据治理、守护进程操作、系统维护或真实执行权限。

上位依据：

- `AGENTS.md`
- `CODEX_PROJECT_RELIABILITY_REMEDIATION_TASK.md`
- `docs/ENGINEERING_ACCEPTANCE_STANDARD.md`
- `docs/reliability_remediation_status.md`
- `docs/paper_v1_test_matrix.md`
- `docs/paper_v1_seal_validation_status.md`
- `docs/reliability_stage2_fingerprint_20260908.json`

本任务不覆盖或删除上述文件；冲突时以 `AGENTS.md`、安全边界和验收规范中更严格的要求为准。

---

## 0. 必须先理解的结论

本轮不是“把测试改绿”，而是回答三个不同问题：

1. 一条公共成交何时成为**持久、可审计、当时可见**的证据？
2. 数据源是否真的提供了足以证明同 token、同时间组已经完整封闭的语义？
3. 状态文件中的 integrity、PID、heartbeat 和 reported state 如何共同决定实际健康状态？

不得混淆以下结果：

- **安全阻断成功**：无法证明完整性时不成交、不推进 queue；
- **模型内核可用**：人工提供完整、有序输入时，queue/account 数学正确；
- **生产证据入口可用**：真实 producer 能生成并验证足够证据，正式入口才允许消费；
- **正式前向证据**：Paper 实际启动后产生的独立 forward 样本。本任务禁止产生此类证据。

当前 Stage 2 的 F02 是安全 containment：公共 Paper 入口保持零 queue consumption。除非本任务从数据源契约到 producer、持久化、验证器、恢复和测试全链路证明 group closure，否则不得把它描述为“恢复成交能力”。

如果数据源本身无法提供可证明的组完整性，正确结果是：

```text
UNSUPPORTED_GROUP_COMPLETENESS
accepted_queue_trade_rows = 0
formal Paper N = 0
PnL = N/A
NOT SEALED
```

这不是任务失败，而是诚实的工程结论。不得为了获得 MATCH、PASS 或非零 fill 而创建没有来源语义支撑的证明字段。

---

## 1. 绝对安全边界

### 1.1 严禁执行

本任务全过程：

- **不得启动** `paper-spread-engine`，包括 `--once`、replay、smoke 或任何有限时长运行；
- 不得启动、停止、重启、接管或修改任何 daemon；
- 不得读取、修改或重装 Task Scheduler；
- 不得修改 Windows 网络、代理、防火墙、证书、socket provider、注册表或系统 Python；
- 不得读取钱包、私钥、助记词、API key、passphrase、认证 header 或任何凭据内容；
- 不得导入、构造或调用 Polymarket 认证执行客户端；
- 不得调用 User WebSocket、Relayer、签名、POST/DELETE order 或真实下单/撤单接口；
- 不得进行外部网络 probe，包括公开市场 API 的“只读试一下”；
- 不得安装、升级、同步或删除依赖；只允许使用当前已安装环境和离线 lock 检查；
- 不得修改、迁移、压缩、删除、截断、回填或重写正式 `data/`；
- 不得把测试夹具写进正式 `data/`；
- 不得 commit、push、rebase、merge、reset、clean、checkout、stash；
- 不得清理现有 `.claude/`、临时 worktree 或其他未跟踪内容；
- 不得顺手修复本任务范围外的脏改动。

### 1.2 工作树保护

开始前必须记录：

```powershell
git -c safe.directory=D:/poly rev-parse HEAD
git -c safe.directory=D:/poly status --short
git -c safe.directory=D:/poly diff --stat
```

当前已知基线 HEAD 为：

```text
35ccb4530f6ec031d4b590e9ef688a49e5e60112
```

该值只用于判断工作树来源；若实际 HEAD 已变化，记录实际值并停止假定，不得 reset 回该提交。

开始前重读所有将修改的文件。另一进程可能已改变工作树；不得用旧缓存内容覆盖新改动。结束报告必须分别列出：

- 开始时状态；
- 本任务实际修改；
- 结束时状态；
- 哪些脏改动明确未碰。

### 1.3 不可放宽的语义

- `execution_enabled` 恒为严格布尔 `false`；
- 真实可成交价格只来自同 token 的原生 bid/ask/depth；
- 不用 midpoint、last trade、历史价格、对侧 token、`1-p`、`1-YES` 或结算值代替盘口；
- maker touch 不等于 fill；盘口数量下降不等于 taker trade；
- 无 receipt、无完整性、无 sequence、无健康质量或有 gap 时必须 UNKNOWN/UNSUPPORTED；
- 不改变 Paper 全局 `$200`、`20+30+50+100`、四档退出、900 秒 timeout、7200 秒 max hold、300 秒风险盘口年龄及 `cumulative_buy_cost`；
- 不更改研究阈值、天气策略、样本口径、Wilson 规则或已有 N/A 结论；
- 不迁移旧 v2 ledger，不将测试/Nautilus 结果计入正式 Paper 成绩。

---

## 2. 本轮总体目标与顺序

必须按以下依赖顺序实施，不要并行交叉重构：

1. **P0：先纠正文档与矩阵真值**；
2. **P1：定义并实现 durable receipt contract**；
3. **P2：研究并决定 group completeness 是否可证明**；
4. **P3：统一运行健康真值表**；
5. **P4：审计高风险共享 consumer**；
6. **P5：更新工程交接与最终证据**。

P2 的研究结论必须先于正向成交代码。不得先写 `group_complete=true`，再倒找理由。

本轮优先复用现有：

- `runtime_safety.py` 的 durable write、checksum、sequence、last-good 能力；
- 当前 Stage 2 的 row-level receipt、pending quality、canonical identity 和 containment；
- 当前 append-only Paper ledger 与 durable HALT；
- 现有 public tape/WS adapter 和测试 fixture；
- `docs/ENGINEERING_ACCEPTANCE_STANDARD.md` 的 S01–S13。

不要另造平行的弱持久化层、第二套时间定义或仅供测试的生产接口。

---

## 3. P0：立即纠正文档与测试矩阵

这一部分必须先完成，避免后续继续引用错误 PASS。

### 3.1 修正对象

至少检查并更新：

- `docs/paper_v1_test_matrix.md`
- `docs/paper_v1_seal_validation_status.md`
- `docs/reliability_remediation_status.md`

必要时只做最小范围更新：

- `CURRENT_CONCLUSIONS.md`
- `HANDOFF.md`
- `README.md`
- `AGENTS.md`

不得借文档更新改变策略或研究结论。

### 3.2 每项结果必须分层

矩阵中的每条测试都必须明确标为以下一种主要证据层级：

| 分类 | 含义 |
| --- | --- |
| `PRODUCTION_INGRESS` | 经真实生产调用路径、证据 admission、持久化和恢复入口验证 |
| `MODEL_KERNEL` | 仅证明在可信完整输入下的 queue/account/strategy 数学 |
| `CONTAINMENT_ONLY` | 证明不安全输入被拒绝或保持零成交 |
| `UNVERIFIED` | 尚无直接测试或生产路径证明 |
| `UNSUPPORTED` | 上游语义不足，当前明确不支持正向能力 |

“PASS”只能说明列出的断言成立，不能跨层扩张。例如：

- 私有 `_process_ordered_model_trades` 测试不能证明公共 Paper ingress 可成交；
- `accepted_queue_trade_rows=0` 是 containment，不是 fill capability；
- 单元测试 fill 不是正式 forward fill；
- producer 没生成 closure 证书时，validator 的人工 fixture 不能证明生产可达。

### 3.3 必须纠正的已知漂移

使用当前 `pytest --collect-only` 取得真实测试名，至少纠正以下旧引用和旧结论：

- `test_replayed_successful_prefix_is_idempotent_after_cycle_failure` 已不应被当作当前生产成交证明；核对当前实际名称与断言；
- `test_account_commit_oserror_halts_without_cursor_advance` 与当前证据写入测试的名称/层级不一致；
- downtime public tape 测试当前若断言零 fill，矩阵不得再写“消费一次并成交”；
- WS 后续匹配若只解决 match、未证明 group closure，矩阵不得再写“解除 UNKNOWN 并消费一次”；
- 旧 `429 collected / 394 diagnostic` 等计数必须用本轮实际命令结果替换，并标注命令、日期和排除项；
- 当前公共 Paper queue consumption 的真实状态必须直写，不能藏在脚注。

历史报告不得无痕改写。若旧结论已被推翻，标为 `SUPERSEDED`，说明由哪项新测试/契约替代。

### 3.4 文档自动一致性测试

新增最小测试或静态检查，至少保证：

- 文档引用的测试名能在 collect-only 清单中找到；
- 已删除/改名测试不再被列为当前权威；
- `MODEL_KERNEL` 不得包含“生产 follower 已成交”等措辞；
- 正式 N、PnL 和 Paper 启动状态只有一个一致来源；
- 测试总数由验证命令生成，不在多个文件中长期手填成互相冲突的“当前值”。

---

## 4. P1：F03 durable receipt contract

### 4.1 先写契约，后写代码

在代码修改前新增或更新一份证据契约文档，明确区分：

| 字段 | 定义 |
| --- | --- |
| `source_timestamp` | 上游事件声称发生的时间，不代表本地当时可见 |
| `request_started_at` | 本地发起请求的时间 |
| `response_received_at` | 完整响应返回到采集进程后的时间 |
| `receipt_committed_at` | receipt 事实完成 durable commit 的时间 |
| `receipt_journal_sequence` | receipt journal 的单调提交序号或等价 durable identity |
| `first_seen_at` | 该规范化经济事件第一次被本地可靠识别的时间 |
| `last_seen_at` | 后续重复观察时间；不得覆盖 first receipt |
| `file_written_at` | 派生 tape 文件写入时间，不得冒充 first receipt |
| `decision_visible_at` | 做无前视判定时可用的最晚必要证据时刻 |

必须诚实说明：

> HTTP 响应已到达进程内存、但进程在任何 durable write 前崩溃时，原始内存 receipt 不可能恢复。

因此，正式可用性不能早于 durable receipt commit。该窗口的正确处理是重试后使用更晚 receipt，不是用请求开始时间、源事件时间、mtime 或推测时间恢复早 receipt。

### 4.2 推荐实现：durable response receipt journal

实现专用的 append-only receipt journal 或语义等价机制。必须复用经审查的 durable writer；不得仅用普通 `open(..., "a")` 后假设安全。

每个 durable receipt fact 至少包含：

- schema/version 和 journal identity；
- source、collector run、request scope；
- request start 与 response receipt；
- query/filter/page 范围；
- 原始响应或规范化成员的稳定 digest；
- 成员 identity/digest/count；
- upstream quality、incident、gap 与错误状态；
- producer version/config hash；
- journal sequence/commit identity；
- `execution_enabled=false`（若进入 Paper 证据域）。

不得在 journal 中写凭据、认证 header 或 secret。

### 4.3 提交顺序和 crash 语义

把以下顺序写入代码注释、契约文档和测试：

```text
response complete
→ capture receipt
→ durable receipt journal commit
→ idempotent tape materialization
→ cursor/checkpoint commit
→ status/audit derivation
```

逐个定义：

1. **journal 前崩溃**：没有 durable receipt；重试使用新 receipt，不能追认旧时间；
2. **journal fsync 后、tape 前崩溃**：启动 reconciliation 从 journal 恢复同一 receipt；
3. **tape 后、cursor 前崩溃**：重放必须幂等，不重复成员、不改变 first receipt；
4. **cursor 后、status 前崩溃**：经济/证据事实已确认，status 可重建；
5. **坏尾行**：只有满足预声明 recoverable-tail 条件才可截断到 last-good；
6. **中间损坏、checksum 冲突、sequence 回退**：fail-closed/quarantine，不当空集合；
7. **journal 与 tape 冲突**：不得覆盖旧证据，记录 discrepancy 并阻断相关对象。

### 4.4 去重与兼容

- 重复刷新、无请求周期、失败重试不得改变已 durable 的 first receipt；
- Decimal 表示、sequence 表示或 JSON 格式变化不得创建第二个经济事件；
- API/WS 别名不得创建第二个 queue consumption；
- legacy 行没有可靠 receipt 时保持 `historical_only/UNKNOWN_RECEIPT`；
- 不用当前扫描时间、文件 mtime、最终归档时间或 source timestamp 补 receipt；
- 不批量改写正式旧 tape；迁移建议只能写文档，执行需另行授权。

若 journal 无法安全接入现有 collector，则保留公共 queue 禁用，明确报告该阻塞；不得降级为普通 JSON 状态文件。

---

## 5. P2：F02 可证明的 trade-group completeness

### 5.1 必须先提交 source-semantics 决策记录

在写任何正向 admission 代码前，新增一份本地决策记录，至少回答：

- Data API 的过滤、排序、分页、上限、迟到与重复语义是什么？
- 查询区间闭开边界是否明确？
- 某页为空是否证明此前时间组已经完整？
- 是否存在稳定且唯一的上游逐笔 ID？transaction hash 是否只是多 fill 共享身份？
- WS sequence 是交易所级、连接级、消息级，还是本地归档序号？
- 重连后 sequence 是否连续、可比较、可证明无 gap？
- API 和 WS 对同一成交的 alias 如何确认？
- 什么时候可证明同 token、同 UTC 时间组不会再出现合法 sibling？

优先查项目内已有官方 API 归纳、当前 adapter 与已保存 fixture。不要暗猜接口，不做外部网络 probe。若本地资料不足，结论必须是“尚无法证明”，并列出将来需要哪份官方契约；不得自行发明保证。

以下均**不是** group closure 证明：

- 等待 N 秒；
- 一次 poll 结束；
- 一页返回少于 limit；
- 文件结束或日期分区结束；
- 当前批只有一条；
- 看到了更大本地 sequence；
- mtime 不再变化；
- caller 传入 `group_complete=true`；
- 测试 fixture 手工构造一个“证书”；
- 下游 validator 自己给输入盖章。

### 5.2 只有上游语义足够时，才允许实现 closure certificate

若且仅若决策记录证明数据源提供可验证封闭边界，才实现内部 `TradeGroupClosure`（名称可按现有架构调整）。证书必须由受信 producer 生成，不得作为公开策略 API 的任意入参。

建议字段至少包括：

- schema/version；
- source、collector run、producer version/config hash；
- event/market/token scope；
- UTC group key 与精度；
- query interval 和闭开边界；
- 完整分页证明、page count、page digests；
- response receipt 与 durable journal commit；
- 超过该 group 的权威 watermark；
- member stable identities、count 和 digest；
- sequence coverage/gap 状态；
- quality/maintenance/incident 状态；
- closure reason 和 source-contract reference；
- certificate digest/commit identity。

consumer 必须重新计算成员集合与 digest。仅字段存在不等于验证通过。

### 5.3 正向 admission 的全部条件

若实现正向路径，必须同时满足：

- 同 token、同 event/market scope；
- opposite aggressor；
- 精确 Decimal price/size；
- receipt-safe 且不晚于决策；
- API/WS alias 一致或不存在冲突；
- 所有分页/查询成功；
- 没有 cursor gap、archive gap、reconnect gap；
- 质量区间完整健康；
- 成员身份稳定且 sibling 可区分；
- group closure 来自受信 producer 并通过 consumer 重验；
- canonical trade identity 尚未 durable consumed；
- queue consumption 与成交后 order state 仍是同一 durable fact；
- restart 后 exactly once。

任何一项不足都保持具名 UNKNOWN，不能进入 `_process_ordered_model_trades`。

### 5.4 迟到 sibling 和已发布结果

必须预先选择并记录策略：

- 若 source contract 能证明 closure 后绝不出现合法 sibling，迟到行是 corruption/contract breach，相关对象 HALT/quarantine；
- 若 source 允许迟到，则原 closure 不成立，不能开放正向消费；
- 已 append 的 ledger 事实不得重写；若后续证据使评分前提失效，追加 invalidation/discrepancy，并使相关成绩不可发布。

不能“把新 sibling 忽略掉”来保持测试通过，也不能倒扣历史 queue 来伪造事务回滚。

### 5.5 允许的诚实终态

如果没有可靠 source closure：

- 保持 `PaperSpreadProcessor.process_trade/process_trades` 的公共入口零经济 mutation；
- 保持 `accepted_queue_trade_rows=0`；
- 记录 `UNKNOWN_TRADE_GROUP_COMPLETENESS` 或 `UNSUPPORTED_GROUP_COMPLETENESS`；
- `_process_ordered_model_trades` 保持 private/test-only；
- 用静态 callsite 测试证明生产路径无法绕过；
- 所有正向 fill 测试标为 `MODEL_KERNEL`；
- A 轨继续 NOT SEALED。

不要把这种终态称为“F02 已恢复”；可称“F02 污染风险已 containment，正向能力 unsupported”。

---

## 6. P3：F06 单一运行健康真值表

### 6.1 分离状态维度

统一 `runtime_safety.read_status()`、`stream-status`、runner/readiness 和相关 consumer 的语义，至少分开输出：

- `integrity_state`：checksum/schema/last-good 是否可信；
- `reported_state`：状态文件自报 running/connected/reconnecting 等；
- `pid_state`：missing/alive/dead/reused/ownership_mismatch/unknown；
- `heartbeat_state`：missing/invalid/future/fresh/stale；
- `dependency_state`：必要上游是否健康；
- `progress_state`：cursor/sequence 是否前进或 stalled；
- `effective_state`：由固定真值表派生的最终状态；
- `reasons`：具名原因列表。

`verified` 只表示文件完整性，不表示进程存活、心跳新鲜或采集正常。

### 6.2 固定真值要求

在代码前先把真值表写进规范/测试。至少满足：

- dead PID → effective stopped，无论 reported state；
- missing PID 对 live reported state → unknown/unhealthy；
- PID reused 或命令归属不符 → ownership mismatch，不算 alive；
- missing heartbeat 对 live state → unknown/unhealthy；
- heartbeat 格式错误 → invalid，不算 fresh；
- heartbeat 超过预声明 future-skew tolerance → clock-skew/unknown，不得 `max(0, age)` 变成 0 秒新鲜；
- stale heartbeat → stale/stalled，不因 PID alive 变 running；
- reported reconnecting + dead PID → effective stopped；
- integrity verified + dead PID → stopped with verified file，不能显示 healthy；
- status missing/corrupt/unreadable 分别处理，不能都当 stopped 或 empty；
- dependency unhealthy 时，下游不能仅靠自身 PID/heartbeat 宣称 ready。

时钟容差必须在看测试结果前固定并配置化/常量化，不能按当前状态文件调参。

### 6.3 Windows PID 安全

- 复用项目现有 Windows `OpenProcess`/命令归属检查；
- 不用 `os.kill(pid, 0)` 作为 Windows 唯一 PID 判断；
- 测试只可使用当前测试进程或测试创建并负责回收的子进程；
- 不得探测、终止或接管现有 daemon PID；
- PID 存活但命令不匹配时按 ownership mismatch 处理。

### 6.4 消费方一致性

以下入口不得各自实现冲突的健康判断：

- `stream-status` CLI；
- Paper readiness/status；
- signal/shadow 上游闸门；
- Windows runner 的状态判断；
- 文档中的健康结论。

允许展示层增加文字，但其 effective health 必须来自同一规范化结果。

---

## 7. P4：高风险共享 consumer 审计

完成一份 producer→converter→consumer→ledger→restart→report 调用表，至少覆盖：

- `load_event_trade_tapes`
- `_public_trade_events_from_file`
- `build_shadow_trade_events`
- `ShadowOrderEngine.process_trade/process_trades`
- Paper V1 follower
- v2 shadow follower
- QUIET maker/replay
- complement-pair replay
- Nautilus challenger
- public trade analytics/reporting

逐个回答：

1. 使用哪个 event/source/receipt/availability clock？
2. 是否仍有 file-level `fetched_at`、mtime、event time 回退？
3. 如何处理 legacy missing receipt？
4. API/WS alias 是否会重复计数或重复消费？
5. 是否要求 group completeness；若不要求，是否只是研究统计而非 queue fill？
6. quality/incident/gap 是否贯穿 normal、pending、restart、replay？
7. Decimal identity 是否会因字符串/sequence 表示改变？
8. crash 后 authority 是 ledger、journal、cursor 还是 status？
9. 输出是否可能把 UNKNOWN/UNSUPPORTED 报成零成交事实或 clean score？

优先完成原整改任务中的 Q01/Q03/Q05/Q06。Q02/Q04/Q07/Q08 若未充分检查，必须保留为 pending，不得写“项目级全部完成”。

共享函数的修复必须带相关 consumer 回归。不得为 Paper 修一条路径，却让 v2/complement/QUIET 语义悄悄改变。

---

## 8. 保留 F04/F05 的安全边界

### 8.1 F04 tape/cursor 损坏隔离

保留现有 missing、empty、corrupt、unreadable、recoverable-tail 区分和排他写入。新 receipt journal 必须与该模型兼容：

- corrupt 不当 empty；
- 原字节保留；
- cursor 不越过争议证据；
- 单对象失败不无依据污染其他对象；
- 写入失败不能只更新内存状态后继续。

短写、目录 fsync 平台差异、独立进程 writer ownership 若未验证，应继续列为限制。

### 8.2 F05 retention

- 不得实际运行正式 retention；
- 不得为了测试关闭 market collection；
- 在没有 writer seal/ownership 证明前，继续禁止压缩后删除源文件；
- containment 可以保留，但不能报告“归档功能完整修复”；
- positive writer-seal 协议仍是独立任务，除非本轮能在不触碰正式 data/daemon 的情况下完整设计和临时验证。

---

## 9. 必须新增或补强的测试

所有测试写临时目录。测试名可调整，但最终矩阵必须能映射到以下 ID 和实际入口。

### D：文档和矩阵

| ID | 必须证明 |
| --- | --- |
| D01 | 矩阵引用的测试名全部存在；旧名称不再充当当前权威 |
| D02 | 每个成交相关测试标明 `PRODUCTION_INGRESS`、`MODEL_KERNEL`、`CONTAINMENT_ONLY`、`UNVERIFIED` 或 `UNSUPPORTED` |
| D03 | 当前公共入口零消费时，文档不得宣称 follower 正向 fill |
| D04 | 当前测试数、排除项和日期由验证输出生成且互相一致 |

### R：receipt durability

| ID | 必须证明 |
| --- | --- |
| R01 | response 完成但 journal 前崩溃，不伪造早 receipt；重试使用更晚 receipt |
| R02 | journal fsync 后、tape 前崩溃，重启恢复完全相同 first receipt |
| R03 | tape 后、cursor 前崩溃，重放不重复、不漂移 receipt |
| R04 | cursor 后、status 前崩溃，status 可由 durable facts 重建 |
| R05 | duplicate/no-request refresh 保持 first receipt，只更新允许的 last-seen 字段 |
| R06 | 请求失败、分页失败、重试和 late response 不把 request start 当 response receipt |
| R07 | journal 坏尾行按固定规则恢复；中间损坏/checksum 冲突 fail-closed |
| R08 | legacy missing receipt 保持 historical-only，mtime/file time 不可提升资格 |
| R09 | Decimal/sequence/JSON 表示变化不改变经济 identity |

### G：group completeness

| ID | 必须证明 |
| --- | --- |
| G01 | 未证明封闭的单条/同秒组始终 UNKNOWN，跨 poll、跨文件、跨重启均零消费 |
| G02 | caller 自带 bool/JSON closure marker 被拒绝，不能给策略入口授权 |
| G03 | 只有 producer 生成且 consumer 重验的 closure 才可能进入正向路径；若 source 不支持则明确 skip/UNSUPPORTED |
| G04 | 任一分页失败、gap、质量 incident、成员 digest 不符或 token scope 不符都使证书无效 |
| G05 | API/WS alias、补 sequence、重复投递和重启只产生一次 canonical observation/consumption |
| G06 | late sibling 按预声明 contract breach/unsupported 策略处理，不能静默忽略 |
| G07 | queue-only、partial fill、full fill 的 trade consumption 与 post-trade order state 原子恢复 |
| G08 | 静态/运行测试证明生产代码没有调用 private model kernel 的旁路 |

若 source semantics 不支持 G03 的正向条件，G03 的正确断言是生产入口保持 `UNSUPPORTED`，不是构造假成功。

### H：健康真值

| ID | 必须证明 |
| --- | --- |
| H01 | missing、invalid、future、fresh、stale heartbeat 分别得到固定结果 |
| H02 | missing、alive、dead、reused、ownership mismatch PID 分别得到固定结果 |
| H03 | reported running/connected/reconnecting/degraded/stalled/stopped 与实际 PID/heartbeat 组合符合真值表 |
| H04 | integrity verified 不会掩盖 dead PID 或 stale heartbeat |
| H05 | CLI、read_status、Paper/readiness 对相同 fixture 给出一致 effective state/reasons |
| H06 | future skew tolerance 的边界值确定且不会被 clamp 为 age=0 |

### X：共享 consumer 和安全

| ID | 必须证明 |
| --- | --- |
| X01 | API-only、WS、pending、restart、replay 对 receipt/quality 资格一致 |
| X02 | v2/complement/QUIET 的既有语义没有被 Paper 私有修复意外改变 |
| X03 | no native bid 时，各种 surrogate price 都不能产生风险 SELL 或 PnL |
| X04 | `execution_enabled` 严格 false，执行依赖扫描 clear，无认证客户端 import/construct |
| X05 | 所有测试输出在临时目录，正式 Paper ledger/cursor/status 不存在，`data` Git diff 为零 |

测试不得：

- monkeypatch validator 直接返回 true 来证明 producer 可达；
- 通过 fixture 手填 closure 就称生产支持；
- 用 private `model_trade` 证明 production ingress；
- xfail/skip 一个真实失败后把矩阵写 PASS；
- 以睡眠时长替代 source closure；
- 依赖外网或正式 data 写入。

---

## 10. 实施质量要求

- 保持类型、Decimal、UTC、schema 和 reason code 明确；
- identity/canonicalization 只在一个共享模块定义，避免 producer/consumer 各自实现；
- 新 schema 必须有版本、兼容策略和 corrupt handling；
- 所有持久化转换记录 authority 和 crash point；
- 所有 UNKNOWN/UNSUPPORTED reason 可统计、可恢复、可报告；
- 不捕获宽泛异常后继续推进 cursor；
- 不把 `status` 当经济权威；
- 不用注释宣称保证，必须由代码路径和故障注入证明；
- 不复制 Nautilus 或其他上游源码来绕过本地证据契约；
- Nautilus 继续 optional challenger，`official_score=false`、`challenger_only=true`、`execution_enabled=false`；
- 若修改可选依赖，仅允许为现有 lock/metadata 一致性所需的最小改动，禁止安装和联网更新。

---

## 11. 验证顺序和命令

优先使用项目 `.venv`。命令中的文件列表应按实际新增测试调整，并在报告中给出原始命令和 exit code。

### 11.1 收集与定向测试

```powershell
.venv\Scripts\python.exe -m pytest --collect-only -q -p no:cacheprovider
```

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_reliability_evidence.py tests/test_paper_trade_evidence.py tests/test_paper_recovery.py tests/test_paper_runtime_boundaries.py tests/test_shadow_runtime.py -p no:cacheprovider --tb=short
```

如新增 receipt/health/doc-contract 专项文件，把它们加入定向命令，不得只跑旧文件。

### 11.2 可选 Nautilus

仅使用已经安装的 optional 环境；不得同步依赖：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider --tb=short
```

若当前 `.venv` 未安装 optional extra，记录 `not installed`；不得联网安装，也不得把 skip 算 PASS。

### 11.3 诊断默认套件

在 F07 系统 socket 阻塞仍存在时，允许继续运行明确排除五个已知 socket-dependent 文件的诊断套件，但必须列出排除清单，且不得称完整 pytest：

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --tb=short --ignore=tests/test_fees.py --ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py --ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py
```

### 11.4 完整套件

做一次有界完整套件尝试，启用可诊断超时/堆栈输出。只允许终止本次测试命令创建的进程，不得终止其他 Python、daemon 或系统进程。

若再次卡在本机 socket：

- 保存具体测试名和堆栈；
- 在固定超时后终止本次测试进程；
- 不修改系统；
- 结果写 `FULL SUITE BLOCKED`，不是 PASS；
- F07 保持 open；
- 项目不得封板。

### 11.5 静态与工作树检查

```powershell
.venv\Scripts\python.exe -m ruff check src tests
```

```powershell
uv lock --check --offline
```

```powershell
git -c safe.directory=D:/poly diff --check
```

```powershell
git -c safe.directory=D:/poly status --short -- data
```

```powershell
git -c safe.directory=D:/poly diff --stat -- data
```

还必须用包含 hidden/ignored 名称的只读搜索确认正式 Paper V1 ledger/cursor/status 不存在。不得读取无关大归档或宣称做了全量字节哈希。

`stream-status` 不是本任务必须验证项。若只读执行，也只能报告当时观测，不得启动进程或把旧状态写成长期事实。

---

## 12. 必交付文件

至少交付：

1. receipt/availability durable contract 文档；
2. group completeness source-semantics 决策记录；
3. 健康状态真值表；
4. 更新后的 `docs/paper_v1_test_matrix.md`；
5. 更新后的 `docs/reliability_remediation_status.md`；
6. 更新后的 `docs/paper_v1_seal_validation_status.md`；
7. 与实现对应的故障注入、生产入口和共享 consumer 测试；
8. 本轮 workspace fingerprint，包含文件 hash、HEAD、开始/结束状态、测试命令与结果；
9. 最终修复报告。

文件名可在现有文档体系内合理选择，但不得另建互相矛盾的第二份“唯一权威”。必须在交接文档中说明每份文件的 authority 与适用范围。

---

## 13. 最终报告格式

最终报告必须按以下顺序，逐项给出事实、文件/测试路径和限制：

1. 安全声明；
2. 开始/结束 HEAD 与 workspace 状态；
3. 本任务实际修改文件；
4. P0 文档纠偏：被推翻的旧声明；
5. receipt 字段定义和 authority；
6. receipt journal 提交顺序；
7. 每个 receipt crash point 的恢复结果；
8. legacy receipt 处理；
9. source-semantics 调查证据；
10. group completeness 最终决定：supported 或 unsupported；
11. 若 supported，closure certificate 与 verifier 字段；
12. 若 unsupported，证明公共入口保持零消费；
13. late sibling、gap、分页失败和 alias 政策；
14. queue/account exactly-once 结果；
15. 健康状态真值表和 consumer 一致性；
16. 高风险共享 consumer 调用表与发现；
17. F04/F05 保留状态；
18. Q01–Q08 中完成、部分、未开始项；
19. 定向测试原始结果；
20. collect-only、诊断套件、完整套件原始结果；
21. Nautilus、Ruff、lock、diff 检查；
22. 正式 `data` 与 Paper 文件检查边界；
23. runtime/daemon/Task Scheduler 未操作声明；
24. 凭据、网络和真实执行未使用声明；
25. Git 未 commit/push 声明；
26. 正式 orders/fills/round trips/N/PnL；
27. 剩余 blocker；
28. 最终判定。

不得只给“测试通过”摘要。每个关键结论必须能追到具体 producer、consumer、故障测试或决策记录。

---

## 14. 完成标准与允许结论

### 14.1 能力恢复候选

只有同时满足以下条件，才可写“公共 trade ingress 技术候选可用”：

- 本地可查的权威 source contract 确实支持 group closure；
- producer 真实生成 durable closure；
- consumer 独立重验，不接受 caller 自证；
- receipt、pagination、gap、quality、identity、alias 和 restart 全部通过反例；
- 生产调用路径有正向和负向测试；
- queue/order/account exactly once；
- 矩阵无跨层夸大；
- 完整默认测试真正结束并通过；
- 独立复核尚需另做。

即使全部满足，也只能写：

```text
TECHNICAL SEAL CANDIDATE
DO NOT START PAPER WITHOUT SEPARATE USER AUTHORIZATION
formal N = 0
PnL = N/A
```

### 14.2 安全 containment 完成但能力不支持

若 source closure 无法证明，允许本任务正常完成为：

```text
F02 contamination risk contained
positive public queue consumption unsupported
accepted_queue_trade_rows = 0
A track NOT SEALED
DO NOT START PAPER
```

该结果不得被自动化或报告转换为 FAIL 后诱导放宽证据要求。

### 14.3 完整套件仍被系统环境阻塞

如果定向/诊断测试通过，但完整套件仍卡在 socket：

```text
TARGETED VALIDATION PASSED
FULL SUITE BLOCKED BY UNRESOLVED LOCAL SOCKET ENVIRONMENT
PROJECT NOT SEALED
```

不得安装替代 Python、改防火墙、改代理、全局 monkeypatch socket 或删除测试来消除阻塞。系统维护需要用户另行授权。

### 14.4 永久独立授权

以下事项永远不由本任务结果自动授权：

- 启动 Paper；
- 操作 daemon/Task Scheduler；
- 修改正式 `data/`；
- 执行 retention；
- 修复系统网络/socket；
- 安装依赖；
- 接入凭据或真实交易；
- commit/push。

最终默认结论必须保持：**正式 Paper N=0、PnL=N/A；在完整套件和独立复核完成前，项目未封板，不要启动模拟盘。**
