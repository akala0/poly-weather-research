"""One bounded authorized public query; no daemon or formal data writes."""
import asyncio
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from poly_weather.adapters.polymarket import GammaClient
from poly_weather.config import load_settlement_registry
from poly_weather.market_supervisor import _city_query
from poly_weather.settlement import parse_settlement_evidence, verify_settlement_evidence

ROOT = Path('D:/poly')
OUT = ROOT / ('docs/strict_rejection_query_' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ'))
OUT.mkdir(exist_ok=False)
FIELDS = ('id', 'slug', 'title', 'question', 'description', 'resolutionSource', 'category',
          'conditionId', 'active', 'closed', 'endDate', 'outcomes', 'outcomePrices', 'clobTokenIds')

def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def public_event(event):
    if not isinstance(event, dict):
        return event
    result = {k: v for k, v in event.items() if k in FIELDS}
    if 'markets' in event:
        result['markets'] = [public_event(m) for m in event['markets']]
    return result

def replay(payload, query, spec):
    # Existing production adapter runs against an in-memory transport only.
    with httpx.Client(base_url='https://gamma-api.polymarket.com', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload, request=request))) as client:
        with GammaClient(client=client) as gamma:
            page = gamma.search_markets_page(query=query, limit=50)
    candidates = [e for e in page.events if re.fullmatch(spec.market_slug_pattern, e.event_slug)
                  and e.event_slug.endswith('-september-10-2026')]
    result = dict(candidate_count=len(candidates), ambiguous=len(candidates)>1, candidates=[])
    for event in candidates:
        row = dict(event_id=event.event_id, event_slug=event.event_slug, stage='parse')
        try:
            evidence = parse_settlement_evidence(event, registry_spec=spec)
            row['parsed'] = evidence.model_dump(mode='json')
            row['stage'] = 'verification'
            row['verification'] = verify_settlement_evidence(evidence, spec).model_dump(mode='json')
        except Exception as exc:
            row['exception'] = dict(type=type(exc).__name__, message=str(exc))
        result['candidates'].append(row)
    return result

async def main():
    specs = load_settlement_registry(ROOT/'configs/settlements.json').specs
    assert len(specs) == 10
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in
              [ROOT/'configs/settlements.json', ROOT/'src/poly_weather/settlement.py',
               ROOT/'src/poly_weather/adapters/polymarket.py', ROOT/'src/poly_weather/modeling.py',
               ROOT/'src/poly_weather/market_supervisor.py']}
    save('scope.json', dict(started_at=datetime.now(UTC).isoformat(), max_gets=10,
                           total_seconds=180, per_request_seconds=15, retries=0,
                           redirects=False, target_date='2026-09-10', source_sha256=hashes))
    began = time.monotonic()
    results = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=False,
        headers={'User-Agent':'poly-weather/0.1 (research; read-only)'}) as client:
        for spec in specs:
            remaining = 180 - (time.monotonic()-began)
            if remaining <= 0:
                break
            query = f'highest temperature in {_city_query(spec)} on September 10'
            params = dict(q=query, events_status='active', limit_per_type=50, page=1,
                          keep_closed_markets=0, search_tags='false', search_profiles='false')
            row = dict(station_id=spec.station_id, local_date='2026-09-10', params=params,
                       requested_at=datetime.now(UTC).isoformat(), registry_expected=spec.model_dump(mode='json'))
            save(f'{spec.station_id}.request.json', row)
            try:
                response = await asyncio.wait_for(client.get('https://gamma-api.polymarket.com/public-search', params=params), timeout=min(15,remaining))
                row.update(received_at=datetime.now(UTC).isoformat(), status_code=response.status_code,
                           wire_sha256=hashlib.sha256(response.content).hexdigest(), wire_bytes=len(response.content))
                response.raise_for_status()
                payload = response.json()
                sanitized = dict(events=[public_event(e) for e in payload.get('events',[])], pagination=payload.get('pagination',{}))
                name = f'{spec.station_id}.response.json'
                save(name, sanitized)
                row['saved_response_sha256'] = hashlib.sha256((OUT/name).read_bytes()).hexdigest()
                original_result = replay(payload, query, spec)
                replayed = replay(json.loads((OUT/name).read_text(encoding='utf-8')), query, spec)
                row['diagnosis'] = replayed
                row['saved_replay_matches_original'] = original_result == replayed
            except Exception as exc:
                row['error_type'] = type(exc).__name__
                # Do not export proxy credentials or arbitrary transport exception text.
            results.append(row)
            save(f'{spec.station_id}.result.json', row)
            print(spec.station_id, row.get('status_code'), row.get('diagnosis',row.get('error_type')), flush=True)
    save('summary.json', dict(finished_at=datetime.now(UTC).isoformat(), elapsed_seconds=time.monotonic()-began,
                             network_get_count=len(results), results=results,
                             source_unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())))
    print('EVIDENCE_DIRECTORY',OUT,flush=True)

asyncio.run(main())
