#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


FAILURE_HINTS: dict[str, str] = {
    "min_success_rate": "PDR不足。送信間隔拡大・TTL/配置見直し・混信チャネル回避を優先。",
    "max_latency_ms": "最大遅延超過。輻輳回避（interval増加）と経路安定化を確認。",
    "max_latency_p95_ms": "遅延のばらつきが大きい。中継ノード配置と混雑時間帯の再試験が必要。",
    "max_retry_rate": "再送率が高い。リンク品質低下か競合。距離/障害物/送信レートを見直し。",
    "max_rx_queue_drop_ratio": "受信キュー溢れ。送信レート低減、ノード分散、FW負荷確認を実施。",
    "require_min_hops": "中継経路が形成されていない。ノード配置を離して再測定。",
    "max_consecutive_failures": "連続失敗発生。瞬断ではなく継続劣化。電源/ノイズ源確認を推奨。",
    "min_probe_hash_ok_rate": "整合性不一致が多い。衝突・ノイズ・フラグメント欠損を疑う。",
    "min_route_hit_rate": "ルートヒット率不足。NodeInfo収束待ち、経路学習時間を確保。",
    "max_route_fallback_ratio": "fallback flood依存が高い。経路固定性と隣接品質を見直し。",
    "stats_collection": "統計取得が途中で欠落。PC/FW負荷や monitor ログも併せて確認。",
}

LOG_SIGNATURES: list[tuple[str, str, str, str]] = [
    ("unknown_cmd ping_probe", "FW_MISMATCH_UNKNOWN_CMD", "protocol_compat", "FW/PCアプリのバージョン不一致。全ノード再書込を推奨。"),
    ("node_list count too small", "NODE_LIST_SHORTFALL", "discovery", "NodeInfo収束不足。待機時間延長と配置確認を実施。"),
    ("NG: directed chat timeout", "DIRECTED_CHAT_TIMEOUT", "delivery", "宛先到達失敗。経路学習/配置/干渉を確認。"),
    ("NG: directed delivery_ack timeout", "DIRECTED_DELIVERY_ACK_TIMEOUT", "ack_path", "逆方向ACK経路不良。中継配置とTTLを確認。"),
    ("NG: long_text_start delivery_ack timeout", "LONG_TEXT_START_ACK_TIMEOUT", "ack_path", "long_text_start ACK未達。逆方向経路と混雑を確認。"),
    ("NG: long_text_chunk delivery_ack timeout", "LONG_TEXT_CHUNK_ACK_TIMEOUT", "ack_path", "long_text_chunk ACK未達。間隔拡大・再送設定を見直し。"),
    ("NG: long_text_end delivery_ack timeout", "LONG_TEXT_END_ACK_TIMEOUT", "ack_path", "long_text_end ACK未達。終端パケットの逆方向経路を確認。"),
    ("NG: pong timeout", "PING_PONG_TIMEOUT", "latency", "ping_probe応答なし。ノイズ/距離/経路切替を確認。"),
    ("NG: long text hash mismatch", "LONG_TEXT_HASH_MISMATCH", "integrity", "チャンク欠損/順序不整合を確認。"),
    ("NG: long text decode mismatch", "LONG_TEXT_DECODE_MISMATCH", "integrity", "受信データ破損またはエンコード不整合を確認。"),
    ("NG: reliable_1k decode failed", "RELIABLE_1K_DECODE_FAILED", "integrity", "復元失敗。冗長率/再送率/干渉状況を確認。"),
    ("NG: reliable_1k payload mismatch", "RELIABLE_1K_PAYLOAD_MISMATCH", "integrity", "復元内容不一致。シャード欠損や重複を確認。"),
    ("NG: threshold violation found", "THRESHOLD_VIOLATION_ENFORCED", "threshold", "閾値超過。summaryの違反項目を確認。"),
]


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def classify(summary: dict[str, Any], raw_logs: list[Path]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    dedup: set[tuple[str, str, str, str]] = set()

    def add_finding(entry: dict[str, Any]) -> None:
        code = str(entry.get("code") or "UNKNOWN")
        file_key = str(entry.get("file") or "")
        reason_key = str(entry.get("reason") or "")
        metric_key = str(entry.get("metric") or "")
        key = (code, file_key, metric_key, reason_key)
        if key in dedup:
            return
        dedup.add(key)
        findings.append(entry)

    if not summary:
        add_finding(
            {
                "code": "SUMMARY_MISSING",
                "metric": "artifact",
                "hint": "summary JSON が見つかりません。smokeログとmonitorログから原因を切り分けてください。",
            }
        )
    else:
        smoke_exit_code = summary.get("smoke_exit_code")
        failure_stage = str(summary.get("failure_stage") or "").strip()
        failure_reason = str(summary.get("failure_reason") or "").strip()
        if isinstance(smoke_exit_code, int) and smoke_exit_code != 0:
            add_finding(
                {
                    "code": "SMOKE_FAILED",
                    "metric": "smoke_exit_code",
                    "actual": smoke_exit_code,
                    "limit": 0,
                    "reason": failure_reason or None,
                    "hint": "mesh_smoke_test が失敗終了しました。summary/ログの直近NG行を確認してください。",
                }
            )
        elif failure_stage or failure_reason:
            add_finding(
                {
                    "code": "SMOKE_INCOMPLETE",
                    "metric": "artifact",
                    "reason": failure_reason or failure_stage,
                    "hint": "回帰実行が途中終了しています。直前のログを確認してください。",
                }
            )

    round_summary = summary.get("round_summary") if isinstance(summary.get("round_summary"), dict) else {}
    stats_section = round_summary.get("stats") if isinstance(round_summary.get("stats"), dict) else {}
    stats_timeout_rounds = int(round_summary.get("stats_timeout_rounds") or 0) if isinstance(round_summary, dict) else 0
    stats_expected_rounds = int(stats_section.get("expected_rounds") or 0) if isinstance(stats_section, dict) else 0
    stats_complete_rounds = int(stats_section.get("complete_rounds") or 0) if isinstance(stats_section, dict) else 0
    stats_incomplete_rounds = stats_section.get("incomplete_rounds") if isinstance(stats_section, dict) else []
    collect_stats = bool(round_summary.get("collect_stats")) if isinstance(round_summary, dict) else False
    violations = round_summary.get("threshold_violations") if isinstance(round_summary, dict) else []
    if isinstance(violations, list):
        for v in violations:
            if not isinstance(v, dict):
                continue
            metric = str(v.get("metric") or "unknown")
            add_finding(
                {
                    "code": metric.upper(),
                    "metric": metric,
                    "actual": v.get("actual"),
                    "limit": v.get("limit"),
                    "reason": v.get("reason"),
                    "hint": FAILURE_HINTS.get(metric, "詳細ログを確認してください。"),
                }
            )
    if (
        collect_stats
        and stats_expected_rounds > 0
        and stats_complete_rounds < stats_expected_rounds
        and not isinstance(stats_incomplete_rounds, list)
    ):
        stats_incomplete_rounds = []
    if (
        collect_stats
        and stats_expected_rounds > 0
        and stats_complete_rounds < stats_expected_rounds
        and not any(str(f.get("code") or "") == "STATS_COLLECTION" for f in findings)
    ):
        add_finding(
            {
                "code": "STATS_COLLECTION",
                "metric": "stats_collection",
                "actual": {
                    "complete_rounds": stats_complete_rounds,
                    "expected_rounds": stats_expected_rounds,
                    "timeout_rounds": stats_timeout_rounds,
                    "incomplete_rounds": stats_incomplete_rounds,
                },
                "limit": stats_expected_rounds,
                "reason": "stats_incomplete",
                "hint": FAILURE_HINTS["stats_collection"],
            }
        )
    elif stats_timeout_rounds > 0 and not any(str(f.get("code") or "") == "STATS_COLLECTION" for f in findings):
        add_finding(
            {
                "code": "STATS_COLLECTION",
                "metric": "stats_collection",
                "actual": stats_timeout_rounds,
                "limit": 0,
                "reason": "stats_timeout_rounds",
                "hint": FAILURE_HINTS["stats_collection"],
            }
        )
    monitor_logs = [path for path in raw_logs if "monitor" in str(path).lower()]
    summary_monitor_logs = summary.get("monitor_logs_attached") if isinstance(summary.get("monitor_logs_attached"), list) else []
    monitor_requested = bool(summary.get("monitor_requested")) if isinstance(summary, dict) and "monitor_requested" in summary else bool(summary)
    monitor_logs_missing = bool(summary.get("monitor_logs_missing")) if isinstance(summary, dict) else False
    monitor_expected_ports = summary.get("monitor_expected_ports") if isinstance(summary.get("monitor_expected_ports"), list) else []
    monitor_missing_ports = summary.get("monitor_missing_ports") if isinstance(summary.get("monitor_missing_ports"), list) else []
    attached_monitor_count = max(len(monitor_logs), len(summary_monitor_logs))
    if monitor_requested and (monitor_logs_missing or attached_monitor_count == 0 or len(monitor_missing_ports) > 0):
        add_finding(
            {
                "code": "MONITOR_LOG_MISSING",
                "metric": "monitor_log",
                "actual": {
                    "attached_count": attached_monitor_count,
                    "expected_ports": monitor_expected_ports,
                    "missing_ports": monitor_missing_ports,
                },
                "limit": max(1, len(monitor_expected_ports)),
                "reason": (
                    "monitor_logs_missing"
                    if monitor_logs_missing
                    else "monitor_logs_incomplete"
                    if len(monitor_missing_ports) > 0
                    else "no_monitor_log_attached"
                ),
                "hint": "monitorログが triage 対象に含まれていません。run_mesh_regression では monitor を有効化してください。",
            }
        )

    for log_file in raw_logs:
        try:
            text = log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # monitorログは追記運用されるため、最新run付近を優先して誤検知を減らす。
        lines = text.splitlines()
        if len(lines) > 4000:
            text = "\n".join(lines[-4000:])
        for signature, code, metric, hint in LOG_SIGNATURES:
            if signature in text:
                add_finding(
                    {
                        "code": code,
                        "metric": metric,
                        "hint": hint,
                        "file": str(log_file),
                    }
                )
        # 任意の NG 行を拾って未知失敗を可視化する
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^NG:\s*(.+)$", raw_line)
            if match is None:
                continue
            reason = match.group(1).strip()
            if not reason:
                continue
            add_finding(
                {
                    "code": "SMOKE_NG_LINE",
                    "metric": "smoke_log",
                    "reason": reason,
                    "line": line_no,
                    "hint": "smokeログのNG行。原因は該当行周辺を確認してください。",
                    "file": str(log_file),
                }
            )
    return findings


def write_report(path: Path, findings: list[dict[str, Any]], summary_path: Path | None) -> None:
    lines: list[str] = []
    lines.append("# Mesh Failure Triage Report")
    lines.append("")
    summary_label = str(summary_path) if summary_path is not None else "(missing)"
    lines.append(f"- summary: `{summary_label}`")
    lines.append(f"- findings: {len(findings)}")
    lines.append("")
    if not findings:
        lines.append("失敗分類は検出されませんでした。")
        lines.append("")
    else:
        for idx, f in enumerate(findings, start=1):
            lines.append(f"## {idx}. {f.get('code', 'UNKNOWN')}")
            lines.append(f"- metric: `{f.get('metric', '-')}`")
            lines.append(f"- actual: `{f.get('actual', '-')}`")
            lines.append(f"- limit: `{f.get('limit', '-')}`")
            if f.get("reason") is not None:
                lines.append(f"- reason: `{f.get('reason')}`")
            if f.get("file"):
                lines.append(f"- file: `{f.get('file')}`")
            if f.get("line") is not None:
                lines.append(f"- line: `{f.get('line')}`")
            lines.append(f"- hint: {f.get('hint', '-')}")
            lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Mesh smoke failure triage")
    parser.add_argument("--summary-json", type=Path, help="mesh_smoke_test summary JSON")
    parser.add_argument("--logs", nargs="*", default=[], help="raw log files to inspect")
    parser.add_argument("--report-md", type=Path, required=True, help="output markdown report")
    parser.add_argument("--bundle-json", type=Path, required=True, help="output JSON bundle")
    args = parser.parse_args()

    summary: dict[str, Any] = {}
    summary_path: Path | None = args.summary_json
    if summary_path is not None and summary_path.exists():
        try:
            summary = read_json(summary_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            summary = {
                "failure_stage": "triage",
                "failure_reason": f"summary_parse_error:{exc}",
            }
    else:
        summary_path = None
    log_paths = [Path(p) for p in args.logs]
    findings = classify(summary, log_paths)

    bundle = {
        "summary_json": str(summary_path) if summary_path is not None else None,
        "logs": [str(p) for p in log_paths],
        "findings": findings,
        "count": len(findings),
    }
    args.bundle_json.parent.mkdir(parents=True, exist_ok=True)
    args.bundle_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(args.report_md, findings, summary_path)
    print(f"triage findings={len(findings)} report={args.report_md} bundle={args.bundle_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
