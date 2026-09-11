"""Execute the actual runner with fake children; never launch a collector."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell runner")
@pytest.mark.parametrize("scenario", ["null_exit", "wait_alive", "wait_unknown", "wait_exited"])
def test_runner_child_faults(tmp_path, scenario):
    source = Path(__file__).resolve().parents[1] / "scripts/windows/poly-weather-daemon-runner.ps1"
    runner = tmp_path / "runner.ps1"
    runner.write_text(
        source.read_text().replace("Global\\PolyWeather.", f"Local\\Test.{tmp_path.name}."),
        encoding="utf-8",
    )
    exe = tmp_path / ".venv/Scripts/poly-weather.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    harness = tmp_path / "harness.ps1"
    harness.write_text(r'''
param($Root, $Scenario)
$global:launches = 0
function Start-Process {
    $global:launches++
    $child = [pscustomobject]@{ Id=123; Handle=1; ExitCode=$null }
    $child | Add-Member ScriptMethod WaitForExit {
        if ($Scenario -ne 'null_exit') { throw 'injected wait failure' }
    }
    $child | Add-Member ScriptProperty HasExited {
        if ($Scenario -eq 'wait_unknown') { throw 'injected query failure' }
        return ($Scenario -eq 'null_exit' -or $Scenario -eq 'wait_exited')
    }
    $child | Add-Member ScriptMethod Dispose { }
    return $child
}
function Start-Sleep { throw 'test stops at retry boundary' }
try { & (Join-Path $Root 'runner.ps1') -DaemonName market-supervisor -ProjectRoot $Root -RawMarketRecovery } catch { }
if ($Scenario -in @('wait_alive', 'wait_unknown')) {
    # A fresh invocation must honor the durable latch, too.
    try { & (Join-Path $Root 'runner.ps1') -DaemonName market-supervisor -ProjectRoot $Root -RawMarketRecovery } catch { }
}
[IO.File]::WriteAllText((Join-Path $Root 'launches.txt'), [string]$global:launches)
''', encoding="utf-8")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(harness), str(tmp_path), scenario],
        check=True, capture_output=True, timeout=30,
    )
    assert (tmp_path / "launches.txt").read_text() == "1"
    logs = tmp_path / "data/logs/daemons"
    result = json.loads((logs / "market-supervisor.attempt.latest.json").read_text())
    assert result["exit_code"] is None
    assert result["state"] != "completed"
    assert result["runner_failure_code"] == (9008 if scenario == "null_exit" else 9009)
    blocked = scenario in {"wait_alive", "wait_unknown"}
    assert result["restart_blocked"] is blocked
    assert result["child_exit_confirmed"] is not blocked
    assert (logs / "market-supervisor.child-unresolved.json").exists() is blocked
    assert len(list((logs / "attempts/market-supervisor").iterdir())) == 1
