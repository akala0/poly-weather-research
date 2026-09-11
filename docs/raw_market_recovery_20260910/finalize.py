import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

root = Path('D:/poly')
out = root / 'docs/raw_market_recovery_20260910'
def read(name):
    return json.loads((out / name).read_text(encoding='utf-8-sig'))
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
pre = read('preflight.json')
before = read('files.before.json')
after = read('files.after.json')
process_before = read('processes.before.json')
process_after = read('processes.after.json')
start = read('supervision.start.json')
result = read('supervision.result.json')
attempts = []
for folder in sorted((root / 'data/logs/daemons/attempts/market-supervisor').iterdir()):
    r = json.loads((folder / 'attempt-result.json').read_text(encoding='utf-8-sig'))
    if r['started_at'] < start['started_at']:
        continue
    target = out / 'attempts' / folder.name
    shutil.copytree(folder, target)
    for name in ('stdout', 'stderr'):
        assert sha(folder / f'{name}.log') == r[name]['sha256']
    attempts.append(r)
protected_equal = before['protected'].keys() == after['protected'].keys() and all(
    row['sha256'] == after['protected'][name]['sha256'] for name, row in before['protected'].items())
weather_ids = (4524, 17784, 17912, 17944)
weather_equal = all(next(p for p in process_before if p['ProcessId'] == pid) ==
                    next(p for p in process_after if p['ProcessId'] == pid) for pid in weather_ids)
weather_xml_equal = ET.tostring(ET.fromstring((out/'PolyWeather-weather-stream.before.xml').read_text(encoding='utf-8-sig'))) == ET.tostring(ET.fromstring((out/'PolyWeather-weather-stream.after.xml').read_text(encoding='utf-8-sig')))
current_hashes = {name: sha(root/name) for name in pre['source_sha256']}
candidate_equal = current_hashes == pre['source_sha256']
diff = subprocess.run(['git', '-c', 'safe.directory=D:/poly', 'diff', '--check'], cwd=root, capture_output=True, text=True, timeout=15)
(out/'diff-check.final.log').write_text(diff.stdout+diff.stderr, encoding='utf-8')
status = subprocess.run(['git', '-c', 'safe.directory=D:/poly', 'status', '--short'], cwd=root, capture_output=True, text=True, timeout=15)
(out/'git-status.after.txt').write_text(status.stdout, encoding='utf-8')
db = {p.name: dict(size=p.stat().st_size, mtime=datetime.fromtimestamp(p.stat().st_mtime, UTC).isoformat()) for p in (root/'data').glob('market_stream.duckdb*') if p.is_file()}
market_status_equal = {name: sha(out/f'before.{name}') == sha(out/f'after.{name}') for name in ('polymarket_ws_status.json', 'market_supervisor_status.json')}
assert len(attempts) == 3 and all(r['exit_code'] == 2 and r['child_exit_confirmed'] for r in attempts)
assert protected_equal and weather_equal and weather_xml_equal and candidate_equal and diff.returncode == 0
assert len(process_after) == 4 and not after['unresolved_exists']
validation = dict(
    as_of=datetime.now(UTC).isoformat(), outcome='NOT_RECOVERED_STOPPED_AT_THREE_FAILURES',
    market_running=False, market_enabled=False, unconfirmed_market_children=0,
    weather_same_process_chain=weather_equal, weather_task_config_unchanged=weather_xml_equal,
    downstream_stopped_disabled=True, paper_started=False,
    head=(out/'head.txt').read_text(encoding='utf-8-sig').strip(),
    candidate_sha256=pre['source_sha256'], candidate_unchanged_through_handoff=candidate_equal,
    full_default_suite={k:v for k,v in pre.items() if 'sha256' not in k},
    test_result='753 passed in 50.03s; no skips',
    static_checks=dict(ruff_candidate=read('ruff-candidate.json'), runner_parse_errors=[], diff_check_exit=diff.returncode,
        broad_ruff_exit=1, broad_ruff_note='Three lint findings in existing .claude worktree and new evidence-only preflight helper; production src/tests/scripts scoped check passed. No fixes applied.'),
    supervision_start=start, supervision_result=result,
    tasks_before=read('tasks.before.json'), tasks_after=read('tasks.after.json'),
    processes_before=process_before, processes_after=process_after, new_runner=read('new-runner.json'),
    attempts=attempts,
    raw_acceptance=dict(passed=False, new_run_id=None, subscribed_tokens=0, full_books=0, supervisor_cycles=0,
        continuous_collection_seconds=0, new_published_positions=0, queue_metrics=None,
        reason='All attempts exited before constructing MarketWebSocketBot/MarketEventSupervisor; historical status files are not current run evidence.'),
    isolation=dict(protected_file_count=len(before['protected']), protected_sha256_equal=protected_equal,
        protected_evidence=['files.before.json','files.after.json'], marker_exists=False,
        market_status_byte_equal=market_status_equal, weather_before=before['today_raw'], weather_after=after['today_raw'],
        market_db_final_stat=db, database_note='mtime predates operation; no full database hash or writable DB probe. CLI exited before sink/DB initialization.',
        retention_called=False, signal_publication_called=False, explicit_db_maintenance_called=False,
        paper_outputs='No selected Paper output appeared; no Paper process observed; initial strict rejection never reached collection constructors.'),
    write_scope=dict(whitelist='write-whitelist.json', actual_market_writes='runner/attempt logs and manifests; unresolved ownership marker created and removed by runner after confirmed exits',
        new_market_raw_files=False, weather_writes='Existing external weather writer continued; not caused by recovery operations'),
    disk=read('disk.after.json'),
    limitations=['Per-event rejection reasons and caught parse/verification exception details are not retained by current initial-discovery diagnostics. Root cause remains unknown; no extra network probes or code fixes authorized.',
        '10 minute continuity, two reconcile cycles, book coverage, committed raw progress and queue/DB operational acceptance were not achieved.',
        'No long-term supervision was installed; all three authorized target tasks are disabled.'],
    evidence_directory=str(out), project_status='NOT SEALED; public group completeness UNSUPPORTED; Paper N=0; PnL=N/A')
(root/'docs/raw_market_recovery_validation_20260910.json').write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(dict(protected_equal=protected_equal, weather_equal=weather_equal, weather_xml_equal=weather_xml_equal, candidate_equal=candidate_equal, market_status_equal=market_status_equal, attempts=len(attempts)), ensure_ascii=False))
