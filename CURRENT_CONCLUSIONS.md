# 当前研究结论（唯一入口）

生成时间：2026-09-01 14:30 +08:00。

本文件是当前结论的唯一入口。下方明确区分稳定判断和会随归档增长的快照；历史报告若与本文件冲突，
以本文件及其链接的当前报告为准。

## 守护链恢复（2026-08-31）

2026-08-29 00:08–2026-08-31 08:50 UTC 发生本地 daemon outage，约 50 小时无前向数据。原因：天气 DuckDB writer OOM（当时无内存限制）、市场 ws status 文件全 NUL、shadow cursor 文件损坏。质量窗口已关闭（`local-daemon-outage-2026-08-29`），该时段所有分析默认排除。

修复后四个守护进程已由 Windows Task Scheduler 接管，当前状态正常：
- market-supervisor: PID 48720，任务 `Running`，440/440 book complete；仅做一次必要的接管重启，未做额外 kill 测试
- weather-daemon: PID 45400，任务 `Running`，受控 kill 后由 runner 自动重启，WRH/NWS/METAR 持续更新
- signal-engine: PID 47376，任务 `Running`，受控 kill 后由 runner 自动重启，evaluation 与 heartbeat 持续前进
- shadow-follower: PID 48184，任务 `Running`，受控 kill 后由 runner 自动重启；195 orders/3 fills/0 round trips/realized PnL 0 均未重复，cursor `restart_count=2`、`halted=false`、discrepancy=0

所有 status 文件现在有 SHA256 checksum、`.last_good` 备份、PID 存活检查。四个 Task Scheduler 任务已端到端验证为 runner 所有；weather/signal/shadow 的非零退出自动拉起已实测。market 接管产生 `2026-08-31 10:05:38–10:15:30 UTC` 的本地 L2 质量窗口 `local-task-scheduler-market-takeover-2026-08-31`，已关闭并保持 `default_excluded=true`，绝不回填伪 L2。

原 health 脚本因 PowerShell `$PID` 大小写不敏感而误显示检查进程自身 PID，且只检查 checksum 字段存在，曾制造假健康输出；现已改为调用安全 `stream-status`，验证 checksum、heartbeat、PID liveness、实际命令行和 `execution_enabled=false`。完整验收为 303 tests passed、Ruff 全绿。

OOM 防护已到位：DuckDB memory_limit=256MB、batch=16、single thread、4GB temp directory 限制。

## 稳定判断

- **没有任何统计依据支持执行。** `execution_enabled=false`，没有钱包、签名、鉴权或下单路径。
- 新保守 `heat_2026` 阈值下有效触发 4 条、已结算 **N=0**，距同季 30 条还差 30 条；其他季节 n=0，禁止跨季合并。
- 22 个已结算事件与本地真实深度归档重叠仍为 **N=0**，所以依赖真实可成交入场价的收益、T3/T8/T10/T11 保持 N/A。
- **成交价不是可成交 ask。** `data-api/trades` 的 token 自身成交价可用于成交时间、成交量和 VWAP，
  但不能恢复当时 resting ask/bid、spread、$200/$1000 深度或滑点；绝不以它替代真实订单簿。
- `prices-history.p` 也不是盘口；NO 侧不允许用 `1−p` 或 `1−YES` 代理。
- Polymarket 前端在 spread≤$0.10 时显示 bid/ask 中点、较宽时显示最近成交；所以前端价不能默认等同于 `prices-history.p`，买入仍取真实 NO ask。
- WRH 高频重算推翻 IEM 小时采样结论：KLAX 16:30 后逆转率是 17.2%，不是 1.1%。
- **中间价位只解决“能不能进”，不自动解决“是否赚钱”。** 真实 NO ask 分层显示 KLAX 的候选
  区间是 0.70–0.85，KLGA 是 0.50–0.70；这两个区间的 $200 直接买 NO 可达性很高，
  但目标毛价差仍必须覆盖当前簿的双边滑点与两笔手续费，并同时通过逆转风险闸门。
- **价格路径的首轮前向结果不支持正期望。** 在真实 NO ask 且 $200 深度完整的入场后，
  只追踪严格晚于入场的同一 NO token best bid；KLAX 0.70–0.85 的 +5/+10/+13/+20¢ 达标率为
  72.4%（63.2–80.0%）/49.5%（40.1–58.9%）/32.4%（24.2–41.8%）/8.6%（4.6–15.5%），
  KLGA 0.50–0.70 为 53.8%（44.3–63.0%）/50.9%（41.6–60.3%）/50.9%（41.6–60.3%）/
  47.1%（37.8–56.6%）（均为 Wilson 95%）。
  这是连续前向路径快照，不是已结算胜率；在 $200 路径子样本的 p90 往返成本分别为 KLAX 37.74¢、
  KLGA 25.95¢，因此四个目标在两站的 p90 净价差均为负，不能宣布正期望。
- **上述价格路径报告只是固定 $200 taker 压力基线，不是否定 maker 影子策略。** 它把每个
  五分钟点假设为立即吃满 $200，未模拟限价挂单、排队、部分成交、撤单/重报价、补仓、分批退出或
  资金周转；因此不能据此否定 $20/$50/$100 分批 maker 策略。$200 在影子口径中是每个
  market-day 的累计库存成本上限，不是每笔交易的固定投入。KLAX 105、KLGA 106 个点是相关的
  名义快照样本，真正独立统计单位至少是 market-day，同站同日多个桶仍不能当独立样本。
- **影子成交仍不是执行授权。** touch、queue-aware、trade-through 是由历史盘口和真实相反方向
  成交事件形成的上界/基准/保守区间；没有真实订单 ID 和确切排队位置，不能把任一模型写成已实现
  成交率或可执行收益。正常 maker 退出手续费按 0，只有风险处置才用真实 bid 深度和官方 taker
  费率；成交价不是可成交 ask/bid。
- **当前只读影子回放已按 token 隔离，但仍不是执行结论。** v2 重放覆盖 50 个独立
  station/market-day（550 个 token portfolios）、81,349 个真实深度快照和 1,187 个配置价带候选；固定 price-band
  queue-aware 为 26 单/3 fills，weather-market-lag 为 207 单/7 fills。所有订单、库存、成本、
  PnL 和 round trip 均按 `event_id + market_id + token_id + market_day` 隔离；station-day
  只作相关统计与 $200 累计买入成本/风险聚类，绝不跨 token 平仓。
  归档 token 与公开成交共有 26,686 条事件级资产交集（WS 侧 27,379 条中 26,875 条与
  canonical Data API hash+token 对上，另有 504 条 WS 未匹配而 fail-closed），所以成交带不是
  token N=0；但影子 fills 仍不是实际订单成交。weather-market-lag 中只有 KDAL 一个合法同-token
  round trip，realized PnL `+$15.0684931507`；KMIA 入场库存仍未验证同-token 出场，故整体净 PnL
  **N/A**，不能当作正期望。此前 `$13.5571351545` 的旧结果已确认是 Miami 两个 token 串账，
  全部标记 `INVALID_CROSS_TOKEN_PNL`（修复前曾标为 `INVALID_PENDING_TOKEN_SCOPING_FIX`），详见
  [token 作用域对账](data/shadow_token_scope_reconciliation_report.md)。
  22 个已结算目录事件与深度仍重叠 **N=0**，结算结果与持有到结算 PnL 继续 N/A。详见
  [影子策略回放报告](data/shadow_spread_strategy_report.md)。
  随后 2026-08-27 19:00 +08:00 的 `shadow-spread-engine --supervised --once` 烟测实测 v2 ledger
  schema=2、26 个订单、3 个 fills、无 HALTED/不变量差异且 `execution_enabled=false`；运行期间归档
  新增 102 个快照，但订单/成交/PnL 状态与主回放一致。
- **YES+NO 互补配对回放没有形成 locked pair。** 截至 2026-08-28 14:01:40 +08:00（固定分析上限
  14:01:46），103,991 个可用配对盘口快照、43,543 个 token-native 成交事件中，预先声明的默认配置在
  queue-aware 模型下提交 293 个 pair、3 个单腿成交、已配平 **N=0**；KLAX 为 28/1/0、KLGA 为
  32/0/0（提交/单腿/配平）。trade-through 更保守，为 293/2/0，touch 仅作乐观上界为 293/8/0。
  按 station-day 聚类，整体 58 个 cluster，KLAX/KLGA 分别 5/6 个；Wilson 区间均应按 n<30 的站点
  分层视为统计不可靠。未配平仓位走同 token 真实 bid 影子 unwind，queue-aware 汇总 unwind PnL 为
  `-$0.2844544674`。这是排队/风险处置诊断，不是实际订单或正期望，不能把计划 YES+NO 成本低于 1
  当作收益。固定 2×3×4 敏感性网格共 24 个预先声明场景，只用于诊断，不选择最优格子。
- **Bias significance gate 仅完成只读审计，未改变校准或策略。** 321 个 `lead_days>=1` 样本、
  4 个 station/model 组中，增加 `|mean bias|/SE > 2` 只会额外禁用 KLGA `multi_model_blend`
  （n=81，z=1.777）；已应用的 KLAX `gfs_seamless` 为 z=2.598。该提议的门槛在一个当前应用组
  会改变 gate，但 gated OOS 的 Brier/LogLoss 变差；两个不显著折合计 20 个测试样本中，
  无条件 bias 修正在折级 MAE/RMSE 变差。证据不足以自动改 gate，详见审计报告。
- **能进不等于能按 $200 卖出。** 同一报告另算了未来 bid 深度能完整承接原始 shares 的达标率；
  KLAX +5/+10/+13/+20¢ 为 65.7%（56.2–74.1%）/27.6%（20.0–36.8%）/23.8%（16.7–32.8%）/
  8.6%（4.6–15.5%），KLGA 为 50.9%（41.6–60.3%）/50.9%（41.6–60.3%）/50.9%（41.6–60.3%）/
  47.1%（37.8–56.6%）（均为 Wilson 95%）。
- **跳空与止损已实测，但不能把观察到的 bid 当成止损订单成交保证。** KLAX 归零率随目标为
  23.8%（16.7–32.8%）/30.5%（22.5–39.8%）/44.8%（35.6–54.3%）/53.3%（43.8–62.6%），
  归零样本的最低 bid 中位数为 0.62/0.62/0.80/0.755；KLGA 为 44.3%（35.2–53.8%）/
  47.2%（37.9–56.6%）/47.2%（37.9–56.6%）/48.1%（38.7–57.6%），最低 bid 中位数均为 0.03。
  归零样本中观察到入场价 95%–100% 的 bid 带，KLAX 为 16.0%（6.4–34.7%）–60.7%（47.6–72.4%），
  KLGA 为 80.9%（67.5–89.6%）–82.0%（69.2–90.2%）；这些是盘口观测，
  不是实际排队/成交结果。

## 当前快照

所有比率均带 Wilson 95%；n<30 的口径明确标为统计不可靠。

| 快照 | 当前结果 | 数据截止/生成时刻 |
|---|---|---|
| 真实 NO 深度 | 53,852 配对、32,456 可用；$200 买 NO 完整成交 84.0%（83.6%–84.4%），$1000 为 46.3%（45.8%–46.9%） | 2026-08-26 17:29 +08:00 报告生成 |
| 高 NO 尾桶可达性（NO≥0.99） | 34,408 个质量窗已排除的真实深度快照中，真实 NO asks 为空 60.3%（59.8%–60.9%）；KLAX/KLGA 分别 59.3%（57.7%–60.9%）/66.1%（64.6%–67.6%）。摘要 `best_ask=1.000` 且真实 asks 为空占 58.3%（57.8%–58.9%），所以用户“进不了场”主因是**无卖单**，不是当前公开 `/book` 所见的 min order 或定价数学。$20/$200 完整买入率为 KLAX 37.7%/26.8%，KLGA 32.4%/23.1%；没有无条件可进场的仓位。 | 2026-08-26 19:24 +08:00 报告生成 |
| T6 已出局桶 | 25 个未结算桶；质量窗口剔除后 15 分钟覆盖 76.0%（56.6%–88.5%），n=25，统计不可靠 | 2026-08-26 17:29 +08:00 报告生成 |
| 成交带陈旧度 | 22 个已结算事件、34,822 个目标日采样点；7.0%（6.7%–7.3%）此前无成交；已知成交年龄 p50/p90=195.6/1,755.4 分钟 | 目标日 2026-08-10–20；2026-08-26 17:37 +08:00 重拉 |
| 上游质量 | 官方维护 04:00–07:30 UTC；实测恢复窗 07:30–07:50:48 UTC；74 次精确重连=66 官方窗+8 恢复窗 | 2026-08-26 17:33 +08:00 审计 |
| 高频天气特征 | KLAX 逆转 17.2%；KLGA 39.7%；KDAL 49.4%；KSEA 50.6% | WRH historical_backfill 截止 2026-08-22；仅事后特征，不可作严格 vintage 回测 |
| 中间 NO 价位可达性 | 排除质量窗后保留 55,466 个配对。十城当前真实 NO asks 为空 38.9%（38.5%–39.3%）；KLAX 0.70–0.85（n=346）当前无 ask 0/346（0.0%–1.1%），$200 买 NO 完整 98.6%（96.7%–99.4%），同簿成本 hurdle p50/p90=3.35¢/17.07¢；KLGA 0.50–0.70（n=614）当前无 ask 0/614（0.0%–0.6%），$200 买 NO 完整 100.0%（99.4%–100.0%），hurdle=3.22¢/14.07¢。≥0.99 cohort 的 $200 买入率仅 KLAX 32.1%、KLGA 31.3%。 | 订单簿截止 2026-08-26 20:02:39 +08:00；生成 20:04:08 +08:00；详见中间价位报告 |
| 价格路径（前向快照） | 订单簿配对 55,682，市场时间线 440；可用 $200 入场 2,379（候选 2,385，因深度不足排除 6），其中 KLAX 主区间 105、KLGA 主区间 106。已结算目录 22 个事件与深度事件重叠 **N=0**、已结算可用入场 **N=0**；其余 2,379 是未结算连续路径，realized PnL 仍 N/A。KLAX p90 成本 37.74¢、KLGA 25.95¢；两站 +5/+10/+13/+20¢ 的 p90 净价差均为负。 | 订单簿截止 2026-08-26 21:01:21 +08:00；生成 21:02:01 +08:00；详见价格路径报告 |
| 影子天气严格 join | 9,829 条 realtime 观测；81,349 个快照中 77,748 次重复对齐、3,448 次新观测、153 次因 source/receipt 双截止无可用观测；不使用 historical_backfill，不把状态复制造成新事件。 | 回放生成 2026-08-27 18:43:15 +08:00 |
| 只读影子限价/价差回放 | v2 token-scoped：50 个独立 station/market-day、81,349 个快照、1,187 个真实 ask 价带候选；固定 price-band queue-aware 26 单/3 fills，weather-market-lag 207 单/7 fills；整体净 PnL N/A（未平仓 token 不用代理估值），KDAL 仅 1 个合法同-token round trip `+$15.0684931507`，n=1 统计不可靠。canonical 成交带与归档 token 交集 26,686 条事件，WS 26,875 条经 hash+token 验证、504 条未匹配 fail-closed；旧 `$13.56` 已确认全为跨 token 串账并归档对账。22 个已结算事件与深度仍重叠 **N=0**。 | 订单簿截止 2026-08-27 18:34:55 +08:00；生成 18:43:15 +08:00 |
| v2 长期只读 follower | 首次连续观察 30.08 分钟；随后重启恢复 `restart_count=1`。cursor/heartbeat 前进，26 条 ledger orders 与 3 fills 未重复，`halted=false`、discrepancy=0；仅从已有归档尾部开始积累前向证据。 | 首次启动 2026-08-28 11:05:03 +08:00；重启 11:35:32 +08:00 |
| YES+NO 互补配对影子 | 103,991 个配对快照、43,543 个 token-native 成交事件；默认 queue-aware 293 个提交、3 个单腿 fill、已配平 **N=0**；KLAX 28/1/0、KLGA 32/0/0。trade-through 293/2/0，touch 293/8/0；station-day 聚类 58 个，KLAX/KLGA 为 5/6（n<30 分层不可靠）。固定敏感性网格 24 个场景；queue-aware unwind `-$0.2844544674`。 | 订单簿截止 2026-08-28 14:01:40 +08:00；固定分析上限 14:01:46；报告生成 14:59:40 +08:00 |
| Market-State Challenger v1 | 冻结历史 cutoff 前 1,035 raw candidates、459 unique episodes；328 有时间覆盖、316 可分类，SURVIVING/FAILED/UNCONFIRMED/UNKNOWN=`6/0/310/143`，所有 5/15/30/60/120m outcome 完整仅 32。只有 KATL 一例 surviving 命中 +5/+10/+13/+20¢；KLAX 一例端点横盘、KLGA 为 0、FAILED 为 0。未通过 ≥30 station-day、两站一致性和失败突破识别门槛，分支停止。 | 固定 cutoff 2026-09-01 00:00 UTC；历史诊断，不是 OOS、maker fill 或 PnL |
| Bias significance gate 审计 | 321 个 lead_days≥1 样本、4 组；提议 `|bias|/SE>2` 只额外禁用 KLGA multi_model_blend。未改校准；walk-forward/OOS 证据不足以自动采用该 gate。 | 审计生成 2026-08-28 |

当前报告：

- [真实 NO 深度](data/real_no_book_report.md)
- [T6 退出可行性](data/eliminated_no_exit_report.md)
- [公开成交流水](data/public_trade_tape_report.md)
- [NO 尾桶进场可达性](data/no_entry_accessibility_report.md)
- [中间 NO 价位可达性](data/price_band_accessibility_report.md)
- [中间 NO 价格路径成功率](data/price_path_report.md)
- [只读影子限价/价差策略回放](data/shadow_spread_strategy_report.md)
- [YES+NO 互补配对影子策略](data/complement_pair_strategy_report.md)
- [Bias significance gate 审计](data/bias_significance_audit.md)
- [上游质量窗口](data/polymarket_maintenance_audit.md)
- [WRH 高频重算](data/high_frequency_weather_reanalysis.md)
- [同季前向进度](data/no_forward_validation_report.md)

## 数据周期四态与 QUIET maker v2（2026-08-29）

v1 `QUIET=0` 仅表示在不完整的事件语义和微观结构覆盖下不可达（N/A），不能被解释为“没有安静窗口”。v2 以真实、receipt-gated 输入重跑后，得到如下当前结论：

- 事件时钟现在区分 `HARD_RESET/SOFT_UPDATE/NO_OP/INVALID`，而不是把每条新 payload/timestamp 都当 EVENT。station-day 日高、token-specific physical margin/tier、forecast distribution、结算/官方状态和天气风险带均有状态跟踪；新 timestamp、重复规则和小数抖动不能重置 anchor。
- trade intensity 来自 canonical WS/Data API 的严格此前 baseline；L2 churn 来自真实 `book`/`price_change`，撤档不等于 fill、archive/reconnect gap 为 UNKNOWN；cross-bucket mass 只由 300 秒同步的 token-native bid/ask interval 给出。未知覆盖与 `UNSTABLE_TRUE_VIOLATION:*` 分开，绝不用 0 伪装。
- 固定 vintage 覆盖 `124,301` 配对 books、`248,602` token snapshots、`20,344` 信息事件、`43,543` canonical trades、`62` 个 station-day clusters。另有 `19,369` accepted / `975` INVALID 信息事件，kind 和 station 的四分类均已落盘。
- strict / neutral / lenient 的 QUIET 计数为 `0 / 37 / 147`；neutral 的 37 个观测来自 18 个 token machines 和仅 4 个 station-days，lenient 为 49 / 7。故“QUIET 不存在”被推翻，但小样本不构成可交易性结论。
- neutral 已知覆盖为 churn `92.3%`、cross-bucket mass `0.9%`、slope/cumulative `60.2%`、完整簿指标 `61.0%`、trade intensity `40.6%`。cross-bucket 同步、warmup、空/不完整簿与 tape gap 仍是主要阻断因素，必须继续 fail-closed。
- 历史 tick/min-order provenance 修复后的 strict / neutral / lenient shadow order 为 `0 / 22 / 90`，fill 均为 `0`；预声明 `$20/$50/$100/$200` grid 为 `22/22/20/10` orders，仍全部零 fill 且无风险不变量差异。maker fill、markout、spread capture、PnL 均为 N/A。neutral 的 after-the-fact decision regret 与 matched control 仅供诊断，不能改变历史决策或构成因果/盈利声明。
- replay 和账本仍完全只读：`execution_enabled=false`；token inventory 的作用域是 `event_id + market_id + token_id + market_day`，station-day 只聚合三策略 `$200` cap。没有修改、重启或启动三个常驻策略。

报告与机器结果：

- [external-information clock / 四态报告 v2](data/information_reaction_report_v2.md)
- [external-information clock v2 JSON](data/information_clock_analysis_v2.json)
- [QUIET maker 验证报告 v2](data/quiet_window_strategy_v2_report.md)
- [QUIET replay JSON v2](data/quiet_window_strategy_v2_analysis.json)
- [QUIET size grid JSON v2](data/quiet_window_v2_size_grid.json)

## QUIET v2 零成交逐单归因（2026-08-31）

- 先前保存的 `85` 张 v2 订单已逐单复核。归档 Gamma market metadata 提供了 `93` 条、覆盖 `40` 个 token 的下单前 tick/min-order 证据；`85/85` 均为 `VALID_ARCHIVED`，所以“min order 默认为 0”不再是零成交解释。
- 机械归因是 `82` 张 `NEVER_TOUCHED`、`3` 张 `CANCELLED_BEFORE_LATER_TOUCH`。没有任何订单在实际生命周期内以 token 自己的真实 ask 触达 BUY limit；后者的三张只是在撤单后的 30 分钟诊断窗中才触价，不能回填为 fill。
- 在 `7` 个 station-day 独立簇上，`TOUCH_UPPER_BOUND`、`QUEUE_UPPER_BOUND`、`CONSERVATIVE_FILL` 都是 `0/7`；订单级为 `0/85`，订单 Wilson 95% 上界 `4.32%`、station-day 上界 `35.43%`，`n<30`，不可作为负期望或可交易性证明。因为 TOUCH 已为零，当前结论是报价路径/被动性问题，不是队列或成交带覆盖问题。
- `$5/$10/$20/$50` 的固定小额敏感性没有任何 min-order 拒绝，也全部保持三层 `0`；它只是固定历史容量上界，不是选择生产 size 的参数搜索。
- 重建出的 `100` 个 QUIET episode 时长 p50/p90 为 `381/1,229` 秒，每 episode 的同一配对 token-native book observation p50/p90 仅 `2/4`；保存订单暴露 p50/p90 为 `348/949` 秒，`12` 张短于五分钟。`91` 次是 coverage flicker、`9` 次是真实 instability；coverage 来源包括 `UNKNOWN_CROSS_BUCKET_SYNC=89`、`WARMUP_INSUFFICIENT_BASELINE=17`、`UNKNOWN_TAPE_GAP=1`，真实来源仅 `imbalance_extreme=9`。因此不能把全部 `stability_lost` 误写成市场波动或通过放宽标准制造成交。
- cross-bucket known coverage 在 `1/2/5/10` 分钟窗口仅为 `0.047%/0.088%/0.887%/2.976%`；即使到 10 分钟仍有 `53,931` 个缺桶 checkpoint 和 `66,671` 个完整但超龄样本，mass interval 宽度 p50/p90 为 `0.146/0.297`。这些只作覆盖诊断，不改变 5 分钟 fail-closed 阈值。
- 本轮 provenance-corrected v2 重跑产生 `0/22/90`（strict/neutral/lenient）新 shadow orders、仍是 `0` fill；这和固定保存的 85 单 forensic cohort 明确分开，不能混合成独立样本。当前不进入 Champion/Challenger 赛马。

报告与路径：

- [QUIET 零成交逐单审计](data/quiet_order_forensics_report.md)
- [QUIET forensic JSON](data/quiet_order_forensics.json)
- [QUIET 报告 canonical 路径](docs/quiet_report_canonical_paths.md)

**QUIET 状态：CHALLENGER_PAUSED。** 85 单审计已证明当前 join-best-bid 报价在候选区间不可达（TOUCH=0），暂停进一步开发。保留代码和前向日志，等 KLAX/KLGA 累计 ≥30 station-day 且出现真实 touch 后才重新评估。

## Market-State Challenger v1（2026-09-01）

- 这是 PA_Agent 市场突破思想的 clean-room、只读历史诊断；没有复制 AGPL 源码、提示词或测试文本。配置在运行前冻结为 `market-state-challenger-v1`，截止 `2026-09-01T00:00:00Z`，不按结果调参。
- 候选只认当时可见的 `weather_market_lag && weather_improving`；同 token 每个 HARD_RESET information episode 只保留首个候选。trailing 严格早于 candidate，confirmation 使用 candidate 后的真实 NO token 盘口，decision 锚定确认窗结束时或之后首个归档快照，outcome 严格晚于该 decision；HARD_RESET 污染后续 horizon，重复时间戳、跨/锁盘、质量窗、缺口及未知规则均 fail-closed。
- 冻结历史结果：`1,035` 个 raw candidates → `459` 个 unique episodes → `328` 个有时间覆盖 → `316` 个盘口指标已知并可分类。状态为 `SURVIVING_BREAKOUT=6`、`FAILED_BREAKOUT=0`、`UNCONFIRMED=310`、`UNKNOWN=143`，状态守恒通过。由于所有 horizon 均完整的候选只有 `32` 个，且 SURVIVING 只有 `6` 个 station-day、FAILED 为 `0`，远低于预声明的 `30` 个独立 station-day 门槛。
- 六个 surviving 中只有 KATL 一例在 15/30/60 分钟命中 +5/+10/+13/+20¢；其余五例均未命中 +5¢，其中 KSEA 60/120 分钟 bid 变化为 `-0.06/-0.08`。KLAX 只有一例且位于 `0.996` 高价端点，15/30/60 分钟变化均为 0；KLGA surviving 为 `0`。没有 FAILED 样本可验证“失败突破识别更差路径”。
- 微观结构诊断已复用 canonical trades、严格此前 trade intensity、真实 L2 churn 与 cross-bucket mass。churn 在 `345/459` 个候选窗口全程已知；但 cross-bucket mass 在 `439/459` 个候选窗口全程 UNKNOWN，另 `20` 个为 OK/UNKNOWN 混合，所以它只保留为覆盖诊断，不能通过放宽同步边界制造分类。
- **裁决：PA 风格市场突破没有通过升级门槛，停止该分支，不进入 Decision-Continuity Challenger，也不修改 live champion。** 这不证明市场状态永远无价值，而是当前预声明标签没有可复现的 30/60 分钟增量：SURVIVING 稀少、FAILED 不可达、KLAX/KLGA 不一致、完整 outcome 严重不足。
- 报告只使用同 token 真实 archived bid/ask，Wilson 95% 按 station-day 聚类展示；它不是 maker fill、订单或 PnL。`execution_enabled=false`，未写现有 shadow ledger，未启动或停止任何 daemon。

报告与机器结果：

- [Market-State Challenger v1 报告](data/market_state_challenger_v1_report.md)
- [Market-State Challenger v1 JSON](data/market_state_challenger_v1_analysis.json)

## Lead-lag 主线前向状态（恢复后待积累）

2026-08-29 00:08–2026-08-31 08:50 UTC 的约 50 小时 outage 造成前向样本断流。恢复后 shadow follower 已从新数据开始（cursor 含4个今日源），前向样本正在积累中。outage 期间的影子订单（195 订单/3 fills）来自 outage 前，与恢复后的样本明确分段，不混算。

## 运行与数据治理

- market stream：PID 42956，state=running，440/440 book complete，upstream status normal。2026-08-26 的完整订阅采集空档仍保留为历史记录。
- 2026-08-29 00:08–2026-08-31 08:50 UTC 的约 50 小时 local daemon outage 已记录并关闭质量窗口（`local-daemon-outage-2026-08-29`），该时段默认排除。故障原因：天气 DuckDB writer OOM（当时无内存限制）、市场 ws status 全 NUL、shadow cursor 损坏。修复：DuckDB memory_limit=256MB/batch=16/single thread；所有 status 文件加 SHA256 checksum + `.last_good` 备份 + Windows PID liveness 检测；Windows Task Scheduler 注册四个 daemon 任务（开机/登录启动，非零退出自动重启）。
- `signal_snapshot` 已启用 NTFS 透明压缩并纳入 2 天 gzip/30 天删除策略；本次按目录分配字节净回收 6.34 GiB。
- T7 触发点、完整 NO 订单簿和里程碑独立保存在 `data/raw/no_forward_validation`，不受上述 30 天删除影响。
- 任意非触发 signal 的原始 17 位浮点只能在 30 天窗口内逐行审计；规范化库永久保留约定精度，
  但不能恢复任意旧行的原始 17 位表示。详见 [signal_snapshot 存储报告](docs/signal_snapshot_storage_report.md)。
- `shadow-spread-engine --supervised` 默认是持续只读跟随，使用原子 cursor + 永久 append-only ledger；
  `--once` 仅用于有限归档烟测。状态会显示 heartbeat、cursor/restart、活动影子单、库存、成交和
  `upstream_maintenance`；启动时执行依赖扫描，`execution_enabled` 始终为 `false`。
- v2 follower 已按上述证据实际运行并完成一次精确重启验证；当前状态只展示
  `shadow_spread_status_v2_token_scoped.json`，旧 `shadow_spread_status.json` 保留为
  `shadow_spread_status_v1_legacy_read_only.json` 只读证据，不再作为 live 状态。互补配对分析同样
  只读、离线、独立账本；其 24 格敏感性结果不改变默认配置或执行边界。
