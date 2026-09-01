# AGENTS.md — 接手须读

本文件是给接手本项目的 AI 助手（codex / Claude 等）的交接说明。**开始工作前必须完整读完本文件。**

配套文档：[README.md](README.md) 讲功能与命令，[HANDOFF.md](HANDOFF.md) 讲环境安装与运行状态，[docs/polymarket_api_reference.md](docs/polymarket_api_reference.md) 是官方 API 归纳。本文件讲**规则、架构判断和当前进度**——这些是从大量试错中得来的，重犯代价很高。

---

## 1. 项目是什么

面向 Polymarket 天气市场（"某城某日最高温落在哪个 2°F 桶"）的**研究系统**，不是交易机器人。

当前只做：公开数据采集、原始数据留存、结算规则核验、不可执行的纸面信号。

**没有**钱包、私钥、签名、下单、撤单、转账代码路径。`execution_enabled` 恒为 `false`，所有信号 `action` 硬编码为 `skip`。

---

## 1.5 用户的策略意图：赚价差，不赌结果（**读到这里必须停下来理解**）

**这一节存在的原因**：助手曾多次按"买入并持有到结算"的框架去分析和反驳，而用户的实际意图从一开始
就是价差交易。这是理解偏差，不是用户改主意。犯这个错会让整段分析跑偏，浪费一整轮对话。

### 用户的玩法

不是"买 30% 持有到结算，赢得 100%、输得 0"，而是**低买高卖，赚中间价差，不管最终结果**：

- 例如 0.80 买入 → 0.93 卖出 → 赚 13¢ 毛价差 → 已平仓，最终结算与我无关
- 也可能是 0.30 买入 → 0.50 卖出。**入场/出场价位不固定，不要假设任何具体数字**
- 目标是资金周转：持有 1–2 小时而非 12–24 小时，同样本金一天可做多笔
- 依据是"市场对温度观测的重新定价有滞后"，赚的是这个滞后，不是赌天气结果

### 这不是新提议，项目里早就有对应实现

| 已有实现 | 对应策略环节 |
|---|---|
| `market_lag.py` / `data/market_lag_report.md` | 量化"天气观测出现后市场多久才重新定价"——即滞后优势本身 |
| T6 已出局桶退出可行性 | 判定标准原文就是"资金周转率高，资金效率显著优于持有到结算" |
| T10 出场条件"赢家桶价格 ≥0.95 或收盘" | 提前平仓，不等结算 |
| `execution_cost.py` 的 $50/$200/$1000 分档 | 价差策略对滑点极度敏感，所以要按仓位测 |

### 影子执行口径（只读）

用户策略的下一层实现是**post-only maker 限价挂单、动态分批补仓和分批退出**，不是把每个
五分钟快照都当成一次 $200 taker 交易。库存状态必须按
`event_id + market_id + token_id + market_day` 隔离；总预算 $200 只由同一 station-day 的风险管理器聚合，
不是每笔必须投入的金额。活动订单的潜在剩余风险与已成交库存合计不得越过该上限；本配置明确采用
`cumulative_buy_cost`，所以平仓释放当前敞口但不会重置当日累计买入额度。
影子策略允许部分成交、撤单/重报价、确认后加仓、条件性回撤补仓和保护/时间/天气失效退出，
但天气恶化时禁止无条件摊平，且每个 token portfolio 只有自己的状态机。统计独立单位仍是
station/market-day：同站同日多个桶按相关风险处理，不能把 token 数、订单数或五分钟快照数当独立样本。

所有成交都是历史重放的模型估计而非真实执行记录：touch 是乐观上界；queue-aware 只用真实
相反方向 taker 成交消耗下单时记录的更优档与同价位前量；trade-through 还要求真实成交穿过限价
且数量足够。盘口数量下降不能当成交，成交带的价格也不能冒充 resting ask/bid。实时命令只写
独立永久影子账本并硬断言 `execution_enabled=false`，绝不访问钱包、签名、User WebSocket、
POST/DELETE order 或 Relayer。

当前 `shadow-spread-engine --supervised` 默认持续跟随本地追加归档；`--once` 仅用于有限烟测。
它用 token-scoped v2 append-only ledger（`shadow_orders_v2_token_scoped.jsonl`）、原子 cursor 和重启代数保存活动影子单、queue ahead、库存、退出阶段与
上游维护状态，启动时扫描执行依赖并在发现维护/失效时只撤销影子单。所有成交、PnL 和周转仍是
历史盘口驱动的只读模型估计，不是实际订单记录；缺少真实订单 ID、确切排队位置或可验证季节版本时
继续 fail-closed。

2026-08-28 起，默认 continuous follower 的首次启动会把 cursor 放在已有归档尾部，确保只积累其后
的新前向证据；旧归档只能通过 `--once` 或显式 `--replay-existing` 回放。该 v2 follower 已实际连续观察
30.08 分钟并做过一次 cursor 恢复：heartbeat/cursor 前进、无 HALTED/discrepancy，26 条既有 ledger
orders 与 3 个 fills 未在重启后重复。`stream-status` 只显示 v2 状态，并把
`shadow_spread_status_v1_legacy_read_only.json` 明确标为 superseded 证据，不得把 v1 状态当作 live 状态。

YES+NO complement pair 是独立的只读影子策略族：每个 binary market 的 YES/NO 各有 token-scoped
库存、订单和队列，station-day 只汇总 $200 风险及相关统计。计划 maker 限价和 `< $1` 的计划成本不是
locked edge；只有相同 shares 的两腿实际 queue-aware/trade-through fill 配平、真实 VWAP 加费用 `< $1`
才记为 locked pair。单腿到达时间/美元上限后只允许 token-native 深度的影子 hedge/unwind；维护、stale、
规则变化或任意 token 作用域不一致都立即撤未成交腿或 HALTED。它绝不能共享单方向 weather lead-lag 的账本。

### 数据更新间隔内的“安静窗口”策略假设

用户提出：每次天气/模型数据到达并完成首轮市场反应后，到下一次可见外生数据到达前，价格波动可能主要是
盘口与交易者行为噪音，适合 maker 限价反复赚 spread。这个假设与 lead-lag 是同一数据周期的两个阶段，
不能混成一个策略：

1. **EVENT**：新信息刚到，撤旧单或只做严格 lead-lag；
2. **DIGESTION**：市场仍在分批消化同一信息，可能持续单向，不能先验当均值回归；
3. **QUIET**：新锚点稳定、无新的可见外生信息，才是 noise/spread harvesting 候选；
4. **PRE-RELEASE**：下一次预期更新前撤单/缩量，避免被新信息打穿。

“无下一次定时数据”不等于“无新信息”：SPECI、WRH 高频观测、NWS/TAF 修订、模型 run 到达、相关站天气、
云层/海风/雷暴、Polymarket 大单和撤单都可能改变条件分布。必须建立统一的 external-information clock。

首轮反应结束不能写死 5/15 分钟，要按站点和数据源实测：价格斜率/跳跃、成交强度、spread、order-book imbalance、
跨桶概率质量与撤单强度恢复稳定后才进入 QUIET。连续若干窗口无新信息且各指标回到基线才算结束；
下一条新信息立即重置为 EVENT。mid/microprice 只能用于状态诊断，PnL 与 fill 仍必须用真实 bid/ask 和 queue 模型。

该假设尚未验证，不得在报告里把整个更新间隔称为“噪音”。需要对比 EVENT/DIGESTION/QUIET/PRE-RELEASE
四态的均值回归率、maker fill、markout、spread capture、跳空和 adverse selection，按 market-day 聚类且 OOS 验证。

**不要把价差策略当成需要重新论证的新想法。** 要做的是用真实数据算它的期望。

### 但逆转率仍然绕不开（这一点要讲清，不是反对）

价差策略的价格路径驱动力**就是温度确认**：市场从 0.80 涨到 0.95，是因为观测不断确认该桶。

因此逆转时价格路径**不是** 0.80→0.75 让你从容止损，而是 0.80→0.05 的**跳空**：一个新观测高点
直接把桶物理排除，做市商瞬间重定价，中间没有挂止损的机会。

所以"我只预测价格趋势、不预测天气"在这个市场不成立——**价格趋势就是温度的函数**。
逆转率量化的正是"预期价格路径反向的频率"。

盈亏平衡算法（用户价位会变，所以记公式而不是记结论）：

```
毛价差   = 卖出价 − 买入价
手续费   = 0.05 × 买入价 × (1−买入价) + 0.05 × 卖出价 × (1−卖出价)
净收益   = 毛价差 − 手续费 − 入场滑点 − 出场滑点
平衡胜率 = 买入价 / (买入价 + 净收益)      ← 亏损侧按跳空归零估计
```

关键：**平衡胜率随价位剧烈变化**，所以不要沿用任何具体数字，按用户当次给的价位重算。

一个重要修正：手续费在两端趋零（`p×(1−p)` 形状），所以 0.80→0.93 这种尾端交易手续费很轻
（约 1.1¢，占毛价差 8.7%），**手续费不是价差策略的杀手**。曾有一次按 p=0.5 估成 2.5% 往返，
那是错的。

### 真正的瓶颈是入场滑点，不是手续费

T7 前四个真实触发的实测（顶层 ask vs $200 实际成交均价）：

| 桶 | 顶层 ask | $200 均价 | 滑点 |
|---|---:|---:|---:|
| KLGA 80–81°F | 0.450 | 0.743 | **+29.3¢** |
| KLAX 84–85°F | 0.640 | 0.774 | **+13.4¢** |
| KLGA 82–83°F | 0.680 | 0.814 | **+13.4¢** |
| KLAX 86–87°F | 0.950 | 0.967 | +1.7¢ |

**入场滑点 13–29¢，与想赚的价差同一量级。** 看到 0.80 的 ask，$200 实际可能成交在 0.93，
目标价位被滑点吃光。

结构性取舍（这个模式值得注意）：
- 低价位毛价差大，但滑点 13¢+，吃掉大半
- 高价位（0.95）滑点仅 1.7¢，但毛价差只剩 5¢

方向性差异：$200 **卖出**完整成交率 97.8%、$1000 为 65.8%；而**买入**分别是 82.5% 和 33.7%。
即"卖得出去"有数据支持，**"买得进去"才是瓶颈**。

### 分析价差策略时的硬要求

- 入场成本必须用**指定仓位的深度均价**，绝不能用顶层 ask（会低估 13–29¢）
- 出场收益用该 token 自己的真实 bid，不用 `1−p` 或对侧代理
- 手续费按 §4.3 公式分别计算入场和出场两笔
- 按**物理余量分层**算胜率，不要用全站平均逆转率。用户只在特定条件下入场，
  余量充分时（如已观测高点超桶上界 3°F+）的逆转率远低于全站平均——这正是 T4/T5 要回答的
- 价位不固定，公式化处理，不要写死任何入场/出场价

---

## 2. 铁律（违反会造成真实损害）

这些不是风格偏好，每一条背后都有踩过的坑。

### 2.1 不要引入执行能力

- 不要接 API key / L2 凭证 / Relayer API / 任何下单路径
- 不要因为"顺手"或"将来要用"就先把签名代码写上
- 密钥的作用域不是"读更多数据"，而是"能动钱"。现有全部数据源都是公开免鉴权的
- 需要鉴权的只有执行侧（下单、撤单、查自己的订单/成交、心跳）

引入执行的前置条件（当前**全部未满足**）：30 个新口径已结算前向样本、Wilson 置信下界高于真实全成本、独立审核过的执行适配器。

### 2.2 严禁用 `1−p` 或 `1−YES` 代理 NO 侧价格

这是本项目最贵的一次教训，导致一批收益数字全部作废。

- `prices-history` / `batch-prices-history` 返回的 `p` 是**历史成交价点位**，不是可成交盘口
- 实测：2 小时窗口只返回 3 个点，每点只有 `{t, p}` 两个字段。没有 side、没有 size、没有"是否真的成交"
- 真实 NO 成本必须取 **NO token 自己的 ask**；退出取 NO 自己的 bid
- 没有真实盘口数据时**标记 N/A**，绝不回退到代理再当成真实结果
- `data-api/trades` 给的是真实成交价，比代理好得多，但**仍然不是"我们当时能吃到的 ask"**。这个区别一旦模糊就会重犯同样的错

### 2.3 fail-closed 不许放宽

以下闸门任何情况下都不要为了"让结果好看"或"让流程跑通"而放宽：

- signal_engine 的健康闸门（数据新鲜度、结算核验、心跳、校准可信度、异常边际）
- 季节窗口闸门：日期落在该站已校准季节窗口外 → `warming_window_no` 不可触发
- **绝不允许沿用邻近季节的阈值**。11 月的 KLAX 用 9 月阈值，比没有阈值更危险——它看起来有依据，实际依据已过期
- 结算源核验：KLGA/KLAX 曾从 Wunderground 换成 weather.gov，核验捕获了这次变更。不要跳过
- ZUCK/ZUUU（重庆/成都）因观测密度和 T 组精度不足，保持不可触发。**不要给一个看起来合理但缺乏数据支撑的阈值**

### 2.4 严格无前视

- 观测只能用 `timestamp <= 决策时刻`
- 市场价格只能用决策时刻之前的报价点
- `lead_days=0` 的 Previous Runs 数据含目标日内更新，**禁止**用于校准和回测
- `collection_mode=historical_backfill` 的天气数据**禁止**用于严格无前视复现（它可能已被 QC 修正，不代表当时可见的值）；用于事后特征分析（确定性曲线、日高统计）是合适的
- 两侧都要在代码里显式保证且有测试覆盖

### 2.5 市场深度采集不能降频、不能中断

**订单簿深度是唯一"现在不录、以后永远拿不到"的数据。**

- Polymarket 无任何可用的历史深度接口（详见 §4.2）
- 天气数据挂了可以用 WRH 历史接口回填；深度挂了就是永久空洞
- 因此：market_stream 的频率不要降，维护期间也不要停采集（维护期仍间断供数，标记即可）

### 2.6 统计严谨性

- 所有比率必须附 **Wilson 95% 置信区间**
- n < 30 的分层明确标注"统计不可靠"
- 用置信**下界**而非点估计计算保守期望
- 为什么重要：n=26 时 100% 胜率的 95% 下界约 87%，按 0.90 成本算期望是 **−3.3%（负期望）**。点估计的 100% 胜率不能作为决策依据
- 禁止跨季节、跨阈值版本合并样本算 Wilson 区间

### 2.7 报告纪律

- N=0 就写 N=0，不要用代理数据凑
- 结论变化如实报告，包括"之前的结论不成立"、"KLAX 不再是首选"、"某站不可用"这类不受欢迎的结果
- 修复后必须**实测验证**，不要改完就宣布修好（见 §5.1 的教训）
- 不要为了让数字好看而抑制计数（如重连次数）

---

## 3. 架构

### 3.1 四个常驻守护进程

| 进程 | 职责 | 状态文件 |
|---|---|---|
| `market-stream` / `market-supervisor` | Polymarket 公共 Market WebSocket，完整 `book` + 逐档 `price_change` 维护可重放本地订单簿；supervisor 负责每日事件自动发现、核验、热订阅轮换 | `data/runtime/polymarket_ws_status.json` |
| `weather-stream` | 十站 WRH/NWS/METAR/TAF/Open-Meteo 采集 | `data/runtime/weather_daemon_status.json` |
| `signal-engine` | 增量跟随两个流的 JSONL，生成不可执行信号快照 | `data/runtime/signal_engine_status.json`、`signal_state.json` |
| `shadow-spread-engine --supervised` | 只读 v2 token-scoped follower；消费新增归档，保存原子 cursor 和永久影子账本，绝不下单 | `data/runtime/shadow_spread_status_v2_token_scoped.json` |

Windows 当前用四个 `PolyWeather-*` Task Scheduler 任务持有上述链路。2026-08-31 已实测 weather、signal、shadow 子进程被受控 kill 后由 runner 自动拉起；market 只做一次接管重启，440/440 完整簿恢复后验收，接管空档作为 `local-task-scheduler-market-takeover-2026-08-31` 默认排除且不回填。健康检查必须使用 `scripts/windows/poly-weather-status.ps1` 或 `stream-status`，核验 checksum、heartbeat、PID liveness 与实际命令归属；不能只读状态文件中的 PID 或把 checksum 字段“存在”当作完整性通过。

查看状态：`uv run poly-weather stream-status`

### 3.2 实时链路 vs 历史链路（重要设计决策）

早期把两者混在一条 60 秒轮询里，既做实时信号又当历史归档，结果为攒历史付出高频请求代价，把实时链路拖垮（PoolTimeout 风暴）。现已分离：

- **实时链路**：只服务信号引擎。按实测更新节奏轮询（WRH/NWS 120s、METAR 900s、TAF 3600s、中国站 1800s、Open-Meteo 10800s）
- **历史链路**：WRH 历史区间批量回填，一次拉整天/多天，每日一次。上限 30 天/批
- 两份数据用 `collection_mode` 物理隔离（见 §2.4）

实测的真实更新频率（用于定轮询间隔的依据）：WRH/NWS 中位数 300s、METAR 中位数 3600s（整点例行报）、TAF 约 2–3.5 小时。原先 60s 轮询是 5–60 倍超采。

### 3.3 数据存储

- 原始 JSONL → `data/raw/<source>/<date>/events.jsonl`
- 聚合 → DuckDB（`market_stream` / `weather_stream` / `signal_stream` / `research`）
- `signal_stream.duckdb` 已做结构化重构（schema v2）：取消 `payload_json`，拆为 `signal_snapshots` + `signal_bucket_observations` + `bucket_dim` + `calibration_dim` + 原因表。体积从 6.88 GiB 降到约 1.25 GiB（−85%），无损、未降采样
- **教训**：不要把整块 JSON 塞进 VARCHAR 列。DuckDB 是列式存储，这样做会让字典编码和列压缩全部失效
- NTFS 透明压缩：`polymarket_clob_websocket` 已启用（约 2.4:1）。注意 macOS 不继承此属性

### 3.4 关键配置

- `configs/settlements.json` — 实际结算 registry（十城）。`settlements.example.json` 是模板
- `configs/warming_window_no_thresholds.json` — 按 `station_id → seasons[]` 的季节阈值，每季带独立版本、日期范围、样本量、provenance、典型高点、联合阈值

---

## 4. 数据源的能力边界（查过的，不要重复试）

### 4.1 各端点实际给什么

| 端点 | 给 | 不给 |
|---|---|---|
| `batch-prices-history` / `prices-history` | 历史成交价点位 `{t, p}` | side、size、是否成交、盘口深度 |
| `/book`、`/prices` | 当前完整深度阶梯（price + size 各档） | 历史——只有当前快照 |
| Market WebSocket | 实时 `book` 全量 + `price_change` 逐档增量 | —（这是唯一能攒出历史深度的途径） |
| `data-api/trades` | 每笔成交的 side / size / price / timestamp / asset，约 3 年历史，公开免鉴权 | 挂单深度（成交流水无法反推 resting bid/ask） |
| 链上数据（Dune/Allium/Goldsky） | trades、balances、positions、redeems、聚合指标 | **订单簿快照不在链上**（链下 CLOB + 链上结算） |

### 4.2 `orderbook-history` — 不要用

存在未公开端点 `GET https://clob.polymarket.com/orderbook-history`（`asset_id`、`startTs`/`endTs` 毫秒、`limit` 最大 500、`offset`），曾返回完整 book 快照。

**但它约在 2026-02-20 20:00 UTC 停止写入新数据**，此后查询返回 `count: 0` 且 HTTP 200。本项目数据始于 8 月，全部落在冻结之后。未公开、无支持保障。

**不要基于它建任何管线。**（Nautilus Trader 的 Polymarket loader 用的就是这个端点，所以它的历史加载器对我们同样无效。）

### 4.3 手续费公式

官方实际公式（不是固定 bps）：

```
fee = shares × feeRate × p × (1 − p)
```

- maker = 0（所有类别）；**Weather taker feeRate = 0.05**
- `p` 是**该 token 自己的成交价**。买 NO @0.82 用 0.82，不是 YES 的 0.18
- 费用在 p=0.5 最高，向两端对称衰减至 0
- 手续费与滑点必须**分离计算**：滑点来自真实深度吃单，手续费按公式
- **坑**：`GET /fee-rate` 返回 `base_fee=1000`，**不能**直接除以 10000 当作 feeRate=0.10（与官方 Weather 0.05 冲突）。真实费率参数在 V2 市场信息的 `fd.r/fd.e/fd.to`，Weather 实测 `0.05/1/true`
- 方向性影响：固定 bps 模型会**高估**尾部桶成本、**低估**中间价位（p≈0.5）成本。T11 邻桶价差策略正好落在费用峰值区

### 4.4 温度精度

- METAR 报文体只给整数摄氏，remarks `T` 组给十分度。**必须优先解析 T 组**，缺失才回退并标记 `temperature_precision_degraded=true`
- 全项目统一"保持源精度 → 换算 → 最后在结算边界取整"。不存在"先取整摄氏再换算"的路径（已审计确认）
- ZUCK/ZUUU 的 METAR **T 组覆盖率 0%**，无法恢复十分度，属精度降级
- weather.gov WRH 页面前端对 `air_temp_set_1` 调 `Math.round` 显示整数，但底层有小数

---

## 5. 已知缺口与踩过的坑

### 5.1 三次误诊的教训（最重要）

天气守护进程 PoolTimeout 问题被误诊两次：

1. 第一次判断"连接池太小" → 扩容 8→80 → 撑了几小时又挂
2. 第二次判断"请求频率太高" → 降频 5–15 倍 → **0% 错误率维持 5 小时后复发**
3. 真正根因：**httpx/httpcore 连接池计数泄漏**。硬证据是"进程实际只持有 3 个 TCP 连接，却在容量 80 的池上排队超时"

**教训**：
- 看起来合理的修复不一定对。改之前先用数据验证假设
- 验证窗口必须超过上次复发的时间尺度。5 小时才复发的问题，观察几分钟毫无意义
- 有本机代理时，httpx 走的是 `AsyncHTTPProxy` 池而非默认直连池

### 5.2 当前未解决

| 项 | 状态 |
|---|---|
| HTTP 池泄漏 | **仍存在，靠 containment**（并发限制 + 自愈重建 client，已实际触发）。对应 httpcore 上游 issue（代理 CONNECT/TLS 失败留 zombie connection），修复在上游审查中。不要描述为"已修复" |
| `data/raw/signal_snapshot` | **已治理**：NTFS 透明压缩 + 2 日 gzip/30 日删除，本次回收 6.34 GiB。T7 完整订单簿在不过期的 `no_forward_validation` 独立保留；任意非触发行的原始 17 位表示只保留 30 日 |
| `market_stream.duckdb` | `bids_json`/`asks_json` 仍是 VARCHAR 存 JSON（同 §3.3 的坑），拆列有收益但只几百 MB，暂不值得动 |
| 夜间行情裁剪 | 美国深夜 tick 极少，理论上窗外可再收紧。但深度不可重建，必须先测量再动 |
| 联合历史重放 | 需用 Single Runs 固定初始化时间做严格 vintage 对齐；Previous Runs lead 1 仍不是完整单一 vintage |
| 挂单排队模拟 | 已估算吃单成本，但未模拟挂单排队、短时撤单 |

### 5.3 已被推翻的结论（不要沿用旧数字）

用 WRH 高频序列重算 813 天后，**逆转率全线上升**（旧值来自 IEM 小时采样，会错过整点之间的真实峰值）：

| 站点 | 逆转率 IEM→WRH | p90 剩余升温 |
|---|---|---|
| KLAX | 1.1% → **17.2%** | 0.0 → **1.8°F** |
| KDAL | 6.4% → 49.4% | 1.0 → 1.9°F |
| KSEA | 6.7% → 50.6% | 1.0 → 2.3°F |
| KLGA | 9.4% → 39.7% | 1.0 → 1.8°F |
| KATL | 13.9% → 46.1% | 2.0 → 3.6°F |
| ZUCK / ZUUU | 23.6% / 31.5%（**不变**） | 1.8°F |

三个后果：

1. **"KLAX 几乎不逆转"不成立**。仍是十城最低，但 17% 和 1% 是完全不同的风险画像
2. **"中国站尾盘风险更高"反转了**，但这是陷阱：中国站数值不变是因为它们本来只有每小时一条观测（约 25 点/日 vs 美国站 280–324 点/日），**其低逆转率同样是采样不足的假象**，不是真实优势
3. p90 剩余升温普遍升至 1.8–3.6°F，原 `physical_margin_f <= −2.0` 阈值偏松，已按季重定

### 5.4 季节窗口不等于日历季

JJA（6/7/8 月）是气象学惯例，不是各城市实际热季。按固定日历切会对不同站造成方向不同的偏差，**KLAX 最严重**（6 月 June Gloom 海洋层最强，9 月才最热、10 月有 Santa Ana 焚风）。

当前热季窗口（实测确定，非日历）：KLAX 7/1–10/31、KLGA 6/1–9/30、KORD 6/1–8/31、KMIA 5/1–10/31、KATL/KDAL/KHOU 5/1–9/30、KSEA 7/1–9/30。

**过拟合防范**：每站 n 仅约 250–300。窗口只能由气候机制假设 + 同质性检验决定，**禁止以"哪个窗口阈值最好看"作为选择依据**。

样本量口径：覆盖期 2024-06-01 至 2026-08-22（813 天/站）。6/7/8 月各覆盖 3 年，其余月份仅 2 年。扩宽窗口增加天数，但新增月份只贡献 2 年样本——年数不足是独立局限。

---

## 6. 当前进度与下一步

### 6.1 核心结论（当前）

**NO 侧策略：证据不足，不能判定为正期望，禁止进入执行阶段。**

不是观察到明确负收益，而是 22 个已结算事件与真实深度归档的重叠数为 **0**，保守期望必须记为 N/A。

质量窗口默认排除并统一读取 retention gzip 后，已确认的执行事实：真实 NO ask 比 `1−YES last` 平均贵 **2.1¢**；$200 买 NO 完整成交率 **84.0%**，$1000 为 **46.3%**。22 个已结算事件与真实深度归档重叠仍为 0，收益继续是 N/A。

公开成交流水把 `p` 陈旧从推断变成了实测：22 个已结算事件、34,822 个目标当地日采样点中，7.0% 在此前没有成交；有成交者最后成交年龄 p50 195.6 分钟、p90 1,755.4 分钟，64.3% 超过 60 分钟。它仍不能恢复 ask/bid/深度。

高 NO 尾桶的“进不了场”已用真实深度单独审计（不是收益或定价结论）：质量窗口排除后的 34,408 个 NO≥0.99 快照中，真实 NO asks 为空 60.3%（Wilson 95% 59.8%–60.9%）；KLAX/KLGA 分别为 59.3%/66.1%。原始摘要 `best_ask=1.000` 且实际 asks 阶梯为空占 58.3%，所以不能把该摘要字段当卖单。$20/$200 的完整买入率为 KLAX 37.7%/26.8%、KLGA 32.4%/23.1%；没有任何无条件可进场的仓位。仅可把 $20 作为满足真实 ask、非近端点、完整深度和 p90 成本闸门后的纸面研究上限，`execution_enabled` 仍为 false。详见 `data/no_entry_accessibility_report.md`。

中间 NO 价位可达性已用排除质量窗后的 55,466 个配对完成：真实 ask 分箱同时保留同期空盘与严格此前 ask cohort，
并比较 $20/$50/$100/$150/$200 的 NO 买入和 YES 卖出路径。KLAX 的平衡候选为 0.70–0.85，KLGA 为 0.50–0.70；
这只是可达性筛选，不是盈利结论。当前簿 hurdle 是成本门槛，不能替代未来价差或逆转风险。成交价不是可成交 ask，
报告为 `data/price_band_accessibility_report.md`，命令为 `analyze-price-band-accessibility`。

影子账本作用域已修正为 `event_id + market_id + token_id + market_day`：每个 token 独立库存、成本、活动单、累计买入成本、PnL 和 round trip；`(station_id, market_day)` 只作相关统计与 $200 风险聚类。v2 重放覆盖 50 个 station-day、81,349 个真实深度快照；price-band 为 26 单/3 fills，weather-market-lag 为 207 单/7 fills。后者只有 KDAL 1 个合法同-token round trip、realized `+$15.0684931507`，因 KMIA token 仍有未验证出场而整体净 PnL 为 N/A；旧 `$13.5571351545` 已确认全部是跨 token 串账，详见 `data/shadow_token_scope_reconciliation_report.md`。旧 v1 账本只读隔离，新默认账本为 `shadow_orders_v2_token_scoped.jsonl`，`execution_enabled=false`。

YES+NO 互补配对影子离线回放（数据截止 2026-08-28 14:01:40 +08:00，固定分析上限 14:01:46）覆盖
103,991 个可用配对盘口快照和 43,543 个 token-native 成交事件。默认 queue-aware 配置提交 293 个 pair、
3 个单腿 fill、已配平 **N=0**；KLAX 为 28/1/0、KLGA 为 32/0/0，trade-through 为 293/2/0，touch
为 293/8/0。58 个 station-day 聚类中 KLAX 5 个、KLGA 6 个，站点分层 n<30 不可靠；queue-aware
未配平仓位的同 token 真实 bid 影子 unwind 汇总为 `-$0.2844544674`。计划成本低于 1 不计为 locked
edge；该结果是排队/风险诊断，不能宣称正期望或执行可行。固定 2×3×4 敏感性网格共 24 个预先声明场景，
仅作诊断、不选择最优参数，详见 `data/complement_pair_strategy_report.md`。

Bias significance gate 已完成只读审计，未改变 live calibration：当前 gate 并非只有样本量，仍要求
严格 walk-forward RMSE/Brier/LogLoss。321 个 `lead_days>=1` 样本分为 4 个 station/model 组；提议的
`|mean bias|/SE > 2` 只会额外禁用 KLGA `multi_model_blend`（n=81，0.439°F/0.247°F=1.777）的当前 bias，
其余当前已应用组 KLAX `gfs_seamless` 为 2.598。该组的 gated OOS RMSE 略降但 Brier/LogLoss 变差，且
在两个不显著折（合计 20 个测试样本）中无条件修正使折级 MAE/RMSE 变差；证据不支持自动改 gate，详见
`data/bias_significance_audit.md`。

### 6.2 前向样本进度（主要卡点）

季节阈值重定后**时钟归零**：

- 旧阈值触发 12 条、结算 2 条；新保守阈值下仍符合的只有 4 条，**新口径已结算 0 条**
- 距 `heat_2026` 的 30 个独立已结算样本：还差 **30 条**
- 其他季节：n=0
- **每季独立需要 30 个样本**，全年验证约需 120 个
- 前向样本全部采集于 8 月，即使攒够也只是"热季有效样本"，不能推广到其他季节

按当前速度这是数周的事，催不来。旧样本未删除但必须与新版本分开统计。

### 6.3 待办（按优先级）

1. 继续积累新阈值前向已结算样本；每季独立 30 条的门槛不变
2. 挂单排队、短时撤单与夜间行情裁剪只做测量后再决定

已完成：官方状态组件级 5 分钟订阅；2026-08-26 官方维护最终窗口为 04:00–07:30 UTC，另按本地遥测增加 07:30–07:50:48 恢复质量窗。74 次精确重连中 66 次落在官方窗口、8 次落在恢复窗；全量流 19,196 条、检查点 9,186 条按时间维度默认排除。旧版 28 次只有汇总，虽运行区间与维护重叠 10.7 分钟，但逐次时间已永久丢失，不能强行归因。`data-api/trades` 已接入；链上 SQL 已评估为宏观统计备选，本轮未实现。NO 尾桶可达性已用真实 asks、严格截止 p/成交带、当前公开 market-rule 读数和默认质量窗排除完成审计。当前结论统一从 `CURRENT_CONCLUSIONS.md` 进入。

### 6.4 T8/T10/T11 为何仍是 N/A

T8 双侧分层、T10 动态加仓、T11 邻桶价差都缺"已结算结果 × 真实深度"重叠样本，按停止条件保持 N/A，**没有用代理数据强行回测**。最新重跑仍是 0 个重叠事件。T6 当前有 25 个未结算前向出局桶，质量窗口剔除后 15 分钟深度覆盖率 76.0%（Wilson 95% 56.6%–88.5%），n<30，统计不可靠，不能视为结论。

### 6.5 数据周期四态与 QUIET maker 验证 v2（2026-08-29）

v1 的 `QUIET=0` 保留为诊断性 **N/A：在不完整事件语义/指标覆盖下不可达**，不是“市场不存在安静窗口”的证据；v1 报告和账本不覆盖。v2 完成真实输入闭环后，已出现有限的 QUIET 观测，但仍没有 maker fill，因此结论仍是诊断性 N/A，而非正/负收益结论：

- `information_clock.py` 以 source+receipt 时钟区分 `HARD_RESET`、`SOFT_UPDATE`、`NO_OP`、`INVALID`。日高、每 token 的 physical margin/tier、forecast hash、结算/官方状态和固定天气风险带都在当前可见状态内跟踪；新报文时刻、同语义 settlement/status 和温度/格式抖动不重置状态。`INVALID` 在状态推进前拒绝。
- `market_microstructure.py` 用 canonical receipt-available WS/Data API trades 建立严格此前、同站/token/price-band/local-hour 的 trade-intensity baseline；从真实 `book`/`price_change` 重建 L2 churn，明确把同价位撤除与可验证 traded volume 分开，archive/reconnect gap 为 UNKNOWN；跨桶 mass 只由同一事件、300 秒同步的 token-native bid/ask 区间诊断得出，绝不用 `1-p` 或对侧 token 代理。
- `market_regime.py` 将 `WARMUP_INSUFFICIENT_BASELINE`、`UNQUOTABLE_INCOMPLETE_BOOK`、`UNKNOWN_TAPE_GAP`、`UNKNOWN_CROSS_BUCKET_SYNC` 与 `UNSTABLE_TRUE_VIOLATION:*` 分开。未知绝不填 0；一个 token 的不完整簿不会污染同 station-day 的健康 token。HARD 无条件回 EVENT，SOFT 以预声明规则回/留 DIGESTION，NO_OP 不重置 anchor。
- 最终固定 vintage replay 覆盖 `124,301` 个配对 book、`248,602` 个 token 快照、`20,344` 个输入信息事件、`43,543` 个 canonical token-native trades、`62` 个 station-day cluster。信息报告另列 `19,369` 个 accepted 与 `975` 个 INVALID 事件，并按 kind/station 给出四类计数。
- 状态计数为 strict `EVENT=15,497`、`DIGESTION=228,474`、`QUIET=0`、`PRE_RELEASE=4,631`；neutral `15,497/231,022/37/2,046`；lenient `15,497/231,696/147/1,262`（顺序同前）。neutral 的 37 个 QUIET 来自 18 个 token machine / 4 个 station-day，lenient 为 49 / 7；不能把快照或 token 当独立样本。
- neutral mandatory coverage：churn `92.3%`、cross-bucket mass `0.9%`、价格斜率/累计移动 `60.2%`、完整双边簿指标 `61.0%`、trade intensity `40.6%`。主要未进入 QUIET 的原因仍是 `UNKNOWN_CROSS_BUCKET_SYNC`、warmup、unquotable book 与 tape gap；这些是 coverage 限制，和已知的真实 instability 分开报告，不能靠放宽阈值制造 QUIET。
- 实际 QUIET 入口后，strict/neutral/lenient 分别产生 `0/13/72` 个影子 order，但全为 `0` fill、`0` round trip，所有 PnL/markout 为 N/A。预声明 `$20/$50/$100/$200` grid 分别有 `13/13/11/6` order、均为零 fill，风险账本无 discrepancy；neutral 有 29 条 strictly-later decision-regret 记录和 37 个诊断性 matched control，但它们不能反向改变历史订单，也不构成因果或盈利声明。
- 三个策略族仍使用隔离账本；inventory/PnL 作用域为 `event_id + market_id + token_id + market_day`，仅 station-day 由共同 `$200` cap 聚合。v2 仅离线读取归档，`execution_enabled=false`，没有 key、钱包、签名、Relayer、POST/DELETE order、User WebSocket，也未启动/停止/改写任何常驻策略。

机器产物：`data/information_reaction_report_v2.md`、`data/information_clock_analysis_v2.json`、`data/quiet_window_strategy_v2_report.md`、`data/quiet_window_strategy_v2_analysis.json`、`data/quiet_window_v2_size_grid.json`。全量重跑为 `.venv\Scripts\python.exe -m poly_weather analyze-quiet-window`；只有新的前向归档使 QUIET 及实际 queue-aware fill 数量足够后，才可按 station-day 聚类评估 maker fill、markout、spread capture 与 PnL。

---

## 7. 验证要求

每次改动后：

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
```

（本机 `uv` 不在 PATH，直接用 venv 里的 python）

- 当前基线：**303 passed**，Ruff 全绿
- 提交前确认无密钥进入版本控制。注意 `adapters/wrh.py` 会从 weather.gov 抓公开 Synoptic token——必须是运行时动态获取，不能硬编码或写进配置
- `data/` 保持在 `.gitignore` 里
- 不要 push 到远端，除非用户明确要求
- 按逻辑主题拆多个语义清晰的 commit，不要一个巨型 commit
