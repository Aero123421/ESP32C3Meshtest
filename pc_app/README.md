# LPWA Test PC App (tkinter)

`pc_app/` は Windows 向けの `tkinter` GUI です。  
ファームウェアとは **JSON Lines (1行1JSON)** でシリアル通信します。

## 機能

- COMポート一覧表示 / 接続 / 切断
- 画面を用途別タブに分割（通信 / 試験 / トポロジ / ログ / FW書込）
- GUIからのBuild / ファーム書き込み（選択COM / 全COM）
- ノード一覧表示（ID / RSSI / 最終受信時刻 / 最終メッセージ / 最新Ping）
- ノード一覧の選択ノードをチャット/Pingの宛先へ一括反映
- 宛先コンボボックス（既知ノード候補 + Broadcast）
- TTL設定（10ノード向けにGUIから調整可能）
- チャット送信
- チャット送信（`wifi` / `ble` 切替）
- 宛先指定Wi-Fi送信のE2E配達確認（`delivery_ack`）と自動再送
- 長文テキスト送信（大きいメッセージを `long_text_start/chunk/end` に自動分割）
- 長文テキスト受信再構成（欠損/サイズ不一致/ハッシュ不一致は破棄）
- Ping単発送信 / 連続テスト
- Ping単発送信 / 連続テストは `ping_probe`（既定1KB）を使用
- Broadcast Pingの複数応答を1ラウンドとして扱い、遅延Pongを誤警告しにくい集計
- PDR / 遅延統計（sent, received, lost, pdr, avg, min, max, p95）
- 連続Ping中の `route_lookup_hit/miss` / `routed_fallback_flood` 可視化
- 試験タブ下部に通信品質グラフ（PDR / Avg / P95 / Loss）をリアルタイム表示
- イベントログ表示と保存（レベル別色分け、横スクロール対応）
- トポロジ専用タブで大型キャンバス表示（リアルタイム更新）
- トポロジ表示モード（`tree` / `flow` / `both`）を切替可能
- `mesh_observed` / `mesh_trace` を使い、複数PCでも中継系統図を更新
- 下部テーブルを「リンク集計」と「通信フロー」に分割して可視化
- 自端末ノードを `SELF` / `★` 表示で強調
- トポロジ凡例を表示（矢印=通信方向、線太さ=回数、色=種別）

## ディレクトリ構成

## 追加メモ

- `Route` ボタンと自動 `routes_request` で `route_list` を定期取得
- `pong` / `delivery_ack` に `route_hops` / `next` を補足表示
- トポロジの `Hops` 列で `~2` は `route_list` 推定、`Path` 列で実観測経路を表示

### Hop Telemetry

- Newer firmware emits `request_hops` on `pong` / `delivery_ack` payloads.
- PC receive events keep `hops` and also expose `reply_hops` for the return path.
- Topology Flow `Msg` shows `req=... rep=...` when these fields are present.

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
- `pyserial`（`pip install -r requirements.txt` で導入）

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
- 長文チャンク生成と再構築
- E2E ACK関連フィールド（`need_ack` / `e2e_id` / `retry_no`）
- Ping統計の基本計算

## JSON Lines プロトコル例

### PC -> Firmware

```json
{"type":"nodes_request","src":"pc","ts_ms":1710000000000}
{"type":"routes_request","src":"pc","ts_ms":1710000000000}
{"cmd":"ping_probe","type":"ping","src":"pc","via":"wifi","dst":"0x00A1B2C3","seq":12,"ping_id":"a1b2c3d4","probe_bytes":1000,"ts_ms":1710000000100}
{"type":"chat","src":"pc","via":"wifi","dst":"0x00A1B2C3","text":"hello","need_ack":true,"e2e_id":"chat-abc","ts_ms":1710000000001}
{"type":"ping","src":"pc","dst":"0x00A1B2C3","seq":12,"ping_id":"a1b2c3d4","ts_ms":1710000000100}
{"type":"long_text_start","src":"pc","dst":"0x00A1B2C3","text_id":"...","encoding":"utf-8","size":1024,"chunks":6,"sha256":"...","need_ack":true,"e2e_id":"...:s","ts_ms":1710000000400}
{"type":"long_text_chunk","src":"pc","dst":"0x00A1B2C3","text_id":"...","index":0,"data_b64":"...","need_ack":true,"e2e_id":"...:c:0","ts_ms":1710000000401}
{"type":"long_text_end","src":"pc","dst":"0x00A1B2C3","text_id":"...","need_ack":true,"e2e_id":"...:e","ts_ms":1710000000402}
```

### Firmware -> PC（期待例）

```json
{"event":"nodes","type":"node_list","nodes":[{"node_id":"0x00A1B2C3","rssi":-67}]}
{"event":"routes","type":"route_list","count":2,"total":2,"routes":[{"dst_node_id":"0x00A1B2C3","next_hop_node_id":"0x00F01CEE","hops":2,"metric_q8":912}]}
{"event":"mesh_rx","type":"chat","via":"wifi","src":"0x00A1B2C3","text":"hi from node","hops":1}
{"event":"pong","type":"pong","src":"0x00A1B2C3","seq":12,"latency_ms":34.7}
{"event":"ack","type":"ack","cmd":"chat","ok":true,"via":"wifi","msg_id":1234}
{"event":"delivery_ack","type":"delivery_ack","src":"0x00A1B2C3","ack_for":"chat","e2e_id":"chat-abc","msg_id":1234,"status":"ok","hops":1}
{"event":"error","type":"error","code":"payload_too_large","detail":"..."}
```

注意:
- Directed宛先 `dst` は `0xXXXXXXXX` 形式のみ受け付けます（不正形式は `invalid_field`）。
- BLE経路は広告メッシュのため短文用途です（実装上の文字数上限あり）。
- 長文テキストは既定で小さめチャンク（32 bytes）に分割し、宛先指定時は `delivery_ack` 再送を使って到達率を優先します。
- GUIから書き込みする場合、対象ポートを開いているシリアル接続は切断してから実行してください（アプリ側でも確認ダイアログを表示します）。
