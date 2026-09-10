# 可靠性整改执行记录

## 当前工程入口：E01–E04 局部交付、完整回归通过（2026-09-09，未部署）

公共 Paper 入口 **accepted_queue_trade_rows=0**，属于 CONTAINMENT_ONLY；
正向成交能力尚无可验证来源语义。正式样本/启动唯一来源为
[CURRENT_CONCLUSIONS](../CURRENT_CONCLUSIONS.md#paper-v1-formal-status)。
本轮测试名、参数化总数、日期、源码 hash 和诊断排除项只从生成的
[reliability_test_inventory.json](reliability_test_inventory.json) 读取。
下方 Stage 1/2 数字属于对应候选的历史运行结果，不是本轮当前总数。
新任务 P0 先纠正文档：旧生产 fill 声明被替代，下层 MODEL_KERNEL 不再充当
PRODUCTION_INGRESS 证据。

本轮实现 receipt fact/witness journal 与恢复、共享健康判定、完整质量区间拒绝、
等价归档双份去重/冲突阻断；P2 决定为 `UNSUPPORTED_GROUP_COMPLETENESS`。
之后 E01–E04 已补成员集合、跨文件恢复、归档 cursor、天气前缀与业务进度的局部证据。
独立完整 pytest **741 passed in 34.44s，exit 0**，无文件排除/跳过；测试前后 220 个候选文件指纹一致。
当前完整回归阻塞已解除；旧 socket 根因没有被证明已修复。公共成交闭合与真实 forecast vintage
仍有来源缺口，旧无哈希非零 cursor 仍阻断，大规模性能及 Q05/Q06/Q08 余项继续待验收。
当前交付见 [E01–E04 报告](reliability_evidence_closure_report_20260909.md)，
最新验证见 [独立完整复测](reliability_independent_full_validation_20260909.json)。
前轮 [28 项报告](reliability_next_phase_report_20260908.md) 与其验证文件保留为历史。
契约依次为 [receipt](reliability_receipt_contract.md)、
[来源语义](reliability_trade_source_semantics.md)、[健康真值表](reliability_health_contract.md)，
跨消费方边界见 [consumer audit](reliability_consumer_audit.md)。继续 NOT SEALED。

<!-- SUPERSEDED_HISTORY -->
## 历史阶段记录（按当时候选解释，不作为新阶段验收）

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

## 阶段 2 检查点（2026-09-08；未部署）

本轮开始读取到 HEAD `35ccb4530f6ec031d4b590e9ef688a49e5e60112`，
Git 状态仅有既存未跟踪 `.claude/`。这与阶段 1 的脏树基线不同；
本轮没有执行 commit、reset、stash 或全局 Git 配置变更。
因沙箱用户归属提示，Git 只读命令使用单次 `-c safe.directory=D:/poly`。
最终候选绑定 `reliability_stage2_fingerprint_20260908.json`，包含 tracked/untracked
源码、测试、配置、公开工程文档和脚本，不包括 data、凭据或该清单自身。

### F03：首次 receipt（已修复所列路径，恢复边界仍有局限）

生产链：`collect_depth_event_trades` → `_merge_trades` → row-level tape →
`load_event_trade_tapes` / `_public_trade_events_from_file` → Paper/v2 消费者。
collector 新 tape schema 为 3；新增字段为本地规范化元数据，不是上游 wire 字段。

- `clock` 在请求前、同步 client 返回完整结果后、写文件前分别读取，检查 aware UTC
  和单调性；`now` 只保留报告时间兼容，不再用它伪装 receipt。
- 新行冻结 `request_started_at`、`response_received_at`、`first_seen_at`、
  `available_at`、原文 receipt 和 `receipt_provenance=collector_market_trades_return_v1`。
- `file_written_at` 表示写入开始前的时刻，不声称是 fsync 完成时刻。
- 已存在行逐字段保留；无请求刷新、重复返回、迟到 sibling、请求失败后重试不改旧 receipt。
- 旧行缺 receipt 保持 historical-only；两个消费者不再回退文件 fetched_at/mtime。
  shadow queue 入口拒绝缺失 receipt，而历史诊断仍可读取这类行，不将其作为前向资格。

限制：这里证明的是完整 `market_trades` 返回后的本地可见上界，不是分页 HTTP
逐页/逐 socket 的最早到达时间。若首次响应尚未有任何 durable 事实就遭遇进程退出或
首次 tape 写失败，不能恢复已丢失的首次时刻；本轮没有响应 WAL，不能声称此边界关闭。
没有改写或迁移正式旧 tape。跨质量区间的统一资格审查仍在 Q03。

### F01：pending 质量单调性

`MarketWsTrade` 保留真实归档的 `upstream_incident_id`；解析缺失 upstream_status
为 unknown。pending evidence schema 2 持久化 quality、incident、run/source、market slug、
精确 source/receipt 原文；恢复时不再硬编码 normal/run=None。
普通匹配与恢复继续复用 `build_shadow_trade_events`，没有 mock 合格结果。

degraded/maintenance/unknown/缺失状态 → 原生 follower 两轮 → 重启后两轮，
均保持零成交及原质量/incident/run。另测新健康 observation 不改写旧坏 pending、
旧缺字段恢复为 unknown。旧 schema evidence id 不自动迁移；未知旧 pending 可能持续
阻断，需要明确的另行审计，不以清空旧记录解决。

### F02：组完整性 fail-closed；正向成交能力未恢复

最初仅拒绝无序单条还不充分：新增反例证明两条有 sequence 的交易仍会在缺组结束证明时
产生 1 股模型成交。最终公开 `process_trade/process_trades` 入口因此不再调用经济内核：
所有当前 API/WS 证据只持久化观察和 UNKNOWN，不消耗 queue、不产生 fill。
**这是安全阻断，不是已经找到完整组证据。当前 Paper 原生 tape 成交能力不可用。**

`trade_time_groups` 由既有 append-only identity journal 恢复，以 token + UTC 秒索引，
在已消费去重前纳入成员；sequence 和完整性分别记录。API/WS 的序号、poll 边界、
文件边界、等待时间都不作为完整组证明。无排序记录 `UNKNOWN_TRADE_SEQUENCE`，
组未封闭记录 `UNKNOWN_TRADE_GROUP_COMPLETENESS`，计数单位为 durable group，
不再随同组批次中行数变化。后来的 sequence 可以补充排序观察，但不能解除完整性 UNKNOWN。

旧已消费组遭遇新证据时追加 `UNKNOWN_TRADE_GROUP_INVALIDATED`；测试比较原 ledger
字节前缀及完整订单状态，旧经济事实不重写。重启不重复追加同一失效记录。
status 的 `matched_trade_rows` 与 `accepted_queue_trade_rows=0` 分开；组完整性未证明
始终阻断 paper_score_eligible。启动只重建组与禁止干净评分，不声称自动修正全部历史账目。

原经济算法保留为私有 `_process_ordered_model_trades` 下层模型内核，当前生产调用点为零。
测试 `paper_model_support.model_trade` 直接调用该内核验证 queue/account/intent/commit/recovery，
不 monkeypatch 验证器，不从公开入口传入一个“允许成交”布尔值，也不编造上游 completeness。
相关旧经济夹具改为显式排序，其通过只代表下层经济一致性，**不再计作生产证据入口合格**。
原生 follower 测试保留真实调用，期望匹配可解决但组完整性仍未知、零队列消费。
原 follower account-commit 故障现在不可达，改测实际可达的 decision-append 失败与 cursor/HALT；
account-commit 的经济故障覆盖保留在下层恢复/故障参数测试中，不宣称原生路径已验收。

未来若恢复正向消费，必须先有真实 producer→完整组证据→verifier 契约及审查，
不能让公开 JSON 的一个新字段或私有模型内核直接授权。

### 新增矩阵与实际验证

`tests/test_reliability_evidence.py` 为本阶段新增入口/恢复测试：

| 需求 | 测试与断言 | 结果/限制 |
| --- | --- | --- |
| R03/R04 | distinct_clocks_and_duplicate_receipt、legacy_missing_receipt、failure_retry_and_late_sibling、backwards_response_clock | 对应路径通过；响应未落 durable 事实的崩溃窗口未关闭 |
| R01 | pending_restart、native_follower_two_polls_restart、old_pending_missing_quality、new_healthy_observation | 质量与来源不升级；完整质量区间资格仍归 Q03 |
| R02 | split_unsequenced_group、native_follower_batch_file_switch_restart（有/无 sequence、同/分批、跨文件和重启） | 公开入口始终零消费；不构成正向组完整性证明 |
| R12/R13 | late_evidence_invalidates_old_consumption_without_rewriting | 旧字节前缀和订单保持、失效幂等；所有历史迁移情形未穷尽 |

Python 3.14.3；pytest 8.4.2；ruff 0.16.4；httpx 0.28.1；duckdb 1.5.5；
nautilus-trader 2.0.0rc4；未安装/同步依赖。
所有 pytest 命令均用 `.venv\Scripts\python.exe`，`-p no:cacheprovider`，测试数据 tmp_path。

| 实际命令（Python 前缀省略） | 结果 |
| --- | --- |
| 新增 F01/F02 初始反例 | 5 failed / 2 passed；明确重现坏 pending 解锁、无序单条扣 queue |
| 新增有 sequence 原生反例（修复前） | 2 failed / 2 passed / 14 deselected；同/分批各错误 fill 1 |
| `-m pytest -q tests/test_reliability_evidence.py -p no:cacheprovider --tb=short` | 最终新增测试 20 passed，2.71s，exit 0 |
| `-m pytest -q -p no:cacheprovider --tb=short --ignore=tests/test_fees.py --ignore=tests/test_market_supervisor.py --ignore=tests/test_wrh_backfill.py --ignore=tests/test_stream_daemons.py --ignore=tests/test_polymarket_status.py` | 最终诊断子集 501 passed，14.32s，exit 0；不是完整默认回归 |
| `-m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider --tb=short` | 10 passed，0.66s，exit 0，无 skip |
| `-m pytest -q -p no:cacheprovider -o faulthandler_timeout=15 --tb=short`，外层 60s | 无完整结果；test_fees.py:59→asyncio→socket._fallback_socketpair→accept 挂起；60s 超时，wrapper marker=TIMEOUT_60S，工具 exit 1；只终止持有的 pytest PID 38700 子树 |
| `uv lock --check --offline` | 权限提升后 exit 0，32 packages；最初沙箱 uv cache WinError 5，不算锁文件失败 |

完整套件的有界尝试发生在最后 F02 安全阻断补丁之前；因已知同一挂起不再无限重跑。
最终版本仍没有完整默认 suite result。沙箱首次 pytest 为 49 个 setup 错误，
原因是临时目录 WinError 5，非业务失败；申请限定测试命令权限后恢复运行。
本轮曾创建的空 `D:\poly\.pytest-stage2-f03-a01` 已确认空目录后非递归删除，
未删除任何测试证据文件。末轮 497 项历史检查点的一个会话句柄丢失，未将其计为新结果；
最终明确取得上述 501 项结果。

S01/S02/S03/S04/S05/S07/S10/S11/S12 的本阶段适用边界如上落实或具名保留，
不是全规范符合声明。F03 首次未落盘窗口、F02 正向完整组协议、F04/F05 残留，
F06/F08、Q01–Q08 全面审查、完整默认回归及独立复核仍待办。
没有操作正式 data、Paper CLI、daemon/Task Scheduler，没有外部 probe、凭据读取、
真实执行、commit/push；仅有本任务明确持有的超时测试进程终止。
未部署，未封板；正式 N=0/PnL=N/A 不变。
