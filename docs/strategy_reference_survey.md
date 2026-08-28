# 预测市场交易策略参考调研

调研日期：2026-08-27；2026-08-28 已复核。目标不是引入真实执行代码，而是为本项目的只读影子策略寻找可验证、可迁移的设计。
本文只保留理念、失败模式和验证要求；未复制或改编所列项目的执行、钱包、凭证、下单或撤单代码，归属见
`THIRD_PARTY_NOTICES.md`。

## 结论先行

没有一个开源项目可以直接复制后变成适合天气桶市场的策略。最值得借鉴的是以下六个组件的组合：

1. **天气观测 → 市场价格的 lead-lag 单边 maker 策略**：本项目的核心 alpha；参考 Oracle3 的 lead-lag 状态机，但 leader 应是严格时间对齐的天气观测，不是另一个市场。
2. **库存偏斜 + 市场状态机**：参考 `warproxxx/poly-maker` 的 QUIET / TRENDING / EVENT / REDUCE_ONLY / HALTED，以及 inventory skew、toxicity、quote hysteresis。
3. **限价 bands / 分层挂单**：参考 Polymarket 官方旧 `poly-market-maker` 和 Hummingbot D-Man 的分层 maker 报价，但仓位总上限仍为 $200。
4. **YES+NO 互补库存配对套利**：参考真实 Polymarket 做市钱包取证。只有两腿真实成交均价之和小于 $1 才形成锁定收益；必须模拟单腿风险。
5. **队列归因和 adverse-selection 检测**：参考 `spencerfletcher/market-maker` 的 queue tracker。不能把队列消失等同成交；成交带断流必须输出 UNKNOWN，不能输出零成交。
6. **有限期限二元市场的时间缩量**：参考 Paradigm pm-AMM，只借用“临近事件/结算降低库存和挂单量”的思想，不直接使用其 Brownian/连续套利公式。

目前不建议引入完整 Hummingbot、NautilusTrader 或其他执行框架，也不建议复制任何钱包/签名/下单模块。

---

## 排名与适用性

### 1. `warproxxx/poly-maker` — 最适合作为影子 maker 架构参考

- GitHub：https://github.com/warproxxx/poly-maker
- MIT；约 1,474 stars；2026-07 仍活跃；README 声称 83 tests。
- 核心：纯函数策略 `(book, inventory, params, clock) -> TargetQuotes`，I/O 与策略分离；maker-only；reservation fair value；inventory skew；短/长波动与 markout toxicity；状态机；订单 reconciliation 和 churn tolerance。

最值得迁移：

- `QUIET / TRENDING / EVENT / REDUCE_ONLY / HALTED` 状态；
- 持仓越偏，重仓方向报价越差、对冲方向更积极；
- volatility/toxicity 上升时扩大 spread、减小 size；
- 不因 1 tick 抖动撤单，只有超过 reprice threshold 才失去队列位置；
- strategy core 保持纯函数，订单状态/账本在外层。

不能照搬：

- 它做双边政治市场 maker 和流动性奖励；我们的核心是天气观测后单边重定价；
- 它尚无 journal replay backtester；
- 它包含真实执行路径，本项目只读阶段不能引入。

### 2. `spencerfletcher/market-maker` — 最适合修正队列模拟

- GitHub：https://github.com/spencerfletcher/market-maker
- MIT；stars 很少但源码约 29K Python 行、README 声称 684 tests；2026-08 活跃。
- 最有价值的是 `bot/kalshi/queue_tracker.py`，而非执行器。

关键教训：

- 队列前量消失可能是成交，也可能是撤单；经济意义相反；
- `removed = traded + cancelled` 只在价格层级成立，不等于“取消的都是排在我们前面”；
- 离线拼接秒级 fill 与亚秒 trade tape 会制造顺序幻觉，因此无法判定时应输出 `UNKNOWN_FILL_SECOND`；
- trade tape 重连/丢包时必须标记所有受影响 resting orders 为 UNKNOWN；否则“traded=0”会制造假的 adverse-selection 结论；
- “成交与 L2 token 重叠 N=0”应被诊断为 plumbing/data coverage 失败，不是策略 0 fill。

这与当前影子策略的 token 重叠 N=0 完全一致：先闭合成交带，不能调参数。

### 3. Oracle3 — 最适合 lead-lag 与跨信号策略接口

- GitHub：https://github.com/YichengYang-Ethan/oracle3
- Apache-2.0；约 248 stars；2026-05 活跃；README 声称 633 tests。
- 有 `LeadLagStrategy`、`OrderBookPressureStrategy`、paper/backtest、相关性风险管理。

最值得迁移：

- leader 发生显著移动 → follower 尚未跟随时挂单；
- 只有 rolling relationship 仍成立才启用；
- follower 追上、spread 收敛或 max hold 到期就退出；
- order-book imbalance 只作二级时机信号。

本项目的改写：

- leader = 新到达且严格无前视的 WRH/NWS/METAR 观测或 physical-margin 状态变化；
- follower = 对应温度桶真实 NO bid/ask；
- 不使用 mid/p 代理；
- 不用另一个市场的相关性替代天气因果。

限制：Oracle3 的 paper trader 用随机 fill-rate 范围，不能替代本项目 queue-aware/trade-through 模型。

### 4. Polymarket 官方 `poly-market-maker` — 适合 bands 与 cancel/place 生命周期

- GitHub：https://github.com/Polymarket/poly-market-maker
- MIT；约 322 stars；最后活动 2024-07，已明显陈旧。
- 核心 bands：目标价上下按 margin 建区间；每个 band 保持 `[minSize,maxSize]`，低于 min 补到 avg，高于 max 撤到 avg；先算应撤订单，再算应挂订单。

最值得迁移：

- 目标库存 band，而非每次价格变化都全撤全挂；
- 可配置的 min/avg/max size；
- 订单不在任何 band 时撤销；
- 二元 token 的互补方向映射。

不能照搬：旧 CLOB、无毒性/队列/部分成交处理、未覆盖当前手续费和状态 API。

### 5. `octavi42/prediction-market-maker` — 适合 toxicity filter 和小单匹配

- GitHub：https://github.com/octavi42/prediction-market-maker
- MIT；约 28 stars；2026-04；Paradigm Prediction Market Challenge #2。
- 合成挑战里有“全知套利者先扫错误报价，再由 retail 成交”。

值得迁移：

- spread 相对短期波动不足时不挂单（z-score / edge-to-volatility filter）；
- 库存偏斜是必需项；
- 挂单大小应接近真实零售成交大小，过多余量只会暴露给 informed flow；
- 多层正常报价反而表现更差，说明“挂得多”不是优势；
- 临近终点降低最大库存。

不能照搬：110 次参数搜索高度过拟合合成环境；其中 magic numbers 不可用于真实天气市场；所谓 monopoly 极端价策略与本项目“尾桶多数无真实 ask”不是同一问题。

### 6. Polymarket 真实做市钱包取证 — 适合互补配对与动态 aggression

- GitHub：https://github.com/pascal-labs/market-maker-forensics
- MIT；104,049 trades、157 个 15 分钟 BTC 窗口、约 1Hz L2。

发现：

- 65% maker / 35% taker；
- 同时积累 UP/DOWN，组合均价中位数约 $0.9911；
- 库存失衡越大，主动吃单补缺侧的比例越高；
- 临近到期主动平衡增加；
- 多层静态挂单 + 动态主动补缺。

可迁移为新的影子策略：

- 在同一二元桶同时被动买 YES 和 NO；
- 只有两腿实际平均成本 + 费用 `< $1 - safety_buffer` 才算 complete pair；
- 第一腿成交后对第二腿设置最大等待、价格上限和 unmatched exposure；
- 必须报告 pair completion、单腿暴露、combined VWAP、资本分钟数。

限制：BTC 15 分钟市场每天 96 场、流量极高；天气市场无法复制其成交频率。不能把 0.9¢ 小边际直接外推。

### 7. Hummingbot — 适合 DCA maker 结构，不适合整体引入

- GitHub：https://github.com/hummingbot/hummingbot
- Apache-2.0；约 19.6K stars；持续活跃。
- `DManMakerV2` 支持 maker DCA spreads、分层 amounts、各层独立 refresh。
- `PMMDynamic` 用波动动态调 spread、参考价和 triple-barrier 风控。

可迁移：配置化分层价差、每层独立刷新、激活边界、DCA 只作有限库存。

不迁移：完整 connector/executor 框架、K 线 MACD/NATR（天气桶不是连续资产）、真实执行依赖。

### 8. Paradigm pm-AMM — 只借用有限期限风险曲线

- 文章：https://www.paradigm.xyz/2024/11/pm-amm
- 核心：二元概率波动随概率与剩余时间变化；动态版本使流动性规模约按 `sqrt(T-t)` 收缩，以避免临近结算的 arbitrage loss 爆炸。

可迁移：越接近典型高点/结算，挂单量越小、quote 越保守、库存上限越低。

不能直接套公式：其假设是连续 Brownian 信息和连续套利；天气观测会离散跳变，正是模型最不适用的场景。

---

## 天气类仓库评估

### `Xeron2000/pm-bot` — 被动报价代码值得参考，策略证据不足

- GitHub：https://github.com/Xeron2000/pm-bot
- MIT；约 2 stars；2026-08 活跃。
- 有 EMOS 多模型、adjacent ladder、lead-time sizing、METAR boost、dead-man switch，以及独立 `passive_price.py`。

可迁移：

- `bid + 1 tick` 但绝不穿 ask；
- GTC/GTD、reprice buffer、最大挂单时间；
- 评估价格和订单 intent 必须共用同一 passive-price 函数，避免研究按中点、执行却吃 spread；
- adjacent-bucket ladder 可作为现有 T11 的参考。

警告：

- NO anchor `fair_no - 0.10` 是未经证明的 legacy 常数；
- `passive_price.py` 尚未接入其生产策略；
- 部分 Brier/deadman 代码是简化近似；
- 不可复制参数或收益声明。

### `BallesJr/polymarket-weather-edge` — 最有价值的是失败案例

- GitHub：https://github.com/BallesJr/polymarket-weather-edge
- 未见明确许可证，不能复制代码。
- NO T+0、价格带 0.15–0.40、纸面记录很多，但 2026-07-10 至 08-12 的 847 笔样本：胜率 28.9% vs break-even 31.3%，PnL -$592；whole portfolio -$1,196。
- 作者自己定位到训练/线上 feature mapping 错误、季节漂移和热浪尾部校准失效。

可迁移的只有教训：

- 训练/线上 feature schema 必须哈希/版本化；
- 分季、分城市评估；
- 价格带在一个月有效不代表下一月有效；
- 高 claimed-edge 反而可能是模型坏了；
- paper trade 和 OOS 必须分离。

### `natestokens/polymarket-weather-bot` — NWS tiebreaker / mean buffer 值得验证

- GitHub：https://github.com/natestokens/polymarket-weather-bot
- 173-member ECMWF/AIFS/GEFS/ICON，公开日志只有 23 笔、样本很小。
- 可研究两个想法：NWS 人工预报作多模型分歧时的 tiebreaker；距 ensemble mean 3°F 内的 NO 信号降权/跳过。
- 不能采用其固定城市 bias（有的只基于 4 天或从别城假设）。

### `jffrz78/kalshi-weather-bot` — 风控与模式隔离参考

- GitHub：https://github.com/jffrz78/kalshi-weather-bot
- MIT；Kalshi 而非 Polymarket；paper 为默认、live 严格隔离。
- 值得参考：严格 station/terms quarantine、内容寻址原始证据、风险配置确认 token、station allowlist、相关簇/日亏损/回撤上限、单腿失败后 reduce-only unwind。
- 不迁移执行与签名模块。

---

## `jattree/weather-edge` — 最值得读的失败复盘与二次审计

- GitHub：https://github.com/jattree/weather-edge
- MIT；约 93 个文件、190 项测试；项目主动标注 sunset，真实 proving run 从 `$210` 降到 `$51.61`（−75.4%）。
- 它的真正价值不是策略参数，而是完整记录了**错误数据、错误站点、错误执行假设如何层层叠加，最后连 alpha 是否存在都没测到**。

### 最重要的纠正：亏损不等于 alpha 被证伪

原始 post-mortem 说“没有 edge”，后来独立复审发现：整个 live run **没有一天**同时满足所有正确条件：

- 3/31–4/2 用 Open-Meteo reanalysis 代替实际 Wunderground/METAR 结算源；
- 3/31–4/3 Denver/Houston/Hong Kong 三站点映射错误；
- 3/31–4/4 同城同时买多个相邻 YES 桶，结构上多数腿必亏；
- 3/31–4/7 仍有 12 个后来才修的正确性 bug，包括 Fahrenheit 桶上界多算约 0.8°F、系统性高估 YES 概率；
- 4/7 后进入故意移除全部安全栏的 hail-mary 模式。

所以准确结论是：**停止实盘完全正确，但 alpha 从未被正确测量；既不能说有，也不能说没有。**

这与本项目非常相关：不能拿当前成交带 token N=0 的影子报告证明策略 0 fill，也不能拿固定 $200 taker 压力测试否定 maker 限价策略。

### 已经踩过、我们必须继续避免的坑

1. **结算源与站点必须逐事件验证**
   - 该项目用错误 grid actual 后，67% 交易落入不同 whole-degree bucket；虚构出 `+$8,471` paper PnL。
   - Houston 用 KIAH 而实际是 KHOU，单日差 1.7°C；Denver、Hong Kong 也映射错。
   - 我们现有 settlement registry、SHA 证据、WRH 精确源和 fail-closed 正是必要措施，不能放宽。

2. **同一数据路径必须贯穿 live/hindcast/backtest**
   - Fahrenheit bucket 曾因转换后给上界直接 `+1°C` 而扩大约 0.8°F；subzero regex、SPECI、T-group、6-hour max、half-up、local civil day 都曾错。
   - live 解析与 hindcast 必须调用同一个实现，不能复制公式。

3. **没有真实盘口的 PnL 必须标成 illustrative**
   - 它曾用无 fee/spread/fill 的固定 `+0.90/-1.00` 产生假利润；后来才把 forecast skill 与假设成本 PnL 分开。
   - 当前公开版仍明确承认没有历史 order book，不能产生真实 track record。

4. **Paper 不能用 midpoint 假成交**
   - 第二次复审才发现 paper entries 仍用 Gamma midpoint、零手续费，重犯 post-mortem 自己的 lesson 2。
   - 我们必须继续使用真实 L2、queue-aware/trade-through；midpoint/trade price 不得替代 ask/bid。

5. **偏差修正必须过统计显著门槛**
   - 14 个样本就统一应用 bias，曾让 HKG 改善 49%，却让原本很准的 London 误差恶化 302%。
   - 修正后要求 `|mean bias| > 2×SE`，不显著就零修正。
   - 可迁移：为每站/模型 bias 增加显著性 gate，而不是“样本够 N 条就修”。

6. **季节/天气 regime 会使 30 日 bias 失效**
   - 它尝试 ENSO transition 时 shrink bias；具体城市敏感度是手工常数，不能抄。
   - 可迁移的是原则：regime 变化时收缩/停用旧 bias，并要求 OOS 证据。我们已按热季分窗，但天气型/SST 分层仍未完成。

7. **模型 run timing 必须实测，不用固定“黄金窗口”常数**
   - 该项目假设模型发布后 5–15 分钟市场未反应，并列出 GFS/ECMWF/HRRR 固定发布时间；来源部分是 LLM 分析。
   - 可研究，但必须从真实 run timestamp、Open-Meteo receipt、市场 L2 事件实测 lag；不能直接复制 schedule 或 confidence boost。

8. **安全栏删除永远不能成为默认**
   - 它 sunset 前的 hail-mary 关闭 horizon、agreement、dedupe、AI veto、exposure、correlation、edge 等所有闸门，后来这个模式竟成为开源默认配置，第二轮才修掉。
   - 本项目任何实验性策略都必须显式 opt-in、默认关闭、不可影响 `execution_enabled=false`。

9. **发现 PnL/结算不一致必须全停审计**
   - 它第一天 paper/live 差 `$440`，只补一个洞继续交易，导致错误层叠。
   - 可迁移为影子策略 invariant：订单账本、盘口重放、天气结算或 PnL 对不上即 HALTED，不继续积累“样本”。

10. **“Almost working”不是完成**
    - Auto-redeem 连续六次宣称修好才真正成功。
    - 任何修复需观察真实目标行为；本项目已有 PoolTimeout 两次误诊，应继续遵守长时实测。

### 它的 YES+NO spread 模块：概念可用，实现不可用

`trading/market_maker.py` 也提出同时买 YES+NO，组合成本 `<1` 获得锁定收益，并在方向仓位上补另一侧做 hedge。

但实现存在本项目已经禁止的做法：

- 输入是 `yes_price/no_price` midpoint 风格字段；
- 简单各减 1¢ 就假定被动成交；
- `simulate_spread_pnl` 明确承认 paper 无法确认两侧真实 fill；
- 没有 queue、partial fill、unmatched-leg 风险或两腿先后顺序；
- 因此其 `guaranteed_profit` 并不 guaranteed。

只借用**策略概念**，不复制实现。我们的 complement pair 只有在 YES/NO 两腿都由真实 queue-aware/trade-through 成交、份数配平、费用计入后才允许标记 locked edge。

### 值得作为候选研究的做法

- **Bias significance gate**：当前项目应评估是否需要从“样本量 gate”升级为“均值偏差显著性 gate”。
- **Model-run lead-lag**：把新模型 run arrival 作为 leader，与 WRH 实况 leader 分开测试；必须严格保存 run initialization 和实际 receipt。
- **Regime shrinkage**：ENSO/SST/天气型变化时收缩 bias，而非写死灵敏度。
- **退出 regret tracking**：记录每个 EXIT/HOLD/SKIP 决定事后价值，衡量规则是否真的增加 alpha；不需要 LLM 才能实现。
- **Correlation groups**：同一天受同一天气系统影响的城市不视作独立风险；现有 T9 可扩展，但分组必须数据验证。

### 不采用

- 它的 Kelly、midpoint edge、AI confidence boost、fixed golden-window 时间、penny lottery、adjacent-YES ladder、midpoint spread simulation 和固定 ENSO sensitivity。
- “sub-48h 一定属于 direct-feed bots”作为绝对结论也不采用；我们的实时 WRH + L2 lead-lag 需自己前向验证。但它对延迟的警告成立。

### 对当前项目的直接行动

1. 继续完成影子策略成交带/L2/天气事件闭环；没有正确 plumbing 不讨论 alpha。
2. 在 complement-pair shadow 策略里新增 unmatched-leg risk，绝不复刻它的 midpoint 假配对。
3. 审计当前 per-station bias 是否只有 sample-count gate；若是，新增独立任务评估 2×SE/滚动 OOS gate。
4. 未来所有影子规则输出“决策后悔值”：若当时不挂/撤单/退出，事后真实盘口会怎样，但不把事后标签泄漏进决策。
5. 把 settlement/PnL discrepancy 升级为 stop-everything invariant。

---

## 推荐给本项目的策略候选族

### A. Weather lead-lag one-sided maker（第一优先）

- 新天气观测严格到达后，若 physical margin / nowcast 改善且市场尚未重定价，挂 NO post-only bid；
- 报价 best bid 或改善 1 tick；
- market catch-up、天气恶化、timeout 或 stale 即撤；
- inventory skew 和分批补仓；
- 用 queue-aware/trade-through 前向验证。

这是本项目已有方向，应先闭合数据链而非换策略。

### B. Complement pair maker（第二优先，新方向）

- 同一桶同时挂 YES/NO maker bids；
- 只有完成的 pair 成本小于 $1 才算锁定 edge；
- 动态 skew 到缺失的一腿；
- unmatched leg 有严格 $/时间上限，必要时 taker unwind；
- 总预算 $200。

这是最接近“赚价差、不赌结果”的结构性策略，也回应了用户早期提出的对冲想法。

### C. Inventory/toxicity regime overlay（策略 A/B 共用）

- QUIET：正常 maker；
- TRENDING：偏斜、减量、扩大 spread；
- EVENT：新观测跳变/扫单时撤单冷却；
- REDUCE_ONLY：库存或时间压力下只减仓；
- HALTED：stale/维护/闸门失败全部撤单。

### D. Adjacent-bucket ladder（低优先）

- 仅当相邻桶模型概率接近且组合可达性/成本达标；
- 不能用旧的单押结算逻辑；
- 需要 non-atomic leg 风险和完整组合成本。

### E. Complete-set / neg-risk scanner（研究项）

- 对同一事件互斥桶，检查所有 YES 的真实可执行总成本是否 `< $1`；
- 必须先确认事件规则、neg-risk/complete-set 机制和每腿深度；
- 非原子多腿，任何单腿缺失都可能变成方向敞口；
- 只做影子扫描，不执行。

---

## 暂不采用

- 全量 Avellaneda–Stoikov：天气跳变/稀疏成交违背连续扩散和 Poisson fill 假设；只借 inventory reservation / time scaling。
- Kelly：独立 OOS 样本与概率校准不足，Kelly 会放大模型错误。
- 纯 order-book imbalance：浅盘口易撤单/操纵，只作 timing/toxicity filter。
- 复制交易与 LLM 交易 agent：没有独立 edge，且引入执行与安全风险。
- 直接引入 Hummingbot/Nautilus：复杂度和执行依赖过大；当前自研影子状态机更贴合审计要求。
- README 宣称收益但无许可/测试/OOS 的天气 bot。

---

## 建议顺序

1. 完成当前“影子策略数据闭环与持续前向运行”任务；
2. 在现有 shadow engine 上增加 A（weather lead-lag maker）的 regime/inventory overlay；
3. 并行新增 B（YES+NO complement pair）的影子回放；
4. 只在 B 出现真实两腿 queue-aware/trade-through 成交后评估结构性收益；
5. A/B 均稳定后才研究 D/E。

不要现在把所有参考策略一起实现。当前最大瓶颈仍是成交带与深度 token N=0 重叠，而不是缺少策略名称。
