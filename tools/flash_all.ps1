[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Ports,

    [string]$Environment = "seeed_xiao_esp32c3",

    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,

    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

if ($Ports.Count -ne 3) {
    throw "Ports は3台分を指定してください。例: -Ports COM5,COM6,COM7"
}

$pioCmd = Get-Command pio -ErrorAction SilentlyContinue
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pioCmd -and -not $pythonCmd) {
    throw "pio も python も見つかりません。PlatformIO実行環境を確認してください。"
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
            throw "ビルドに失敗しました。"
        }
    }

    $failed = @()
    for ($i = 0; $i -lt $Ports.Count; $i++) {
        $port = $Ports[$i]
        Write-Host ("== Upload [{0}/3] {1} ==" -f ($i + 1), $port) -ForegroundColor Yellow
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
        throw ("書き込み失敗ポート: " + ($failed -join ", "))
    }

    Write-Host "3台すべて書き込み成功。" -ForegroundColor Green
}
finally {
    Pop-Location
}
