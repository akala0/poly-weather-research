# 采集链只读核查与恢复设计：R01 / R02

日期：2026-09-10。HEAD `2c8d9b9d91504d579912985aad33ffd01bc5011a`。本轮没有执行恢复。本文件是可审查的设计，不是启动授权或可直接运行的脚本。

证据 ID 见 [机器索引](source_recovery_evidence_20260910.json)；来源能力见 [S01/S02 报告](source_capability_audit_20260910.md)。带时钟首轮状态为 02:55:27 UTC / 北京时间 10:55:27；最终标准状态复核与随后日志跟进的 as-of 分别在索引 R03、R04。

## R01：当前事实

现有原始天气链正在产生新记录；行情 supervisor 的 runner 在重试，但子进程未形成持续采集，观测到的失败输出跨尝试发生变化。signal/v2 shadow 未获得有效当前运行证据。四个 Task Scheduler 显示 Running 只证明任务层状态，不能当作四个业务服务正常。[R01、R02、R04、SAMPLE01、SAMPLE04]

| 组件 | 首轮 reported → effective | 完整性、心跳 UTC / 年龄 | PID 与归属 | 依赖、业务进度、last_error |
| --- | --- | --- | --- | --- |
| market | reconnecting → stopped | verified；09-04 11:45:44.670529 / 约 486583 秒 | 23304 dead；提升权限的目标 CIM 查询也未找到 | 原始 root，不依赖 Paper；business UNKNOWN；旧错误 ConnectionClosedError。当前 runner 的多次失败见下一段。[R01、R02、R04] |
| supervisor | running → stopped | verified；09-04 11:45:29.148221 / 约 486599 秒 | 同一 23304 dead | root；business UNKNOWN；旧 last_error=null 不代表现在无错。[R01、R02] |
| weather | running → unknown（受限权限首轮） | verified；09-10 02:55:27.993812 / 约 1 秒 | 17944；随后 CIM 确认 python.exe、weather-stream 命令匹配，创建于 09-09 10:04:13.498518 UTC | 不依赖 market；last_error=null；新 raw 可见追加，但缺 producer business_sample，正式 business_ready 仍 UNKNOWN/false。[R01、R02、SAMPLE03、SAMPLE04] |
| signal | running → stale（受限权限首轮） | verified；09-04 11:45:45.189532 / 约 486584 秒 | 6220 当前属于 InstallHelper.exe，创建于 09-09 10:04:02.105535 UTC；不匹配 signal，旧 PID 已复用 | 依赖 market/supervisor/weather；business UNKNOWN；旧 last_error=null。[R01、R02] |
| v2 shadow | running → stopped | verified；09-04 11:45:27.999428 / 约 486601 秒 | 8344 dead | 依赖前三个上游及 signal；business UNKNOWN；旧 halted=false/last_error=null 不构成恢复资格。[R01、R02] |

首轮受限权限查询的 unknown 不改写为服务死亡。补充提升权限查询只针对任务文件允许的 PID/项目任务，未导出其他进程命令或环境值；signal 的最终运行判断采用归属冲突证据。最终标准 CLI 复核见 R03。

最终复核（03:07:45 UTC / 北京时间 11:07:45）已在可查询归属的权限下完成，exit 0、stderr 为空、未超时：weather 为 running/alive，signal 为 ownership_mismatch/reused，market/supervisor/shadow 仍为 stopped/dead，五个 status 均 verified。weather 此时 last_error 已变为 `KSEA:nws: ConnectError:`，但最后业务事件为 03:07:05.870604 UTC、数据库 writer 报告最后提交为 03:07:05.887197 UTC；不能概括为零错误，也不能据此宣称整个 weather 停止。五个组件的 business_progress 仍 UNKNOWN，weather 缺 producer-owned 新契约 sample 的限制不变。[R03]

### 任务层与失败层

四个 `PolyWeather-{market-supervisor,weather-stream,signal-engine,shadow-spread-engine}` 均 enabled=true、Running，最后运行 2026-09-09 18:04:04+08:00，LastTaskResult=267009；触发类型为 Boot 和 Logon，重试策略 5 次 / PT1M。入口为 WindowsPowerShell 调用 `D:\poly\scripts\windows\poly-weather-daemon-runner.ps1`，参数指定各自 DaemonName、ProjectRoot D:\poly，工作目录 D:\poly。没有把仓库 XML 当实际已注册任务配置。[R02]

market runner 有 02:45—02:48 UTC 的多次 child started/退出记录，02:48:05 进入 900 秒 crash-loop pause。首轮保存的 58 字节 stderr 为 `No current events passed strict settlement verification.`；`cli.py:market_supervisor` 的该分支在构造 bot/supervisor 前退出 2。[SAMPLE01、L10]

后续快照显示 runner 又在 03:03:05—03:06:21 UTC 完成六次失败尝试并重新进入 900 秒 pause；此时 stderr 已被 runner 轮换/覆盖为 19643 字节 traceback，末尾是 `httpx.ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`。这证明至少一个后续尝试在 HTTP/SSL 传输层失败，也证明首轮 58 字节文件只能作为当时样本，不能冒充当前同一文件身份。runner 每次记录的 9008 都是 `WaitForExit` 后拿不到实际退出码时的本地替代值，不能说子进程真实退出 9008。[R04、L11]

**已确认的失败层仅能收敛为“持续行情采集建立前的启动/发现阶段”；没有单一、稳定且已证实的业务根因。** 早期样本落在 strict verification 拒绝路径，后续样本落在 HTTP/SSL 传输异常；不能把所有尝试归为没有候选，也不能据此改结算 registry、网络配置或放宽核验。启动前 discovery 只查当地 today，而循环中会查 today/tomorrow；这个差异是代码事实，不是本轮已证根因。[R04、L10]

signal/shadow runner 尾部记载 09-09 10:04:16 UTC dependency gate closed。它们目前仍是可自动唤起下游的等待任务。直接恢复 market 可能让其通过依赖门启动；“只运行一个 market 命令”并不自动实现授权隔离。[R02、SAMPLE01、L11]

### 原始记录、连续性与旧状态

| 选定证据 | 实际读取结果 | 解释边界 |
| --- | --- | --- |
| 09-04 WS 日文件尾 256 KiB | 最后完整行 receipt 11:45:20.811769 UTC，source_ms=1788522319613，run=8c77ab19…，sequence=305671，类型 new_market，book_complete=false | 这是最后看到的原始消息，不是最后合格 L2。首 4 MiB 同样只见 new_market；不推断全日没有 book。[SAMPLE01、SAMPLE02] |
| 09-03 checkpoint 文件尾 1 MiB | 最后行 receipt 12:00:44.414182 UTC，source_ms=1788436764506，sequence=285096，book_complete=true、upstream_status=normal | 是最后检查到的完整簿候选；未全查 token membership、全部质量窗或每个源 prefix，不称为全链最后已认证覆盖。09-04 checkpoint 路径不存在。[SAMPLE03] |
| 09-10 weather 文件两个尾部样本 | 02:57:29 读取大小 11068467、末行 sequence=8911；03:01:37 读取大小 11197382，保守选取的完整行 sequence=8929，receipt 03:01:05.981130；同一 run/file identity | 有真实 producer 原始输出追加，不能说只有心跳；缺新契约 business_sample，标准 readiness 仍 UNKNOWN。第二次诊断保守丢弃最后分片，因此这行不是当时精确文件末行。[SAMPLE01、SAMPLE04] |
| v2 cursor 全文件 3076370 字节 | 58 sources，58 个非零、58 个缺 prefix_sha256；含 plain/gzip 重复逻辑源与 skip_existing_gzip | 用首个现存 position 调现有读取函数立即得到 UNKNOWN_ARCHIVE_PREFIX，未读取/解压该大型 gzip。更多重复表示还可能引出 AMBIGUOUS_ARCHIVE_CURSOR。[SAMPLE03、L12] |
| v2 派生 status / ledger 尾 | status：3 active orders、21.35 shares、成本 $9.6075、3 fills、0 round trips；ledger 6013283 字节，只读尾 256 KiB | 不做全量账本重建，不把 status 当账户权威；这些非空经济状态已足以禁止丢弃旧 cursor 后 tail-bootstrap。[SAMPLE03、SAMPLE04] |

截至本轮未重新建立近期行情有效覆盖。09-03 checkpoint 之后是“本轮未验证覆盖的区间”，不能精确宣称从该秒起完全停机；09-04 的 new_market 也不能填补 L2。当前记录 PID 死亡与重试失败是当下证据，不可倒推所有历史时段均停机。深度缺口不能从 trades/历史价格回填，天气后补 QC 也不能升级成当时 realtime receipt。

首轮读取预算：固定几份 status/cursor（单文件上限 4 MiB）；raw 文件的首 4 MiB 或尾 256 KiB/1 MiB；三份小于 2 MiB 的旧 tape；8 份日志最多各 8 KiB。后续只对两份 market 日志追加有界跟进：runner 尾 80 行、stderr 尾 100 行，并计算当时小于 0.5 MiB 的全文件 SHA-256；没有全盘扫描、打开数据库、解压巨型活跃归档或获取业务 API。每个保存样本都有读取范围或全文件 SHA-256 与 size/mtime；同一次跟进前后元数据稳定。跨时点文件变化则拒绝同一身份比较。外部 writer 继续追加或 runner 轮换日志是正常竞态，不是本轮修改数据。[R04、SAMPLE01—SAMPLE04]

## R02：按实际入口拆分恢复

| 组件 | 已核实入口、读写路径与依赖 | 旧格式/ownership 与恢复副作用 | 未来选择 |
| --- | --- | --- | --- |
| market + supervisor | CLI `market-supervisor`，runner 同名分支。读 configs/settlements.json、公开 Gamma；写 WS/checkpoint、market_stream.duckdb、runtime market/supervisor/status/active-set/config update 及元数据归档 | 原始 bot 新 run 不消费 v2 cursor；runner 有 Global\PolyWeather.market-supervisor mutex。初始核验后才建 bot；supervisor 每轮自动 apply_market_retention | 可独立于 Paper，但当前不能用默认入口做“无 retention 的纯采集恢复”。需要副作用拆分和核验失败诊断。[L10、L11] |
| 原始 weather | CLI `weather-stream`，十站点及既有间隔；写 raw/weather_daemon、weather_stream.duckdb/WAL、runtime/weather_daemon_status、数据库恢复报告和 tmp/duckdb-weather | root；进程 run_id/sequence；runner mutex。启动 ensure_weather_database 会写恢复报告，损坏时自动建 recovered DB；research.duckdb 权重读取使用 warehouse | 当前在运行，维持，不提出为审查重启。若以后需要恢复，数据库自动重建必须另列授权，不可当纯启动无副作用。[L06、L11、L13] |
| signal | CLI `signal-engine --supervised`，读 signal_config_update、raw WS/weather、registry/校准；写 signal_state/status、raw/signal_snapshot、signal_stream.duckdb | 需上游健康及新业务证据；JsonlTail 用共享 prefix，但其 positions 是实例内状态，不应假称存在可直接迁移的独立 signal cursor 文件 | 独立准入与授权；先在副本核验新启动 bootstrap/未来可见性与 forecast UNKNOWN。不能靠放宽 business_ready 放行。[L07、L11、L12] |
| v2 shadow | runner 指定 raw/shadow_orders/shadow_orders_v2_token_scoped.jsonl、runtime 同名 status/cursor；supervised 读上游与 tape | 旧 schema1 外层、无 per-source schema2 哈希；3 active orders/非空库存，账本权威需完整重建。writer/ledger lock 与 runner mutex 需未来验证唯一 ownership | 保持未恢复；旧无 hash position 拒绝；先账本/cursor/reconciliation 副本设计，不得清空重启。[SAMPLE03、L12] |
| Paper | runner 的 ValidateSet 和显式 switch 存在 paper-spread-engine；没有默认将 shadow 换成 Paper | 独立 paper_spread_v1 账本/cursor/status/账户，正式来源仍不合格 | 不纳入任何本次或首轮采集恢复清单，禁止启动。[L05、L11] |

补充：`market-stream` 直接入口仍做事件核验并写正式 market 状态，没有提供天然独立命名空间或授权屏障；不能当作绕过严格核验、现有 runner 或下游级联的快捷方式。runner 自身每次启动 child 会轮换 stdout/stderr 并删除最老副本，因此恢复前需保全失败证据，不能把日志轮换隐去。[L10、L11]

### 推荐的最小实施顺序（需后续授权）

1. **先补一个按 child attempt 保全的启动诊断能力。** 每次尝试必须有独立 attempt ID、开始/结束时间、阶段（transport/discovery/filter/verification）、异常类型和不可变日志文件；发现成功时再输出站点/目标日、候选数、过滤/核验 reason、原始响应 digest 与 receipt。保留原失败门，不记录响应正文，不改规则或增加执行能力。源码范围应限 runner 日志保全、`cli.py:market_supervisor` / `market_supervisor.py:discover_event` 及针对性测试。它能区分本轮已经出现的 HTTP/SSL 异常与 strict verification 拒绝，优先级高于 forecast adapter。[R04、L10、L11]
2. **同时为首轮 raw-only 运维建立隔离条件。** 给 supervisor 明确禁用 retention 的采集模式/参数，或把 retention 从采集循环拆成独立明确授权的操作；这是待实现设计，不声称已有 `--no-retention`。为下游保持门建立可审查方案，不能恢复 market 后自动启动 signal/v2。可选为只暂停本来就在等待的两个下游任务/runner，但这仍需明确运维授权、先重查没有健康子进程，且不触碰 weather。单纯修改 runner 文件不改变已运行 PowerShell 的内存逻辑，不能视为立即生效。[L10、L11]
3. 在临时副本用已有响应 fixture 验证原因分流、严格核验仍拒绝、retention 函数没有调用、零下游启动；有界解析和源码测试不启动 collector/follower/engine。需要真实业务原因时，另申请最多 10 个站点 each today 的 Gamma discovery 小请求及最多 2 个选定 event 详情，总计最多 12 次、15 秒/次、1 MiB/次、无重试，临时目录输出。先核实 adapter 实际 URL，授权包列具体 endpoint 后才请求；本轮未执行，网络根因仍 UNKNOWN。
4. 源响应原因明确、严格核验通过且 raw-only 路径在副本验收后，单独申请 market/supervisor 运维恢复。维持 weather 当前进程和频率。新行情 run 与断点明示，预先登记 source/run/schema/code fingerprint、订阅集和最初 token-native full books。停止条件不自动停止其他健康采集。
5. signal/v2 分别满足自身状态与证据条件后再授权。forecast Single Runs 样本验证属于独立来源任务；不为推进 raw collection 等待它，也不把它当 Paper 启动许可。

“不需要改代码”的范围：当前 weather 继续采集、只读状态查询和原始文件核对不需要改代码。当前默认 market-supervisor 的自动 retention 与无细分启动失败原因，则不能把恢复包装为已经验证的零代码一键操作。

### 三种 cursor 方案逐一判定

| 方案 | 必须具备的证据 | 当前决定 |
| --- | --- | --- |
| 兼容恢复已证 prefix | schema2、logical identity、representation identity、解压 offset、newline line count、确认前缀 SHA-256；plain/gzip 内容等价、文件稳定、所有已消费 source 仍可定位 | 原理可行，现有测试是小规模历史证据；本次旧 cursor 不满足。副本先测实际规模，不能逐周期重哈希巨型文件后宣称性能合格。[L12、H01] |
| 旧非零无 hash cursor | 独立历史证据必须证明当时消费的完整前缀；今天 hash 只证明今天文件 | 58 个位置均缺 hash；严格拒绝，不设计“补 hash 后恢复”。同时必须处理 plain/gzip 双键歧义。[SAMPLE03、L12] |
| 新 run/命名空间/明确断点 | 原始采集无经济状态、唯一 writer、独立数据边界、旧证据保全、下游不会误读混合命名空间 | 原始 market 可作为候选；不能用新命名空间丢弃 v2 的库存、订单、reservation、pending、去重、未解决 intent。[L11、L12、SAMPLE04] |

v2 下游经济状态处置前提：完整逐行验证 ledger 与 schema/portfolio identity；重建库存/成本/累计买入、活动订单与 queue ahead、退出阶段、reservation、consumed identities、pending 与未决 intent；和 checkpoint/cursor 做可唯一解释的对账。账本冲突或已消费前缀无法证明就保持 HALT/不运行。缺 token-native 新鲜 bid 的仓位保持未定价，不补造退出或收益。旧 v1、v2、Paper、challenger 不混账。

本轮 ledger 只读尾部，尚未满足这套恢复前提。不能把 status 的 0 discrepancy 当成完整 reconciliation。新 raw boundary 不会追认旧经济效果，也不追认旧正式前向样本。

### 分离授权清单

| 授权包 | 精确目标与预期变化 | 明确不包含 |
| --- | --- | --- |
| 代码 A | startup discovery 失败原因保全；supervisor retention 隔离；副本针对性验证。待另行给实现范围 | 不改 registry/策略阈值；不操作正式任务/数据 |
| 数据 probe B | 上述有界公共 Gamma 请求，或 S01/S02 报告的独立小样本请求；各自额度与临时路径另登记 | 不批量采集/WS/后台订阅，不凭结果直接恢复 |
| 运维 C | 仅 market/supervisor 的采集恢复；必要时显式暂停等待中的 signal/shadow runner 防止级联；事前保全对应 runner logs，记录断点 | 不停止/重启 weather；不运行 retention；不启动 signal/shadow/Paper |
| 下游 D | signal 与 v2 分开；前者验证 forecast/readiness，后者完成旧状态/ledger/cursor 唯一恢复 | 不清空经济状态，不混账，不启动 Paper |

路径范围以现有表格为准；新增采集命名空间的实际路径须未来实现后核实，不能给包含占位路径的“可直接运行”脚本。本文件不要求现在批准上述全部操作。

### 未来规模与长时验收

先在临时规模副本测量，再声明容量阈值，未测一律 UNMEASURED，不写 PASS。现有 prefix reader 会重读确认前缀并将剩余 tail 一次读入内存；大 backlog、gzip 恢复和热文件竞态是明确测量对象，不是只有理论上的“小文件测试可推广”。[L12]

| 指标 | 未来记录口径 | 阈值制定依据 |
| --- | --- | --- |
| 输入/处理速率、backlog | 每个 source 的记录/字节、durable frontier 差值；排除 heartbeat 次数 | 按实测峰值输入保留处理余量；连续窗口 backlog 不应无界增长，数值先测再定 |
| p95/p99 周期与发布延迟 | source→receipt、receipt→fsync/publication、消费周期分别记录 | 保持现有 WS/观测频率；以 120 秒 NOAA/WRH、900 秒 METAR、3600 秒 TAF、10800 秒 forecast 与组件 freshness 窗口为预算约束，不能靠降频通过 |
| prefix/gzip | 每周期已确认前缀重哈希字节数、解压/恢复耗时、plain/gzip 等价成本 | 用实际历史位置大小/冷缓存/热追加副本测；既有数十 GiB 位置不能按小 fixture 延迟估计 |
| 内存/fsync | RSS、增长斜率、队列峰值、fsync p95/p99、短写/磁盘满结果 | 先登记可用内存/磁盘和写入预算；不以 `GetProcessMemoryInfo unavailable` 当零内存 |
| useful progress | 至少两次真实 producer-owned committed positions 与原始首次 receipt 对照；心跳单列 | 缺 business_sample 或 continuity UNKNOWN 不授 business_ready；已追加 raw 可独立记事实，不伪造 sample |

针对历史数小时后复发的 HTTP pool 问题，未来观察应超过既知约 5 小时尺度；建议预声明至少 12 小时，完整覆盖多轮 3 小时 forecast 更新，并按首次异常/自愈/复发分别记录。12 小时通过也只证明此窗口，不宣称根因修复。当前 weather 已运行较长时间不是本轮完整监测实验。

停止/回退条件：唯一 writer/命令归属失败、未经授权的 retention/级联、原始写入失败、prefix/identity 冲突、membership/规则不符、经济对账不能唯一解释、资源越过预声明预算。只在未来授权目标内止损；保留已产生新证据与失败快照，回退代码不删除数据，不自动终止其他健康服务。行情失败窗口明确 quality exclusion，不能用回填“补平”。

## 本轮交付验收与边界

- R01/S01/S02/R02 均有实际证据或具体 UNKNOWN；来源保证、样本观察、本地逻辑与模型资格分开。
- 开始工作树为 `?? .claude/` 与 `?? CODEX_SOURCE_CAPABILITY_AND_COLLECTION_RECOVERY_TASK.md`；保留不动。Git 初次受 ownership 检查拒绝，后续仅以每条命令 `-c safe.directory=D:/poly` 只读查询，没有改全局配置。[G01]
- `git diff --check`、最终 Git/data 状态与未跟踪交付物的独立格式/链接检查结果见 G02。G02 的跨时点对照明确记录两处变化：market stderr 被 runner 轮换/覆盖，weather status 被健康外部 writer 更新；两者均不用于跨读一致性证明。Git 对 ignored data 的空输出不等于全盘数据指纹审计；限定输入的稳定性以每次读数为准。
- 741 passed 是既有独立记录；本轮只运行状态查询、已有 parser 的有界诊断及文档验证，没有重跑 pytest，不把诊断当新回归。[H01]
- 本轮未实施、未部署、未启动 Paper，未操作 daemon/任务计划、未跑 retention、未修改正式 data 或系统网络，未 commit/push。尚不能恢复旧 cursor，当前来源也未取得正式 queue 资格。**NOT SEALED。**
