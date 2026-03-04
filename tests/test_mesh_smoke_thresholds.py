from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def _load_mesh_smoke_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "tools" / "mesh_smoke_test.py"
    spec = importlib.util.spec_from_file_location("mesh_smoke_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_threshold_file_extended_keys(tmp_path: Path) -> None:
    mod = _load_mesh_smoke_module()
    payload = {
        "min_success_rate": 0.95,
        "max_latency_ms": 2200,
        "max_latency_p95_ms": 1800,
        "max_retry_rate": 0.25,
        "max_rx_queue_drop_ratio": 0.01,
        "require_min_hops": 1,
        "max_consecutive_failures": 2,
        "min_probe_hash_ok_rate": 0.95,
        "min_route_hit_rate": 0.55,
        "max_route_fallback_ratio": 0.60,
    }
    path = tmp_path / "th.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    parsed = mod.parse_threshold_file(path)
    assert parsed["max_latency_p95_ms"] == 1800
    assert parsed["max_consecutive_failures"] == 2
    assert parsed["min_route_hit_rate"] == 0.55


def test_parse_threshold_file_unknown_key(tmp_path: Path) -> None:
    mod = _load_mesh_smoke_module()
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"unknown_key": 1}), encoding="utf-8")
    try:
        mod.parse_threshold_file(path)
    except ValueError as exc:
        assert "unknown threshold keys" in str(exc)
    else:
        raise AssertionError("ValueError expected for unknown threshold key")


def test_combine_thresholds_cli_and_file() -> None:
    mod = _load_mesh_smoke_module()
    combined = mod.combine_thresholds(
        cli_require_min_hops=1,
        cli_max_latency_ms=2000,
        cli_max_retry_rate=0.2,
        from_file={
            "min_success_rate": 0.95,
            "max_latency_ms": 2200,
            "max_latency_p95_ms": 1800,
            "max_retry_rate": 0.25,
            "max_rx_queue_drop_ratio": 0.01,
            "require_min_hops": 0,
            "max_consecutive_failures": 2,
            "min_probe_hash_ok_rate": 0.95,
            "min_route_hit_rate": 0.55,
            "max_route_fallback_ratio": 0.60,
        },
    )
    assert combined["max_latency_ms"] == 2000
    assert combined["max_retry_rate"] == 0.2
    assert combined["require_min_hops"] == 1
    assert combined["max_latency_p95_ms"] == 1800
