# 任务：隔离原始 capture 独立验收与有界试采授权包

> 本次交付仅为创建/整理任务文件，不执行下述验收、修复、测试或网络查询。下述步骤供后续明确启动本任务时执行；真实单事件短时公开试采仍须在验收通过后另行授权。不得启动采集或 Paper。

## 1. 目标与停止点

当前不是重写采集系统，也不是恢复正式 market/supervisor。
对默认关闭的 `market-capture` 做独立源码审查、临时端到端验收和必要的最小修复，交付可审核的有界公开试采方案。
**完成本任务后停止，等待用户另行授权真实网络试采。不得自行开始试采。**

当前已知：2026-09-11 修复项目 Python IPv4 回环防火墙问题后，完整默认测试实际为 **849 passed in 39.92s**，此前两项 capture 异步测试均通过。
这是历史候选基线，不覆盖后续改动，不证明真实 WS 协议兼容、采集恢复或整个项目封板。
最新环境证据以 `docs/windows_asyncio_loopback_fix_20260911.md` 为准；早期 capture 报告中的环境阻塞记录保留为历史。

本轮成功状态只能是 `READY_FOR_BOUNDED_CAPTURE_REVIEW`（可提交授权审查）或 `BLOCKED`。
项目仍 `NOT SEALED`；Paper 不启动，正式 N=0、PnL=N/A；Nautilus 仍为 challenger。

## 2. 必读与事实核对

完整读取 AGENTS.md，以及：

- `docs/collection_admission_layering_design_20260911.md`
- `docs/unresolved_rule_evidence_review_20260911.md`
- `docs/collection_capture_implementation_20260911.md`
- `docs/collection_capture_validation_20260911.json`
- `docs/windows_asyncio_loopback_fix_20260911.md`
- `src/poly_weather/collection_identity.py`、`collection_capture.py`
- CLI 的实际 `market-capture` 入口、相关测试与复用持久化原语。

先记录 Git 状态、候选代码/测试/registry 指纹。逐项区分设计要求、已实现能力、已验证事实，禁止把旧设计提议写成已存在接口。
保留全部既有脏工作树和历史证据；不 reset/clean/stash，不覆盖其他任务的改动。

## 3. 授权范围与禁区

本轮允许：源码与文档审查、临时目录 fixture、fake HTTP/WS、必要的本进程 loopback 测试、与本主题直接相关的窄修复和回归。

禁止：

- 任何真实 market/weather HTTP 或 WS probe、真实 capture 启动。
- 启停、重启、启用或修改 daemon、weather、Task Scheduler、现有 runner action。
- 修改正式 `data/`、旧 v2 ledger/cursor/库存、正式 registry 或策略配置；不补 hash、不 bootstrap 旧 cursor。
- 钱包、凭据、签名、真实执行客户端、私有端点、User WS、POST/DELETE order。
- 改系统防火墙、代理、驱动；当前 Python 回环修复保持原范围。若环境复发，留证并报告，不能扩大全机权限。
- 自动 promotion、导出旧 schema、接入共享 DB、发布正式 active set/status/signal 配置。
- commit/push；添加常驻运行能力、自动重启服务或扩大既定采集业务范围。

## 4. A：独立审查和最小修复

不要为通过验收而重写整个旧链路。发现问题先给出可执行反例，再修最窄的 capture 或输入边界；若需要改变共享架构，列为阻塞并申请扩范围。

### A1 身份与规则资格

验证首次连接和重连均绑定 event → market → condition → YES/NO → token，且绑定站点/目标日与不可变 evidence hash。
测试同 slug 身份变化、跨 event token 冲突、交换映射、缺字段、多候选/集合歧义，以及 today/tomorrow 同时存在。
不能只看最终映射长度或用字典覆盖冲突；原响应必须在拒绝结果前可追溯留存。
完整结算 verifier 的 INCOMPLETE/拒绝继续保留，不因身份合格变 VERIFIED。
明确当前是用户显式选择有限 event，还是自动发现；不为匹配设计文案引入新发现系统。

### A2 真实入口、超时、锁与存储

通过实际 CLI 与 `run_capture` 控制流，仅 fake 外部传输及必要时钟，不 mock 身份门、存储或消费者准入为成功。
覆盖默认关闭时零客户端/零输出、有效有界 plan、HTTP/转换/身份失败、WS 连接/接收超时、重连上限、取消退出、总运行时限和资源上限。
完整调用链必须受总期限约束，包括 discovery、退避及退出；不能仅在消息循环检查时限。
验证独占 writer、不同 root 的并发边界、失败/不确定尾部恢复；未知 ownership 不得强抢锁。
临时 TMP/TEMP 隔离，避免测试接触真实 capture 的全局锁。
对 evidence/frame/flush/fsync/frontier/finish 故障、短写、ENOSPC 注入，检查原字节、hash-chain、已确认 prefix 和退出结果；不能误报 complete、删除坏尾或重写 receipt。
验证 Windows 路径别名、大小写、junction/symlink、正式根及其父目录重叠；无法验证的路径拒绝，不用临时改系统权限补测试。

### A3 有状态消费者隔离（本轮关键补证）

现有 `test_capture_envelopes_do_not_enter_actual_shared_consumers` 是有效基础，但转换器拒绝、空目录和 `requests={}` 不等于完整生产消费者隔离。
从实际 `CaptureStore` 生成带有效原始 full/delta/trade/规则内容的 capture 文件，覆盖：

1. 默认目录枚举与显式把 capture root 当 data root；
2. 显式把 capture 文件/行交给现有输入适配层；
3. 有效旧输入与 capture 混合，旧输入仍正常、隔离输入无新增效果。

建立 reader 家族 → 实际入口 → 测试 → 比较字段矩阵，至少覆盖：

- signal 的状态/输出/业务发布；Paper 和 v2 的订单、queue、fill、库存、pending、输入 cursor；
- QUIET/complement 的状态与订单；天气 join / information clock 的可见前缀；
- real-NO、深度、流动性与报告计数；public-trade discovery 的新增请求目标；
- shared archive/转换器/元数据 reader，以及正式 status/下游启动判断。

所有状态用临时 fixture；经济状态须包含活动订单或库存，不只断言 BUY=0。深度请求须非空。
使用一致时钟与对照运行，区分合法 lifecycle 自然推进与 capture 引起的变化；不能要求时间变化后的全部文件天然逐字相同。
显式拒绝可作为正确结果，但捕获异常必须断言明确原因，不能吞任意异常后算通过。
若多个家族确实共用同一入口，可共享 fixture，并以源码调用链证明覆盖；不为追求测试数量重复造测试框架。
不测试恶意解包改造成旧格式的任意外部脚本；本任务禁止提供这种导出能力。

### A4 原文留存与 L2 能力分离

检查 full 缺侧/空侧、delta 先于 full、重连、错 token/condition、未知 wire、坏帧、超限帧的原始留证和明确终态。
目前 `SnapshotBoundary`/输出中 `healthy_l2=false` 不应为了试采改为 true。
当前交付可仅为 identity-bound 原始字节留存器；明确未实现/未证明的 L2 重建、源端连续性、组闭合。
原始字节成功持久化不等于完整盘口恢复；local sequence/hash-chain 不证明上游无丢帧。
本轮不新增撮合资格、不用成交价/mid/1-p/对侧 token 补簿，public group completeness 保持 UNSUPPORTED。

## 5. B：生成待授权的真实有界试采包（只设计，不执行）

以实际 `CapturePlan` 和 CLI 字段为准，生成经 schema 验证的 plan 模板及操作清单；不能创造不存在的 flag。
event slug/市场日期必须有证据。已有日期若过期，标记需要授权范围内重新获取；不把历史候选假装当前有效，也不生成可误运行的虚构 slug。

建议初次仅 1 个身份明确的事件、最多 5 分钟、最多 1 次连接失败即停止；其他字节/frame/token/磁盘额度依据实现和 fixture 测量给出有限值及理由。
若此建议与实际 schema/控制流不符，解释并提出可执行边界；本轮不暗改上限。

授权包必须写清：

- 候选 revision（未提交则用文件清单与 SHA-256）、解释器、配置/plan hash、明确独立绝对输出根；
- 精确命令模板、允许的公共域名/端点、HTTP 请求总预算、WS session/重连预算、总期限；
- schema 未提供 HTTP 总请求上限时，按实际 selections × attempts 推导并证明；不能声明代码不存在的硬限制。
- 手动单次有界进程优先，不通过恢复旧 supervisor/计划任务实现。weather/旧 v2/下游/Paper 不动；不复用旧恢复授权。
- 开始/结束只读核查、输出允许清单、真实退出码、attempt 失败留证、确认自建进程退出；无法确认不得重试。
- 成功仅指身份门通过、公共响应/帧可持久化且验证 hash-chain、配额守住、隔离未破坏；协议不支持可留证但不能冒称健康 L2。
- 失败立即停止，不换旧 `market-stream` 绕过，不改 registry，不删证据，不自动延长窗口。
- 即使通过也不转常驻；持续采集、扩大站点和正式链路恢复各需后续授权。

## 6. 验证和交付

改动完成后执行完整默认 pytest（不得用排除集替代），定向 capture/identity/新增隔离测试、Ruff、diff 检查。
使用当前 `.venv\Scripts\python.exe`；外层有界超时和 faulthandler 留堆栈，TMP/TEMP 隔离。不安装依赖或改 Python 版本来绕过失败。
涉及可选 Nautilus 接口才追加相应可选验收，始终与默认结果分列；不把历史结果挪成当前通过。

建议新增交付：

1. `docs/isolated_capture_acceptance_report_20260911.md`：问题、反例、修复、真实测试结果、未决能力。
2. `docs/isolated_capture_acceptance_evidence_20260911.json`：候选指纹、逐家族测试/断言、命令、退出码、范围声明。
3. `docs/isolated_capture_bounded_trial_plan_20260911.md`：下一次独立授权包，明显标注“未执行，待授权”。

日期可按实际执行日命名，若目标已存在先读后增补，不覆盖历史证据。原 capture 报告只追加指向新验收的状态注记，不抹去旧失败记录。
不要为了此任务新增网页、通用框架、重复清单或全项目整改。

## 7. 完成判定

以下全部满足，才能报 `READY_FOR_BOUNDED_CAPTURE_REVIEW`：

- 身份、持久化、期限/资源和消费者隔离的本轮反例通过，未发现未处理的阻塞缺陷；
- 完整默认回归通过，未靠 skip/deselect、宽松断言或关闭闸门；
- 原文采集、L2、策略资格的能力边界真实且一致；
- 有明确可审核的有限试采包，未执行真实网络或运维；
- 候选可复核，既有脏改动保留，正式 data/registry/旧 v2 未被本轮修改。

若不满足，报告具体阻塞、证据及最小下一步，不再把“测试数量增加”当进展结论。
最终必须单独写明：**本任务没有启动采集；用户仍需明确授权有界试采。Paper 不启动，项目 NOT SEALED。**
