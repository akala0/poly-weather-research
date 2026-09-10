# Codex 任务：本地证据完整性与跨重启验收（2026-09-09）

状态：待实施。前一阶段是已交付的局部修复候选，项目仍 NOT SEALED，禁止启动 Paper。

本轮按 E01 → E02 → E03 → E04 顺序推进。目标是修复已复现的派生 tape 成员丢失漏检，并完成归档切换、天气 provenance 和业务 readiness 的有限范围验收。公共成交组闭合继续为 `UNSUPPORTED_GROUP_COMPLETENESS`，本任务不重开其正向撮合能力。

## 1. 接手依据与本轮独立检查

开始前完整阅读 `AGENTS.md`、`docs/ENGINEERING_ACCEPTANCE_STANDARD.md`，再阅读：

- `CODEX_PROJECT_RELIABILITY_REMEDIATION_TASK.md`：F01–F08、Q01–Q08 的原始定义。
- `CODEX_PROJECT_RELIABILITY_NEXT_PHASE_TASK.md`：上一阶段任务，保留为历史依据。
- `docs/reliability_next_phase_report_20260908.md`、对应 validation/fingerprint JSON。
- `docs/reliability_receipt_contract.md`、`docs/reliability_health_contract.md`。
- `docs/reliability_consumer_audit.md`、`docs/reliability_trade_source_semantics.md`。
- 当前测试 inventory、Paper 矩阵与正式状态入口。

本任务作者在 2026-09-09 做了以下独立检查：

| 检查 | 结果与证明范围 |
| --- | --- |
| 既有候选指纹 | 191 个文件逐一 SHA-256 一致；不含所有 ignored 正式归档 |
| 报告指定的定向集合 | 182 passed in 9.37s，exit 0 |
| 首次沙箱测试 | 56 passed、126 setup errors；原因是 pytest 临时根目录 WinError 5。沙箱外同一集合成功，不作为产品逻辑失败 |
| 560 诊断、10 Nautilus、595 collected | 已核对前轮原始记录；作者本轮没有重新执行这些全集命令 |
| 完整默认 pytest | 沿用前轮 socketpair 超时证据；本轮未重试，不称通过 |
| `data` Git 状态与 diff | 均无输出；不是 ignored data 全字节审计 |
| 新增独立反例 | `verify_materialized_receipts()` 接受删除全部 journal-backed 成员后的 payload，见 E01 |

开始时记录实际 HEAD、tracked/untracked 状态。已知 HEAD 为 `35ccb4530f6ec031d4b590e9ef688a49e5e60112`，但 HEAD 不代表脏树候选内容。发现漂移时重读并记录，不重置工作树。不得改写前轮指纹以掩盖本轮修改；交付新的指纹。

## 2. 范围与权限

允许修改本任务涉及的源码、测试、契约、交接文件，并在全新临时目录中运行 fake client、可注入时钟、真实 collector/follower 函数和恢复测试。CLI 只做解析或临时状态查询；不得运行 Paper engine 命令。

必须保持：

- 不启动 Paper，不启动/停止/接管/重启 daemon，不操作 Task Scheduler。
- 不修改正式 `data/`，不迁移旧证据，不运行正式 retention，不降频或中断采集。
- 不改系统网络、socket provider、代理、防火墙、注册表或 Python 安装。
- 不读凭据，不连接钱包、认证执行、User WebSocket、Relayer 或订单 API；不进行外部网络 probe。
- 不安装/升级/同步依赖；Nautilus 维持现有 optional challenger。
- 不 commit/push，不 reset/clean/stash/checkout，不清理 `.claude/` 或其他已有未跟踪文件。
- 不修改冻结策略、预算、超时、风险年龄、费用版本、校准阈值或统计口径。
- `execution_enabled=false`；成交带、中间价、对侧 token、`1-p`、结算价不作为可成交 bid/ask。
- 私有模型内核不得成为公共入口旁路。无闭合证据继续零 queue consumption。

保留已有 dirty tree，每次修改前重读实际文件。测试只写 tmp_path 或本任务新建的临时目录。无需修改系统权限来解决 pytest 临时目录冲突；可为本次测试指定全新、可写且经核验的 `--basetemp`，不得指向已有内容目录，因为 pytest 会清理该目录。

## 3. E01 / P1：验证派生 tape 的完整成员集合

### 3.1 已复现问题

路径：`src/poly_weather/public_trade_collection.py::verify_materialized_receipts`，本轮审查位置为 357–374 行。

目前算法选择仍带 `durable_public_receipt_v1` 标记的行，验证这些行与 journal 中预期行一致，但没有验证派生集合有没有缺成员。空列表使逐行验证循环直接结束。

独立反例使用真实 collector 和临时 fake client：

1. 调用既有 `coverage_fixture` 和 `_FakeClient(_trade('token', at, 'tx'))`。
2. collector 在临时目录生成一条合法成交、fact、witness、tape、cursor。
3. 读取 tape 到内存，复制 payload，将 `trades=[]`、`trade_count=0`，其他字段及 anchor 保持原样。
4. 调用 `verify_materialized_receipts(path, altered_payload)`。
5. 当前函数正常返回：`original_rows=1, remaining_rows=0, verified_missing_member=true`。

本次反例修改的是内存 payload，未破坏正式 tape。两个 tape loader 都调用该验证器，故不能把“现有成员验证通过”报告成“整个派生 tape 完整”。这不等于源端 trade-group closure；本项只证明本地已经 durable 的成员有没有被正确物化。

### 3.2 修复前先补真实入口失败测试

将同类损坏写入临时 tape，再通过 `load_event_trade_tapes` 和 `_public_trade_events_from_file` 的实际入口验证。至少覆盖：

- 删掉全部成员、删掉部分成员；
- 重复一条成员；
- 删除或更改行级 provenance 字段，企图把 journal 行降成 legacy；
- 删除顶层 receipt contract 或 anchor；
- 成员内容、事件作用域、count、digest 与 anchor 互相矛盾；
- 真正的零成员成功响应、失败响应、包含 legacy 的混合 tape，各自保持正确含义。

损坏不能变成 verified-empty、干净零成交或成功完整覆盖；不得静默跳过后推进 cursor。

### 3.3 先定义 bounded materialization contract

明确以下 authority，并选最小可实现方案：

1. 一个 tape 物化版本对应哪个 journal 前缀、哪个 event、哪些成员。
2. 前缀以内 first-receipt 去重后的完整预期集合如何计算。
3. 混合 legacy 行如何绑定其原有内容，而不升级为正式可用 receipt。
4. 合法空集合如何证明；失败查询不能提供完整零成交证明。
5. count/digest/版本/anchor 的可信绑定在哪里持久保存，避免只在同一可改写 payload 中自证。

可复用现有 fact/witness 和原子写机制，增加必要的物化 manifest/commit。不得为修一个集合验证缺陷再造无关账本。不要仅增加 `len(rows)==trade_count`：两者可以一起被删改。

**必须按物化时的 anchor 前缀验证。** journal 后来追加新事件、新交易时，旧的完整 tape 快照仍应可验证；不能拿 journal 当前尾部的全部成员直接要求旧 tape 包含未来行。相反，tape 声称覆盖某前缀却漏行必须拒绝。

对 legacy 或丢失识别标志的 tape，不能仅因字段缺失就退回宽松分支。基于明确 schema、producer/manifest 关系识别 downgrade；确实无法证明旧来源时报告 legacy/unverified，不假装已认证。不得迁移正式旧档案。

### 3.4 恢复及并发边界

要求：

- fact、witness、manifest、tape、cursor 的提交与重建顺序有一张 crash 表。
- reader 遇到 writer 已落 fact、尚未落 witness/物化提交时，有明确的“未提交新尾部”策略；不能读取半提交事实。能否继续读取上一个已验证前缀须由契约和测试证明。
- anchor、chain、scope、成员校验完成前，不执行会修复/覆盖证据的恢复操作；当前构造 journal 会写 recovery witness 的时机需要专门检查，尚未实测的风险不得写成已发生事故。
- 合法缺失派生物可以依权威事实幂等重建；存在但矛盾/损坏的 tape 必须保留字节并隔离，自动修复权限和流程必须明确。
- 不要求无限增加“见证见证写完”的时间字段；定义可持久推导的可用时刻、实际首次 admission 时刻及其关系，并用延迟 witness 的测试验证。
- reader 只读校验不生成 witness、不更新 cursor、不修 tape。

仍无法从本地检测“journal 和全部外部 anchor 同时被一致回滚”的场景，明确列为威胁模型限制，不能承诺无法实现的完整防篡改认证。

### 3.5 有界性能检查（Q07 局部）

两个 loader 当前会为 tape 验证重读共享 journal。用两档固定临时规模测量 journal 对象读取数、字节数、耗时和 peak memory，确认不会出现每个 tape 都重扫全部 journal 的失控增长。

只有测量表明需要优化时才增加每次读取周期共享的已验证快照或缓存。缓存绑定 journal generation/anchor/文件身份，变化、损坏、回滚时失效；不能用仅 mtime 缓存掩盖坏内容。性能优化不能推迟 lifecycle 到无界时间，也不降低验证强度。

## 4. E02：归档表示切换的 cursor 等价（Q01/Q06 局部）

重点文件：`archive_io.py`、`shadow_runtime.py::ShadowCursor/_incremental_jsonl_rows`、Paper cursor 和天气恢复路径。先列所有 archive reader 调用方，再确定最小兼容修改。

### 4.1 核心不变量

同一逻辑归档由 plain JSONL 变为内容等价的 gzip 后，已确认输入前缀不得被重复消费，也不得因换路径跳过未消费后缀。

定义 logical archive identity 与 representation identity。plain byte offset、gzip compressed offset、解压 byte offset、行号不是同一单位，不能直接照搬。

只有可验证的内容/前缀映射支持 cursor 转换。旧 cursor 无映射证据时，允许具名 UNKNOWN/阻断；不得猜 offset、重新 tail-bootstrap 或清空去重。兼容规则分版本，不能暗改 v2 schema。

### 4.2 最小端到端矩阵

- plain 全部/部分消费 → 正常重启；
- plain 部分消费 → 临时创建等价 gzip → 两种表示并存 → 重启；
- 后续只剩 gzip → 再次重启；
- gzip-only 首次启动与有 cursor 恢复，禁止混淆 tail-bootstrap；
- 含多字节 UTF-8、末行无换行、空行、尾部不完整行的 offset/行边界；
- 同名不同内容、压缩损坏、源同时变化、文件身份替换；
- cursor 在转换提交前后崩溃，重复两次恢复结果不变。

比较实际读取身份序列、cursor、天气 consumed evidence、订单/账户/queue 状态。公共 Paper 保持零消费时，仍要比 observation 次数与位置，不能靠两边 fill=0 宣称等价。

临时夹具可以模拟表示切换，不能调用正式 retention。缺少源封存证明继续禁止压缩后删除；本项完成不代表 F05 正向删除协议已完成。

## 5. E03：天气 provenance 与未来追加前缀不变（Q02/Q04 局部）

起点：`weather_market_join.py::_observation_from_row` 当前缺失 `collection_mode` 会默认 `realtime`。这是待逐来源验证的兼容风险；先核对 producer 和旧 schema，不把所有 legacy 行一律改坏或默认健康。

### 5.1 建立来源资格表

对实际 weather producer→archive→loader→join→information clock→signal/Paper 路径，列：source、schema/version、collection_mode、source_timestamp、received_at、精度、station、run/vintage、资格理由。

- 新记录字段缺失不能自动视为 realtime。
- 旧数据例外必须有已查明的版本/producer 来源依据；目录名或 fixture 断言不构成来源认证。
- historical_backfill 仅允许适用的事后分析，不得进入严格前向 join。
- 无 receipt、future receipt、source>receipt、时区/精度异常保持具名拒绝。
- 不破坏已有 METAR T 组优先与源精度保留规则。

### 5.2 决策前缀不变测试

构造生产形状的临时天气/市场输入，在决策截止 t 保存输出；追加 t 以后才可见的天气修订、日高、forecast/vintage、结算元数据和迟到观测，再回放到 t。

严格比较之前的 join、信息状态、信号、Paper decision、消耗 observation 与 cursor。晚到 QC 或最终温度不得反向改变已提交前缀。

区分“源时间早但 receipt 晚”与“当时可见”；历史 replay 不使用当前墙钟替旧订单过期。固定 vintage 和 `lead_days=0` 禁用规则必须走实际转换入口。

本轮只验证选定完整转换链。QUIET/complement 的全部收益算法、所有 walk-forward 折和全部历史季节仍未逐项核验，继续在 Q04 写明剩余范围；不跑正式历史重评分，不修改研究结论。

## 6. E04：完整业务 readiness 与真实依赖（F06/S09 剩余项）

保留已验证的 PID/heartbeat/integrity 真值表与 5 秒时钟容差。当前 `health_ready` 被定义为运行存活/依赖判断，progress 默认 UNKNOWN；不能把它直接升格为完整采集或 Paper 业务可用。

### 6.1 从调用链确认依赖

区分：进程启动/保活依赖、数据消费依赖、策略开仓依赖、评分资格依赖。

查实际 producer 与 runner 调用链验证 DAG。尤其不能因市场或 signal 不健康，误阻止一个可独立留存原始天气/市场数据的 collector 工作。状态汇总需要上游不等于采集器必须被其停启控制。

修改 runner 仅限源码与测试，禁止运行系统脚本、读取计划任务或触发恢复动作。

### 6.2 定义业务进度

至少需要两个带采样时刻、run identity、generation 和 committed cursor/sequence 的状态样本。明确：

- advancing：同一运行实例的合法进展；
- verified_idle：无新市场事件但轮询/连接维护有证据，不能仅凭心跳自报；
- stalled：有待处理输入或必须完成的工作，超过预声明窗口仍无进度；
- unknown：缺少比较样本/来源；
- reset/rollback/gap：换运行实例、代数变化、位置回退，各自处理。

零交易量和夜间安静不是 stalled 的充分条件。单纯 status sequence 自增也不是消费进度证明。不持久化两次样本时第一次判 UNKNOWN，不能把自己生成的观察当 producer 证明。

### 6.3 Paper readiness 贯穿全部必要闸门

通过共享 evidence 对象集中组合实际必要的 market、supervisor active set、weather、规则/季节、quality、archive continuity、账户/ledger 状态及 group completeness。

明确区分 `operational_ready`、`input_evidence_ready`、`paper_score_eligible`（可沿用等价现有字段）。字段要有 reason、时间和作用域。闭合为 UNSUPPORTED 时，score eligibility 保持 false；supervisor 单项健康不代表整个链路可用。

readiness 变坏不能禁止必要的 cancel、release、lifecycle sweep 或 durable HALT；风险 SELL 仍只依据新鲜完整同 token native bid，不能用普通存活状态替代价格资格。

用临时状态 provider 测试 CLI、signal/shadow、Paper 的消费结果；全链路中任何一个必要证据变未知都不能被另一处 fallback 洗白。不要引入循环依赖或因 heartbeat 正常绕过 stalled。

## 7. 本轮验收矩阵

| ID | 必须覆盖的实际断言 | 层级 |
| --- | --- | --- |
| EC01 | 删除全部/部分成员，两个 loader 均不能接受为完整空/少量 tape | PRODUCTION_INGRESS |
| EC02 | provenance/contract 降级、重复行、count/digest/scope 矛盾拒绝 | PRODUCTION_INGRESS |
| EC03 | 合法 empty、失败请求、legacy 混合各有准确状态 | PRODUCTION_INGRESS |
| EC04 | journal 追加后旧完整 anchor 快照仍可验证，未来成员不倒灌 | PRODUCTION_INGRESS |
| EC05 | fact/witness/物化提交/tape/cursor 各边界恢复及 reader 不写 | PRODUCTION_INGRESS |
| EC06 | bounded reader 与并发未提交尾部策略一致 | PRODUCTION_INGRESS |
| EC07 | 两档性能规模、实际读取次数及验证强度未降级 | 诊断测量，非 PASS 推断 |
| AR01 | plain→双份→gzip-only 的已消费前缀无重放/遗漏 | PRODUCTION_INGRESS |
| AR02 | 多字节/不完整末行/损坏/换身份/转换 crash 明确处理 | PRODUCTION_INGRESS |
| WE01 | 新 missing-mode、legacy、backfill 的真实 schema 资格表 | PRODUCTION_INGRESS |
| WE02 | future append、晚 receipt、固定 vintage 不改变已提交前缀 | PRODUCTION_INGRESS |
| HE01 | 两样本 advancing/idle/stalled/unknown/reset，采集与消费依赖分离 | PRODUCTION_INGRESS |
| HE02 | 完整 Paper readiness 不被单一 supervisor health 代替 | PRODUCTION_INGRESS |
| HE03 | readiness 拒绝仍执行必要释放/过期与合格风险处置 | MODEL_KERNEL + 临时真实入口 |
| SA01 | group closure 保持 UNSUPPORTED，无 private-kernel 生产旁路 | CONTAINMENT_ONLY |

每行关联真实 test nodeid、代码路径、反例、断言、结果与未覆盖边界。`PRODUCTION_INGRESS` 指临时夹具经过生产函数，不代表部署或正式前向运行。

## 8. 验证与交付

先保留 EC01 修复前失败，再跑定向回归。复用 `scripts/reliability_inventory.py` 与现有有界测试工具；不硬编码前轮测试数为本轮期望。

必要命令：

```powershell
.venv\Scripts\python.exe -m pytest --collect-only -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q tests/test_reliability_receipts.py tests/test_reliability_consumers.py tests/test_reliability_health.py tests/test_reliability_group_contract.py tests/test_reliability_documents.py -p no:cacheprovider --tb=short
.venv\Scripts\python.exe -m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider --tb=short
.venv\Scripts\python.exe -m ruff check src tests
uv lock --check --offline
git -c safe.directory=D:/poly diff --check
git -c safe.directory=D:/poly status --short -- data
git -c safe.directory=D:/poly diff --stat -- data
```

定向命令补上本轮新增测试、受影响 weather、archive、readiness、Paper/v2 回归文件。诊断套件仍明确列出 fees、market_supervisor、wrh_backfill、stream_daemons、polymarket_status 五个排除文件及 node 数。

完整默认测试：仅在准备最终候选时做一次有界验收；日志已有相同环境/相同候选的尝试则不重复制造相同超时。使用外层超时和堆栈输出，只回收本次创建的测试进程。超时、权限 setup error、业务断言失败分别报告。临时目录权限失败可修正测试目录，不能归咎产品逻辑；完整套件 socket 阻塞不许用 skip/xfail 冒充通过。

本任务只允许准备 F07 环境维护交接：固定命令、版本、堆栈、已知对照与最小待证假设。不得改变系统或把“换 Python/改代理”写成已验证修复。后续环境维护需单独授权。

交付：

1. 本轮修复、专项测试、更新的 receipt/health/consumer 契约。
2. `docs/reliability_evidence_closure_report_20260909.md`：范围、独立反例、修复依据、恢复表、性能测量、验证命令与限制。
3. 对应 machine-readable validation 与新候选 fingerprint；保存原始反例和完整失败日志，不覆盖旧指纹。
4. 更新 inventory/矩阵/整改入口。历史报告标注补充发现，不能无痕改成“前轮早已验证”。

最终报告不重复扩充固定 28 段模板；按四个 E 项分别给出“修改、反例→通过、实际入口、未覆盖项”，另列权限、指纹、测试结果与剩余工作即可。

## 9. 本轮不包含的事项与完成条件

本轮暂不扩大到：源端 closure 正向能力、正式 retention writer-seal 部署、所有策略全历史重算、费用线上更新、系统网络修复、Windows 真断电认证、完整许可证法律审核。

Q05 费用聚合舍入、Q06 各策略所有多文件 crash、Q08 历史 revision/来源继续具名待办；找不到历史 revision 就保留未知。Q04/Q07 只关闭本任务实际覆盖的转换链和测量范围。

完成本轮需要 EC01 反例被修复，E01–E04 所列入口、恢复和负向测试具备对应证据；未完成项如实保留，不能只因其他 500 多个测试通过就勾全表。

若完整测试仍阻塞或公共 ingress 仍为 UNSUPPORTED，允许报告“本轮局部整改已交付”，但总体必须保持：

```text
PROJECT NOT SEALED
PUBLIC GROUP COMPLETENESS UNSUPPORTED
DO NOT START PAPER
formal N=0, PnL=N/A
```

不要再把证据不足转化成无限新增任务或虚构接口。每个未关闭项给出缺什么证据、可执行的下一步及是否需要独立权限；确认来源根本不支持的能力保持 UNSUPPORTED，集中完成能在当前代码/测试范围解决的问题。
