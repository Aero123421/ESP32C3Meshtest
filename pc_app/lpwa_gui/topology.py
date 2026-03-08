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
    if kind in {"mesh_observed", "mesh_trace", "trace_obs"}:
        app_type = str(payload.get("app_type") or "").strip().lower()
        if app_type:
            kind = app_type
    if kind in {"lt_s", "long_text_start"}:
        return "long_text_start"
    if kind in {"lt_c", "long_text_chunk"}:
        return "long_text_chunk"
    if kind in {"lt_e", "long_text_end"}:
        return "long_text_end"
    if kind in {"r1k_s", "reliable_1k_start"}:
        return "reliable_1k_start"
    if kind in {"r1k_d", "reliable_1k_chunk"}:
        return "reliable_1k_chunk"
    if kind in {"r1k_e", "reliable_1k_end"}:
        return "reliable_1k_end"
    if kind in {"r1k_n", "reliable_1k_nack"}:
        return "reliable_1k_nack"
    if kind in {"r1k_r", "reliable_1k_repair"}:
        return "reliable_1k_repair"
    if kind in {"r1k_o", "reliable_1k_result"}:
        return "reliable_1k_result"
    if kind.startswith("image"):
        return kind
    if kind.startswith("long_text"):
        return kind
    if kind.startswith("reliable_1k"):
        return kind
    if not kind:
        return "unknown"
    return kind


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


def _normalize_mac(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip().replace("-", ":").upper()
    if not raw:
        return ""
    parts = [part for part in raw.split(":") if part]
    if len(parts) != 6:
        return ""
    normalized: list[str] = []
    for part in parts:
        if len(part) != 2:
            return ""
        for ch in part:
            if ch not in "0123456789ABCDEF":
                return ""
        normalized.append(part)
    return ":".join(normalized)


def _hop_note(payload: dict[str, Any], *, kind: str, observed_hops: int) -> str:
    request_hops = _to_int(payload.get("request_hops"), -1)
    reply_hops = _to_int(payload.get("reply_hops"), -1)
    parts: list[str] = []
    if request_hops >= 0:
        parts.append(f"req={request_hops}")
    if reply_hops >= 0:
        parts.append(f"rep={reply_hops}")
    elif kind in {"pong", "delivery_ack"} and observed_hops >= 0:
        parts.append(f"rep={observed_hops}")
    return " ".join(parts)


@dataclass
class TopologyEvent:
    ts_ms: int
    src: str
    dst: str
    observer: str
    via_node: str
    via_mac: str
    via: str
    kind: str
    hops: int
    retry_no: int
    bytes_size: int
    rssi: int | None
    msg_id: str
    e2e_id: str
    hop_note: str


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
class TopologyRelaySummary:
    parent: str
    child: str
    via: str
    count: int
    hops_max: int
    last_seen_ms: int


@dataclass
class TopologySnapshot:
    generated_ms: int
    nodes: list[str]
    edges: list[TopologyEdgeSummary]
    relay_links: list[TopologyRelaySummary]
    flow_events: list[TopologyEvent]
    event_count: int


class TopologyTracker:
    def __init__(self, *, max_events: int = 20000) -> None:
        self._events: deque[TopologyEvent] = deque(maxlen=max_events)
        self._mac_to_node: dict[str, str] = {}
        self._node_to_mac: dict[str, str] = {}

    def clear(self) -> None:
        self._events.clear()
        self._mac_to_node.clear()
        self._node_to_mac.clear()

    def update_node_records(self, entries: list[Any]) -> None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get("node_id") or entry.get("id") or "").strip()
            if not node_id:
                continue
            mac = _normalize_mac(entry.get("mac"))
            if not mac:
                continue
            self._node_to_mac[node_id] = mac
            self._mac_to_node[mac] = node_id

    def _resolve_via_node(self, payload: dict[str, Any]) -> tuple[str, str]:
        via_node = str(payload.get("via_node") or "").strip()
        via_mac = _normalize_mac(payload.get("via_mac"))
        if via_node and via_mac:
            self._node_to_mac[via_node] = via_mac
            self._mac_to_node[via_mac] = via_node
            return via_node, via_mac
        if via_node:
            known = self._node_to_mac.get(via_node, "")
            return via_node, known
        if via_mac:
            resolved = self._mac_to_node.get(via_mac, "")
            return resolved, via_mac
        return "", ""

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

        local_label = (local_node_id or "").strip()
        src = str(payload.get("src") or payload.get("from") or payload.get("origin") or "").strip()
        dst = str(payload.get("dst") or "").strip()
        observer = str(payload.get("observer") or "").strip()
        via_node, via_mac = self._resolve_via_node(payload)
        if direction == "tx":
            if src.lower() in {"pc", "local"}:
                src = local_label
            if not src:
                src = local_label
            if not src:
                return
            if not dst:
                dst = BROADCAST_NODE
            if not observer:
                observer = local_label
        else:
            if not src:
                return
            if not dst:
                dst = BROADCAST_NODE
            if not observer:
                observer = local_label

        hops = _to_int(payload.get("reply_hops"), _to_int(payload.get("hops"), 0))
        retry_no = _to_int(payload.get("retry_no"), 0)
        bytes_size = _estimate_bytes(payload)
        rssi = payload.get("rssi")
        rssi_value = _to_int(rssi, 10_000) if rssi is not None else 10_000
        if rssi_value == 10_000:
            rssi_value_opt: int | None = None
        else:
            rssi_value_opt = rssi_value

        msg_id = str(
            payload.get("msg_id")
            or payload.get("e2e_id")
            or payload.get("ping_id")
            or payload.get("text_id")
            or payload.get("image_id")
            or ""
        ).strip()
        e2e_id = str(payload.get("e2e_id") or "").strip()
        hop_note = _hop_note(payload, kind=kind, observed_hops=hops)
        self._events.append(
            TopologyEvent(
                ts_ms=now_ms,
                src=src,
                dst=dst,
                observer=observer,
                via_node=via_node,
                via_mac=via_mac,
                via=via,
                kind=kind,
                hops=hops,
                retry_no=retry_no,
                bytes_size=bytes_size,
                rssi=rssi_value_opt,
                msg_id=msg_id,
                e2e_id=e2e_id,
                hop_note=hop_note,
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
        relay_key_to_agg: dict[tuple[str, str, str], dict[str, Any]] = {}
        nodes: set[str] = set()
        event_count = 0
        flow_events: list[TopologyEvent] = []

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
            flow_events.append(ev)
            nodes.add(ev.src)
            if ev.dst != BROADCAST_NODE:
                nodes.add(ev.dst)
            if ev.observer:
                nodes.add(ev.observer)
            if ev.via_node:
                nodes.add(ev.via_node)

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

            parent = ""
            child = ""
            if ev.via_node and ev.observer and ev.via_node != ev.observer:
                parent = ev.via_node
                child = ev.observer
            elif ev.hops <= 1 and ev.src and ev.observer and ev.src != ev.observer:
                parent = ev.src
                child = ev.observer
            if parent and child:
                relay_key = (parent, child, ev.via)
                relay = relay_key_to_agg.get(relay_key)
                if relay is None:
                    relay = {
                        "count": 0,
                        "hops_max": 0,
                        "last_seen_ms": 0,
                    }
                    relay_key_to_agg[relay_key] = relay
                relay["count"] += 1
                if ev.hops > relay["hops_max"]:
                    relay["hops_max"] = ev.hops
                if ev.ts_ms >= relay["last_seen_ms"]:
                    relay["last_seen_ms"] = ev.ts_ms

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

        relay_links: list[TopologyRelaySummary] = []
        for (parent, child, via), agg in relay_key_to_agg.items():
            relay_links.append(
                TopologyRelaySummary(
                    parent=parent,
                    child=child,
                    via=via,
                    count=int(agg["count"]),
                    hops_max=int(agg["hops_max"]),
                    last_seen_ms=int(agg["last_seen_ms"]),
                )
            )

        edges.sort(key=lambda e: (e.last_seen_ms, e.count), reverse=True)
        relay_links.sort(key=lambda e: (e.last_seen_ms, e.count), reverse=True)
        flow_events.sort(key=lambda e: e.ts_ms, reverse=True)
        return TopologySnapshot(
            generated_ms=now_ms,
            nodes=sorted(nodes, key=lambda x: x.lower()),
            edges=edges,
            relay_links=relay_links,
            flow_events=flow_events[:240],
            event_count=event_count,
        )
