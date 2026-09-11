"""Bounded regression, evidence output only; no daemon operations."""
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path('D:/poly')
OUT=ROOT/'docs/settlement_rule_upgrade_20260910'
paths=list((ROOT/'src').rglob('*.py'))+list((ROOT/'tests').rglob('*.py'))+list((ROOT/'configs').glob('*.json'))
before={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
cmd=[sys.executable,'-c','import faulthandler,pytest; faulthandler.dump_traceback_later(30,repeat=True); raise SystemExit(pytest.main(["-q","-p","no:cacheprovider","--tb=short","-p","user_tmp_plugin"]))']
started=time.monotonic()
with (OUT/'pytest.stdout.log').open('wb') as stdout,(OUT/'pytest.stderr.log').open('wb') as stderr:
    proc=subprocess.Popen(cmd,cwd=ROOT,stdout=stdout,stderr=stderr)
    timeout=False
    try:
        code=proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        timeout=True
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=20)
        code=proc.wait(timeout=20)
after={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
record=dict(command=cmd,exit_code=code,timeout=timeout,timeout_seconds=180,elapsed_seconds=time.monotonic()-started,python=sys.version,source_sha256=before,unchanged=before==after)
(OUT/'full_regression.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print((OUT/'pytest.stdout.log').read_text(errors='replace')[-6000:])
raise SystemExit(code)
