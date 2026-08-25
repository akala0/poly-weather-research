# Polymarket 官方文档摘要

来源：https://docs.polymarket.com/ （2026-08-24 抓取归纳）。本文件是团队自用摘要，用于指导本仓库的实现决策；细节以官方文档实时页面为准，不作为对外引用。

## 1. API 总览与基础地址

| 接口面 | Base URL | 鉴权 |
|---|---|---|
| CLOB REST（下单、行情） | `https://clob.polymarket.com` | 公开读无需鉴权；交易需 L2 |
| Gamma（市场/事件元数据） | `https://gamma-api.polymarket.com` | 无 |
| Data API（持仓/历史） | `https://data-api.polymarket.com` | 无 |
| Relayer | `https://relayer-v2.polymarket.com` | Builder/relayer key |
| CLOB Market WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | 无 |
| CLOB User WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/user` | 需 API key/secret/passphrase |
| Sports WebSocket | `wss://sports-api.polymarket.com/ws` | 无 |
| RTDS（实时数据） | `wss://ws-live-data.polymarket.com` | 可选 |

本仓库当前只使用 CLOB REST 的公开只读端点（`/batch-prices-history`、`/prices`）和 Market WebSocket，均无需鉴权，与 README 中"不读取钱包、不签名、不下单"的边界一致。

## 2. 鉴权（本仓库暂不实现，仅记录供未来参考）

两层鉴权，仅在需要真实下单时才涉及：

- **L1（钱包签名）**：对 EIP-712 结构 `ClobAuth`（domain `ClobAuthDomain`，version `"1"`，chainId `137`）签名，证明控制该地址。用私钥对 `{address, timestamp, nonce, message}` 签名。
- **L2（API key HMAC）**：用 L1 签名换取 `apiKey`/`secret`/`passphrase`（`POST /auth/api-key`），此后每次私有请求用 `timestamp + METHOD + path + body` 做 HMAC-SHA256（对 secret base64 解码后签，签名结果再 URL-safe base64 编码）。

L2 请求头：`POLY_ADDRESS`、`POLY_SIGNATURE`、`POLY_TIMESTAMP`、`POLY_API_KEY`、`POLY_PASSPHRASE`。下单还需对订单本身的钱包签名（L1 之外的第二个签名，签名类型 0=EOA / 1=Proxy / 2=Gnosis Safe / 3=POLY_1271）。

**当前状态**：仓库无任何私钥、签名或下单代码路径，`execution_enabled` 恒为 `False`。此节仅作未来若要开放真实执行时的实现参考。

## 3. 订单类型（同上，仅供参考，当前不实现下单）

Time-in-force 四种，默认 GTC：

| 类型 | 行为 | 说明 |
|---|---|---|
| GTC | Good-Til-Cancelled，挂单直到成交或撤单 | 限价单，`postOnly` 可用 |
| GTD | Good-Til-Date，到指定时间自动失效 | 限价单，`postOnly` 可用；到期时间有 1 分钟安全阈值（若想约 90 秒失效，要设成 now+1分30秒） |
| FOK | Fill-Or-Kill，立即全部成交或全部取消 | 市价单，BUY 传美元金额，SELL 传份数 |
| FAK | Fill-And-Kill（类 IOC），立即成交能成交的部分，剩余取消 | 市价单 |

## 4. 行情/订单簿 REST

`GET /book?token_id=...`：返回 `market`（condition id）、`asset_id`、`timestamp`、`hash`、`bids`（价格降序）、`asks`（价格升序）、`min_order_size`、`tick_size`、`neg_risk`、`last_trade_price`。价格/数量均为字符串。**无字段直接表示数据新鲜度**，只有 `timestamp` 快照时刻，陈旧判断需自己做。

`POST /batch-prices-history`（本仓库 `adapters/clob.py` 已用）：`markets`（token id 列表，最多 20 个）、`start_ts`/`end_ts`（Unix 秒）、`fidelity`（分钟）。合法 fidelity 值：1m/5m/15m/30m/1h/4h/1d/1w。

`POST /prices`（本仓库已用，`batch_quotes`）：请求体为 `[{token_id, side}]` 数组（side 为 BUY/SELL），返回按 token 分组的 best bid/ask。

## 5. ⚠️ 与本仓库现有认知的关键差异：p 代理不等于可成交盘口

官方文档没有明确说明 `prices-history` 返回的 `p` 字段在低成交量尾部桶上会陈旧到什么程度，但从 REST 语义可以确认一个结构性事实，支持我们此前观察到的矛盾（物理余量已 <-5°F 但 1-p 仍算出 0.83-0.89）：

- `/prices-history`、`/batch-prices-history` 返回的是**历史成交价序列**（`t`/`p` 点），不是任意时刻的盘口快照。低活跃度桶如果很久没有新成交，最后一个 `p` 点可能是几小时前的价格。
- 真正代表"此刻能否以此价成交"的只有 `/book`（完整深度）和 `/prices`（best bid/ask 快照）。这两个接口是即时盘口，`p` 序列不是。

这直接印证了 T1 任务的方向：**历史回测中用 `1 - p(YES)` 代理 NO 的可成交成本是错的**，必须用同一时刻的 NO token 自己的 `/book` 或 `/prices` 结果，或标记 N/A。已有的 WebSocket 深度归档（`market_stream.py`）走的是正确路径（`book` 全量快照 + 逐档 `price_change` 增量），这条路径本身没有 p 代理问题。

## 6. WebSocket（Market channel，本仓库 `market_stream.py` 已用）

连接：`wss://ws-subscriptions-clob.polymarket.com/ws/market`

订阅消息：
```json
{ "assets_ids": ["<token_id>", ...], "type": "market" }
```
可选 `custom_feature_enabled: true` 解锁额外事件。同连接可发 `operation: "subscribe"/"unsubscribe"` 追加或移除资产。

**心跳**：每 10 秒发文本帧 `PING`，服务端回 `PONG`。连接后必须立即订阅，否则服务端可能主动断开。

事件类型：
- `book`：全量快照（market/asset_id/timestamp/hash/bids/asks）
- `price_change`：增量更新数组，每条含 asset/price/size/side/hash/当前 best bid ask
- `last_trade_price`：最新成交（market/asset/price/size/fee/side/timestamp/tx hash）
- `tick_size_change`：market/asset/旧新 tick 值/timestamp
- （需 `custom_feature_enabled`）`best_bid_ask`：top-of-book + spread + timestamp
- （需 `custom_feature_enabled`）`new_market` / `market_resolved`

本仓库的实现（"保存初始 book 全量 bids/asks，逐档应用 price_change 维护可精确重放的本地订单簿"）与官方事件语义一致，是正确用法。文档未给出并发订阅上限，与 HANDOFF.md 记录的"8 城市 176 个 token 单连接实测无重连无丢包"一致，暂不需要连接池。

## 7. 限流

CLOB 通用：9,000 req / 10s。行情相关端点：`/book` 1,500/10s，`/books` 500/10s，`/price` 1,500/10s，`/prices` 500/10s，`/midpoint` 1,500/10s，`/prices-history` 1,000/10s。下单类端点有独立的 burst/sustained 双层限额（如 `POST /order` burst 5,000/10s，sustained 120,000/10min）。超限走 Cloudflare 排队/延迟，而非直接拒绝；429 时应指数退避。

本仓库当前只读采集频率（NWS/METAR 60s、Open-Meteo 3h、market-stream 常驻单连接）远低于这些限额，暂无需专门处理限流退避逻辑，但如果未来提高轮询频率或扩展站点数，`/prices` 500/10s 是需要关注的瓶颈项（多站点批量查询要控制批大小）。

## 8. 错误码

统一 JSON 格式 `{"error": "<message>"}`。关键几类：

- 429 `Too Many Requests`：限流，需退避
- 503 `Trading is currently disabled` / `cancel-only` / `post-only mode`：交易所暂停或限制模式（本仓库不下单，不受影响，但监控 `/book` 时若长期收到交易暂停类响应，可作为市场健康信号）
- 404 `No orderbook exists for the requested token id`：token 无订单簿（可能是已结算或从未有流动性），历史回放遇到此类应视为"无深度数据"而非当作 0 流动性
- 400 系列：payload/参数错误，多为调用方问题

## 9. 手续费（当前仓库用简化 bps 模型，与官方实际公式有出入，需要评估）

官方公式：
```
fee = shares × feeRate × p × (1 - p)
```
- Maker 费率恒为 0（不管类别）
- Taker 费率按市场类别不同：Weather 类别 taker feeRate = **0.05**（5%），有 25% maker rebate 返还给做市方（不影响 taker 侧成本）
- 费用在 p=0.5 时最高，向 0/1 两端对称衰减；例如 100 份 @ 0.05 类别、p=0.10 或 0.90 时，taker fee ≈ 100 × 0.05 × 0.10 × 0.90 = **$0.45**；p=0.50 时 ≈ 100 × 0.05 × 0.25 = **$1.25**

**与本仓库现状的差异**：`paper.py:15` 当前用固定 `fee_bps`（默认 0）线性叠加到滑点成本上，即 `estimated_cost = (fee_bps + slippage_bps) / 10000`，是一个与价格无关的固定比例模型。真实费用公式是 `p×(1-p)` 形状，在极端价位（如 NO 侧 p 接近 0.99 时对应的 YES p≈0.01）费用趋近于 0，而不是固定比例。这意味着：

- 对尾部桶（YES<1% 或 NO 接近 0.99）套利分析，如果之前用固定 fee_bps 估算过手续费成本，可能**高估**了尾部桶的手续费（真实费用在两端趋近 0）
- 对中间价位（p≈0.5，比如"邻桶价差"策略里两个概率接近的桶）手续费影响更大，之前若忽略手续费或用统一 bps，可能**低估**了这部分成本

这个差异应该纳入 T3/T8 的成本模型：滑点用真实深度算,手续费应该用 `0.05 × p × (1-p) × shares`（Weather 类别 taker 费率）而不是当前的固定 bps，否则中间价位策略的净收益会被系统性算错。

## 10. 结算机制（UMA Optimistic Oracle）

Polymarket 不自行判定结果，通过 UMA 乐观预言机 + `UmaCtfAdapter` 合约：

1. 事件结束后，任何人可提出结果并抵押 bond（约 $750），进入 **2 小时挑战窗口**
2. 无人质疑 → 2 小时后直接resolve，赢方份额兑 $1，输方归零，交易停止
3. 第一次被质疑 → 不会立刻进入投票，而是**重置并重新提议**（第二轮提议 + 新的 2 小时窗口）
4. 第二次被质疑才升级到 UMA DVM 代币持有人投票（约 4-6 天）

典型无争议情况下，事件结束到最终结算约 2 小时。这个时间窗口和 T6（已出局桶退出可行性）相关：一个桶物理上已不可能中（`physical_margin_f > 0`）之后，市场价格逼近 0/1 是交易者根据物理事实自行调整盘口的结果，跟官方链上结算（UMA 2 小时窗口）是两件独立的事——**物理出局到 UMA 最终结算之间可能有数小时到数天的窗口，这段时间盘口价格已经反映预期结果但尚未真正 resolve，仍可正常交易/退出**，不需要等链上结算完成才能平仓。这点对 T6 的"退出可行性"分析是有利的确认：只要有对手盘深度，物理出局后即可挂单卖出，无需等 UMA 走完流程。

## 11. 与本仓库现有实现的对照结论

| 官方文档要点 | 本仓库现状 | 是否需要改动 |
|---|---|---|
| `/book`、`/prices` 是即时盘口，`p` 历史序列不是 | `execution-cost-calibration` 已经在对比 `p` 代理与真实吃单价偏差，方向正确 | 继续按 T1/T3 任务量化 |
| Market WS `book`+`price_change` 语义 | `market_stream.py` 实现方式与文档一致 | 无需改动 |
| 手续费公式为 `feeRate × p × (1-p)`，非固定比例 | `paper.py` 用固定 `fee_bps` | **建议按 T3/T8 任务改为按公式计算**，否则中间价位策略成本被低估、尾部桶成本被高估 |
| Taker 费率 Weather 类别为 5%，Maker 为 0 | 未见区分 maker/taker 的费用逻辑 | 若后续要精细化收益计算，需要区分是主动吃单还是被动挂单成交 |
| 结算走 UMA 2 小时窗口（无争议情况） | README 提到"结算规则解析…改编自 polymarket-tmax-lab" | 与 T6 分析一致，无冲突 |
| WS 无官方并发订阅上限说明 | HANDOFF 记录 8 城市 176 token 单连接实测稳定 | 无需改动，继续监控 stream-status |

## 参考页面

- [API 总览](https://docs.polymarket.com/getting-started/api)
- [鉴权](https://docs.polymarket.com/api-reference/authentication)
- [下单与订单类型](https://docs.polymarket.com/trading/orders/create)
- [Order book 端点](https://docs.polymarket.com/api-reference/market-data/get-order-book)
- [CLOB 市场信息](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)
- [错误码](https://docs.polymarket.com/resources/error-codes)
- [限流](https://docs.polymarket.com/api-reference/rate-limits)
- [实时数据（Market WS）](https://docs.polymarket.com/market-data/realtime-data)
- [手续费](https://docs.polymarket.com/trading/fees)
- [结算机制（Concepts）](https://docs.polymarket.com/concepts/resolution)
