# 原始 market 限定恢复执行报告

日期：2026-09-10。**结果：未恢复；累计三次 child 失败后已停止本轮。**

market 最终停止且禁用，没有剩余或未确认的 market child；保留带 `-RawMarketRecovery` 的新 action 和原 action/XML 备份。signal/shadow 已停止且禁用。weather 保持原进程链运行，Paper 未启动。此次没有恢复深度采集，不满足原始采集限定恢复验收。

## 执行边界与终态

| 任务（TaskPath 均为 `\`） | 原状态 | 实际操作及最终状态 |
|---|---|---|
| PolyWeather-signal-engine | enabled / Running，等待 runner PID 4516 | 先禁用，再停止等待 runner；Disabled / enabled=false，无子进程 |
| PolyWeather-shadow-spread-engine | enabled / Running，等待 runner PID 3216 | 先禁用，再停止等待 runner；Disabled / enabled=false，无子进程 |
| PolyWeather-market-supervisor | enabled / Running，旧 runner PID 4504 | 禁用、停止旧 runner、仅追加 action 参数、启用并启动一次；第三次失败后再次禁用并停止新 runner 16944 |
| PolyWeather-weather-stream | enabled / Running，runner PID 4524 | 未操作；仍 Running / enabled=true |

weather 子进程链 `4524 → 17784 → 17912 → 17944` 的 PID、创建时间、命令及 executable 前后相同；weather run ID 为 `dce60e51-02f7-493c-834d-df3ced93a0fc`。配置 XML 仅导出末尾换行不同，解析后的内容完全相同。末次保存心跳为 `2026-09-10T08:57:00.063020+00:00`。

market action 仍使用原 PowerShell executable、工作目录 `D:\poly`、原 principal/trigger/settings。唯一 action 参数变更是在原参数尾部追加 `-RawMarketRecovery`。更新后、启用前已重新导出 XML 并逐项核对。下游不自动恢复 enabled 状态。

## 候选预检

- HEAD：`2c8d9b9d91504d579912985aad33ffd01bc5011a`。保留全部既存 dirty/untracked 文件和 `.claude/`；本任务没有修改产品源码、配置或测试，没有 commit/push。
- runner SHA-256：`44fb107523b37b9d312ba0d48cbe083cfb2ab836f3900eb366a29519e4627b10`。源码、测试、PowerShell、配置及依赖清单的完整内容指纹见 validation JSON / `preflight.json`；完整测试前后及最终交付时一致。
- 实际解释器：`D:\poly\.venv\Scripts\python.exe`，Python 3.14.3，Windows AMD64。
- 完整默认套件：`python -c 'import faulthandler,pytest; faulthandler.dump_traceback_later(30,repeat=True); raise SystemExit(pytest.main(["-q","-p","no:cacheprovider"]))'`，**753 passed in 50.03s，退出码 0，无跳过**；外层用时 50.79 秒，180 秒上限，30 秒重复堆栈。未超时，未终止测试进程。原始 stdout/stderr 已保存；周期堆栈不是失败。
- 故障测试包含 null ExitCode、wait 异常后 child 活着、状态不可查询和已退出反例。源码核对确认：原值非 null 且已确认退出才转 int；launch 前持久化 unresolved；无法确认退出则禁止重试；新 runner 拒绝已有 unresolved。
- `ruff check src tests scripts --output-format concise` 退出码 0；runner PowerShell 解析无错误；最终 `git diff --check` 退出码 0。相关未跟踪故障测试纳入完整套件及指纹。
- 首次 `ruff check .` 退出码 1，三个 lint 问题来自既存 `.claude` 工作树及本任务证据用 `preflight.py`，原输出保留。没有修改这些文件来消除检查结果；随后候选范围检查通过。不能把全目录 Ruff 写成通过。

原始测试结果、命令、环境、源码指纹、git 状态及 XML 备份均位于 [证据目录](raw_market_recovery_20260910/)。此次没有以此前的 741/753 或定向测试代替当前候选完整测试。

## 时间线与 attempt 证据

以下时间为 UTC；北京时间加 8 小时。

| 时间 | 已完成动作 |
|---|---|
| 08:53:49.216 | signal task 禁用，旧等待 runner 4516 确认退出 |
| 08:53:50.407 | shadow task 禁用，旧等待 runner 3216 确认退出 |
| 08:53:51.952 | market task 禁用，旧 runner 4504 确认退出；无旧 child |
| 08:53:53.085 | 新 market action 参数及其余 XML 设置核对通过；仍保持禁用 |
| 08:55:31.700 | 开始有界监督，启用并启动一次 market；30 分钟截止为 09:25:31.700 |
| 08:56:11 左右 | 第三次 child 失败；监督循环触发禁用及停止 |
| 08:56:13.637 | 停止后确认 market 剩余进程数 0，unresolved 不存在 |

| attempt ID | child PID | 开始 / 结束 UTC | 阶段 / 实际退出码 |
|---|---:|---|---|
| `20260910T0855333928914Z-4bb49bb58e01480d9f017ea915305899` | 27072 | 08:55:33.393 / 08:55:45.431 | strict_verification / 2 |
| `20260910T0855505376425Z-b28ded0cc1e24e95aa7508cb86aecd12` | 25436 | 08:55:50.538 / 08:55:57 | strict_verification / 2 |
| `20260910T0856077097498Z-bc9e70c7dcb44591a6735db4a3017127` | 26220 | 08:56:07.710 / 08:56:11 | strict_verification / 2 |

三次均记录 `candidate_count=10`、`rejected_count=10`、`verified_event_count=0`、`verified_template_count=10`、`execution_enabled=false`、真实 process exit code 2、`child_exit_confirmed=true`、`restart_blocked=false`。独立 attempt 目录、start/result manifest、stdout/stderr 及内容哈希已复制留证；日志哈希与 manifest 一致。未发生第四次 attempt。

实际进程快照捕获第二次 child 链 `16944 → 25436 → 16656 → 33976`；命令带对应 `--startup-attempt-id` 和 `--raw-collection-recovery`。第一次和第三次 child PID 由 runner manifest 留证，未捕获其全部瞬时进程链，不把 manifest 等同于完整 OS 快照。

本轮失败分类为严格核验阶段拒绝，未记录 discovery transport/HTTP failure。**具体拒绝原因仍未知**：当前初始发现路径把逐事件 parse/verification 异常与 `verification.passed=false` 都归入 rejected_count，未保存逐事件证据/拒绝明细。不能据此断言具体站点规则、结算源或网络配置有问题。本任务没有加探针、改 registry 或放宽核验，也没有在运维期间修代码。

## 数据、隔离与验收结果

- 允许写入路径在进程操作前保存于 `write-whitelist.json`。本轮实际 market 写入为 runner/attempt 日志与 manifest，以及 runner 在 launch 前创建、确认退出后移除的 unresolved 标记；操作者未删除标记。不能声称正式 data 完全没有写入。
- 三次均在初始化 collector/supervisor/market 数据库之前退出。今日没有新增 market WS、checkpoint、Gamma 或 settlement 原始归档；订阅 token=0、完整簿=0、reconcile 周期=0、新 run ID=N/A、新已确认发布位置=0。旧 market/supervisor 状态文件前后字节相同，仍属历史状态，不能作为当前采集证据。
- 选定 21 个旧 v2 ledger/cursor/status、signal 状态/配置及项目配置文件的全文件 SHA-256 前后相同；逐文件大小、mtime 和读取前后 stat 见 `files.before.json` / `files.after.json`。未对全部历史归档或 DB 做全文件哈希，不以 git status 推断 data 不变。
- 未启动 signal/shadow/Paper；没有新选定 Paper 输出。raw recovery 源码跳过 retention、signal 配置发布、显式 DB 维护；实际运行在这些路径之前已退出。未进行压缩、删除、历史迁移、回填、DB CHECKPOINT/VACUUM 或修复。
- market DB 最终大小 2,727,620,608 字节，WAL 4,008,068 字节；mtime 均早于本轮（2026-09-03）。未用可写连接探测或恢复 DB。由于未进入采集，DB 事务、队列、写入延迟运行验收均 N/A。
- weather 自己继续写入：今日 weather raw 从 31,945,989 增至 32,164,711 字节；这是既有外部 writer 的推进。本轮没有停止或重启 weather。
- 终态 D 盘空闲 78,056,783,872 字节，仅为该时点磁盘快照；不代表负载或容量压力验收。

连续 10 分钟、两次 reconcile、真实完整簿及 committed raw 前进条件均未达到，故明确判定未恢复。监督在约 42 秒内因三次失败结束，没有等待 30 分钟或继续默认 crash loop。未新增定时任务或长期监控。

## 后续边界

market 保持禁用，新 action 保留；signal/shadow 继续禁用，weather 照常运行。下一步需要独立处理逐事件严格核验拒绝证据缺口；诊断或代码修改完成后再单独授权下一轮恢复。此次没有实施 Single Runs 验证，也没有改旧 v2 cursor/库存。

结构化交付：[raw_market_recovery_validation_20260910.json](raw_market_recovery_validation_20260910.json)。证据脚本是本次执行记录，不是可直接重复执行的长期运维接口。

项目继续 **NOT SEALED**；公共组闭合 **UNSUPPORTED**，正式 Paper **N=0、PnL=N/A**。原始行情恢复授权不构成 Paper 或真实执行授权。
