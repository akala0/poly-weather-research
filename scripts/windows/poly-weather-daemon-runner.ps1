[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("market-supervisor", "weather-stream", "signal-engine", "shadow-spread-engine")]
    [string]$DaemonName,

    [string]$ProjectRoot = "D:\poly",
    [int]$DependencyPollSeconds = 30,
    [int]$InitialBackoffSeconds = 5,
    [int]$MaxBackoffSeconds = 300,
    [int]$MaxRestartsInWindow = 6,
    [int]$RestartWindowSeconds = 900,
    [int]$CrashLoopPauseSeconds = 900,
    [int]$LogRetentionCount = 5
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$exePath = Join-Path $ProjectRoot ".venv\Scripts\poly-weather.exe"
$dataDir = Join-Path $ProjectRoot "data"
$configPath = Join-Path $ProjectRoot "configs\settlements.json"
$warmingPolicyPath = Join-Path $ProjectRoot "configs\warming_window_no_thresholds.json"
$logDir = Join-Path $dataDir "logs\daemons"
$runnerLogPath = Join-Path $logDir "$DaemonName.runner.log"
$stdoutPath = Join-Path $logDir "$DaemonName.stdout.log"
$stderrPath = Join-Path $logDir "$DaemonName.stderr.log"
$mutexName = "Global\PolyWeather.$DaemonName"

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project root does not exist: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "poly-weather executable does not exist: $exePath"
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-RunnerLog {
    param([string]$Message)

    $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $DaemonName, $Message
    Add-Content -LiteralPath $runnerLogPath -Value $line -Encoding UTF8
}

function Rotate-Log {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $oldest = "$Path.$LogRetentionCount"
    if (Test-Path -LiteralPath $oldest -PathType Leaf) {
        Remove-Item -LiteralPath $oldest -Force
    }
    for ($index = $LogRetentionCount - 1; $index -ge 1; $index--) {
        $source = "$Path.$index"
        $destination = "$Path.$($index + 1)"
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Move-Item -LiteralPath $source -Destination $destination -Force
        }
    }
    Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
}

function Get-PropertyValue {
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object) {
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Get-ChainStatus {
    try {
        $output = & $exePath stream-status --data-dir $dataDir 2>$null
        if ($LASTEXITCODE -ne 0 -or $null -eq $output) {
            return $null
        }
        return (($output -join [Environment]::NewLine) | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Test-VerifiedStatus {
    param([AllowNull()]$Status)

    if ($null -eq $Status) {
        return $false
    }
    $pidAlive = Get-PropertyValue $Status "status_pid_alive"
    $integrity = [string](Get-PropertyValue $Status "status_integrity")
    return ($pidAlive -eq $true -and $integrity -in @("verified", "legacy_unchecked"))
}

function Test-MarketReady {
    param([AllowNull()]$Status)

    $market = Get-PropertyValue $Status "market"
    $supervisor = Get-PropertyValue $Status "supervisor"
    return (
        (Test-VerifiedStatus $market) -and
        (Test-VerifiedStatus $supervisor) -and
        ([int](Get-PropertyValue $market "book_snapshot_count") -gt 0)
    )
}

function Test-Dependencies {
    $status = Get-ChainStatus
    if ($DaemonName -eq "market-supervisor") {
        return $true
    }
    if ($null -eq $status) {
        return $false
    }
    $weather = Get-PropertyValue $status "weather"
    $signal = Get-PropertyValue $status "signal"
    $marketReady = Test-MarketReady $status
    if ($DaemonName -eq "weather-stream") {
        return $marketReady
    }
    if ($DaemonName -eq "signal-engine") {
        return (
            $marketReady -and
            (Test-VerifiedStatus $weather) -and
            (Get-PropertyValue $weather "state") -eq "running"
        )
    }
    return (
        $marketReady -and
        (Test-VerifiedStatus $weather) -and
        (Get-PropertyValue $weather "state") -eq "running" -and
        (Test-VerifiedStatus $signal) -and
        (Get-PropertyValue $signal "state") -eq "running"
    )
}

function Wait-ForDependencies {
    if ($DaemonName -eq "market-supervisor") {
        return
    }
    $loggedWaiting = $false
    while (-not (Test-Dependencies)) {
        if (-not $loggedWaiting) {
            Write-RunnerLog "dependency gate closed; waiting without starting child"
            $loggedWaiting = $true
        }
        Start-Sleep -Seconds $DependencyPollSeconds
    }
    if ($loggedWaiting) {
        Write-RunnerLog "dependency gate passed"
    }
}

function Get-ArgumentList {
    switch ($DaemonName) {
        "market-supervisor" {
            return @(
                "market-supervisor", "--runtime", "0", "--config", $configPath,
                "--data-dir", $dataDir
            )
        }
        "weather-stream" {
            return @(
                "weather-stream",
                "new-york-daily-high-research-seed",
                "los-angeles-daily-high-research-seed",
                "chicago-daily-high-research-seed",
                "miami-daily-high-research-seed",
                "atlanta-daily-high-research-seed",
                "dallas-daily-high-research-seed",
                "houston-daily-high-research-seed",
                "seattle-daily-high-research-seed",
                "chongqing-daily-high-research-seed",
                "chengdu-daily-high-research-seed",
                "--runtime", "0", "--config", $configPath, "--data-dir", $dataDir
            )
        }
        "signal-engine" {
            return @(
                "signal-engine", "--supervised", "--runtime", "0", "--config", $configPath,
                "--warming-policy", $warmingPolicyPath, "--data-dir", $dataDir
            )
        }
        "shadow-spread-engine" {
            return @(
                "shadow-spread-engine", "--supervised", "--runtime", "0", "--data-dir", $dataDir,
                "--ledger", (Join-Path $dataDir "raw\shadow_orders\shadow_orders_v2_token_scoped.jsonl"),
                "--status", (Join-Path $dataDir "runtime\shadow_spread_status_v2_token_scoped.json"),
                "--cursor", (Join-Path $dataDir "runtime\shadow_spread_cursor_v2_token_scoped.json")
            )
        }
    }
    throw "Unsupported daemon: $DaemonName"
}

$createdNew = $false
$mutex = [Threading.Mutex]::new($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-RunnerLog "another runner owns the single-instance lock; exiting"
    exit 0
}

try {
    $proxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    $proxyPresent = @($proxyNames | Where-Object { Test-Path "Env:\$_" })
    Write-RunnerLog ("runner started; exe={0}; proxy variable names present={1}; execution_enabled=false" -f $exePath, ($proxyPresent -join ","))
    $arguments = Get-ArgumentList
    $failureStreak = 0
    $restartTimes = [Collections.Generic.Queue[datetime]]::new()

    while ($true) {
        Wait-ForDependencies
        Rotate-Log $stdoutPath
        Rotate-Log $stderrPath
        try {
            $child = Start-Process -FilePath $exePath -ArgumentList $arguments -WorkingDirectory $ProjectRoot `
                -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
            Write-RunnerLog "child started; pid=$($child.Id)"
            $child.WaitForExit()
            $exitCode = $child.ExitCode
            $child.Dispose()
        }
        catch {
            $exitCode = 9009
            Write-RunnerLog "child launch/wait failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
        }

        if ($exitCode -eq 0) {
            Write-RunnerLog "child exited cleanly with code 0; runner stopped"
            exit 0
        }

        $failureStreak++
        $now = Get-Date
        $restartTimes.Enqueue($now)
        while ($restartTimes.Count -gt 0 -and ($now - $restartTimes.Peek()).TotalSeconds -gt $RestartWindowSeconds) {
            [void]$restartTimes.Dequeue()
        }
        if ($restartTimes.Count -ge $MaxRestartsInWindow) {
            Write-RunnerLog "child exited code=$exitCode; crash-loop guard engaged; failures_in_window=$($restartTimes.Count); sleeping ${CrashLoopPauseSeconds}s"
            Start-Sleep -Seconds $CrashLoopPauseSeconds
            $failureStreak = 0
            continue
        }
        $power = [math]::Min($failureStreak - 1, 8)
        $delay = [int][math]::Min($MaxBackoffSeconds, $InitialBackoffSeconds * [math]::Pow(2, $power))
        Write-RunnerLog "child exited code=$exitCode; exponential backoff ${delay}s; failures_in_window=$($restartTimes.Count)"
        Start-Sleep -Seconds $delay
    }
}
finally {
    if ($createdNew) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    $mutex.Dispose()
}
