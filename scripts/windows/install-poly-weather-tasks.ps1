[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\poly",
    [string]$TaskPrefix = "PolyWeather"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$runnerPath = Join-Path $ProjectRoot "scripts\windows\poly-weather-daemon-runner.ps1"
$exePath = Join-Path $ProjectRoot ".venv\Scripts\poly-weather.exe"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) {
    throw "Runner script not found: $runnerPath"
}
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "poly-weather executable not found: $exePath"
}

$principalId = "$env:USERDOMAIN\$env:USERNAME"
$powershellPath = (Get-Command powershell.exe).Source
$definitionsPath = Join-Path $ProjectRoot "scripts\windows\task-definitions"
New-Item -ItemType Directory -Force -Path $definitionsPath | Out-Null

$daemonNames = @(
    "market-supervisor",
    "weather-stream",
    "signal-engine",
    "shadow-spread-engine"
)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $principalId -LogonType InteractiveToken -RunLevel Limited

foreach ($daemonName in $daemonNames) {
    $taskName = "$TaskPrefix-$daemonName"
    $argumentList = @(
        "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", $runnerPath, "-DaemonName", $daemonName, "-ProjectRoot", $ProjectRoot
    )
    $action = New-ScheduledTaskAction -Execute $powershellPath -Argument ($argumentList -join " ") -WorkingDirectory $ProjectRoot
    $triggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -AtLogOn -User $principalId)
    )
    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Principal $principal `
        -Description "PolyWeather read-only $daemonName; fail-closed; execution_enabled=false"
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    $xml = Export-ScheduledTask -TaskName $taskName
    $xmlPath = Join-Path $definitionsPath "$taskName.xml"
    Set-Content -LiteralPath $xmlPath -Value $xml -Encoding UTF8
    Write-Host "registered $taskName; exported $xmlPath"
}

Write-Host "All four read-only daemon tasks are installed for $principalId."
Write-Host "Dependency gates remain in the runner; no task bypasses fail-closed checks."
