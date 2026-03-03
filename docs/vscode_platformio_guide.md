# vscode_platformio_guide.md

## 1. 目的
- VSCode + PlatformIOでXIAO ESP32C3へ安定して書き込み・監視するための手順をまとめる。

## 2. 前提環境
- OS: Windows 10/11
- VSCode 最新版
- 拡張機能:
  - `PlatformIO IDE`
- インストール済み推奨ツール:
  - Python 3.x
  - Git

## 3. 初期セットアップ
1. VSCodeを起動し、拡張機能で `PlatformIO IDE` をインストールする
2. 再起動後、PlatformIO Homeが表示されることを確認する
3. 対象プロジェクトを開く

## 4. `platformio.ini` の基本設定例

```ini
[env:seeed_xiao_esp32c3]
platform = espressif32
board = seeed_xiao_esp32c3
framework = arduino
monitor_speed = 115200
```

補足:
- ボードIDは `seeed_xiao_esp32c3` を使用する
- フレームワークをESP-IDFにする場合は別envを作成して分離管理する

## 5. 1台の書き込み手順（GUI）
1. ボードをUSB接続する
2. PlatformIOの `Build`（チェックマーク）を実行
3. `Upload`（右矢印）を実行
4. `Monitor`（プラグ）でログ確認

## 6. ポート確認（CLI）

```powershell
pio device list
# pio がPATHに無い場合
python -m platformio device list
```

確認ポイント:
- `COMx` が各ノードに固有に割り当てられている
- 接続し直すたびにCOM番号が変わる場合は試験前に固定メモを更新する

## 7. 複数台同時運用（推奨）
- 書き込み:

```powershell
.\tools\flash_all.ps1 -Ports COM5,COM6,COM7
# もしくは
.\tools\flash_all.ps1 -Ports @("COM5","COM6","COM7")
```

10台例:

```powershell
.\tools\flash_all.ps1 -Ports COM3,COM4,COM5,COM6,COM7,COM8,COM9,COM10,COM11,COM12
```

- モニタ:

```powershell
.\tools\monitor_all.ps1 -Ports COM5,COM6,COM7 -Baud 115200
# もしくは
.\tools\monitor_all.ps1 -Ports @("COM5","COM6","COM7") -Baud 115200
```

ログ保存先を固定したい場合:

```powershell
.\tools\monitor_all.ps1 -Ports COM5,COM6,COM7 -Baud 115200 -LogDir .\test_logs\session_20260303
```

- 試験セッション準備:

```powershell
.\tools\prepare_test_session.ps1 -Ports COM5,COM6,COM7
# もしくは
.\tools\prepare_test_session.ps1 -Ports @("COM5","COM6","COM7")
```

## 8. 典型的なトラブルと対処
- `No device found`:
  - USBケーブルが充電専用の可能性。データ対応ケーブルへ交換
- `Failed to connect`:
  - ポート誤指定、他アプリがポートを占有、電源不足を確認
- 書き込み途中で失敗:
  - ポート固定し直し、USBハブ経由を避ける、PC直結で再試行
- モニタ文字化け:
  - `monitor_speed` と実際の `Serial.begin(...)` を一致させる

## 9. 運用ルール（推奨）
- ノードとCOMポートの対応を毎回 `test_logs` に残す
- 複数台同時テスト時は同一コミットのファームを使用する
- 失敗ログは「時刻・ポート・試験ID」を含めて保存する
