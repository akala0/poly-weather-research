# Codex 任务：来源能力核查与采集链恢复设计

日期：2026-09-10。状态：待执行；本文件不是调查结果或启动授权。

本轮只做两件事：确定成交/天气来源能否满足现有证据要求；只读核查采集链并提交可审查的恢复方案。允许形成报告，不修改产品实现、不执行恢复、不启动 Paper。

## 1. 目标与阶段边界

已发布候选：`market-state-challenger`，发布 HEAD 为 `2c8d9b9d91504d579912985aad33ffd01bc5011a`。开始时重新记录实际 HEAD 与工作树，不假定仍无漂移。

既有独立证据：`docs/reliability_independent_full_validation_20260909.json` 记录完整回归 `741 passed in 34.44s`，无跳过/排除，测试前后 220 个候选文件指纹一致。这是当时环境与候选的验证，不是本轮重新运行或全部代码独立审查通过。历史 socket 超时根因仍未证实，不继续将其列为当前完整回归阻塞，也不宣称已修复系统。

当前基线：

- 本地可靠性整改候选已交付，未部署；正式运行前验收尚未完成。
- 公共成交组完整性为 `UNSUPPORTED_GROUP_COMPLETENESS`，公共入口零 queue consumption。
- 真实 forecast 固定初始化证据有缺口；合成 vintage 仅证明消费者契约。
- 旧非零无前缀哈希 cursor 禁止直接恢复。
- 前次采集 PID 未存活是历史快照，本轮必须重新核查，不能照抄为当前状态。
- `NOT SEALED`；不启动 Paper；正式 N=0、PnL=N/A 是既有报告口径。本轮若未覆盖正式文件检查，须注明沿用，不假称新全量审计。

本轮完成条件是交付有依据的能力判定和恢复决策材料，不是强行让所有来源变成可用。证实当前证据不足可以完成调查，不能完成正式 Paper 封板。

## 2. 必读依据

完整阅读 `AGENTS.md`、`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`，再按下列入口查阅当前内容：

- `CURRENT_CONCLUSIONS.md`、`HANDOFF.md`、`README.md` 的状态与命令部分。
- `docs/reliability_evidence_closure_report_20260909.md` 的当前结论，区分历史检查点。
- `docs/reliability_independent_full_validation_20260909.json`。
- `docs/reliability_trade_source_semantics.md`、`docs/reliability_receipt_contract.md`、`docs/reliability_materialization_contract.md`。
- `docs/reliability_weather_qualification.md`、`docs/reliability_archive_cursor_contract.md`。
- `docs/reliability_health_contract.md`、`docs/reliability_consumer_audit.md`。
- `docs/polymarket_api_reference.md`、`docs/strategy_reference_survey.md`、`THIRD_PARTY_NOTICES.md` 的相关来源条目。

旧文档的状态、费率、上游字段和接口能力不能直接当作最新事实。冲突须列出时间、适用层和权威来源，不擅自修改规则使两者看似一致。

## 3. 权限白名单与绝对禁区

### 3.1 本轮允许

- 读取本仓库源码、配置、测试、既有报告及必要的正式状态/归档证据。
- 查询当前相关 PID 的存活、启动时间和命令归属；只读查询项目任务计划的配置与最后运行信息。
- 读取官方公开文档、官方公开源码及现有参考项目源码/许可证，用于核实接口契约。联网不可用时记录限制，不更改系统网络。
- 在全新临时目录中分析必要的有界证据副本或内存样本；只验证已有读取/解析行为，不开发适配器，不运行 collector/follower/engine。
- 仅创建第 8 节指定的调查报告、恢复方案和证据索引。临时分析脚本不进入产品代码。

### 3.2 本轮禁止

- 不修改 `src/`、`tests/`、`configs/`、`scripts/`、依赖/锁文件、策略、校准或现有 runner。
- 不启动 Paper，包括 smoke、once、replay 等 engine 命令；不启动、停止、重启、接管或重装 daemon/Task Scheduler。
- 不修改正式 `data/`；不运行 retention，不压缩/删除/迁移/回填归档，不修改 cursor/status/ledger，不解除文件锁。
- 不直接探测业务数据 API，不新建 Market/User WebSocket，不启动后台订阅或批量采集；如确需小规模免鉴权数据请求，单独提交端点、请求上限、写入路径和理由，等待授权。
- 不读取 `.env`、凭据存储、钱包/私钥/API key/passphrase；不构造认证执行客户端，不使用签名、Relayer、订单 API。
- 不修改网络、代理、防火墙、注册表、系统权限或 Python 安装；不安装/升级/同步依赖。
- 不 commit/push、切换/重置/清理/stash 工作树，不清理 `.claude/`。上轮 GitHub 发布授权不延伸为本轮自动发布。
- 不把本地 group flag、采集 sequence、文件末尾或等待时间包装成上游完整性证明；不开放私有 `MODEL_KERNEL` 作为正式旁路。

所有操作先核对调用链的副作用。状态查询只允许确认不触发 repair/写文件的入口。数据库若不能保证只读且不创建 WAL/锁等副作用，跳过该路径，不为了查询打开可写连接。

## 4. R01：先做采集链当前状态快照

此步优先执行，不必等待来源文档研究结束。原始深度缺口不可重建；若确认仍在断流，尽早报告事实与需授权事项，但绝不擅自恢复。

1. 记录 UTC 和北京时间、HEAD、工作树状态、实际解释器路径。使用现有 `.venv`，不用会隐式同步依赖的命令。
2. 阅读 `stream-status` 实现确认只读后，优先使用 `.venv\Scripts\python.exe -m poly_weather stream-status`。有界执行，保存退出码、超时和 stderr，不把异常当成服务停止。
3. 对 market、supervisor、weather、signal、v2 shadow 分别列出：reported/effective state、integrity、heartbeat 时间/年龄、PID liveness、命令/run ownership、依赖、业务进度、last_error。
4. 复用现有 Windows liveness 方法；不使用 `os.kill(pid, 0)` 推断 Windows 进程状态。只查询目标 PID，不导出其他进程的完整命令或环境变量。
5. 项目 Task Scheduler 仅查询匹配任务的 enabled/state、入口路径、参数、工作目录、触发/重启策略、last result。存在敏感参数时脱敏，不输出凭据。
6. 状态文件完整性不证明采集在前进。若要判断 advancing/stalled，须有两个真实可比较的 producer-owned 样本；否则标 UNKNOWN。未收到新消息不能直接判掉线，也不能直接判 quiet。
7. 对必要归档检查最后可验证记录及来源时间/receipt、source/run identity、cursor 位置、表示形式和文件稳定性。限定文件/字节/时长预算；不全盘扫描或解压巨型活跃归档。
8. 文件读取期间若变动，记录竞态并停止使用该样本作一致性证明；不得停采集换取稳定性。mtime 只作文件元数据，不充当 receipt 或成交完整性证明。

给出最后已知合格证据与当前观察时刻，区分“无已验证覆盖区间”与“已证实完全停机区间”，不臆造精确 outage 起点或责任归因。

## 5. S01：公共成交来源能力判定

先定义需求，再查官方契约、真实本地原始样本和生产转换链：

`官方 wire → adapter → 首次 receipt/journal → tape 物化 → validator → pending/restart → Paper 公共入口`。

重点阅读实际 `adapters/polymarket_data.py`、`market_trade_tape.py`、`public_trade_collection.py`、`receipt_journal.py`、`shadow_runtime.py`、`paper_spread_runtime.py` 及相关调用方。函数名、字段和行号以当前源码为准。

### 5.1 必须逐项回答

| 能力 | 需要区分的证据 |
| --- | --- |
| 单笔身份 | trade/fill identity 与 transaction hash、participant、同交易 sibling 的关系；跨 WS/API 别名是否可唯一识别 |
| 事件排序 | exchange sequence 与本地 sequence；作用域、重连重置、同秒排序、补序号与矛盾序号 |
| 完整集合 | pagination snapshot、稳定排序、边界包含性、截断上限、迟到/修正机制、源端 watermark 或等价闭合证明 |
| 时间可见性 | source time、逐页 receipt、整次响应 receipt、首次 durable visibility、组证明何时才可见 |
| 质量与恢复 | gap、reconnect、重复、乱序、成员删减、API/WS 冲突如何影响资格 |

每行须标明：官方保证、样本观察、本地实现假设、尚未知；样本中有字段不等于官方保证字段稳定语义。

必须分别回答三层问题，不能混为一个 PASS：

1. 本地 durable receipt 是否完整物化？
2. WS/API 是否可唯一交叉核验某条成交？
3. 对所需 token/时间组，源端全部成员与顺序是否已可证明？

短页/空页、轮询结束、文件结束、固定等待、后续时间戳、无重连、HTTP 200、transaction hash 均不能自动回答第 3 项。

### 5.2 可用结论与停止条件

- **本地适配缺失**：官方契约和可核验原始样本支持，转换层丢失；列出具体字段/位置、最小适配设计和未来正反测试，不实施。
- **文档支持但尚无样本验证**：列为待验证能力；明确最小额外数据请求计划，不给生产资格。
- **现有来源证据不足**：继续 `UNSUPPORTED_GROUP_COMPLETENESS`，说明缺哪一条保证，不能把“未找到保证”说成“全世界不存在”。
- **替代来源候选**：仅评估公开、合法且免凭据的文档/源码契约；列许可、成本/依赖、语义差异。链上顺序不自动等于 CLOB aggressor/排队顺序；不构造链上执行能力。

现有来源和已有参考项目审查完仍无证明即可结束此调查，不无界搜索或为产生 fills 放宽模型。若要变更正式模型假设，单独提出版本化研究方案，不能混入本轮。

Nautilus 保持现有可选 challenger，不替代本地资格权威，不重开已知不可用的历史深度端点探测。

## 6. S02：真实天气 forecast vintage 核查

沿实际 weather producer → 原始 payload → 规范化 → weather join/signal/information clock/Paper 追踪，区分观测数据、预报数据和事后修订。

1. 对当前实际使用的 provider/product/model 建表，列原始字段、保留字段、资格消费方；查明初始化字段是源端未提供还是本地丢失。
2. 分开记录 model initialization、发行/可用时刻、valid time、首次本地 receipt、修订身份和不可变 run 标识。进程 run_id、采集时间、小时 valid time 均不能冒充模型初始化。
3. 多模型混合需逐模型有 lineage；不能用一个外层时间给全部模型授权。初始化时间本身不证明数据在该时刻已经可获取。
4. 阅读官方关于当前产品、Single Runs/历史预报产品及修订语义的资料。产品名字、Previous Runs lead 1 或现在查询历史运行不能单独证明当时可见性。
5. 保持现行 `lead_days=0` 禁入严格校准规则，不借来源核查调整既有闸门。历史回填/QC 数据可用于事后研究，不能补造首次 receipt。
6. 区分“已有历史能否合格”与“未来新采集能否保留足够证据”；今天可取得固定 run 不表示旧档案已补齐 provenance。
7. 列出哪些业务消费者真正依赖 forecast，哪些只依赖观测；用调用链证明，不先验把 forecast 缺口升级为全部原始天气采集必须停用。

结果分类与 S01 一致。必须给出未来最小验收设计：逐模型字段缺失、未来追加不改变过去决策、晚到 run、修订、混合模型不同 initialization、source/receipt 不合法。此处写验收方案，不新增产品测试或适配代码。

## 7. R02：采集恢复设计（不执行）

根据 R01 实况与当前代码设计，不直接复制旧启动命令。为每个组件列出启动入口、读写路径、依赖、旧状态格式、锁/ownership、恢复副作用和需要的新能力。

### 7.1 依赖与授权拆分

- 原始 market、supervisor、weather 采集与下游 signal、v2 shadow、Paper 分层；weather 原始采集不得因 market/Paper readiness 不足而被无依据阻断。
- 核实当前 runner 是否自动级联、自动 retention、默认引擎选择或自动迁移，不能只凭命令名称判断安全。
- 首轮未来运维授权应尽量限定为必要原始采集；signal/v2 shadow 另列条件；Paper 启动始终独立，不包含在任何采集恢复清单中。
- 如当前组件实际在运行，不提出为方便审查而重启或降频的步骤。

### 7.2 旧 cursor 与新边界选择

分别评价下列方案，不默认其中任何一个可执行：

1. 已有完整 prefix/identity/offset 证据的兼容恢复：列出必须验证的格式、解压偏移/行数/hash、plain/gzip 等价与来源稳定性。
2. 旧非零无 hash cursor：保持拒绝；今天给当前文件算 hash 不能证明旧进程当时消费了同一前缀。若无独立历史证明，不设计“补上 hash 后放行”。
3. 新采集 run/命名空间与明确断点：只能作为独立方案；旧证据保持只读，新旧 forward 边界分开。不能通过新命名空间丢弃未完成订单、库存、reservation、pending、去重状态或未解决 intent。
4. 下游若持有经济状态，先列出 ledger/account/reconciliation 处置条件；无法唯一恢复时维持 HALT，不以 tail-bootstrap 掩盖经济状态。

行情深度缺口不能回填；天气历史回填也不能升级为实时 receipt 证据。新边界不追认旧成绩，不合并 Paper/v2/challenger 账本。

### 7.3 面向下一任务的实施与验收包

恢复方案须包含：

- 所需最小代码/配置变更与原因；没有必要就明确“不需要改代码”。
- 精确目标组件、路径、预期状态变化、授权清单；未验证的命令用待核实标识，不给看似可直接运行的占位启动脚本。
- 只在副本/临时规模环境先验收的前置条件；本轮不做长时压测。
- 日后规模测量指标：输入/处理速率、backlog、p95/p99 周期延迟、prefix 重哈希读量、gzip 恢复耗时、内存增长、fsync 延迟；阈值依据实际采集节奏和资源预算预先声明，未测不写 PASS。
- 长时观察长度依据已知复发周期，不把几分钟烟测当作旧数小时池问题已解决；首次 receipt 与业务进度需独立于心跳核验。
- 停止/回退条件与恢复证据保全：仅未来明确授权范围内操作，不能回滚删除已产生新证据，不能自动停止其他健康采集。

## 8. 输出文件与证据格式

仅允许新增下列交付物；若已经存在，先阅读、保留原历史，按新 as-of 追加本轮章节，不覆盖他人结论：

1. `docs/source_capability_audit_20260910.md`：S01/S02 来源能力矩阵、实际转换链、证据与明确结论。
2. `docs/collection_recovery_plan_20260910.md`：R01 当前只读快照、R02 方案选择、风险、分离授权清单。
3. `docs/source_recovery_evidence_20260910.json`：机器可读索引，记录实际调查时间、HEAD/dirty 状态、证据 ID、命令与退出码、只读检查范围、限制和输出摘要。

每项来源结论至少关联一个证据 ID。证据包含官方 URL/页面或源码定位、访问时间、已知版本/revision、所支持的准确语义及其限制；本地样本记录路径、所读范围、可用 hash/稳定性检查和时间上下界。找不到历史 revision 写 unknown，不用当前 HEAD 代替。

索引中的 supported/unknown 等是报告分类，不是拟新增的生产字段。只保存必要脱敏片段，不复制完整归档、凭据或大量第三方源码；敏感 URL 参数不得写入报告。网络失败不包装成接口不支持。

本轮不修改 `CURRENT_CONCLUSIONS.md`、旧报告/指纹、现有规范；如发现矛盾，在新报告列勘误建议。未来实现或发布任务再经授权统一更新入口。

## 9. 验收与最终交接

交付前逐项验证：

- [ ] 四个工作项 R01/S01/S02/R02 各有实际结果或具体缺失依据，不只复制本任务。
- [ ] source guarantee、样本观察、本地假设、模型结果严格分开。
- [ ] S01 区分本地物化完整性、单条交叉核验和源端组闭合。
- [ ] S02 区分 initialization、可用时刻与 receipt，逐模型追踪且不回填旧前向资格。
- [ ] 当前 PID/归属/心跳/业务进度重新读取；不能查询的项为 UNKNOWN，非默认正常。
- [ ] R02 不用当前 hash 追认旧 cursor；保留未闭合经济状态；采集恢复与 Paper 分开授权。
- [ ] 官方链接、源码定位及本地证据引用可追溯；JSON 可解析；报告日期和实际 as-of 一致。
- [ ] 开始/结束 Git 状态对比，只增加约定报告；无实现/依赖/正式状态变更，`.claude/` 保留。
- [ ] `git diff --check` 检查 tracked 变化；对新增文档另检查格式与本地链接存在性，不能声称 diff 命令检查了未跟踪内容。
- [ ] `git status --short -- data`、`git diff --stat -- data` 单列结果，明确不等于全部 ignored 数据内容审计。选定只读输入若做前后比对，注明范围和外部 writer 竞态。

这是调查/文档任务，不要求为凑证据重复跑完整 pytest。引用 741 passed 时注明既有记录；如运行有界解析诊断，单列命令、实际输出与范围，不能当作新完整回归。

最终报告先回答：

1. 现有来源有哪些确定可用、适配缺失、尚待样本验证或不受支持的能力？
2. 当前采集链到底怎样，哪些是 UNKNOWN，哪些与旧快照不同？
3. 是否存在不依赖 Paper 就能恢复必要采集的方案？它还缺什么验证或实现？
4. 下一轮应先补哪一个最小能力；分别需要代码授权还是运维授权？
5. 明确本轮未实施/未部署/未启动 Paper，项目仍 NOT SEALED；调查完成不等于来源已合格或系统已恢复。

完成以上交付即停止。不要自动接着实现适配器、发起网络 probe、恢复采集、发布 GitHub 或启动 Paper。
