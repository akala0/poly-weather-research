#!/usr/bin/env powershell
<#
.SYNOPSIS
    Quick health check for the PolyWeather daemon chain.
.DESCRIPTION
    Reads runtime status files and reports overall health.
    Exit code 0 = all healthy, 1 = degraded, 2 = failed.
#>

[CmdletBinding()]
param(
    [string]$DataDir = "D:\poly\data",
    [string]$ProjectRoot = "D:\poly"
)

$ErrorActionPreference = "Stop"
$exePath = Join-Path $ProjectRoot ".venv\Scripts\poly-weather.exe"
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    Write-Host "PolyWeather executable missing: $exePath" -ForegroundColor Red
    exit 2
}

try {
    $statusJson = & $exePath stream-status --data-dir $DataDir 2>$null
    if ($LASTEXITCODE -ne 0 -or $null -eq $statusJson) {
        throw "stream-status exited with code $LASTEXITCODE"
    }
    $chainStatus = (($statusJson -join [Environment]::NewLine) | ConvertFrom-Json)
}
catch {
    Write-Host "=== PolyWeather Health Check ==="
    Write-Host "Overall: failed"
    Write-Host "Issue: safe stream-status failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 2
}

$daemons = @(
    @{ Name = "market-supervisor"; Status = $chainStatus.market; RequiredStates = @("connected", "upstream_maintenance"); Command = "market-supervisor"; MaxHeartbeatSeconds = 120 }
    @{ Name = "weather-daemon"; Status = $chainStatus.weather; RequiredStates = @("running"); Command = "weather-stream"; MaxHeartbeatSeconds = 300 }
    @{ Name = "signal-engine"; Status = $chainStatus.signal; RequiredStates = @("running"); Command = "signal-engine"; MaxHeartbeatSeconds = 120 }
    @{ Name = "shadow-follower"; Status = $chainStatus.shadow; RequiredStates = @("running"); Command = "shadow-spread-engine"; MaxHeartbeatSeconds = 120 }
)

$overallStatus = "healthy"
$issues = @()
$summary = @()

foreach ($daemon in $daemons) {
    $status = $daemon.Status
    $state = if ($null -ne $status.state) { [string]$status.state } else { "unknown" }
    $daemonPid = if ($null -ne $status.pid) { [int]$status.pid } else { 0 }
    $heartbeatAge = if ($null -ne $status.status_heartbeat_age_seconds) {
        [double]$status.status_heartbeat_age_seconds
    }
    else {
        [double]::PositiveInfinity
    }
    $integrity = [string]$status.status_integrity
    $pidAlive = $status.status_pid_alive -eq $true
    $commandMatches = $status.pid_state -eq "alive"
    if ($status.health_ready -ne $true) {
        $issues += "$($daemon.Name): normalized health rejected: $($status.reasons -join ',')"
        $overallStatus = "failed"
    }

    if ($integrity -ne "verified") {
        $issues += "$($daemon.Name): status integrity=$integrity"
        $overallStatus = "failed"
    }
    if (-not $pidAlive) {
        $issues += "$($daemon.Name): status PID is not alive"
        $overallStatus = "failed"
    }
    elseif (-not $commandMatches) {
        $issues += "$($daemon.Name): PID $daemonPid command line does not match $($daemon.Command)"
        $overallStatus = "failed"
    }
    if ($heartbeatAge -gt [double]$daemon.MaxHeartbeatSeconds) {
        $issues += "$($daemon.Name): heartbeat age $([math]::Round($heartbeatAge, 1))s exceeds $($daemon.MaxHeartbeatSeconds)s"
        $overallStatus = "failed"
    }
    if ($state -notin $daemon.RequiredStates) {
        $issues += "$($daemon.Name): state=$state (expected $($daemon.RequiredStates -join '/'))"
        $overallStatus = "failed"
    }
    if ($null -ne $status.execution_enabled -and $status.execution_enabled -ne $false) {
        $issues += "$($daemon.Name): execution_enabled is not false"
        $overallStatus = "failed"
    }

    $summary += [pscustomobject]@{
        Daemon = $daemon.Name
        State = $state
        PID = if ($daemonPid -gt 0) { $daemonPid } else { "N/A" }
        "Heartbeat(s)" = if ([double]::IsPositiveInfinity($heartbeatAge)) { "N/A" } else { [math]::Round($heartbeatAge, 1) }
        Integrity = $integrity
        Command = $commandMatches
    }
}

function Set-Degraded {
    param([string]$Issue)
    $script:issues += $Issue
    if ($script:overallStatus -eq "healthy") {
        $script:overallStatus = "degraded"
    }
}

function Set-Failed {
    param([string]$Issue)
    $script:issues += $Issue
    $script:overallStatus = "failed"
}

$market = $chainStatus.market
$supervisor = $chainStatus.supervisor
$weather = $chainStatus.weather
$signal = $chainStatus.signal
$shadow = $chainStatus.shadow

if ($supervisor.status_integrity -ne "verified") {
    Set-Failed "market supervisor status integrity=$($supervisor.status_integrity)"
}
if ($supervisor.status_pid_alive -ne $true) {
    Set-Failed "market supervisor PID is not alive"
}
elseif ([int]$supervisor.pid -ne [int]$market.pid) {
    Set-Failed "market supervisor PID $($supervisor.pid) does not own market PID $($market.pid)"
}
if ([double]$supervisor.status_heartbeat_age_seconds -gt 360) {
    Set-Failed "market supervisor heartbeat age=$([math]::Round([double]$supervisor.status_heartbeat_age_seconds, 1))s"
}
if ($supervisor.state -ne "running") {
    Set-Failed "market supervisor state=$($supervisor.state)"
}
if ($supervisor.execution_enabled -ne $false) {
    Set-Failed "market supervisor execution_enabled is not false"
}

if ([int]($market.queue_depth) -gt 1000) {
    Set-Degraded "market queue depth=$($market.queue_depth)"
}
if ([int]($market.database_queue_depth) -gt 100) {
    Set-Failed "market database queue depth=$($market.database_queue_depth)"
}
if ($market.disk_warning -eq $true) {
    Set-Degraded "market stream reports a disk warning"
}
if ($market.upstream_maintenance -eq $true) {
    Set-Degraded "market stream reports upstream maintenance"
}
if ([int]($weather.queue_depth) -gt 500) {
    Set-Failed "weather queue depth=$($weather.queue_depth)"
}

foreach ($writer in @(
    @{ Name = "weather"; Status = $weather.writer },
    @{ Name = "signal"; Status = $signal.writer }
)) {
    $database = $writer.Status.database
    if ($null -ne $database.last_database_error) {
        Set-Failed "$($writer.Name) database error=$($database.last_database_error.code)"
    }
    $walBytes = [int64]($database.wal.size_bytes)
    if ($walBytes -gt 1GB) {
        Set-Degraded "$($writer.Name) WAL size=$walBytes bytes"
    }
}

if ($shadow.halted -eq $true) {
    Set-Failed "shadow follower is HALTED: $($shadow.halt_reason)"
}
if ([int]($shadow.discrepancy_count) -ne 0) {
    Set-Failed "shadow discrepancy count=$($shadow.discrepancy_count)"
}
if ($shadow.execution_enabled -ne $false) {
    Set-Failed "shadow execution_enabled is not false"
}

$disk = Get-PSDrive D -ErrorAction SilentlyContinue
$diskFreeGB = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { "unknown" }
if ($disk -and $disk.Free / 1GB -lt 20) {
    $issues += "Disk low: ${diskFreeGB} GiB free"
    if ($overallStatus -eq "healthy") { $overallStatus = "degraded" }
}

Write-Host "=== PolyWeather Health Check ==="
Write-Host "Overall: $overallStatus"
Write-Host "Disk free: ${diskFreeGB} GiB"
Write-Host ""
$summary | Format-Table -AutoSize

if ($issues.Count -gt 0) {
    Write-Host "Issues:" -ForegroundColor Yellow
    foreach ($issue in $issues) {
        Write-Host "  - $issue" -ForegroundColor Yellow
    }
}

switch ($overallStatus) {
    "healthy" { exit 0 }
    "degraded" { exit 1 }
    default { exit 2 }
}
