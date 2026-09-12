# Mesh Lab アプリ

新しい既定アプリは `python pc_app/app.py` で起動します。Python 3.10以上とpyserialを使用し、macOS/Windows/Linuxで同じローカルブラウザーUIを表示します。Tk/Qtや外部CDNは不要です。詳しいセットアップとC3/S3/C6別ビルドは [ルートREADME](../README.md) を参照してください。

- `python pc_app/app.py --demo`: 明示的な合成データの読み取り専用プレビュー
- `python pc_app/app.py --no-browser`: localhostの一時トークン付きURLを表示
- `python pc_app/legacy_app.py`: 従来のTkアプリ（FEC/BLE専用ワークフローを含む互換用）

新アプリの機能は、USB接続、観測ベースの接続マップ/経路表/詳細、遠端Ping、短文/長文の配達ACK、C3/S3/C6ビルド/書き込み、試験条件記録とJSON保存です。UIに描かれていない中間リンクは未観測です。RSSIは固定SDKで取得できないため不明として表示します。USB受理ACKと遠端配達ACKは異なります。

サーバーを外部公開しないでください。URLのトークンは同じPCからのシリアル操作とビルドの認証情報です。`Ctrl+C`で終了するとシリアルポートも閉じます。実機USBの互換性・長距離性能の確認は [実機試験手順](../docs/field_validation.md) に従います。
