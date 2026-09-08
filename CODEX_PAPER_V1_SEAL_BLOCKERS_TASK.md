# Codex 任务：关闭 Paper V1 第二轮封板阻塞项

## 0. 任务结论与执行口令

**不要启动模拟盘。**

上一轮 hardening 确实修复了 production weather metadata、普通 order/account recovery、timeout release、partial-exit retry、supervisor restart recovery、动态 quality reload 等一批问题，但独立高强度复核仍确认 A 轨存在会造成丢证据、重复成交、HALT 失效、时间穿越或策略执行错误的阻塞项；B 轨虽然保持隔离，却有多项 conformance case 没有真正比较相同语义。

本任务只允许修改代码、配置、测试和文档。完成后最多把 A 轨标为“技术封板候选”，不得自行启动 `paper-spread-engine`。

当前独立基线（仅供复核，不得硬编码）：

- 默认测试：`383 passed, 1 skipped`；\n- 安装 `nautilus-eval` 后，`tests/test_nautilus_conformance.py`：`4 passed`；
- 产品源码 Ruff（排除保留中的 `.claude/worktrees`）通过；
- `uv lock --check` 通过；
- `data/` Git diff 为零；
- 未发现正式 `paper_spread_v1` ledger/cursor/status；
- 本任务开始前模拟盘没有启动。

---

## 1. 绝对安全边界

必须全部遵守：

1. 不连接钱包、私钥、funder、API key、passphrase、签名器、Relayer；
2. 不导入、构造或调用 `PolymarketExecutionClient`、execution factory 或 authenticated client；
3. 不发送真实 POST/DELETE order，不订阅 User WebSocket；
4. 不启动 `paper-spread-engine`，包括 `--once`；
5. 不启动、停止、重启或重装任何 daemon / Task Scheduler；
6. 不做网络 probe；Nautilus 只用本地 deterministic fixtures；
7. 不修改、截断、迁移、补写或删除 `D:\poly\data` 下任何正式文件；
8. 不修改旧 v2 ledger/cursor/status；
9. 不为了测试创建正式 Paper 路径；所有测试只用 `tmp_path` 或系统临时目录；
10. `execution_enabled` 在所有配置、ledger、status 和报告中必须严格为布尔 `false`；
11. 不得用 midpoint、last trade、历史 price、对侧 token、`1-p` 或 settlement 0/1 代替本 token 原生 bid/ask；
12. 不降低 freshness、quality、maintenance、season、settlement、receipt、sequence、token identity 或 no-lookahead gate；
13. 不 commit、不 push；
14. 当前 working tree 有既存未提交内容。开始时记录 `git status --short`，不得 reset、checkout、clean、stash、覆盖或顺手修改无关文件；
15. 修改任何已存在文件前，重新读取磁盘当前版本，不能依赖旧 diff 或本文行号；
16. 如果必须触碰真实凭据、真实执行或正式数据才能完成，立即停止并报告。

建议先保存：

```text
git status --short
git diff --stat
git status --short -- data
git diff --stat -- data
```

---

## 2. 修改范围

预计主要涉及：

- `src/poly_weather/paper_account.py`
- `src/poly_weather/paper_spread_runtime.py`
- `src/poly_weather/shadow_orders.py`
- `src/poly_weather/shadow_runtime.py`
- `src/poly_weather/runtime_safety.py`
- `src/poly_weather/polymarket_status.py`
- `src/poly_weather/weather_market_join.py`（仅在确有必要时）
- `src/poly_weather/nautilus_conformance.py`
- `configs/paper_spread_strategy_v1.json`
- `tests/test_paper_account.py`
- `tests/test_paper_spread_runtime.py`
- `tests/test_paper_runtime_boundaries.py`
- `tests/test_paper_recovery.py`
- `tests/test_paper_cli.py`
- `tests/test_nautilus_conformance.py`
- 必要的新 Paper 专用测试文件
- `docs/paper_v1_test_matrix.md`
- `THIRD_PARTY_NOTICES.md`
- `pyproject.toml`
- `uv.lock`

不要重构无关研究模块，也不要修改 retention/daemon 代码，除非本任务列出的 production path 确实直接依赖它。

---

# A 轨：Paper V1 封板阻塞项

## A1. 轮询 cursor 必须事务化，异常时不能丢输入

### 已确认缺陷

`_incremental_jsonl_rows()` 在读取 checkpoint/weather/ws JSONL 时直接修改传入的 cursor position。continuous cycle 随后若在以下任一位置抛出已捕获异常：

- `BookSnapshot.from_mapping()`；
- `processor.process_snapshot()`；
- `processor.process_trades()`；
- `processor.sweep_lifecycle()`；

当前逻辑仍会执行 `cursor.save()`。这样未完成处理的 rows 会在重启后永久跳过。

### 必须实现

1. 每轮创建 staged cycle state，至少包含：
   - `ShadowCursor.sources`；
   - `pair_latest`；
   - `pair_last_emitted`；
   - weather join state；
   - 本轮 public-trade file state/watermark；
   - 其他由读取动作直接推进的 durable source position。
2. `_incremental_jsonl_rows()` 只能修改 staged position，不能提前修改 committed cursor。
3. 只有以下步骤全部成功后才能提交 staged state：
   - source rows 完整转换；
   - snapshots 处理完成；
   - trades 处理完成；
   - lifecycle sweep 完成；
   - ledger/account frontier 已持久化；
   - status/checkpoint 写入成功。
4. 若 cycle 内出现 handled exception：
   - 可以尽力写 error status；
   - 不得提交 source cursor；
   - 不得把 checkpoint 写成成功 frontier；
   - 下一轮必须重新读取同一批 rows。
5. 若本轮前半段已有 ledger effects 成功落盘，下一轮 replay 必须靠 deterministic event/transition identity exactly-once，不得要求回滚 append-only ledger。
6. 复用 `atomic_json_write`、`read_json_with_fallback` 及现有 checksum/last-good 机制，不要另造普通 `write_text()` 状态文件。

### 必须测试

- 在 snapshot conversion、snapshot processing、trade processing、lifecycle processing 后分别注入异常；
- 断言 committed cursor offset/line 完全不前进；
- 下一轮移除故障后，相同 rows 被重新处理；
- ledger 中已成功的前半段 effects 不重复；
- 最终 source rows 不丢失、不重复产生成交/资金影响。

---

## A2. 同一公开 trade 必须只能推进一次 queue/fill

### 已确认缺陷

queue-aware partial fill 当前先通过 `_apply_fill()` 保存包含随机 fill ID 的 order snapshot，随后才另存 public `trade_key`。如果进程在两者之间崩溃：

- 重启会恢复 partially-filled order 和已减少的 queue-ahead；
- ledger 没有 durable `trade_key`；
- 相同 public trade 可再次填充剩余 shares。

这是实际 double-consumption crash window。

### 必须实现

1. 为每条 queue-eligible trade 只生成一次 canonical identity：

```text
trade:<event_or_tx_id>:<asset_id>:<ts_event>:<price>:<size>:<sequence>
```

字段必须按现有 public tape 语义规范化，不能用进程内随机值。

2. 对 queue-only 更新、partial fill、full fill，首次 durable post-trade order snapshot 必须同时携带 canonical `trade_key` 作为该 ledger record 的 `event_key`，使“order/queue/fill 后状态”和“trade 已消费”在一次 append 中成立。
3. 不得继续使用“先保存 fill record，稍后遍历 active orders 写 trade marker”的分裂方式。
4. fill 本身可以保留独立 `fill_id`，但 replay 去重权威必须是 canonical `trade_key`，不能依赖随机 fill ID。
5. startup 必须从 ledger event keys 恢复已消费 trade，而不是只恢复进程内 set。
6. Paper V1 每个 token/portfolio 同一时刻只允许一个可被同一 trade 推进的 active order。若恢复出多个 eligible active orders，写 durable discrepancy 并 HALT，不能自行分配一个 trade 的 volume。
7. queue-only trade、partial fill、full fill 都要覆盖 crash boundary。

### 必须测试

- partial fill post-state 已持久化、旧 marker 尚未写时模拟崩溃；
- 重启后 replay 同一 trade：
  - queue-ahead 不再减少；
  - filled shares 不增加；
  - account inventory/cost 不增加；
  - fill count 不增加；
- queue-only 更新与 full fill 同样 exactly-once；
- 同 transaction hash 下不同 price/size/sequence 的合法多行不得错误合并；
- 多个 eligible active orders 必须 durable HALT。

---

## A3. 持久化 I/O 失败必须 fail closed

### 已确认缺陷

account effect 可能已应用到内存，随后 ledger append/fsync 抛出 `OSError`。多个调用点只捕获 `PaperLedgerIntegrityError`/`ValueError`；cycle 层把 `OSError` 变成 `last_error` 后仍可能继续保存 cursor/checkpoint 并进入下一轮。

### 必须实现

1. 为以下边界建立统一 transition persistence failure path：
   - `begin_transition`；
   - order state save；
   - account effect apply/append；
   - commit；
   - abort；
   - discrepancy/HALT append。
2. 捕获 `OSError`，立即：
   - 设置 processor/account 当前进程内 HALT；
   - 阻止任何后续经济 mutation；
   - 尽力追加 durable discrepancy；
   - 阻止 cursor/checkpoint 成功提交。
3. 如果 ledger 本身不可写，无法落 durable HALT：
   - 不得假装 halt 已持久化；
   - 写 best-effort status（若 status 也不可写则保留原错误）；
   - 终止 follower，返回非零/明确 fatal state；
   - 下次启动依靠 order/transition reconciliation repair 或 HALT。
4. 不得在未知 commit 状态下继续新 order、fill、cancel、release 或 risk sell。

### 必须测试

分别在 intent、order save、effect append、commit、HALT append 注入 `OSError`，证明：

- 当前进程不再继续处理；
- cursor 未推进；
- 唯一可恢复时 startup exactly-once repair；
- 不唯一时 startup durable HALT；
- status 不得声称 clean/eligible。

---

## A4. HALT 后所有普通处理入口必须零 mutation

### 已确认缺陷

`process_snapshot()` 有部分 HALT guard，但 `process_trades()` 没有。resting order 可在 processor 已 HALTED 后继续被公开成交推进甚至生成 fill。

### 必须实现

1. `process_snapshot()`、`process_trade(s)`、`sweep_lifecycle()`、主动 close/risk-exit 等普通入口统一检查 HALT。
2. HALT 后不得改变：
   - order state；
   - queue ahead；
   - fills；
   - reservation；
   - cash/inventory/cost/PnL；
   - tranche/exit state。
3. startup reconciliation 使用明确的内部 recovery path，不要通过放宽普通账户方法来绕过 HALT。
4. 重复 HALT 调用幂等；原 halt reason/discrepancy 不得被较晚错误覆盖。

### 必须测试

建立 resting BUY 和已持仓状态，触发 HALT 后分别发送 snapshot、trade、timeout sweep、close event，逐字段断言前后完全一致。

---

## A5. 重启 weather join 不得读取未来 observation

### 已确认缺陷

continuous restart 当前从整个 weather archive 初始化 `previous_observation`/`previous_temperature`，而 market/weather cursor 可能仍指向更早边界。回放 backlog 时，旧 observation 会与 cursor 之后的未来温度比较或被误判为已消费。

### 必须实现

1. saved cursor 是 weather join 可见历史的唯一时间/位置边界。
2. restart 初始 weather state 只能由 cursor 已提交 prefix 重建。
3. first-start tail bootstrap 可在设置 tail cursor 后，用同一 tail prefix 建立 baseline。
4. 不能先加载整个 archive 再回放较早 rows。
5. gzip line watermark 与普通 JSONL byte offset 都必须支持一致重建。
6. 继续保持严格 ordering key：

```text
(source_timestamp, received_at, observation_id)
```

7. rejected observation 不消费，consumed evidence 跨重启保持。

### 必须测试

- cursor 在 weather archive 中段；
- cursor 后存在更高/更低未来温度；
- restart 回放较早 checkpoint/weather rows；
- tranche decision 与没有未来 rows 时完全相同；
- first-start tail bootstrap 不重放历史。

---

## A6. 已有库存的 exit 必须优先于下一 tranche 判断

### 已确认缺陷

第一 tranche 成交后，只要 `tranche_index` 仍为 1/2/3，多个补仓分支会在 weather/dip gate 不满足时直接 return，不调用 `_submit_exit_if_eligible()`。即使 bid 已达到第一档止盈目标，也可能一直持仓到 max hold。

### 必须实现

1. snapshot 通过 health/quality/season 检查并完成 terminal-order sync 后：
   - 若已有 inventory；
   - 且没有 active order；
   - 先调用当前未完成 exit stage eligibility；
   - 只有没有可提交 exit 时才继续评估下一 tranche。
2. 不允许“补仓 gate 拒绝”屏蔽已经满足的止盈。
3. 不改变既有 exit 口径：
   - 基于累计实际 BUY shares；
   - stage filled shares durable；
   - attempt identity 唯一；
   - retry 只卖 residual；
   - final stage 只卖当前剩余库存。
4. 若同时满足 exit 与下一 tranche，优先 exit，避免先增加风险再卖出。

### 必须测试

- tranche 0 实际成交；
- tranche 1 没有新天气确认；
- bid 达到 average + 0.05；
- 断言提交 stage-0 SELL，而不是仅记录 tranche rejection；
- 对 tranche index 1/2/3 分别覆盖；
- 未达 exit target 时仍可正常进入下一 tranche gate。

---

## A7. risk exit 报价与决策之间不能跨质量事故

### 已确认缺陷

当前只检查 `snapshot.timestamp` 是否落入 quality window。若 bid 在事故开始前产生，事故开始后仍处于 300 秒 freshness 内，max-hold/close 可在事故期间使用该旧 bid 制造可执行收益。

### 必须实现

1. 在 `polymarket_status.py` 增加可复用 interval-overlap helper，或提供同等清晰的纯函数。
2. 对 `[snapshot.timestamp, decision_as_of]`，只要存在：
   - `default_excluded=true`；
   - `affects_market_data=true`；
   - 时间区间有交集；
   就拒绝该 risk exit quote。
3. 继续要求：
   - same token/portfolio；
   - native bid depth；
   - age 在 `[0, 300]`；
   - `health_ok`；
   - feed continuity verified；
   - supervisor generation compatible。
4. 被拒绝后不得使用其他价格替代，只能 stranded/PnL N/A。

### 必须测试

- quote 在 incident 开始前 1 秒，decision 在开始后 1 秒，仍在 300 秒内：必须拒绝；
- 完全位于健康窗口：允许；
- incident 已结束但覆盖 quote-to-decision interval：仍拒绝；
- future quote、stale quote、无 bid：仍拒绝。

---

## A8. 新 entry 必须属于当前 supervisor active set

### 已确认缺陷

`_paper_buy()` 验证 supervisor integrity，却不要求 `snapshot.event_id` 位于 `supervisor_active_events`。某 event 在首个 snapshot 到达前已被移除时，没有现有 engine 可供 removal 逻辑关闭，迟到 archive snapshot 仍可开单。

### 必须实现

1. `supervisor_integrity` 必须为 verified；
2. `snapshot.event_id` 必须存在于当前 verified active set；
3. 不存在时记录明确 rejection：`event_not_active_in_verified_supervisor`；
4. closed/resolved、generation removal、rule/season invalid 都不能由迟到 snapshot 重新激活；
5. active set 为空是“verified empty”，不是未知，也必须阻止 entry；
6. unreadable 与 verified empty 保持不同 reason code。

### 必须测试

- verified active set 不含迟到 event：零 order；
- active set 为空：零 order；
- unreadable status：不同拒绝原因；
- event 加入 verified active set 后，其他 gates 满足时才允许。

---

## A9. 停机期间 public trades 不能被静默 baseline

### 已确认缺陷

startup 会读取所有 Data API trade files 并记录当前 mtime，但只有文件以后再次变化才生成 `api_trade_events`。如果 active Paper maker order 跨停机恢复，停机期间已有 public trades 而文件重启后不再变化，这些成交永远不会处理。

### 必须实现

1. public-trade file state/watermark 必须进入 staged、可持久恢复的 cursor 状态，不能只放进每次启动的新内存 dict。
2. 若恢复出的 Paper ledger 有 active order：
   - startup 必须扫描现有 public trade rows；
   - 按 `fetched_at/available_at`、order submitted time、token、side、price、sequence 和 quality gate 筛选；
   - 交给 durable `trade_key` 去重；
   - 不能直接 baseline 掉。
3. fresh first start 且没有 Paper order 时，仍可 tail-bootstrap，避免把历史数据算作 forward Paper 成绩。
4. overwrite/rewrite 的 JSON public tape 文件必须用稳定 fingerprint/watermark 判断，不得仅相信进程内 mtime。
5. public tape 只用于 queue evidence，不能成为 quote。

### 必须测试

- active order 后 follower 停机；
- 停机期间 public trade file 出现 receipt-eligible trade；
- 重启且文件 mtime 不再变化；
- trade 被处理一次；
- 再重启不重复；
- fresh empty Paper 首启不回放历史。

---

## A10. unmatched WS trade 必须是 durable pending UNKNOWN

### 已确认缺陷

WS trade 找不到 public tape match 时，当前只增加本轮 counter 并 continue。没有 durable evidence，feed continuity 可被重新设为 verified，重启后歧义消失。

### 必须实现

1. 为 unmatched WS row 生成 deterministic evidence identity，至少包含 transaction hash、asset、event/received timestamp 和必要 sequence 字段。
2. 写 append-only pending evidence，例如：

```text
UNKNOWN_TRADE_PUBLIC_MATCH_PENDING
```

3. pending evidence 必须跨重启恢复，并使相关 feed continuity / Paper score 保持 unknown/ineligible。
4. 后续 public tape 出现匹配时：
   - 写独立 resolution record；
   - 构造 canonical verified trade；
   - 只处理一次；
   - 不删除旧 UNKNOWN 证据。
5. 只有所有影响当前订单时间线的 pending ambiguity 都得到明确 resolution，continuity 才能恢复 verified。
6. cumulative historical unknown count 与当前 unresolved pending set 必须分开，不能因为历史曾有已解决 unknown 永久锁死，也不能因为重启清零。

### 必须测试

- unmatched WS 写 durable pending；
- 重启后仍 pending 且 score ineligible；
- 后续 public match 出现后追加 resolution；
- trade 只处理一次；
- 第二次重启保持 resolved，不重新变 pending；
- 不相关 token 的 pending 不应错误改变另一 token 的 queue，但总体 score/readiness 如何处理必须明确记录。

---

## A11. Paper V1 配置和 ledger identity 必须真正冻结

### 已确认缺陷

当前 `PaperStrategyConfig.validate()` 只冻结部分 identity。修改 timeout、max hold、station entry bands 或 `trigger_strategy` 后，新 ledger 仍可正常运行。

### 必须实现

对以下内容逐项 exact validation：

- `schema_version == 3`；
- `version == "paper-spread-v1-account-200"`；
- `execution_enabled is false`；
- `initial_cash_usd == 200`；
- `trigger_strategy == "weather_market_lag_in_band"`；
- `quote_mode == best_bid`；
- `fill_model == queue_aware`；
- entry bands：
  - `KLAX: 0.70–0.85`；
  - `KLGA: 0.50–0.70`；
  - 不允许静默增加、删除或改变 station/band；
- tranches：`20/30/50/100` 及四个既定 gate；
- exits：`+0.05/+0.10/+0.13/+0.20`，每档初始实际 shares 的 25%；
- `order_timeout_seconds == 900`；
- `max_hold_seconds == 7200`；
- `risk_exit_snapshot_age_seconds == 300`；
- `station_day_budget_mode == cumulative_buy_cost`。

另外：

1. Paper ledger 所有新 row 增加明确 identity，例如 `ledger_kind="paper_spread_v1"`；
2. envelope 同时验证 schema、ledger kind、execution flag；
3. 任意仅碰巧使用 schema 2/3 和 `execution_enabled=false` 的 Shadow/non-Paper row 必须拒绝；
4. 正式 Paper 尚未启动，不得迁移或创建正式 ledger；测试 ledger 可使用新 schema；
5. 如果决定 bump Paper ledger schema，必须一次性更新 writer、loader、tests、status 和文档，不能保留模糊双解析。

### 必须测试

对上面每个 frozen field 做独立 tamper test；同时测试：

- 当前真实 legacy-v2 row shape 被拒绝；
- crafted non-Paper row 即使顶层 `execution_enabled=false` 也被拒绝；
- 正确 Paper row 正常恢复。

---

# 测试矩阵整改

## T1. 22 项矩阵不得用窄单元测试冒充 production-path 覆盖

当前文档中的多项 PASS 证据不足，例如：

- poll expiry 测试直接调用 `sweep_lifecycle()`，没有走 continuous poll cycle；
- replay event-clock 测试没有真实 resting order；
- stale/quality entry 行没有独立 stale-entry case；
- repeat release 没有直接统计 durable account/order transitions；
- two portfolios 只测了 `PaperAccount`，没有两个 processor portfolios 的并发 reservation；
- no-native-bid test 没有放入容易被误用的 surrogate prices。

必须补齐：

1. 完整 continuous follower cycle：没有后续 token frame 也在 900 秒过期，release exactly once；
2. replay 中先建立或恢复 resting order，用 event clock 证明 `<900s` 不过期、`>=900s` 过期，wall clock 不参与；
3. repeat sweep 直接断言 ledger 中 release transition 仅一条；
4. 两个不同 station/portfolio 通过 `PaperSpreadProcessor` 共享同一 $200 account；
5. SELL submission 在 order 创建前就拒绝超过 same-token inventory；
6. worsening 后再给一个本来可补仓的 snapshot，证明不能 replenishment；
7. stale snapshot 直接尝试 entry，证明零 order；
8. closed event 只释放目标 token reservation，不触碰另一个 token；
9. no-native-bid fixture 同时携带：
   - midpoint；
   - last trade；
   - opposite token price；
   - `1-p` 候选值；
   - historical/settlement price；
   仍必须 stranded 且 PnL unpriced；
10. Windows runner 参数构造测试只检查 command/path/config，不运行进程。

`docs/paper_v1_test_matrix.md` 每行增加或保持清晰列：

- exact requirement；
- exact test name；
- production code path；
- unit/integration；
- restart/crash boundary；
- production metadata contract；
- actual assertions；
- PASS/FAIL。

只有测试真正断言 requirement 时才能写 PASS。

---

# B 轨：修正 Nautilus conformance challenger

## B1. local 与 native 必须使用相同的信息时钟

### 已确认缺陷

fixture 中 trade 可在 `ts_event=t+1`、`available_at=t+10` 才收到。native `TradeTick` 保存 `ts_event/ts_init`，local Shadow replay 却直接按 `trade.timestamp` 处理，双方不是同一信息可用时间线。

### 必须实现

1. 定义 canonical timeline row，同时保留：
   - exchange/event timestamp；
   - local availability/receipt timestamp；
   - sequence；
   - deterministic identity。
2. local 与 Nautilus 都按相同 availability order 接收信息；
3. order 是否在 trade 发生前 resting 仍由 event timestamp 验证；
4. 任何 strategy/lifecycle decision 不得在 `available_at` 之前使用该 trade；
5. normalized trace 同时记录 event time 和 decision/availability time；
6. 若 Nautilus 无法复现完全相同调度，分类为明确 limitation/unsupported，不能报 MATCH。

---

## B2. touch-only case 必须真的发生 touch

当前 case 下单后没有后续 touching book update，所以双方“不成交”不能证明 no-touch policy。

必须构造：

1. 初始 L2 book；
2. resting post-only BUY；
3. 后续 ask 移动到/穿过 order limit；
4. 全程没有 public trade；
5. local queue-aware 正式语义必须 no fill；
6. Nautilus 实际结果必须从 trace 读取并按已知语义分类。

测试需要证明：若 fill model 被改成 touch-filling，该 case 会失败或改变分类。

---

## B3. 报告中的 sandbox config/hash 必须是实际执行配置

### 已确认缺陷

当前构造并 hash `SandboxExecutionClientConfig`，但实际 `_run_nautilus_sandbox()` 独立调用 `BacktestEngine.add_venue(...)`，没有使用该对象。两个配置可悄悄漂移。

### 必须实现

1. 创建单一 canonical sandbox semantics payload；
2. 从该 payload 同时构造：
   - 可展示/验证的 Sandbox config；
   - `BacktestEngine.add_venue` 实际 kwargs；
3. artifact hash 覆盖实际传给 engine 的规范化参数；
4. report 输出实际 active parameters，而不是未使用对象；
5. 测试改变任一 active parameter 时 hash 必须变化。

固定语义仍为：

- venue `POLYMARKET`；
- CASH；
- NETTING；
- 200 USDC；
- L2_MBP；
- trade execution；
- queue position；
- liquidity consumption；
- Polymarket fee model；
- 无 live execution。

---

## B4. 同秒 sequence 无法表达时必须诚实分类

Nautilus rc4 `TradeTick` 没有 sequence 字段。Python 输入列表先排序不等于 native event stream 保留 sequence，特别是同 `ts_event` 而 `ts_init` 顺序相反时。

要求：

1. 新增 receipt order 与 exchange sequence 相反的 fixture；
2. 不得仅断言 tick count；
3. 若无法在不伪造时间的前提下表达 sequence，明确标：
   - `NAUTILUS_LIMITATION`；或
   - `UNSUPPORTED`；
4. 不得把它标为 MATCH；
5. 不得通过篡改真实 receipt time 来制造一致。

---

## B5. 18-case 分类必须由 trace/assertions 产生

1. 不允许只根据 case name 预填 `MATCH`、`EXPECTED_DIFFERENCE`、`NAUTILUS_LIMITATION` 或 `UNSUPPORTED`；
2. 每个可运行 case 必须保存 normalized local/native trace；
3. adapter/setup exception 应为 `UNKNOWN` 或测试失败，不能包装成已知 limitation；
4. `post_only_maker_submission` 必须区分：
   - 预期 native semantic difference；
   - malformed instrument/config；
   - adapter failure；
5. stale/out-of-order case 必须真的测试 out-of-order，而不只是 `market_stale=True`；
6. trade ID identity 必须包含足够字段，避免同 transaction/timestamp/sequence 下不同 price/size 冲突；
7. matrix 输出每行必须有：inputs、local trace、native trace、compared fields、classification、reason。

B 轨无论多少 MATCH，都必须保持：

```text
official_score = false
challenger_only = true
execution_enabled = false
```

并明确 `execution_enabled=false` 指没有外部/真实执行；本地 backtest simulated matching 是 challenger 的必要组成，不得把两者混淆。

---

## B6. optional dependency 兼容范围

- `nautilus-trader==2.0.0rc4` 仍只放在 `nautilus-eval` optional extra；
- 项目 Python metadata 与已验证范围一致：`>=3.12,<3.15`；
- 更新 `uv.lock`；
- 默认 import/CLI/daemon 不得加载 Nautilus；
- 默认测试在未安装 optional extra 时正常 skip Nautilus runtime case；
- optional suite 安装后全部通过。

---

# 第三方许可证与归属

## L1. 恢复 polymarket-tmax-lab MIT notice

当前 `README.md` 和 `src/poly_weather/settlement.py` 仍声明 settlement/parser 设计参考并适配自 `YoungseokOh/polymarket-tmax-lab`，并指向 `THIRD_PARTY_NOTICES.md`；当前 notice 却只剩 Nautilus 和 Spencer Fletcher。

必须：

1. 恢复 `polymarket-tmax-lab` repository、reviewed revision（从原 notice/git history 恢复，不能猜）、MIT license 和 adapted scope；
2. 保留 NautilusTrader LGPL 条目；
3. 保留 Spencer Fletcher MIT 条目；
4. 不得伪称复制了实际未复制的源码；区分 source adaptation、design reference 和 unmodified dependency；
5. 如果打包分发 optional Nautilus dependency，检查 LGPL/GPL license-text 随附义务；源代码仓库仅声明 optional dependency时也要保留准确链接与许可证说明。

---

# 实施顺序

必须按以下顺序，不建议并行大改：

1. 记录 workspace/data 安全基线；
2. A1 transactional cursor；
3. A2 atomic trade consumption；
4. A3 persistence failure；
5. A4 HALT freeze；
6. 运行全部 durability/fault-injection tests；
7. A5–A10 temporal/strategy/evidence；
8. A11 frozen config/ledger identity；
9. 重写并真实补齐 22 项测试矩阵；
10. B1–B6 Nautilus challenger；
11. 修复第三方 notices；
12. 完整测试、lint、lock 和只读检查；
13. 输出完成报告；
14. **停止，不启动模拟盘。**

如果前四项 durability contract 未通过，不要继续声称后续 tests 能形成可信封板证据。

---

# 强制验证命令

不得只给摘要，需记录命令、exit code 和简明原始结果。

## 1. 定向 Paper 测试

至少运行覆盖以下关键词/文件的 tests：

```text
cursor transaction / handled exception replay
partial fill trade crash
queue-only trade crash
account persistence OSError
HALT zero mutation
weather cursor-bound restart
exit before replenishment
quality interval overlap
supervisor active membership
downtime public trades
pending/resolved WS evidence
frozen config
Paper ledger identity
no-price-substitution
continuous lifecycle
replay event clock
```

## 2. 完整默认测试

```powershell
uv run pytest -q
```

## 3. Nautilus optional 测试

```powershell
uv run --extra nautilus-eval pytest tests/test_nautilus_conformance.py -q
```

## 4. Ruff

由于当前 workspace 可能保留 `.claude/worktrees` 临时目录，至少运行：

```powershell
uv run ruff check src tests
```

如运行仓库级检查，必须显式排除该保留目录，而不是删除它：

```powershell
uv run ruff check . --exclude .claude
```

## 5. Lock

```powershell
uv lock --check
```

## 6. 只读 daemon 状态

```powershell
uv run poly-weather stream-status
```

只能读取和报告，不得据此启动、停止或修复 daemon。

## 7. 正式数据零变更

```text
git status --short -- data
git diff --stat -- data
```

并检查正式路径不存在：

```text
data/**/paper_spread_v1*
```

## 8. 安全扫描

- Paper execution dependency scan clear；
- Nautilus forbidden dependency scan clear；
- 默认 CLI/daemon import 不加载 Nautilus；
- 不存在真实 execution client construction；
- 所有新增 ledger/status/report row 的 `execution_enabled` 严格为 false。

---

# 必须新增的 fault-injection tests

至少覆盖以下可命名测试；名称可按项目风格调整，但语义不能缺：

1. `test_handled_cycle_exception_does_not_commit_source_cursor`
2. `test_replayed_successful_prefix_is_idempotent_after_cycle_failure`
3. `test_partial_fill_and_trade_consumption_are_one_durable_fact`
4. `test_restart_cannot_consume_partial_fill_trade_twice`
5. `test_queue_only_trade_replay_does_not_reduce_queue_twice`
6. `test_account_commit_oserror_halts_without_cursor_advance`
7. `test_unwritable_halt_record_terminates_follower_fail_closed`
8. `test_halted_processor_rejects_snapshot_trade_and_lifecycle_mutation`
9. `test_restart_weather_join_uses_only_cursor_visible_prefix`
10. `test_profit_exit_precedes_rejected_future_tranche`
11. `test_risk_exit_rejects_quality_window_between_quote_and_decision`
12. `test_inactive_supervisor_event_cannot_open_first_order`
13. `test_restart_replays_downtime_public_trade_once`
14. `test_unmatched_ws_trade_is_durable_pending_unknown`
15. `test_later_public_match_resolves_unknown_and_consumes_once`
16. `test_exact_paper_config_rejects_each_tampered_field`
17. `test_paper_ledger_rejects_non_paper_schema_collision`
18. `test_continuous_poll_expires_without_new_token_frame_exactly_once`
19. `test_replay_expiration_uses_resting_order_event_clock`
20. `test_no_native_bid_ignores_all_surrogate_prices`
21. `test_nautilus_touch_case_contains_real_touch_without_trade`
22. `test_nautilus_local_and_native_share_availability_timeline`
23. `test_nautilus_sequence_limitation_is_not_reported_as_match`
24. `test_nautilus_report_hashes_actual_active_venue_config`

测试不能通过 monkeypatch 绕开 production function；故障注入点可以是小型可控 hook/context manager，但生产默认必须无行为差异。

---

# 完成报告格式

Codex 最终回复必须按以下顺序，不能只说“全部通过”：

## 1. 安全声明

逐项确认：

- 未启动 Paper；
- 未读凭据；
- 未调用网络 probe；
- 未连接真实 execution；
- 未操作 daemon/Task Scheduler；
- 未修改 `data/`；
- 未 commit/push。

## 2. 开始与结束 workspace 状态

- 开始 `git status --short`；
- 结束 `git status --short`；
- 说明哪些是 pre-existing，哪些是本任务修改；
- `data/` status/diff 原始结果。

## 3. A1–A11 逐项结果

每项给出：

- 修改文件；
- 核心实现；
- 具体 test name；
- crash/restart boundary；
- PASS/FAIL；
- 仍有限制。

## 4. 22 项矩阵

逐行给 test 和实际 assertion，不得只贴文档链接。

## 5. B1–B6 结果

- 实际 Nautilus version；
- optional dependency 与 Python 范围；
- actual active sandbox config/hash；
- 18 行 classification；
- 每个 MATCH 比较了哪些 trace fields；
- 所有 limitation/unsupported/unknown；
- `official_score=false`、`challenger_only=true`。

## 6. 测试与静态检查

列出：

- 定向测试；
- 默认完整 pytest；
- optional Nautilus pytest；
- Ruff；
- lock check；
- exit codes。

## 7. 当前只读 runtime 状态

原样摘要 `stream-status`，但不得操作进程。

## 8. 正式 Paper 证据状态

必须明确：

- 正式 Paper ledger/cursor/status 是否存在；
- 正式 order/fill/round trip/N；
- 本任务是否启动过 Paper。

## 9. 最终判断

只能在所有阻塞项和 tests 都通过时写：

```text
A 轨：技术封板候选，等待独立复核；未获得启动授权。
B 轨：隔离 challenger，非正式评分权威。
```

任何一项失败，必须写：

```text
A 轨：未封板；不要启动模拟盘。
```

---

# Definition of Done

只有同时满足以下条件，任务才算完成：

1. handled cycle exception 不会提交未处理 rows 的 cursor；
2. queue-only/partial/full fill 的 public trade exactly-once；
3. 任意 persistence `OSError` 后当前进程 fail closed；
4. HALT 后零经济 mutation；
5. restart weather state 无未来观测；
6. exit 优先级正确；
7. risk exit 不跨 quality incident；
8. inactive supervisor event 无法开单；
9. downtime public trades 不丢失；
10. unmatched WS ambiguity durable、可明确 resolve；
11. Paper config 和 ledger identity 真正冻结；
12. 22 项矩阵由真实 assertions 支撑；
13. Nautilus local/native 时钟一致，touch/sequence/config evidence 诚实；
14. 第三方 notice 完整；
15. 默认与 optional tests、Ruff、lock 全通过；
16. `data/` 零变更、正式 Paper 文件不存在；
17. 模拟盘未启动；
18. 没有真实执行能力或授权。

完成后停止，等待 Claude 独立复核和用户下一步明确指令。
