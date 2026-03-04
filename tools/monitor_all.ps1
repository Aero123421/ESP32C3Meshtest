[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Ports,

    [int]$Baud = 115200,

    [string]$ProjectDir = "",

    [string]$LogDir = "",

    [string]$SessionDir = ""
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

if ([string]::IsNullOrWhiteSpace($ProjectDir)) {
    $scriptRoot = $PSScriptRoot
    if ([string]::IsNullOrWhiteSpace($scriptRoot) -and $MyInvocation.MyCommand.Path) {
        $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    if ([string]::IsNullOrWhiteSpace($scriptRoot)) {
        $scriptRoot = (Get-Location).Path
    }
    $ProjectDir = (Resolve-Path (Join-Path $scriptRoot "..")).Path
}

$pioCmd = Get-Command pio -ErrorAction SilentlyContinue
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pioCmd -and -not $pythonCmd) {
    throw "Neither pio nor python was found. Check PlatformIO runtime."
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (-not [string]::IsNullOrWhiteSpace($SessionDir)) {
    New-Item -ItemType Directory -Path $SessionDir -Force | Out-Null
    if ([string]::IsNullOrWhiteSpace($LogDir)) {
        $LogDir = Join-Path $SessionDir "monitor"
    }
}
if ([string]::IsNullOrWhiteSpace($LogDir)) {
    $LogDir = Join-Path $ProjectDir ("test_logs\monitor_" + $timestamp)
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$manifestFile = if (-not [string]::IsNullOrWhiteSpace($SessionDir)) { Join-Path $SessionDir "monitor_manifest.json" } else { Join-Path $LogDir "monitor_manifest.json" }
$manifestItems = @()

for ($i = 0; $i -lt $Ports.Count; $i++) {
    $port = $Ports[$i]
    $node = "Node$($i + 1)"
    $title = "PIO Monitor - $node ($port)"
    $safePort = $port.Replace(":", "_").Replace("\", "_").Replace("/", "_")
    $logFile = Join-Path $LogDir ("{0}_{1}.log" -f $node, $safePort)
    $safeLogFile = $logFile.Replace("'", "''")

    $safeProjectDir = $ProjectDir.Replace("'", "''")
    if ($pioCmd) {
        $command = @"
Set-Location '$safeProjectDir'
`$Host.UI.RawUI.WindowTitle = '$title'
Write-Host 'Log file: $safeLogFile' -ForegroundColor DarkGray
& pio device monitor --port '$port' --baud $Baud 2>&1 | Tee-Object -FilePath '$safeLogFile' -Append
"@
    }
    else {
        $command = @"
Set-Location '$safeProjectDir'
`$Host.UI.RawUI.WindowTitle = '$title'
Write-Host 'Log file: $safeLogFile' -ForegroundColor DarkGray
& python -m platformio device monitor --port '$port' --baud $Baud 2>&1 | Tee-Object -FilePath '$safeLogFile' -Append
"@
    }

    $proc = Start-Process powershell -PassThru -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $command
    )
    Start-Sleep -Milliseconds 150
    if ($proc.HasExited) {
        throw ("Failed to start monitor process for {0}. ExitCode={1}" -f $port, $proc.ExitCode)
    }

    $manifestItems += [ordered]@{
        node = $node
        port = $port
        baud = $Baud
        log_file = $logFile
        process_id = $proc.Id
        started_at = (Get-Date).ToString("o")
    }
    Write-Host ("Monitor started: {0} ({1}) -> {2}" -f $node, $port, $logFile) -ForegroundColor Green
}

Write-Host ("Started monitors for {0} ports. Log root: {1}" -f $Ports.Count, $LogDir) -ForegroundColor Cyan
$manifestPayload = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    project_dir = $ProjectDir
    log_dir = $LogDir
    session_dir = $SessionDir
    ports = @($Ports)
    items = $manifestItems
}
$manifestPayload | ConvertTo-Json -Depth 6 | Set-Content -Path $manifestFile -Encoding UTF8
Write-Host ("Monitor manifest: {0}" -f $manifestFile) -ForegroundColor Cyan
