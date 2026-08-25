$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$polyWeather = Join-Path $projectRoot ".venv\Scripts\poly-weather.exe"
$dataDir = Join-Path $projectRoot "data"
$logDir = Join-Path $dataDir "logs"
$reportPath = Join-Path $dataDir "wrh_backfill_daily_report.md"
$logPath = Join-Path $logDir "wrh_backfill_daily.log"
$settlementKeys = @(
    "new-york-daily-high-research-seed",
    "chicago-daily-high-research-seed",
    "los-angeles-daily-high-research-seed",
    "miami-daily-high-research-seed",
    "atlanta-daily-high-research-seed",
    "dallas-daily-high-research-seed",
    "houston-daily-high-research-seed",
    "seattle-daily-high-research-seed",
    "chongqing-daily-high-research-seed",
    "chengdu-daily-high-research-seed"
)

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
"[$(Get-Date -Format o)] WRH daily backfill starting" | Add-Content -LiteralPath $logPath
& $polyWeather wrh-backfill @settlementKeys `
    --config (Join-Path $projectRoot "configs\settlements.example.json") `
    --data-dir $dataDir `
    --output $reportPath 2>&1 | Add-Content -LiteralPath $logPath
if ($LASTEXITCODE -ne 0) {
    throw "WRH daily backfill failed with exit code $LASTEXITCODE"
}
"[$(Get-Date -Format o)] WRH daily backfill completed" | Add-Content -LiteralPath $logPath
