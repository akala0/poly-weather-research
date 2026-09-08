# Codex 任务：修复 Paper V1 封板阻塞项，并引入 NautilusTrader 一致性验证轨

## 一、任务目标与当前判断

当前 `Paper V1` 的账户、独立 ledger、策略配置和基础订单状态机已经建立，完整 pytest 与 Ruff 曾通过；但 Claude 的人工代码审核确认 continuous 正式路径仍有若干会造成策略不运行、资金冻结、错误成交或错误恢复的阻塞问题。

本任务分为两条并行但权限严格隔离的工作轨：

### A 轨：修复本地 Paper V1，使其满足正式模拟盘启动前的封板标准

必须修复：

1. continuous 天气 observation 字段接线错误；
2. order/fill 与 account event 分裂持久化造成的 crash window；
3. snapshot 驱动的订单过期不释放 PaperAccount 预留；
4. max-hold 可能使用陈旧缓存盘口进行风险退出；
5. partial exit 过期/取消后无法按剩余目标重挂；
6. continuous trade 逐笔处理绕过同秒无序歧义保护；
7. follower 重启期间发生的 market generation 退订/关闭无法恢复；
8. 初始/累计实际买入 shares 的退出基准错误；
9. quality windows 只在启动时加载；
10. strictly-newer observation 时间链、完整 processor restart、status/readiness 和 22 项测试矩阵缺口。

### B 轨：直接引入成熟的 NautilusTrader，建立隔离的 conformance challenger

固定使用：

```text
nautilus-trader==2.0.0rc4
```

已人工验证：

- PyPI 存在 Windows AMD64、CPython 3.12 和 3.14 wheel；
- Python 要求为 `>=3.12,<3.15`；
- `PolymarketDataLoader`、`PolymarketFeeModel`、`SandboxExecutionClientConfig`、`BookType.L2_MBP` 可实际导入；
- `SandboxExecutionClientConfig` 可以配置 `$200 USDC` cash account、L2、trade execution、queue position、liquidity consumption 和 Polymarket fee model；
- public `PolymarketDataLoader.query_markets()` 无钱包、私钥或 API key 可工作；
- 许可证为 LGPL-3.0；应作为未修改依赖使用，不复制其 Rust 源码。

B 轨只能使用临时 fixture 和临时输出，不能成为正式 Paper V1 评分来源。只有 conformance 逐项通过后，未来才可另开任务评估 Paper V2 是否让 Nautilus 接管撮合/市场数据层。

---

## 二、绝对安全边界

本任务只能做代码、配置、测试和只读验证。必须始终满足：

- 不连接钱包、私钥、funder、API key、passphrase、签名器、Relayer；
- 不使用 Polymarket authenticated execution client；
- 不发送 POST/DELETE order，不订阅 User WebSocket；
- 不启动 `paper-spread-engine`；
- 不启动、停止或重启任何现有 daemon；
- 不重装或修改 Task Scheduler；
- 不修改、截断、迁移、补写或删除 `D:\poly\data` 下任何正式 ledger/cursor/status/archive/database；
- 所有新增测试、Nautilus probe 和 crash fixture 必须使用 `tmp_path` 或明确的临时目录；
- 旧 v2 ledger/cursor/status 保持原样；
- 新 Paper V1 生产路径仍不存在时，不得为了测试创建它们；
- `execution_enabled` 在配置、ledger、status、报告和所有新状态中必须严格为布尔值 `false`；
- 不得降低 freshness、season、settlement、quality、maintenance、receipt、token identity、无前视或真实 bid/ask 闸门来让测试通过；
- 不得用 midpoint、last trade、历史 price、`1-YES`、对侧 token 或结算 0/1 代替本 token 的真实执行盘口；
- 不得把测试 fixture、Nautilus challenger 或旧 v2 数据写成正式 Paper 成绩；
- 不得 commit 或 push；
- 当前 working tree 原本就有其他未提交工作；开始时先只读记录 `git status --short`，保留所有 pre-existing changes，尤其不得 reset、checkout、clean、stash、覆盖或“顺手修复”与本任务无关的文件（包括可能并行变化的 retention 文件）；
- 每次准备修改已存在文件前先阅读当前磁盘版本，不能依据旧 diff 或本任务文本中的行号覆盖后来变更。

如任何步骤只能通过真实鉴权或真实交易模块完成，立即停止并报告，不要绕过。

---

## 三、允许并要求复用的开源实现

### 1. NautilusTrader：直接作为固定 optional dependency 使用

上游：

- Repository: `https://github.com/nautechsystems/nautilus_trader`
- Version: `2.0.0rc4`
- License: LGPL-3.0
- Relevant APIs:
  - `nautilus_trader.adapters.polymarket.PolymarketDataLoader`
  - `nautilus_trader.adapters.polymarket.PolymarketFeeModel`
  - `nautilus_trader.adapters.sandbox.SandboxExecutionClientConfig`
  - `nautilus_trader.model.BookType.L2_MBP`
  - Nautilus Sandbox / Backtest matching engine

要求：

- 在 `pyproject.toml` 中增加独立 optional dependency，例如 `nautilus-eval`，不得放入默认生产 dependencies；
- 精确 pin `nautilus-trader==2.0.0rc4`；
- 更新 `uv.lock`；
- 更新 `THIRD_PARTY_NOTICES.md`，说明它作为未修改 LGPL 依赖，仅用于独立一致性验证；
- 业务模块不得 import `PolymarketExecutionClient` 或任何 execution factory；
- 测试必须证明 import 后没有加载项目已禁止的 Python execution modules；
- 不要求网络测试作为默认 pytest 的一部分；公共 query 只允许作为显式、跳过默认运行的手工 probe，并且不得访问 authenticated endpoints。

### 2. Spencer Fletcher `market-maker`：允许移植小型 MIT 纯逻辑和测试规格

上游：

- Repository: `https://github.com/spencerfletcher/market-maker`
- Inspected revision: `68f6c44730dab0772a3601072b0e00ee190f3a4c`
- License: MIT
- Relevant files:
  - `bot/core/durable.py`
  - `bot/core/maker_state.py`
  - `bot/core/feed_health.py`
  - `bot/kalshi/queue_tracker.py`
  - `tests/test_maker_state.py`
  - `tests/test_maker_recovery.py`
  - `tests/test_queue_tracker.py`

优先移植的是设计和测试规格：

- intent/transition 先持久化；
- corrupt/missing/unreadable/confirmed-empty 必须区分；
- pure `RecoveryPlan` / `assess_recovery` 风格；
- prior unclean run 不能静默覆盖；
- duplicate fill across restart；
- cumulative fill delta；
- explicit UNKNOWN evidence taxonomy；
- feed gap 必须污染跨越 gap 的活动订单证据。

如果逐行复制或实质改编上游表达：

- 保留相应 MIT copyright/permission notice；
- 在 `THIRD_PARTY_NOTICES.md` 写明具体上游文件和本地目标文件；
- 不复制 Kalshi complement ladder、live venue client、私有 order feed、真实撤单/平仓代码；
- 不直接复制 POSIX `fcntl` 锁，当前目标平台是 Windows；
- 本项目已有的 `runtime_safety.atomic_json_write()` 已包含 fsync、atomic replace、checksum、sequence、last-good，应优先复用，不再复制另一套 durable writer。

### 3. 只作设计参考，不作为本任务核心依赖

- `warproxxx/poly-maker`：借鉴 fill dedupe、in-flight guard、startup reconcile 测试；其 float 会计和 live REST authority 不可直接使用；
- `perpetual-s/polymarket-python-infrastructure`：借鉴 persist-before-submit/ambiguous outcome，不引入其 execution client；
- `Quentin-Piot/prediction-market-backtester`：不得用于正式 fill/PnL；它使用 float、midpoint/估算 spread，缺少本任务所需真实 L2 queue semantics。

---

## 四、A 轨详细要求：修复 Paper V1

## A1. 修复 continuous 天气 observation 接线与严格时间语义

当前真实 `align_weather_to_snapshots()` 把以下内容放在 `row["metadata"]`：

- `weather_observation_id`
- `weather_source_timestamp`
- `weather_received_at`
- `weather_observation_new`
- `weather_improving`
- `weather_worsening`
- `weather_unchanged`
- `weather_market_lag`

但 Paper runtime 当前从 row 顶层读取 ID，并期待生产 join 不生成的：

- `weather_receipt_eligible`
- `weather_observed_at`

要求：

1. 定义一个单一、typed/validated 的 Paper weather evidence 解析边界，不允许 production 与 fixture 使用两套字段；
2. continuous 从 `BookSnapshot.metadata` 读取真实 observation ID/source timestamp/received_at；
3. receipt eligibility 必须由：

```text
source_timestamp <= received_at <= decision_snapshot.timestamp
```

并结合 upstream join status/quality 判定，不能依赖 fixture 人工布尔值；

4. `PaperPortfolioState` 持久化并恢复：
   - consumed observation IDs；
   - last consumed `source_timestamp`；
   - last consumed `received_at`；
5. 第二档 observation 必须 strictly newer than 初始 signal/上一次有效观察；
6. 第四档必须使用另一个、未消费且同时按 `(source_timestamp, received_at, observation_id)` 严格更晚的 improving observation；
7. 被拒绝 observation 不能提前消费；
8. 所有 allowed/rejected tranche decision 必须记录：observation ID、source timestamp、received_at、decision timestamp、gate、完整 reason code、strategy version、config hash、execution_enabled=false；
9. fixture helper 必须调用与 production 相同的 metadata contract，不得继续构造虚假的 `weather_observed_at`/`weather_receipt_eligible` 让测试通过。

## A2. 建立单一的可恢复经济 transition 协议

当前 order snapshot/fill 与 account event 分两次 append。进程在中间退出会造成：

- order 已提交但未 reserve；
- fill 已写入 order，但账户未扣 cash/释放 reservation/增加库存；
- cancel/expire 已写入 order，但账户未 release；
- risk sell 已写入 order，但账户未记录 sell。

要求不要只用 `try/finally` 缩小窗口；SIGKILL、断电和进程崩溃必须可恢复。

实现要求：

1. 定义稳定的 logical transition identity，例如：

```text
submit:<order_id>
fill:<fill_id>
cancel:<order_id>:<terminal_timestamp>
expire:<order_id>:<terminal_timestamp>
strand:<portfolio_key>
```

2. 每个经济 transition 必须能从 durable facts 确定：
   - before order/account state；
   - order delta；
   - account delta；
   - strategy-state delta；
   - commit/completion 状态；
3. 优先设计一个单一 append-only `paper_transition` record，包含 order/account/strategy deltas；如果为兼容现有 `order`/`account_event` 保留多条记录，则必须增加 durable intent/commit marker 和启动 reconciliation，不能继续无检测地分裂；
4. append 后必须 flush + `os.fsync()`；
5. Paper ledger load 必须：
   - 空文件/坏 JSON/中间坏行/截断末行不可静默忽略；
   - 明确区分 recoverable incomplete tail 与 non-tail corruption；
   - 不足以唯一恢复时 durable discrepancy + HALTED；
6. startup 在处理任何新 input 之前必须 reconciliation：
   - 活动 BUY residual notional 总和 == `buy_reserved_usd`；
   - 每个 persisted fill 有且只有一个经济 account effect；
   - PaperAccount positions/shares/cost basis 与 order fills 一致；
   - terminal BUY 没有 residual reservation；
   - stranded position 与 account/state 一致；
   - strategy tranche/exit/consumed observation 与 durable transitions 一致；
7. 如果 persisted order/fill facts 足以唯一推出遗漏 account event，可以写一条幂等 `recovery_repair` transition 后继续；如果不唯一，必须 HALT，不能猜；
8. discrepancy 必须是 append-only durable evidence，重启后继续 HALTED；
9. 不允许恢复时从旧 v2 或在线 API 补数据。

建议复用本项目 `runtime_safety.atomic_json_write()` 增加一个 checksummed Paper recovery checkpoint，但 append-only transition ledger 仍是审计 authority。checkpoint 只能加速/证明边界，不能静默覆盖 ledger。

checkpoint 至少保存：

- schema/version/config hash/execution_enabled=false；
- clean_exit/run_id；
- committed transition frontier；
- account state；
- latest order state与 queue state；
- portfolio strategy state；
- station-day cumulative cost；
- source cursor/generation；
- feed continuity/evidence status。

## A3. 统一所有 terminal order transition 与资金释放

当前 `ShadowOrderEngine.process_snapshot()` 会自行 timeout/cancel，但 Paper processor 没有同步账户。

要求二选一，优先选更清晰者：

- Paper processor 成为 timeout/health terminal transition 的唯一 owner，底层在 Paper 模式不自行 expire/cancel；或
- 底层返回 typed order transitions，Paper processor 对每个 transition 统一写 economic transition。

必须保证：

- `RESTING` 与 `PARTIALLY_FILLED` BUY timeout 只释放 `remaining_shares * limit_price`；
- 成交库存与成本不变；
- snapshot path 与 global sweep path 语义相同；
- 重复 snapshot/sweep/restart 不重复释放；
- cancel/expire SELL 不改变 cash；
- 不跨 token 或 market-day 释放预留。

## A4. contemporaneous risk exit

`latest_snapshots` 不能无限期作为 max-hold risk exit 的真实当时盘口。

要求：

1. 明确定义并配置/常量化最大可接受风险退出 snapshot age，使用项目已有市场数据 cadence 推导，测试中显式注入；不得为了多平仓而放宽；
2. 只有同时满足以下条件才允许 shadow taker risk exit：
   - same portfolio key；
   - same token；
   - `as_of - snapshot.timestamp` 在 freshness boundary 内且不为负；
   - book complete；
   - native bid ladder 非空；
   - market/weather/season/quality/upstream health 全部合格；
   - snapshot 未跨已知 feed gap/generation boundary；
3. max-hold 到期但无 contemporaneous snapshot 时：取消 maker、释放 residual BUY reservation、标记 `STRANDED_UNPRICED`，PnL N/A；
4. closed/resolved/unsubscribed 场景不得通过 `replace(timestamp=as_of)` 把旧 snapshot 伪造成当前证据；
5. 风险退出只按真实 bid depth 和实际 taker fee；深度不足时剩余库存 stranded，不得当作全平。

## A5. 修复 partial exit residual retry

每个 exit stage 需要两个身份：

- stable logical stage identity；
- monotonically increasing attempt identity。

例如：

```text
logical: paper-exit:<portfolio>:<stage>
attempt: paper-exit:<portfolio>:<stage>:attempt:<n>
```

要求：

1. stage target 从 durable cumulative actual bought shares 计算；
2. 当前 stage residual：

```text
stage_target_shares - durable_stage_filled_shares
```

3. partial order timeout/cancel 后只按 residual 创建新 attempt；
4. attempt key 不能命中旧 terminal order；
5. 任何时刻所有 active SELL reserved shares + 已 sell shares 不得超过库存/原目标；
6. final stage 始终只卖当前全部剩余 inventory；
7. restart 后恢复 stage fills、completed 状态和 next attempt number；
8. 已 completed stage 不重复；
9. 处理 Decimal floor、dust/min-order residual；无法合法挂出的 dust 必须明确记录，而不是循环重试或超卖。

## A6. 明确累计实际买入 shares 的退出基准

按原任务文档语义，退出基准是该 portfolio 所有 tranche 的累计实际 BUY shares，不是计划美元，也不是第一笔 partial fill。

要求：

- 将含糊的 `initial_filled_shares` 重命名为准确字段，例如 `cumulative_bought_shares` 或明确的 `exit_basis_shares`；
- 每个 BUY fill 累计；
- SELL 不减少历史 exit basis，但减少当前 inventory；
- 四档前三档各目标为累计实际 BUY shares 的 25%，第四档清空剩余；
- 后续 BUY fill 若发生在已有 exit 之后，必须明确且测试 stage target 如何增量调整；不得倒退 completed stage 或重复卖出；
- position open time 只在第一次实际 BUY fill 设置，不能被后续 fill 重置。

## A7. 恢复 conservative trade ordering

continuous runtime 当前逐笔调用 `process_trade()`，绕过 `process_trades()` 的同秒歧义保护。

要求：

- 按 token 和 timestamp/sequence 分组；
- 无 sequence 的同秒多笔 trade 标记 `UNKNOWN_TRADE_SEQUENCE`，不得按文件顺序推进 queue；
- 有可靠 sequence 时按 sequence 排序；
- 相反方向、same token、receipt-verified/public-tape verified 才可推进；
- Data API 与 WebSocket 匹配后去重必须使用稳定 identity；
- ambiguous/duplicate/gap 必须进入 evidence counters/status，但不得伪造成 zero-trade clean evidence；
- 使用 batch API 或统一的 processor batch route，不能再在 follower 外层逐笔绕过。

## A8. generation、closed/resolved、rule/season invalidation 的持久恢复

要求：

1. cursor/checkpoint 保存上一 verified supervisor generation 和 active event set/hash；
2. 重启第一次读取 supervisor status 时，也必须把恢复仓位/活动订单的 event IDs 与当前 verified active set 比较；
3. 只有 supervisor status integrity verified 时才把 active-set removal 当作退订证据；unreadable ≠ empty；
4. 接入本地当时可见的 archived `closed/resolved` 证据；
5. 接入 settlement rule/season version invalidation；
6. 任一关闭证据触发：取消 maker、释放 BUY residual、按 A4 检查 contemporaneous native bid，否则 stranded；
7. generation/status 损坏或不可读时禁止新单，并使 `paper_score_eligible=false`；不能把不可读当作没有关闭；
8. 不允许事后在线查询补历史评分，但未来 continuous 运行可以消费当时本地归档的新证据。

## A9. 动态刷新 quality/maintenance

- 每个 poll cycle 检查 quality-window 文件是否发生安全可验证的更新；
- 重新读取失败时 fail closed，禁止新单并标记 status；
- 新增 maintenance/quality window 必须应用于之后的 snapshot 和 trade；
- 如果活动订单跨越新发现的 source gap/quality window，按规则取消或标记 evidence UNKNOWN；
- status 输出当前 upstream quality/maintenance、最近刷新时间、integrity、affected active orders。

## A10. status/readiness 完整性

补齐并测试原任务要求：

- capital utilization：必须给出清晰定义，至少包括当前 `(inventory cost + stranded cost + BUY reserved) / initial cash`；
- 资金占用时长：定义并报告 current capital-seconds/minutes 和累计 capital-time；使用事件时间/continuous as_of，不使用不可重现的隐式 wall clock；
- upstream quality/maintenance；
- feed continuity / UNKNOWN evidence counts；
- recovery state / clean exit / transition frontier；
- supervisor generation 与 active-set integrity；
- current risk-exit book age；
- `paper_score_eligible=false` 的所有原因必须逐项列出，而不是一个笼统 halted。

以下任一存在时必须 false：

- invariant/reconciliation failure；
- unfinished transition；
- corrupt/incomplete ledger；
- overdue active order；
- config mismatch；
- forbidden dependency/capability boundary failure；
- cursor/status/checkpoint integrity failure；
- old v2 contamination；
- execution_enabled 不是严格 false；
- supervisor/quality evidence currently unreadable；
- unresolved UNKNOWN source gap 影响正式订单/fill；
- stale snapshot 被用于或等待用于 risk exit。

---

## 五、B 轨详细要求：Nautilus conformance challenger

## B1. 架构与隔离

新增独立模块，建议命名：

```text
src/poly_weather/nautilus_conformance.py
```

它只能由显式测试/probe 命令调用，不得被默认 daemon、Paper V1 runner 或正式 status 自动 import。

要求：

- optional dependency 缺失时给出明确“安装 `nautilus-eval` extra”提示；
- lazy import；
- 不 import 或构造 `PolymarketExecutionClient`；
- 不读取任何凭据环境变量；
- 不发起 authenticated 网络请求；
- 默认只使用调用者传入的 in-memory/temp fixture；
- 所有输出写入临时目录或用户显式指定的非 `data/` 路径；
- 输出必须带：
  - `official_score=false`
  - `challenger_only=true`
  - `execution_enabled=false`
  - Nautilus version
  - config hash
  - local engine version/config hash
  - fixture hash
  - semantic differences。

## B2. 输入适配

实现最小适配器，把本地：

- token-native `BookSnapshot`/L2 levels；
- verified `TradeEvent`；
- event clock；
- instrument tick/size precision；

转换为 Nautilus BinaryOption/order-book/trade data。

要求：

- 不把 YES/NO 对侧互补价格转换为当前 token 价格；
- 本 token 一个独立 instrument；
- `ts_event` 与 `ts_init/receipt` 保留；
- 无 aggressor side、无 sequence、feed gap 等不得被转换成看似正常的 clean `TradeTick`；
- 只转换已经通过本地 receipt/token/public-tape verification 的成交；
- 不从 Nautilus public API 在线补测试 fixture。

## B3. Nautilus sandbox 配置

至少：

```text
venue = POLYMARKET
starting balance = 200 USDC
account type = CASH
OMS = NETTING
book type = L2_MBP
trade_execution = true
queue_position = true
liquidity_consumption = true
fee_model = PolymarketFeeModel
bar_execution = false
```

额外要求：

- 禁止默认 touch fill 成为官方等价结果；明确配置/包裹 `FillModel`，touch-only 不得 fill；
- `NO_AGGRESSOR` trade 不能用于正式一致性判断；过滤或明确记作 expected semantic divergence；
- L2 DELETE/UPDATE 推进 queue 的行为必须单独测试并标记，因为它比本地冻结的“只有 verified taker tape 消耗 queue”更乐观；
- challenger 不能因为这些差异反过来修改 Paper V1 标准。

## B4. conformance matrix

同一个 deterministic fixture 分别运行：

1. 当前修复后的 `ShadowOrderEngine`；
2. Nautilus Sandbox；

比较 normalized trace：

```text
order state
remaining shares
queue ahead / evidence state
fills and fill quantities
fees
cash
reserved cash（Nautilus 若无同口径则标 N/A，不伪造）
positions
realized PnL
terminal reason
event timestamp
```

必须至少覆盖：

1. post-only maker 提交不穿 ask；
2. 单纯 touch 不成交；
3. same-token opposite taker trade 清队列后 partial fill；
4. wrong-side trade 不成交；
5. wrong-token trade 不成交；
6. displayed trade size 不可被多个订单重复消费；
7. partial fill 后 residual 保留；
8. 有 sequence 的同秒 trades 确定性排序；
9. 无 sequence 的同秒 trades 本地 UNKNOWN，Nautilus 输入被拒绝/隔离；
10. L2 DELETE/UPDATE queue advance 显示为已知 semantic divergence，不能悄悄算通过；
11. Polymarket maker/taker fee 对照本地 fee helper；
12. `$200` cash account 不可透支；
13. SELL 不超库存；
14. GTD/外部 lifecycle timeout；
15. stale/out-of-order market data；
16. market close/resolution；
17. deterministic replay ID/result；
18. reset/restart 能力边界明确展示——如果 Nautilus Sandbox 不恢复 queue/account，则结果必须是 unsupported，不得声称通过。

结果分类：

```text
MATCH
EXPECTED_DIFFERENCE
LOCAL_BUG
NAUTILUS_LIMITATION
UNSUPPORTED
UNKNOWN
```

不得用简单 pass rate 掩盖高严重性差异。

## B5. public data smoke probe

可以新增一个默认不运行的显式命令或测试标记，只验证：

- public `PolymarketDataLoader` 可列出 active market；
- 不需要 credential；
- 不创建 execution client；
- 不写正式 data。

默认完整 pytest 不依赖网络。没有显式人工调用时不得联网。

## B6. 未来采用门槛

本任务不能把 Nautilus 设为正式 Paper V1 权威。

最终报告必须给出建议：

- 哪些 contract 为 MATCH；
- 哪些是可由 wrapper 消除的 EXPECTED_DIFFERENCE；
- 哪些需要自定义 Nautilus fill model 或上游改动；
- 哪些导致它暂时只能当 challenger；
- 是否值得另开 Paper V2 迁移任务。

不得把 Nautilus 的回测结果当作策略盈利证据。

---

## 六、必须新增的 crash/recovery 测试

除了原任务 22 项，至少增加以下 fault-injection 测试：

1. crash after order persisted, before reserve account event；
2. crash after fill persisted, before account fill event；
3. crash after cancel/expire persisted, before release event；
4. crash after risk sell persisted, before account sell event；
5. transition intent 存在但无 commit；
6. duplicate transition replay；
7. truncated final JSONL line；
8. malformed middle JSONL line；
9. corrupt checkpoint with valid ledger；
10. valid checkpoint behind ledger frontier；
11. checkpoint ahead of ledger frontier；
12. missing file 与 corrupt file 区分；
13. unclean prior run 不得被 fresh start 覆盖；
14. active BUY residual reservation 与 account mismatch；
15. account position 与 order fills mismatch；
16. unique deterministic recovery repair；
17. ambiguous mismatch durable HALTED；
18. full processor restart equality，一次性断言：
    - account cash/reserved/available/inventory/fees/PnL；
    - active orders/state/remaining/queue；
    - station-day cumulative cost；
    - consumed observation IDs/timestamps；
    - tranche index/fills；
    - exit stage targets/fills/completed/attempt；
    - position open time；
    - stranded/closed；
    - supervisor generation/active set；
    - evidence UNKNOWN states；
    - capital-time counters。

测试可使用显式 fault hook 或测试专用 exception 注入，但生产代码不能靠正常捕获异常假装具备 crash atomicity。

---

## 七、原任务 22 项逐条测试矩阵

必须新增一份机器可检查或人工清晰可查的矩阵，例如：

```text
docs/paper_v1_test_matrix.md
```

对 `CODEX_PAPER_SIMULATION_TASK.md` 第 408–429 行的 22 项逐条列出：

- requirement number；
- test function(s)；
- production code path；
- pass/fail；
- 是否包含 restart/crash boundary；
- 是否使用真实 production metadata contract；
- 尚存限制。

不允许用“完整 345 tests 通过”替代逐项覆盖证明。

原 22 项全部必须有对应测试；特别补齐：

- closed + healthy native bid risk exit；
- closed + no bid stranded/PnL N/A；
- stale/maintenance/quality no order；
- four-stage share conservation across partial/retry/restart；
- historical replay event clock；
- first-start tail bootstrap；
- forbidden dependency scan；
- every state/order/report execution_enabled=false。

---

## 八、对现有人工审核问题的回归测试

每个以下问题都必须至少有一个“修复前失败、修复后通过”的定向测试：

1. production aligned row 的 observation ID 从 metadata 正确进入第二档；
2. production 字段 `weather_source_timestamp`/`weather_received_at` 生效，不再使用 fixture-only 名称；
3. snapshot path timeout 释放 residual reservation；
4. 两小时前缓存 book 不可用于 max-hold exit；
5. partial exit timeout 后创建新 residual attempt；
6. continuous batch 拒绝 same-second unsequenced group；
7. follower 停止期间 supervisor 移除 event，重启首 cycle 仍关闭/stranded；
8. first BUY 多次 partial fill 和后续 tranche fills 正确累计 exit basis；
9. runtime 中途新增 quality window 后新订单被拒绝；
10. order/account split crash 被 repair 或 HALT，绝不静默继续。

---

## 九、实现质量要求

- 优先小型、纯函数、typed transition/recovery plan；
- 不在 continuous loop 中继续堆隐式 dict 字段；
- 定义稳定 enum/reason code；
- 金额、价格、份额、费用全部使用 `Decimal`；
- 不使用 float 会计；
- 同一业务事实只允许一个 authority；
- 写入和状态恢复必须幂等；
- 不捕获宽泛异常后静默返回 `None`；拒绝/异常必须有 ledger/status reason；
- 不用修改测试 fixture 来回避 production wiring；
- 不削弱旧 v2 测试和行为；
- 保持现有命名、注释密度和代码风格；
- 对从上游改编的代码保留清晰 provenance 和许可证归属；
- 如发现本任务设计与当前代码有冲突，先以安全、保守、可恢复、无前视为优先，并在最终报告说明。

---

## 十、执行顺序

严格按以下顺序：

1. 阅读本任务、原 `CODEX_PAPER_SIMULATION_TASK.md`、当前 Paper 文件与测试；
2. 阅读本地已有 `runtime_safety.py`，避免重复实现 durable writer；
3. 源码核对上述上游项目的固定 revision/tag；
4. 先写/补回归测试，确认能暴露已知问题；
5. 设计 logical transition/recovery schema；
6. 修复 A1–A10；
7. 完成完整 processor restart/fault tests；
8. 建立 22 项矩阵；
9. 增加 Nautilus optional dependency 与 notices；
10. 实现 B 轨离线 challenger；
11. 运行局部测试；
12. 运行完整测试和 Ruff；
13. 只读运行 `stream-status`；
14. 检查 `git diff` 和 `git status`，确认未触碰正式 data；
15. 写最终报告；
16. 不启动新模拟盘，不 commit，不 push。

如果 A 轨过大，可以在同一工作会话中分阶段实现，但不能用 B 轨的 challenger 通过掩盖 A 轨未完成。A 轨封板优先级高于 B 轨。

---

## 十一、验证命令

完成后运行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m poly_weather stream-status
```

Nautilus tests 必须使用包含 optional extra 的隔离环境；如选择同步当前 `.venv`，必须说明依赖变化并保证现有测试仍通过。建议另建临时环境验证 wheel/API，但项目 lockfile 必须准确反映 optional extra。

还需运行：

- Paper V1 22 项矩阵相关测试；
- crash/recovery fault-injection tests；
- Nautilus offline conformance matrix；
- forbidden execution import/capability tests；
- 检查生产 Paper V1 三个输出仍不存在（除非任务开始前已存在；若已存在只能只读报告，不能改）；
- `git diff -- data` 或等价只读检查，证明未修改正式 data。

不允许运行任何带真实交易能力的 smoke test。

---

## 十二、最终报告格式

最终回复必须包括：

1. 总判定：A 轨是否达到可封板、B 轨是否仅为 challenger；
2. 修改文件及关键行号；
3. 每个已知 P0/P1 的根因、修复与回归测试；
4. logical transition schema、持久顺序和 crash recovery 算法；
5. startup reconciliation 的所有不变量、repair 与 HALT 边界；
6. continuous/replay 生命周期和 contemporaneous risk-exit 时钟设计；
7. observation strictly-newer 的准确 tuple/比较规则；
8. cumulative BUY share basis 与四档 exit residual/attempt 语义；
9. generation/closed/resolved/rule/season/quality 的恢复路径；
10. status/readiness 新增字段和 `paper_score_eligible` 条件；
11. 原 22 项逐条测试矩阵摘要；
12. crash/fault-injection 测试摘要；
13. Nautilus 固定版本、wheel、optional dependency、许可证和 notices；
14. Nautilus conformance matrix：MATCH / EXPECTED_DIFFERENCE / LIMITATION / UNSUPPORTED；
15. 明确说明是否使用/加载任何 execution client、credential、wallet、private endpoint，预期全部为“否”；
16. 完整 pytest 数量与输出；
17. Ruff 输出；
18. `stream-status` 守护链结果，包括任何组件级 `last_error`，不能只报整体健康；
19. 明确声明旧 data、daemon、Task Scheduler、commit、push 是否发生变化，预期全部为“否”；
20. 确认正式 Paper 输出是否仍不存在、正式 orders/fills/round trips/N 是否仍为 0；
21. 列出尚存统计限制和 Nautilus 迁移限制；
22. 只有 A 轨全部满足时才给人工审核后的启动命令；若仍有任何封板问题，明确写“不要启动”，不得给出看似已授权的措辞。

---

## 十三、完成标准

只有同时满足以下条件才能称 Paper V1 技术封板：

- 所有人工审核的 P0/P1 均有修复和回归测试；
- production weather metadata contract 与 fixture 完全一致；
- 任意指定 crash boundary 后能确定性 repair 或 durable HALT；
- order/account/strategy/cursor/checkpoint 恢复一致；
- 所有 timeout/cancel/expire path 资金动作一致；
- stale cached book 不能风险平仓；
- partial exit 可安全 residual retry；
- same-second ambiguous trades 不推进 queue；
- supervisor restart gap 能恢复关闭证据；
- dynamic quality updates fail closed；
- 原 22 项测试矩阵无空项；
- 完整 pytest 与 Ruff 通过；
- 未触碰正式 data 或 daemon；
- `execution_enabled=false` 与 no-execution dependency/capability boundary 通过。

B 轨完成标准独立：

- fixed optional dependency 可安装；
- public-data/sandbox API 可导入；
- offline conformance matrix 可重复运行；
- 差异不会进入正式 Paper 成绩；
- 未使用真实执行或凭据；
- 对能否迁移到 Paper V2 给出基于测试的判断。

即使 B 轨全部通过，只要 A 轨未满足，仍不得启动 Paper V1。
