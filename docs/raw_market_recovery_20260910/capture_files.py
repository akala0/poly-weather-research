import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('D:/poly')
OUT = ROOT / 'docs/raw_market_recovery_20260910'
label = sys.argv[1]
runtime = ROOT / 'data/runtime'
protected = set()
for pattern in ('shadow*.json', 'signal*.json', 'paper*'):
    protected.update(runtime.glob(pattern))
protected.update((ROOT / 'data/raw/shadow_orders').glob('*'))
protected.update((ROOT / 'configs').glob('*.json'))
for folder in (ROOT / 'data', ROOT / 'data/raw'):
    protected.update(folder.glob('paper*'))

def stat(p):
    s = p.stat()
    return dict(size=s.st_size, mtime_ns=s.st_mtime_ns)

hashes = {}
for p in sorted(protected):
    if p.is_file():
        before = stat(p)
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        hashes[p.relative_to(ROOT).as_posix()] = dict(before=before, after=stat(p), sha256=digest)
logs = {}
for p in sorted((ROOT / 'data/logs/daemons').glob('market-supervisor.*log*')):
    before = stat(p)
    with p.open('rb') as f:
        offset = max(0, before['size'] - 65536)
        f.seek(offset)
        content = f.read(65536)
    (OUT / f'{label}.{p.name}.tail').write_bytes(content)
    logs[p.name] = dict(before=before, after=stat(p), offset=offset, length=len(content), sha256=hashlib.sha256(content).hexdigest())
raw = {}
today = datetime.now(UTC).date().isoformat()
for source in (ROOT / 'data/raw').iterdir():
    p = source / today / 'events.jsonl'
    if p.is_file():
        raw[p.relative_to(ROOT).as_posix()] = stat(p)
for name in ('weather_daemon_status.json', 'polymarket_ws_status.json', 'market_supervisor_status.json'):
    p = runtime / name
    if p.exists():
        (OUT / f'{label}.{name}').write_bytes(p.read_bytes())
record = dict(at=datetime.now(UTC).isoformat(), protected=hashes, log_ranges=logs, today_raw=raw,
              unresolved_exists=(ROOT/'data/logs/daemons/market-supervisor.child-unresolved.json').exists())
(OUT / f'files.{label}.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(dict(label=label, protected_files=len(hashes), unresolved=record['unresolved_exists'], raw=raw)))
