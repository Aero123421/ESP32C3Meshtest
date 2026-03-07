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

    [switch]$StartMonitor = $true,

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
    Write-Host "== Step 3/5: monitor spawn (deferred) ==" -ForegroundColor Yellow
    Write-Host "WARN: smoke test uses the COM ports exclusively, so live monitors are not attached before smoke." -ForegroundColor Yellow
} else {
    Write-Host "== Step 3/5: monitor spawn (skipped) ==" -ForegroundColor Yellow
}

$monitorLogFiles = @()

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
try {
    if (Test-Path $summaryJson) {
        $summaryObj = Get-Content -Raw -Path $summaryJson | ConvertFrom-Json -ErrorAction Stop
        if ($null -ne $summaryObj) {
            $expectedMonitorPorts = @()
            $missingMonitorPorts = @()
            $monitorCaptureMode = if ($StartMonitor) { "deferred_not_attached" } else { "disabled" }
            if ($StartMonitor) {
                $expectedMonitorPorts = @($Ports)
                $attachedPortMap = @{}
                foreach ($monitorPath in $monitorLogFiles) {
                    foreach ($candidatePort in $Ports) {
                        if ($monitorPath -match [regex]::Escape($candidatePort)) {
                            $attachedPortMap[$candidatePort.ToUpperInvariant()] = $true
                        }
                    }
                }
                foreach ($candidatePort in $Ports) {
                    if (-not $attachedPortMap.ContainsKey($candidatePort.ToUpperInvariant())) {
                        $missingMonitorPorts += $candidatePort
                    }
                }
                if ($monitorLogFiles.Count -gt 0 -and $missingMonitorPorts.Count -eq 0) {
                    $monitorCaptureMode = "attached"
                } elseif ($monitorLogFiles.Count -gt 0) {
                    $monitorCaptureMode = "partial"
                }
            }
            $monitorMissing = ([bool]$StartMonitor -and $missingMonitorPorts.Count -gt 0)
            if ($summaryObj.PSObject.Properties.Match("monitor_logs_attached").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_logs_attached" -NotePropertyValue @($monitorLogFiles)
            } else {
                $summaryObj.monitor_logs_attached = @($monitorLogFiles)
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_logs_attached_count").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_logs_attached_count" -NotePropertyValue $monitorLogFiles.Count
            } else {
                $summaryObj.monitor_logs_attached_count = $monitorLogFiles.Count
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_logs_missing").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_logs_missing" -NotePropertyValue $monitorMissing
            } else {
                $summaryObj.monitor_logs_missing = $monitorMissing
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_expected_ports").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_expected_ports" -NotePropertyValue @($expectedMonitorPorts)
            } else {
                $summaryObj.monitor_expected_ports = @($expectedMonitorPorts)
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_missing_ports").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_missing_ports" -NotePropertyValue @($missingMonitorPorts)
            } else {
                $summaryObj.monitor_missing_ports = @($missingMonitorPorts)
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_requested").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_requested" -NotePropertyValue ([bool]$StartMonitor)
            } else {
                $summaryObj.monitor_requested = [bool]$StartMonitor
            }
            if ($summaryObj.PSObject.Properties.Match("monitor_capture_mode").Count -eq 0) {
                $summaryObj | Add-Member -NotePropertyName "monitor_capture_mode" -NotePropertyValue $monitorCaptureMode
            } else {
                $summaryObj.monitor_capture_mode = $monitorCaptureMode
            }
            Write-JsonNoBom -Path $summaryJson -Data $summaryObj -Depth 16
        }
    }
}
catch {
    Write-Host ("WARN: failed to annotate monitor metadata: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
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
