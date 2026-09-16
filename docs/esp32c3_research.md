# C3/S3/C6 RF設定と認証上の注意

旧調査書の「現行ファームはLRが既定ON」という説明は、当時のBLE共存ビルド条件と矛盾していました。本書を現在の仕様に置き換えます。

C3/S3/C6は同じESP-NOW wire形式で試験可能ですが、同一binや同一GPIO番号では動きません。ファームウェアはSoC別にビルドし、チャンネル、PHYプロファイル、アンテナ、電源、設置高さ、送信内容を揃えて比較します。CPU性能とRF到達距離を混同しません。

通常ビルドは1Mbpsを明示設定し、BLEを無効化します。LRビルドは `WIFI_PROTOCOL_LR` と `WIFI_PHY_RATE_LORA_250K` を設定します。protocol maskだけでは送信レートがLRになることを保証できないため、rate APIの成功も確認します。`LORA_250K` はこのSDKにおけるWi-Fi LR PHYの列挙名で、SX1262等のSub-GHz LoRa無線ではありません。

日本向けcountry設定はJP、既定channel1、20MHz、送信出力要求72 quarter-dBmです。`esp_wifi_set_max_tx_power()` の指定は上限設定であり、チップのデータシート最大値やEIRPそのものではありません。SDKの段階化・PHY・地域制約があるため、84指定を21dBm実出力とみなしません。出力要求、ドライバー読み戻し、アンテナ利得、実測値は区別します。

技適対象はアンテナを含む構成と無線方式の条件に依存します。ボードのマークだけから、LRや任意の高利得アンテナを含む全設定が認証済みと推定しません。現物の技適番号とSeeedの指定アンテナ一覧を確認し、不明な組み合わせ・方式はメーカーへ確認します。高利得アンテナ/ブースターを無条件に推奨しません。

固定SDKの受信コールバックにRSSIがないため、本版はRSSI不明として扱います。PDRはパケット到達率、RSSIは受信電力で、互いの代用値ではありません。数値が不明な場合に0dBmを表示したり、RSSIから距離を推定したりしません。

## 一次資料

- [ESP-IDF v4.4.7 ESP-NOW API / C3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32c3/api-reference/network/esp_now.html)
- [同SDKのesp_now.h（送信rate APIとcallback定義）](https://github.com/espressif/esp-idf/blob/v4.4.7/components/esp_wifi/include/esp_now.h)
- [同SDKのWi-Fi API / C3](https://docs.espressif.com/projects/esp-idf/en/v4.4.7/esp32c3/api-reference/network/esp_wifi.html)
- [Seeed K.K. 指定アンテナ一覧（2024年公開）](https://lab.seeed.co.jp/entry/2024/06/19/120000)
- [XIAO C3](https://wiki.seeedstudio.com/XIAO_ESP32C3_Getting_Started/)
- [XIAO S3](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/)

距離や屋外環境の成功を主張するには [field_validation.md](field_validation.md) の実測結果が必要です。


## C6のSDK差

ESP32-C6はESP-IDF 5.1以降でサポートされるため、C3/S3の固定Arduino 2.x環境へ無理に載せません。C6はSeeed PlatformIO platformのcommitを固定し、Arduino 3.3.7系で別ビルドします。IDF 5.5以降で変更されたESP-NOW送信callbackも条件コンパイルで吸収し、wire formatは共通に保ちます。C6のWi-Fi 6機能はこの互換Meshでは使用せず、2.4GHz ESP-NOW共通設定を使用します。
