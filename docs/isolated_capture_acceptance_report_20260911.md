# 隔离 capture 独立验收 — 2026-09-11

**结论：BLOCKED。没有启动真实采集，也不能提交为已通过的试采候选。**

本轮完成独立反例检查、窄修复和当前候选完整回归。硬性总期限仍不满足；A3 的完整有状态消费者矩阵仍有未覆盖项。通过测试不等于满足全部验收条件。项目 NOT SEALED，Paper 不启动，正式 N=0、PnL=N/A。

## 候选与范围

修改前 Git 状态、全部 Python 源码/测试及 configs 文件 SHA-256，修改后指纹、命令和日志全文见 [机器证据](isolated_capture_acceptance_evidence_20260911.json)。候选 manifest SHA-256：`a3c95e5aa301f607ca6c547ff263d986060d04c9453bcf945febb3e9f376f206`，算法为 after_sha256 对象排序、紧凑 JSON 的 SHA-256；未提交工作树不能仅用 HEAD 代表候选。

本轮源码/测试增量仅为 `collection_identity.py`、`collection_capture.py`、`archive_position.py` 和新增 `test_isolated_capture_acceptance.py`。更新测试 inventory 与本轮报告，历史报告只增补状态。冻结的 configs 文件逐一相同；保留全部既有脏改动。

所有执行验证使用临时目录、fake HTTP/WS 和项目 Python。未调用真实 market/weather 端点、未启动 capture CLI 的真实传输、未操作 daemon/Task Scheduler/防火墙/代理，未迁移旧 v2、启动 Paper、写正式 data 或 commit/push。本结论不声称其他已有进程停止自行写入数据，也没有为核查而接管它们。

## 反例、修复及未决边界

| 问题 | 修改前证据 | 本轮处理 | 当前结论 |
|---|---|---|---|
| question 缺失、日期冲突、resolutionSource 重复冲突 site | 三种非法输入未被拒绝 | question 必须存在并与目标当地日一致；site 参数唯一且等于 registry station | 反例通过；规则 verifier 未放宽 |
| halted store 发布 complete | 明确 halted 后 finish 成功 | complete 前拒绝 halted | 不再误报成功 |
| finish 的 fsync 异常留下可被重启接受的成功 JSON | 故障注入后旧路径仍存在成功终态 | 先独占写 pending、flush/fsync 后 rename；pending/不确定尾部阻断同根重启 | 当前进程可观察的写失败已封堵；断电目录项持久性未认证 |
| 启动耗时未计入 runtime | 慢首个 fsync 后仍进入全窗口；可无身份成功 | 起点提前至 run_capture 启用检查后；到期且无身份时失败 | 只修记账和误成功；未实现硬墙钟时限 |
| 显式 capture 文件使共享 cursor 推进 | 行最终被配对器丢弃，但输入位置已前移 | read_positioned_rows 在返回新位置前拒绝带 capture_domain 的行 | 显式文件及旧前缀追加 capture 均不推进 cursor；原已提交前缀仍可重建 |
| 接收层已交付的超限帧缺少内容诊断 | 超限直接抛错 | 保留总长度、完整内容 hash、受限前缀、truncated=true 后失败 | 仅诊断，不能冒称完整原文；传输库提前拒收的内容仍不可保存 |

最初反例运行 `4 failed, 6 passed`，退出码 1；其中两个 passing diagnostic 表示成功复现缺陷，并非安全通过。最终新增文件含 28 项，测试名称与原始日志均保留在机器证据中。

### 阻塞 B1：完整调用链没有硬性总期限

`test_diagnostic_synchronous_fsync_can_exceed_campaign_deadline` 将首次 fsync 延迟 1.2 秒，plan.runtime_seconds=1。当前候选能在返回后检测到期、以 `capture_budget_before_identity` 失败，但真实耗时仍至少 1.2 秒。这个 passing test **确认阻塞仍存在**。

`asyncio.timeout` 不能抢占同步文件操作。CLI plan 读取、启动扫描/registry 读取、写入/fsync、最终落盘及退出清理尚不能被同一硬期限约束。不能把 `runtime_seconds=300` 写成进程一定在 300 秒内退出。

最小下一步是单独明确并验收单次子进程的外部期限/ownership/退出确认方案（仍不接旧 runner），或提供能证明覆盖上述调用链的等效实现。不能靠在线试采测运气。若使用父进程监督，超时后须确认自己创建的 child 已退出；未知退出码不得当 0，未知存活不得重试。该实现涉及进程控制边界，本轮未扩成新的运行框架。

### A1/A2 验收边界

当前是用户显式有限 selections，不是自动发现。十站历史 fixture 每站 22 个唯一 token；完整规则核验均仍未通过。首次与重连重新保留响应、校验绑定；token 交换和 condition 变化在第二次订阅前拒绝；只有规则文本变化时更新 evidence/projection hash，策略资格仍 false。身份是公开源字段一致性检查，不是链上独立身份证明。

覆盖默认关闭零输出、CLI 传输失败实际 exit 2、JSON/HTTP/连接超时、失败预算、取消、不同 root 竞争同一锁、短写/flush/fsync/低空间/字节额度、失败终态与坏尾拒绝、Windows 大小写/父根/junction。锁实际位于 TMP：不同 root **仅在相同 TMP 命名空间内**互斥，源码的 per-host 注释不能作为跨用户/不同 TMP 的证据；未强抢锁。

未完成项：today/tomorrow 同时存在的集合验收；控制文件 start/membership/finish 各写阶段的完整故障矩阵；跨 TMP 并发边界。启动或失败日志自身无法持久化时，也不能保证有结构化 failure，必须由外部日志/退出证据补足，不能覆盖坏尾。低磁盘余量测试不是全套真实 ENOSPC 系统调用验收。

## A3 reader 家族与有状态证据

新 fixture 由实际 CaptureStore 写出 full/delta/trade/规则内容，保持 capture envelope 原样。以下测试名省略 `test_` 前缀，均在新增文件，除注明旧基础测试外。

| 家族 / 实际入口 | 对照与断言 | 证据 / 覆盖限度 |
|---|---|---|
| signal：LiveSignalEngine._ingest_market/_ingest_weather/_event_signal | 先有真实 token book，固定时钟；books/weather/current_signals/已提交位置/评估计数、输出、DB 表计数及非 DB 文件不变；天气入口明确 UNKNOWN_WEATHER_COLLECTION_MODE | signal_existing_book_output_and_business_state_unchanged；**没有完整 run/业务发布周期对照** |
| signal JsonlTail、v2 增量、Paper 增量/提交前缀 | 明确 ISOLATED_CAPTURE_INPUT_FORBIDDEN；新位置空、已有 cursor 不变、合法 committed prefix 仍可读 | explicit_capture_file_cannot_advance_shared_input_cursor；mixed_capture_cannot_advance_existing_cursor_or_rebuild_prefix |
| Paper/v2：BookSnapshot.from_mapping → 实际 processor | 先建立活动订单；固定时钟 status、pending trade evidence、账本无变化；明确 no timestamp 拒绝 | paper_and_v2_active_order_state_reject_explicit_capture_rows；**仅实际适配入口，非完整 follower 生命周期** |
| QUIET/complement 转换器 → engine | 有活动单/已提交 pair；订单和固定时点 pair summary 不变；明确转换错误 | quiet_and_complement_existing_orders_unchanged；**完整 replay 混合路径未补齐** |
| depth：replay_books_at_or_before | 非空 token/截止时刻请求，真实旧 full 可读取；追加 capture 后相同；capture root 得 None | nonempty_depth_and_discovery_mixed_legacy_inputs |
| public-trade discovery：discover_depth_event_coverage | 旧输入产生非空请求目标；混合文件及显式 capture 文件不增加目标 | 同上；没有执行任何请求 |
| real-NO、weather join、WS tape、liquidity、information clock | 旧基础测试确认 capture root 不被默认目录枚举、显式 envelope 无输出 | test_collection_capture.py::test_capture_envelopes_do_not_enter_actual_shared_consumers；**天气/info clock 可见有效前缀及 real-NO/liquidity 非空混合报告计数仍未补齐** |
| shared archive/元数据/status/下游启动 | capture 独立目录/schema，未写正式 status/active set；共享 positioned reader 已加入口拒绝 | 默认路径基础测试及源码审查；**正式 status/启动判断完整运行级对照未补齐；未操作现有 runner** |

共同入口调用链：signal_engine.py 的 JsonlTail.poll 调 read_positioned_rows；shadow_runtime.py 的 _incremental_jsonl_rows 在成功返回后才更新 position；paper_spread_runtime.py 导入该 helper，提交前缀重建直接调用 read_positioned_rows(committed_only=True)。此次窄修复在返回位置之前拒绝 capture。混合 batch 拒绝后保持旧提交位置，不能描述为跳过 capture 并自动消费后面的合法行。

因此 **B2：不能宣称所有有状态消费者完全隔离**。转换器拒绝与共享 cursor 闸门是有效证据，但不替代矩阵中标出的业务运行路径。后续只补这些有限 fixture 对照，不需要启动 Paper 或正式消费者。

## A4 原文与 L2

在配额内、传输层已交付的文本/二进制以 base64、receipt、epoch 和 payload SHA-256 留存；本地序号/hash-chain 只证明本地记录的完整性。未知格式可留原文，结构观察保持 `healthy_l2=false`。重连重新创建 SnapshotBoundary；旧 full 不授予新 epoch 的初始化资格，delta 不替代新 full。缺侧、错误作用域和未知格式的已有反例通过。

没有实现或认证完整 L2 重建、源端连续性、源端无丢帧、公平成交组闭合。max_size 在 WS 库交付前拒收时只能留下连接失败/gap，不能声称留存了不可获得的原文；已交付超限帧只保存受限前缀和 hash。配额/落盘失败可能连终态也无法落盘，保留不确定状态而非自动修复。所有输出仍不具备策略/Paper 准入；public group completeness=UNSUPPORTED。

## 当前候选验证

| 检查 | 结果 |
|---|---|
| 新验收 + identity + capture + archive_position_closure 定向 | **84 passed in 3.87s**，exit 0 |
| 完整默认 pytest，无排除集 | **877 passed in 44.27s**，exit 0；外层 180 秒，faulthandler 45 秒，墙钟 44.84 秒 |
| Ruff src tests | All checks passed，exit 0 |
| git diff --check | exit 0；只有既有 LF/CRLF 提示 |

解释器为 `D:\poly\.venv\Scripts\python.exe`，Python 3.14.3。测试 TMP/TEMP 位于 `C:\Users\Administrator\AppData\Local\Temp\poly-capture-acceptance-3oyrfa1u`；新测试逐项隔离锁。849 是历史候选基线，本轮不借用旧结果；原 asyncio 环境阻塞已被后续环境修复取代。本轮没有改可选 Nautilus 接口，也未把可选历史结果合并到默认回归。

## 交付停止点

[试采包草案](isolated_capture_bounded_trial_plan_20260911.md) 已按真实 schema 验证历史模板，但标为 BLOCKED / 未执行 / 待独立授权。需要先关闭 B1、补足所列验收缺口并重新跑当前候选回归，才能申请真实有界试采。不得使用旧 market 恢复授权绕过。

**本任务没有启动采集；用户仍需明确授权有界试采。Paper 不启动，项目 NOT SEALED。**
