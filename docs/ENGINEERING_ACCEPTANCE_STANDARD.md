# 工程开发与验收规范

版本：1.0（2026-09-08）。适用：采集、归档、研究回放、signal、shadow、Paper、离线 challenger、运行状态和工程报告。

本规范是用户要求的新开发/验收约束，不是“现有代码全部符合”的声明。当前不符合项见 `../CODEX_PROJECT_RELIABILITY_REMEDIATION_TASK.md`。不追认旧成绩，不扩大原有执行、运维或数据变更授权。

## S01. 权限与任务分层

- 审查：只读检查、隔离诊断、临时测试；不得顺便实现或部署。
- 写任务/规范：只修改指定文档；不因文件包含启动命令就运行它。
- 代码修复：仅用户授权范围内的源码/测试/文档；测试输出只能在临时目录。
- 环境维护、进程操作、正式数据治理、Paper 启动、真实执行：是不同授权，不相互继承。
- 命令使用白名单式目的审查：公开批量数据 POST 不等于订单 POST；同样，execution=false 字段不证明没有实际执行客户端。检查能力和调用链，不只匹配词。
- 不读钱包/密钥/认证凭据，不构造执行客户端；发现凭据只报告风险，不输出内容。
- Paper 不启动；daemon/Task Scheduler、网络/代理/防火墙、正式 data、commit/push 保持原禁止边界，除非用户另行明确授权相应动作。

验收证据：任务范围、允许/禁止操作、开始结束 Git 状态与实际调用声明。不要宣称检查范围以外“绝无副作用”。

## S02. 单一责任与跨层契约

必须区分：原始事实、规范化对象、验证结论、策略决策、经济效果、派生展示。

| 层 | 负责 | 禁止 |
| --- | --- | --- |
| 采集 | 原始响应与真实首次本地 receipt | 补造源时间、用请求开始时间代替收到 |
| 规范化 | 精确数值、时区、作用域、来源引用 | 丢掉质量、精度、回执、原始身份 |
| 验证 | 逐项资格与 reason、歧义隔离 | 以批次成功给无效行授权 |
| 策略 | 消费当前合格证据作决策 | 用最终结果或未来证据修改过去决策 |
| ledger | append-only 经济事实与可恢复状态 | 用 status/cursor 的摘要覆盖经济权威 |
| cursor | 已确认输入前沿与恢复辅助 | 读到即确认、处理失败仍前进 |
| status/report | 派生当前状态和证据限制 | 用字段存在、旧 PID、测试 fill 宣称当前健康/盈利 |

任何修改须列 producer→converter→consumer→ledger→restart→report 调用方；共享函数修改要验证所有相关策略，不允许局部私有规则与其他入口矛盾。

## S03. 原始证据不可被“修好”

- missing、verified-empty、corrupt、unreadable、partial、recovered-last-good 是不同状态。
- corrupt/unreadable 不得转换为空数组/空字典后覆盖旧文件。保留原字节，明确隔离对象和恢复依据。
- 解析失败不得静默跳过后让 cursor 永久越过争议行。选择阻断、durable quarantine 或其它明确策略，必须可审计且不将缺失误报零成交。
- 质量状态、incident、source/run identity、source timestamp 原文/精度、received_at、token/market identity 必须贯穿 pending 与恢复。
- 缺字段不是 healthy、normal、realtime 或可消费的充分条件。旧 schema 的例外必须按版本和已证实来源范围定义，不能无限默认。
- 新证据恢复健康只影响其自己的资格，不能回头把先前 degraded 记录改为 normal。

验收：质量单调性测试——删除或恶化证据不能使原本拒绝的同一记录变为允许；可验证补证据只能按照显式规则解除 UNKNOWN。

## S04. 时间契约与无前视

至少区分 source/event time、request start、response receipt、first local visibility、persistence time、decision time。一个 now 参数不得冒充全部时钟。

- first receipt 在响应完成后取得，去重/刷新/无请求调用不改变旧值；重复接收可另记 last_seen。
- 验证依赖多个来源时，可用时刻不早于全部必要证据最后到达时刻。
- 当时缺失的 receipt 只能 unknown/historical-only，不用 mtime、今天时间或最终档案时间倒填为前向证据。
- 精确 UTC 转换；保留原始时区/精度。朴素时间、非有限数值、超出支持精度不能静默修正。
- continuous 使用显式可注入 UTC；historical replay 只用事件/receipt 时间线，绝不在回放首帧调用真实墙钟过期历史订单。
- 时间回退、future receipt、组内排序不充分有显式处置；不能用 max(0, age) 把未来心跳当新鲜。
- weather source/receipt 均不晚于决策，historical_backfill 不得混入严格实时回放；模型使用固定 vintage，lead_days=0 不用于无前视校准。
- 质量窗口须检查实际依赖区间，不仅看区间端点；停机缺口不能靠最终价格或后来的天气填造。

验收：未来数据追加不改变已提交前缀；请求模拟耗时、回执刷新、时间精度、重启时钟、跨质量窗口全部有反例。

## S05. 经济身份、重复、别名与组完整性

- 经济成交身份、source observation identity、sequence、receipt 独立建模。
- Decimal 精确等价表示统一；不得转 float、按价格 tick/容差合并本来不同的经济事件。
- transaction hash 不是单笔 fill ID；合法可区分 sibling 保留，不可区分者 UNKNOWN。不得凭不同字符串字段就宣称有可信上游逐笔 ID。
- API/WS 别名不创造新交易；补 sequence 不再消费一次；矛盾 sequence 不静默吞掉。
- 持久化去重不是仅维护进程内 set；queue-only、partial/full fill、跨源、重启都恰好一次。
- 同秒组需持久记录完整性与排序依据，不能先移除旧事件再判定新批次是否有歧义。
- 不具有可证完整性时，不按“这一 poll 只有一条”“等了若干秒”假设组已封闭。
- 迟到证据使先前假设失效时，追加失效/隔离记录并禁止发布干净评分，不改写旧经济事实。
- 任何 key/schema 升级有兼容与冲突恢复规则；不能清空去重后继续消费旧档案。

验收：等价输入表示、来源交换、合法不同批次切分、重复投递、崩溃恢复的经济结果保持一致；不具备相同可见性/完整性的组合应明确 UNKNOWN，而不是硬凑相等。

## S06. 匹配与策略保护必须在所有路径生效

- WS 核验逐条验证作用域、方向、精确价格/数量、时间、唯一性、receipt、质量；一个 bool 不给整批授权。
- normal、pending/resolved、API-only supplement、重启与 replay 复用同一资格契约。
- 冲突交易不能从 fallback 重新进入队列；独立无争议交易可以按自身证据处理。
- maker touch 不等于 fill，盘口量减少不等于 taker；只允许相反 aggressor 的同 token 合格 tape。
- 风险卖出仅用同 token 新鲜、完整、健康、未跨缺口的真实 bid depth；缺失时 stranded、成本占用、未定价 PnL=N/A。
- 研究的 midpoint/last-price 可作诊断，不得用于模型 fill、可成交价或正式收益。
- 预算/策略固定参数不能为过测试而放宽。Paper 全局 $200，station-day cumulative cost 是附加闸门；不同策略账本隔离。
- 四档退出以约定累计真实 shares，partial/timeout 只重挂残量，不超卖；已有合格退出不被未满足的补仓条件压制。

## S07. 持久化、HALT 与恢复

- 明确每次逻辑 transition 的写入顺序和 authority：intent、订单/queue事实、账户 effect、commit、checkpoint、cursor 各自是什么。
- queue 消费身份与消费后的订单状态同一 durable fact；别名观察本身不是消费 commit。
- 单文件原子 replace 不等于多文件原子事务；必须解释每个 crash 窗口如何恢复。
- 复用经审查的临时文件、flush/fsync、replace、目录同步（平台支持范围内）、唯一 writer/ownership 协议。
- 只有 durable 事实可唯一推导的缺失效果可幂等 repair；冲突、坏行、无法唯一解释的未完成 intent 必须 fail-closed。
- persistence OSError 后不继续使用未确认内存经济状态；durable HALT 写不了时停止相关进程并报告无法持久化，不假称写成功。
- HALT 后外部 snapshot/trade/lifecycle/quality/supervisor 入口都不继续经济 mutation。
- 同周期成功前缀后失败：输入 cursor 不越界，重试不重复 effects；checkpoint 前后错位均有测试。
- durability 测试同时比较账户、订单、queue、fill、预留、tranche、exit stage、consumed observations、open time、stranded、站日累计成本和证据状态，不只比较余额。

## S08. 归档与删除是独立风险域

- 年龄或分区名不证明文件不会再追加；先证明封存/ownership。
- 新建和已有 gzip 均要求解压内容字节数＋SHA-256一致，并检查源稳定性、文件身份和目标可读性。
- 校验与删除之间不能留下可写窗口；无法提供排他/封存保证时不删除源。
- 临时文件不得覆盖其他 writer；压缩失败、短写、磁盘满、损坏、并发任务时保留可恢复副本。
- plain/gzip 同时存在时不能盲目双读或只挑一个；等价合并和冲突隔离均须遵循证据/cursor版本。
- 递归删除/移动先验证绝对路径及目标作用域，包含 symlink/junction 风险；禁止宽泛 home/workspace/root 操作。
- 正式 retention 配置调整、运行压缩/删除、迁移都需授权；不因代码测试成功自动执行。不以停止市场深度采集换取“无竞争”。
- 报告区分逻辑字节、磁盘分配字节、压缩等价证明和已删除可恢复性。

## S09. 运行健康协议

健康必须同时证明：完整性、进程存活、命令/运行实例归属、心跳新鲜、上游依赖与业务进度符合契约。

- status_integrity=verified 只代表内容完整性。
- PID 存活不证明归属；PID 可重用，需要 run/process identity 与命令证据。
- running/connected/reconnecting/degraded/stalled 等状态定义清晰，非终止状态须统一执行 liveness/freshness 判定。
- heartbeat 缺失/无效/未来/过期不允许返回“当前健康”；last-good 回退显式 degraded。
- 已死亡进程的 reported_state 可以保留，但 effective state 必须明确停机/失效。
- 无消息可能是 quiet，也可能是断流；必须使用可验证进度/连接证据区分，不能抑制重连计数。
- 读状态不包含修复动作；修复守护链、网络、任务计划需另行授权，维护窗口记录不得补造缺口。

验收：状态真值表覆盖异常组合，CLI、runner、signal、Paper readiness 对相同输入结论一致。

## S10. 测试分层与反例纪律

每个缺陷必须先有可复现反例，再有修复；不能只改测试期望接受错误行为。

至少五层：

1. 纯规则/数学测试：identity、精度、费用、守恒。
2. 转换契约：生产 metadata/schema、receipt、quality 原样传播。
3. 完整入口：临时原始归档→实际 follower/collector→ledger/cursor/status。
4. 故障与恢复：每个 durable 边界前后、重复/乱序/迟到、损坏/无权限/ENOSPC。
5. 共享模块回归：Paper、v2、QUIET、complement、研究和CLI调用方。

补充性质测试可复用现有工具：表示不变性、批次划分、duplicate投递、restart等价、未来追加前缀不变、质量恶化不增加资格、资金/份额守恒。不能把排序不可知的情形硬定义成可成交。

允许 fake 外部响应、时钟、故障hook；禁止把被审查的匹配/质量/账户函数 mock 为成功。测试不得写正式 data，不启动正式 Paper 或现有daemon。

完整默认套件、可选依赖套件、诊断子集分开。skip 必须列原因；超时/KeyboardInterrupt 不算 suite result。设置外层有界超时与堆栈输出，不无限挂起或批量杀其他 Python。

测试矩阵每行记录：需求、代码路径、测试名、实际断言、层级、故障/重启边界、结果、限制。PASS 只代表该断言在该环境通过，不代表整个需求或正式运行已验收。

## S11. 版本、开源复用与改动纪律

- 优先成熟且语义/许可匹配的现有实现；区分直接依赖、源码改编、设计参考与拒绝。不为保持自研重造成熟基础设施。
- Nautilus 固定可选依赖和隔离 challenger；其限制明示，不能因为 matrix MATCH 数量多就升级为正式成绩权威。
- 不臆造上游字段或接口。新增字段注明来自真实wire、规范化契约还是纯fixture；不能用fixture证明上游真实提供该字段。
- 依赖版本、配置/schema hash、源代码内容指纹、测试环境与报告绑定。脏树中的 HEAD 不是候选版本完整标识；未跟踪文件也需纳入相关文件清单。
- 第三方 notice 追加维护不覆盖旧归属；历史 revision 找不到如实记录，当前HEAD不替代历史证据。
- 冻结配置改变必须有显式策略版本和授权，不以 lint/refactor 混入。
- 一批修一类契约，明确回归范围；没有性能/复杂度证据，不以“大文件”自动启动全项目重构。

## S12. 报告与验收状态

报告至少区分：

- 已复现缺陷：输入、路径、实际输出与预期差异。
- 代码风险：依据明确，但未有完整反例，不包装成已发生事故。
- 已验证修复：红→绿＋生产路径＋恢复边界。
- 环境阻塞：具体命令/堆栈/退出状态，不归咎未经验证的组件。
- 历史证据、当前快照、推断、UNKNOWN/N/A 各自标注。

每个当前结论有 as-of、证据来源、版本与适用范围。历史PID/心跳不得长期作为当前状态；工程文档改动不能改变正式研究成绩。

正式样本仍以 station-day 等相关风险簇/季节/阈值版本划分；订单、快照、token数量不是独立样本。n<30 明示不可靠；比率与保守期望按项目要求提供置信区间，不以点估计或 fixture PnL 宣称正期望。

状态阶梯：

1. 任务/规范已写。
2. 修复实现完成。
3. 定向测试通过。
4. 完整默认及可选回归通过。
5. 独立复核通过，技术封板候选。
6. 用户另行批准的正式前向运行。

不得跨级：第5级不是第6级，更不是真实执行授权。任何关键缺陷、完整测试阻塞或必要证据缺失未关闭，继续“未封板；不要启动模拟盘”。

## S13. 每次交付的最小检查表

- [ ] 范围和权限写清，禁止动作未发生。
- [ ] 既存脏工作树保留，相关 tracked/untracked 文件与测试版本可识别。
- [ ] 反例及实际生产转换链已测试，未用fixture字段替代真实schema。
- [ ] 首次receipt、quality、identity、group completeness跨恢复不丢失。
- [ ] durable写边界、cursor和HALT有故障测试。
- [ ] 共享调用方回归，不只测新增模块。
- [ ] 完整/可选/诊断结果、skip、超时如实区分。
- [ ] 代码修复、部署状态、系统环境、正式成绩分开。
- [ ] data检查的证明范围准确；无正式样本仍N=0、PnL=N/A。
- [ ] 未关闭事项、下一步授权与独立复核要求明确。

此表不是勾满即可封板；每项须能指向真实证据。
