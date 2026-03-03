from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

MAX_IMAGE_CHUNK_BYTES = 320
MAX_LONG_TEXT_CHUNK_BYTES = 32


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
    ttl: int | None = None,
    require_ack: bool = False,
    e2e_id: str | None = None,
    retry_no: int | None = None,
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
    if ttl is not None:
        payload["ttl"] = max(1, min(255, int(ttl)))
    if require_ack and dst and via == "wifi":
        payload["need_ack"] = True
        payload["e2e_id"] = e2e_id or uuid.uuid4().hex
    if retry_no is not None and int(retry_no) > 0:
        payload["retry_no"] = int(retry_no)
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
    ttl: int | None = None,
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
    if ttl is not None:
        payload["ttl"] = max(1, min(255, int(ttl)))
    return payload


def make_image_messages(
    path: Path,
    dst: str | None = None,
    *,
    src: str = "pc",
    via: str = "wifi",
    chunk_size: int = MAX_IMAGE_CHUNK_BYTES,
    ttl: int | None = None,
    require_ack: bool = False,
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
    if ttl is not None:
        start["ttl"] = max(1, min(255, int(ttl)))
    if require_ack and dst and via == "wifi":
        start["need_ack"] = True
        start["e2e_id"] = f"{image_id}:s"

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
        if ttl is not None:
            packet["ttl"] = max(1, min(255, int(ttl)))
        if require_ack and dst and via == "wifi":
            packet["need_ack"] = True
            packet["e2e_id"] = f"{image_id}:c:{idx}"
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
    if ttl is not None:
        end["ttl"] = max(1, min(255, int(ttl)))
    if require_ack and dst and via == "wifi":
        end["need_ack"] = True
        end["e2e_id"] = f"{image_id}:e"
    messages.append(end)
    return messages


def make_long_text_messages(
    text: str,
    dst: str | None = None,
    *,
    src: str = "pc",
    via: str = "wifi",
    chunk_size: int = MAX_LONG_TEXT_CHUNK_BYTES,
    ttl: int | None = None,
    require_ack: bool = False,
) -> list[dict[str, Any]]:
    if via != "wifi":
        raise ValueError("long text supports wifi only")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if chunk_size > MAX_LONG_TEXT_CHUNK_BYTES:
        raise ValueError(f"chunk_size must be <= {MAX_LONG_TEXT_CHUNK_BYTES}")

    raw = text.encode("utf-8")
    text_id = uuid.uuid4().hex[:12]
    ts = now_ms()
    total_chunks = 0 if len(raw) == 0 else ((len(raw) - 1) // chunk_size) + 1
    text_hash = hashlib.sha256(raw).hexdigest()

    start: dict[str, Any] = {
        "type": "long_text_start",
        "src": src,
        "via": via,
        "text_id": text_id,
        "encoding": "utf-8",
        "size": len(raw),
        "chunks": total_chunks,
        "ts_ms": ts,
    }
    if dst:
        start["dst"] = dst
    if ttl is not None:
        start["ttl"] = max(1, min(255, int(ttl)))
    if require_ack and dst:
        start["need_ack"] = True
        start["e2e_id"] = f"{text_id}:s"

    messages: list[dict[str, Any]] = [start]
    for idx, offset in enumerate(range(0, len(raw), chunk_size)):
        chunk = raw[offset : offset + chunk_size]
        packet: dict[str, Any] = {
            "type": "long_text_chunk",
            "src": src,
            "via": via,
            "text_id": text_id,
            "index": idx,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
            "ts_ms": now_ms(),
        }
        if dst:
            packet["dst"] = dst
        if ttl is not None:
            packet["ttl"] = max(1, min(255, int(ttl)))
        if require_ack and dst:
            packet["need_ack"] = True
            packet["e2e_id"] = f"{text_id}:c:{idx}"
        messages.append(packet)

    end: dict[str, Any] = {
        "type": "long_text_end",
        "src": src,
        "via": via,
        "text_id": text_id,
        "encoding": "utf-8",
        "size": len(raw),
        "chunks": total_chunks,
        "sha256": text_hash,
        "ts_ms": now_ms(),
    }
    if dst:
        end["dst"] = dst
    if ttl is not None:
        end["ttl"] = max(1, min(255, int(ttl)))
    if require_ack and dst:
        end["need_ack"] = True
        end["e2e_id"] = f"{text_id}:e"
    messages.append(end)
    return messages
