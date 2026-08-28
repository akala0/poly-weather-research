# 当前研究结论（唯一入口）

生成时间：2026-08-28 14:59:40 +08:00。互补配对影子回放订单簿数据截止：2026-08-28 14:01:40 +08:00；固定分析上限：14:01:46 +08:00。

本文件是当前结论的唯一入口。下方明确区分稳定判断和会随归档增长的快照；历史报告若与本文件冲突，
以本文件及其链接的当前报告为准。

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

## 运行与数据治理

- market stream 当前 run `328905f1-979b-4beb-b4f4-7462c732c9d8`：440/440 book complete、0 重连、官方状态 normal。
- 2026-08-26 部署的完整订阅采集空档为 09:15:22.749–09:16:03.879 UTC，约 41.1 秒；当天首批 220 token 在 7.2 秒恢复，次日热订阅完成后才达到完整 440。
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
