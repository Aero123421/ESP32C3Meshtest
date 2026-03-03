# LPWA Test PC App (tkinter)

`pc_app/` は Windows 向けの `tkinter` GUI です。  
ファームウェアとは **JSON Lines (1行1JSON)** でシリアル通信します。

## 機能

- COMポート一覧表示 / 接続 / 切断
- ノード一覧表示（ID / RSSI / 最終受信時刻 / 最終メッセージ / 最新Ping）
- チャット送信
- チャット送信（`wifi` / `ble` 切替）
- 画像ファイル送信（`image_start` / `image_chunk` / `image_end`）
- 受信画像の再構成保存（`received_images/`）
- Ping単発送信 / 連続テスト
- PDR / 遅延統計（sent, received, lost, pdr, avg, min, max, p95）
- イベントログ表示と保存

## ディレクトリ構成

```text
pc_app/
  app.py                      # tkinter GUI
  self_check.py               # 最低限の自己診断スクリプト
  requirements.txt
  lpwa_gui/
    __init__.py
    protocol.py               # JSON Linesエンコード/デコード、送信用メッセージ生成
    serial_worker.py          # シリアルI/Oスレッド、thread-safe queue
    models.py                 # ノード情報管理
    stats.py                  # Ping統計計算
```

## 前提

- Windows 10/11
- Python 3.10 以上
- `tkinter`（標準同梱）

## セットアップ

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 実行

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
python app.py
```

## バッチで実行（venv作成から）

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
.\setup_and_run_gui.bat
```

セットアップのみ行ってGUIを起動しない場合:

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
.\setup_and_run_gui.bat --setup-only
```

## 自己診断

```powershell
cd D:\codebase\LPWAtestESP32\pc_app
python self_check.py
```

`self_check.py` は以下を検証します。

- JSON Lines encode/decode roundtrip
- 画像チャンク生成と再構築
- Ping統計の基本計算

## JSON Lines プロトコル例

### PC -> Firmware

```json
{"type":"nodes_request","src":"pc","ts_ms":1710000000000}
{"type":"chat","src":"pc","via":"wifi","dst":"0x00A1B2C3","text":"hello","ts_ms":1710000000001}
{"type":"ping","src":"pc","dst":"node-2","seq":12,"ping_id":"a1b2c3d4","ts_ms":1710000000100}
{"type":"image_start","src":"pc","dst":"node-2","image_id":"...","name":"photo.jpg","size":12345,"chunks":18,"sha256":"...","ts_ms":1710000000200}
{"type":"image_chunk","src":"pc","dst":"node-2","image_id":"...","index":0,"data_b64":"...","ts_ms":1710000000201}
{"type":"image_end","src":"pc","dst":"node-2","image_id":"...","ts_ms":1710000000300}
```

### Firmware -> PC（期待例）

```json
{"event":"nodes","type":"node_list","nodes":[{"node_id":"0x00A1B2C3","rssi":-67}]}
{"event":"mesh_rx","type":"chat","via":"wifi","src":"0x00A1B2C3","text":"hi from node","hops":1}
{"event":"pong","type":"pong","src":"0x00A1B2C3","seq":12,"latency_ms":34.7}
{"event":"ack","type":"ack","cmd":"chat","ok":true,"via":"wifi","msg_id":1234}
{"event":"error","type":"error","code":"payload_too_large","detail":"..."}
```

注意:
- BLE経路は広告メッシュのため短文用途です（実装上の文字数上限あり）。
- 画像送信は `wifi` 前提です。
