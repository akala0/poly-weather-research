# 严格核验有界查询结果

查询时间：2026-09-10 09:12:17–09:12:29 UTC。10 次 GET，全部 HTTP 200，11.69 秒；无重试、重定向、分页或额外请求。未操作 daemon、正式 data、生产代码或 registry。

## 结论

十站各发现一个 2026-09-10 候选；全部解析 COMPLETE，无解析异常，唯一失败项均为 finalization_known / finalization_exact。现有解析器输出 unknown，registry 预期 first_next_day_observation。保存的脱敏响应经现有 GammaClient 的内存 transport 重放，十站均与原响应诊断完全一致。源码及 registry 查询前后 SHA-256 一致。

这是当前查询时点的可复现根因，不是原三次未保存响应的追溯证明。

| 站点 | event ID | 当地日期 | 结果 |
|---|---|---|---|
| KLGA | 987074 | 2026-09-10 | finalization_known / finalization_exact |
| KORD | 987248 | 2026-09-10 | finalization_known / finalization_exact |
| KLAX | 987929 | 2026-09-10 | finalization_known / finalization_exact |
| KMIA | 987247 | 2026-09-10 | finalization_known / finalization_exact |
| KATL | 987076 | 2026-09-10 | finalization_known / finalization_exact |
| KDAL | 987075 | 2026-09-10 | finalization_known / finalization_exact |
| KHOU | 987250 | 2026-09-10 | finalization_known / finalization_exact |
| KSEA | 987928 | 2026-09-10 | finalization_known / finalization_exact |
| ZUCK | 986731 | 2026-09-10 | finalization_known / finalization_exact |
| ZUUU | 986734 | 2026-09-10 | finalization_known / finalization_exact |

## 源规则与预期值

每站源条款及 registry 全字段分别保存在对应 response.json / result.json。以下抽取实际匹配候选的结算相关段落：

> If NOAA data for the observation date is unavailable by 11:59 PM ET on the day following the observation date, the Weather Underground Daily Observations table will be used as the resolution source.
> In the event that there is no data for the observation date by 11:59 PM ET on the day following the observation date, this market will resolve to the lowest bracket.
> This market will resolve once the first data point for the following date has been published on the resolution source, or by 11:59 PM ET on the day following the observation date, whichever comes first.
> Revisions to temperatures recorded within this market's timeframe will be considered until the first datapoint for the following date has been published, after which any alterations will not be considered.

## 最小修复方向（本轮未实施）

不能只扩展正则后映射成旧枚举。应完整表达次日首条数据与次日 23:59 America/New_York 截止时间的较早者、NOAA 缺数后的 Wunderground fallback、仍无数据时最低桶结算，以及修订截止规则。先明确这些条款的版本化字段和严格对照，再人工审核对应 registry；未知或不完整继续拒绝。

另一个源码风险：description 出现备用源的 Daily Observations 时，当前 observation_table 优先分支会将 NOAA 主来源也标成 Daily Observations；该字段不在现有严格 checks 内。这不是本轮直接拒绝原因，但说明新增 fallback 不能被忽略。

诊断证据持久化应在初始核验之前完成，并区分转换异常、解析异常和核验 failures。修复范围应先限于这些契约及生产路径反例，完整回归后再单独授权 market 恢复。本次没有修改或放宽检查。

## 证据

本目录含逐站请求、脱敏响应、结果，summary.json 汇总全部 checks、parsed evidence、registry 预期、hash、receipt 与复现一致性。wire_sha256 是原响应字节哈希；仅保存公开合约字段投影，未保存原响应字节本体。保留字段覆盖现有转换和核验所读取字段，且原响应与投影结果已经对照。

生产源码未修改，本轮验证为十站真实响应的重复离线复现，不将此前 753 passed 当作未来修复的回归结果。market 恢复、Paper 或下游启动均未执行。
