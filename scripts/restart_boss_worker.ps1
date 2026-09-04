param(
    [Parameter(Mandatory = $true)]
    [string]$WorkerId,
    [string]$ProxyId = "",
    [string]$EnvFile = ".env",
    [string]$WorkersFile = "",
    [string]$ProxyFile = "",
    [string]$ChromePath = $env:CHROME_PATH,
    [string]$ProfileRoot = "runtime\chrome-profiles",
    [string]$StateRoot = "runtime\boss-workers",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$stopArgs = @{
    WorkerId = $WorkerId
    EnvFile = $EnvFile
    StateRoot = $StateRoot
}
if ($WorkersFile) {
    $stopArgs.WorkersFile = $WorkersFile
}
if ($DryRun) {
    $stopArgs.DryRun = $true
}
& "$PSScriptRoot\stop_boss_worker.ps1" @stopArgs

$startArgs = @{
    EnvFile = $EnvFile
    ProfileRoot = $ProfileRoot
    StateRoot = $StateRoot
    WorkerId = $WorkerId
}

if ($ChromePath) {
    $startArgs.ChromePath = $ChromePath
}
if ($WorkersFile) {
    $startArgs.WorkersFile = $WorkersFile
}
if ($ProxyFile) {
    $startArgs.ProxyFile = $ProxyFile
}
if ($DryRun) {
    $startArgs.DryRun = $true
}

if ($ProxyId) {
    $startArgs.ProxyId = $ProxyId
}

& "$PSScriptRoot\start_boss_workers.ps1" @startArgs
