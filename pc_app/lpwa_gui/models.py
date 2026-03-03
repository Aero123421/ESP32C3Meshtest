from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping


def _now_ms() -> int:
    return int(time.time() * 1000)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit())):
            return int(stripped)
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _pick_node_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("node_id", "id", "src", "from"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


@dataclass
class NodeInfo:
    node_id: str
    rssi: int | None = None
    ping_ms: float | None = None
    last_seen_ms: int = 0
    last_message: str = ""


class NodeRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, NodeInfo] = {}

    def upsert_from_payload(self, payload: Mapping[str, Any]) -> NodeInfo | None:
        node_id = _pick_node_id(payload)
        if not node_id:
            return None

        node = self._nodes.get(node_id)
        if node is None:
            node = NodeInfo(node_id=node_id)
            self._nodes[node_id] = node

        rssi = _coerce_int(payload.get("rssi"))
        if rssi is not None:
            node.rssi = rssi

        latency = _coerce_float(payload.get("latency_ms"))
        if latency is not None:
            node.ping_ms = latency

        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            node.last_message = text.strip()

        explicit_seen = _coerce_int(payload.get("last_seen_ms"))
        node.last_seen_ms = explicit_seen if explicit_seen is not None else _now_ms()
        return node

    def update_from_list(self, entries: list[Any]) -> int:
        changed = 0
        for entry in entries:
            payload: Mapping[str, Any]
            if isinstance(entry, str):
                payload = {"node_id": entry}
            elif isinstance(entry, Mapping):
                payload = entry
            else:
                continue
            if self.upsert_from_payload(payload) is not None:
                changed += 1
        return changed

    def snapshot(self) -> list[NodeInfo]:
        return sorted(self._nodes.values(), key=lambda node: node.node_id.lower())

    def clear(self) -> None:
        self._nodes.clear()
