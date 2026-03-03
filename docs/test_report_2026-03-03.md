# test_report_2026-03-03

## 1. 実施環境
- 実施日: 2026-03-03
- ボード: XIAO ESP32C3 x3
- ポート: `COM6`, `COM7`, `COM8`
- ビルド: PlatformIO (`seeed_xiao_esp32c3`, Arduino framework)

## 2. ビルド・書き込み結果
- `python -m platformio run` : 成功
- `& .\tools\flash_all.ps1 -Ports @('COM6','COM7','COM8')` : 3台成功
- 取得MAC:
  - COM6: `94:a9:90:6a:ee:c4`
  - COM7: `94:a9:90:7a:b5:60`
  - COM8: `94:a9:90:7a:26:ac`

## 3. 通信テスト結果

### 3.1 Wi-Fiメッシュ（ESP-NOW）
- `python tools/two_port_mesh_test.py --tx COM6 --rx COM7 --timeout 25` : 成功
  - `wifi chat` 成功
  - `ping/pong` 成功
  - `ble chat` 成功（この組み合わせでは成功）
- `python tools/two_port_mesh_test.py --tx COM6 --rx COM8 --timeout 25` : 部分成功
  - `wifi chat` 成功
  - `ping/pong` 成功
  - `ble chat` タイムアウト
- `python tools/two_port_mesh_test.py --tx COM7 --rx COM8 --timeout 25` : 部分成功
  - `wifi chat` 成功
  - `ping/pong` 成功
  - `ble chat` タイムアウト

### 3.2 3ノード認識
- `python tools/get_nodes_wait.py COM6 --timeout 30 --min-count 3` : 成功
- COM6の `node_list` で3ノード同時認識を確認:
  - `0x005447FE` (COM6)
  - `0x00F01CEE` (COM7)
  - `0x003C8FEE` (COM8)

### 3.3 シリアル安定性
- `python tools/raw_send_watch.py COM6 --watch 8` で `chat` 送信時のクラッシュ再現なし
- 以前発生していた `loopTask stack overflow` は、`DynamicJsonDocument` 化で解消

### 3.4 E2E delivery_ack / 再送 / long_text（今回更新）
- `python tools/mesh_smoke_test.py --ports COM6 COM7 COM8 --timeout 35 --ack-timeout 4 --ack-retries 6 --skip-ble` : 成功
  - `node_list count=3` を確認
  - `wifi chat broadcast` 成功
  - `Directed Wi-Fi chat + delivery_ack` 成功
  - `Directed long text (1045 bytes, 33 chunks) + delivery_ack` 成功
  - `Directed ping` 成功
  - `ALL TESTS PASSED`
- `python tools/mesh_smoke_test.py --ports COM6 COM7 COM8 --timeout 35 --ack-timeout 12 --ack-retries 2 --skip-ble` : 失敗を確認
  - `Directed long text` の chunk/end で `delivery_ack` タイムアウトが発生しやすい
  - 再送パラメータを強化 (`ack-timeout=4`, `ack-retries=6`) すると安定化

## 4. 既知事項
- BLE広告リレーは組み合わせ依存で成功/失敗が分かれる
  - COM6↔COM7: 成功
  - COM6↔COM8 / COM7↔COM8: タイムアウトを確認
- Wi-Fiメッシュ（ESP-NOW）系は3台で安定して動作
- 宛先指定Wi-Fiの `delivery_ack` + 再送は実機で動作確認済み
- 1000文字級 long_text は再送設定を強めた条件で実機成功

## 5. 改善優先度（次）
1. BLE広告リレーの受信率改善（送信間隔、スキャン条件、電波環境ログを含む）
2. BLE経路のACK/再送設計（Wi-Fi実装と同等の運用要件を定義）
3. GUIからの組み合わせ別試験をワンクリックで実施できる統合テスター追加
