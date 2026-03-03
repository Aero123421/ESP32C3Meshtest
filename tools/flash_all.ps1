[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
    [string[]]$Ports,

    [string]$Environment = "seeed_xiao_esp32c3",

    [string]$ProjectDir = "",

    [switch]$SkipBuild
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

function Invoke-Pio {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )
    if ($pioCmd) {
        & pio @Args
    }
    else {
        & python -m platformio @Args
    }
}

Push-Location $ProjectDir
try {
    if (-not $SkipBuild) {
        Write-Host "== Build start ($Environment) ==" -ForegroundColor Cyan
        Invoke-Pio -Args @("run", "-e", $Environment)
        if ($LASTEXITCODE -ne 0) {
            throw "Build failed."
        }
    }

    $failed = @()
    $total = $Ports.Count
    for ($i = 0; $i -lt $Ports.Count; $i++) {
        $port = $Ports[$i]
        Write-Host ("== Upload [{0}/{1}] {2} ==" -f ($i + 1), $total, $port) -ForegroundColor Yellow
        Invoke-Pio -Args @("run", "-e", $Environment, "-t", "upload", "--upload-port", $port)
        if ($LASTEXITCODE -ne 0) {
            $failed += $port
            Write-Host ("NG: {0}" -f $port) -ForegroundColor Red
        }
        else {
            Write-Host ("OK: {0}" -f $port) -ForegroundColor Green
        }
    }

    if ($failed.Count -gt 0) {
        throw ("Upload failed ports: " + ($failed -join ", "))
    }

    Write-Host ("Upload succeeded on all {0} ports." -f $Ports.Count) -ForegroundColor Green
}
finally {
    Pop-Location
}
