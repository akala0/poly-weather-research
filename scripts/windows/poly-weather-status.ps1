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
    [string]$DataDir = "D:\poly\data"
)

$ErrorActionPreference = "SilentlyContinue"
$runtimeDir = Join-Path $DataDir "runtime"

$daemons = @(
    @{ Name = "market-supervisor"; File = "market_supervisor_status.json"; RequiredState = "running" }
    @{ Name = "weather-daemon"; File = "weather_daemon_status.json"; RequiredState = "running" }
    @{ Name = "signal-engine"; File = "signal_engine_status.json"; RequiredState = "running" }
    @{ Name = "shadow-follower"; File = "shadow_spread_status_v2_token_scoped.json"; RequiredState = "running" }
)

$overallStatus = "healthy"
$issues = @()
$summary = @()

foreach ($daemon in $daemons) {
    $path = Join-Path $runtimeDir $daemon.File
    if (-not (Test-Path $path)) {
        $issues += "$($daemon.Name): status file missing"
        $overallStatus = "failed"
        $summary += [pscustomobject]@{ Daemon = $daemon.Name; State = "missing"; PID = "N/A"; Age = "N/A" }
        continue
    }

    try {
        $d = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        $issues += "$($daemon.Name): status file unreadable"
        $overallStatus = "failed"
        $summary += [pscustomobject]@{ Daemon = $daemon.Name; State = "unreadable"; PID = "N/A"; Age = "N/A" }
        continue
    }

    $state = if ($d.state) { $d.state } else { "unknown" }
    $pid = if ($d.pid) { $d.pid } else { "N/A" }
    $startedAt = if ($d.started_at) { $d.started_at } else { $d.checked_at }

    $ageMinutes = "N/A"
    if ($startedAt) {
        try {
            $started = [DateTime]::Parse($startedAt).ToUniversalTime()
            $ageMinutes = [math]::Round(([DateTime]::UtcNow - $started).TotalMinutes)
        }
        catch {}
    }

    $hasChecksum = $d.status_checksum_sha256 -ne $null

    if ($state -ne $daemon.RequiredState) {
        if ($state -in @("failed", "stopped", "stale", "stalled")) {
            $issues += "$($daemon.Name): state=$state (expected $($daemon.RequiredState))"
            $overallStatus = "failed"
        }
        elseif ($state -eq "degraded") {
            $issues += "$($daemon.Name): state=degraded"
            if ($overallStatus -ne "failed") { $overallStatus = "degraded" }
        }
    }

    $summary += [pscustomobject]@{
        Daemon = $daemon.Name
        State = $state
        PID = $pid
        "Age(min)" = $ageMinutes
        Checksum = $hasChecksum
    }
}

# Check disk
$disk = Get-PSDrive D -ErrorAction SilentlyContinue
$diskFreeGB = if ($disk) { [math]::Round($disk.Free / 1GB, 1) } else { "unknown" }
if ($disk -and $disk.Free / 1GB -lt 20) {
    $issues += "Disk low: ${diskFreeGB} GiB free"
    if ($overallStatus -eq "healthy") { $overallStatus = "degraded" }
}

# Output
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
