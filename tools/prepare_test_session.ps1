[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Ports,

    [string]$LogRoot = "",

    [string]$Operator = $env:USERNAME
)

$ErrorActionPreference = "Stop"

$Ports = @(
    $Ports |
    ForEach-Object { $_ -split "," } |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }
)

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

Write-Host ("Session folder created: {0}" -f $sessionDir) -ForegroundColor Green
Write-Host ("Record template: {0}" -f $sessionFile) -ForegroundColor Green
Write-Host ""
Write-Host "Recommended next commands:" -ForegroundColor Cyan
Write-Host ("  .\tools\flash_all.ps1 -Ports {0}" -f ($Ports -join ",")) -ForegroundColor Yellow
Write-Host ("  .\tools\monitor_all.ps1 -Ports {0} -LogDir {1}" -f ($Ports -join ","), $sessionDir) -ForegroundColor Yellow
