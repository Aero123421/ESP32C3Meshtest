# reliable_1k GUI/運用設計（pc_app + tools）

## 1. 目的
- `reliable_1k` は「1KB級データを実機で再現性高く送受信し、復元率/再送率/失敗理由を可視化する」ためのGUI運用プロファイル。
- 既存の `ping_probe (1KB)` と `long_text (1000文字級)` を統合して、リンク品質と復元品質を同一画面で評価する。

## 2. 現状整理（読み取り結果）
- GUIの信頼送信は `dst` 指定 `wifi` 時のみ有効（`_is_reliable_target`）。
- `ping_probe` は常時 `probe_bytes=1000` の単発送信/連続送信。
- `long_text` は `long_text_start/chunk/end` へ分割し、`delivery_ack` と再送を利用。
- `reliable_1k` は `start/chunk/end + nack/repair/result` をGUIで送受信できる。
- `ReliableStats` で復元率/再送率/失敗理由/profile利用を集計表示できる。
- 運用スクリプトは `prepare_test_session.ps1` / `flash_all.ps1` / `monitor_all.ps1` が揃っている。
- `setup_and_run_gui.bat` は venv 構築とGUI起動を実施し、GUI試験の起点として利用できる。

## 3. UI追加設計（必須1）

### 3.1 追加項目
- モード切替:
  - `通常`（現行相当）
  - `高信頼(reliable_1k)`（再送強化 + 冗長送信）
- 冗長率:
  - `0.00〜1.00`（0.05刻み）
  - 意味: `0.00=冗長送信なし`、`1.00=最大冗長`
- 統計表示:
  - `復元率(%)`
  - `再送率(%)`
  - `復元失敗理由トップN`
  - `delivery_ack 成功率(%)`
  - `1KB ping_probe 成功率(%)`

### 3.2 画面配置
- `試験`タブの `Ping / 連続試験` の下に `reliable_1k 設定` セクションを追加。
- `PDR / 遅延統計` の下に `Reliable統計` を追加（既存Ping統計と並列表示）。
- `ログ`タブに `失敗理由ヒートマップ（件数）` と `再送率トレンド` を追加。

### 3.3 モード動作
- 通常:
  - 現行ロジック維持（後方互換）。
- 高信頼:
  - `delivery_ack timeout` と `max retry` を冗長率連動で拡張。
  - `long_text_chunk` を冗長率に応じて追加送信（同一 `e2e_id` 系列で管理）。
  - `ping_probe` は1ラウンド内で複数送信を許可し、最良/中央値を統計化。

## 4. ping_probe と long_text の関係（必須2）
- `ping_probe`:
  - 1KB固定ペイロードでリンク健全性（往復遅延・欠損）を測る層。
- `long_text`:
  - 1KB級実データの分割再構成と `delivery_ack` 完了性を測る層。
- `reliable_1k` では以下を1セットとして扱う:
  1. Directed `ping_probe`（経路とRTT確認）
  2. Directed `long_text`（分割再構成/ハッシュ整合確認）
  3. `delivery_ack` と再送履歴を突合して1ケース判定
- 判定優先度:
  - `long_text` 復元成功を主指標、`ping_probe` は前兆監視指標。

## 5. ログ/可視化設計（必須3）

### 5.1 追加ログ
- GUI保存ログ（既存 `.log`）に加えて JSONL を追加:
  - `test_logs/<session>/gui_reliable_1k_events.jsonl`
- 1イベント1行で最低限以下を保持:
  - `ts_ms`, `mode`, `redundancy_ratio`, `scenario_id`
  - `type` (`ping_probe`/`long_text`/`delivery_ack`)
  - `e2e_id`, `retry_no`, `result`
  - `fail_reason`（失敗時のみ）

### 5.2 集計指標
- 復元率:
  - `restore_success / (restore_success + restore_failed) * 100`
- 再送率:
  - `retry_packets / total_packets * 100`
- delivery_ack 成功率:
  - `delivery_ok / delivery_total * 100`

### 5.3 復元失敗理由コード（標準化）
- `missing_chunks`
- `size_mismatch`
- `sha256_mismatch`
- `decode_failed`
- `delivery_timeout`
- `delivery_status_ng`
- `ack_mismatch`
- `probe_hash_ng`
- `unknown_cmd_ping_probe`
- `queue_full`
- `session_expired`

## 6. 運用手順（setup_and_run_gui.bat 反映、必須4）
1. セッション作成  
   `.\tools\prepare_test_session.ps1 -Ports COMx,COMy,...`
2. 書き込み  
   `.\tools\flash_all.ps1 -Ports COMx,COMy,...`
3. モニタ開始  
   `.\tools\monitor_all.ps1 -Ports COMx,COMy,... -LogDir .\test_logs\<session>`
4. GUI準備/起動  
   `cd .\pc_app`  
   `.\setup_and_run_gui.bat --profile reliable_1k --session-dir ..\test_logs\<session>`
5. GUIで `高信頼(reliable_1k)` + 冗長率設定し、Directed 宛先で試験実行
6. 終了時に `gui_reliable_1k_events.jsonl` と `session.md` へ結果を転記

## 7. ファイル単位の変更案

### pc_app/app.py
- `試験`タブへ `reliable_1k 設定` UIを追加（モード/冗長率/統計）。
- `handle_delivery_ack` / `handle_long_text_payload` / `handle_pong` で構造化メトリクスを更新。
- 既存 `save_logs()` とは別に `save_reliable_events_jsonl()` を追加。
- 起動時に `LPWA_GUI_PROFILE` / `LPWA_TEST_SESSION_DIR` を読んで初期値反映。

### pc_app/lpwa_gui/stats.py
- `PingStats` は維持し、`ReliableStats` を追加:
  - `restore_success`, `restore_failed`, `retry_packets`, `fail_reason_counts` 等を保持。
  - `snapshot()` でGUI表示用辞書を返す。

### pc_app/lpwa_gui/protocol.py
- 冗長送信用ヘルパーを追加（例: `make_reliable_long_text_messages(..., redundancy_ratio)`）。
- 既存 `make_ping_probe_command` は維持し、ラウンドID/試験IDを任意付与できる拡張を検討。

### pc_app/self_check.py
- `ReliableStats` の計算検証を追加。
- `redundancy_ratio` 指定時の long_text 送信生成数を検証。
- 失敗理由コードの正規化テストを追加。

### pc_app/README.md
- `reliable_1k` モードの説明を追加。
- GUI操作手順（通常/高信頼、冗長率、統計の見方）を追記。
- ログ出力（`.log` + `jsonl`）と解析例を追記。

### pc_app/setup_and_run_gui.bat
- 新オプション追加:
  - `--profile reliable_1k|normal`
  - `--session-dir <path>`
- 指定値を環境変数で `app.py` へ渡す。
- `--setup-only` は現行維持。

### tools/prepare_test_session.ps1
- `session.md` テンプレートに `reliable_1k` 記録欄を追加:
  - モード、冗長率、復元率、再送率、失敗理由トップ3。

### tools/monitor_all.ps1
- 追記不要でも運用可能だが、任意で `-SessionId` を追加しGUIログと命名規約を統一。

### tools（新規）/analyze_reliable_1k_log.py
- `gui_reliable_1k_events.jsonl` から集計CSVを生成:
  - `restore_rate`, `retry_rate`, `fail_reason_breakdown`。
- `test_logs/<session>/reliable_1k_summary.csv` を出力。

### docs/test_plan.md
- `U07` 系に `reliable_1k` 実施手順と判定式（復元率/再送率）を追記。

### docs/architecture.md
- `ACK/再送` 節へ `reliable_1k` プロファイル（通常/高信頼、冗長率）を追記。

## 8. 導入順（最小リスク）
1. `stats.py` と `app.py` で集計のみ追加（送信挙動は変えない）。
2. UI追加でモード/冗長率を表示し、通常モードをデフォルト維持。
3. 高信頼モードの送信拡張（再送/冗長）を有効化。
4. `tools/analyze_reliable_1k_log.py` と `docs` 更新で運用を固定化。
