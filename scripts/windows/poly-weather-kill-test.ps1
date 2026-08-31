[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("weather-stream", "signal-engine", "shadow-spread-engine")]
    [string]$DaemonName,

    [string]$ProjectRoot = "D:\poly",
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$exePath = Join-Path $ProjectRoot ".venv\Scripts\poly-weather.exe"
$dataDir = Join-Path $ProjectRoot "data"
$statusNames = @{
    "weather-stream" = "weather"
    "signal-engine" = "signal"
    "shadow-spread-engine" = "shadow"
}
$statusName = $statusNames[$DaemonName]

function Get-Status {
    $output = & $exePath stream-status --data-dir $dataDir 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $output) { return $null }
    return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Get-PropertyValue {
    param([AllowNull()]$Object, [Parameter(Mandatory = $true)][string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

$before = Get-Status
$target = Get-PropertyValue $before $statusName
$pidValue = Get-PropertyValue $target "pid"
$pidAlive = Get-PropertyValue $target "status_pid_alive"
if ($null -eq $pidValue -or $pidAlive -ne $true) {
    throw "kill test refused: $DaemonName has no live verified PID"
}

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue"
if ($null -eq $process) {
    throw "kill test refused: PID $pidValue disappeared"
}
$commandLine = [string]$process.CommandLine
if ($commandLine -notlike "*$DaemonName*" -or $commandLine -notlike "*poly-weather.exe*") {
    throw "kill test refused: PID $pidValue command line does not match $DaemonName"
}

$eventPath = Join-Path $dataDir "runtime\daemon_recovery_kill_tests.jsonl"
$event = [ordered]@{
    observed_at = (Get-Date).ToUniversalTime().ToString("o")
    daemon = $DaemonName
    pid = [int]$pidValue
    action = "controlled_stop_process"
    execution_enabled = $false
}
Add-Content -LiteralPath $eventPath -Value (($event | ConvertTo-Json -Compress)) -Encoding UTF8
Stop-Process -Id ([int]$pidValue) -Force

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$replacementPid = $null
do {
    Start-Sleep -Seconds 5
    $current = Get-Status
    $currentTarget = Get-PropertyValue $current $statusName
    $currentPid = Get-PropertyValue $currentTarget "pid"
    $currentAlive = Get-PropertyValue $currentTarget "status_pid_alive"
    if ($currentAlive -eq $true -and $null -ne $currentPid -and [int]$currentPid -ne [int]$pidValue) {
        $replacementPid = [int]$currentPid
        break
    }
} while ((Get-Date) -lt $deadline)

if ($null -eq $replacementPid) {
    throw "automatic restart was not observed within ${WaitSeconds}s for $DaemonName"
}

$result = [ordered]@{
    observed_at = (Get-Date).ToUniversalTime().ToString("o")
    daemon = $DaemonName
    old_pid = [int]$pidValue
    replacement_pid = $replacementPid
    auto_restart = $true
    execution_enabled = $false
}
Add-Content -LiteralPath $eventPath -Value (($result | ConvertTo-Json -Compress)) -Encoding UTF8
$result | ConvertTo-Json
