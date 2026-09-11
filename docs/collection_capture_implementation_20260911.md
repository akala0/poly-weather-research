# 带身份门的隔离原始 market capture：临时验证记录

日期：2026-09-11

本轮只实现并验证“带身份门的隔离原始 market capture，默认关闭”。它不是 market/supervisor 恢复授权，也不改变 weather、旧 v2 cursor/库存、Paper 或任何正式数据。

## 已实现的边界

- `src/poly_weather/collection_identity.py` 在创建订阅前一次性核验 `event → market → condition → YES/NO → token`。缺失、重复、冲突、日期/站点不一致、未对齐的 outcome/token、非法 condition/token 和超出预算都会整体拒绝；不会通过字典覆盖后继续。完整结算 verifier 的结果只作为逐事件诊断保存，不能把 identity 资格升级为策略或 `VERIFIED`。
- `src/poly_weather/collection_capture.py` 使用独立 `isolated-public-market-capture/v1` 根目录和 `*.capture.*` 文件。每个 run 有独立 attempt/run ID、开始记录、原始 Gamma 响应、原始 WebSocket 帧、hash-chain、结构观察、连接 gap、失败/完成结果和身份 membership。写入采用独占创建、flush/fsync、序号和前值 hash；短写、配额、磁盘余量或断尾会阻断继续写入，不会自动截断、压缩、删除、治理或迁移旧 cursor。
- capture 入口 `market-capture` 默认关闭，必须同时提供显式 `--enable-capture`、有界 plan 和 capture root。入口不构造 `MarketWebSocketBot`/`ResearchWarehouse`，不写共享 DB、正式 raw/status、active set、signal 配置、旧 checkpoint 或旧格式导出。
- `SnapshotBoundary` 只记录结构观察。重连会清空旧 token 的 full-snapshot 状态；新 epoch 在 full snapshot 之前收到 delta 会记录原因。未知协议、坏报文、连续性、群组完整性和 L2 健康均保持 `UNKNOWN`/`UNSUPPORTED`/`false`，原始字节仍留存。

## 临时测试证据

- `tests/test_collection_identity.py`：25 passed。覆盖十站 unresolved 规则样本、缺 condition、重复 token/market/condition、长度不齐、跨 event 冲突、日期/站点/活动状态、非法 token 和预算拒绝，以及映射/原文投影 hash 分离。
- `tests/test_collection_capture.py` 的同步部分：21 passed。覆盖默认关闭、正式 `data` 路径和 root 文件拒绝、空 runs 目录恢复、原文 byte/hash-chain、失败/断尾重启阻断、写失败不推进 frontier、未知/错 token/错 condition、重连 full-snapshot 边界和实际共享消费者隔离。
- 实际消费者隔离测试单独通过 1 项：capture 文件不会进入 archive glob、天气 join、WS trade tape、深度/流动性/信息时钟、real-NO、shadow pair 或 BookSnapshot 转换器。
- 异步 fake recorder 的 2 项入口测试未计为通过：当前 Windows 测试环境在创建 asyncio event loop 的 `socket.socketpair()` 阶段阻塞，不能据此判断 recorder 逻辑通过或失败。
- 当前测试清单收录 849 项；排除这 17 项会触发同一 asyncio 入口的测试后，广泛回归为 **832 passed, 17 deselected**。其中包含其余已有测试和新 capture/identity 同步测试。完整 Ruff（`src tests`）和 `compileall` 通过；新 capture/identity 文件格式检查通过。

## 停止条件

本记录不授予正式 market/supervisor 接管许可。未完成的异步真实入口/重连验收必须在能运行 asyncio loop 的临时环境重新执行；在此之前 capture 保持默认关闭。没有启动 daemon、Task Scheduler、signal/shadow/Paper，也没有读写正式采集目录或旧 v2 状态。

## 后续独立验收注记（2026-09-11）

原环境阻塞及本报告历史测试结果保留，不代表当前候选。后续环境修复见 `windows_asyncio_loopback_fix_20260911.md`。本轮独立验收、最小修复和完整默认回归为 877 passed，但总期限与完整消费者验收仍有阻塞，当前状态 BLOCKED；详见 [新验收报告](isolated_capture_acceptance_report_20260911.md) 与机器证据。没有真实试采；Paper 不启动，NOT SEALED。
