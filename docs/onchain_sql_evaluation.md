# Polymarket 链上 SQL 路径评估

更新日期：2026-08-26。本轮只做能力评估，不接入任何链上数据服务。

## 结论

现在不值得接入链上 SQL。对于“某 token 在某时刻之前最后一笔成交”这类点查，公开
`data-api.polymarket.com/trades` 明显更合适：它已经按 event/condition/token 归一化，直接给出
side、size、price、timestamp、asset 与 transactionHash，并支持 start/end 时间窗。本机对 22 个
已结算天气事件实际拉取 63,534 条规范 taker 成交约需 25 秒，规模和延迟都不构成瓶颈。

链上 SQL 的优势在跨市场、跨参与者、长时间范围的宏观聚合，而不是恢复某一时刻的可成交盘口。
等出现明确的类别级研究问题时再接入，避免现在增加第三方 schema、索引延迟和地址语义维护成本。

## 能力边界

Polymarket 官方列出的链上数据包括 trades、balances、positions 和 redeems。Goldsky 可把这些活动
流式写入自有数据库/仓库；CryptoHouse/ClickHouse、Dune 与 Allium 提供 SQL 或分析平台。官方示例
覆盖 notional volume、maker/taker USDC volume、锁定 USDC 与估算 open interest。

订单簿不在链上。Polymarket 使用链下 CLOB 撮合、链上结算；未成交挂单、撤单、排队位置、当时
resting bid/ask 和完整深度不会因为查询链上成交而恢复。因此链上 SQL 与 `/trades` 都不能填补：

- 历史真实 ask/bid 与 spread；
- $200/$1000 当时可成交量和逐档滑点；
- 挂单排队、短时撤单和维护期间的真实深度完整性。

这些指标仍只能依赖本项目从 Market WebSocket 实时归档的订单簿；没有归档重叠时必须保持 N/A。

## 路径对比

| 需求 | `/trades` | 链上 SQL | 当前选择 |
|---|---|---|---|
| 某 token 截止时刻前最后成交 | event/market + start/end 直接查，字段已归一化 | 需要理解合约事件、token/condition 映射和索引延迟 | `/trades` |
| 22 个天气事件逐笔成交/VWAP | 22 个 event 查询即可 | 可做，但增加平台与 schema 依赖 | `/trades` |
| 整个天气类别多年成交量分布 | 逐 event REST 枚举低效 | 一次 SQL 分组更自然 | 有明确需求时用 SQL |
| 独立参与地址、留存、集中度 | 返回 proxyWallet，但大范围 REST 成本高 | 地址级扫描和去重是 SQL 强项 | 有明确需求时用 SQL |
| balances/positions/redeems 生命周期 | 需组合多个 API | 链上事件天然完整 | 有持仓结构研究时用 SQL |
| 历史订单簿/滑点 | 不支持 | 不支持 | 本地 WebSocket 归档 |

## 各平台适用场景

- Goldsky：适合需要持续、低延迟地把全量链上活动送入自有仓库的长期数据工程；当前 22 个事件的
  点查不值得承担管线运维。
- CryptoHouse/ClickHouse：适合交互式大范围聚合和高基数 group-by，例如类别×月份×地址。
- Dune：适合可复现公开 SQL、共享图表及 notional/TVL/OI 等行业口径；不适合实时信号路径。
- Allium：适合托管式规范链表和跨链分析；当前没有它才能回答的明确问题。

## 决策

本轮不增加依赖、不配置账号、不实现 SQL 客户端。把链上路径保留为“宏观统计备选”：只有当研究
问题明确要求全类别历史、独立地址或 positions/redeems 生命周期时，再选择平台并先审计其 schema、
索引延迟、去重口径和费用。点查与成交陈旧度继续使用公开 `/trades`。

官方资料：

- https://docs.polymarket.com/resources/blockchain-data
- https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets
