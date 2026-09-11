import hashlib, json, pathlib, subprocess, sys, time
from datetime import datetime, UTC
ROOT = pathlib.Path('D:/poly')
OUT = ROOT / 'docs/raw_market_recovery_20260910'
def hashes():
    paths = list((ROOT/'src').rglob('*.py')) + list((ROOT/'tests').rglob('*.py')) + list((ROOT/'scripts').rglob('*.ps1')) + list((ROOT/'configs').glob('*.json')) + [ROOT/'pyproject.toml', ROOT/'uv.lock']
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}
before = hashes()
cmd = [sys.executable, '-c', 'import faulthandler,pytest; faulthandler.dump_traceback_later(30,repeat=True); raise SystemExit(pytest.main(["-q","-p","no:cacheprovider"]))']
start = time.monotonic()
with (OUT/'pytest.stdout.log').open('wb') as stdout, (OUT/'pytest.stderr.log').open('wb') as stderr:
    child = subprocess.Popen(cmd,cwd=ROOT,stdout=stdout,stderr=stderr)
    timed_out = False
    try:
        code = child.wait(timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,timeout=20)
        code = child.wait(timeout=20)
after = hashes()
record = dict(at=datetime.now(UTC).isoformat(),command=cmd,interpreter=sys.executable,python=sys.version,exit_code=code,timed_out=timed_out,elapsed_seconds=time.monotonic()-start,timeout_seconds=180,traceback_interval_seconds=30,source_sha256=before,after_sha256=after,candidate_unchanged=before==after)
(OUT/'preflight.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in record.items() if 'sha256' not in k}))
print((OUT/'pytest.stdout.log').read_text(errors='replace')[-1200:])
raise SystemExit(0 if code==0 and not timed_out and before==after else 1)
