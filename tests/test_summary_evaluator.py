from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_triage_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "tools" / "triage_mesh_failure.py"
    spec = importlib.util.spec_from_file_location("triage_mesh_failure", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classify_threshold_violation(tmp_path: Path) -> None:
    triage = _load_triage_module()
    summary = {
        "round_summary": {
            "threshold_violations": [
                {"metric": "min_success_rate", "actual": 0.6, "limit": 0.9},
                {"metric": "max_latency_ms", "actual": 2500, "limit": 1800},
            ]
        }
    }
    findings = triage.classify(summary, [])
    codes = {f["code"] for f in findings}
    assert "MIN_SUCCESS_RATE" in codes
    assert "MAX_LATENCY_MS" in codes


def test_classify_stats_collection_violation_and_missing_monitor() -> None:
    triage = _load_triage_module()
    summary = {
        "monitor_requested": True,
        "monitor_logs_missing": True,
        "monitor_expected_ports": ["COM6", "COM7", "COM8"],
        "monitor_missing_ports": ["COM6", "COM7", "COM8"],
        "round_summary": {
            "threshold_violations": [
                {"metric": "stats_collection", "actual": 2, "limit": 0, "reason": "stats_timeout_rounds"},
            ]
        }
    }
    findings = triage.classify(summary, [])
    codes = {f["code"] for f in findings}
    assert "STATS_COLLECTION" in codes
    assert "MONITOR_LOG_MISSING" in codes


def test_classify_stats_collection_from_incomplete_stats_block() -> None:
    triage = _load_triage_module()
    summary = {
        "monitor_requested": False,
        "round_summary": {
            "collect_stats": True,
            "stats_timeout_rounds": 1,
            "stats": {
                "expected_rounds": 4,
                "complete_rounds": 2,
                "incomplete_rounds": [2, 4],
            },
            "threshold_violations": [],
        },
    }
    findings = triage.classify(summary, [])
    stats_findings = [f for f in findings if f.get("code") == "STATS_COLLECTION"]
    assert len(stats_findings) == 1
    assert stats_findings[0]["reason"] == "stats_incomplete"
    assert not any(f.get("code") == "MONITOR_LOG_MISSING" for f in findings)


def test_classify_log_signature(tmp_path: Path) -> None:
    triage = _load_triage_module()
    log_file = tmp_path / "node.log"
    log_file.write_text("... unknown_cmd ping_probe ...", encoding="utf-8")
    findings = triage.classify({"round_summary": {"threshold_violations": []}}, [log_file])
    assert any(f.get("code") == "FW_MISMATCH_UNKNOWN_CMD" for f in findings)
    assert any(f.get("code") == "MONITOR_LOG_MISSING" for f in findings)


def test_classify_summary_missing() -> None:
    triage = _load_triage_module()
    findings = triage.classify({}, [])
    assert any(f.get("code") == "SUMMARY_MISSING" for f in findings)


def test_classify_node_list_shortfall_signature(tmp_path: Path) -> None:
    triage = _load_triage_module()
    log_file = tmp_path / "smoke.log"
    log_file.write_text("NG: node_list count too small count=1 expected>=3", encoding="utf-8")
    findings = triage.classify({"round_summary": {"threshold_violations": []}}, [log_file])
    assert any(f.get("code") == "NODE_LIST_SHORTFALL" for f in findings)


def test_classify_monitor_log_missing_uses_summary_ports() -> None:
    triage = _load_triage_module()
    summary = {
        "monitor_requested": True,
        "monitor_logs_missing": True,
        "monitor_expected_ports": ["COM1", "COM2", "COM3"],
        "monitor_missing_ports": ["COM2", "COM3"],
        "monitor_logs_attached": ["test_logs/session/monitor_COM1.log"],
        "round_summary": {"threshold_violations": []},
    }
    findings = triage.classify(summary, [])
    missing = [f for f in findings if f.get("code") == "MONITOR_LOG_MISSING"]
    assert len(missing) == 1
    assert missing[0]["actual"]["missing_ports"] == ["COM2", "COM3"]
