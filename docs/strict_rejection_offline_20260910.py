"""Offline evidence diagnostic only: no HTTP client, daemon or formal-data writes."""
import hashlib
import json
import re
from pathlib import Path

from poly_weather.adapters.polymarket import EventSnapshot
from poly_weather.config import load_settlement_registry
from poly_weather.domain import Market
from poly_weather.settlement import parse_settlement_evidence, verify_settlement_evidence

ROOT = Path('D:/poly')
OUT = ROOT / 'docs/strict_rejection_offline_20260910'
OUT.mkdir(exist_ok=False)
registry = load_settlement_registry(ROOT / 'configs/settlements.json')
paths = sorted((ROOT / 'data/raw/polymarket_gamma_event/2026-09-04').glob('events.jsonl'))
selected = {}
for path in paths:
    with path.open('rb') as stream:
        for number, line in enumerate(stream, 1):
            row = json.loads(line)
            payload = row['payload']
            for spec in registry.specs:
                if re.fullmatch(spec.market_slug_pattern, payload.get('slug', '')):
                    selected[spec.key] = (spec, row, number, hashlib.sha256(line).hexdigest())
results = []
for key, (spec, row, number, digest) in selected.items():
    payload = row['payload']
    # Preserve public rule and contract fields only; do not retain creator/profile fields.
    public = {k: payload[k] for k in ('id', 'slug', 'title', 'description', 'resolutionSource') if k in payload}
    public['markets'] = [{k: m[k] for k in ('id', 'slug', 'question', 'description', 'resolutionSource', 'outcomes', 'clobTokenIds', 'groupItemTitle', 'active', 'closed') if k in m} for m in payload.get('markets', [])]
    record = dict(station_id=spec.station_id, settlement_key=key, event_id=payload.get('id'),
                  fetched_at=row['fetched_at'], historical_only=True, origin_line=number,
                  origin_line_sha256=digest, registry_expected=spec.model_dump(mode='json'), source_fields=public)
    try:
        markets = []
        for item in payload.get('markets', []):
            enriched = dict(item)
            for field in ('category', 'description', 'resolutionSource'):
                enriched.setdefault(field, payload.get(field))
            markets.append(Market.from_gamma(enriched))
        event = EventSnapshot(request_url=row['request_url'], fetched_at=row['fetched_at'],
                              event_id=str(payload['id']), event_slug=payload['slug'], title=payload.get('title', ''),
                              resolution_source=payload.get('resolutionSource'), raw_payload=payload, markets=tuple(markets))
        record['stage'] = 'parse'
        evidence = parse_settlement_evidence(event, registry_spec=spec)
        record['parsed'] = evidence.model_dump(mode='json')
        record['local_date'] = str(evidence.target_date)
        record['stage'] = 'verification'
        record['verification'] = verify_settlement_evidence(evidence, spec).model_dump(mode='json')
    except Exception as exc:
        record.setdefault('stage', 'snapshot_conversion')
        record['exception'] = dict(type=type(exc).__name__, message=str(exc))
    results.append(record)
inventory = {name: [p.name for p in sorted((ROOT/'data/raw'/name).iterdir()) if p.is_dir()] for name in ('polymarket_gamma_event','polymarket_gamma_search','polymarket_gamma_markets','settlement_evidence')}
hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'configs/settlements.json', ROOT/'src/poly_weather/settlement.py', ROOT/'src/poly_weather/cli.py', ROOT/'src/poly_weather/adapters/polymarket.py', ROOT/'src/poly_weather/modeling.py')}
(OUT/'evidence.json').write_text(json.dumps(dict(scope='Latest saved September 4 historical event per registry key; NOT September 10 attempt responses', archive_inventory=inventory, source_hashes=hashes, results=results), ensure_ascii=False, indent=2), encoding='utf-8')
for r in results:
    print(r['station_id'], r['event_id'], r['stage'], r.get('verification', {}).get('failures', r.get('exception')))
