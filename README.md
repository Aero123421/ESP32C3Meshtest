# LPWAtestESP32

ESP32-C3（XIAO ESP32C3）を複数台使って、  
Wi-Fi（ESP-NOW）メッシュ + BLE短文リレー + PC GUI試験を行うプロジェクトです。

## 1. このリポジトリでできること

- ESP-NOWベースのメッシュ通信（TTL中継、重複排除、分割再構成）
- BLE広告ベースの短文リレー（テキスト数バイト向け）
- Python GUIでのチャット、Ping試験、PDR/遅延統計、画像送受信（Wi-Fi経路）
- PlatformIO（VSCode）からの書き込み・シリアル監視
- 3台構成の基本試験スクリプト実行

## 2. ディレクトリ構成

- `src/`, `include/`: ESP32-C3ファームウェア
- `pc_app/`: PC GUIアプリ（tkinter）
- `tools/`: 書き込み・監視・試験補助スクリプト
- `docs/`: 設計、調査、手順、試験記録
- `AGENTS.md`: 開発/運用ルール

## 3. 前提環境

- Windows 10/11
- Python 3.10+
- VSCode + PlatformIO IDE
- XIAO ESP32C3 x 3（最低1台はPC直結、他はUSB給電でも可）

## 4. クイックスタート（ファーム）

### 4.1 ビルド

```powershell
cd D:\codebase\LPWAtestESP32
python -m platformio run
```

### 4.2 3台へ一括書き込み

```powershell
cd D:\codebase\LPWAtestESP32
.\tools\flash_all.ps1 -Ports COM6,COM7,COM8
```

`pio` が PATH にある場合は自動で `pio` を使用し、無い場合は `python -m platformio` にフォールバックします。

### 4.3 3台同時モニタ

```powershell
cd D:\codebase\LPWAtestESP32
.\tools\monitor_all.ps1 -Ports COM6,COM7,COM8 -Baud 115200
```

## 5. PC GUIアプリ

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

バッチで `venv` 作成から実行する場合:

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
.\setup_and_run_gui.bat
```

セットアップのみ:

```powershell
.\setup_and_run_gui.bat --setup-only
```

GUI機能:

- COM接続/切断
- ノード一覧表示
- chat送信（`wifi` / `ble` 切替）
- ping単発/連続試験
- 画像送信（`image_start/chunk/end`）
- ログ保存

詳細は `pc_app/README.md` を参照してください。

## 6. テスト実行例

### 6.1 2台間メッシュ試験（chat + ping、任意でBLE）

```powershell
cd D:\codebase\LPWAtestESP32
python tools/two_port_mesh_test.py --tx COM6 --rx COM7 --timeout 25
```

BLEを省略する場合:

```powershell
python tools/two_port_mesh_test.py --tx COM6 --rx COM7 --timeout 25 --skip-ble
```

### 6.2 3ノード認識確認

```powershell
cd D:\codebase\LPWAtestESP32
python tools/get_nodes_wait.py COM6 --timeout 30 --min-count 3
```

## 7. VSCodeからの書き込み

`docs/vscode_platformio_guide.md` に手順をまとめています。

- `Build`（チェック）
- `Upload`（右矢印）
- `Monitor`（プラグ）

## 8. 既知事項

- Wi-Fiメッシュ（ESP-NOW）は3台で安定動作を確認済み
- BLE短文リレーは組み合わせ/環境依存で成功率にばらつきがある
- 画像送信はWi-Fi経路前提（BLEは短文用途）

最新の試験結果は `docs/test_report_2026-03-03.md` を参照してください。

## 9. 主要ドキュメント

- `AGENTS.md`
- `docs/architecture.md`
- `docs/esp32c3_research.md`
- `docs/vscode_platformio_guide.md`
- `docs/test_plan.md`
- `docs/test_report_2026-03-03.md`
