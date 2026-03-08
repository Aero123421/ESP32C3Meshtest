from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_topology_module():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "pc_app" / "lpwa_gui" / "topology.py"
    spec = importlib.util.spec_from_file_location("lpwa_gui_topology", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_topology_tracker_prefers_reply_hops_for_pong() -> None:
    topology = _load_topology_module()
    tracker = topology.TopologyTracker()
    tracker.ingest(
        {
            "type": "pong",
            "src": "0x005447FE",
            "dst": "0x00F01CEE",
            "request_hops": 3,
            "reply_hops": 2,
            "hops": 9,
            "msg_id": "123",
        },
        direction="rx",
        local_node_id="0x00F01CEE",
        now_ms=1000,
    )

    snapshot = tracker.snapshot(
        now_ms=1100,
        window_s=30,
        via_filter="all",
        kind_filter="all",
        include_broadcast=True,
    )
    assert len(snapshot.flow_events) == 1
    event = snapshot.flow_events[0]
    assert event.hops == 2
    assert event.hop_note == "req=3 rep=2"


def test_topology_tracker_builds_reply_note_from_observed_hops() -> None:
    topology = _load_topology_module()
    tracker = topology.TopologyTracker()
    tracker.ingest(
        {
            "type": "delivery_ack",
            "src": "0x005447FE",
            "dst": "0x00F01CEE",
            "hops": 1,
            "e2e_id": "ack-1",
        },
        direction="rx",
        local_node_id="0x00F01CEE",
        now_ms=2000,
    )

    snapshot = tracker.snapshot(
        now_ms=2100,
        window_s=30,
        via_filter="all",
        kind_filter="all",
        include_broadcast=True,
    )
    assert len(snapshot.flow_events) == 1
    event = snapshot.flow_events[0]
    assert event.hops == 1
    assert event.hop_note == "rep=1"
