# Codex 任务：原始 market 采集限定运维恢复

日期：2026-09-10。状态：待执行。

用户已明确授权限定恢复，并要求将授权写成任务。本文件的编写不代表运维已经执行。执行者只可按下列范围接管，不得将采集恢复解释为 Paper 启动、下游恢复或正式封板。

## 1. 授权范围与目标

目标：在 signal/v2 shadow 被隔离、weather 不受干预的前提下，将原始 market/supervisor 切换到已实现的 `-RawMarketRecovery` 模式，建立可核查的新原始行情采集边界，并完成有界验收。

允许的运维目标仅为实际核实后的三个项目任务及其所属进程：

- `PolyWeather-signal-engine`：先禁用触发，再停止等待中的 runner，保持禁用；不启动 signal 子进程。
- `PolyWeather-shadow-spread-engine`：同上，旧 v2 账本/cursor/库存不动。
- `PolyWeather-market-supervisor`：临时禁用防并发拉起，停止旧 runner，确认没有旧 market 子进程，修改现有 action 加入 `-RawMarketRecovery`，再启用并启动新 runner。

任务实际 TaskPath、action、PID、启动时间、命令归属必须重新查询。不得按模糊名称批量停止 PowerShell/Python，不使用旧状态文件 PID 作为停止依据。

**不在授权内：** weather 任务/进程操作；启动 signal、v2 shadow、Paper；真实执行或认证接口；策略/registry/校准修改；网络、代理、防火墙、证书或 Python 安装修复；依赖升级；代码修复；commit/push；历史归档压缩、删除、回填、迁移；旧 cursor 补 hash、清空或 tail-bootstrap。

## 2. 正式数据写入的精确例外

本次恢复会产生正式原始采集输出，不能再声称“正式 data 完全没有写入”。允许现有 raw-recovery 路径正常写入：

- 新 market WS/checkpoint/必要市场元数据与公开质量证据；
- market 数据库的正常采集事务及必要 WAL；
- market/supervisor 的 runtime status、active set、运行身份；
- market runner/attempt 日志、manifest、ownership 阻断标记。

执行前从实际代码列出完整路径白名单并保存。未知或额外副作用先停在预检，不自行扩大白名单。正常数据库内部行为不等于授权显式维护；禁止运行 retention、显式自动 CHECKPOINT/VACUUM、修复或重建数据库。

禁止写旧 v2 ledger/cursor/status、signal state/config update、Paper 输出或天气配置。weather 自己继续写入的日志/状态/原始记录属于已有外部 writer，不得误报为本轮操作，也不得为比对而停止它。

保留历史字节和原始来源身份；新 run 不追认旧深度空洞或旧经济效果。公共组闭合仍 UNSUPPORTED，正式 Paper N=0、PnL=N/A、NOT SEALED。

## 3. 必读与候选预检（先于任何进程操作）

完整阅读 `AGENTS.md`、`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`，以及：

- `docs/collection_recovery_plan_20260910.md`；
- `docs/source_capability_audit_20260910.md` 和 `docs/source_recovery_evidence_20260910.json`；
- 当前 runner、market CLI、market supervisor/stream、runtime safety 的恢复相关代码；
- `tests/test_windows_runner_faults.py` 及本轮相关测试。

记录实际 HEAD、dirty/untracked 清单、解释器路径、候选源码/测试/runner 内容 SHA-256，不用 HEAD 代替脏树指纹。保留已有 `.claude/` 与所有未提交修改，不切分、清理或覆盖他人改动。

已知报告称本轮 `753 passed in 49.14s`、无跳过；741 是更早候选的证据。执行者须核对本轮完整命令、退出码、环境和候选绑定。如无可复核原始记录，先在临时测试环境重跑当前完整套件，设置 180 秒外层上限、定期堆栈输出，并仅终止本次拥有的测试进程树；超时/失败不进入运维阶段。测试不得操作现有任务或正式 data。使用既有解释器，不同步依赖。

预检还须确认：

1. null 退出码不转换为 0，unknown 不记成功。
2. launch 之前写 unresolved 标记；无法确认 child 已退出时不重试，新 runner 也拒绝启动。
3. 恢复模式跳过 retention、signal 配置发布和显式数据库维护。
4. 下游健康闸门识别恢复模式；实际 Task Scheduler 禁用提供独立隔离，不只依赖状态字段。
5. runner PowerShell 解析、相关故障测试、Ruff 与 diff 检查通过；未跟踪文件另核验，不以 `git diff` 代替其检查。

任何代码漂移或新缺陷需回到独立代码修复任务，本运维任务不得边部署边改代码。

## 4. 操作前留证与停止条件

在新建 `docs/raw_market_recovery_20260910/` 中保存必要脱敏证据；目录若已存在则新建带实际时间的子目录，不覆盖旧记录。最终报告以第 8 节为准。

留证至少包括：

- 三个目标任务的原始 XML/config、enabled、action、触发/重试设置、工作目录；保存格式不得泄露敏感参数。只导出配置，不读取账户密码。
- weather 当前 PID/创建时间/命令归属/心跳和原始输出基线。
- 目标 runner 和 child 的实际父子关系、PID/创建时间/命令归属。
- market 旧失败日志的有界副本及范围 hash；若日志在变化，分时点保留，不宣称整个文件不可变。
- 旧 v2 ledger/cursor/status 选定全文件 SHA-256 与大小，以及 signal 配置/状态的保护基线；明确读取范围和竞态。
- 现有 `market-supervisor.child-unresolved.json` 是否存在。**存在即暂停，不删除、不覆盖、不自动清锁；单独交付所需 ownership reconciliation 证据。**

若发现 signal/shadow 有实际健康子进程，停止本任务并报告，与“仅停止等待 runner”的授权条件不符。若 market 已有健康采集进程，不为完成清单而中断它：报告现况和与恢复目标的差异，等待进一步决定。

OS 权限不足、PID/任务归属不清、多个同名任务/runner、数据库异常、无法安全保存原配置时，停止在当前步骤，不通过扩大系统权限配置、批量杀进程或重建文件绕过。

## 5. 接管顺序（严格串行）

### O1：先隔离下游

1. 只禁用已确认的 signal/shadow 两个任务，防止 Boot/Logon/重试触发。
2. 停止对应等待 runner；确认任务停止且相应进程确已退出。任务显示 disabled 不等于现有进程已退出。
3. 复核没有 signal/shadow 子进程；核对 weather 不受影响。
4. 两个下游任务在成功或失败交接后都保持禁用，**不得自动恢复原 enabled 状态**。重新启用属于后续独立授权。

### O2：接管 market

1. 临时禁用 market 任务，停止已确认的旧 runner，等待实际退出。
2. 检查其旧 child。若确有尚存但不健康的目标 child，只能在证实 PID/创建时间/父子关系/命令均匹配后停止该 child；不得触碰其他进程。无法确认则暂停。
3. 确认旧进程退出、唯一 writer 条件成立；不得强制释放不明 mutex 或绕过 unresolved 标记。
4. 仅更新现有 market task action 的参数，保留 executable、TaskPath、principal、工作目录、trigger 和其它设置。去重加入 `-RawMarketRecovery`，不替换为 Paper、不添加未验证参数。
5. 在启用之前重新导出 action 并核对参数与 runner hash；再启用、启动一次。记录实际新 runner/child PID、启动时间、attempt ID 和命令行中的恢复开关。

旧 PowerShell 内存中的脚本不会随磁盘文件修改而更新。必须用实际新进程身份及 attempt 证据确认接管，不仅核对磁盘脚本内容。

## 6. 有界验收与自动重试上限

本轮主动验收窗口：从新 market 任务启动起 **最多 30 分钟**。每次状态查询也设超时；执行者须持续监督，不能启动后结束任务而让失败无限循环。

- 最多允许 **3 次失败的 child attempt**。到达第 3 次失败即禁用本轮 market 任务并停止其已核实 runner，不等待默认 crash-loop 继续下一轮。
- 未在 30 分钟内满足下列成功条件，则判本轮未恢复，禁用 market 任务并停止本轮拥有的目标进程，保留所有证据。
- 出现归属冲突、未知 child、越权副作用、写入失败、严格核验异常被绕过，立即停止继续启动。无法确认 child 身份时不强杀，保留阻断并报告人工介入。
- 网络/HTTP/解析/strict verification 失败分别报告；不改 registry、网络或核验闸门。正常公开采集请求属于本授权，额外探索性 probe 不属于。

成功条件必须同时满足：

1. 新 runner/child 归属已确认，attempt ID 与当前 source/run/status 可关联；恢复模式标识存在，`execution_enabled=false`。
2. 严格事件核验通过，active set 可追溯；记录订阅 token 数、已获得 token-native full book 数、未覆盖项及原因。不能用 new_market、成交价或摘要价格当完整簿。
3. 至少连续 **10 分钟**无 child 重启，原始记录与已确认发布位置实际前进，并跨越至少两个按当前配置应发生的 supervisor reconcile 周期。无消息时以真实完成的检查/连接证据区分 quiet 与停滞，不能伪造进度。
4. status/heartbeat/PID/ownership/业务证据相互一致。恢复模式故意使下游 health_ready=false，应单列“原始采集事实已确认”与“下游禁止启动”，不能为了显示绿色去掉闸门。
5. retention、signal 配置发布、显式维护均未调用；signal/shadow 没有启动；weather 未被停止/重启；旧 v2 保护文件未被本轮改写；无 Paper 输出。
6. 数据库正常写入与队列无持续无界积压；记录实际队列/延迟/磁盘状态和测量范围。不用心跳数代替业务记录，也不将短窗验收称为生产规模或 12 小时稳定性证明。

## 7. 到期处置与回退边界

**成功：**保留已恢复的 market 任务在 raw-recovery 模式持续采集；这是本次“恢复采集”授权的预期终态，不启动下游。结束主动验收时明确告知 market 将继续运行、两个下游仍禁用、后续长期监测尚未完成。不新增计划任务/监控自动化。

**失败：**market 任务保持禁用并记录失败原因；保留新的恢复 action 和原 action 备份，不自动恢复旧 action 并重新启用，避免旧失败循环。两个下游保持禁用，weather 保持原状。

成功后将保留既有 runner 退避/重试设置；本轮 3 次失败/30 分钟限制约束主动验收窗口，不宣称另有持续失效自动停止器。若用户要求长期次数上限，需独立实现/授权，不能伪装已具备。

不删除新原始数据、日志、manifest 或 unresolved 标记来“回退”。异常 child 不明时即使验收时间到，也只报告阻断与最后已确认状态，不谎称所有进程已停。成功/失败均不触碰 weather、不恢复旧 v2、不启动 Paper。

## 8. 最终交付

新增：

- `docs/raw_market_recovery_report_20260910.md`：执行时间线、授权动作、成功/失败判定、实际终态。
- `docs/raw_market_recovery_validation_20260910.json`：候选指纹、命令/退出码/时限、任务 before/after、attempt、PID/run、订阅/完整簿/业务进度、隔离与副作用检查证据索引。

报告必须先说明：market 是否实际恢复并继续运行；weather 是否保持原进程；signal/shadow 是否停止且禁用；Paper 未启动；如失败则 market 最终是否禁用、有无未确认 child。

列出精确启用/禁用、action 和进程变更，不能只写“没有操作 daemon”。正式新采集写入是本轮授权行为，应单独声明；`git status -- data` 空不等于正式 data 未变。保留所有既存脏工作树，不 commit/push，不改旧报告/指纹或研究成绩。

最终状态最多是“原始采集限定恢复通过，待长期运行观察”。项目继续 **NOT SEALED**，公共 queue 仍不合格；恢复 raw 数据采集不构成 Paper 或真实执行授权。
