# 可靠性整改执行记录

As-of：2026-09-08。范围：本地未部署候选；依据为
`CODEX_PROJECT_RELIABILITY_REMEDIATION_TASK.md` 与工程规范 v1.0。
基线见 `reliability_baseline_20260908.json`；HEAD 不代表脏树候选内容。
本记录不替代 CURRENT_CONCLUSIONS 的研究结论，不追认旧测试成绩。

## 阶段 0：责任与边界

| 权威 | 生产入口/职责 | 恢复与展示边界 |
| --- | --- | --- |
| 原始采集 | public_trade_collection：响应及首次 receipt | 不能用文件刷新补造可见性 |
| 规范化 | trade_evidence / market_trade_tape | 身份、精度、质量、原文不丢失 |
| 资格 | market_trade_tape / Paper follower | pending 与普通入口同一资格契约 |
| 经济事实 | paper_account / shadow_orders | append-only；cursor/status 不能授权经济效果 |
| 输入前沿 | collector cursor / shadow cursor | 成功事实先落盘，之后确认位置 |
| 派生展示 | runtime_safety / CLI / docs | 完整性不等于健康；fixture 不等于正式证据 |

只改源码、测试、工程文档；不运行 Paper CLI、不操作 daemon/Task Scheduler、
不修改正式 data、不安装依赖、不读凭据、不作外部 probe、不 commit/push。

## 阶段 1：存储契约

F04：缺失文件才允许初始化；空字节、截尾、坏 JSON、非对象和错误容器类型
都属于隔离对象，原字节不得覆盖。合法 `trades: []` 是 verified-empty，
不是空文件。坏 cursor 阻断整个调用；坏 tape 冻结该 event 前沿并报告，
独立 event 可继续。没有 tape 时不能使用旧 cursor 缩小请求区间。

collector 持有 output-root 与 cursor 的 OS 排他锁（固定路径顺序，非阻塞）；
锁文件不删除，锁随句柄关闭/进程退出释放。此协议约束使用本入口的 writer，
不声称抵御绕过协议的手工改写。锁不是陈旧 PID 文件。
复用 runtime_safety 的 unique-temp → flush/fsync → replace → 平台支持的目录同步；
不向 tape 注入 status checksum/schema。先 tape、后 cursor、最后派生 audit。
cursor 写失败时重试旧前沿，按经济身份去重；audit 写失败不撤销已提交前沿。
Windows 目录同步及断电保证仍受现有 durable writer 的平台限制。

F05：年龄、相等摘要、stat 稳定均不是停止追加的证明。当前 raw writer 尚无
共同封存/删除排他协议，不能凭一个新 boolean 或 marker 给删除授权。
无此证明时 raw retention 必须报告延期，保留源和已有 gzip；不创造新的双份表示。
安全阻断不等于压缩/删除功能已验收，R07/R08 成功归档分支仍待协议与故障验证。
保留天数、采集频率不变，不以停止采集换取排他。

## 反例基线与未关闭项

初始命令：`.venv\Scripts\python.exe -m pytest -q tests/test_reliability_storage.py -p no:cacheprovider`。
实际 exit 1，3 failed：损坏 tape 被覆盖、复制后追加源被删除、无请求 receipt 漂移。
后续结果按真实运行追加。F01–F03/F06/F08 与 Q01–Q08、R01–R15 完整验收尚未完成；
F07 环境维护、恢复采集与正式 retention 各自另需授权。独立复核未进行。
保持未封板，不启动模拟盘；正式 N=0/PnL=N/A 未由本记录重算。

## 阶段 1 检查点（尚非全任务交付）

本阶段实际修改：`src/poly_weather/public_trade_collection.py`、
`src/poly_weather/retention.py`、`tests/test_retention.py`、新增
`tests/test_reliability_storage.py` 和本记录。最终内容清单为
`reliability_stage1_fingerprint_20260908.json`，排除指纹文件自身。

F04 已实现隔离读取、OS collector 排他、共享 durable writer 与 tape→cursor→audit
提交顺序；R05 的 missing/verified-empty/empty/corrupt/truncated/unreadable、
坏对象不污染独立 event、missing tape 不复用旧 watermark 已通过。
R06 覆盖三种文件提交前/后异常及底层 fsync/replace 前/后异常和幂等重试。
这些是注入异常与重新调用测试，不冒充真实进程崩溃/断电测试。
短写、独立进程终止、跨平台锁及 Windows 断电持久性尚未完整验收；F04 不全量关闭。

F05 为 containment：移除当前无封存证明的 raw 压缩/删除分支；新 gzip 不创建，
已有 gzip 不替换，源不删除；old/compress/recent 三年龄范围及全部三来源验证。
R07/R08 的“无排他时不得 mutation”已覆盖（包括损坏 gzip），
可安全完成压缩/删除的正向协议仍待实现，不能宣称归档功能修复完成。
旧 retention 测试中“仅凭年龄/相等内容删除”的期望按 S08 明确禁止；
改为断言保持原字节及返回 deferred，不是将原有不安全操作当成功。
supervisor 现有调用保存完整 retention_result；本轮没有启动 supervisor。

### 实际命令与结果

全部 pytest 使用既有 `.venv\Scripts\python.exe`，所有故障数据在 tmp_path。

| 命令（Python 前缀省略） | 结果 |
| --- | --- |
| `-m pytest -q tests/test_reliability_storage.py tests/test_public_trade_collection.py tests/test_retention.py tests/test_runtime_safety.py -k 'not f03' -p no:cacheprovider` | exit 0，47 passed / 1 deselected，0.76s；明确排除尚未修复 F03 |
| 同上去掉 `-k 'not f03'` | exit 1，47 passed / 1 failed，0.80s；失败为 F03 receipt 01:00→02:00 |
| `-m pytest -q -p no:cacheprovider --ignore=tests/test_fees.py --ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py --ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py` | exit 1，480 passed / 1 failed，14.99s；唯一失败为 F03；仅诊断子集，不是完整回归 |
| `-m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider` | exit 0，10 passed，0.67s，无 skip |
| `-m ruff check src tests` | exit 0，All checks passed |
| `uv lock --check --offline` | exit 0，32 packages；offline 避免外部请求，未安装/同步 |
| `git diff --check` | exit 0；仅既存 LF/CRLF 提示 |

调试中另有两次 fixture 错误（Windows CRLF 字节假设、fake calls 元组误作 dict），
已按真实接口修正并重跑，不计为生产缺陷。默认完整套件本检查点未重跑，
已知异步环境挂起仍按 F07 分流，未 monkeypatch 网络栈、未无限重试。

### 状态与规范覆盖

| 范围 | 当前状态 |
| --- | --- |
| F01/F02/F03 | 待阶段 2；F03 红测保持可见，未 xfail/skip |
| F04 | 实现及部分故障验证完成，剩余边界如上 |
| F05 | 安全阻断已验证；安全归档正向协议待办 |
| F06/F08 | 待阶段 3，不沿用历史状态为当前健康 |
| F07 | 环境维护另需授权；未修系统网络 |
| Q01–Q08 / R01–R04 / R09–R15 | 待后续阶段完整验证，不用旧成绩代替 |
| S01/S02/S11 | 本阶段权限、责任、基线与变更记录落实 |
| S03/S07/S08 | 上述存储契约部分落实，限制显式保留 |
| S04–S06/S09 | 尚未整改完整，不勾选符合 |
| S10/S12/S13 | 红绿结果、范围和残留记录；完整验收清单未完成 |

没有正式 data 写入/压缩/删除、没有 Paper CLI/daemon 操作、没有 commit/push；
这是本任务实际调用范围声明，不是正式 data 全量字节审计。
后续仍按阶段 2 的 F03→F01→F02 推进，并保留 F04/F05 正向故障验证债务。
