"""Build non-loadable human review package from saved projections only."""
import hashlib
import json
from datetime import date
from pathlib import Path

import httpx

from poly_weather.adapters.polymarket import GammaClient
from poly_weather.config import load_settlement_registry
from poly_weather.market_supervisor import discover_event
from poly_weather.settlement import parse_settlement_evidence, verify_settlement_evidence

ROOT=Path('D:/poly')
SAVED=ROOT/'docs/strict_rejection_query_20260910T091217Z'
registry=load_settlement_registry(ROOT/'configs/settlements.json')
rows=[]
for spec in registry.specs:
    path=SAVED/f'{spec.station_id}.response.json'
    payload=json.loads(path.read_text(encoding='utf-8'))
    with httpx.Client(base_url='https://gamma-api.polymarket.com',transport=httpx.MockTransport(lambda request: httpx.Response(200,json=payload,request=request))) as client:
        event=discover_event(GammaClient(client=client),spec,date(2026,9,10))
    evidence=parse_settlement_evidence(event,registry_spec=spec)
    verification=verify_settlement_evidence(evidence,spec)
    rows.append(dict(review_status='pending_human_review',station_id=spec.station_id,
        event_id=event.event_id,event_slug=event.event_slug,observation_date=str(evidence.target_date),
        query_scope='2026-09-10T09:12:17Z/2026-09-10T09:12:29Z; saved public projection, not original failed attempts',
        source_projection=str(path.relative_to(ROOT)),source_projection_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        old_spec=spec.model_dump(mode='json'),proposed_contract=evidence.rule_contract.model_dump(mode='json'),
        proposed_finalization_rule=evidence.finalization_rule.value,parsed_evidence=evidence.model_dump(mode='json'),
        old_registry_verification=verification.model_dump(mode='json'),
        proposed_effective_from=None,approved_event_scope=None,reviewer=None))
candidate=dict(document_kind='human_review_proposal_NOT_SettlementRegistry',review_status='pending_human_review',
    production_loadable=False,registry_sha256=hashlib.sha256((ROOT/'configs/settlements.json').read_bytes()).hexdigest(),entries=rows)
(ROOT/'docs/settlement_registry_candidate_20260910.json').write_text(json.dumps(candidate,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# Registry 人工审核包','','状态：pending human review。候选不是 SettlementRegistry 格式，默认 loader 不会发现它；手动传给 loader 也应拒绝。未改变生效 registry。','',
'最低审核项必须分别回答，不能用一句“同意更新”代替：','',
'1. 观察日历日期在 America/New_York 截止规则中的日期基准，尤其中国/西海岸站点。',
'2. 23:59 的秒边界/包含关系；当前仅表示文本给出的分钟，不构造 23:59:59。',
'3. 备用 Wunderground 的具体 URL、站点绑定、Daily Observations 产品；源文本未给的信息不得从 registry 补。',
'4. NOAA unavailable 与 confirmed absent 的区别、切换条件、备用仍缺时 no data 的来源集合。',
'5. 最低桶处置是否只在确认主备均无数据时适用；不把查询失败当无数据。',
'6. deadline 先于次日首次发布时，独立修订截止是否继续；当前记录冲突，不选 min。',
'7. 逐站主来源产品，生效时间和获准事件范围；旧证据不迁移。','',
'|站点|事件|主表|备用表|未决项|','|---|---|---|---|---|']
for row in rows:
    c=row['proposed_contract']
    lines.append(f"|{row['station_id']}|{row['event_id']}|{c['primary']['table']}|{c['fallback']['table']}|{', '.join(c['unresolved'])}|")
lines += ['','每站 description 原文的字符区间与文本在 candidate.entries[].proposed_contract.clauses；源文件 hash、旧值、新提议、解析 evidence 与逐项 expected/actual 同时保留。区间定位针对投影 description 字符，不是 wire 字节偏移。',
'','真实十站因未决语义保持 INCOMPLETE / rejected；机器解析成功不代表审核批准。测试中显式构造的 reviewed 契约只用于无歧义比较，不写入本候选。']
(ROOT/'docs/settlement_registry_review_20260910.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('10 pending review entries; production config untouched')
