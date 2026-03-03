# esp32c3_research.md

## 1. 調査目的
- XIAO ESP32C3で「長距離化」と「メッシュ運用」を現実的に進めるため、仕様・制約・設計注意点を整理する。

## 2. 主要仕様サマリ

### 2.1 ボード（Seeed XIAO ESP32C3）
- MCU: ESP32-C3（RISC-V, 最大160MHz）
- メモリ: SRAM 400KB
- Flash: 4MB
- 無線: 2.4GHz Wi-Fi（802.11 b/g/n）+ BLE 5
- 形状: 小型基板、外部アンテナ（U.FL/IPEX）接続可
- 電源: USB給電、LiPoバッテリ入力（充電回路搭載）

### 2.2 チップ（ESP32-C3）
- Wi-Fi: STA/SoftAP/STA+SoftAP
- BLE: LE（拡張アドバタイズ対応）
- 送信出力目安:
  - 802.11bで最大+21dBm級
  - 802.11nで最大+20dBm級
- BLE受信感度目安:
  - 低レート時に高感度（代表値として-105dBm級の記載あり）

注記:
- 数値はデータシート条件やレート設定で変動する。最終判断は対象SDK版の公式ドキュメントと実測で行う。

## 3. 長距離設計の考え方

### 3.1 リンクバジェットの基本
- 受信余裕（概念）:
  - `受信余裕[dB] = 送信電力 + 送受アンテナ利得 - 伝搬損失 - 受信必要感度`
- 2.4GHzは障害物や人体の影響が大きく、見通し環境と屋内で結果が大きく変わる。

### 3.2 実装面で効く施策
- 外部アンテナを正しく接続し、向きと高さを揃える
- チャンネル固定で干渉条件を安定化する
- 送信間隔を調整し、衝突確率を下げる
- ACK/再送/重複排除を実装し、実効到達率を改善する
- 1ホップで無理をしない。中継ノードで多段化する

### 3.3 LR（Long Range）モードの注意
- ESP系同士の専用運用に近い特性がある
- 通常Wi-Fi機器との互換性を失う可能性がある
- PHYレート低下と引き換えに到達距離を狙うモード
- 実運用前に「互換性」「スループット」「遅延」を別々に評価する

現行ファームでは、ノード個別設定なしで `高出力 + オリジン側重複送信 + 転送ジッタ` を既定適用している。
BLE共存の安定性を優先し、`WIFI_PROTOCOL_LR` は既定では有効化していない（Wi-Fi専用ビルドで評価対象）。

## 4. メッシュ運用での注意点
- トラフィック制御:
  - フラッディング過多を避けるため、TTLと重複排除は必須
- アドレス設計:
  - ノードIDを固定し、ログと対応付けできる運用にする
- ルーティング:
  - 3台段階では静的で十分
  - 台数増加時はリンク品質ベースに拡張

## 5. ハードウェア上の注意点
- ストラップピン（例: GPIO2/GPIO8/GPIO9）により起動挙動が変わるため、起動時状態に注意する
- 電源品質が不安定だと、書き込み失敗やランダム再起動が発生しやすい
- 高出力送信やSoftAP連続運用では発熱管理を行う

## 6. 法規・運用上の注意点
- 使用地域の認証範囲（技適/FCCなど）を確認する
- アンテナを変更する場合は、認証条件やEIRP上限への影響を確認する
- 公共空間試験では周辺Wi-Fiへの干渉に配慮する

## 7. 開発時の落とし穴
- USBケーブルが充電専用で書き込み不可
- ポート入れ替わりで誤書き込み
- 3台同時運用時にログが混ざり、原因切り分け不能
- ノードごとのファーム差分管理漏れ

## 8. 推奨する最小検証順序
1. 1台で書き込み・シリアルログ確認
2. 3台で同一ビルド書き込み
3. 1ホップP2Pの疎通確認
4. 2ホップ中継（A→B→C）確認
5. 距離・障害物・電源条件を変えて再試験

## 9. 未確定事項
- 最終運用でWi-Fiメッシュを使うか、ESP-NOW自作メッシュに寄せるか
- 電池駆動時の目標稼働時間と送信Duty
- 鍵配布とノード追加の運用ポリシー

## 10. 参考リンク（一次情報）
- Espressif ESP-IDF v5.5.3（最新安定系）
  - https://github.com/espressif/esp-idf/releases
- ESP32-C3 Wi-Fi API Guide
  - https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-guides/wifi.html
- ESP-NOW API Reference（ESP32-C3）
  - https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-reference/network/esp_now.html
- RF Coexistence Guide
  - https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-guides/coexist.html
- ESP-WIFI-MESH
  - https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-reference/network/esp-wifi-mesh.html
- ESP-BLE-MESH
  - https://docs.espressif.com/projects/esp-idf/en/stable/esp32c3/api-guides/esp-ble-mesh/ble-mesh-index.html
- Seeed XIAO ESP32C3 Wiki
  - https://wiki.seeedstudio.com/XIAO_ESP32C3_Getting_Started/
- PlatformIO board: `seeed_xiao_esp32c3`
  - https://docs.platformio.org/en/stable/boards/espressif32/seeed_xiao_esp32c3.html
