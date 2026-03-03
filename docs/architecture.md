# architecture.md

## 1. 目的と前提
- 目的: XIAO ESP32C3を3台使い、P2P通信からメッシュ通信へ段階的に拡張する
- 重点:
  - 障害物環境で50〜100m級の安定通信を目指す
  - 見通し環境では長距離化の可能性を評価する
- 前提:
  - 2.4GHz帯のみ使用
  - ノードは電池駆動またはUSB給電
  - 将来的なノード増加を想定
  - 現行実装は `ESP-NOW Hybrid Mesh`（`next-hop unicast + flood fallback + TTL + 重複排除 + 分割再構成`）を採用
  - BLEは広告ベースの軽量テキスト中継（短文のみ）を採用

## 2. 設計方針
- 方針1: まず単純構成で成功率を上げる
  - `Wi-Fi UDP` で基本疎通を確認
  - 次に `ESP-NOW` のP2Pへ移行
- 方針2: メッシュ化は制御要素を明確化
  - `TTL`（中継段数制御）
  - `Packet ID`（重複排除）
  - `ACK/再送`（信頼性）
- 方針3: 距離拡張はリンク設計と運用設計の両方で行う
  - 外部アンテナ、設置位置、送信間隔、チャンネル固定をセットで最適化

## 3. 通信レイヤーの使い分け

| レイヤー | 主用途 | 利点 | 制約 |
|---|---|---|---|
| Wi-Fi STA/AP + UDP | 初期検証、ロギング | 実装容易、デバッグ容易 | AP依存またはSoftAP負荷 |
| ESP-NOW | 低オーバーヘッドP2P | 接続処理なし、高速応答 | チャンネル管理が必要 |
| ESP-WIFI-MESH | IPベース拡張 | 既存ネットワークとの連携が容易 | 実装と運用がやや重い |
| ESP-BLE-MESH | 低電力・制御通知 | BLE資産を活用可能 | スループットが低い |

## 4. 推奨トポロジー（3台起点）
- NodeA: ゲートウェイ候補（PC接続優先）
- NodeB: リレー候補（中継専任）
- NodeC: エッジ候補（遠端設置）

通信例:
- 近距離: `A <-> B`, `B <-> C`
- 遠距離: `A -> B -> C` の2ホップ中継

## 5. メッセージフレーム（最小）

| フィールド | 型 | 説明 |
|---|---|---|
| `version` | uint8 | プロトコル版数 |
| `msg_type` | uint8 | DATA/ACK/BEACON/CONTROL |
| `src_id` | uint16 | 送信元ノードID |
| `dst_id` | uint16 | 宛先ノードID（ブロードキャスト予約値あり） |
| `seq` | uint16 | 送信シーケンス |
| `ttl` | uint8 | 中継残り回数 |
| `hop` | uint8 | 通過ホップ数 |
| `payload_len` | uint8 | ペイロード長 |
| `payload` | bytes | 本文 |
| `crc` | uint16 | フレーム検証用 |

## 6. 中継制御の要点
- 重複排除:
  - キー: `src_id + seq`
  - 期限付きキャッシュを保持
- 中継条件:
  - `ttl > 0` のときのみ転送
  - 受信時に `ttl--`、`hop++`
- ACK/再送:
  - 宛先指定Wi-Fi (`dst` 指定) の `chat` / `long_text_*` / `image_*` は `need_ack=true` + `e2e_id` を付与
  - 受信ノードは `delivery_ack` を送信元へ返し、PC GUIは `e2e_id` で照合して完了判定する
  - GUI再送ポリシー:
    - timeout: 2200ms
    - max retry: 4（合計5送信）
  - Broadcast送信とBLE送信は `delivery_ack` 対象外

## 6.1 長距離向け既定プロファイル（現行実装）
- ノードごとの個別設定なしで同一ファームを配布して使用する前提
- ESP-NOW初期化時に以下を自動適用:
  - `esp_wifi_set_max_tx_power(84)`（21dBm相当、法規/実装制限に依存）
  - `esp_wifi_set_ps(WIFI_PS_NONE)`（Wi-Fi省電力OFFで応答遅延を抑制）
  - `esp_wifi_set_protocol(...11b/11g/11n + LR)`（長距離寄りの既定）
  - 送信フレームのオリジン側リピート送信（既定3回）
  - オリジン再送間隔と中継転送間隔のランダムジッタ（衝突確率低減）
  - NodeInfo周期の延長（10s→15s）で常時オーバーヘッド抑制
  - NodeInfo送信タイミングの個体ジッタ化（同時送信バースト緩和）
  - フレーム種別ごとの中継再送回数（Fragment重視 / NodeInfo軽量）
  - `esp_now_send` の `NO_MEM` 時に短いバックオフ再試行
  - `trace_obs` は `TTL=3` + 最短送信間隔（120ms）でテレメトリ過負荷を抑制
- 補足:
  - 必要に応じて `platformio.ini` の build flag で切替可能
    - `LPWA_ENABLE_WIFI_LR`（0/1）
    - `LPWA_MESH_CHANNEL`（1..14）
    - `LPWA_MESH_TX_POWER_QDBM`（8..84）
    - `LPWA_ROUTING_MODE`（0=floodのみ / 1=origin directed / 2=origin+relay directed）

## 6.2 Phase2: 経路学習と次ホップ転送
- Directed送信時は `RoutedFragment` を使用し、宛先ノードIDをフレームメタに付与する。
- 各ノードは受信フレームから `origin -> next_hop` を学習し、経路表を保持する。
- ルーティング指標は `hop + ETX + RSSI` の重み付き合成とし、ヒステリシスで経路フラップを抑制する。
- 中継時の動作:
  - ルート有り: 次ホップへ unicast 転送
  - ルート無し/失敗: flood fallback
- 安全策:
  - 期限切れ経路の自動削除
  - フラグメント整合チェック（`frag_count/index/chunk_len/total_len`）
  - hopカウントとメトリック計算の飽和処理（オーバーフロー回避）

補足:
- JSONプロトコルの `dst` は `0xXXXXXXXX` 形式のみを有効とする。
- 不正な `dst` は bridge が `invalid_field` を返し、暗黙Broadcastへはフォールバックしない。

## 6.3 Phase3: 1KB高信頼転送（`reliable_1k`）
- `reliable_1k_start/chunk/end` をWi-Fi directedで送信し、`delivery_ack` でE2E配達を確認する。
- 受信側は `data_shards + parity_shards` のFECシャードから復元し、欠損時は `reliable_1k_nack` で不足indexを返す。
- 送信側は `reliable_1k_repair` で不足シャードのみ再送し、復元完了時に `reliable_1k_result` を返してセッションを閉じる。
- 互換運用:
  - wire上は短縮型 (`r1k_s/r1k_d/r1k_e/r1k_n/r1k_r/r1k_o`) を利用
  - PC/JSONイベントは正規型 (`reliable_1k_*`) として統一表示

## 6.4 Phase4: 自動適応と観測
- PC GUIは宛先ごとに `25+8` / `25+10` のprofileを自動調整する。
  - 失敗/NACK増加/高再送率: 冗長を強化
  - 連続安定時: 冗長を段階的に緩和
- 統計は `ReliableStats` で一元集計する。
  - 復元率
  - 再送率
  - 失敗理由トップ
  - 使用profile
- `mesh_trace` / `mesh_observed` で複数PCから同じ通信観測を共有し、トポロジと通信フローを同期表示する。

## 7. Wi-Fi/BLEメッシュの使い分け設計
- Wi-Fiメッシュを優先する場面:
  - センサーデータなど比較的大きいデータ転送
  - IPネットワークと連携したい場合
- BLEメッシュを優先する場面:
  - 低頻度の制御コマンド
  - 省電力運用を優先する場合
- ハイブリッド案:
  - 制御チャネル: BLE
  - データチャネル: Wi-Fi/ESP-NOW

## 8. 障害時設計（運用）
- 経路断:
  - 近傍ノード探索を再実行し経路更新
- ノード再起動:
  - 起動後にBEACON送信し再参加
- 干渉増加:
  - チャンネル切替候補を事前定義し、試験中は固定、運用時に再選定

## 9. 今後の決定事項
- 最終プロトコル:
  - ESP-NOW自作メッシュを本命にするか
  - 公式メッシュを併用するか
- セキュリティ:
  - 鍵更新周期
  - ノード追加時のプロビジョニング手順
- 品質指標:
  - PDR（Packet Delivery Rate）目標
  - 遅延上限
  - 電池駆動時間目標
