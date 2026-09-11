# 有界单事件试采授权包草案 — 未执行，待授权

**状态：BLOCKED，当前不能运行。** 独立验收的总期限与完整消费者证据尚未闭合，见 [验收报告](isolated_capture_acceptance_report_20260911.md)。本文件只供后续审核，不代表申请已批准，不复用旧 market/supervisor 恢复授权。

## 候选冻结

未提交候选的源码/测试/config 文件清单及逐文件 SHA-256 位于 [机器证据](isolated_capture_acceptance_evidence_20260911.json) 的 after_sha256；manifest 为 `a3c95e5aa301f607ca6c547ff263d986060d04c9453bcf945febb3e9f376f206`。其中包含正式 registry 的当前只读指纹；不修改 registry。解释器 `D:\poly\.venv\Scripts\python.exe`，Python 3.14.3。任何后续修复或 plan 变更均需重新冻结，当前指纹不能覆盖未来候选。

拟议独立绝对输出根：`D:\poly\collection_quarantine\bounded-trial-review-20260911-01`。本轮 safe_root 只读校验通过，路径尚不存在；没有创建。实际授权前须再次验证无别名/重解析点、与正式 data 不重叠、空间充足，且该路径尚未被占用。

## 经真实 CapturePlan 验证的历史模板

**日期已过期，禁止直接用此模板试采。** slug 来自保存的 `docs/strict_rejection_query_20260910T091217Z/KLGA.response.json`，不是当前活动事件。当前事件/当地日/slug 没有本轮网络证据，须在后续单独批准的查询范围内重新获取并冻结；不能机械替换日期猜 slug。

```json
{
  "selections": [{
    "station_id": "KLGA",
    "target_date": "2026-09-10",
    "event_slug": "highest-temperature-in-nyc-on-september-10-2026"
  }],
  "runtime_seconds": 300,
  "max_tokens": 64,
  "max_bytes": 67108864,
  "min_free_bytes": 1073741824,
  "max_frame_bytes": 2097152,
  "max_frames": 10000,
  "max_connection_failures": 1
}
```

上述实际 schema 验证通过；排序紧凑 JSON SHA-256 为 `a18874a553894f14b76d1300d3b29dce72dd81117a5f678dca2ba7a4c190c0c6`。这不是未来批准 plan 的文件 hash。实际批准文件需同时记录原始文件字节 hash 与 schema 值 hash，不生成可误运行的当前假 slug 文件。

额度依据：历史 fixture 为 22 tokens，事件 JSON 编码 27,220 bytes，最小 full/delta 分别 309/268 bytes。64 tokens 允许有限桶数变化，超过则身份门拒绝；这些最小帧不是现场最大帧或流量测量。2 MiB 单帧、64 MiB 累计根、10,000 帧、至少 1 GiB 剩余空间均为保守的有限停止额度，base64/控制记录也占磁盘，不保证可录满五分钟。WS max_queue=16 是现有实现，不是新增 plan 字段；不声称 RSS 有独立硬上限。

## 精确运行入口及网络预算

下列命令中的文件占位符必须由审批后的真实路径替换；**当前不执行**：

```powershell
& 'D:\poly\.venv\Scripts\python.exe' -B -m poly_weather market-capture --enable-capture --plan '<批准且重新冻结的绝对 plan 路径>' --capture-root 'D:\poly\collection_quarantine\bounded-trial-review-20260911-01' --config 'D:\poly\configs\settlements.json'
```

只有实际 CLI 的 `--enable-capture`、`--plan`、`--capture-root`、`--config`；不存在额外 watchdog 或 HTTP-budget flag。默认仍关闭。

| 项目 | 拟议授权边界 |
|---|---|
| HTTP | 仅 GET `https://gamma-api.polymarket.com/events/slug/{已批准的准确slug}`；无重定向/应用重试，响应上限 2 MiB |
| WS | 仅 `wss://ws-subscriptions-clob.polymarket.com/ws/market`；只订阅身份门产出的 token；最多一次 session，零重连 |
| 请求预算证明 | run_capture 每 epoch 对 S 个 selections 各 fetch 一次，失败累计 F 后退出；正常接收持续至期限，HTTP/身份错误立即退出。S=1、F=1 时 capture GET ≤1、session ≤1；一些 OSError 会更早停止。不是 schema 新增总请求计数器 |
| 前置查询 | 当前 slug 未确定，须另批准确事件的查询来源与预算；若已有人给出可核对准确 slug，可另批最多一次 exact GET。该次计入总授权预算，不能暗中增加自动发现；没有准确 slug 则先停在查询计划审查 |
| 时间 | 目标完整进程最多 300 秒，**当前仅协作式 runtime，尚不具备此硬保证**。必须先实现并离线验收外部时限/退出确认或等效方案；不能把 close_timeout=2 当总期限 |
| 下游 | 无 DB、旧 schema export、active set、正式 status、signal 配置或 promotion；weather/旧 v2/Paper 不动 |

## 单次操作清单（实施前仍须审查与授权）

1. 先关闭验收阻塞并重跑回归，再核对候选/解释器/registry/plan 指纹。只读核对现有进程与 Task Scheduler 的命令归属和状态；不启停、不接管。记录前置状态，不把旧 PID 文件当作存活证明。
2. 为这次单进程分配独立外部 attempt ID 和 stdout/stderr 证据目录（正式 data 外）。固定 TMP 锁命名空间并确认没有另一 capture；当前锁不保证不同 TMP 用户互斥。ownership 不明就停止，不删锁抢占。日志中关联内部 start.capture.json 的 run_id/attempt_id。
3. 人工运行一次已审核的单次命令，不通过 supervisor/计划任务，不自动重试。监督进程方案须覆盖 plan 读取到 child 退出，且保持窗口总上限。不得创建未审查的常驻恢复机制。
4. 结束时保存原始退出码；先判 null，再转整数。未知就是未知，不能记成功。核对自建 PID、启动时间、命令和退出事实；无法确认退出则阻断任何再次启动。记录墙钟、请求/session/failure 数、落盘大小和磁盘余量。
5. 只读核验每个 segment 的 hash-chain、receipt/epoch、身份 response→projection→binding hash，以及 result 的 sequence/digest/committed_bytes。失败/pending/不完整尾部只留证，不删、不截断、不修 cursor，不自动换根继续。
6. 对照开始时的正式 status/路径和下游归属，确认本次没有发布正式数据或启动下游。既有 weather 自然变化单独归因，不能为了逐字一致而停止 weather。

允许的 capture 输出：`capture-root.json`、`runs/<run_id>/start.capture.json`、`frames.capture.jsonl`、`collection-membership.capture.json`、`result.capture.json`；异常时可能留下 `result.pending.capture.json` 或未确认尾部，这是失败证据，不是可恢复成功。外部 attempt 日志保存在独立授权日志目录，不能塞入 capture root 的严格 allowlist。不会 gzip、删除、retention 治理或发布共享 DB。

## 成败定义与停止

成功必须同时有：准确身份绑定、至少一条真实原始 market 帧及公共响应确认持久化、hash-chain/终态核验、实际退出码 0、所有授权额度满足、独立进程确认退出、消费者隔离未破坏。CLI complete 本身不保证收到帧，也不证明硬时限，不能单独作为成功。

原文可保存而协议无法解析时只报告原文能力；健康 L2 永远不在本次成功口径内。源端无丢帧和 public group completeness 仍未证明。任何一次连接失败、持久化失败、身份变化、期限/额度触达异常或退出不确定均结束本轮；不延时、不换旧 market-stream 绕过、不改 registry、不清证据。

即便成功也退出，不转常驻。持续采集、扩大站点、正式 market/supervisor 恢复各需后续授权。

**本任务没有启动采集；用户仍需明确授权有界试采。Paper 不启动，项目 NOT SEALED。**
