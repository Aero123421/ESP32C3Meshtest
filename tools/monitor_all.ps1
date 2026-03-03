[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Ports,

    [int]$Baud = 115200,

    [string]$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
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

for ($i = 0; $i -lt $Ports.Count; $i++) {
    $port = $Ports[$i]
    $node = "Node$($i + 1)"
    $title = "PIO Monitor - $node ($port)"

    $safeProjectDir = $ProjectDir.Replace("'", "''")
    if ($pioCmd) {
        $monitorCmd = "pio device monitor --port $port --baud $Baud"
    }
    else {
        $monitorCmd = "python -m platformio device monitor --port $port --baud $Baud"
    }
    $command = @"
Set-Location '$safeProjectDir'
`$Host.UI.RawUI.WindowTitle = '$title'
$monitorCmd
"@

    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-Command", $command
    ) | Out-Null

    Write-Host ("モニタ起動: {0} ({1})" -f $node, $port) -ForegroundColor Green
}

Write-Host "3台分のモニタを起動しました。" -ForegroundColor Cyan
