# test_plan.md

## 1. 目的
- 10台のESP32-C3でメッシュ中継を構成し、3台のPC直結ノード間で `text` / `image` 通信が成立するかを評価する。
- ノード再起動や輻輳時でも、通信品質が運用可能な範囲に収まるかを確認する。

## 2. 想定ユースケース
- ノード数: 10台
- PC直結ノード: 3台（例: `GW-A`, `GW-B`, `GW-C`）
- 残り7台: 中継専用（USB給電のみ）
- 通信:
  - PC-A <-> PC-B: text / long_text(1000文字級) / image
  - PC-B <-> PC-C: text / long_text(1000文字級) / image
  - PC-A <-> PC-C: text / long_text(1000文字級) / image

## 2.1 Phase定義（1〜5）

| Phase | 目的 | 実装要素 | 完了判定 |
|---|---|---|---|
| Phase1 | P2P成立 | chat/ping基本疎通 | 2台で双方向chat/ping成功 |
| Phase2 | 経路最適化 | route学習 + next-hop転送 + fallback flood | `route_lookup_hit/(hit+miss) >= 70%` |
| Phase3 | 1KB高信頼 | `reliable_1k_start/chunk/end` + NACK/repair | 1KB復元成功 + `delivery_ack` 成立 |
| Phase4 | 自動適応/運用 | 宛先別profile自動調整 + Reliable統計 + route可視化 | 復元率/再送率がKPI範囲で安定 |
| Phase5 | 回帰自動化 | session標準化 + 閾値プロファイル + triage自動化 | `run_mesh_regression.ps1` で再現可能 |

補足:
- Phase3/4の詳細基準は `docs/reliable_1k_test_design.md` を優先する。
- `tools/mesh_smoke_test.py` は `Directed reliable_1k (FEC)` を含む回帰を実施する。

## 3. 体制と固定情報
- すべてのノードは同一ファームを使用する。
- 各セッションで以下を固定して記録する:
  - ポート割当（NodeIDとCOMの対応）
  - 実施場所（屋内/屋外、障害物）
  - Wi-Fiチャネル条件
  - 電源方式（PC USB / モバイルバッテリー）
  - アンテナ条件（向き・高さ）

## 4. 事前準備
1. セッションテンプレートを作成
   - `.\tools\prepare_test_session.ps1 -Ports COM3,COM4,COM5,COM6,COM7,COM8,COM9,COM10,COM11,COM12`
2. 同一ビルドを書き込み
   - `.\tools\flash_all.ps1 -Ports COM3,COM4,COM5,COM6,COM7,COM8,COM9,COM10,COM11,COM12 -SessionDir .\test_logs\<session>`
3. ログ保存付きモニタ起動
   - `.\tools\monitor_all.ps1 -Ports COM3,COM4,COM5,COM6,COM7,COM8,COM9,COM10,COM11,COM12 -Baud 115200 -SessionDir .\test_logs\<session>`
4. PC GUIで各PCの直結ノードを接続し、`nodes_request` でノード一覧を取得

## 5. 評価KPI（初期値）
- Text E2E PDR（1KB未満短文）: `>= 98%`
- LongText E2E PDR（1000文字級、宛先指定）: `>= 95%`
- Directed delivery_ack 成功率（text / image packet）: `>= 98%`
- 再送発生率（retry_no > 0）: `<= 15%`（初期目安）
- 経路学習有効率（Phase2）: `route_lookup_hit / (hit+miss) >= 70%`（安定区間の目安）
- Image転送成功率（64KB〜256KB）: `>= 95%`
- Ping RTT: `p95 <= 1500ms`（10ノード試験時）
- ノード再起動後の復帰時間: `<= 30s`
- 連続運転時の異常:
  - ハング/再起動: `0`
  - `rx_queue_dropped` 比率: `1%未満` を目安

※ 本KPIは初期値。実測後に運用目標へ更新する。

## 6. テスト項目

| ID | 項目 | 目的 | 合格条件 |
|---|---|---|---|
| U01 | 10ノード認識 | 全ノードの可視化 | 3PCいずれからも `node_list >= 10` |
| U02 | PC-A↔PC-B text | E2E text成立 | 双方向PDRがKPIを満たす |
| U03 | PC-B↔PC-C text | E2E text成立 | 双方向PDRがKPIを満たす |
| U04 | 3PC同時text | 輻輳下の成立性 | 全ペアでKPI内 |
| U04b | 3PC同時long_text | 1000文字級の同時成立性 | 全ペアでLongText KPI内 |
| U05 | PC間image転送 | 分割再構成の成立性 | 破損なしで成功率KPI内 |
| U06 | 中継品質 | マルチホップ性能確認 | hop数・遅延・損失を記録し閾値内 |
| U07 | delivery_ack/再送 | 宛先指定通信の信頼性確認 | `delivery_ack` 成功率と再送率がKPI内 |
| U07b | Phase2経路検証 | 次ホップ転送の有効性確認 | `get_stats` の `route_lookup_hit` が増加し、`routed_fallback_flood` が抑制される |
| U08 | 再起動復帰 | 運用回復性確認 | 再参加と通信再開が30秒以内 |
| U09 | 長時間安定性 | 連続運転耐性 | 2時間で重大異常なし |
| U10 | reliable_1k | 1KB級E2E信頼性（3〜10台）評価 | `docs/reliable_1k_test_design.md` の受け入れ基準を満たす |
| U10b | round統計試験 | ラウンド連続時の劣化検出 | `mesh_smoke_test.py` の `summary_json` で success_rate/latency/hops/queue drop を評価 |

## 7. 実施時の注意
- 1セッション内ではファーム差分を混在させない。
- 画像送信は衝突を避けるため、並列数を制御して段階的に増やす。
- Broadcast送信は試験目的がある場合のみ実施し、通常は宛先指定で評価する。
- 失敗時は以下を必ず保存:
  - 失敗時刻
  - 送受信PC
  - 宛先ノード
  - 直前30秒のシリアルログ

## 8. 収集ログ
- `test_logs/<timestamp>/session.md`
- `test_logs/<timestamp>/session.json`
- `test_logs/<timestamp>/flash_result.json`
- `test_logs/<timestamp>/monitor_manifest.json`
- `test_logs/<timestamp>/monitor/Node*_COM*.log`（`monitor_all.ps1` を `-SessionDir` で実行時）
- GUIログ（必要に応じて保存）
- `test_logs/*.jsonl` / `test_logs/*.json`（`mesh_smoke_test.py --jsonl-out --summary-json`）

## 8.1 3台の高速回帰コマンド（実装済み）
```powershell
py -3 .\tools\mesh_smoke_test.py `
  --ports COM6 COM7 COM8 `
  --timeout 45 --ack-timeout 4 --ack-retries 6 --skip-ble `
  --rounds 12 --interval-ms 700 --rotate-tx --collect-stats `
  --scenario indoor_s5 `
  --session-dir .\test_logs\session_xxx --run-id smoke_a `
  --threshold-file .\docs\reliable_1k_thresholds.json --strict-pass `
  --jsonl-out .\test_logs\session_xxx\smoke\smoke_a_rounds.jsonl `
  --events-jsonl .\test_logs\session_xxx\smoke\smoke_a_events.jsonl `
  --summary-json .\test_logs\session_xxx\smoke\smoke_a_summary.json
```

補足:
- `require_min_hops` は threshold file で制御する。中継強制の配置で値を `1` 以上にする。
- 環境別しきい値は `docs/threshold_profiles/*.json` を使用する。
- 3台ベンチの初期確認は `docs/threshold_profiles/indoor_s3_baseline.json` を推奨する。
- `run_mesh_regression.ps1` は既定で `delivery_ack` 必須。初期確認のみ `-AllowMissingDeliveryAck` で緩和する。

## 9. 未解決事項
- 最終KPI（本番運用値）の確定
- 画像サイズ別の許容遅延の確定
- 10ノード実測に基づく `delivery_ack` 成功率/再送率しきい値の再基準化

## 10. reliable_1k 詳細設計
- 1KB級の段階試験（3〜10台）、環境別評価指標、受け入れ基準、`mesh_smoke_test.py` 拡張案は `docs/reliable_1k_test_design.md` を参照する。
