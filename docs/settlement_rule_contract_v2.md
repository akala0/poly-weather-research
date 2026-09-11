# 结算规则契约 v2

本契约只描述公开文本，不执行结算。schema=2、parser=3、semantic_version=deadline-fallback-v2；旧证据可读但不得用旧缓存身份替代新核验。

十站分别使用已保存投影的 description 与 resolutionSource，禁止以一站替换另一站。逐站证据定位及字段对照由本轮 validation/candidate 中的 clause offsets、event ID、投影 hash 提供。

| 原条款定位 | 字段 | 时间/来源作用域 | 核验 | 未决事项 |
|---|---|---|---|---|
| title/slug、recorded by NOAA at | 原 evidence identity/station/date/unit/buckets | 市场当地观察日 | 原逐项检查 | timezone 仍仅在源站点匹配时取 registry，不能据此补规则语义 |
| The resolution source ... NOAA / Hourly Data | primary | NOAA URL station、主数据产品 | 独立 source/table 比较 | 中国站无明确表名则 unknown |
| If NOAA data ... unavailable ... Weather Underground | fallback | 主来源观察日数据缺失，截止时切换 | 名称、URL、station、table、条件 | 文本未给备用 URL/station；unavailable 不等同 HTTP failure |
| resolve once ... whichever comes first | settlement_trigger | 来源的次日首次发布与 deadline 较早者 | 独立关系 | 发布时刻不同于 observation/receipt |
| 11:59 PM ET ... day following observation date | deadline | 观察日历日期+1，23:59 America/New_York | 日期基准、天数、clock、timezone、precision | 秒边界未给；跨观察时区日期基准待审核 |
| no data ... lowest bracket | no_data | 主→备用→仍无数据的有序规则 | 最低桶由合法边界排序得到 | 文本未充分限定 no data 的来源集合；不可用网络失败触发 |
| Revisions ... until first datapoint ... | revision_trigger | 独立次日首次源发布 | 不改成 min | deadline 先到时与结算触发冲突待审核 |

准入：新规则必须具有无缺失、无冲突的完整契约，逐字段等于明确 reviewed 的预期，且 registry 原 VERIFIED 闸门也通过。真实十站存在未决字段，预期仍拒绝。pending 候选无准入资格。旧语义仍单独解析；关键 finalization unknown 不再 COMPLETE。

规则身份纳入 parser/schema/结构化契约、原文本、完整桶身份；格式归一后的语义可一致，但原文证据 hash 可不同。历史对象不迁移、不补字段。SAME_STATION_NOAA 只影响信号观测来源政策，不豁免新结算契约。

## 逐站条款定位

以下均为 2026-09-10 当地观察日，源查询窗口 09:12:17–09:12:29 UTC。每行使用自己的 response.json；精确 description 字符区间位于候选对应 entries[].proposed_contract.clauses，不共享一站的文本作为证据。

|站点|event ID|主来源/表|单位|观察时区（仅 registry-assisted）|规则条款位置及审核|
|---|---|---|---|---|---|
|KLGA|987074|NOAA / Hourly Data Temp|F|America/New_York|KLGA.response.json description；deadline/fallback/no_data/settlement/revision 五项独立定位|
|KORD|987248|NOAA / Hourly Data Temp|F|America/Chicago|KORD.response.json 同名五项，独立核验|
|KLAX|987929|NOAA / Hourly Data Temp|F|America/Los_Angeles|KLAX.response.json 同名五项，观察日与 ET 日期基准待审|
|KMIA|987247|NOAA / Hourly Data Temp|F|America/New_York|KMIA.response.json 同名五项，独立核验|
|KATL|987076|NOAA / Hourly Data Temp|F|America/New_York|KATL.response.json 同名五项，独立核验|
|KDAL|987075|NOAA / Hourly Data Temp|F|America/Chicago|KDAL.response.json 同名五项，独立核验|
|KHOU|987250|NOAA / Hourly Data Temp|F|America/Chicago|KHOU.response.json 同名五项，独立核验|
|KSEA|987928|NOAA / Hourly Data Temp|F|America/Los_Angeles|KSEA.response.json 同名五项，观察日与 ET 日期基准待审|
|ZUCK|986731|NOAA / 表未明示|C|Asia/Shanghai|ZUCK.response.json 同名五项，额外主表/跨日期基准未决|
|ZUUU|986734|NOAA / 表未明示|C|Asia/Shanghai|ZUUU.response.json 同名五项，额外主表/跨日期基准未决|

所有站的备用 Daily Observations 只归属于 Weather Underground。未从主 URL 或 registry 拼造备用 URL/站点。期限名义分钟的日历计算与 IANA DST 可测试，但 boundary unresolved 时比较函数返回 UNKNOWN，不授权结算。
