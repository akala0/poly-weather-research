# Codex 修复任务：Paper V1 跨源成交身份与完整证据核验

## 0. 执行口令与任务边界

**不要启动模拟盘。** 本任务仅修复独立复核新发现的两个 P1 阻塞，并补齐生产路径、故障恢复及共享模块回归测试。

本文件是 `CODEX_PAPER_V1_SEAL_BLOCKERS_TASK.md` 的追加任务，不替代原始 Paper 规范，不撤销此前任何安全要求。完成本任务不等于整体封板，更不构成启动或真实执行授权。

先完整阅读 `AGENTS.md`，再查看 `README.md`、`HANDOFF.md`、以下任务与交接文档的相关条款：

- `CODEX_PAPER_SIMULATION_TASK.md`
- `CODEX_PAPER_V1_HARDENING_NAUTILUS_TASK.md`
- `CODEX_PAPER_V1_SEAL_BLOCKERS_TASK.md`
- `docs/paper_v1_seal_validation_status.md`
- `docs/paper_v1_test_matrix.md`

所有定位以当前磁盘代码为准，不能依赖历史行号。禁止直接把本文复现结果当作修复后的结果。

## 1. 已确认基线与未关闭事项

2026-09-08 独立复核结果：

- Paper 相关七个测试文件加 Nautilus 文件，合计 `120 passed in 4.73s`。
- Ruff `src tests` 与 `uv lock --check` 通过。
- 完整 pytest 在 `test_public_fee_rate_lookup_path` 停住；堆栈为 asyncio 创建唤醒套接字时进入 `socket._fallback_socketpair -> accept`。28 秒受控终止，没有完整 suite result。
- 当前 venv 的独立 `socket.socketpair()` 检查 8 秒超时。尚未定位过滤组件，不能断言根因已知。
- 前轮报告记录 Python 3.12 与 3.14 都失败；不得未经验证再次建议“换 Python 即可解决”。
- 只读状态检查：五个组件记录 PID 均不存活，心跳停留在 2026-09-04；checksum verified 不等于当前采集健康。
- Git 可见 `data/` 变更为空；包含 ignored/hidden 的文件名搜索未发现正式 Paper V1 输出。这不是全部归档内容哈希审计。
- 旧来源的历史 reviewed revision 仍缺失。不得捏造 hash，也不能以当前 upstream HEAD 代替历史审核版本。

**剩余问题不再是“仅网络与来源记录”。本任务下列两个代码 P1 必须加入阻塞清单。**

## 2. 绝对禁止事项

1. 不启动正式 Paper，包括 CLI 的 `--once`；不得创建或写入正式 Paper ledger/cursor/status。
2. 不读取凭据，不接钱包、私钥、签名、Relayer、鉴权客户端、User WebSocket、真实订单接口。
3. 不启动、停止、重启任何 daemon，不操作 Task Scheduler，不改变采集频率。
4. 不修改系统网络、代理、防火墙、Winsock、路由、驱动或安全软件，不以“解除测试阻塞”为由关闭防护。
5. 不做外部网络 probe；本任务不安装新执行依赖、不查询线上成交来构造测试。
6. 不修改、迁移、截断、回填或删除 `D:\poly\data`，包括旧 v2 及 ignored 文件。
7. 所有测试与故障复现使用 `tmp_path` 或系统临时目录；临时 follower 函数测试不等于正式模拟盘启动。
8. 不用 midpoint、last trade、对侧 token、`1-p`、历史价或结算值替代真实原生盘口。
9. 不放宽质量、新鲜度、天气、季节、资金、receipt、sequence、无前视或完整性闸门；`execution_enabled` 必须严格为布尔 `false`。
10. 不 commit/push；不 reset/checkout/clean/stash；保留既存脏工作树和 `.claude` 临时工作树，不顺手修改 retention、runner 等无关内容。

开始、结束均记录 `git status --short`、`git diff --stat`、`git status --short -- data`、`git diff --stat -- data`。当前 Paper 多个实现文件本身是未跟踪文件，不能用“Git 无 diff”误判它们没有既存内容。编辑前重新读取，避免覆盖其他任务更新。

## 3. 允许修改范围与复用原则

重点检查：

- `src/poly_weather/shadow_orders.py`：`canonical_trade_event_key`、trade 消费与原子订单快照。
- `src/poly_weather/market_trade_tape.py`：`validate_ws_side_semantics`、`build_shadow_trade_events`、WS 转换。
- `src/poly_weather/paper_spread_runtime.py`：进程内去重、durable 去重、pending/resolved、每周期 WS/API 合并。
- `src/poly_weather/shadow_runtime.py`：公共 tape 转换及共享调用路径。
- `src/poly_weather/paper_account.py`：仅在新身份/别名持久化与恢复确有需要时修改。
- 公共 tape 模型/加载器：仅在确认丢失必要原始身份或精度证据时最小调整。
- 相关 tests、新增专项测试、矩阵和验证交接文档。

复用现有原子写入、append-only ledger、intent/commit/reconciliation 和证据结构，不另建松散 sidecar 作为账户权威。Nautilus 继续仅作隔离 challenger；不更换版本，不用它代替本地证据闸门或解决身份歧义。

共享模块还被旧 shadow、离线研究与其他策略调用。必须列出调用方并回归，不能只让 Paper fixture 通过。不要直接把破坏 v2 恢复的变化推给未启动的 Paper。

## 4. P1-A：同一笔经济成交的身份不稳定

### 4.1 已复现缺陷

当前 key 将价格、数量直接插入字符串，并将可选 sequence 当作身份的一部分：

```text
trade:<event_id>:<asset_id>:<timestamp>:<price>:<size>:<sequence>
```

独立复现使用现有 `tests/test_paper_seal_blockers.py` 的 processor、snapshot 与 fill_trade 夹具：

1. 初始 BUY 具有 100 shares 前量；第一条 SELL tape `price=0.75, size=101, sequence=1` 使其成交 1 share。
2. 从同一临时 ledger 重启。
3. 同一条 tape 仅改为 `Decimal('0.750')` / `Decimal('101.0')`，其余经济字段不变。
4. 累计成交增至约 `26.66666666666666666666666667` shares，而非维持 1。

另一个独立复现：先输入 `source=data_api, sequence=None`，重启后输入其 `source=market_ws, sequence=1` 表示，同样从 1 share 增至约 26.67。两个路径都不是新的经济成交。

### 4.2 必须实现的语义

1. 分离“经济成交身份”“来源证据身份”“事件排序证据”“本地可见时刻”。source、receipt 或某来源新增 sequence 不得自动创造第二笔成交。
2. 对价格、数量使用精确 Decimal 规范化；`0.75`、`0.750`、等值科学计数表示产生相同数值身份。不得转 float、做容差合并、按 tick 舍入来伪造相等。拒绝非有限及不合法数值。
3. 对时间保留原始精度与时区来源；相同瞬间的等价时区表示统一。秒级和毫秒级记录不能靠随意截断、四舍五入或扩大时间窗拼成 MATCH。
4. 优先复用输入中真实、可验证、稳定的逐笔成交标识。不能臆造上游提供了 trade id / log index；先检查实际模型和归档字段。
5. 没有强逐笔 ID 时，只能在完整、唯一、receipt-safe 的跨源对应关系下建立别名。sequence 的缺失与补充是排序信息变化，不直接代表新成交；矛盾的 sequence 也不能静默忽略。
6. **禁止简单去掉 sequence 或只按交易哈希去重作为最终修复。** 一个 transaction 可含同 token 多笔合法成交；具有独立可验证身份的 sibling fills 必须分别保留。相同经济字段但无法辨别是重复记录还是不同 sibling 时，记录 UNKNOWN 并禁止使用有歧义数量，不猜测合并或累加。
7. 匹配、排序、去重应使用一致的规范化规则，不能只修 durable key 而漏掉 `_trade_identity`、pending ID、跨源合并与转换路径。
8. 同一周期与跨周期、API→WS 与 WS→API、重启前后，已经消费的经济成交最多消费一次；别名补充只能补证据，不能再次减少 queue 或增加 fills。
9. 继续让 canonical 消费事实与成交后/扣队列后的订单状态原子持久化。新增别名映射的写入顺序必须有明确 crash contract，不能增加新的“fill 写了、身份没写”窗口。
10. 新版 key/映射必须有明确版本与恢复策略。旧 Paper fixture 可唯一推导时幂等重建；不充分或冲突时 fail-closed。不得静默丢弃旧 key 后从空去重集合恢复。旧 v2 ledger/cursor/status 不迁移、不重写，兼容性或显式隔离必须测试证明。

## 5. P1-B：WS 核验未验证完整成交事实

### 5.1 已复现缺陷

`validate_ws_side_semantics` 用 `(transaction_hash, asset_id)` 建字典并比较 side；同键 sibling 可被最后一条覆盖。`build_shadow_trade_events` 又把这个批次级 side 判定当作逐条成交可用许可。

复现：相同 hash、token、SELL 方向、价格与时刻，公共 tape `size=1`，WS `size=1000`。当前返回 `queue_use_allowed=true`，并生成数量 1000 的可消费 WS TradeEvent。

“该字段可解释为 taker side”不等于“这一条 WS 的价格、数量与时间已被验证”。必须分开表达。

### 5.2 必须实现的语义

1. 为每条 WS 提供匹配结论，不用一个全局 bool 给整批所有记录授权。
2. 验证强身份（若有）、transaction、token、方向、精确价格、精确数量、源事件时间及其精度/一致性、市场作用域（两侧提供时），并校验 receipt 可见性与质量覆盖。
3. 只允许唯一完整对应关系。禁止按 hash/token 字典覆盖 sibling、选择第一/最后一条、任意文件顺序或最近时间邻居。
4. 字段不一致、候选多义、身份缺失或源时间精度不足时，给出明确 mismatch/UNKNOWN reason，不把“至少一条匹配”当全批成功。
5. WS 不能借公共 tape 的 side 证明自身额外数量；数量 1 与 1000 的复现必须拒绝 WS 队列输入。不同价、不同源时间、相反方向、错 token 等也分别测试。
6. 只有所有必需证据在决策时刻已本地可见才可验证。验证可用时间不得早于依赖证据的最晚 receipt；不能利用稍后 API 回执提前赋予早先 WS 可成交资格。
7. 延迟补证据不能倒写已终结订单或改写已提交的历史决策。事件时间、receipt 时间与当前生命周期状态的处理规则必须显式写清并测试。
8. pending/resolved 使用与正常匹配相同的核验器；不能当前路径严格、重启后只凭 hash 解除 UNKNOWN。仅凭出现一条公共记录不够。
9. 公共 tape 作为独立输入的可用性与 WS 验证失败分开报告：允许独立合法证据时只能用它自身的真实数量与身份；同一经济事件存在不可解释冲突时应隔离争议事件，不能通过 API fallback 或直接 `accepted_trade_events.extend(api_trade_events)` 绕过冲突。
10. 汇总统计区分原始观察条数、唯一经济事件数、重复、合法 sibling、pending、conflict、实际 queue 消费量。不得通过抑制未知计数或把 UNKNOWN 当未成交来美化结果。

## 6. 实施顺序

1. 固化上述两个最小反例为失败回归测试，确认在修改前确实失败。
2. 只读梳理 WS/API 原始字段、转换函数、consumer 与持久化结构。先写清身份/匹配/歧义/时钟 contract，再实现。
3. 统一规范化与逐条完整匹配，接入 pending/resolved 与 follower 合并路径。
4. 实现稳定经济身份、别名持久化和重启兼容；证明修复不重复成交，也不错误吞并可区分的真实 sibling。
5. 增加全生产路径和故障注入测试，回归共享 shadow 与 Nautilus 隔离。
6. 更新矩阵与交接文档，保留此前未关闭的完整测试环境和 provenance 限制。

不要趁机调策略参数、预算、退出档位或更换 fill model。

## 7. 必须具备的验收矩阵

以下名称可按项目风格调整，但不得删减语义。每项必须有实际断言，不以“未抛异常”或测试存在作为 PASS。

| ID | 场景 | 必须断言 |
| --- | --- | --- |
| T01 | 等值 Decimal 表示 | 0.75/0.750、101/101.0、科学计数等价；不重复消费 |
| T02 | API→WS 补 sequence | 同周期、跨周期、重启三种情况下只消费一次 |
| T03 | WS→API 缺 sequence | 对称顺序同样只消费一次，不丢合法首次成交 |
| T04 | 序列信息冲突 | 不当作第二笔，也不静默认定为同笔有效；有明确保守结果 |
| T05 | 真实 sibling | 同 hash/token 下有独立可信逐笔身份的成交分别保留 |
| T06 | 无法区分的 sibling/重复 | 禁止字典覆盖、按顺序猜测或静默相加；UNKNOWN 可恢复 |
| T07 | queue-only 重放 | 多次重启后前量只减少一次，fills=0 |
| T08 | partial/full fill 重放 | fills、shares、预留、成本、费用与累计买入成本均不重复 |
| T09 | 数量不匹配 | 公共 1 / WS 1000 拒绝 WS 量；争议量不经 fallback 绕过 |
| T10 | 其他字段不匹配 | 逐一改变价格、方向、token、时间、市场作用域，明确拒绝原因 |
| T11 | 批次内混合有效/无效记录 | 逐条判定，无全局成功泄漏；无效 sibling 不被字典覆盖 |
| T12 | receipt 与时间精度 | 验证不早于最后必要回执；时区等价与精度歧义分别处理 |
| T13 | 延迟公共补证据 | 到达前 UNKNOWN；到达后仅唯一完整匹配可 resolve，终结订单不复活 |
| T14 | pending 重启与冲突 | 重启不丢失；数量/时间不符或多候选不能解除 pending |
| T15 | 身份/订单/账户写边界故障 | 各持久化步骤前后中断重启：唯一 repair 或 HALT；不重复消费 |
| T16 | OSError 与 HALT | 写失败不提交输入 cursor；HALT 后订单、queue、账户零经济 mutation |
| T17 | failed cycle 重放 | durable 成功前缀含别名/partial fill，重试与干净基线经济状态相同 |
| T18 | 旧 key 恢复与共享 v2 | 不因 key 升级遗忘已消费证据；真实 v2 不写，仅临时兼容 fixture |
| T19 | 完整 continuous 路径 | 临时 checkpoint/WS/API 归档经真实解析、匹配、合并、ledger 与 cursor 重启，证明 T02/T03/T09/T14 |
| T20 | 安全与 challenger 隔离 | 默认入口不加载 Nautilus 执行客户端，execution=false，正式 data 不写 |

必须至少覆盖两个连续 poll；不得所有测试都绕开 follower 直接调用 processor。可以注入时钟、临时路径、持久化故障；不能 monkeypatch 生产匹配/身份函数为“验证成功”。

定义重启等价比较：账户、所有订单状态、queue ahead、fill 数量/份额、未成交预留、station-day cumulative cost、tranche/exit stage、pending/resolved 与已消费经济身份。审计时间或运行 ID 的差异可以单独解释，经济状态差异不可以忽略。

## 8. 验证命令与环境阻塞处理

优先使用现有 `.venv\Scripts\python.exe`，不为运行命令自动同步或改装主环境。新增专项文件必须包含在完整测试与定向命令中。

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_paper_account.py tests/test_paper_spread_runtime.py tests/test_paper_recovery.py tests/test_paper_runtime_boundaries.py tests/test_paper_cli.py tests/test_shadow_runtime.py tests/test_paper_seal_blockers.py tests/test_market_trade_tape.py -p no:cacheprovider
.venv\Scripts\python.exe -m pytest -q tests/test_nautilus_conformance.py -p no:cacheprovider
.venv\Scripts\python.exe -m ruff check src tests
uv lock --check
git diff --check
```

检查现有可选环境是否安装 Nautilus；未安装的 skip 必须如实报告，不等于 native conformance 通过。本任务不要求更换依赖版本；已有隔离可选环境可复用。

完整套件目标：

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -o faulthandler_timeout=15
```

外层必须有有界超时（建议 60 秒，正常长任务可说明后调整）；faulthandler 只打印堆栈，不负责终止。超时仅停止本任务创建的测试子进程，不按进程名批量杀 Python，不触碰现有 backfill 或 daemon。

若仍卡在相同 socketpair 路径，记录实际堆栈、命令、超时与无完整结果；不要反复无限运行，不改测试网络 mock/事件循环来制造全绿，不删除或 xfail 挂起测试。可运行诊断子集，但必须列出排除项且标为非完整验收。

系统修复另需用户明确授权，当前任务不得开展。历史 reviewed revision 只可从现有历史证据恢复；找不到就保留缺口，禁止猜造。

结束前只读执行：

```powershell
.venv\Scripts\python.exe -m poly_weather stream-status --data-dir D:\poly\data
git status --short -- data
git diff --stat -- data
rg --files --hidden --no-ignore data -g '*paper_spread_v1*'
```

`rg` 无匹配 exit=1 是文件名搜索无结果，不是全目录内容未变的证明。不要因采集状态异常自行操作进程。

## 9. 交付文件与最终报告

更新 `docs/paper_v1_seal_validation_status.md` 与 `docs/paper_v1_test_matrix.md`，不得覆盖历史失败事实。可新增专项回归测试和精简设计说明。

最终报告必须包含：

1. 安全声明与开始/结束 workspace 差异；明确本轮修改及保留内容。
2. P1-A、P1-B 修改前复现与修改后实际输出，给出代码路径与测试。
3. 经济身份、来源别名、sequence、receipt 的角色；强身份缺失、重复/sibling 不可分时的保守处理。
4. 正常匹配与 pending/resolved 共用核验的证据；API supplement 不绕过冲突的证据。
5. T01–T20 逐项实际断言与 PASS/FAIL/未完成；特别说明哪些经过完整 follower。
6. 新旧身份版本与共享 v2 的兼容/隔离方案，所有 crash 边界结果。
7. 定向、共享模块、可选 Nautilus、完整套件、Ruff、lock 的原始结果和退出码。中断不是 PASS。
8. 当前只读 runtime、正式 Paper 文件与 N/PnL 状态；无正式证据时 N=0、PnL=N/A。
9. 所有剩余代码、环境、来源记录和独立复核缺口，不得写“仅剩环境”而遗漏未检验路径。

只在两个 P1 的生产路径与故障测试全部通过时，才可写“两个新 P1 已修复，等待独立复核”。完整默认测试或其他原任务条件尚未满足时，最终仍必须写：

```text
A 轨：未封板；不要启动模拟盘。
B 轨：隔离 challenger，非正式评分权威。
```

即便所有测试通过，也最多是技术封板候选；正式启动始终需要独立审核和用户另行明确授权。完成后停止，不自动启动、部署、commit 或 push。
