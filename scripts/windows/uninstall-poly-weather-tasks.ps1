[CmdletBinding()]
param(
    [string]$TaskPrefix = "PolyWeather"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

foreach ($daemonName in @("shadow-spread-engine", "signal-engine", "weather-stream", "market-supervisor")) {
    $taskName = "$TaskPrefix-$daemonName"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "unregistered if present: $taskName"
}
