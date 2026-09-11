# 严格核验拒绝原因：离线诊断

状态：历史样本根因已复现；2026-09-10 三次 attempt 的具体原因仍 UNKNOWN，等待单独授权有界公开查询。没有修改生产代码、registry 或严格核验，没有网络请求、daemon 操作或正式 data 写入。

## 当前 attempt 的证据缺口

三次均发现 10 个候选并全部拒绝，实际退出码 2。attempt 目录仅包含 start/result manifest、空 stdout 和汇总 stderr，没有候选响应。检查现有 Gamma event / search / markets 和 settlement_evidence 归档目录：event/evidence 最晚为 9 月 4 日，search/markets 为 8 月 22 日，没有本次 9 月 10 日响应。

源码确认：CLI 初始发现取得的 EventSnapshot 只留在内存；解析异常和核验不通过均计入 rejected_count。仅在 initial 非空后才构造 bot/supervisor 并进入归档路径。GammaClient 没有自动持久化响应。因此不能从当前日志还原每个 event ID 或具体 reasons。

`strict_rejection_offline_20260910/attempt_candidates_missing.json` 保存 30 行待补证据：站点与当地日期来自 registry 迭代及 attempt 时间推导；event ID、解析阶段、reasons 明确为 null/UNKNOWN，绝不拿历史 ID 顶替。

## 已复现的历史原因

只读扫描 `data/raw/polymarket_gamma_event/2026-09-04/events.jsonl`，为每个 registry key 选取该文件最后一条事件，共十站。十条目标当地日期均为 **2026-09-05**。通过现有 `Market.from_gamma → EventSnapshot → parse_settlement_evidence → verify_settlement_evidence` 离线执行，未创建 HTTP client 或数据库。

| 站点 | 历史 event ID | 解析 | 核验 failures |
|---|---|---|---|
| KLGA | 958036 | COMPLETE | finalization_known, finalization_exact |
| KORD | 958200 | COMPLETE | finalization_known, finalization_exact |
| KLAX | 958834 | COMPLETE | finalization_known, finalization_exact |
| KMIA | 958039 | COMPLETE | finalization_known, finalization_exact |
| KATL | 958038 | COMPLETE | finalization_known, finalization_exact |
| KDAL | 958037 | COMPLETE | finalization_known, finalization_exact |
| KHOU | 958042 | COMPLETE | finalization_known, finalization_exact |
| KSEA | 958833 | COMPLETE | finalization_known, finalization_exact |
| ZUCK | 957697 | COMPLETE | finalization_known, finalization_exact |
| ZUUU | 957708 | COMPLETE | finalization_known, finalization_exact |

逐事件站点、当地日期、源字段、registry 全部预期值、parsed evidence、checks/failures/reason、来源行号及原始行 SHA-256 保存于 `strict_rejection_offline_20260910/evidence.json`。仅保留公开规则与合约字段，不复制 creator/profile 字段；源记录本身只读保留。当前源码和 registry 指纹一并保存。诊断脚本为 `docs/strict_rejection_offline_20260910.py`，输出目录若存在即拒绝覆盖；再次复现应使用新的输出目录。

历史源文本的 finalization 条款：

> This market will resolve once the first data point for the following date has been published on the resolution source, or by 11:59 PM ET on the day following the observation date, whichever comes first.

字段对照：

- 解析器 `_NEXT_DAY_RE` 要求旧句式 `can not resolve until the first data point for the following date has been published`，新文本不匹配。
- `_FINALIZED_RE` 也不匹配该条款，最终输出 `FinalizationRule.UNKNOWN`。
- registry 十站均要求 `first_next_day_observation`，故 `finalization_known=false`、`finalization_exact=false`，其余现有 checks 通过。
- 新文本另有 NOAA 缺数时转 Weather Underground、仍无数据时结算最低桶的条款；现有 checks 通过不代表这些附加条款已被完整建模。

**历史样本确定原因是源规则文本/语义与现有解析及 registry 表达不兼容。** 这不是解析抛异常；也不能简单判成正则 bug。即使扩展句式匹配，把“首条数据或时间截止，以较早者为准”映射成原单一规则仍会丢失语义。当前不提出让其直接通过的修复。

这些历史事件不属于 9 月 10 日的三次 attempt；历史同型失败只能作为当前原因的候选解释。没有本次响应，不能把十站历史结果升级为当前根因结论。

## 待授权的有界公开查询

建议单独授权一次诊断查询，明确边界如下：

1. 固定原 attempt 的十站目标当地日期 **2026-09-10**；每站只调用一次已有 Gamma `/public-search` 参数路径，最多 10 个 HTTP GET，单站不翻页、不重试、不跟随 HTTP 重定向，总计最多 180 秒、单次超时 15 秒。到期立即停止并保留已完成证据。
2. 不运行 CLI market-supervisor，不启动任何 daemon、不订阅 WebSocket、不打开正式数据库；仅写新的 `docs/strict_rejection_query_<timestamp>/`。
3. 每个请求先保存脱敏 public rule/contract 响应及 receipt 时间、查询参数、内容 hash；按生产转换与严格匹配逻辑记录所有候选/歧义/无候选，再独立记录 snapshot conversion、parse exception、parse missing_fields 和 verification reasons。源字段与 registry 预期值并排保存，重复离线执行验证结果一致。
4. 查询失败、无候选或歧义均如实记录，不追加 event detail 请求、改 registry、网络环境或检查闸门。新响应只能证明查询时状态，不能冒充原三次响应。

获得这些证据后再决定最小修复及回归范围。market 恢复仍需另行授权。此次未重跑完整套件，因为没有改动产品代码或测试；仅执行离线诊断脚本，退出码 0，十条生产解析/核验结果已保存。此前 753 passed 不代表本次原因已解决。
