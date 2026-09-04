param(
    [string]$MihomoDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $MihomoDir) {
    $MihomoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\static\proxy\mihomo-boss")).Path
}

$pidFile = Join-Path $MihomoDir "mihomo-boss.pid"
if (-not (Test-Path $pidFile)) {
    Write-Host "mihomo-boss pid file not found; nothing to stop"
    exit 0
}

$pidValue = Get-Content -Path $pidFile -ErrorAction SilentlyContinue
if (-not $pidValue) {
    Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
    Write-Host "mihomo-boss pid file empty; removed"
    exit 0
}

$process = Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $process.Id -Force
    Write-Host "mihomo-boss stopped pid=$pidValue"
} else {
    Write-Host "mihomo-boss pid=$pidValue not running"
}

Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
