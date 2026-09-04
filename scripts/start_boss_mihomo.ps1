param(
    [string]$MihomoDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $MihomoDir) {
    $MihomoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\static\proxy\mihomo-boss")).Path
}

$exe = Join-Path $MihomoDir "mihomo.exe"
$config = Join-Path $MihomoDir "config.yaml"
$pidFile = Join-Path $MihomoDir "mihomo-boss.pid"
$logDir = Join-Path $MihomoDir "logs"
$stdout = Join-Path $logDir "mihomo-boss.out.log"
$stderr = Join-Path $logDir "mihomo-boss.err.log"

if (-not (Test-Path $exe)) {
    throw "mihomo executable not found: $exe"
}
if (-not (Test-Path $config)) {
    throw "mihomo config not found: $config"
}

if (Test-Path $pidFile) {
    $oldPid = Get-Content -Path $pidFile -ErrorAction SilentlyContinue
    if ($oldPid) {
        $oldProcess = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($oldProcess) {
            Write-Host "mihomo-boss already running pid=$oldPid"
            exit 0
        }
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

& $exe -t -d $MihomoDir
if ($LASTEXITCODE -ne 0) {
    throw "mihomo config test failed"
}

$process = Start-Process `
    -FilePath $exe `
    -ArgumentList @("-d", $MihomoDir) `
    -WorkingDirectory $MihomoDir `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII
Write-Host "mihomo-boss started pid=$($process.Id) dir=$MihomoDir"
