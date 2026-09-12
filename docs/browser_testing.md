# ブラウザー回帰試験

対象フローは、実際のMesh Labを開く → USBゲートウェイへ接続 → 1000bytesのPing試験 → 配達ACK付きメッセージ送信 → JSON保存 → 切断、です。

`tools/browser_smoke.py` は本番のHTML/CSS/JavaScript・HTTPサーバー・Controllerを使用し、シリアル境界だけを模擬します。実際のUSBポートを開かず、ファームウェアを書き込みません。Browserプラグインがこの作業環境にないため、再現可能なPlaywright検査を使用しています。

## 実行

```sh
python -m pip install -r pc_app/requirements.txt playwright==1.57.0
python -m playwright install chromium webkit
python tools/browser_smoke.py --browser chromium --output /tmp/mesh-ui-chromium
python tools/browser_smoke.py --browser webkit --output /tmp/mesh-ui-webkit
```

CIではChromium/WebKitを別ジョブで実行し、日本語フォントを導入します。WebKitのLinux上での成功はSafari/macOS実機USBの成功と同義ではありません。

## 検査内容

デモと実接続画面の分離、5画面の切替、ノード選択、ズーム/全体表示、1440×1000・390×844の横はみ出し、JavaScriptエラーを検査します。模擬接続では、3回のうち1回を遠端未応答にし、その回にもlocal ACKを返してPDRが66.67%になることを確かめます。1000bytesの指定、日本語/絵文字、HTMLを含む文字列のエスケープ、remote delivery ACK、保存JSONと切断後のポート解放を確認します。

スクリーンショット、検査summary、模擬セッションJSONはCI artifactに14日間保存します。`demo-*` と `simulated-*` は合成データで、到達距離やRF安定性の実測証拠ではありません。PNGの確認では文字サイズ、余白、表の整列、ノード/経路表示、画面幅への追従を見ます。主な実機未検証項目は `field_validation.md` を参照してください。
