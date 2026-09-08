# Codex 任务：正式模拟盘封板

## 目标

把现有 `shadow-spread-engine` 从“工程诊断影子 follower”升级为能够从明确起始时刻建立可信成绩单的**严格只读模拟盘**。

本任务只实现和测试代码：

- 不连接钱包、私钥、API key、签名、User WebSocket、Relayer 或真实订单 API；
- `execution_enabled` 必须始终为 `false`；
- 不启动、停止或重启当前四个常驻进程；
- 不修改、迁移、截断或续写成正式成绩的旧 v2 ledger/cursor/status；
- 不 commit，不 push，除非用户另行明确要求。

完成后只能说明“模拟盘工程条件已满足”，不能据此宣称策略正期望或已获实盘授权。

---

## 开始前必须阅读与复核

先完整阅读：

- `AGENTS.md`
- `CURRENT_CONCLUSIONS.md`
- `README.md`
- `HANDOFF.md`
- `src/poly_weather/shadow_orders.py`
- `src/poly_weather/shadow_runtime.py`
- `src/poly_weather/shadow_spread_replay.py`
- `src/poly_weather/cli.py`
- `scripts/windows/poly-weather-daemon-runner.ps1`
- 相关测试

再用只读方式核实当前事实，不要盲目照任务描述修改：

1. 当前 v2 status 曾显示 6 张活动影子单，其中有 2026-08-29 提交后仍为 `RESTING` 的旧订单；
2. 当前超时主要由同 token 的后续快照触发，市场结束或退订后可能永远没有下一帧；
3. 旧账本存在影子库存但 `round_trip_count=0`，因此没有完整价差交易样本；
4. Windows runner 当前没有显式传入 `--strategy-config`，常驻 follower 使用代码默认策略；
5. 默认策略是固定价带、best-bid、单笔 `$50`、`replenish_mode=none`，不是本任务要测试的动态分批策略。

若当前代码或状态与上述事实不同，在最终报告中指出差异，并依据实际代码实施最小正确方案。

---

## 一、修复影子订单生命周期

### 1. 增加独立于行情更新的 lifecycle sweep

新增可注入 `as_of` 时钟的全局 lifecycle sweep，并在 continuous follower 的每个 poll cycle 执行。它不能依赖某个 token 是否收到新盘口。

要求：

- `RESTING` 或 `PARTIALLY_FILLED` 订单满足
  `as_of - submitted_at >= order_timeout` 时进入 `EXPIRED`；
- 只释放 BUY 订单尚未成交部分对应的资金预留；
- 已成交库存继续保留，不能因订单过期而消失；
- 重复运行 sweep 必须幂等，不得重复写事件、重复释放资金或重复改变状态；
- 单元测试使用显式时间，禁止依赖真实系统日期。

### 2. 区分 continuous 时钟与 replay 时钟

- continuous follower 可以用当前 UTC 作为生命周期时钟；
- 历史 replay 必须继续使用事件时间；
- 不得把 wall clock 引入历史回放，造成订单在历史第一帧就被错误过期；
- 这两个模式需要有清晰的代码边界和测试。

### 3. 市场结束、退订与规则失效

当本地、当时可见的归档证据表明市场已 closed/resolved、已从核验 generation 退订，或规则/season version 失效时：

- 立即取消剩余 maker 单；
- 释放未成交 BUY 预留；
- 不允许为了补齐历史而在事后在线查询并倒填状态。

如果此时仍有库存：

- 只有在同 token、同一时点存在完整、健康、真实 bid 深度时，才允许按现有风险退出模型记录影子 taker exit；
- 没有合格 bid 时，将仓位标记为 `STRANDED_UNPRICED`（名称可按项目惯例调整），PnL 保持 N/A；
- 不得用 last trade、midpoint、`prices-history.p`、`1-YES`、对侧 token 或最终结算值代替退出 bid；
- stranded inventory 继续占用其历史成本，不能凭空释放现金。

旧 v2 ledger 中的不正确活动状态必须原样保留为审计证据；新逻辑只用于新正式模拟盘和测试 fixture。

---

## 二、增加全账户 `$200` 模拟资金账本

保留现有：

- `event_id + market_id + token_id + market_day` 的 token portfolio scope；
- station-day 的相关风险聚类与 `cumulative_buy_cost` 次级闸门。

在其上新增唯一、共享的账户层。**不能再把每个 station-day 理解为各自拥有 `$200`。**

### 1. 账户字段

至少包含：

- `initial_cash_usd = Decimal("200")`
- `cash_usd`
- `buy_reserved_usd`
- `available_cash_usd`
- `inventory_cost_usd`
- `fees_usd`
- `realized_pnl_usd`
- `stranded_inventory_cost_usd`
- 按明确成本口径计算的 equity

所有金额与份额继续使用 `Decimal`，禁止 float 会计。

### 2. 资金动作

#### BUY 提交

- 先按剩余限价名义金额预留现金；
- 请求不得超过 `available_cash_usd`；
- 所有城市、token 和 market-day 的活动 BUY 与库存共同竞争全账户 `$200`。

#### BUY 部分/全部成交

- 成交成本及费用从现金扣除；
- 对应减少 BUY 预留；
- 未成交部分继续预留。

#### BUY 撤单或过期

- 只释放未成交部分的预留；
- 已成交库存不变。

#### SELL 提交与成交

- SELL 预留 shares，不预留现金；
- 禁止超卖；
- 成交后按一致的成本法减少 inventory cost、增加现金并确认 realized PnL；
- 说明并测试采用的成本法，不允许同一仓位混用多种口径。

### 3. 资金守恒

选择并文档化唯一会计定义。至少持续验证等价于以下关系的守恒式：

```text
cash + inventory_cost
= initial_cash + realized_pnl - fees
```

如果 `realized_pnl` 定义为净费用，则相应调整，但全项目只能有一个明确口径。

注意：

- `buy_reserved_usd` 是 `cash_usd` 的锁定子集，不得再次加到 equity；
- station-day 退出后现金可以释放，但该日 `cumulative_buy_cost` 不得刷新；
- 任一账户或 token 不变量失败时立即 durable `HALTED`，写入 append-only discrepancy；
- 重启不能静默修正差异。

### 4. 确定性重启恢复

账户状态必须由新 append-only ledger 确定性重建。

重启前后以下字段必须逐项一致：

- cash、预留、available cash；
- token inventory shares 与 cost basis；
- fees、realized PnL；
- 活动订单及其未成交量；
- station-day 累计预算；
- 已消费的天气确认 observation；
- tranche/exit stage；
- stranded 状态。

---

## 三、冻结首版动态限价策略

新增显式配置，例如：

- 文件：`configs/paper_spread_strategy_v1.json`
- 版本：`paper-spread-v1-account-200`

禁止参数搜索。配置、每张订单和 runtime status 必须保存：

- strategy version；
- 规范化配置的 SHA-256；
- config path；
- `execution_enabled=false`。

进程未来启动时必须显式传入 `--strategy-config`，不能继续依赖隐藏代码默认值。

### 建议冻结配置

```json
{
  "schema_version": 3,
  "version": "paper-spread-v1-account-200",
  "execution_enabled": false,
  "initial_cash_usd": "200",
  "trigger_strategy": "weather_market_lag_in_band",
  "quote_mode": "best_bid",
  "fill_model": "queue_aware",
  "entry_bands": {
    "KLAX": [["0.70", "0.85"]],
    "KLGA": [["0.50", "0.70"]]
  },
  "tranche_plan": [
    {"usd": "20", "gate": "initial"},
    {"usd": "30", "gate": "new_weather_confirmation"},
    {"usd": "50", "gate": "conditional_dip"},
    {"usd": "100", "gate": "second_new_weather_confirmation"}
  ],
  "exit_plan": [
    {"rise": "0.05", "fraction_of_initial_shares": "0.25"},
    {"rise": "0.10", "fraction_of_initial_shares": "0.25"},
    {"rise": "0.13", "fraction_of_initial_shares": "0.25"},
    {"rise": "0.20", "fraction_of_initial_shares": "0.25"}
  ],
  "order_timeout_seconds": 900,
  "max_hold_seconds": 7200,
  "station_day_budget_mode": "cumulative_buy_cost"
}
```

如现有 schema 无法表达，在保持旧配置兼容的前提下扩展，不能破坏旧 v2 replay。

### 1. 初始 `$20`

同一 token 必须同时满足：

- `weather_market_lag=true`；
- `weather_improving=true`；
- 当前 NO ask 位于该站冻结价带；
- season/version 完整并匹配；
- 盘口完整；
- 所有 freshness、maintenance、quality-window 和 health gates 通过；
- 全账户与 station-day 预算均允许。

初始报价仍为 post-only maker best bid。

### 2. 第二档 `+$30`

只有同时满足以下条件才允许：

- 初始 BUY 已有实际影子 fill；
- 前一 BUY 已终结，不允许无限并发补仓单；
- 出现 strictly newer、receipt-eligible 的天气 observation；
- 新 observation 再次确认 `weather_improving=true`；
- 同一 observation ID 从未被该 portfolio 用于解锁 tranche；
- 所有健康与预算闸门仍通过。

### 3. 第三档 `+$50 conditional dip`

只有同时满足以下条件才允许：

- 前一 BUY 已终结；
- 当前价格较该 portfolio 最近实际 fill 至少回撤 2 ticks；
- weather 为 unchanged 或 improving；
- weather 绝不能 worsening；
- signal 仍有效；
- spread、book、season 和 freshness 均健康；
- 全账户与 station-day 预算允许。

价格下跌本身不能构成补仓理由；禁止无条件摊平。

### 4. 第四档 `+$100`

只有同时满足以下条件才允许：

- 第三档 conditional dip 已发生实际 fill；
- 出现第二个 strictly newer、此前未消费的确认 observation；
- `weather_improving=true`；
- 当前 best bid 不低于组合平均成本；
- 所有健康与预算闸门通过。

不满足就永久跳过该档，不要求每天投入满 `$200`。

### 5. 补仓审计证据

每个 tranche decision 保存：

- observation ID；
- source timestamp；
- received_at；
- decision timestamp；
- gate 名称；
- allowed/rejected；
- 完整原因；
- strategy version/config hash。

重启后同一 observation 不能再次解锁 tranche。

### 6. 分批退出

按照该 portfolio 累计实际买入 shares，而不是计划美元数：

- 平均成本 `+5¢`：退出初始累计 shares 的 25%；
- `+10¢`：再退出 25%；
- `+13¢`：再退出 25%；
- `+20¢`：退出全部剩余 shares。

要求：

- 正常退出继续使用 maker 限价，不以 bid 触达直接认定成交；
- queue-aware 只由严格 token-native、相反方向真实 taker tape 消耗队列；
- 处理 Decimal 舍入、部分成交和剩余不足，最终不能负库存或超卖；
- 已完成 exit stage 在重启后不能重复；
- weather worsening、stale/maintenance 或两小时 max hold 才进入风险退出；
- 风险退出只能使用该 token 真实 bid 深度并按实际 taker fee 计算；无 bid 时 stranded/PnL N/A。

---

## 四、创建全新正式模拟盘路径

使用版本化的新路径，建议：

- `configs/paper_spread_strategy_v1.json`
- `data/raw/shadow_orders/paper_spread_v1_orders.jsonl`
- `data/runtime/paper_spread_v1_cursor.json`
- `data/runtime/paper_spread_v1_status.json`

要求：

1. 新 ledger 不得读取、迁移或续写旧 v2 的订单、fill、库存和 PnL；
2. 旧 v2 ledger/cursor/status 保留只读审计；
3. 首次正式启动必须 tail-bootstrap，只消费启动边界之后的新归档；
4. 保存：
   - `score_started_at`
   - config version/hash
   - 当前 git commit
   - ledger schema
   - cursor bootstrap boundary
   - execution dependency scan
5. 更新 Windows runner 的参数构造，使未来经人工审核后可以显式启动新版本；
6. **本任务不得重装 Task Scheduler，不得停止或重启现有 daemon，也不得实际启动新模拟盘。**

如果为了避免与现有命令混淆而新增 CLI，例如 `paper-spread-engine`，可以这样做；但必须复用安全的 shadow 基础设施，不能复制出第二套不一致的订单会计逻辑。

---

## 五、状态与 readiness 输出

扩展 status 或新增只读 readiness 命令，至少输出：

### 策略身份

- strategy version
- config path
- config SHA-256
- score start
- git commit
- ledger schema
- `execution_enabled=false`
- forbidden execution dependency scan

### 账户

- initial cash
- cash
- BUY reserved
- available cash
- inventory cost
- stranded inventory cost
- fees
- realized PnL
- cost-basis equity

### 生命周期

- 活动订单数
- 最老活动订单年龄
- 超过 timeout 仍活动的订单数
- stranded/unpriced position 数
- market-closed position 数

### 统计

- orders、fills、partial fills
- round trips
- turnover
- capital utilization 与资金占用时长
- 按 token 和 station-day 的明细

### 安全状态

- HALTED
- discrepancy count
- last error
- upstream quality/maintenance
- cursor/restart count
- `paper_score_eligible`
- `paper_score_ineligible_reasons`

以下任何一项存在时，`paper_score_eligible` 必须为 false：

- 不变量失败；
- 超时订单仍活动；
- 配置 hash 不一致；
- ledger schema 不匹配；
- 执行依赖扫描不清洁；
- cursor 或 status integrity 失败；
- 旧 v2 状态被载入新账户；
- `execution_enabled` 不是严格 false。

---

## 六、必须覆盖的测试

至少新增以下测试：

1. 没有任何后续 token 快照时，poll-cycle sweep 仍让 15 分钟订单过期；
2. 部分成交 BUY 过期时只释放未成交部分预留；
3. 重复 sweep 不重复 ledger event、不重复释放资金；
4. market closed 时取消订单；
5. market closed 且有健康真实 bid 时可以影子风险退出；
6. market closed 且无 bid 时进入 stranded，PnL N/A；
7. 两个城市同时挂单时共享 `$200`，第二笔超过 available cash 必须被拒绝；
8. BUY fill、部分 SELL、撤单和费用发生后账户资金守恒；
9. SELL 不能超过库存 shares；
10. 重启从 ledger 重建账户，关键字段逐项完全一致；
11. 人工构造 ledger discrepancy 后必须 durable HALTED；
12. 退出释放现金，但 station-day `cumulative_buy_cost` 不刷新；
13. 四个 tranche 只按规定的新 observation 和价格/天气条件解锁；
14. 同一 observation ID 不能重复用于确认；
15. weather worsening 禁止补仓并触发风险退出；
16. stale/maintenance/quality-excluded 不创建新订单；
17. 四档退出 shares 守恒，无超卖、负库存或 Decimal 漂移；
18. 新 ledger 拒绝加载旧 v2 状态；
19. 首次启动只 tail-bootstrap；
20. historical replay 不受 wall clock 污染；
21. 所有新状态、订单和报告均为 `execution_enabled=false`；
22. forbidden execution module scan 保持 clear。

测试必须使用临时目录和确定性 fixture，不得修改 `D:\poly\data` 的正式证据。

---

## 七、验证命令

完成后运行：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m poly_weather stream-status
```

最后一条只用于确认现有守护链仍健康；不得通过启动新 daemon 来验证实现。

如果完整测试或 Ruff 失败，必须报告真实失败，不得只跑局部测试后宣布完成。

---

## 八、禁止事项和停止条件

### 永久禁止

- 不接 API key、钱包、私钥、签名、Relayer、User WebSocket、POST/DELETE order；
- 不引入 `py_clob_client`、`web3`、`eth_account`、`ccxt` 等执行依赖；
- 不用 `1-p`、`1-YES`、对侧 token、last trade、midpoint 或历史 p 代理真实 bid/ask；
- 不把订单簿数量下降视为成交；
- 不把 queue-aware 影子 fill 描述成真实订单成交；
- 不用最终结算 0/1 把 stranded inventory 伪造成价差 round trip；
- 不放宽 season、receipt、freshness、quality-window、contract verification 或无前视闸门来制造订单/fill；
- 不修改旧 v2 append-only 证据来消除旧活动订单；
- 不启动、停止或重启当前常驻进程。

### 遇到以下情况立即停止并报告

- 需求只能通过真实鉴权或交易接口完成；
- 无法在不修改旧证据的前提下隔离新账本；
- 账户守恒式与当前 PnL 定义冲突且无法从现有代码确定唯一口径；
- replay 与 continuous 时钟无法安全隔离；
- 测试暴露跨 token、跨 market-day 串账或重复 fill。

---

## 九、最终报告格式

最终回复必须包括：

1. 根因结论；
2. 修改文件及关键行号；
3. 订单 lifecycle 的 continuous/replay 时钟设计；
4. 全账户 `$200` 的会计口径和守恒式；
5. 动态四档建仓与四档退出的准确语义；
6. 新配置完整内容、version 和 SHA-256；
7. 新旧 ledger 隔离方式；
8. 使用临时 fixture 演示：
   - 并发预留不超过 `$200`；
   - 无后续快照也能过期；
   - 部分成交只释放剩余预留；
   - 重启前后账户一致；
9. 完整 pytest 数量与 Ruff 输出；
10. `stream-status` 的现有守护链健康结果；
11. 明确声明是否修改旧 data、是否启动/停止/restart daemon；预期均为“否”；
12. 提供人工代码审核后才可执行的新模拟盘启动命令，但**不要实际执行**；
13. 列出仍然存在的统计限制：新模拟盘开始时 round trip 仍为 N=0，必须继续按 station-day 聚类积累，不得提前作盈利结论。
