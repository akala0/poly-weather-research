# Poly Weather 开发交接说明

更新日期：2026-08-28

> **接手的 AI 助手请先读 [AGENTS.md](AGENTS.md)。** 那里有长期铁律、架构判断、数据源能力边界、
> 已被推翻的旧结论和当前进度——都是从大量试错中得来的，重犯代价很高。本文件只讲环境安装和运行状态。

结算 registry 的 CLI 默认路径已改为 `configs/settlements.json`；
`configs/settlements.example.json` 暂时保留相同内容以兼容旧脚本和已有调用。
常驻进程在启动时已载入 registry，此次文件拆分本身不要求重启。

## 项目定位与安全边界

这是一个仍在开发中的 Polymarket 天气市场研究系统，不是可投入资金的交易机器人。当前只访问公开的 Polymarket、NOAA/NWS、NOAA Aviation Weather Center、NOAA NCEI 和 Open-Meteo 接口。仓库中没有钱包适配器、私钥读取、签名、下单、撤单或转账功能；实时信号的所有 `action` 都被硬编码为 `skip`。

不要在未经独立安全审核、回放验证和用户明确授权前加入真实执行路径。

## 当前已完成

- Polymarket Gamma/CLOB 市场发现、历史价格采集和公共 Market WebSocket。
- WebSocket 单连接已真实覆盖 10 城当天和次日共 440 个 outcome token；暂不需要连接池。
- NOAA/NWS 最新机场观测、AWC METAR/TAF 与 Open-Meteo deterministic 预报采集。
- KLGA、KLAX、KORD、KMIA、KATL、KDAL、KHOU、KSEA 八站点异步天气守护进程。
- 原始 JSONL、SQLite/DuckDB 研究存储和带时间戳的审计记录。
- 温度分桶解析、连续性检查及基于 Normal CDF 半度边界的概率积分。
- 结算证据解析、SHA-256 快照与 fail-closed 核验。
- 同站点 NOAA NCEI 日最高温与 Open-Meteo Previous Runs 历史预报连接。
- `lead_days=0` 前视阻断；校准和回测只允许 Previous Runs lead 1–7。
- Open-Meteo 请求/返回网格 3 km fail-closed 校验。
- GFS/ICON/GEM 一次请求式历史采集、DuckDB JSON 逐模型值和逆 MAE 权重学习。
- 真实 2°F 桶概率、首选桶命中率及 `evaluate-bucket-skill` CLI。
- 实时天气流已切换到逐站点学习权重的 GFS/ICON/GEM blend，事件保留三模型和 blended 完整序列。
- signal engine 只消费 `multi_model_deterministic_forecast`，并校验流权重与校准权重完全一致。
- 实时原始概率、验证后选用概率、盘口净边际和可信度闸门。
- 完整 `book` + 所有逐档增量无损写 JSONL；DuckDB 保存初始和每资产 30 秒深度检查点，避免拖慢收包。
- $50/$200/$1000 深度成交均价、滑点、部分成交比例和扣滑点边际已进入只读信号快照。
- `liquidity-report` 与 `execution-cost-calibration` CLI 已加入；旧 p 代理交易与现有深度档案暂无日期重叠。
- 官方状态 summary + Atom 每 5 分钟组件级订阅；维护期间不停采集，归档自动标记且分析默认排除。
- 2026-08-26 CLOB 官方维护 04:00–07:30 UTC，另有本地遥测恢复窗 07:30–07:50:48 UTC；全量流 19,196 条、深度检查点 9,186 条默认排除。
- 公共 `data-api/trades` 已按 canonical taker 流水接入；22 个已结算事件共 63,534 笔成交，严格截止时刻陈旧度分析已落盘。
- `analyze-no-entry-accessibility` 已用真实 NO asks/bids 审计 NO≥0.99 尾桶的空盘、近端点、$20–$200 深度和 KLAX/KLGA 时段；默认排除维护/恢复质量窗口，成交价不替代 ask。
- `analyze-price-band-accessibility` 已按真实 NO best ask 分箱，比较 $20–$200 的 NO 买入与等价 YES 卖出深度、滑点、实际手续费、同簿成本门槛和本地时段；KLAX/KLGA 的中间候选分别为 0.70–0.85/0.50–0.70，结果仅用于纸面可达性筛选。
- `analyze-price-paths` 已对 $200 深度完整入场后的同一 NO token 严格未来 best bid 追踪 +5/+10/+13/+20¢，区分真实 bid 触达、未来 $200 深度触达、物理出局跳空、最低 bid 和 −5% 止损带，并按物理余量×典型高点阶段分层；当前 22 个已结算目录事件与深度重叠为 N=0，结果是未结算前向路径快照。
- 只读影子策略已加入：`shadow_orders.py` 提供 post-only 状态机、touch/queue-aware/trade-through 队列模型、有限 $200 预算、补仓闸门、分批退出、紧急 taker 费和 append-only 永久账本；`analyze-shadow-spread` 与 `shadow-spread-engine --supervised` 只读重放/写账本，绝不执行订单。
- 影子回放按 `event_id + market_id + token_id + market_day` 隔离订单、库存、成本基准、PnL 和 round trip；station/market-day 只保留为相关统计与 $200 风险聚类，不能跨 token 平仓。比较 4×$50、20+30+50+100、2×$100 与单笔对照；当前回放有严格 season/version 元数据和天气 source/receipt 双截止，缺失时仍对候选 fail-closed，结果不能宣称 maker 可执行收益。
- 最新只读回放已完成 v2 token-scoped 重跑：550 个 token portfolios、50 个独立 station/market-day、81,349 个深度快照和 1,187 个价带候选；固定 price-band queue-aware 26 单/3 fills，weather-market-lag 207 单/7 fills。所有订单、库存、成本、PnL 和 round trip 按 `event_id + market_id + token_id + market_day` 隔离，station-day 只作相关统计和 $200 累计预算聚类。weather-market-lag 仅 KDAL 有 1 个合法同-token round trip、realized PnL `+$15.0684931507`；KMIA 库存未验证同-token 出场，整体净 PnL 为 N/A，不能宣称正期望。旧诊断 `$13.5571351545` 已确认全部来自跨 token 串账，逐笔对账见 `data/shadow_token_scope_reconciliation_report.md`。
- `shadow-spread-engine --supervised` 默认持续跟随，使用原子 cursor、重启代数和新的 token-scoped v2 永久账本；默认路径为 `shadow_orders_v2_token_scoped.jsonl`、`shadow_spread_status_v2_token_scoped.json` 与 `shadow_spread_cursor_v2_token_scoped.json`。旧 v1 账本只读隔离；如显式指向它，运行时 HALTED 而不改写。状态暴露 heartbeat、cursor、每 token 库存、station-day 预算、活动单、成交、上游维护和执行依赖扫描，`execution_enabled` 始终为 `false`。
- 2026-08-27 19:00 +08:00 已完成一次有限 `--once` 烟测：v2 schema、26 个账本订单、3 个 fills、无 HALTED/不变量差异；与主回放的订单/成交/PnL 状态一致，运行期间仅因归档继续写入而多出 102 个快照。
- 2026-08-28 已上线默认 v2 长期只读 follower：启动前市场为 `connected`、天气与 signal 为 `running`、官方质量窗没有活动的 CLOB 排除窗，且 v2 ledger 的 token-scope 不变量和执行依赖扫描均通过。首次启动为 `2026-08-28T11:05:03+08:00`，wrapper PID 为 `41128`（PID 仅作当时证据，不能复用），日志为 `data/runtime/shadow_spread_engine_v2_continuous.{stdout,stderr}.log`。首次启动以 25 个已有归档文件的尾部为 cursor 边界，不把旧归档误记成前向证据。
- 首次 follower 已观察 30.08 分钟：heartbeat/cursor 持续前进，最后观测 cycle 为 357，最近一轮消费 6 条盘口、176 条 WS 成交带和 40 条 signal 行；无 HALTED、无 discrepancy、无活动影子单。随后在不触碰市场/天气/signal 守护的前提下重启该影子进程：`2026-08-28T11:35:32+08:00` 的新 wrapper PID 为 `31148`，日志为 `data/runtime/shadow_spread_engine_v2_restart.{stdout,stderr}.log`。恢复后 cursor `restart_count=1`、heartbeat 正常、26 条 ledger orders 和 3 个 fills 均未重复，`halted=false`。`stream-status` 只显示 v2 状态，并将 `shadow_spread_status_v1_legacy_read_only.json` 标记为 superseded 证据路径；旧 `shadow_spread_status.json` 未删除。
- 当前 Windows 运行命令（不要加执行参数，也不要接 user/order API）：

  ```powershell
  .\.venv\Scripts\poly-weather.exe shadow-spread-engine --supervised --runtime 0 --data-dir D:\poly\data
  ```

  默认初次 continuous 启动只从已有归档尾部开始；只有审计旧数据时才显式加 `--replay-existing`，而不是把历史重放伪装成前向样本。
- 2026-08-28 14:59 +08:00 完成固定 vintage 的互补配对离线重放：103,991 个可用配对盘口快照、43,543 个
  token-native 成交事件（数据截止 14:01:40，分析上限 14:01:46）。默认 queue-aware 配置提交 293 个
  pair、3 个单腿 fill、已配平 N=0；KLAX 为 28/1/0、KLGA 为 32/0/0（提交/单腿/配平）。
  trade-through 为 293/2/0，touch 为 293/8/0；58 个 station-day 聚类，KLAX 5、KLGA 6，站点层
  n<30 不可靠。未配平仓位只用同 token 真实 bid 影子 unwind，queue-aware 汇总为 `-$0.2844544674`；
  计划成本低于 1 没有计入 locked edge。固定 2×3×4 敏感性网格共 24 个预先声明场景，仅作诊断，
  不选择最优参数，详见 `data/complement_pair_strategy_report.md`。
- 2026-08-28 12:02 +08:00 的 bias significance 审计为只读：321 个 lead_days≥1 样本、4 个组，
  `|mean bias|/SE>2` 只会额外禁用 KLGA multi_model_blend；未改校准或 gate，详见
  `data/bias_significance_audit.md`。
- 链上 SQL 路径已完成只读评估，当前不接入；宏观类别/地址/持仓研究出现明确需求时再启用。
- `signal_snapshot` 已启用 NTFS 透明压缩并纳入 2 日 gzip/30 日删除；T7 完整订单簿证据由不参与过期的 `no_forward_validation` 独立保留。
- 当前结论唯一入口为仓库根目录 `CURRENT_CONCLUSIONS.md`；IEM 小时版 `multi_city_certainty_report.md` 已明确废弃。
- 市场、天气、信号心跳监测与过期阻断。
- 候选净边际超过 15% 时强制告警并阻止 paper alert。
- 237 项单元测试全部通过，Ruff 静态检查全绿；最近一次验收时间为 2026-08-28。

## 当前校准结论

P0 验证确认 Previous Runs `lead_days=0` 含目标日内模型更新，不能用于校准或回测。实时和 CLI 现统一使用 deterministic 数据；历史回归及校准默认使用 lead 1。现有 day 0 数据仍保留用于审计，但所有生产 CLI 和实时校准入口都会拒绝它。

2026-08-22 洛杉矶回归使用 lead 1 单值 82.2°F、81 条此前样本的 `+0.764°F` 偏差和 2.647°F 残差标准差，模型首选桶由旧 ensemble 路径的 86–87°F 修正为 82–83°F。该结果验证管线修复，不证明存在稳定盈利能力。

多模型 walk-forward 实验结果：KLAX 在多模型共同可用的 78 天上有 48 个测试日，真实桶平均概率由同日期 GFS 的 19.80% 提升至 24.94%，首选桶命中率由 25.0% 提升至 43.75%；全样本诊断权重约为 GFS 46.2%、ICON 29.5%、GEM 24.3%。KLGA 的 51 个测试日从 GFS 的 18.84%/27.45% 提升至多模型的 24.82%/35.29%，全样本权重约为 GFS 32.1%、ICON 35.2%、GEM 32.7%。权重在正式评估中按测试日逐步学习，全样本权重只作诊断展示。

## 尚未完成

1. 联合历史回放：需要使用 Single Runs 固定初始化时间，把当时已发布的 deterministic 跑次与 CLOB 盘口严格按时间对齐；Previous Runs lead 1 仍不是完整的单一 vintage。
2. 成交可实现性：已加入 touch、queue-aware、trade-through、动态补仓和分批退出的只读模型；真实订单 ID、确切排队位置、撤单归因和可执行收益仍未知，现有结论不能视作实盘收益。
3. 多日、多城市泛化：十城行情采集已启动，但尚未积累足够的新阈值已结算样本。
4. Wunderground 最终结算差异审计：实时信号使用同机场 NOAA 数据，但最终仍应记录官方页面值并比较差异。
5. 任意非触发 signal 的原始 17 位浮点在 30 日后不再逐行保留；若未来需要永久逐位复现，应新增冷归档或确定性抽样。
6. 告警渠道、服务管理、开机自启与进程自动拉起。
7. 真实资金执行明确不在当前范围内。

## 已知缺口：天气 HTTP 连接池仍靠 containment

天气进程的 httpx/httpcore 池计数泄漏**尚未根治**。长时实测确认：
httpcore 记录的连接数会在连接错误后高于进程真实 TCP 连接数；此前 5.198 小时内差值单调升至 21，
而等待队列始终为 0。控制流检查同时推翻了“外层 `asyncio.wait_for` 取消请求”这个最初假设。

当前措施是 containment，不得写成“已修复”：请求并发受限；每 60 秒记录池/TCP 双口径；
差值达到 10 会标记 `degraded`，连续 3 个采样达到阈值时，在所有请求槽排空后重建 HTTP client。
`weather_daemon_status.json` 的 `http_pool_health.rebuild_count`、`last_rebuild_at` 和
`last_rebuild_reason` 必须持续可观测。2026-08-26 已实际触发过 1 次自愈：差值 18 连续 3 个样本。

环境使用 httpx 0.28.1 / httpcore 1.0.9。上游已知存在相同故障家族：连接错误可毒化池，
以及代理 CONNECT/TLS 失败留下不可复用的 zombie connection。相关 httpcore 修复截至本说明仍在审查中：

- https://github.com/encode/httpcore/issues/550
- https://github.com/encode/httpcore/pull/1071
- https://github.com/encode/httpcore/pull/1084

这与本项目使用 `AsyncHTTPProxy` 且泄漏紧随连接错误的证据高度一致，但尚不能证明是完全相同的上游路径。
不要关闭诊断或自愈，也不要仅靠扩大 `max_connections`。待上游发布修复后，应先在隔离环境升级并重跑超过 5 小时的对照验证。

## 季节化 warming_window_no 状态

`warming_window_no` 的阈值由 `configs/warming_window_no_thresholds.json` 提供，结构是
`station_id -> seasons[]`。当前只启用了每站第 1 个实例 `heat_2026`；窗口首尾日均包含。
窗口外、窗口重叠、站点缺配置或时间档无验证规则时一律 fail-closed，绝不能借用邻季阈值。
ZUCK/ZUUU 因每天约 24 条观测且 METAR T 组覆盖为 0，当前季节仍禁用。

前向样本必须记录 `warming_season_id` 与 `warming_threshold_version`，按季节独立计算 Wilson 区间；
30 个已结算样本的门槛也是每季独立。当前历史前向样本全部来自 8 月，只能验证 `heat_2026`。
将来若扩展全年，约需 30 x 4 = 120 个季节样本。新增下一季的固定流程是：提出气候机制候选窗口、
逐月同质性检验、推导“余量 x 距高点时间”联合阈值、追加季节配置、独立前向验证；
信号引擎不应为新季节改代码。非热季诊断已写入 `data/warming_window_threshold_report.md`，但尚未启用。

## 新电脑安装

要求：Python 3.12 或更高版本、Git、网络可访问上述公开接口。推荐安装 `uv`。

### macOS（推荐）

先安装 Apple Command Line Tools、Homebrew、GitHub CLI 和 `uv`：

```bash
xcode-select --install
brew install git gh uv
gh auth login
```

选择 `GitHub.com`、`HTTPS` 和浏览器登录，然后克隆私有仓库：

```bash
git clone https://github.com/akala0/poly-weather-research.git
cd poly-weather-research
uv sync --extra dev
uv run ruff check src tests
uv run pytest -q
uv run poly-weather validate-settlements
```

项目没有必须依赖 Windows 的运行时代码。`pathlib`、IANA 时区和依赖锁文件可在 macOS 使用；不要复制 Windows 的 `.venv`，必须在 Mac 上重新执行 `uv sync`。Apple Silicon 与 Intel Mac 均应使用各自平台重新解析的 Python 二进制依赖。

如果 Homebrew 尚未安装，请从 [brew.sh](https://brew.sh/) 使用其官方安装命令，不要从第三方脚本安装。

### Windows

```powershell
git clone <GitHub 仓库地址>
cd poly-weather-research
uv sync --extra dev
uv run ruff check src tests
uv run pytest -q
uv run poly-weather validate-settlements
```

如果不使用 `uv`：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

## 数据不会随 Git 仓库迁移

`data/` 被有意排除，因为其中包含持续增长的实时 JSONL、DuckDB、状态和日志。新电脑有两种选择：

- 从这台电脑安全复制整个 `D:\poly\data` 目录，以保留监测历史；复制前先停止三个守护进程，避免得到不一致的 DuckDB/WAL。
- 不复制数据，在新电脑重建校准样本。没有研究库时，实时校准闸门会保持 `blocked`，这是预期的 fail-closed 行为。

DuckDB、SQLite 和 JSONL 文件可以从 Windows 复制到 macOS，但不要复制 `.venv`、PID 或旧日志。建议在停止守护进程后打包 `D:\poly\data`，在 Mac 仓库根目录解压为 `data/`。历史 payload 中可能保留旧的 Windows 绝对归档路径；这些字段只用于审计展示，不应作为 Mac 上的新写入路径。

当前 Windows 主机已对 `data/raw/polymarket_clob_websocket` 启用 NTFS 透明压缩。2026-08-24 实测 863 MB 逻辑 JSON 占用约 350 MB 物理空间（2.5:1）；读取、测试和复制时仍表现为普通 JSONL。macOS 不继承 NTFS 压缩属性，因此迁移后需要另行配置 APFS 压缩/归档和磁盘保留策略。

重建当前实时校准样本：

```powershell
uv run poly-weather backfill-calibration new-york-daily-high-research-seed 2026-06-01 2026-08-21 --lead-days 1 --multi-model
uv run poly-weather backfill-calibration los-angeles-daily-high-research-seed 2026-06-01 2026-08-21 --lead-days 1 --multi-model
uv run poly-weather evaluate-bucket-skill new-york-daily-high-research-seed --start-date 2026-06-01 --end-date 2026-08-21 --lead-days 1
uv run poly-weather evaluate-bucket-skill los-angeles-daily-high-research-seed --start-date 2026-06-01 --end-date 2026-08-21 --lead-days 1
```

## 八城市只读行情运行

以下命令需要分别保持运行。市场事件由 supervisor 自动发现并严格核验，不再手工替换日期 slug。

```powershell
uv run poly-weather market-supervisor --runtime 0

uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed chicago-daily-high-research-seed miami-daily-high-research-seed atlanta-daily-high-research-seed dallas-daily-high-research-seed houston-daily-high-research-seed seattle-daily-high-research-seed chongqing-daily-high-research-seed chengdu-daily-high-research-seed --runtime 0

uv run poly-weather signal-engine --supervised --runtime 0
```

查看心跳：

```powershell
uv run poly-weather stream-status
uv run poly-weather liquidity-report --source auto
uv run poly-weather execution-cost-calibration
```

macOS 使用相同参数，在三个 Terminal 窗口分别运行：

```bash
uv run poly-weather market-supervisor --runtime 0

uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed chicago-daily-high-research-seed miami-daily-high-research-seed atlanta-daily-high-research-seed dallas-daily-high-research-seed houston-daily-high-research-seed seattle-daily-high-research-seed chongqing-daily-high-research-seed chengdu-daily-high-research-seed --runtime 0

uv run poly-weather signal-engine --supervised --runtime 0
```

初次接手建议先在前台运行并观察 `uv run poly-weather stream-status`。验证稳定后再使用 `tmux`、`launchd` 或其他 macOS 服务管理方式；不要一开始就配置自动重启，以免错误参数持续写入数据。

关键文件：

- `data/runtime/polymarket_ws_status.json`
- `data/runtime/market_supervisor_status.json`
- `data/runtime/signal_config_update.json`
- `data/runtime/weather_daemon_status.json`
- `data/runtime/signal_engine_status.json`
- `data/runtime/signal_state.json`

## 建议的下一阶段

先实现联合历史重放与报告，要求每个决策点只能看到当时已经发布的数据，并输出候选次数、命中率、Brier/LogLoss、理论边际、盘口可成交深度和最大不利偏差。回放通过后再接只读告警；真实交易执行继续保持隔离。

## 2026-08-28 数据周期四态与 QUIET maker

该阶段已经实现并完成一次全量离线验证。新增模块为：

- `src/poly_weather/information_clock.py`：统一 external-information clock，保留 source/receipt 双截止、payload hash、修订和不可预测 SPECI；缺 receipt 的事件只作 N/A 诊断。
- `src/poly_weather/market_regime.py`：每个 token portfolio 的 EVENT/DIGESTION/QUIET/PRE_RELEASE/HALTED 状态机，预声明 strict/neutral/lenient 阈值和 PRE_RELEASE 诊断。
- `src/poly_weather/quiet_window_strategy.py`：独立 QUIET maker、token-scoped queue replay、因果匹配、Wilson/cluster bootstrap、after-the-fact decision regret、stop-everything 和三策略风险隔离。

复现命令（只读，默认包含预声明 size grid）：

```powershell
.venv\Scripts\poly-weather.exe analyze-quiet-window --data-dir data
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
```

最后一次归档结果：`106,724` 配对快照、`213,448` token 快照、`18,052` 输入信息事件、`43,543` public trades、`60` 个 station-day cluster。三档固定阈值都没有 QUIET 观测；neutral 为 `EVENT=82,185`、`DIGESTION=130,504`、`PRE_RELEASE=759`。订单、成交、matched control 和 regret 均为 0/N/A，size grid 对 `$20/$50/$100/$200` 做了明确的 no-entry-gate short-circuit。该结果是证据不足，不是 maker 策略的正负收益结论。

产物位于 `data/information_reaction_report.md`、`data/quiet_window_strategy_report.md`、`data/quiet_window_strategy_analysis.json` 和 `data/quiet_window_size_grid.json`。`data/` 被 Git 忽略；本次未启动实时 QUIET follower，也未改变现有 v2 weather lead-lag shadow 主策略。
