[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("market-supervisor", "weather-stream", "signal-engine", "shadow-spread-engine", "paper-spread-engine")]
    [string]$DaemonName,

    [string]$ProjectRoot = "D:\poly",
    [int]$DependencyPollSeconds = 30,
    [int]$InitialBackoffSeconds = 5,
    [int]$MaxBackoffSeconds = 300,
    [int]$MaxRestartsInWindow = 6,
    [int]$RestartWindowSeconds = 900,
    [int]$CrashLoopPauseSeconds = 900,
    [int]$LogRetentionCount = 5,
    [switch]$RawMarketRecovery
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
$attemptRoot = Join-Path $logDir ("attempts\{0}" -f $DaemonName)
$latestAttemptPath = Join-Path $logDir "$DaemonName.attempt.latest.json"
$childUnresolvedPath = Join-Path $logDir "$DaemonName.child-unresolved.json"
$mutexName = "Global\PolyWeather.$DaemonName"

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project root does not exist: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "poly-weather executable does not exist: $exePath"
}
if ($RawMarketRecovery -and $DaemonName -ne "market-supervisor") {
    throw "RawMarketRecovery is valid only for market-supervisor"
}
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $attemptRoot | Out-Null

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite
    )
    try {
        $digest = $algorithm.ComputeHash($stream)
        return ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Write-RunnerLog {
    param([string]$Message)

    $line = "{0} [{1}] {2}" -f (Get-Date).ToUniversalTime().ToString("o"), $DaemonName, $Message
    Add-Content -LiteralPath $runnerLogPath -Value $line -Encoding UTF8
}

if ($DaemonName -eq "market-supervisor" -and -not $RawMarketRecovery) {
    Write-RunnerLog "market startup blocked; explicit -RawMarketRecovery is required; no child started"
    throw "market-supervisor runner requires explicit -RawMarketRecovery"
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

function Write-JsonAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Payload
    )

    $temporary = "{0}.{1}.{2}.tmp" -f $Path, $PID, ([guid]::NewGuid().ToString("N"))
    $json = $Payload | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText(
        $temporary,
        $json + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-LogEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{
            path = $Path
            bytes = 0
            sha256 = $null
        }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        path = $Path
        bytes = [int64]$item.Length
        sha256 = Get-FileSha256 -Path $Path
    }
}

function Get-StartupDiagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$AttemptId
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $prefix = "POLY_WEATHER_STARTUP_DIAGNOSTIC "
    $lines = @(Get-Content -LiteralPath $Path -Tail 200)
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        $line = [string]$lines[$index]
        if (-not $line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            continue
        }
        try {
            $value = $line.Substring($prefix.Length) | ConvertFrom-Json
            if ([string](Get-PropertyValue $value "attempt_id") -eq $AttemptId) {
                return $value
            }
        }
        catch {
            continue
        }
    }
    return $null
}

function Update-CompatibilityLog {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Rotate-Log $Destination
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
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
    # Same normalized truth table as CLI/Paper/signal/shadow; checksum alone is not health.
    return ((Get-PropertyValue $Status "health_ready") -eq $true)
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
    # Raw collectors must keep recording independently of strategy consumers.
    if ($DaemonName -in @("market-supervisor", "weather-stream")) {
        return $true
    }
    $status = Get-ChainStatus
    if ($null -eq $status) {
        return $false
    }
    $weather = Get-PropertyValue $status "weather"
    $signal = Get-PropertyValue $status "signal"
    $marketReady = Test-MarketReady $status
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
    param([Parameter(Mandatory = $true)][string]$AttemptId)

    switch ($DaemonName) {
        "market-supervisor" {
            $commandArguments = @(
                "market-supervisor", "--runtime", "0", "--config", $configPath,
                "--data-dir", $dataDir, "--startup-attempt-id", $AttemptId
            )
            if ($RawMarketRecovery) {
                $commandArguments += "--raw-collection-recovery"
            }
            return $commandArguments
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
        "paper-spread-engine" {
            return @(
                "paper-spread-engine", "--supervised", "--runtime", "0", "--data-dir", $dataDir,
                "--strategy-config", (Join-Path $ProjectRoot "configs\paper_spread_strategy_v1.json"),
                "--ledger", (Join-Path $dataDir "raw\shadow_orders\paper_spread_v1_orders.jsonl"),
                "--status", (Join-Path $dataDir "runtime\paper_spread_v1_status.json"),
                "--cursor", (Join-Path $dataDir "runtime\paper_spread_v1_cursor.json")
            )
        }
    }
    throw "Unsupported daemon: $DaemonName"
}

$runnerSourceSha256 = Get-FileSha256 -Path $PSCommandPath
$createdNew = $false
$mutex = [Threading.Mutex]::new($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-RunnerLog "another runner owns the single-instance lock; exiting"
    exit 0
}

try {
    $proxyNames = @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    $proxyPresent = @($proxyNames | Where-Object { Test-Path "Env:\$_" })
    Write-RunnerLog (
        "runner started; exe={0}; runner_sha256={1}; raw_market_recovery={2}; proxy variable names present={3}; execution_enabled=false" -f
        $exePath, $runnerSourceSha256, [bool]$RawMarketRecovery, ($proxyPresent -join ",")
    )
    $failureStreak = 0
    $restartTimes = [Collections.Generic.Queue[datetime]]::new()

    while ($true) {
        if (Test-Path -LiteralPath $childUnresolvedPath) {
            throw "Unresolved child ownership; manual reconciliation required: $childUnresolvedPath"
        }
        Wait-ForDependencies
        $attemptStarted = (Get-Date).ToUniversalTime()
        $attemptId = "{0}-{1}" -f $attemptStarted.ToString("yyyyMMddTHHmmssfffffffZ"), ([guid]::NewGuid().ToString("N"))
        $attemptDir = Join-Path $attemptRoot $attemptId
        $attemptStdoutPath = Join-Path $attemptDir "stdout.log"
        $attemptStderrPath = Join-Path $attemptDir "stderr.log"
        $attemptStartPath = Join-Path $attemptDir "attempt-start.json"
        $attemptResultPath = Join-Path $attemptDir "attempt-result.json"
        New-Item -ItemType Directory -Path $attemptDir | Out-Null
        $arguments = Get-ArgumentList -AttemptId $attemptId
        $attemptStart = [ordered]@{
            schema_version = 1
            attempt_id = $attemptId
            daemon_name = $DaemonName
            state = "starting"
            started_at = $attemptStarted.ToString("o")
            runner_pid = $PID
            runner_source_sha256 = $runnerSourceSha256
            raw_market_recovery = [bool]$RawMarketRecovery
            stdout_path = $attemptStdoutPath
            stderr_path = $attemptStderrPath
            execution_enabled = $false
        }
        Write-JsonAtomically -Path $attemptStartPath -Payload $attemptStart
        Write-JsonAtomically -Path $latestAttemptPath -Payload $attemptStart
        Write-RunnerLog "attempt started; attempt_id=$attemptId; evidence_dir=$attemptDir"

        $child = $null
        $childPid = $null
        $actualExitCode = $null
        $runnerFailureCode = $null
        $launchError = $null
        $childExitConfirmed = $false
        # Persist before launch: even loss of the process handle must block a new writer.
        Write-JsonAtomically -Path $childUnresolvedPath -Payload $attemptStart
        try {
            $child = Start-Process -FilePath $exePath -ArgumentList $arguments -WorkingDirectory $ProjectRoot `
                -RedirectStandardOutput $attemptStdoutPath -RedirectStandardError $attemptStderrPath `
                -WindowStyle Hidden -PassThru
            $childPid = $child.Id
            # Force Windows PowerShell to retain the native process handle. Without
            # this access, ExitCode can remain unavailable even after WaitForExit.
            $processHandle = $child.Handle
            Write-RunnerLog "child started; attempt_id=$attemptId; pid=$childPid"
            $child.WaitForExit()
            $childExitConfirmed = $child.HasExited -eq $true
            $rawExitCode = $child.ExitCode
            if ($null -ne $rawExitCode -and $childExitConfirmed) {
                $actualExitCode = [int]$rawExitCode
            }
        }
        catch {
            $runnerFailureCode = 9009
            $launchError = [ordered]@{
                error_type = $_.Exception.GetType().Name
                error = $_.Exception.Message
            }
            Write-RunnerLog "child launch/wait failed; attempt_id=$attemptId; error_type=$($launchError.error_type)"
        }
        finally {
            if ($null -ne $child) {
                if (-not $childExitConfirmed) {
                    try { $childExitConfirmed = $child.HasExited -eq $true } catch { }
                }
                try { $child.Dispose() } catch { }
            }
        }
        if ($childExitConfirmed) {
            Remove-Item -LiteralPath $childUnresolvedPath -Force
        }
        if ($null -eq $actualExitCode -and $null -eq $runnerFailureCode) {
            $runnerFailureCode = 9008
            Write-RunnerLog "child exit code unavailable; attempt_id=$attemptId; preserving actual_exit_code=null"
        }

        $diagnostic = Get-StartupDiagnostic -Path $attemptStderrPath -AttemptId $attemptId
        $diagnosticIsTerminal = (
            $null -ne $diagnostic -and
            (Get-PropertyValue $diagnostic "terminal") -eq $true
        )
        $stage = if ($null -ne $launchError) {
            "launch"
        }
        elseif ($diagnosticIsTerminal) {
            [string](Get-PropertyValue $diagnostic "stage")
        }
        else {
            "process_runtime"
        }
        $outcome = if ($diagnosticIsTerminal) {
            [string](Get-PropertyValue $diagnostic "outcome")
        }
        elseif ($actualExitCode -eq 0) {
            "completed"
        }
        else {
            "failed"
        }
        $attemptEnded = (Get-Date).ToUniversalTime()
        $attemptResult = [ordered]@{
            schema_version = 1
            attempt_id = $attemptId
            daemon_name = $DaemonName
            state = $outcome
            stage = $stage
            started_at = $attemptStarted.ToString("o")
            ended_at = $attemptEnded.ToString("o")
            child_pid = $childPid
            child_exit_confirmed = $childExitConfirmed
            restart_blocked = -not $childExitConfirmed
            exit_code = $actualExitCode
            exit_code_source = if ($null -ne $actualExitCode) { "process" } elseif ($null -ne $launchError) { "launch_error" } else { "unavailable" }
            runner_failure_code = $runnerFailureCode
            runner_source_sha256 = $runnerSourceSha256
            raw_market_recovery = [bool]$RawMarketRecovery
            startup_diagnostic = $diagnostic
            launch_error = $launchError
            stdout = Get-LogEvidence -Path $attemptStdoutPath
            stderr = Get-LogEvidence -Path $attemptStderrPath
            execution_enabled = $false
        }
        Write-JsonAtomically -Path $attemptResultPath -Payload $attemptResult
        Write-JsonAtomically -Path $latestAttemptPath -Payload $attemptResult
        if (-not $childExitConfirmed) {
            Write-RunnerLog "restart blocked; child exit unconfirmed; attempt_id=$attemptId"
            throw "Child exit unconfirmed; durable ownership latch retained"
        }
        Update-CompatibilityLog -Source $attemptStdoutPath -Destination $stdoutPath
        Update-CompatibilityLog -Source $attemptStderrPath -Destination $stderrPath

        $exitLabel = if ($null -eq $actualExitCode) { "unavailable" } else { [string]$actualExitCode }
        Write-RunnerLog "attempt finished; attempt_id=$attemptId; stage=$stage; outcome=$outcome; actual_exit_code=$exitLabel"
        $effectiveExitCode = if ($null -ne $actualExitCode) { $actualExitCode } else { $runnerFailureCode }
        if ($effectiveExitCode -eq 0) {
            Write-RunnerLog "child exited cleanly with actual code 0; attempt_id=$attemptId; runner stopped"
            exit 0
        }

        $failureStreak++
        $now = Get-Date
        $restartTimes.Enqueue($now)
        while ($restartTimes.Count -gt 0 -and ($now - $restartTimes.Peek()).TotalSeconds -gt $RestartWindowSeconds) {
            [void]$restartTimes.Dequeue()
        }
        if ($restartTimes.Count -ge $MaxRestartsInWindow) {
            Write-RunnerLog "child failed; attempt_id=$attemptId; actual_exit_code=$exitLabel; runner_failure_code=$runnerFailureCode; crash-loop guard engaged; failures_in_window=$($restartTimes.Count); sleeping ${CrashLoopPauseSeconds}s"
            Start-Sleep -Seconds $CrashLoopPauseSeconds
            $failureStreak = 0
            continue
        }
        $power = [math]::Min($failureStreak - 1, 8)
        $delay = [int][math]::Min($MaxBackoffSeconds, $InitialBackoffSeconds * [math]::Pow(2, $power))
        Write-RunnerLog "child failed; attempt_id=$attemptId; actual_exit_code=$exitLabel; runner_failure_code=$runnerFailureCode; exponential backoff ${delay}s; failures_in_window=$($restartTimes.Count)"
        Start-Sleep -Seconds $delay
    }
}
finally {
    if ($createdNew) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    $mutex.Dispose()
}
