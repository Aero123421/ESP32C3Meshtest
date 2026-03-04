# failure_triage.md

## 1. 目的
- `mesh_smoke_test.py` 失敗時の原因分類と再試験条件を統一する。

## 2. 自動分類手順
1. `summary.json` を入力して `tools/triage_mesh_failure.py` を実行
2. `triage_report.md` と `failure_bundle.json` を確認
3. 主要 `code` ごとに再試験条件を適用

コマンド例:
```powershell
py -3 .\tools\triage_mesh_failure.py `
  --summary-json .\test_logs\<session>\smoke\<run_id>_summary.json `
  --report-md .\test_logs\<session>\triage\<run_id>_triage_report.md `
  --bundle-json .\test_logs\<session>\triage\<run_id>_failure_bundle.json
```

## 3. 代表コードと対処
- `MIN_SUCCESS_RATE`: 送信間隔を広げる、TTL/配置を見直す、混信チャネル回避。
- `MAX_LATENCY_MS` / `MAX_LATENCY_P95_MS`: 混雑時間帯回避、リレー配置再調整。
- `MAX_RETRY_RATE`: 通信負荷低減、ノード間距離短縮、障害物条件を記録。
- `MAX_RX_QUEUE_DROP_RATIO`: 送信頻度を下げる、同時送信本数を削減。
- `REQUIRE_MIN_HOPS`: 直通化されている可能性。中継が必須の配置で再試験。
- `MIN_ROUTE_HIT_RATE`: NodeInfo収束待ち時間を延長し再評価。
- `MAX_ROUTE_FALLBACK_RATIO`: 経路不安定。電源状態/ノイズ源/設置高さを再確認。
- `FW_MISMATCH_UNKNOWN_CMD`: ファーム/PCアプリの不整合。全ノード再書込を実施。

## 4. 再試験時に必ず残す情報
- セッションID、git SHA、firmware SHA256
- ポート割当、配置図、電源方式、アンテナ条件
- 失敗時刻前後30秒のノードログ
