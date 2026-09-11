$ErrorActionPreference = 'Stop'
$out = 'D:\poly\docs\raw_market_recovery_20260910'
$baseline = Get-Content "$out\processes.before.json" -Raw | ConvertFrom-Json
function Processes {
    @(Get-CimInstance Win32_Process -OperationTimeoutSec 10 -Filter "Name='powershell.exe' OR Name='python.exe' OR Name='poly-weather.exe'" | Where-Object {
        $_.CommandLine -match 'poly-weather-daemon-runner.ps1|poly-weather.exe|poly_weather' -and $_.CommandLine -notmatch 'Get-CimInstance|preflight.py'
    })
}
function Assert-Weather {
    foreach ($b in @($baseline | Where-Object { $_.ProcessId -in @(4524,17784,17912,17944) })) {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($b.ProcessId)" -OperationTimeoutSec 10
        if (-not $p -or $p.CommandLine -ne $b.CommandLine -or ([datetime]$p.CreationDate) -ne ([datetime]$b.CreationDate)) { throw 'Weather identity changed; stop' }
    }
}
function Stop-WaitingRunner([string]$daemon) {
    $taskName = "PolyWeather-$daemon"
    $task = @(Get-ScheduledTask -TaskName $taskName)
    if ($task.Count -ne 1 -or $task[0].TaskPath -ne '\') { throw "Ambiguous task $daemon" }
    $rows = Processes
    $runners = @($rows | Where-Object { $_.CommandLine -match 'poly-weather-daemon-runner.ps1' -and $_.CommandLine -match "-DaemonName $daemon " })
    if ($runners.Count -ne 1) { throw "Ambiguous runner $daemon" }
    $runner = $runners[0]
    $b = @($baseline | Where-Object ProcessId -eq $runner.ProcessId)
    if ($b.Count -ne 1 -or $runner.CommandLine -ne $b[0].CommandLine -or ([datetime]$runner.CreationDate) -ne ([datetime]$b[0].CreationDate)) { throw 'Runner identity changed' }
    $children = @($rows | Where-Object { $_.CommandLine -notmatch 'poly-weather-daemon-runner.ps1' -and $_.CommandLine -match " $daemon( |$)" })
    if ($children.Count -gt 0) { throw "Existing child for $daemon; stop without touching it" }
    Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
    Stop-ScheduledTask -TaskName $taskName -TaskPath '\'
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($runner.ProcessId)" -OperationTimeoutSec 10
        if (-not $p) { break }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    if ($p) { throw 'Runner still present; no broad kill fallback' }
    $left = @(Processes | Where-Object { $_.CommandLine -match "(-DaemonName $daemon | $daemon( |$))" })
    if ($left.Count -gt 0) { throw "Remaining process $daemon" }
    $t = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
    if ($t.Settings.Enabled -or $t.State -eq 'Running') { throw 'Task not disabled/stopped' }
    @{at=(Get-Date).ToUniversalTime().ToString('o'); action='disabled_and_stopped_waiting_runner'; daemon=$daemon; old_pid=$runner.ProcessId; old_creation=$runner.CreationDate} | ConvertTo-Json -Compress | Add-Content "$out\operations.jsonl"
    Assert-Weather
}
if (Test-Path 'D:\poly\data\logs\daemons\market-supervisor.child-unresolved.json') { throw 'Unresolved marker exists' }
Assert-Weather
Stop-WaitingRunner 'signal-engine'
Stop-WaitingRunner 'shadow-spread-engine'
Stop-WaitingRunner 'market-supervisor'
$t = Get-ScheduledTask -TaskName 'PolyWeather-market-supervisor' -TaskPath '\'
if (@($t.Actions).Count -ne 1) { throw 'Multiple actions' }
$old = $t.Actions[0]
if ($old.Arguments -match 'RawMarketRecovery') { throw 'Unexpected preexisting flag' }
$newArguments = $old.Arguments + ' -RawMarketRecovery'
$action = New-ScheduledTaskAction -Execute $old.Execute -Argument $newArguments -WorkingDirectory $old.WorkingDirectory
Set-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Action $action | Out-Null
$xml = Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath
$xml | Set-Content "$out\PolyWeather-market-supervisor.candidate.xml"
$new = Get-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath
if ($new.Actions[0].Arguments -ne $newArguments -or $new.Actions[0].Execute -ne $old.Execute -or $new.Actions[0].WorkingDirectory -ne $old.WorkingDirectory -or $new.Settings.Enabled) { throw 'Action verification failed' }
[xml]$before = Get-Content "$out\PolyWeather-market-supervisor.before.xml" -Raw
[xml]$after = $xml
$before.Task.Actions.Exec.Arguments = $newArguments
$ns = [Xml.XmlNamespaceManager]::new($before.NameTable)
$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
$enabled = $before.SelectSingleNode('//t:Settings/t:Enabled',$ns)
if (-not $enabled) { $enabled=$before.CreateElement('Enabled',$before.DocumentElement.NamespaceURI); $before.Task.Settings.AppendChild($enabled) | Out-Null }
$enabled.InnerText='false'
# Compare every principal/trigger/action and setting by name, ignoring XML element order.
foreach ($part in @('Principals','Triggers','Actions','RegistrationInfo')) {
    if ($before.Task.$part.OuterXml -ne $after.Task.$part.OuterXml) { throw "Unexpected task diff: $part" }
}
$bSettings = @($before.Task.Settings.ChildNodes | ForEach-Object { $_.OuterXml } | Sort-Object)
$aSettings = @($after.Task.Settings.ChildNodes | ForEach-Object { $_.OuterXml } | Sort-Object)
if (Compare-Object $bSettings $aSettings) { throw 'Unexpected settings diff' }
@{at=(Get-Date).ToUniversalTime().ToString('o'); action='market_action_updated_verified_disabled'; arguments=$newArguments; runner_sha256=(Get-FileHash 'D:\poly\scripts\windows\poly-weather-daemon-runner.ps1' -Algorithm SHA256).Hash} | ConvertTo-Json -Compress | Add-Content "$out\operations.jsonl"
Processes | Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Depth 5 | Set-Content "$out\processes.isolated.json"
'Downstream isolated; old market stopped; new action verified; market remains disabled pending supervised start.'
