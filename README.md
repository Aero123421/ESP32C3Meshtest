# Mesh Lab — XIAO ESP32-C3 / ESP32-S3

ESP-NOWの1ホップ・多段中継を比較するためのファームウェアと、Windows / macOS / Linux用ローカル試験アプリです。APへの接続を前提にする通常のWi-Fiアプリではなく、**ESP-NOW上の独自Hybrid Mesh**です。リポジトリの旧名・ログ内の `LPWAtestESP32` / `lpwa` は互換性のため一部残っています。

> **ビルド成功は、長距離通信成功や技適への適合を証明しません。** この変更でC3/S3の無線設定と中継の不具合を修正し、実測用の道具を整備しています。実際の到達距離、C3⇄S3混在通信、障害時の復旧性能は [実機受入試験](docs/field_validation.md) で別途確認してください。既存レポートには距離・設置条件が記録されておらず、100m/数百mの実証として扱えません。

## 何が変わったか

通常ビルドは **BLE停止・ESP-NOW 1Mbps明示設定・20MHz・省電力OFF**。LRは別ビルドで **250kbpsを明示設定**します。単に `WIFI_PROTOCOL_LR` を追加しただけの構成ではありません。無線APIの戻り値を確認し、設定の読み戻しをシリアルと画面へ出力します。日本向けcountry設定は `JP`、チャンネルは1〜13に制限しています。

中継は次ホップunicast、予備経路、flood fallback、TTL、重複排除、分割・再構成を使用します。`esp_now_send()` の受付成功とMAC送信完了を区別し、実際のMAC失敗で再送・経路失効・迂回へ進みます。送信は同時に1件のみ。コールバック欠落時は次の送信との混同を防ぎ、長時間停止時に再起動します。過剰な反復送信、送信元へ戻る中継、期限切れpeerの蓄積、破損フレームの早期処理、負数のシフトも修正しています。

新アプリはブラウザーUI＋Pythonのlocalhostサーバーです。クラウド不要、Tk/Qt不要、外部CDN不要。接続マップ、ノード詳細、経路表、Ping試験、配達ACK付き短文/長文、ビルド・書き込み、試験条件記録、JSONセッション保存を備えます。観測していないホップを推測で描きません。

## 1. セットアップ

Python **3.10以上**を使用します。CIではPython 3.12をWindows/macOS/Linuxで検証します。初回のパッケージ導入、PlatformIOのツールチェーン導入にはインターネット接続が必要です。

macOS / Linux（リポジトリ直下）:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r pc_app/requirements.txt platformio==6.1.18
python pc_app/app.py
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r pc_app\requirements.txt platformio==6.1.18
.\.venv\Scripts\python.exe pc_app\app.py
```

ブラウザーが自動で開きます。起動時に表示するURLにはローカル操作用の一時トークンが含まれます。URLを共有せず、サーバーをLAN公開・ポート転送しないでください。終了はターミナルで `Ctrl+C`。同じPCのブラウザーだけから操作できます。

```sh
python pc_app/app.py --demo          # 明示的な読み取り専用サンプル。実測値ではない
python pc_app/app.py --no-browser    # URLだけ表示
python pc_app/app.py --help
```

macOSは `/dev/cu.usbmodem...` / `/dev/cu.usbserial...` を選択します。Windowsは `COM...`、Linuxは `/dev/ttyACM...` / `/dev/ttyUSB...`。Linuxで権限エラーになる場合はディストリビューションのシリアルポート権限を設定してください。別のシリアルモニターと同じポートを同時に開かないでください。

## 2. C3/S3のビルドは別々

**C3とS3で同じ `.bin` は使えません。** C3はRISC-V、S3はXtensaです。同じソースを、対象ボードに応じてビルドします。CPUの違いは無線互換性を妨げませんが、GPIO・USB・メモリ設定の違いは残ります。

| ビルド環境 `-e` | ボード | 無線プロファイル | BLE |
|---|---|---|---|
| `seeed_xiao_esp32c3` | XIAO C3 | 通常 / 1Mbps | OFF |
| `seeed_xiao_esp32s3` | XIAO S3 | 通常 / 1Mbps | OFF |
| `seeed_xiao_esp32c3_lr` | XIAO C3 | LR / 250kbps（実験用） | OFF |
| `seeed_xiao_esp32s3_lr` | XIAO S3 | LR / 250kbps（実験用） | OFF |
| `seeed_xiao_esp32c3_coexist` | XIAO C3 | 通常 / BLE共存 | ON |
| `seeed_xiao_esp32s3_coexist` | XIAO S3 | 通常 / BLE共存 | ON |

```sh
python -m platformio run -e seeed_xiao_esp32c3
python -m platformio run -e seeed_xiao_esp32s3
python -m platformio run -e seeed_xiao_esp32c3_lr
python -m platformio run -e seeed_xiao_esp32s3_lr
```

書き込み（ポートを現物に合わせて変更）:

```sh
# macOS: C3
python -m platformio run -e seeed_xiao_esp32c3 -t upload --upload-port /dev/cu.usbmodemXXXX
# macOS: S3
python -m platformio run -e seeed_xiao_esp32s3 -t upload --upload-port /dev/cu.usbmodemYYYY
# Windows: S3
python -m platformio run -e seeed_xiao_esp32s3 -t upload --upload-port COM7
```

S3でポートが見えない/書き込めない場合は、データ対応USBケーブルを確認し、BOOTを押しながらRESETしてダウンロードモードへ入り、列挙し直したポートを選択します。USBポート名は書き込み・リセットの前後で変わる場合があります。`--force` で別チップ用binを書かないでください。

成果物は `.pio/build/<環境>/firmware.bin`。これはアプリ領域のbinで、単独では初回書き込みに必要なbootloader/partition tableを含みません。原則PlatformIOのuploadを使ってください。CIのファームウェアビルドはUbuntuで実施し、macOS実機でのUSB書き込み・ローカルコンパイルは別途検証対象です。

## 3. C3とS3はGPIO番号が違う

XIAOの端子名・配置が似ていても、**数値GPIOの直書きは互換ではありません**。

| XIAO端子 | C3のGPIO | S3のGPIO |
|---|---:|---:|
| D0 | 2 | 1 |
| D1 | 3 | 2 |
| D2 | 4 | 3 |
| D3 | 5 | 4 |
| D4 / SDA | 6 | 5 |
| D5 / SCL | 7 | 6 |
| D6 / TX | 21 | 43 |
| D7 / RX | 20 | 44 |
| D8 / SCK | 8 | 7 |
| D9 / MISO | 9 | 8 |
| D10 / MOSI | 10 | 9 |

この表は通常のXIAO ESP32C3 / ESP32S3向けです。S3 Plus、拡張基板、別メーカーのDevKitへそのまま流用しないでください。Arduinoでは `D6` / `SDA` などのボード定義を優先し、周辺機能、ストラップピン、電源条件は個別に確認します。現在のメッシュ基本試験はUSB給電/シリアル中心で、外部GPIO接続を必要としません。

## 4. 長距離試験と日本国内の運用

LRを利用する場合は、**ボード・アンテナ・無線方式を含む認証範囲を確認**してください。通常Wi-Fiの技適表示だけから、任意のアンテナやLRの適法性を断定しません。Seeedの指定アンテナ以外、任意の高利得アンテナ、外付け増幅器を前提にしません。

全ノードを同じチャンネル・プロファイルに揃えます。C3通常＋S3 LRを無条件に混在させる手順は推奨しません。既存ファームとの無線互換性も、同一設定での実機確認が必要です。

```sh
python -m platformio device monitor --port /dev/cu.usbmodemXXXX --baud 115200
```

接続後に `{"cmd":"get_radio_profile"}` を送ると、`ready`、`chip`、`profile`、`channel`、`bandwidth_mhz`、`lr_enabled`、`power_save`、`tx_power_requested_qdbm`、`tx_power_readback_qdbm`、`readback_ok` が分かります。`espnow_rate_kbps` は送信APIが受理した設定値で、空中で測定したPHYレートではありません。

送信電力の既定要求は `72` quarter-dBm = 18dBm。これは最大値を要求するAPIの設定であり、実際の空中線電力・EIRPではありません。**84を指定すれば21dBmになる、という扱いはしません。** SDKの段階化、地域、PHY、認証条件の制約を受けます。距離保証に換算しないでください。

固定ツールチェーンはPlatformIO Espressif32 6.10.0 / Arduino 2.0.17系です。このSDKの通常ESP-NOW受信コールバックにはRSSIがないため、現在は **RSSI不明** と表示します。経路評価では不明値を中立扱いし、0dBmの良好リンクとして評価しません。受信RSSIが必須の試験は、別途取得実装・実機検証を追加してから行ってください。

## 5. 新アプリでの測定

上部でポートを選び接続します。トポロジの実線は受信時の隣接リンク、破線は接続ゲートウェイの経路表にある次ホップです。遠端までの全経路が観測できたことを意味しません。マップ位置は模式図で、GPS座標や距離ではありません。観測テレメトリの無線配信は既定OFFで、通信負荷を抑えています。

通信テストでは遠端ノードを指定し、64/256/1000bytes、回数、間隔、応答期限、TTLを設定します。測定はstop-and-wait方式。応答期限内に対応する遠端Pongが届いた試行のみ成功です。ローカルACK、別ノード、別ID、重複、期限後の応答は成功を増やしません。停止時の未確定試行は損失から除外します。RTTはPCの送信キュー投入からPong受信までの、USB等を含む往復です。

短文・長文は `delivery_ack` を待って送ります。長文はUTF-8バイト単位の分割とSHA-256確認を使用します。**配達ACKは相手ファームウェアの受信であり、相手PCの保存完了ではありません。** 旧アプリのFEC/NACK/repair専用ワークフローとBLE画面は `python pc_app/legacy_app.py` に残しています。新UIは通常の宛先指定テキストとPing試験を中心に再構成しています。

試験前に場所、距離、アンテナ、設置高さ、障害物、電源を記録し、終了後に「セッションを保存」を押します。設定の読み戻し、試験開始時の条件、確定した試行、ログ、直近ビルドのbinハッシュを含めて保存します。ビルドのハッシュはその場で作成したbinのハッシュで、接続中ボードに書かれているbinを読み出して照合した値ではありません。

## 6. 回帰と検証範囲

```sh
python -m pip install pytest==8.3.5
python -m pytest -q tests
```

CIはC3/S3×通常/LR/BLE共存の6ビルド、Pythonテストの3OS、ブラウザーJavaScript構文検査を行います。ネイティブC++ハーネスは実際の送信関数とフレーム検証コードをfake driverで検査します。無線到達距離や干渉・アンテナ性能のテストではありません。

既存の実機スモークツールも残しています:

```sh
python tools/mesh_smoke_test.py --ports COM6 COM7 COM8 --timeout 35 --ack-timeout 4 --ack-retries 6 --skip-ble
```

このプロジェクトは試験用です。ブロードキャストを含む無線認証・鍵管理は完成しておらず、第三者による偽装や観測を防ぐ製品向けセキュリティを保証しません。認証・製品受入検証を終えるまでは、保安操作・機密情報・無人の本番設備に使用しないでください。

- [現在の設計](docs/architecture.md)
- [RFと認証に関する注意](docs/esp32c3_research.md)
- [実機受入試験 / 未検証項目](docs/field_validation.md)
- [従来の実機試験記録](docs/test_report_2026-03-03.md)
