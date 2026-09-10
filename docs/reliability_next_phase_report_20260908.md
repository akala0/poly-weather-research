# 下一阶段可靠性整改报告（2026-09-08；2026-09-09 续接验收）

工程候选，未部署。依照任务 §13 顺序交付；本报告不替代正式研究状态。
原始命令、输出和失败历史见 [validation](reliability_next_phase_validation_20260908.json)，
测试清单及分层见 [inventory](reliability_test_inventory.json)，候选内容见
[fingerprint](reliability_next_phase_fingerprint_20260908.json)。

## 1. 安全声明

未启动正式 Paper，未修改正式 data，未操作现有 daemon 或 Task Scheduler，未部署。
所有新增运行证据来自临时测试文件；检查正式 data 仅使用 Git 状态和文件名，不读取归档内容。
保留原有脏工作树和 `.claude/`；无清理、提交或推送。

## 2. 开始/结束 HEAD 与工作区

开始和结束 HEAD 均为 `35ccb4530f6ec031d4b590e9ef688a49e5e60112`。
开始已存在 Stage 2 的 12 个 tracked 修改及未跟踪测试/helper/fingerprint/task/`.claude/`。
HEAD 不代表候选内容。开始状态记录和结束逐文件 SHA256 见 fingerprint；
旧 Stage 2 指纹是历史参考，不被重写成新阶段的证据。

## 3. 本任务实际改动

生产源码：`receipt_journal.py`（新）、`public_trade_collection.py`、`runtime_safety.py`、
`paper_spread_runtime.py`、`market_trade_tape.py`、`shadow_runtime.py`、
`trade_tape_analysis.py`、`archive_io.py`、`signal_engine.py`、`daemon_recovery.py`、`cli.py`。
Windows 两个 status/runner 脚本仅修改源码，未执行。

新增 receipt/health/group/consumer/document 五个专项测试文件和 `runtime_health_support.py`；
更新 CLI、signal、shadow 和既有 reliability_evidence 断言。新增 inventory/bounded-test 脚本、
四份契约/审计文档及交付记录；更新矩阵、seal status、整改入口和 CURRENT_CONCLUSIONS。
其他既存 Paper 模型测试修改属于 Stage 2，不冒领为本任务新增。

## 4. P0 文档纠偏

旧“生产 follower 正向成交”“成功 fill 前缀恢复”等声明已标 SUPERSEDED，保留历史不追认。
当前 native follower 证明的是零消费 containment，private kernel 才测试合成经济效果。
当前测试名来自真实 collect-only；四个文档检查核对名称、分层、源码 hash、单一正式状态入口和计数。
不把旧 383/501 等历史通过数当当前总数。

## 5. Receipt 定义与 authority

详见 [先写后实现的契约](reliability_receipt_contract.md)。source、request、完整响应、
post-fact-fsync 观察上界、first_seen、journal sequence、派生文件时间和 decision_visible 分开。
`receipt_committed_at` 是事实写入返回后采样的保守完成上界，非臆造 fsync 指令精确时间；
witness 保存该上界，不宣称自己的稍后写入已在该时刻完成。
失败请求另记 failure_observed_at，response_complete=false、response_received_at=null。
源时间晚于 response 的行保留 UNKNOWN_FUTURE_SOURCE，不修造成有效 receipt。

## 6. 提交顺序

完整响应 → capture → 不可变 fact fsync → 完成时间 witness fsync → tape → cursor → audit。
复用唯一临时文件/fsync/atomic-replace writer，并新增短写检查；collector OS 锁覆盖整个提交。
成员 digest、序号、前序 hash、witness、tape/cursor anchor 相互校验；journal 不是可覆盖 latest JSON。

## 7. Receipt crash points

| 边界 | 验证结果与限制 |
| --- | --- |
| fact 前 | 无 durable receipt；重试使用更晚 first receipt，不恢复 RAM 时刻 |
| fact 后、witness 前 | 原完成时钟不可恢复；保留 fact，恢复时给更晚提交上界 |
| witness 后、tape 前 | 不再请求，重建同一 first receipt、成员及提交上界 |
| tape 后、cursor 前 | 幂等重放；成员不重复、receipt 不漂移 |
| cursor 后、audit 前 | 从 durable facts 重建 audit |
| 损坏尾对象/中间对象/checksum/witness/rollback | fail-closed，保留原字节；未定义自动截断，不擅自修尾 |
| tape/journal 冲突 | 不覆盖 tape；独立 discrepancy 并阻断 |

`test_receipt_crash_reconciliation`、`test_receipt_corruption_preserves_bytes_and_blocks`、
`test_receipt_tape_conflict_is_quarantined_without_overwrite` 经实际 collector 路径测试。
这些是故障注入与重建测试，不等于真实断电/磁盘故障认证。

## 8. Legacy receipt

旧缺失 receipt 行保持 historical-only；不以 mtime/fetched_at/source/current scan 补齐。
重复观察不覆盖已有 first receipt。新 journal 行由两个 loader 只读重验，冲突不降级为空数据。
未执行正式旧 tape 迁移；旧非 journal 行的全量 provenance 仍未认证。

## 9. Source-semantics 调查

仅使用本地 API reference §6/6.2、Data API adapter `_range/_trades_for_query`、
WS archive parser 与测试；详情见 [来源决策](reliability_trade_source_semantics.md)。
短页终止、10,000 offset 限制、秒区间二分和排序是本地实现，不构成服务端完整快照契约。
没有可查的 bounded-lateness watermark、唯一逐撮合 ID 或全局 exchange closure sequence。

## 10. Group completeness 决定

`UNSUPPORTED_GROUP_COMPLETENESS`。F02 污染风险已 containment，正向能力未恢复。

## 11. Closure certificate

不适用：没有足够来源语义，不实现自证 certificate，不把 caller bool 或 fixture 当生产授权。
G04 中依赖可信 certificate producer 的正向项目记 UNSUPPORTED，而非 PASS。

## 12. 公共入口零消费

Paper `process_trade/process_trades` 只记录观察和 UNKNOWN，不调用经济 kernel。
native 多 poll、跨文件、重启测试保持 `accepted_queue_trade_rows=0`；
静态 AST 检查生产源码没有调用或反射查找 `_process_ordered_model_trades`。

## 13. Late sibling / gap / pagination / alias

迟到 sibling 追加 invalidation，不修改既有经济字节；同秒本地 sequence 不赋予 closure。
请求/分页异常不产生成功空页或提前 watermark。WS/API 模糊别名、receipt 不明、
坏质量或 source→receipt 中间事故均不得升级资格。重复刷新与表示变化不创建第二经济身份。
API 历史质量缺口仍见 consumer audit，不把未做的全链路 gap 认证写完成。

## 14. Exactly once

Receipt 重放幂等经 collector 测试。Paper queue-only/partial/full、消费与账户事实原子性、
双向 alias 和重复重启等既有经济测试属 MODEL_KERNEL。
公共入口 zero-effects 不证明可达的生产成交 exactly-once；目前没有这一正向能力。

## 15. 健康真值与消费者

先声明 5 秒 future-skew 容差，再实现共享 normalize/read_status/read_chain_status。
dead PID 无论自报为何均 stopped；无 PID/无效或缺失心跳、future/stale、归属不明/复用均拒绝。
checksum verified 与 health 独立。CLI、runner、signal、shadow 和 Paper supervisor 闸门接入该结果。
进程提供器只用测试替身/测试自身，不验证常驻进程。

重要限制：单次状态无法证明业务进度，progress 默认 UNKNOWN；health_ready 是运行存活/依赖闸门，
不是采集业务进度已证实。工程规范 S09 的完整业务进度验收仍未关闭。Windows runner 仅静态检查，
未做系统级重启实验。Paper 的完整链路 readiness 仍不能由 supervisor 单项健康替代。

## 16. 高风险 consumer 调用表

详见 [consumer audit](reliability_consumer_audit.md)，覆盖 collector、两个 tape loader、
WS matcher、ShadowOrderEngine、Paper/v2、QUIET/complement、Nautilus 和历史 analytics。
本轮真实补上完整区间 quality 拒绝及 journal claim 验证；不是项目级所有策略审计通过。

## 17. F04/F05

F04：排他 collector、immutable receipt 链、损坏保留、短写拒绝及故障测试已补。
Windows directory-entry power-loss durability、全部账户写边界仍部分未验证。
F05：未启用任何压缩/删除；无 writer seal / exclusion 证明时继续 deferred。
归档等价检测不授权 retention 删除。

## 18. Q01–Q08

Q01 部分：等价双份去重、冲突阻断已测；仅剩 gzip 后的跨重启 cursor 切换未全面证明。
Q03 部分：共享 receipt/质量区间反例通过；legacy API provenance 与完整 gap 证明未关闭。
Q05 部分：固定 Weather Decimal 公式/舍入/共享 wrapper 通过；线上更新和所有聚合舍入未审计。
Q06 部分：receipt 与 Paper 模型恢复通过；v2/QUIET/complement 全部多文件边界未验证。
Q02/Q04/Q07/Q08 保持 pending，不宣称项目级整改全部完成。

## 19. 定向结果

最终指定集合：`182 passed in 9.08s`，exit 0。涵盖新增专项及旧 evidence/recovery/boundaries/shadow。
完整原始命令见 validation.targeted；无跳过/xfail 真实失败来伪造通过。

## 20. Collection / diagnostic / full

最新 collect-only：`595 tests collected in 0.34s`，exit 0。
最终诊断：`560 passed in 15.77s`，exit 0；明确排除 fees、market_supervisor、wrh_backfill、
stream_daemons、polymarket_status 五个 socket-dependent 文件，共 35 项，非完整套件。

本任务唯一完整尝试 2026-09-08 11:43:50 UTC：15 秒栈显示
`socket.py:298 accept → :633 _fallback_socketpair → asyncio → test_fees.py:59`。
60 秒后回收本次 PID 39228 的测试树；cleanup exit 0，wrapper 输出 124，工具外层 exit 1。
没有完整 pytest 结算，不能算 PASS。续接未重复相同完整尝试，亦未修系统。
上次最后诊断命令因额度被拒绝而未启动，2026-09-09 已成功补跑。

## 21. Nautilus / Ruff / lock / diff

已安装可选环境：`10 passed in 0.66s`，exit 0；仍 challenger-only/official_score=false。
Ruff src/tests：All checks passed，exit 0。uv lock --check --offline：32 packages，exit 0。
git diff --check：exit 0（有 Windows LF→CRLF 提示，不是错误）。版本及命令见 validation。

## 22. 正式 data 检查边界

Git `status --short -- data` 与 `diff --stat -- data` 均无输出、exit 0。
`rg --files --hidden --no-ignore data` 按 `*paper_spread_v1*` / `*paper_v1*` 文件名过滤无匹配，exit 1。
这证明指定正式命名路径未出现，**不是整个 ignored data 的内容 hash 审计**。
没有为证明“零修改”而扫描深度归档正文。

## 23. Runtime / daemon / Scheduler

未读取/修改 Task Scheduler，未启动、停止、接管或探测现有 daemon PID。
仅受控终止本次超时 pytest 进程树；状态脚本修改没有部署或执行。
临时 collector/follower 函数测试不属于正式 Paper 前向运行。

## 24. 凭据 / 网络 / 执行

未读取凭据、认证环境值、钱包或私钥；未外网探针、安装/同步依赖、修改系统网络。
没有新增真实下单、撤单、签名路径。测试的 Paper CLI 仅参数拒绝/临时空账本状态，
未启动 Paper engine 运行模式。所有生产执行能力仍关闭。

## 25. Git

未 commit/push/reset/clean/stash/checkout/rebase/merge。原有未提交更改保留；
没有更改 global safe.directory，命令只使用单次 `-c safe.directory=D:/poly`。

## 26. 正式数量

按未启动事实与指定路径缺失：formal orders=0、fills=0、round trips=0、N=0、PnL=N/A。
这是正式 Paper 口径，不是测试 fixture 数量，也不是 v2 diagnostic 成绩。
唯一正式状态入口仍为 [CURRENT_CONCLUSIONS](../CURRENT_CONCLUSIONS.md#paper-v1-formal-status)。

## 27. 剩余 blocker

来源不支持 group closure；完整默认 pytest 尚未通过；Windows 掉电/目录提交与完整业务进度未认证；
Q01/Q03/Q05/Q06 余项及 Q02/Q04/Q07/Q08 未关闭；独立复核未进行。
receipt 完成上界及跨对象 witness 方案需要独立复核，不以本轮单人测试替代。
后续系统诊断/修复、部署、正式数据迁移或 Paper 启动均需另行授权。

## 28. 最终判定

后续复核补充（2026-09-09，不改写历史测试结果）：发现删除 tape 已有成员后，
旧验证器仍接受空列表。此前成员完整性结论不能作为完整物化证明。
新修复及尚未关闭的验收项见 `reliability_evidence_closure_report_20260909.md`。

本轮局部实现和隔离验证已交付；不能称项目级整改完成或公共 trade ingress 已恢复。
**NOT SEALED — DO NOT START PAPER — formal N=0, PnL=N/A。**
