[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Ports,

    [string]$LogRoot = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")).Path "test_logs"),

    [string]$Operator = $env:USERNAME
)

$ErrorActionPreference = "Stop"

if ($Ports.Count -ne 3) {
    throw "Ports は3台分を指定してください。例: -Ports COM5,COM6,COM7"
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$sessionDir = Join-Path $LogRoot $timestamp

New-Item -ItemType Directory -Path $sessionDir -Force | Out-Null

$sessionFile = Join-Path $sessionDir "session.md"
$template = @"
# テストセッション記録

- 実施日時: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
- 実施者: $Operator
- NodeA Port: $($Ports[0])
- NodeB Port: $($Ports[1])
- NodeC Port: $($Ports[2])

## 実施内容
- 試験ID:
- 実施場所:
- チャンネル:
- アンテナ条件:
- 電源条件:

## 結果
- 成功条件達成:
- 失敗内容:
- 再現手順:
- 次回アクション:
"@

Set-Content -Path $sessionFile -Value $template -Encoding UTF8

Write-Host ("セッションフォルダを作成しました: {0}" -f $sessionDir) -ForegroundColor Green
Write-Host ("記録テンプレート: {0}" -f $sessionFile) -ForegroundColor Green
Write-Host ""
Write-Host "次の推奨コマンド:" -ForegroundColor Cyan
Write-Host ("  .\tools\flash_all.ps1 -Ports {0}" -f ($Ports -join ",")) -ForegroundColor Yellow
Write-Host ("  .\tools\monitor_all.ps1 -Ports {0}" -f ($Ports -join ",")) -ForegroundColor Yellow
