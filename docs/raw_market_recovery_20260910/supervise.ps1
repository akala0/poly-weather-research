$ErrorActionPreference='Stop'
$out='D:\poly\docs\raw_market_recovery_20260910'
$logs='D:\poly\data\logs\daemons'
$name='PolyWeather-market-supervisor'
$baseline=Get-Content "$out\processes.before.json" -Raw | ConvertFrom-Json
$fingerprint=Get-Content "$out\preflight.json" -Raw | ConvertFrom-Json
foreach ($entry in $fingerprint.source_sha256.PSObject.Properties) {
    if ((Get-FileHash (Join-Path 'D:\poly' $entry.Name) -Algorithm SHA256).Hash.ToLower() -ne $entry.Value) { throw "Candidate drift: $($entry.Name)" }
}
function Rows {
    @(Get-CimInstance Win32_Process -OperationTimeoutSec 10 -Filter "Name='powershell.exe' OR Name='python.exe' OR Name='poly-weather.exe'" | Where-Object {
        $_.CommandLine -match 'poly-weather-daemon-runner.ps1|poly-weather.exe|poly_weather' -and $_.CommandLine -notmatch 'Get-CimInstance|preflight.py'
    })
}
function Save-Json($payload,[string]$path) { $payload | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $path }
function Stop-Owned([string]$reason) {
    Disable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null
    $current=Rows
    $owned=@($current | Where-Object { $_.CommandLine -match '-DaemonName market-supervisor ' -and $_.CommandLine -match ' -RawMarketRecovery' })
    $identityConfirmed=($owned.Count -eq 1 -and $null -ne $script:runner -and $owned[0].ProcessId -eq $script:runner.ProcessId -and $owned[0].CreationDate -eq $script:runner.CreationDate -and $owned[0].CommandLine -eq $script:runner.CommandLine)
    $children=@($current | Where-Object { $_.CommandLine -notmatch 'poly-weather-daemon-runner.ps1' -and $_.CommandLine -match ' market-supervisor( |$)' })
    $childOwnership=$true
    foreach ($child in $children) {
        $cursor=$child
        $found=$false
        for($i=0;$i -lt 5;$i++) {
            if ($null -ne $script:runner -and $cursor.ParentProcessId -eq $script:runner.ProcessId) { $found=$true; break }
            $parent=@($current | Where-Object ProcessId -eq $cursor.ParentProcessId)
            if ($parent.Count -ne 1) { break }
            $cursor=$parent[0]
        }
        if (-not $found -or $child.CommandLine -notmatch '--raw-market-recovery' -or $child.CommandLine -notmatch '--startup-attempt-id') { $childOwnership=$false }
    }
    if ($identityConfirmed -and $childOwnership) {
        Stop-ScheduledTask -TaskName $name -TaskPath '\'
        Start-Sleep -Seconds 2
        # Task stop may leave launcher descendants. Stop only the exact previously verified identities.
        foreach ($child in $children) {
            $p=Get-CimInstance Win32_Process -OperationTimeoutSec 10 -Filter "ProcessId=$($child.ProcessId)"
            if ($p -and $p.CreationDate -eq $child.CreationDate -and $p.CommandLine -eq $child.CommandLine) { Stop-Process -Id $p.ProcessId -Force }
        }
    }
    $remaining=@(Rows | Where-Object { $_.CommandLine -match '-DaemonName market-supervisor | market-supervisor( |$)' })
    Save-Json @{at=(Get-Date).ToUniversalTime().ToString('o'); result='FAILED'; reason=$reason; failed_attempts=$script:failed; runner_identity_confirmed=$identityConfirmed; child_ownership_confirmed=$childOwnership; remaining=$remaining | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine; marker_exists=(Test-Path "$logs\market-supervisor.child-unresolved.json")} "$out\supervision.result.json"
    Write-Output "STOPPED: $reason; failures=$script:failed; remaining=$($remaining.Count)"
}
if (Test-Path "$logs\market-supervisor.child-unresolved.json") { throw 'Preexisting unresolved marker' }
foreach ($daemon in @('signal-engine','shadow-spread-engine')) {
    $t=Get-ScheduledTask -TaskName "PolyWeather-$daemon" -TaskPath '\'
    if ($t.Settings.Enabled -or $t.State -eq 'Running') { throw 'Downstream not isolated' }
}
if (@(Rows | Where-Object {$_.CommandLine -match '-DaemonName market-supervisor | market-supervisor( |$)'}).Count) { throw 'Old market present' }
$started=(Get-Date).ToUniversalTime()
$deadline=$started.AddMinutes(30)
$script:runner=$null
$script:failed=0
Save-Json @{started_at=$started.ToString('o'); deadline=$deadline.ToString('o'); failure_limit=3; minimum_continuous_seconds=600} "$out\supervision.start.json"
Enable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null
Start-ScheduledTask -TaskName $name -TaskPath '\'
try {
    $nextSnapshot=Get-Date
    while ((Get-Date).ToUniversalTime() -lt $deadline) {
        $attempts=@()
        foreach ($dir in @(Get-ChildItem "$logs\attempts\market-supervisor" -Directory -ErrorAction SilentlyContinue)) {
            $sp=Join-Path $dir.FullName 'attempt-start.json'
            if (-not (Test-Path $sp)) { continue }
            $s=Get-Content $sp -Raw | ConvertFrom-Json
            if ([datetime]$s.started_at -lt $started) { continue }
            $rp=Join-Path $dir.FullName 'attempt-result.json'
            if (Test-Path $rp) {
                $r=Get-Content $rp -Raw | ConvertFrom-Json
                $attempts+= $r
            }
        }
        $script:failed=@($attempts | Where-Object { $null -eq $_.exit_code -or $_.exit_code -ne 0 -or $_.restart_blocked }).Count
        if ($script:failed -ge 3) { Stop-Owned 'three_failed_child_attempts'; exit 0 }
        if (@($attempts | Where-Object restart_blocked).Count) { Stop-Owned 'unresolved_child'; exit 0 }
        if ((Get-Date) -ge $nextSnapshot) {
            $rows=Rows
            $rs=@($rows | Where-Object { $_.CommandLine -match '-DaemonName market-supervisor ' -and $_.CommandLine -match ' -RawMarketRecovery' })
            if ($rs.Count -ne 1) { Stop-Owned 'runner_missing_or_ambiguous'; exit 0 }
            if ($null -eq $script:runner) {
                $script:runner=$rs[0]
                Save-Json ($script:runner | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine) "$out\new-runner.json"
            } elseif ($rs[0].ProcessId -ne $script:runner.ProcessId -or $rs[0].CreationDate -ne $script:runner.CreationDate) { Stop-Owned 'runner_restarted'; exit 0 }
            foreach ($b in @($baseline | Where-Object {$_.ProcessId -in @(4524,17784,17912,17944)})) {
                $p=@($rows | Where-Object ProcessId -eq $b.ProcessId)
                if ($p.Count -ne 1 -or $p[0].CommandLine -ne $b.CommandLine -or ([datetime]$p[0].CreationDate) -ne ([datetime]$b.CreationDate)) { Stop-Owned 'weather_identity_changed'; exit 0 }
            }
            if (@($rows | Where-Object {$_.CommandLine -match '-DaemonName (signal-engine|shadow-spread-engine) | (signal-engine|shadow-spread-engine|paper-spread-engine)( |$)'}).Count) { Stop-Owned 'downstream_process_detected'; exit 0 }
            $stamp=(Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
            Save-Json ($rows | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine) "$out\processes.$stamp.json"
            foreach ($file in @('polymarket_ws_status.json','market_supervisor_status.json','weather_daemon_status.json')) {
                $path="D:\poly\data\runtime\$file"
                if(Test-Path $path) { Copy-Item -LiteralPath $path -Destination "$out\$stamp.$file" }
            }
            Write-Output "$stamp failures=$script:failed runner=$($script:runner.ProcessId)"
            $nextSnapshot=(Get-Date).AddSeconds(20)
        }
        if (Test-Path "$out\acceptance.confirmed.json") {
            Save-Json @{at=(Get-Date).ToUniversalTime().ToString('o'); result='SUCCESS'; failed_attempts=$script:failed; runner_pid=$script:runner.ProcessId; note='Detailed acceptance evidence in acceptance.confirmed.json; market remains running'} "$out\supervision.result.json"
            exit 0
        }
        Start-Sleep -Seconds 1
    }
    Stop-Owned 'thirty_minute_deadline'
} catch {
    $message=$_.Exception.Message
    Stop-Owned "supervision_exception: $message"
    throw
}
