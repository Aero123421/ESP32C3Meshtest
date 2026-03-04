[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Ports,

    [string]$Environment = "seeed_xiao_esp32c3",

    [int]$Baud = 115200,

    [double]$BootTimeout = 25,

    [double]$SmokeTimeout = 45,

    [double]$AckTimeout = 4,

    [int]$AckRetries = 6,

    [int]$Rounds = 12,

    [int]$IntervalMs = 700,

    [string]$Scenario = "indoor_baseline",

    [string]$ThresholdFile = ".\docs\reliable_1k_thresholds.json",

    [switch]$SkipFlash,

    [switch]$StartMonitor,

    [switch]$AllowMissingDeliveryAck
)

$ErrorActionPreference = "Stop"

function Write-JsonNoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [object]$Data,
        [int]$Depth = 16
    )
    $json = $Data | ConvertTo-Json -Depth $Depth
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

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
if ($Ports.Count -lt 3) {
    throw "Specify at least three ports. Example: -Ports COM5,COM6,COM7"
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logRoot = Join-Path $projectRoot "test_logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

Write-Host "== Step 1/5: session prepare ==" -ForegroundColor Cyan
$beforeSessions = @{}
Get-ChildItem -Directory $logRoot | ForEach-Object {
    $beforeSessions[$_.FullName.ToLowerInvariant()] = $true
}

& (Join-Path $PSScriptRoot "prepare_test_session.ps1") -Ports $Ports -Scenario $Scenario -PlatformIoEnv $Environment

$newSessions = @(
    Get-ChildItem -Directory $logRoot |
    Where-Object { -not $beforeSessions.ContainsKey($_.FullName.ToLowerInvariant()) } |
    Sort-Object CreationTime -Descending
)
if ($newSessions.Count -gt 0) {
    $sessionDir = $newSessions[0].FullName
} else {
    Write-Host "WARN: new session directory detection failed. Fallback to latest LastWriteTime." -ForegroundColor Yellow
    $latestSession = Get-ChildItem -Directory $logRoot |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latestSession) { throw "session directory not found." }
    $sessionDir = $latestSession.FullName
}
Write-Host ("Session: {0}" -f $sessionDir) -ForegroundColor Green

if (-not $SkipFlash) {
    Write-Host "== Step 2/5: flash ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "flash_all.ps1") -Ports $Ports -Environment $Environment -SessionDir $sessionDir
} else {
    Write-Host "== Step 2/5: flash (skipped) ==" -ForegroundColor Yellow
}

if ($StartMonitor) {
    Write-Host "== Step 3/5: monitor spawn ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "monitor_all.ps1") -Ports $Ports -Baud $Baud -SessionDir $sessionDir
} else {
    Write-Host "== Step 3/5: monitor spawn (skipped) ==" -ForegroundColor Yellow
}

$runId = Get-Date -Format "yyyyMMdd_HHmmss"
$summaryJson = Join-Path $sessionDir ("smoke\{0}_summary.json" -f $runId)
$roundsJsonl = Join-Path $sessionDir ("smoke\{0}_rounds.jsonl" -f $runId)
$eventsJsonl = Join-Path $sessionDir ("smoke\{0}_events.jsonl" -f $runId)
$smokeLog = Join-Path $sessionDir ("smoke\{0}_smoke.log" -f $runId)
$smokeExitCode = 1

Write-Host "== Step 4/5: mesh smoke ==" -ForegroundColor Cyan
Push-Location $projectRoot
try {
    $smokeArgs = @(
        ".\tools\mesh_smoke_test.py"
        "--ports"
    )
    $smokeArgs += $Ports
    $smokeArgs += @(
        "--baud", $Baud
        "--boot-timeout", $BootTimeout
        "--timeout", $SmokeTimeout
        "--ack-timeout", $AckTimeout
        "--ack-retries", $AckRetries
        "--skip-ble"
        "--rounds", $Rounds
        "--interval-ms", $IntervalMs
        "--rotate-tx"
        "--collect-stats"
        "--scenario", $Scenario
        "--run-id", $runId
        "--session-dir", $sessionDir
        "--threshold-file", $ThresholdFile
        "--strict-pass"
        "--jsonl-out", $roundsJsonl
        "--events-jsonl", $eventsJsonl
        "--summary-json", $summaryJson
    )
    if (-not $AllowMissingDeliveryAck) {
        $smokeArgs += "--require-delivery-ack"
    }
    & py -3 @smokeArgs 2>&1 | Tee-Object -FilePath $smokeLog
    $smokeExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($smokeExitCode -ne 0) {
    Write-Host ("WARN: mesh_smoke_test.py failed exit_code={0}" -f $smokeExitCode) -ForegroundColor Yellow
}

if (-not (Test-Path $summaryJson)) {
    Write-Host "WARN: summary json missing. Generating fallback summary payload." -ForegroundColor Yellow
    $fallbackSummary = [ordered]@{
        timestamp_ms = [int64][DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        run_id = $runId
        scenario = $Scenario
        ports = @($Ports)
        smoke_exit_code = $smokeExitCode
        failure_stage = "mesh_smoke_test"
        failure_reason = if ($smokeExitCode -ne 0) { "mesh_smoke_test_exit_nonzero" } else { "summary_missing" }
        round_summary = [ordered]@{
            threshold_violations = @()
            threshold_pass = $false
        }
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $summaryJson) -Force | Out-Null
    Write-JsonNoBom -Path $summaryJson -Data $fallbackSummary -Depth 8
}
else {
    try {
        $summaryObj = Get-Content -Raw -Path $summaryJson | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $summaryObj) {
            throw "summary json parse returned null"
        }
        if ($summaryObj.PSObject.Properties.Match("smoke_exit_code").Count -eq 0) {
            $summaryObj | Add-Member -NotePropertyName "smoke_exit_code" -NotePropertyValue $smokeExitCode
        } else {
            $summaryObj.smoke_exit_code = $smokeExitCode
        }
        if ($smokeExitCode -ne 0) {
            if ($summaryObj.PSObject.Properties.Match("failure_stage").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "failure_stage" -NotePropertyValue "mesh_smoke_test"
            } elseif (-not $summaryObj.failure_stage) {
                $summaryObj.failure_stage = "mesh_smoke_test"
            }
            if ($summaryObj.PSObject.Properties.Match("failure_reason").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "failure_reason" -NotePropertyValue "mesh_smoke_test_exit_nonzero"
            } elseif (-not $summaryObj.failure_reason) {
                $summaryObj.failure_reason = "mesh_smoke_test_exit_nonzero"
            }
        }
        Write-JsonNoBom -Path $summaryJson -Data $summaryObj -Depth 16
    }
    catch {
        Write-Host ("WARN: failed to update summary metadata: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
}

Write-Host "== Step 5/5: triage ==" -ForegroundColor Cyan
$triageReport = Join-Path $sessionDir ("triage\{0}_triage_report.md" -f $runId)
$triageBundle = Join-Path $sessionDir ("triage\{0}_failure_bundle.json" -f $runId)
$triageLogs = @()
if (Test-Path $smokeLog) {
    $triageLogs += $smokeLog
}
$monitorManifest = Join-Path $sessionDir "monitor_manifest.json"
if (Test-Path $monitorManifest) {
    try {
        $manifestObj = Get-Content -Raw -Path $monitorManifest | ConvertFrom-Json -ErrorAction Stop
        if ($null -ne $manifestObj.items) {
            foreach ($item in $manifestObj.items) {
                if ($null -eq $item) { continue }
                $logFile = [string]$item.log_file
                if (-not [string]::IsNullOrWhiteSpace($logFile) -and (Test-Path $logFile)) {
                    $triageLogs += $logFile
                }
            }
        }
    }
    catch {
        Write-Host ("WARN: failed to parse monitor manifest: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
    }
}

$triageArgs = @(
    (Join-Path $PSScriptRoot "triage_mesh_failure.py"),
    "--summary-json", $summaryJson,
    "--report-md", $triageReport,
    "--bundle-json", $triageBundle
)
if ($triageLogs.Count -gt 0) {
    $triageArgs += @("--logs")
    $triageArgs += $triageLogs
}
& py -3 @triageArgs
if ($LASTEXITCODE -ne 0) { throw "triage_mesh_failure.py failed." }

if ($smokeExitCode -ne 0) {
    throw ("mesh_smoke_test.py failed exit_code={0}" -f $smokeExitCode)
}

Write-Host "Regression completed." -ForegroundColor Green
Write-Host (" summary: {0}" -f $summaryJson) -ForegroundColor Green
Write-Host (" rounds : {0}" -f $roundsJsonl) -ForegroundColor Green
Write-Host (" triage : {0}" -f $triageReport) -ForegroundColor Green
