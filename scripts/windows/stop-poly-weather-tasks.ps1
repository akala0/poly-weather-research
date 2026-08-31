[CmdletBinding()]
param(
    [string]$TaskPrefix = "PolyWeather"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($daemonName in @("shadow-spread-engine", "signal-engine", "weather-stream", "market-supervisor")) {
    $taskName = "$TaskPrefix-$daemonName"
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Host "stop requested: $taskName"
    }
}
