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


def test_classify_log_signature(tmp_path: Path) -> None:
    triage = _load_triage_module()
    log_file = tmp_path / "node.log"
    log_file.write_text("... unknown_cmd ping_probe ...", encoding="utf-8")
    findings = triage.classify({"round_summary": {"threshold_violations": []}}, [log_file])
    assert any(f.get("code") == "FW_MISMATCH_UNKNOWN_CMD" for f in findings)


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
