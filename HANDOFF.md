# Poly Weather 开发交接说明

更新日期：2026-08-24

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
- 市场、天气、信号心跳监测与过期阻断。
- 候选净边际超过 15% 时强制告警并阻止 paper alert。
- 66 项单元测试及 Ruff 静态检查。

## 当前校准结论

P0 验证确认 Previous Runs `lead_days=0` 含目标日内模型更新，不能用于校准或回测。实时和 CLI 现统一使用 deterministic 数据；历史回归及校准默认使用 lead 1。现有 day 0 数据仍保留用于审计，但所有生产 CLI 和实时校准入口都会拒绝它。

2026-08-22 洛杉矶回归使用 lead 1 单值 82.2°F、81 条此前样本的 `+0.764°F` 偏差和 2.647°F 残差标准差，模型首选桶由旧 ensemble 路径的 86–87°F 修正为 82–83°F。该结果验证管线修复，不证明存在稳定盈利能力。

多模型 walk-forward 实验结果：KLAX 在多模型共同可用的 78 天上有 48 个测试日，真实桶平均概率由同日期 GFS 的 19.80% 提升至 24.94%，首选桶命中率由 25.0% 提升至 43.75%；全样本诊断权重约为 GFS 46.2%、ICON 29.5%、GEM 24.3%。KLGA 的 51 个测试日从 GFS 的 18.84%/27.45% 提升至多模型的 24.82%/35.29%，全样本权重约为 GFS 32.1%、ICON 35.2%、GEM 32.7%。权重在正式评估中按测试日逐步学习，全样本权重只作诊断展示。

## 尚未完成

1. 联合历史回放：需要使用 Single Runs 固定初始化时间，把当时已发布的 deterministic 跑次与 CLOB 盘口严格按时间对齐；Previous Runs lead 1 仍不是完整的单一 vintage。
2. 成交可实现性：完整深度吃单成本和 Weather 官方 taker 手续费曲线已经分别估算，但仍未模拟挂单排队和短时撤单；现有结论仍不能视作可执行收益。
3. 多日、多城市泛化：8 城市行情采集已启动，但新增六城的结算 registry 仍是 `unverified`，且尚未积累足够已结算样本。
4. 自动市场轮换：每日新事件仍需要发现、核验并更新运行参数。
5. Wunderground 最终结算差异审计：实时信号使用同机场 NOAA 数据，但最终仍应记录官方页面值并比较差异。
6. 告警渠道、服务管理、开机自启、进程自动拉起与磁盘保留策略。
7. 真实资金执行明确不在当前范围内。

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
