# Poly Weather

面向 Polymarket 天气预测市场的研究、回放与纸面决策系统。当前里程碑只做公开数据读取、原始数据留存、市场目录标准化、结算规则校验和不可执行的纸面记录；不读取钱包、不签名、不下单。

## 安全边界

- 下游信号必须通过站点、日期、单位、取整和终止规则核验，并使用显式 `signal_truth_policy`。
- `same_station_noaa` 允许 NOAA 同机场站点数据及时驱动纸面信号，不等待 Wunderground 页面更新；严格结算源核验仍单独报告。
- 原始 API 响应先落盘，再生成标准化记录，便于重放和审计。
- 当前没有交易执行模块，也没有私钥相关配置。

## 环境

```text
uv sync --extra dev
uv run pytest
uv run poly-weather validate-settlements
uv run poly-weather discover-markets --pages 1 --page-size 20
uv run poly-weather nws-latest KNYC
uv run poly-weather aviation-snapshot SETTLEMENT_KEY --metar-hours 6
uv run poly-weather forecast-buckets EVENT_SLUG SETTLEMENT_KEY YYYY-MM-DD
uv run poly-weather inspect-settlement EVENT_SLUG SETTLEMENT_KEY
uv run poly-weather backfill-calibration SETTLEMENT_KEY 2026-07-01 2026-07-31
uv run poly-weather evaluate-calibration SETTLEMENT_KEY
uv run poly-weather noaa-daily-high SETTLEMENT_KEY YYYY-MM-DD
uv run poly-weather backfill-prices EVENT_SLUG 2026-08-21T00:00:00Z 2026-08-22T00:00:00Z
uv run poly-weather paper-decision EVENT_SLUG MARKET_SLUG SETTLEMENT_KEY 0.50 2026-08-22T05:00:00Z
uv run poly-weather monitor EVENT_SLUG SETTLEMENT_KEY --cycles 1
uv run poly-weather monitor EVENT_SLUG SETTLEMENT_KEY --cycles 0 --interval 60
uv run poly-weather market-stream EVENT_SLUG
uv run poly-weather weather-stream SETTLEMENT_KEY
uv run poly-weather market-stream NYC_EVENT_SLUG LA_EVENT_SLUG
uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed
uv run poly-weather signal-engine --market NYC_EVENT_SLUG=new-york-daily-high-research-seed --market LA_EVENT_SLUG=los-angeles-daily-high-research-seed
uv run poly-weather stream-status
```

默认数据写入 `data/`：原始事件位于 `data/raw/`，标准化目录位于 `data/catalog.sqlite3`。

`forecast-buckets` 会输出并归档 31 个 GEFS 成员的分桶分布、相对市场 Yes 价格的差值和完整输入路径。只有与目标事件完全匹配且已核验的 settlement entry 才可能通过规则闸门；当前纽约与洛杉矶 2026-08-22 证据已核验，芝加哥与迈阿密模板仍保持未核验。

当研究库中存在足够多、日期早于目标日的同站点样本时，`forecast-buckets` 可应用历史平均偏差修正。实时 `signal-engine` 采用更严格的走步验证闸门：至少 30 条训练历史和 30 条样本外测试结果，原始 RMSE 必须不高于 4°F；只有 Brier、LogLoss 均改善且校准 RMSE 不劣于原始值 10% 以上时才采用偏差修正。元数据保留原始概率、选用概率、样本区间和验证指标，不会把未来日期或当天结果用于校准。

`inspect-settlement` 会从事件标题、规则正文、resolution URL 和所有二元分桶中生成带 SHA-256 的证据快照，再与人工维护的 registry 逐项比对。解析成功不等于核验成功。

`backfill-calibration` 使用 Open-Meteo Previous Runs 的固定提前期预报，并与 NOAA NCEI Daily Summaries 的同站点日最高温连接。样本进入 `data/research.duckdb`，同时导出为 Parquet，并标记为 `noaa_same_station_daily_final`。

`noaa-daily-high` 从 NOAA/NWS 拉取同一机场站点观测，严格按配置的 IANA 时区切分本地日，并支持 `--as-of` 截止时间。该数据是及时信号源，不等待 Wunderground 更新；官方结算网站仍保留为规则与差异审计来源。

`aviation-snapshot` 从 NOAA Aviation Weather Center 同时归档机场 METAR 和 TAF。METAR 用作同站实况与数据新鲜度交叉检查；TAF 提取降水、雷暴、低云和是否含温度指导等风险特征。TAF 不直接替代 GEFS 日最高温分布。

`backfill-prices` 使用 Polymarket 官方 CLOB 批量历史接口采集每个二元分档的 Yes token。`paper-decision` 只从 DuckDB 选择决策时刻之前、且未过期的价格，扣除配置费用与滑点后执行风险闸门。它只记录纸面决策，不包含钱包、签名或下单路径。

`monitor` 是持续只读监测器。每轮读取 NOAA/NWS 最新站点温度、Aviation Weather METAR 和公开 CLOB 双边报价；TAF 默认每 10 分钟刷新。`--cycles 1` 用于单轮检查，`--cycles 0` 持续运行到 Ctrl+C。轮询间隔不得低于 60 秒。每轮快照同时进入 `data/research.duckdb` 和 `data/raw/monitor_snapshot/`。

`market-stream` 是事件驱动的 Polymarket 公共 Market WebSocket 守护进程。它一次订阅事件下全部 outcome token，维护本地盘口顶层状态，处理 `book`、`price_change`、`best_bid_ask` 和 `last_trade_price`，使用应用层 PING/PONG、静默超时和指数退避重连。接收与写盘解耦，通过有界队列批量写入 `data/market_stream.duckdb` 和 `data/raw/polymarket_clob_websocket/`。不连接需要认证的 user channel。

`weather-stream` 是独立的异步天气守护进程。NWS 最新观测和 AWC METAR 默认每 60 秒读取，TAF 每 10 分钟，GEFS 每 3 小时；共用持久 HTTP 连接池并对各数据源独立退避。事件批量写入 `data/weather_stream.duckdb` 和 `data/raw/weather_daemon/`。两个守护进程使用独立 DuckDB，避免多进程写锁竞争。

两个流命令都接受一个或多个位置参数。当前后台实例以单个市场 WebSocket 同时订阅纽约与洛杉矶的 44 个 token，并在同一天气进程内分别调度 KLGA 与 KLAX。状态文件按事件和站点分别展示计数与错误。守护进程不调用任何大模型，因此持续监控本身不消耗模型 token；只有另行启用基于模型的解释或摘要才会产生 token 消耗。

`signal-engine` 增量跟随两个守护进程的追加式 JSONL，不读取正在写入的流式 DuckDB。它把当日已观测最高温作为硬下界，使用最新 GEFS 成员生成温度分档概率，再与 Yes/No 两侧最佳卖价比较，并扣除可配置成本缓冲。NWS/METAR 新鲜度、两源温差、WebSocket/天气进程健康、结算核验和历史校准可信度都是确定性闸门。纽约与洛杉矶 2026-08-22 市场的精确结算证据已经版本化并核验；系统仍严格只读，所有 `action` 强制为 `skip`，不会访问钱包、签名或下单。状态位于 `data/runtime/signal_engine_status.json` 和 `data/runtime/signal_state.json`，快照进入 `data/signal_stream.duckdb` 与 `data/raw/signal_snapshot/`。

`market-stream` 和 `weather-stream` 的 `--runtime 0` 表示持续运行；设置正数可做有限时长烟测。市场原始 JSONL 与 DuckDB 使用分离队列，数据库写入在线程中执行，DuckDB 检查点延迟不会阻塞 WebSocket 收包和心跳。`stream-status` 读取三个运行状态文件，展示 PID、连接、错误、内存队列、数据库队列和落盘计数；信号闸门会拒绝超过阈值的守护进程心跳。后台标准输出和错误输出可重定向到 `data/logs/`。

默认实时信号健康闸门为：NWS 数据年龄不超过 75 分钟、METAR 不超过 70 分钟、两源温差不超过 2°F、市场 WebSocket 心跳不超过 1 分钟、天气守护进程心跳不超过 3 分钟。主 NOAA、GEFS、守护进程或 CLOB 失效会把信号标记为 `stale`；METAR 交叉检查或盘口不完整标记为 `warning`。监测器没有钱包、签名、下单或资金代码路径。

`evaluate-calibration` 按目标日期排序，只用较早日期拟合偏差，在随后的日期块上计算 MAE、RMSE、Brier 和 LogLoss。原始模型和校准模型的结果分开报告。

### 已验证的真实数据样例

KLGA 2026-07-01 至 2026-07-31 已成功生成 31 条提前一天样本。以最初 14 天训练、每 7 天向前滚动，得到 3 个测试折、17 个严格样本；该小样本结果只能验证管线，不能证明存在可交易优势。

## 开源复用

结算规则解析、来源闸门和滚动验证的设计参考并改编自 MIT 许可的 [polymarket-tmax-lab](https://github.com/YoungseokOh/polymarket-tmax-lab)。完整归属见 `THIRD_PARTY_NOTICES.md`。

公开接口依据：

- [Polymarket Market Data](https://docs.polymarket.com/market-data/overview)
- [Polymarket List Markets](https://docs.polymarket.com/api-reference/markets/list-markets)
- [National Weather Service API](https://www.weather.gov/documentation/services-web-api)
- [NOAA Aviation Weather Center Data API](https://aviationweather.gov/data/api/)
- [Polymarket Batch Prices History](https://docs.polymarket.com/api-reference/markets/get-batch-prices-history)
- [Polymarket Market WebSocket](https://docs.polymarket.com/api-reference/wss/market)

## 当前开发顺序

1. 只读市场发现和结算规则注册表。
2. NWS 观测与 GEFS 集合预报采集。
3. 温度分桶概率、校准和历史价格重放（已完成基础链路）。
4. NOAA 同站信号、METAR/TAF 参考和纸面风险约束（已完成基础链路）。
5. 联合历史重放、基差统计与实时行情监控。
6. 真实执行保持为独立适配器，需另行审核与授权。

跨电脑接手、当前运行状态、已知缺口和数据重建步骤见 [`HANDOFF.md`](HANDOFF.md)。
