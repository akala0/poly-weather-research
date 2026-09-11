# Registry 人工审核包

状态：pending human review。候选不是 SettlementRegistry 格式，默认 loader 不会发现它；手动传给 loader 也应拒绝。未改变生效 registry。

最低审核项必须分别回答，不能用一句“同意更新”代替：

1. 观察日历日期在 America/New_York 截止规则中的日期基准，尤其中国/西海岸站点。
2. 23:59 的秒边界/包含关系；当前仅表示文本给出的分钟，不构造 23:59:59。
3. 备用 Wunderground 的具体 URL、站点绑定、Daily Observations 产品；源文本未给的信息不得从 registry 补。
4. NOAA unavailable 与 confirmed absent 的区别、切换条件、备用仍缺时 no data 的来源集合。
5. 最低桶处置是否只在确认主备均无数据时适用；不把查询失败当无数据。
6. deadline 先于次日首次发布时，独立修订截止是否继续；当前记录冲突，不选 min。
7. 逐站主来源产品，生效时间和获准事件范围；旧证据不迁移。

|站点|事件|主表|备用表|未决项|
|---|---|---|---|---|
|KLGA|987074|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KORD|987248|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KLAX|987929|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KMIA|987247|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KATL|987076|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KDAL|987075|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KHOU|987250|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|KSEA|987928|Hourly Data / Temp|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline|
|ZUCK|986731|None|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline, primary_table_missing|
|ZUUU|986734|None|Daily Observations|deadline_second_boundary, deadline_date_basis_review, fallback_url_missing, fallback_station_missing, unavailable_vs_absent, no_data_source_scope, revision_after_settlement_deadline, primary_table_missing|

每站 description 原文的字符区间与文本在 candidate.entries[].proposed_contract.clauses；源文件 hash、旧值、新提议、解析 evidence 与逐项 expected/actual 同时保留。区间定位针对投影 description 字符，不是 wire 字节偏移。

真实十站因未决语义保持 INCOMPLETE / rejected；机器解析成功不代表审核批准。测试中显式构造的 reviewed 契约只用于无歧义比较，不写入本候选。
