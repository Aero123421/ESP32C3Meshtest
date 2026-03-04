[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Ports,

    [string]$LogRoot = "",

    [string]$Operator = $env:USERNAME,

    [string]$Scenario = "indoor_baseline",

    [string]$Channel = "1",

    [string]$Antenna = "default",

    [string]$PlatformIoEnv = "seeed_xiao_esp32c3"
)

$ErrorActionPreference = "Stop"

$parsedPorts = @(
    $Ports |
    ForEach-Object { $_ -split "," } |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }
)
$Ports = @()
$seenPorts = @{}
foreach ($port in $parsedPorts) {
    $key = $port.ToUpperInvariant()
    if ($seenPorts.ContainsKey($key)) {
        continue
    }
    $seenPorts[$key] = $true
    $Ports += $port
}
if ($parsedPorts.Count -ne $Ports.Count) {
    $dupCount = $parsedPorts.Count - $Ports.Count
    Write-Host ("WARN: duplicate ports removed ({0}) -> {1}" -f $dupCount, ($Ports -join ", ")) -ForegroundColor Yellow
}

if ($Ports.Count -lt 1) {
    throw "Specify at least one port. Example: -Ports COM5,COM6,COM7"
}

if ([string]::IsNullOrWhiteSpace($LogRoot)) {
    $scriptRoot = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($scriptRoot) -and $MyInvocation.MyCommand.Path) {
        $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
        $scriptRoot = (Get-Location).Path
    }
    $LogRoot = Join-Path (Resolve-Path (Join-Path $scriptRoot "..")).Path "test_logs"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$sessionDir = Join-Path $LogRoot $timestamp

New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null

$gitSha = ""
try {
    $gitSha = (git -C (Join-Path $PSScriptRoot "..") rev-parse --short HEAD 2>$null).Trim()
} catch {
    $gitSha = ""
}

$sessionFile = Join-Path $sessionDir "session.md"
$portLines = @()
for ($i = 0; $i -lt $Ports.Count; $i++) {
    $portLines += ("  - Node{0}: {1}" -f ($i + 1), $Ports[$i])
}
$portBlock = $portLines -join [Environment]::NewLine
$template = @"
# Test Session Record

- DateTime: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
- Operator: $Operator
- Node Count: $($Ports.Count)
- Port Map:
$portBlock
- PC Map (example for 10 nodes / 3 PCs):
  - PC1:
  - PC2:
  - PC3:
- Role Map (source/sink/relay):
  - 

## Test Context
- Test ID:
- Location:
- Channel:
- Antenna:
- Power:

## Result
- Pass:
- Failure:
- Reproduction:
- Next Action:
"@

Set-Content -Path $sessionFile -Value $template -Encoding UTF8

$sessionJsonFile = Join-Path $sessionDir "session.json"
$sessionId = Split-Path -Leaf $sessionDir
$sessionPayload = [ordered]@{
    session_id = $sessionId
    created_at = (Get-Date).ToString("o")
    operator = $Operator
    scenario = $Scenario
    channel = $Channel
    antenna = $Antenna
    platformio_env = $PlatformIoEnv
    git_sha = $gitSha
    ports = @($Ports)
    node_alias = @(
        for ($i = 0; $i -lt $Ports.Count; $i++) {
            [ordered]@{
                alias = "Node$($i + 1)"
                port = $Ports[$i]
            }
        }
    )
    command_lines = [ordered]@{
        flash_all = ".\tools\flash_all.ps1 -Ports $($Ports -join ',') -Environment $PlatformIoEnv -SessionDir $sessionDir"
        monitor_all = ".\tools\monitor_all.ps1 -Ports $($Ports -join ',') -SessionDir $sessionDir -Baud 115200"
    }
}
$sessionPayload | ConvertTo-Json -Depth 8 | Set-Content -Path $sessionJsonFile -Encoding UTF8

Write-Host ("Session folder created: {0}" -f $sessionDir) -ForegroundColor Green
Write-Host ("Record template: {0}" -f $sessionFile) -ForegroundColor Green
Write-Host ("Session metadata: {0}" -f $sessionJsonFile) -ForegroundColor Green
Write-Host ""
Write-Host "Recommended next commands:" -ForegroundColor Cyan
Write-Host ("  .\tools\flash_all.ps1 -Ports {0} -Environment {1} -SessionDir {2}" -f ($Ports -join ","), $PlatformIoEnv, $sessionDir) -ForegroundColor Yellow
Write-Host ("  .\tools\monitor_all.ps1 -Ports {0} -SessionDir {1}" -f ($Ports -join ","), $sessionDir) -ForegroundColor Yellow
