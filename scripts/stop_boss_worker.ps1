param(
    [Parameter(Mandatory = $true)]
    [string]$WorkerId,
    [string]$EnvFile = ".env",
    [string]$WorkersFile = "",
    [string]$StateRoot = "runtime\boss-workers",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Read-EnvValue {
    param([string]$Path, [string]$Name)
    if (-not (Test-Path $Path)) {
        return $null
    }
    $prefix = "$Name="
    foreach ($line in Get-Content -Path $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0 -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed.StartsWith($prefix)) {
            return $trimmed.Substring($prefix.Length).Trim()
        }
    }
    return $null
}

function Resolve-ConfigPath {
    param([string]$RawPath, [string]$BaseDir)
    if (-not $RawPath) {
        return ""
    }
    if ([System.IO.Path]::IsPathRooted($RawPath)) {
        return $RawPath
    }
    return (Join-Path $BaseDir $RawPath)
}

function Read-JsonListFromFile {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path $Path)) {
        return @()
    }
    $value = Get-Content -Path $Path -Encoding UTF8 -Raw | ConvertFrom-Json
    return @($value)
}

function Stop-ByPid {
    param([int]$PidValue, [string]$Reason)
    if ($PidValue -le 0) {
        return $false
    }
    $process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    if ($DryRun) {
        Write-Host "DRY RUN: stop BOSS worker $WorkerId pid=$PidValue ($Reason)"
    } else {
        try {
            Stop-Process -Id $PidValue -Force -ErrorAction Stop
            Write-Host "BOSS worker $WorkerId stopped pid=$PidValue ($Reason)"
        } catch [Microsoft.PowerShell.Commands.ProcessCommandException] {
            Write-Host "BOSS worker $WorkerId pid=$PidValue already exited ($Reason)"
        }
    }
    return $true
}

$resolvedEnvFile = Resolve-Path $EnvFile -ErrorAction SilentlyContinue
if ($resolvedEnvFile) {
    $envDir = Split-Path -Parent $resolvedEnvFile
} else {
    $envDir = (Get-Location).Path
}

$workersFileFromEnv = Read-EnvValue -Path $EnvFile -Name "BOSS_ZHIPIN_WORKERS_FILE"
$resolvedWorkersFile = Resolve-ConfigPath -RawPath $(if ($WorkersFile) { $WorkersFile } else { $workersFileFromEnv }) -BaseDir $envDir
$workerConfig = $null
foreach ($item in Read-JsonListFromFile -Path $resolvedWorkersFile) {
    if ($item.worker_id -eq $WorkerId) {
        $workerConfig = $item
        break
    }
}

$statePath = Join-Path $StateRoot "$WorkerId.json"
if (Test-Path $statePath) {
    $snapshot = Get-Content -Path $statePath -Encoding UTF8 -Raw | ConvertFrom-Json
    $pidValue = [int]$snapshot.pid
    if (Stop-ByPid -PidValue $pidValue -Reason "state file") {
        exit 0
    }
    Write-Host "BOSS worker $WorkerId pid=$pidValue from $statePath is not running"
} else {
    Write-Host "BOSS worker $WorkerId state file not found: $statePath"
}

if ($null -eq $workerConfig) {
    Write-Host "BOSS worker $WorkerId config not found; fallback stop skipped"
    exit 0
}

$hostPort = [string]$workerConfig.browser_host_port
$profileId = if ($workerConfig.profile_id) { [string]$workerConfig.profile_id } else { $WorkerId }
$port = ""
if ($hostPort.Contains(":")) {
    $port = $hostPort.Split(":")[-1]
}

$matches = @()
if ($port) {
    $matches += @(Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
        Where-Object { $_.CommandLine -like "*--remote-debugging-port=$port*" })
}
if ($profileId) {
    $matches += @(Get-CimInstance Win32_Process -Filter "name = 'chrome.exe'" |
        Where-Object { $_.CommandLine -like "*$profileId*" })
}

$processIds = @($matches | Where-Object { $_.ProcessId } | Select-Object -ExpandProperty ProcessId -Unique)
if ($processIds.Count -eq 0) {
    Write-Host "BOSS worker $WorkerId chrome process not found by port/profile fallback"
    exit 0
}

foreach ($processId in $processIds) {
    [void](Stop-ByPid -PidValue ([int]$processId) -Reason "port/profile fallback")
}
