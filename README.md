# LPWAtestESP32

ESP32-C3（XIAO ESP32C3）を複数台使って、  
Wi-Fi（ESP-NOW）メッシュ + BLE短文リレー + PC GUI試験を行うプロジェクトです。

## 1. このリポジトリでできること

- ESP-NOW Hybridメッシュ（次ホップunicast + flood fallback、TTL中継、重複排除、分割再構成）
- 宛先指定Wi-Fiメッセージの `delivery_ack` + 自動再送（chat / long_text）
- 1000文字級メッセージ向けの長文チャンク送信（`long_text_start/chunk/end`）
- 1KB高信頼プロファイル（`reliable_1k_start/chunk/end + nack/repair/result`）
- BLE広告ベースの短文リレー（テキスト数バイト向け）
- Python GUIでのチャット、Ping試験、PDR/遅延統計
- Python GUIでの中継系統図（tree）/通信フロー（flow）可視化
- `mesh_trace` による観測共有（複数PCでトポロジ同期しやすい構成）
- 堅牢寄り既定プロファイル（11b/11g/11n、送信電力・再送/ジッタ最適化）
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
- `pyserial`（`python -m pip install pyserial` または `python -m pip install -r pc_app\requirements.txt`）
- XIAO ESP32C3 x 3（最低1台はPC直結、他はUSB給電でも可）

PowerShell 実行ポリシーで `.ps1` がブロックされる場合は、実行セッション内だけ一時的に次を実行してください。

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## 4. クイックスタート（ファーム）

### 4.1 ビルド

```powershell
cd D:\codebase\LPWAtestESP32
python -m platformio run
```

### 4.2 3台へ一括書き込み

```powershell
cd D:\codebase\LPWAtestESP32
.\tools\flash_all.ps1 -Ports COM6,COM7,COM8 -SessionDir .\test_logs\session_20260304
```

`-Ports` は1台以上の可変長指定に対応しています（例: 10台）。
PowerShellからは `-Ports COM6,COM7,COM8` と `-Ports @("COM6","COM7","COM8")` の両方を利用できます。

`pio` が PATH にある場合は自動で `pio` を使用し、無い場合は `python -m platformio` にフォールバックします。

### 4.3 3台同時モニタ

```powershell
cd D:\codebase\LPWAtestESP32
.\tools\monitor_all.ps1 -Ports COM6,COM7,COM8 -Baud 115200 -SessionDir .\test_logs\session_20260304
```

ログを保存する場合:

```powershell
.\tools\monitor_all.ps1 -Ports COM6,COM7,COM8 -Baud 115200 -LogDir .\test_logs\session_20260303
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
- GUIからBuild / ファーム書き込み（選択COM / 複数選択）
- 宛先入力の厳格化（`0xXXXXXXXX` のみ許可）
- 用途別タブUI（通信 / 試験 / トポロジ / ログ / FW書込）
- ノード一覧表示
- chat送信（`wifi` / `ble` 切替）
- 宛先指定Wi-Fi送信のE2E配達確認（`delivery_ack`）と自動再送
- フェーズ2経路観測（GUIの「経路要求」→ `route_list` 取得）
- フェーズ3/4向け Reliable 1KB 送信（復元率/再送率/自動profile適応）
- 長文テキスト送信（一定サイズ超過時に自動チャンク化）
- ping単発/連続試験（1KB `ping_probe`）
- Broadcast Ping時の複数ノード応答をラウンド集計（遅延Pongを誤警告しにくい）
- 試験タブ下部に通信品質グラフ（PDR/遅延/Loss）のリアルタイム表示（時間軸つき）
- 通信品質の宛先フィルタ（`all` / ノード別）
- 連続Ping中の `route_lookup_hit/miss` / `routed_fallback_flood` 可視化
- トポロジ表示モード切替（`tree` / `flow` / `both`）
- 通信フロー表（observer / via_node / path / hops / msg）表示
- `route_list` 表示タブ（primary/backup/rank）
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

### 6.3 複数ノード smoke 試験

```powershell
cd D:\codebase\LPWAtestESP32
python tools/mesh_smoke_test.py --ports COM6 COM7 COM8 COM9 --timeout 35 --ack-timeout 4 --ack-retries 6 --collect-stats
```

BLE短文試験をスキップしてWi-Fi系だけ確認する場合:

```powershell
python tools/mesh_smoke_test.py --ports COM6 COM7 COM8 --timeout 35 --ack-timeout 4 --ack-retries 6 --skip-ble
```

この smoke には、宛先指定 `chat`/`ping` に加えて `long_text`（約1KB）チャンク送受信と
`delivery_ack` 検証、および `Directed reliable_1k (FEC)` が含まれます。

### 6.4 回帰一括実行（Phase5）

```powershell
cd D:\codebase\LPWAtestESP32
.\tools\run_mesh_regression.ps1 -Ports COM6,COM7,COM8 -Scenario indoor_s5
```

`run_mesh_regression.ps1` は既定で `delivery_ack` を必須化します。  
逆方向経路が未収束の検証初期のみ緩和したい場合は `-AllowMissingDeliveryAck` を付与します。

3台ベンチ検証向けに閾値を緩和する場合:

```powershell
.\tools\run_mesh_regression.ps1 -Ports COM6,COM7,COM8 -Scenario indoor_s3 -ThresholdFile .\docs\threshold_profiles\indoor_s3_baseline.json -AllowMissingDeliveryAck
```

実行後に `session.json` / `flash_result.json` / `smoke/*` / `triage/*` が生成されます。  
`monitor_manifest.json` は `-StartMonitor` 指定時のみ生成されます。  
`mesh_smoke_test.py` が失敗した場合でも `triage/*` は生成され、原因切り分けに必要な最低限のサマリを残します。

## 7. VSCodeからの書き込み

`docs/vscode_platformio_guide.md` に手順をまとめています。

- `Build`（チェック）
- `Upload`（右矢印）
- `Monitor`（プラグ）

## 8. 既知事項

- Wi-Fiメッシュ（ESP-NOW）は3台で安定動作を確認済み
- 1KB級長文（チャンク送信）はWi-Fi経路で検証対象に追加済み
- BLE短文リレーは組み合わせ/環境依存で成功率にばらつきがある
- `mesh_smoke_test.py` は `--skip-ble` を使うとWi-Fi系の回帰確認に特化できる
- 長距離設定は build flag で調整可能（`LPWA_ENABLE_WIFI_LR`, `LPWA_ALLOW_WIFI_LR_WITH_BLE`, `LPWA_MESH_CHANNEL`, `LPWA_MESH_TX_POWER_QDBM`）
- Directed宛先 `dst` は `0xXXXXXXXX` 形式を使用（不正形式はFWが `invalid_field` を返す）

最新の試験結果は `docs/test_report_2026-03-03.md` を参照してください。

## 9. 主要ドキュメント

- `AGENTS.md`
- `docs/architecture.md`
- `docs/esp32c3_research.md`
- `docs/vscode_platformio_guide.md`
- `docs/test_plan.md`
- `docs/test_report_2026-03-03.md`
