from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


BROADCAST_NODE = "*"


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw and (raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit())):
            return int(raw)
    return default


def _normalize_type(payload: dict[str, Any]) -> str:
    kind = str(payload.get("type") or payload.get("event") or "").strip().lower()
    if kind == "mesh_observed":
        kind = str(payload.get("app_type") or "").strip().lower()
    if kind.startswith("long_text"):
        return "long_text"
    if kind.startswith("image"):
        return "image"
    if kind in {"chat", "ping", "pong", "delivery_ack"}:
        return kind
    return "other"


def _estimate_bytes(payload: dict[str, Any]) -> int:
    if isinstance(payload.get("size"), int) and int(payload["size"]) > 0:
        return int(payload["size"])
    text = payload.get("text")
    if isinstance(text, str):
        return len(text.encode("utf-8", errors="replace"))
    data_b64 = payload.get("data_b64")
    if isinstance(data_b64, str) and data_b64:
        return max(0, (len(data_b64) * 3) // 4)
    return 0


@dataclass
class TopologyEvent:
    ts_ms: int
    src: str
    dst: str
    via: str
    kind: str
    hops: int
    retry_no: int
    bytes_size: int
    rssi: int | None
    msg_id: str


@dataclass
class TopologyEdgeSummary:
    src: str
    dst: str
    via: str
    kind: str
    count: int
    bytes_size: int
    retry_total: int
    hops_max: int
    rssi_avg: float | None
    last_seen_ms: int
    last_msg_id: str


@dataclass
class TopologySnapshot:
    generated_ms: int
    nodes: list[str]
    edges: list[TopologyEdgeSummary]
    event_count: int


class TopologyTracker:
    def __init__(self, *, max_events: int = 20000) -> None:
        self._events: deque[TopologyEvent] = deque(maxlen=max_events)

    def clear(self) -> None:
        self._events.clear()

    def ingest(
        self,
        payload: dict[str, Any],
        *,
        direction: str,
        local_node_id: str | None,
        now_ms: int,
    ) -> None:
        via = str(payload.get("via") or "wifi").strip().lower() or "wifi"
        kind = _normalize_type(payload)
        if kind == "other":
            return

        src = str(payload.get("src") or "").strip()
        dst = str(payload.get("dst") or "").strip()
        if direction == "tx":
            if not src:
                src = local_node_id or "local"
            if not dst:
                dst = BROADCAST_NODE
        else:
            if not src:
                return
            if not dst:
                dst = BROADCAST_NODE

        hops = _to_int(payload.get("hops"), 0)
        retry_no = _to_int(payload.get("retry_no"), 0)
        bytes_size = _estimate_bytes(payload)
        rssi = payload.get("rssi")
        rssi_value = _to_int(rssi, 10_000) if rssi is not None else 10_000
        if rssi_value == 10_000:
            rssi_value_opt: int | None = None
        else:
            rssi_value_opt = rssi_value

        msg_id = str(payload.get("msg_id") or payload.get("e2e_id") or "").strip()
        self._events.append(
            TopologyEvent(
                ts_ms=now_ms,
                src=src,
                dst=dst,
                via=via,
                kind=kind,
                hops=hops,
                retry_no=retry_no,
                bytes_size=bytes_size,
                rssi=rssi_value_opt,
                msg_id=msg_id,
            )
        )

    def snapshot(
        self,
        *,
        now_ms: int,
        window_s: int,
        via_filter: str,
        kind_filter: str,
        include_broadcast: bool,
    ) -> TopologySnapshot:
        cutoff = now_ms - max(1, int(window_s)) * 1000
        key_to_agg: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        nodes: set[str] = set()
        event_count = 0

        for ev in self._events:
            if ev.ts_ms < cutoff:
                continue
            if via_filter != "all" and ev.via != via_filter:
                continue
            if kind_filter != "all" and ev.kind != kind_filter:
                continue
            if not include_broadcast and ev.dst == BROADCAST_NODE:
                continue

            event_count += 1
            nodes.add(ev.src)
            if ev.dst != BROADCAST_NODE:
                nodes.add(ev.dst)

            key = (ev.src, ev.dst, ev.via, ev.kind)
            agg = key_to_agg.get(key)
            if agg is None:
                agg = {
                    "count": 0,
                    "bytes_size": 0,
                    "retry_total": 0,
                    "hops_max": 0,
                    "rssi_sum": 0,
                    "rssi_count": 0,
                    "last_seen_ms": 0,
                    "last_msg_id": "",
                }
                key_to_agg[key] = agg

            agg["count"] += 1
            agg["bytes_size"] += max(0, ev.bytes_size)
            agg["retry_total"] += max(0, ev.retry_no)
            if ev.hops > agg["hops_max"]:
                agg["hops_max"] = ev.hops
            if ev.rssi is not None:
                agg["rssi_sum"] += ev.rssi
                agg["rssi_count"] += 1
            if ev.ts_ms >= agg["last_seen_ms"]:
                agg["last_seen_ms"] = ev.ts_ms
                agg["last_msg_id"] = ev.msg_id

        edges: list[TopologyEdgeSummary] = []
        for (src, dst, via, kind), agg in key_to_agg.items():
            if agg["rssi_count"] > 0:
                rssi_avg = float(agg["rssi_sum"]) / float(agg["rssi_count"])
            else:
                rssi_avg = None
            edges.append(
                TopologyEdgeSummary(
                    src=src,
                    dst=dst,
                    via=via,
                    kind=kind,
                    count=int(agg["count"]),
                    bytes_size=int(agg["bytes_size"]),
                    retry_total=int(agg["retry_total"]),
                    hops_max=int(agg["hops_max"]),
                    rssi_avg=rssi_avg,
                    last_seen_ms=int(agg["last_seen_ms"]),
                    last_msg_id=str(agg["last_msg_id"]),
                )
            )

        edges.sort(key=lambda e: (e.last_seen_ms, e.count), reverse=True)
        return TopologySnapshot(
            generated_ms=now_ms,
            nodes=sorted(nodes, key=lambda x: x.lower()),
            edges=edges,
            event_count=event_count,
        )
