# 项目级可靠性审查与分阶段整改任务

审查日期：2026-09-08。状态：待实施、待独立复核，**未封板，禁止启动 Paper**。

配套规范：`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`。

本文件同时承载本次审查记录与后续任务，避免另造一份容易失同步的结论。它是前述 Paper 任务的项目级补充，不覆盖历史证据，不把写任务等同于执行修复或批准运维。

## 1. 结论与审查范围

不是“所有模块都坏了”。已有 Decimal 账户、intent/commit 恢复、执行隔离、天气 provenance 闸门、同 token 深度限制及相当数量回归测试值得保留。当前主要问题是：**单个函数的保护没有贯穿采集、转换、匹配、持久化、恢复和报告全链路**。

本轮为风险驱动的项目级抽查，覆盖：

| 层 | 实际检查 | 结论边界 |
| --- | --- | --- |
| 公共成交 | adapter、collector、tape loader、WS 匹配、Paper 消费 | 有可复现跨层缺陷，见 F01–F04 |
| Paper 会计/恢复 | ledger reconciliation 与成交状态边界抽查、现有专项测试 | 保留已有修复；不声称所有崩溃组合已穷尽 |
| 归档治理 | 新压缩/已有 gzip 分支、archive reader、临时并发写故障 | 新压缩存在数据丢失反例 F05；未对正式归档执行 retention |
| 运行安全 | read_status、stream-status、runner 闸门入口 | 缺失/未来心跳和 reconnecting 状态存在问题 F06 |
| 天气/研究 | realtime/backfill provenance、weather join、signal calibration/health 入口 | 见 Q01–Q04 待系统验证，不能由抽查断言预测模型正确 |
| 执行隔离 | 搜索 import/客户端/POST/DELETE 调用与可选依赖 | 未发现新的真实下单调用；CLOB POST 是公开批量历史/报价读取，不能误报成下单 |
| 测试/文档 | 诊断套件、lint/lock、当前结论入口和交接记录 | 全套仍未验收，结论文档存在时效漂移 |

未做：逐行穷尽全部研究算法、线上请求、正式历史数据重评分、归档全内容哈希、系统网络根因定位、运行进程接管、许可证法律合规认证。以下区分“实测缺陷”“代码待验证风险”“环境/运维阻塞”，不得混称全部已证明。

## 2. 本次验证证据

### 已执行

- 诊断测试：`447 passed in 13.23s`，排除了下列五个文件，因此不是完整默认套件：
  - `tests/test_fees.py`
  - `tests/test_market_supervisor.py`
  - `tests/test_wrh_backfill.py`
  - `tests/test_stream_daemons.py`
  - `tests/test_polymarket_status.py`
- `.venv\Scripts\python.exe -m ruff check src tests`：通过。
- `uv lock --check`：通过；未同步或安装依赖。
- `git diff --check`：通过，仅换行提示。
- 本文 F03/F04/F05/F06 使用系统临时目录、fake client/故障注入复现，未请求外部服务。
- F01/F02 来自本会话上一轮独立复核的真实 follower 临时两轮/重启复现；本轮磁盘代码仍保留相应路径。此前定向＋Nautilus 179 项通过不证明它们已关闭。

完整 pytest 的 socketpair 挂起沿用前轮实际堆栈证据，本轮未重复运行已知阻塞；不能写成“本轮完整测试失败了某个业务断言”或“完整测试通过”。

### 当前运行与正式证据

只读 stream-status：market=`reconnecting`，supervisor/weather/signal/shadow=`stopped`；五个组件记录的 PID 均不存活，完整性字段 verified；心跳仍为 2026-09-04 11:45 UTC 左右。不得把旧状态文件或旧 PID 当当前健康采集证明，也不据此推断具体停机责任或全部正式归档的缺口范围。

`git status --short -- data`、`git diff --stat -- data` 均为空；ignored/hidden-inclusive 的 `*paper_spread_v1*` 文件名搜索无匹配。这是 Git 可见零变更与指定文件不存在的证据，不是所有 ignored 归档的字节审计。

正式 Paper N=0、PnL=N/A。未启动 Paper，未操作 daemon/Task Scheduler，未修系统网络，未 commit/push。

## 3. 已确认问题清单

P1 表示可污染证据/成交或损害数据，应阻止相关路径验收；P2 表示误导运行判定或交接。没有实测正式损失时不得把注入反例写成真实事故。

### F01 / P1：pending 恢复抹掉原始质量状态

- 路径：`paper_spread_runtime.py::_ws_trade_evidence`、`_pending_ws_row`（审查时约 954、1046–1054 行）。
- 原始质量未完整持久化，恢复硬编码 `upstream_status="normal"`。
- 实测：第一轮 degraded WS 被拒绝，fill=0/pending=1；第二轮用原生 follower 恢复后 fill=1/pending=0。
- 关闭要求：保留完整质量与来源事实，正常匹配和 pending 共用同一判定；旧字段缺失不得默认为健康。验证无需新 WS 行也不能洗白；质量变好不等于旧坏证据变好。

### F02 / P1：同秒歧义取决于 poll/batch 切分

- 路径：`paper_spread_runtime.py::process_trades`（约 1748–1760 行）。
- 先移除已消费事件，再对本批剩余事件做同秒 sequence 检查，历史组成员被排除。
- 实测：同 token 同秒、无 sequence 的 tx-A=50 shares、tx-B=51 shares，初始 queue=100。同批输入为 UNKNOWN、fill=0；分两轮且重启后为 fill=1、UNKNOWN_TRADE_SEQUENCE=0。
- 关闭要求：持久化 token/时间组及排序/完整性证据；不能靠本轮只有一条就认定可排序。明确事件迟到、组未封闭、补证据与已提交前缀的政策；没有可证明的完整性边界时宁可 UNKNOWN，不按经验等待时长猜测“已到齐”。
- 不允许用累计计数掩盖已经错误消费的队列。若较晚证据使既有模型结果不再可评分，须持久标记受影响结果失效/隔离，不改写原账本，更不能继续发布干净成绩。

### F03 / P1：公共成交首次 receipt 未冻结且可能早于真实响应

- 路径：`public_trade_collection.py::collect_depth_event_trades`（约 244、294 行）、`_merge_trades`；`trade_tape_analysis.py::load_event_trade_tapes` 的文件级 fetched_at 回退。
- 实测：相同 coverage 两次调用，fake client 请求次数始终为 1；同一条成交读出的 available_at 却从 01:00 UTC 变为 02:00 UTC。
- 代码还在请求前记录整次采集的 fetched_at，再将其用于所有事件；它不能证明后续 HTTP 响应在这个时刻已收到。
- 关闭要求：响应完成后捕获每条首次本地可见时间，并在去重、刷新、无请求周期、失败重试中保持不变；区分 request_started_at、response_received_at、first_seen_at、file_written_at。传入 now 不能同时充当所有阶段时钟，使用可注入时钟并保留兼容说明。
- 老数据没有可靠 receipt 时显式 unknown/historical-only，不得拿当前写文件时间、mtime 或事件时间补成正式前向证据。不得批量改正式历史档案。

### F04 / P1：损坏 tape 被当空文件并覆盖

- 路径：`public_trade_collection.py::_read_payload`（约 184 行）、`load_trade_cursor`、collector 写入路径。
- 实测：临时 `event-1.json` 为 `{broken-old-evidence`，采集函数没有拒绝，而是用 fake 返回的新交易覆盖成合法 JSON，旧字节不保留。
- 关闭要求：区分 missing、verified-empty、corrupt、unreadable、recoverable；corrupt 不得返回空字典继续写。保留原字节和诊断，冻结该对象 watermark，避免因单事件失败无依据污染其他对象。
- 复用 runtime_safety 的 durable-write 能力，但不要未经兼容设计给旧消费方硬加 checksum/schema；约定 tape/cursor 的提交顺序、恢复事实和唯一 writer。
- 当前 collector 自写 replace helper 无显式 fsync，也是本项需要补验的 durability 风险，不得把 rename 原子性宣传为整个事务原子性。

### F05 / P1：新 gzip 分支在源变化后仍删除源文件

- 路径：`retention.py::apply_market_retention`（约 168–173 行）。已有 gzip 的校验分支较强，但新 gzip 路径直接 copy→replace→unlink，未复验。
- 实测故障注入：源 `old\n`；复制完成后、返回前追加 `late\n`；最终源不存在，gzip 解压只剩 `old\n`。这是临时反例，不是正式数据已丢失的结论。
- 关闭要求：只对可证明封存且不再写入的源操作；新增与已有 gzip 都验证解压字节数/SHA-256、源稳定性和文件身份；校验到删除之间仍需 ownership/封存协议或等效排他保证，不能仅增加一次 stat 留下同类竞争窗口。
- crash、ENOSPC、短写、压缩损坏、晚到写入、目标已存在、并发 retention、重解析点/路径逃逸均需测试。无唯一安全结论时保留源且报告冲突。
- 不允许通过停止行情采集解决并发，不改变保留周期；实际正式压缩/删除另需授权。

### F06 / P2：状态判定对心跳与活动状态不完整

- 路径：`runtime_safety.py::read_status`、`_LIVE_STATES`（约 28、338–373 行）。
- 实测：临时 verified 状态＋本测试自身存活 PID，缺失 heartbeat 仍返回 running/age=None；未来一天 heartbeat 返回 running/age=0。
- 现有 reconnecting 不在活动状态集合，因此真实已死 market PID 仍显示 reconnecting，其他组件显示 stopped。PID 布尔字段虽正确，顶层语义不一致，易被调用方误用。
- 关闭要求：声明状态枚举、缺失/坏格式/未来/过期 heartbeat、PID 缺失/死亡/重用/归属不符的组合真值表；不得将未知或未来时间夹为新鲜。合法时钟容差预声明，不按结果调参。
- verified 只代表完整性；liveness、freshness、ownership、依赖健康独立显示。统一 CLI/runner/signal/readiness 的健康谓词并回归，不直接改系统任务。

### F07 / 验收阻塞：完整默认套件未完成

socketpair/asyncio 创建卡住已有最小复现和堆栈，但没有具体过滤组件根因。35 项所在的五个文件被诊断子集排除；447 项不能替代它们。修复系统环境是独立维护任务，未经授权不可开展。本任务可准备超时隔离的验收器与测试证据，不得 monkeypatch 掉真实业务路径或 xfail 隐藏挂起。

### F08 / P2：当前结论入口与现实状态漂移

`CURRENT_CONCLUSIONS.md` 标为唯一入口，但生成时间为 2026-09-01，仍包含“当前状态正常”及旧 PID；AGENTS/交接还有旧测试数，Paper 新口径和历史 v2 预算说明也必须区分。

关闭要求：文档标注 as-of、版本、来源、有效范围，历史运行验收改为历史陈述；只引用实时健康命令，不复制长寿 PID 为当前事实。更新工程状态不得偷偷改研究结论、阈值或已有 N/A；正式 Paper $200 是全账户共享，station-day cap 是附加累计成本限制，不沿用旧 v2 口径混写。

## 4. 需要扩大检查、尚不算已确认缺陷的项目

| ID | 待核验问题 | 如何证明 |
| --- | --- | --- |
| Q01 | archive_io 同时枚举 JSONL/gzip；重复表示是否被所有消费方正确处理 | 等价双份、冲突双份、压缩切换与 cursor 重启；不靠文件名假定等价 |
| Q02 | weather provenance 对旧缺失 collection_mode 默认 realtime 的范围 | 版本化旧数据适配边界和生产入口测试；不能把未知新记录自动归为实时 |
| Q03 | receipt/质量窗口在 API-only、WS、pending、replay 是否一致 | 同一证据走各入口比较；质量跨区间、迟到、无 receipt、future 必须具名拒绝 |
| Q04 | calibration/walk-forward、信息时钟、QUIET/complement 是否有转换层前视 | 固定 vintage＋未来数据追加不改变过去决策，按 station-day/季节版本统计；不能用最终天气参与当时决策 |
| Q05 | fee 配置/取整在 Paper、shadow、challenger 是否同口径 | 固定版本数学测试和 native token 校验；线上费率更新不在本轮授权中 |
| Q06 | 多文件提交、账户/策略/checkpoint/cursor 是否恢复等价 | 每个 append/fsync/replace 边界前后进程崩溃；state fingerprint 比较 |
| Q07 | 大量全量扫描/大模块是否妨碍 lifecycle 与恢复 | 固定规模夹具的耗时、内存、poll 延迟；未测量不以行数为理由大重构 |
| Q08 | 开源来源/历史 revision、可选依赖加载是否可审计 | 真实本地历史与许可证清单；找不到历史 hash 则记录未知，不猜造 |

这些是检查任务，不是先验要求全部重写。找到缺陷先加入清单、写反例，未找到则记录检查范围和限制。

## 5. 分阶段执行计划

### 阶段 0：冻结边界与基线

完整阅读 AGENTS、原任务、配套规范；保存 Git 状态，记录相关 tracked/untracked 文件哈希、测试环境/依赖版本，避免无 commit 状态下无法辨认候选版本。不得读取凭据。

建立接口责任表：原始采集者负责首次 receipt；统一规范化负责身份表示；匹配器负责证据资格；ledger 负责经济权威；cursor 只负责已确认输入位置；status 只描述不能凭空授权。

### 阶段 1：防止证据破坏（F04、F05）

先为损坏覆盖和并发追加丢行建立红测，再实现存储契约。retention 仅在临时目录运行。以“旧字节仍可恢复、cursor 未越界、未中断写入者”为验收，不以磁盘空间节省为验收。

### 阶段 2：恢复端到端证据语义（F03 → F01 → F02）

先冻结采集 receipt，再保证质量恢复，再解决跨 poll 组完整性。沿用已修复的 Decimal/cross-source dedupe，不退回旧实现。所有输入分批、别名和重启组合需走原生 follower。

### 阶段 3：真实健康与文档治理（F06、F08）

实现单一健康契约，测试顶层状态与详细字段一致；修订历史与当前边界，统一测试/报告验收措辞。源码修改不等于已部署到常驻进程。

### 阶段 4：横向回归（Q01–Q08）

按上游→消费者检查，尤其 shared shadow/QUIET/complement，不只跑 Paper。每个新增风险需明确 blast radius；除必要修复不作全项目拆分、框架迁移或策略重做。

### 阶段 5：独立验收与后续维护分流

代码分支、回归和证据包达到要求后交独立复核。环境 F07、恢复采集、正式数据治理分别列“另需授权”，不能混进本代码任务。不要提前宣称正式启动资格。

每阶段交付：修改范围、红测→绿测、生产路径证据、共享模块回归、未关闭项。允许安全独立检查并行，禁止多个任务同时改同一文件。

## 6. 最低验收矩阵

| 编号 | 必测场景 |
| --- | --- |
| R01 | degraded/maintenance/unknown WS → pending → 两轮 → restart，不变健康，不产生争议成交 |
| R02 | 同秒无序交易同批、分批、跨文件、重启，组歧义不可因已消费去重而消失 |
| R03 | 请求前/响应后时间不同；首次 receipt 不早于响应，不晚借文件刷新改写 |
| R04 | 无请求 refresh、重复返回、延迟新 sibling、重试，旧 receipt 保持且新 receipt 有来源 |
| R05 | 缺失/空/坏 JSON/截尾/不可读 tape 与 cursor 明确区分，损坏原字节不覆盖 |
| R06 | collector 每个文件提交边界中断，watermark 不跳过、成功前缀重放幂等 |
| R07 | 新/已有 gzip 均内容校验；复制时与校验后晚到追加，保留全部源数据 |
| R08 | 压缩短写/ENOSPC/损坏/并发/路径逃逸；不删唯一可恢复副本 |
| R09 | plain/gzip 表示切换与 reader/cursor 兼容，等价与冲突分开处理 |
| R10 | 心跳缺失/未来/格式坏/过期、PID 死亡/缺失/重用、reconnecting 的真值表 |
| R11 | API/WS/pending/replay 同样数据相同 eligibility、receipt 与质量边界 |
| R12 | old schema、legacy v2、Paper 新身份不串账、不迁移正式历史、不失忆 |
| R13 | reserve/queue-only/partial/full/release/risk-sell 写失败、重启、重复回放的全状态等价 |
| R14 | weather 新旧 schema/provenance 与未来追加测试；无凭据、无真实执行、无代理成交价 |
| R15 | 文档中的每个当前结论有 as-of/来源；fixture PASS 不等于 production PASS |

不允许只 monkeypatch 被审查的生产函数返回成功；允许可控时钟、fake 外部响应、故障注入和 tmp_path。至少为 F01–F05 各有生产入口整链路测试，而非只有 helper 单测。

## 7. 验证与操作边界

使用 `.venv\Scripts\python.exe`；不自动同步主环境，不安装新依赖。默认与可选环境分开报告。

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -o faulthandler_timeout=15
.venv\Scripts\python.exe -m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider
.venv\Scripts\python.exe -m ruff check src tests
uv lock --check
git diff --check
```

完整测试加外层有界超时（当前建议 60 秒）；只终止本任务创建并确认身份的测试进程及其子进程，禁止按名字批量杀 Python。已知挂起不可无限重试。忽略五个异步相关文件的447项仅作诊断；新测试数量变化需实时核验。

禁止：Paper CLI 启动、daemon/Task Scheduler 操作、正式 data 写入/压缩/删除/迁移、系统网络修复、凭据读取、真实执行、外部 probe、commit/push。不得修改保留天数、采集频率或策略参数“让流程跑通”。所有实际运维另行授权。

保持脏工作树；修改前重新读取。不得 reset/clean/stash/覆盖其他任务改动；未跟踪实现也属于用户已有工作。文档更新不改变 AGENTS 原有授权边界。

## 8. 最终交付与状态判定

提交（不执行 Git commit）下列工作成果：

1. F01–F08、Q01–Q08 逐项“修复/验证/待办/另需授权”，代码路径与真实测试结果。
2. 数据身份、receipt、质量、组完整性、存储与恢复协议；遵循配套规范的符合性表。
3. R01–R15 测试清单，完整命令/退出码/超时、未运行及 skip 的原因。
4. 当前工作树内容指纹、依赖与 schema/version，测试对应的准确候选版本。
5. 数据与运行安全声明；历史文档仅更新工程状态，不改正式研究证据。
6. 独立复核待办、环境与采集维护单独列项；没有线上运行证据则依旧 N=0/PnL=N/A。

只有全部相关缺陷关闭、完整默认与可选回归满足要求、未知项有明确处置且独立复核通过，才可提出“技术封板候选”。不能把文档已写、定向全绿、系统环境恢复中的任一项当作整体封板。

**本项目仍未获得 Paper 启动授权，更没有真实执行授权。**

## 9. 执行记录入口（2026-09-08）

阶段基线、存储契约、实际红绿测试与残留项见
[可靠性整改执行记录](docs/reliability_remediation_status.md)。
当前仅为阶段 1 检查点，不改变本文件审查发现，不代表全部整改或封板完成。
