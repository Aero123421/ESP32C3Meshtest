# reliable_1k_test_design.md

## 1. 目的
- `reliable_1k` は、宛先指定 Wi-Fi（ESP-NOW Hybrid Mesh）で 1KB 級データを **3台から10台へ段階拡張**しながら、到達率と復旧性を定量評価する試験プロファイルとする。
- 対象ペイロード:
  - `ping_probe` (`probe_bytes=1000`)
  - `long_text`（約 1000 bytes、`long_text_start/chunk/end` + `delivery_ack`）

## 2. 前提条件（固定）
- ボード: XIAO ESP32C3
- 同一ファームを全ノードに書き込み（混在禁止）
- `platformio.ini` 既定値を固定:
  - `LPWA_MESH_CHANNEL=1`
  - `LPWA_ENABLE_WIFI_LR=1`
  - `LPWA_MESH_TX_POWER_QDBM=84`
- BLE は本試験対象外（`--skip-ble`）
- ポート割当（NodeID/COM対応）をセッション記録に固定保存

## 3. 評価指標（環境共通の定義）

| 指標 | 定義 | 算出式 |
|---|---|---|
| PDR | 1KB E2E到達率 | `PDR[%] = 100 * (成功受信数 / 送信数)` |
| E2E遅延 | 送信から応答受信までの往復遅延 | `pong.latency_ms` の `p50/p95/max` |
| goodput | アプリ有効データの実効転送量 | `goodput[KB/s] = 成功payload総bytes / 試験時間[s] / 1024` |
| 再送率 | 再送が必要だった送信の割合 | `retry_rate[%] = 100 * (retry_no>0 の成功件数 / 成功件数)` |
| 復元率 | 障害注入後にKPI帯へ復帰できた割合 | `recovery_rate[%] = 100 * (復帰成功イベント / 障害イベント)` |

補足:
- `復帰成功` は「障害注入後 `recovery_window_sec` 内に PDR と遅延が基準値へ復帰」と定義する。
- `mesh.rx_queue_dropped` は `rx_frames` 比率で監視し、過負荷兆候として別途 fail 判定に使う。
- 現行 `mesh_smoke_test.py` のラウンド判定は次を使用する:
  - 遅延: 成功ラウンドの `latency_ms` の `max`
  - 再送率: `mesh_delta.tx_no_mem_retries / mesh_delta.tx_frames`（`--collect-stats` 時）

## 4. 試験環境の定義（家屋内 / 障害物 / 屋外）

| 環境 | 推奨配置 | 主な観測ポイント |
|---|---|---|
| 家屋内 | 同一建屋・壁1〜2枚/ホップ、5〜12m/ホップ | 安定時PDR、再送発生率、連続運転ドリフト |
| 障害物 | 鉄筋壁/階段/金属棚を跨ぐ、8〜20m/ホップ | 再送増加時の遅延悪化、復元率 |
| 屋外 | 見通し（LoS）中心、15〜40m/ホップ | 最大到達距離での goodput と遅延 |

## 5. 段階試験計画（3台→10台）

| Stage | 台数 | 目的 | 必須シナリオ | 合格して次へ進む条件 |
|---|---|---|---|---|
| S1 | 3 | 基本成立性確認 | node_list / directed chat / long_text / 1KB ping_probe | S1 KPIを3連続pass |
| S2 | 4〜5 | 中継増加時の劣化確認 | S1 + 送信元入替（先頭ポート回転） | S2 KPIを3連続pass |
| S3 | 6〜7 | 中規模メッシュ | S2 + 30分連続運転 | S3 KPIを3連続pass |
| S4 | 8 | 高密度前段 | S3 + 障害注入1回（中継1台再起動） | S4 KPIを3連続pass |
| S5 | 10 | 最終受入評価 | S4 + 障害注入2回（別ノード） | 3環境すべてで最終KPI pass |

## 6. 実行コマンド例（現行スクリプトで即実行可能）

### 6.1 セッション準備（10台例）
```powershell
cd D:\codebase\LPWAtestESP32
$ports10 = @("COM6","COM7","COM8","COM9","COM10","COM11","COM12","COM13","COM14","COM15")
.\tools\prepare_test_session.ps1 -Ports $ports10
.\tools\flash_all.ps1 -Ports $ports10
.\tools\monitor_all.ps1 -Ports $ports10 -Baud 115200 -LogDir .\test_logs\reliable_1k_20260303
```

### 6.2 Stage別実行（1回）
```powershell
# S1: 3台
py -3 .\tools\mesh_smoke_test.py --ports COM6 COM7 COM8 --timeout 35 --ack-timeout 4 --ack-retries 6 --skip-ble --min-node-count 3

# S3: 6台
py -3 .\tools\mesh_smoke_test.py --ports COM6 COM7 COM8 COM9 COM10 COM11 --timeout 45 --ack-timeout 4 --ack-retries 6 --skip-ble --min-node-count 6

# S5: 10台
py -3 .\tools\mesh_smoke_test.py --ports COM6 COM7 COM8 COM9 COM10 COM11 COM12 COM13 COM14 COM15 --timeout 55 --ack-timeout 4 --ack-retries 8 --skip-ble --min-node-count 10
```

### 6.3 連続試験（例: 20ラウンド）
```powershell
$ports6 = @("COM6","COM7","COM8","COM9","COM10","COM11")
1..20 | ForEach-Object {
  py -3 .\tools\mesh_smoke_test.py --ports $ports6 --timeout 45 --ack-timeout 4 --ack-retries 6 --skip-ble --min-node-count 6 2>&1 |
    Tee-Object ".\test_logs\reliable_1k_20260303\S3_indoor_round$($_).log"
  if ($LASTEXITCODE -ne 0) { throw "round $_ failed" }
}
```

### 6.4 復元率試験（現行運用）
1. 連続試験実行中に中継ノード1台のUSBを 10 秒抜き差しする。  
2. `node_list` が期待台数に戻る時刻をログで記録する。  
3. 復帰後 2 分間の PDR/遅延が閾値に戻るかを確認する。  

## 7. 受け入れ基準（pass/fail）

### 7.1 Stage進行用（最低条件）
- S1（3台, 家屋内）:
  - PDR `>= 98%`
  - E2E遅延 `p95 <= 1200ms`
  - goodput `>= 0.90 KB/s`
  - 再送率 `<= 12%`
- S2〜S3（4〜7台, 家屋内）:
  - PDR `>= 96%`
  - E2E遅延 `p95 <= 1500ms`
  - goodput `>= 0.75 KB/s`
  - 再送率 `<= 18%`
- S4（8台, 家屋内+障害物）:
  - 家屋内: PDR `>= 95%`, p95 `<= 1700ms`
  - 障害物: PDR `>= 92%`, p95 `<= 2200ms`
  - 復元率 `>= 85%`

### 7.2 最終受け入れ（S5: 10台）

| 環境 | PDR | E2E遅延(p95) | goodput | 再送率 | 復元率 |
|---|---|---|---|---|---|
| 家屋内 | `>= 95%` | `<= 1600ms` | `>= 0.70 KB/s` | `<= 20%` | `>= 90%` |
| 障害物 | `>= 90%` | `<= 2400ms` | `>= 0.45 KB/s` | `<= 30%` | `>= 85%` |
| 屋外 | `>= 93%` | `<= 1800ms` | `>= 0.65 KB/s` | `<= 22%` | `>= 90%` |

共通 fail 条件:
- `node_list` が `expected_nodes` 未達（30秒以内）
- `ALL TESTS PASSED` が得られない
- `rx_queue_dropped / rx_frames >= 1%`（`get_stats` で確認）
- ノードハング/再起動が発生

## 8. `tools/mesh_smoke_test.py` 実装済み拡張

### 8.1 追加CLI（実装済み）
- `--rounds N` / `--interval-ms N`
- `--rotate-tx`（ラウンドごとに送信ノードをローテーション）
- `--collect-stats`（各ラウンド前後で `{"cmd":"get_stats"}` を自動取得）
- `--threshold-file <path>` + `--strict-pass`（閾値判定で終了コード制御）
- `--require-min-hops N`（成功ラウンドで最小ホップ要件を課す）
- `--session-dir <path>` / `--run-id <id>` / `--scenario <name>`
- `--jsonl-out <path>`（ラウンドごとの詳細）
- `--events-jsonl <path>`（イベントログ）
- `--summary-json <path>`（集計結果）

### 8.2 ラウンド集計ロジック（実装済み）
- directed `ping_probe` を `N` ラウンド実行し、各ラウンドで:
  - `success` / `latency_ms` / `hops` / `probe_hash_ok`
  - `mesh_delta`（`--collect-stats` 時）
  - `retry_rate` / `rx_queue_drop_ratio`（`mesh_delta` から算出）
- 最終集計:
  - `success_rate`
  - `latency min/max/avg/p95`
  - `hops min/max`
  - `max_consecutive_failures_observed`
  - `route_hit_rate_observed`
  - `route_fallback_ratio_observed`
  - `threshold_violations`

### 8.3 閾値ファイル仕様（実装済み）
- JSON object のキー:
  - `min_success_rate`（0.0..1.0）
  - `max_latency_ms`（>0）
  - `max_latency_p95_ms`（>0）
  - `max_retry_rate`（0.0..1.0）
  - `max_rx_queue_drop_ratio`（0.0..1.0）
  - `require_min_hops`（>=0）
  - `max_consecutive_failures`（>=0）
  - `min_probe_hash_ok_rate`（0.0..1.0）
  - `min_route_hit_rate`（0.0..1.0）
  - `max_route_fallback_ratio`（0.0..1.0）
- CLI側 `--require-min-hops` / `--r1k-max-latency-ms` / `--r1k-max-retry-rate` と合成される。

### 8.4 実行例（3台）
```powershell
py -3 .\tools\mesh_smoke_test.py `
  --ports COM6 COM7 COM8 `
  --timeout 45 --ack-timeout 4 --ack-retries 6 --skip-ble `
  --rounds 12 --interval-ms 700 --rotate-tx --collect-stats `
  --session-dir .\test_logs\session_xxx --run-id smoke_a --scenario indoor_s5 `
  --threshold-file .\docs\reliable_1k_thresholds.json --strict-pass `
  --jsonl-out .\test_logs\session_xxx\smoke\smoke_a_rounds.jsonl `
  --events-jsonl .\test_logs\session_xxx\smoke\smoke_a_events.jsonl `
  --summary-json .\test_logs\session_xxx\smoke\smoke_a_summary.json
```

補足:
- 近距離で直通リンクが成立する配置では `hops=0` になり、`--require-min-hops 1` は fail/violation になる。
- 中継強制を評価する場合は `A-C` を直通不可に配置し、`A->B->C` を物理的に作る。

## 9. 運用記録
- 実施時は `test_logs/<session>/session.md` に以下を必須記録:
  - 実施環境（家屋内/障害物/屋外）
  - アンテナ向き/高さ
  - 障害注入時刻と対象ノード
  - pass/fail 判定根拠（指標値）
