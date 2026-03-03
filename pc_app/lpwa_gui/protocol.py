from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

MAX_IMAGE_CHUNK_BYTES = 320


class ProtocolError(Exception):
    """JSON Lines protocol error."""


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_json_line(payload: Mapping[str, Any]) -> bytes:
    try:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"JSON encode failed: {exc}") from exc
    return (line + "\n").encode("utf-8")


def decode_json_line(line: str) -> dict[str, Any]:
    stripped = line.strip()
    if not stripped:
        raise ProtocolError("empty line")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("JSON payload must be object")
    return payload


def make_chat_message(
    text: str,
    dst: str | None = None,
    *,
    src: str = "pc",
    via: str = "wifi",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "chat",
        "src": src,
        "via": via,
        "text": text,
        "ts_ms": now_ms(),
    }
    if dst:
        payload["dst"] = dst
    return payload


def make_nodes_request(*, src: str = "pc") -> dict[str, Any]:
    return {
        "type": "nodes_request",
        "src": src,
        "ts_ms": now_ms(),
    }


def make_ping_message(
    seq: int,
    dst: str | None = None,
    *,
    ping_id: str | None = None,
    src: str = "pc",
    via: str = "wifi",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "ping",
        "src": src,
        "via": via,
        "seq": seq,
        "ping_id": ping_id or uuid.uuid4().hex[:8],
        "ts_ms": now_ms(),
    }
    if dst:
        payload["dst"] = dst
    return payload


def make_image_messages(
    path: Path,
    dst: str | None = None,
    *,
    src: str = "pc",
    via: str = "wifi",
    chunk_size: int = MAX_IMAGE_CHUNK_BYTES,
) -> list[dict[str, Any]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if not path.exists():
        raise FileNotFoundError(str(path))

    raw = path.read_bytes()
    image_id = uuid.uuid4().hex
    ts = now_ms()
    total_chunks = 0 if len(raw) == 0 else ((len(raw) - 1) // chunk_size) + 1

    start: dict[str, Any] = {
        "type": "image_start",
        "src": src,
        "via": via,
        "image_id": image_id,
        "name": path.name,
        "size": len(raw),
        "chunks": total_chunks,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "ts_ms": ts,
    }
    if dst:
        start["dst"] = dst

    messages: list[dict[str, Any]] = [start]
    for idx, offset in enumerate(range(0, len(raw), chunk_size)):
        chunk = raw[offset : offset + chunk_size]
        packet: dict[str, Any] = {
            "type": "image_chunk",
            "src": src,
            "via": via,
            "image_id": image_id,
            "index": idx,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
            "ts_ms": now_ms(),
        }
        if dst:
            packet["dst"] = dst
        messages.append(packet)

    end: dict[str, Any] = {
        "type": "image_end",
        "src": src,
        "via": via,
        "image_id": image_id,
        "ts_ms": now_ms(),
    }
    if dst:
        end["dst"] = dst
    messages.append(end)
    return messages
