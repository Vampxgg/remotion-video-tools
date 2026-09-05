param(
    [string]$EnvFile = ".env",
    [string]$WorkersFile = "",
    [string]$ProxyFile = "",
    [string]$ChromePath = $env:CHROME_PATH,
    [string]$ProfileRoot = "runtime\chrome-profiles",
    [string]$StateRoot = "runtime\boss-workers",
    [string]$WorkerId = "",
    [string]$ProxyId = "",
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

function Find-Chrome {
    param([string]$Configured)
    if ($Configured -and (Test-Path $Configured)) {
        return $Configured
    }
    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "$programFilesX86\Google\Chrome\Application\chrome.exe",
        "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }
    throw "chrome.exe not found; pass -ChromePath or set CHROME_PATH."
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
    param([string]$Path, [string]$Name)
    if (-not (Test-Path $Path)) {
        throw "$Name file not found: $Path"
    }
    $value = Get-Content -Path $Path -Encoding UTF8 -Raw | ConvertFrom-Json
    return @($value)
}

function Split-HostPort {
    param([string]$HostPort)
    $parts = $HostPort.Split(":")
    if ($parts.Count -ne 2) {
        throw "browser_host_port must be host:port, got $HostPort"
    }
    return @{ Host = $parts[0]; Port = [int]$parts[1] }
}

$resolvedEnvFile = Resolve-Path $EnvFile -ErrorAction SilentlyContinue
if ($resolvedEnvFile) {
    $envDir = Split-Path -Parent $resolvedEnvFile
} else {
    $envDir = (Get-Location).Path
}

$workersFileFromEnv = Read-EnvValue -Path $EnvFile -Name "BOSS_ZHIPIN_WORKERS_FILE"
$proxyFileFromEnv = Read-EnvValue -Path $EnvFile -Name "BOSS_ZHIPIN_PROXY_POOL_FILE"
$resolvedWorkersFile = Resolve-ConfigPath -RawPath $(if ($WorkersFile) { $WorkersFile } else { $workersFileFromEnv }) -BaseDir $envDir
$resolvedProxyFile = Resolve-ConfigPath -RawPath $(if ($ProxyFile) { $ProxyFile } else { $proxyFileFromEnv }) -BaseDir $envDir

if ($resolvedWorkersFile) {
    $workers = Read-JsonListFromFile -Path $resolvedWorkersFile -Name "BOSS_ZHIPIN_WORKERS_FILE"
} else {
    $workersJson = Read-EnvValue -Path $EnvFile -Name "BOSS_ZHIPIN_WORKERS"
    if (-not $workersJson) {
        throw "BOSS_ZHIPIN_WORKERS or BOSS_ZHIPIN_WORKERS_FILE not found."
    }
    $workers = @($workersJson | ConvertFrom-Json)
}

$proxyPool = @()
if ($resolvedProxyFile) {
    $proxyPool = Read-JsonListFromFile -Path $resolvedProxyFile -Name "BOSS_ZHIPIN_PROXY_POOL_FILE"
} else {
    $proxyPoolJson = Read-EnvValue -Path $EnvFile -Name "BOSS_ZHIPIN_PROXY_POOL"
    if ($proxyPoolJson) {
        $proxyPool = @($proxyPoolJson | ConvertFrom-Json)
    }
}

$proxyById = @{}
$availableProxyIds = New-Object System.Collections.Queue
foreach ($proxy in @($proxyPool)) {
    if ($proxy.proxy_id) {
        $proxyById[$proxy.proxy_id] = $proxy
        if ($null -eq $proxy.enabled -or $proxy.enabled -eq $true) {
            $availableProxyIds.Enqueue($proxy.proxy_id)
        }
    }
}

if (-not $DryRun) {
    $chrome = Find-Chrome -Configured $ChromePath
} else {
    $chrome = if ($ChromePath) { $ChromePath } else { "chrome.exe" }
}

# 绝对化 ProfileRoot/StateRoot：Git Bash / 服务进程 / PowerShell 当前目录不一致时，
# 相对 --user-data-dir 会让 Chrome profile 路径漂移（曾导致 "无法读写数据目录" 弹窗
# 与临时 profile 混用）。以项目根（脚本上级目录）为基准解析成绝对路径。
$projectRoot = Split-Path -Parent $PSScriptRoot
function Resolve-AbsoluteRoot {
    param([string]$RawPath)
    if ([System.IO.Path]::IsPathRooted($RawPath)) {
        return $RawPath
    }
    return (Join-Path $projectRoot $RawPath)
}
$ProfileRoot = Resolve-AbsoluteRoot -RawPath $ProfileRoot
$StateRoot = Resolve-AbsoluteRoot -RawPath $StateRoot

New-Item -ItemType Directory -Force -Path $ProfileRoot | Out-Null
New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

$usedProxyIds = @{}
$matchedWorker = $false

foreach ($worker in @($workers)) {
    if (-not $worker.worker_id) {
        throw "BOSS_ZHIPIN_WORKERS contains an item without worker_id."
    }
    if ($WorkerId -and $worker.worker_id -ne $WorkerId) {
        continue
    }
    $matchedWorker = $true
    if (-not $worker.browser_host_port) {
        throw "worker=$($worker.worker_id) missing browser_host_port."
    }

    $hostPort = Split-HostPort -HostPort $worker.browser_host_port
    $profileId = if ($worker.profile_id) { $worker.profile_id } else { $worker.worker_id }
    $profileDir = Join-Path $ProfileRoot $profileId
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

    $selectedProxyId = if ($ProxyId) { $ProxyId } else { $worker.proxy_id }
    if (-not $selectedProxyId -and $proxyPool.Count -gt 0) {
        while ($availableProxyIds.Count -gt 0 -and $usedProxyIds.ContainsKey($availableProxyIds.Peek())) {
            [void]$availableProxyIds.Dequeue()
        }
        if ($availableProxyIds.Count -eq 0) {
            throw "No available proxy left for worker=$($worker.worker_id)."
        }
        $selectedProxyId = $availableProxyIds.Dequeue()
    }

    $chromeProxyServer = $worker.chrome_proxy_server
    if ($selectedProxyId) {
        if (-not $proxyById.ContainsKey($selectedProxyId)) {
            throw "worker=$($worker.worker_id) references missing proxy_id=$selectedProxyId."
        }
        $usedProxyIds[$selectedProxyId] = $true
        if (-not $chromeProxyServer) {
            $chromeProxyServer = $proxyById[$selectedProxyId].chrome_proxy_server
        }
        if (-not $chromeProxyServer) {
            throw "worker=$($worker.worker_id) proxy_id=$selectedProxyId missing chrome_proxy_server."
        }
    }

    $args = @(
        "--remote-debugging-address=$($hostPort.Host)",
        "--remote-debugging-port=$($hostPort.Port)",
        "--user-data-dir=$profileDir",
        "--no-first-run",
        "--no-default-browser-check"
    )
    if ($chromeProxyServer) {
        $args += "--proxy-server=$chromeProxyServer"
    }

    Write-Host "BOSS worker $($worker.worker_id) port=$($worker.browser_host_port) profile=$profileId proxy_id=$selectedProxyId proxy=$([bool]$chromeProxyServer)"
    if ($DryRun) {
        Write-Host "DRY RUN: $chrome $($args -join ' ')"
    } else {
        $process = Start-Process -FilePath $chrome -ArgumentList $args -PassThru
        $snapshot = [ordered]@{
            worker_id = $worker.worker_id
            pid = $process.Id
            browser_host_port = $worker.browser_host_port
            profile_id = $profileId
            proxy_id = $selectedProxyId
            chrome_proxy_server = $chromeProxyServer
            started_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        }
        $statePath = Join-Path $StateRoot "$($worker.worker_id).json"
        $snapshot | ConvertTo-Json -Depth 5 | Set-Content -Path $statePath -Encoding UTF8
    }
}

if ($WorkerId -and -not $matchedWorker) {
    throw "worker_id not found: $WorkerId"
}
