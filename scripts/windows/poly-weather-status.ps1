[CmdletBinding()]
param(
    [string]$ProjectRoot = "D:\poly"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
$exePath = Join-Path $ProjectRoot ".venv\Scripts\poly-weather.exe"
$dataDir = Join-Path $ProjectRoot "data"
& $exePath stream-status --data-dir $dataDir
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
