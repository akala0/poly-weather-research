# Poly Weather

> **AI 助手接手请先读 [AGENTS.md](AGENTS.md)**（长期规则、架构、数据源边界、当前进度）。
> 本文件讲功能与命令，[HANDOFF.md](HANDOFF.md) 讲环境安装与运行状态。

当前研究判断与数据截止统一见 [CURRENT_CONCLUSIONS.md](CURRENT_CONCLUSIONS.md)；历史报告不得越过该入口单独作为当前结论。

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
uv run poly-weather backfill-calibration SETTLEMENT_KEY --start-date 2026-06-01 --end-date 2026-08-20 --multi-model
uv run poly-weather evaluate-bucket-skill SETTLEMENT_KEY --start-date 2026-06-01 --end-date 2026-08-20
uv run poly-weather noaa-daily-high SETTLEMENT_KEY YYYY-MM-DD
uv run poly-weather backfill-prices EVENT_SLUG 2026-08-21T00:00:00Z 2026-08-22T00:00:00Z
uv run poly-weather paper-decision EVENT_SLUG MARKET_SLUG SETTLEMENT_KEY 0.50 2026-08-22T05:00:00Z
uv run poly-weather monitor EVENT_SLUG SETTLEMENT_KEY --cycles 1
uv run poly-weather monitor EVENT_SLUG SETTLEMENT_KEY --cycles 0 --interval 60
uv run poly-weather market-stream EVENT_SLUG
uv run poly-weather market-supervisor --runtime 0
uv run poly-weather weather-stream SETTLEMENT_KEY
uv run poly-weather market-stream NYC_EVENT_SLUG LA_EVENT_SLUG
uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed
uv run poly-weather signal-engine --supervised
uv run poly-weather stream-status
uv run poly-weather liquidity-report --source auto
uv run poly-weather execution-cost-calibration
uv run poly-weather multi-city-certainty-report --refresh
uv run poly-weather sync-polymarket-status
uv run poly-weather audit-polymarket-maintenance
uv run poly-weather collect-public-trades
uv run poly-weather analyze-public-trades
uv run poly-weather analyze-no-entry-accessibility
```

默认实际结算 registry 为 `configs/settlements.json`。原来的
`configs/settlements.example.json` 继续作为兼容镜像保留；旧命令显式传入该路径仍可运行。
换机部署应复制实际配置文件，不要把 `.example` 文件误当作唯一运行配置。

默认数据写入 `data/`：原始事件位于 `data/raw/`，标准化目录位于 `data/catalog.sqlite3`。

`forecast-buckets` 使用 Open-Meteo deterministic 单值日最高温和无前视历史校准的残差标准差，通过 Normal CDF 计算每个温度分桶的概率。历史目标日只允许使用 `lead_days>=1`；`lead_days=0` 已确认包含目标日内更新。只有与目标事件完全匹配且已核验的 settlement entry 才可能通过规则闸门；美国八城与重庆、成都模板均有 2026-08-24 证据快照，单位和桶宽也是严格比较项。

当研究库中存在足够多、日期早于目标日的同站点样本时，`forecast-buckets` 可应用历史平均偏差修正。实时 `signal-engine` 采用更严格的走步验证闸门：至少 30 条训练历史和 30 条样本外测试结果，原始 RMSE 必须不高于 4°F；只有 Brier、LogLoss 均改善且校准 RMSE 不劣于原始值 10% 以上时才采用偏差修正。元数据保留原始概率、选用概率、样本区间和验证指标，不会把未来日期或当天结果用于校准。

`inspect-settlement` 会从事件标题、规则正文、resolution URL 和所有二元分桶中生成带 SHA-256 的证据快照，再与人工维护的 registry 逐项比对。解析成功不等于核验成功。

`backfill-calibration` 使用 Open-Meteo Previous Runs 的固定提前期预报，并与 NOAA NCEI Daily Summaries 的同站点日最高温连接。允许的提前期为 1–7 天，day 0 禁止进入校准和回测。样本进入 `data/research.duckdb`，同时导出为 Parquet，并标记为 `noaa_same_station_daily_final`。

`backfill-calibration --multi-model` 会在一次 Previous Runs 请求中获取 GFS Seamless、ICON Seamless 和 GEM Seamless，把各模型日最高温保存在 DuckDB `JSON` 列，并以等权平均作为初始主预报。`evaluate-bucket-skill` 逐日只用此前样本按模型 MAE 的倒数学习权重，再使用与线上相同的 Normal CDF 公式评估真实 2°F 桶概率和首选桶命中率。

`noaa-daily-high` 从 NOAA/NWS 拉取同一机场站点观测，严格按配置的 IANA 时区切分本地日，并支持 `--as-of` 截止时间。该数据是及时信号源，不等待 Wunderground 更新；官方结算网站仍保留为规则与差异审计来源。

`aviation-snapshot` 从 NOAA Aviation Weather Center 同时归档机场 METAR 和 TAF。METAR 用作同站实况与数据新鲜度交叉检查；TAF 提取降水、雷暴、低云和是否含温度指导等风险特征。TAF 不直接替代 deterministic 日最高温预测。

`backfill-prices` 使用 Polymarket 官方 CLOB 批量历史接口采集每个二元分档的 Yes token。`paper-decision` 只从 DuckDB 选择决策时刻之前、且未过期的价格，扣除配置费用与滑点后执行风险闸门。它只记录纸面决策，不包含钱包、签名或下单路径。

`monitor` 是持续只读监测器。每轮读取 NOAA/NWS 最新站点温度、Aviation Weather METAR 和公开 CLOB 双边报价；TAF 默认每 10 分钟刷新。`--cycles 1` 用于单轮检查，`--cycles 0` 持续运行到 Ctrl+C。轮询间隔不得低于 60 秒。每轮快照同时进入 `data/research.duckdb` 和 `data/raw/monitor_snapshot/`。

`market-stream` 保存完整初始 `book` 并逐档应用 `price_change`，持续维护准确的内存订单簿。每个事件按结算时区在当地 09:00-20:00 全量落盘；窗口外仍保持连接、心跳和内存更新，但仅保存每小时完整深度及最终 `market_resolved`。原始写盘与数据库写入使用独立队列，不连接需要认证的 user channel。

`market-stream` 还每 5 分钟读取官方状态的实际机器源（summary + Atom 历史），按组件区分 CLOB WebSocket 与其他服务。维护/故障时继续原频率采集，但新归档带 `upstream_status`/incident 标记，运行状态显示 `upstream_maintenance`；历史未带字段的记录由持久化质量窗口在分析时默认排除。状态变化保存在 `data/runtime/polymarket_status_history.jsonl`。

`market-supervisor` 通过 Gamma public-search 发现当天与次日事件，复用 `inspect-settlement` 的严格证据核验。新 token 先热订阅并等待全部初始 book，Gamma 已关闭的旧事件才会退订；失败城市记录差异并 fail-closed，不阻塞其他城市。状态在 `market_supervisor_status.json`，配置变更通知在 `signal_config_update.json`；`signal-engine --supervised` 会校验证据 SHA 后原子热加载每个新 generation。

`weather-stream` 是独立的异步天气守护进程。实时链路按实测更新节奏轮询：WRH/NWS 120 秒、METAR 900 秒、TAF 3600 秒、中国站 1800 秒、Open-Meteo 10800 秒。每个站点从 `multi_model_blend` 历史样本学习逆 MAE 权重；历史不足时明确回退等权。事件 `multi_model_deterministic_forecast` 同时保存三套模型序列、权重、blended 序列和原始响应。Open-Meteo 返回网格距请求机场超过 3 km 时立即拒绝该响应。事件批量写入 `data/weather_stream.duckdb` 和 `data/raw/weather_daemon/`。

2026-08-24 真实烟测证明单个市场 WebSocket 可同时覆盖美国八城及重庆、成都的当天和次日共 20 个事件、440 个 token：440/440 收到完整 book，0 重连、0 解析错误。Polymarket 文档未给出固定订阅上限，因此当前不引入连接池；运行时指标若持续越界，再按资产拆分。守护进程不调用任何大模型，因此持续监控本身不消耗模型 token。

`signal-engine` 增量跟随两个守护进程的追加式 JSONL，不读取正在写入的流式 DuckDB。它把当日已观测最高温作为硬下界，以最新多模型 blended 日最高温作为均值、无前视多模型校准残差标准差作为离散度生成温度分档概率。除了原有 best-ask 理论边际，每个信号还按完整深度估算 $50/$200/$1000 的 Yes/No 吃单均价、滑点 bps、可成交比例和扣滑点净边际；盘口不完整时不以 best quote 兜底。流事件权重必须与校准权重逐项一致，否则 fail-closed 为 stale。候选净边际超过 15% 会加入 `implausible edge suggests model error`，强制阻止 paper alert。`execution_enabled` 始终为 `false`，不会访问钱包、签名或下单。

`market-stream` 和 `weather-stream` 的 `--runtime 0` 表示持续运行；设置正数可做有限时长烟测。`liquidity-report` 按候选站点时段汇总 spread、$200/$1000 滑点和深度不足比例；热文件运行中读取小型深度检查点侧流，封存数据可用 DuckDB。`execution-cost-calibration` 严格按历史入场/退出截止时刻重放深度，比较 prices-history 的 `p` 代理与真实吃 ask/吃 bid 的偏差。当前 16 笔旧代理交易与 8/22 后深度归档无日期重叠，所以三档仓位均为 N=0，不用假价格补齐。Windows 实例对原始市场 JSONL 目录启用了 NTFS 透明压缩，代码仍读取普通 JSONL；跨平台部署需自行配置等效的压缩和保留策略。`stream-status` 展示 PID、连接、错误、内存队列、数据库队列和落盘计数。

`collect-public-trades` 从免鉴权的 Data API 保存 canonical taker 成交流水；`analyze-public-trades` 对每个 prices-history 点只寻找其时间戳之前的成交，实测陈旧度、NO token 自身成交和 VWAP。成交价绝不是历史 ask/bid，也不能恢复深度；依赖真实入场 ask 或 $200/$1000 滑点的结论继续保持 N/A。

`analyze-no-entry-accessibility` 只用已归档的真实 NO asks/bids 和公开只读数据，审计 NO≥0.99 尾桶为什么无法买入：空 asks、近端点报价、指定 $20/$50/$100/$150/$200 深度不足、`prices-history.p` 与可成交 ask 的差异，以及 KLAX/KLGA 本地时段。维护/恢复质量窗口默认排除；历史 p 和公开成交严格截止到各自 NO 订单簿时刻。成交价不是可成交 ask，输出只用于研究，不产生订单或鉴权路径。

默认实时信号健康闸门为：NWS 数据年龄不超过 75 分钟、METAR 不超过 70 分钟、两源温差不超过 2°F、市场 WebSocket 心跳不超过 1 分钟、天气守护进程心跳不超过 3 分钟、Open-Meteo 网格距离不超过 3 km、候选净边际不超过 15%。主 NOAA、deterministic 预报、守护进程或 CLOB 失效会把信号标记为 `stale`；METAR 交叉检查、盘口不完整或异常边际标记为 `warning`。监测器没有钱包、签名、下单或资金代码路径。

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
- [Polymarket Rate Limits](https://docs.polymarket.com/api-reference/rate-limits)
- [Polymarket Prices History](https://docs.polymarket.com/api-reference/markets/get-prices-history)
- [Polymarket Public Trades](https://docs.polymarket.com/api-reference/core/get-trades-for-a-user-or-markets)
- [Polymarket Blockchain Data](https://docs.polymarket.com/resources/blockchain-data)

## 当前开发顺序

1. 只读市场发现和结算规则注册表。
2. NWS 观测与 Open-Meteo deterministic 预报采集。
3. 温度分桶概率、校准和历史价格重放（已完成基础链路）。
4. NOAA 同站信号、METAR/TAF 参考和纸面风险约束（已完成基础链路）。
5. 联合历史重放、基差统计与实时行情监控。
6. 真实执行保持为独立适配器，需另行审核与授权。

跨电脑接手、当前运行状态、已知缺口和数据重建步骤见 [`HANDOFF.md`](HANDOFF.md)。
