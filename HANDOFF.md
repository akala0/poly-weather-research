# Poly Weather 开发交接说明

更新日期：2026-08-22

## 项目定位与安全边界

这是一个仍在开发中的 Polymarket 天气市场研究系统，不是可投入资金的交易机器人。当前只访问公开的 Polymarket、NOAA/NWS、NOAA Aviation Weather Center、NOAA NCEI 和 Open-Meteo 接口。仓库中没有钱包适配器、私钥读取、签名、下单、撤单或转账功能；实时信号的所有 `action` 都被硬编码为 `skip`。

不要在未经独立安全审核、回放验证和用户明确授权前加入真实执行路径。

## 当前已完成

- Polymarket Gamma/CLOB 市场发现、历史价格采集和公共 Market WebSocket。
- WebSocket 单连接同时监测纽约和洛杉矶共 44 个 outcome token。
- NOAA/NWS 最新机场观测、AWC METAR/TAF 与 GEFS 集合预报采集。
- KLGA、KLAX 双站点异步天气守护进程。
- 原始 JSONL、SQLite/DuckDB 研究存储和带时间戳的审计记录。
- 温度分桶解析、连续性检查、整华氏度 `ROUND_HALF_UP` 规则。
- 结算证据解析、SHA-256 快照与 fail-closed 核验。
- 同站点 NOAA NCEI 日最高温与 Open-Meteo Previous Runs 历史预报连接。
- 无前视偏差的滚动校准评估：MAE、RMSE、Brier、LogLoss。
- 实时原始概率、验证后选用概率、盘口净边际和可信度闸门。
- 市场原始流与 DuckDB 写入分离，数据库检查点不会阻塞 WebSocket 收包。
- 市场、天气、信号心跳监测与过期阻断。
- 37 项单元测试及 Ruff 静态检查。

## 当前校准结论

本机研究库各包含 80 个 `lead_days=0` 样本和 80 个 `lead_days=1` 样本。实时系统使用 `lead_days=0`，且只使用目标日前的数据。

- KLGA：50 个样本外测试；原始 RMSE 约 1.469°F，校准 RMSE 约 1.474°F。保留已验证的原始概率，不采用偏差修正。
- KLAX：50 个样本外测试；原始 RMSE 约 0.855°F，校准 RMSE 约 0.926°F；Brier 与 LogLoss 改善，且 RMSE 退化未超过 10% 闸门。采用约 `+0.385°F` 的偏差修正。

这些结果只说明管线与当前校准策略通过了预设验证规则，不证明存在稳定盈利能力。

## 尚未完成

1. 联合历史回放：需要把当时可见的天气、集合预报版本和 CLOB 盘口严格按时间对齐。
2. 成交可实现性：当前边际基于最佳卖价和固定成本缓冲，尚未完整模拟盘口深度、排队、滑点和短时撤单。
3. 多日、多城市泛化：当前实时配置只验证了 KLGA/KLAX 和 2026-08-22 两个事件。
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

`data/` 被有意排除，因为其中包含约数百 MB 的实时 JSONL、DuckDB、WAL、状态和日志。新电脑有两种选择：

- 从这台电脑安全复制整个 `D:\poly\data` 目录，以保留监测历史；复制前先停止三个守护进程，避免得到不一致的 DuckDB/WAL。
- 不复制数据，在新电脑重建校准样本。没有研究库时，实时校准闸门会保持 `blocked`，这是预期的 fail-closed 行为。

DuckDB、SQLite 和 JSONL 文件可以从 Windows 复制到 macOS，但不要复制 `.venv`、PID 或旧日志。建议在停止守护进程后打包 `D:\poly\data`，在 Mac 仓库根目录解压为 `data/`。历史 payload 中可能保留旧的 Windows 绝对归档路径；这些字段只用于审计展示，不应作为 Mac 上的新写入路径。

重建当前实时校准样本：

```powershell
uv run poly-weather backfill-calibration new-york-daily-high-research-seed 2026-06-01 2026-08-21 --lead-days 0
uv run poly-weather backfill-calibration los-angeles-daily-high-research-seed 2026-06-01 2026-08-21 --lead-days 0
uv run poly-weather evaluate-calibration new-york-daily-high-research-seed --lead-days 0
uv run poly-weather evaluate-calibration los-angeles-daily-high-research-seed --lead-days 0
```

## 双城市只读运行

以下三个命令需要分别保持运行。事件 slug 带日期，切换日期时必须先核验新事件规则，不能只替换日期后直接认为可用。

```powershell
uv run poly-weather market-stream highest-temperature-in-nyc-on-august-22-2026 highest-temperature-in-los-angeles-on-august-22-2026 --runtime 0

uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed --runtime 0

uv run poly-weather signal-engine --market highest-temperature-in-nyc-on-august-22-2026=new-york-daily-high-research-seed --market highest-temperature-in-los-angeles-on-august-22-2026=los-angeles-daily-high-research-seed --runtime 0
```

查看心跳：

```powershell
uv run poly-weather stream-status
```

macOS 使用相同参数，在三个 Terminal 窗口分别运行：

```bash
uv run poly-weather market-stream highest-temperature-in-nyc-on-august-22-2026 highest-temperature-in-los-angeles-on-august-22-2026 --runtime 0

uv run poly-weather weather-stream new-york-daily-high-research-seed los-angeles-daily-high-research-seed --runtime 0

uv run poly-weather signal-engine --market highest-temperature-in-nyc-on-august-22-2026=new-york-daily-high-research-seed --market highest-temperature-in-los-angeles-on-august-22-2026=los-angeles-daily-high-research-seed --runtime 0
```

初次接手建议先在前台运行并观察 `uv run poly-weather stream-status`。验证稳定后再使用 `tmux`、`launchd` 或其他 macOS 服务管理方式；不要一开始就配置自动重启，以免错误参数持续写入数据。

关键文件：

- `data/runtime/polymarket_ws_status.json`
- `data/runtime/weather_daemon_status.json`
- `data/runtime/signal_engine_status.json`
- `data/runtime/signal_state.json`

## 建议的下一阶段

先实现联合历史重放与报告，要求每个决策点只能看到当时已经发布的数据，并输出候选次数、命中率、Brier/LogLoss、理论边际、盘口可成交深度和最大不利偏差。回放通过后再接只读告警；真实交易执行继续保持隔离。
