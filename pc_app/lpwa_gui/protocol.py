from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Mapping

from .reliable_codec import (
    RELIABLE_PROFILES,
    decode_shards,
    encode_shards,
    get_profile,
    interleaved_indexes,
    missing_shard_indexes,
)

MAX_IMAGE_CHUNK_BYTES = 320
MAX_LONG_TEXT_CHUNK_BYTES = 32
PING_PROBE_BYTES = 1000
RELIABLE_1K_BYTES = 1000
RELIABLE_PROFILE_DEFAULT = 0


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


def make_routes_request(*, src: str = "pc") -> dict[str, Any]:
    return {
        "type": "routes_request",
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


def make_ping_probe_command(
    seq: int,
    dst: str | None = None,
    *,
    ping_id: str | None = None,
    src: str = "pc",
    via: str = "wifi",
    ttl: int | None = None,
    probe_bytes: int = PING_PROBE_BYTES,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cmd": "ping_probe",
        "type": "ping",
        "src": src,
        "via": via,
        "seq": int(seq),
        "ping_id": (ping_id or uuid.uuid4().hex[:8]).lower(),
        "probe_bytes": max(1, min(PING_PROBE_BYTES, int(probe_bytes))),
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


def make_reliable_1k_messages(
    text: str,
    dst: str,
    *,
    src: str = "pc",
    via: str = "wifi",
    ttl: int | None = None,
    profile_id: int = RELIABLE_PROFILE_DEFAULT,
    r1k_id: str | None = None,
    require_ack: bool = True,
    interleave: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if via != "wifi":
        raise ValueError("reliable_1k supports wifi only")
    if not dst:
        raise ValueError("reliable_1k requires directed destination")

    profile = get_profile(profile_id)
    raw = text.encode("utf-8")
    if len(raw) > RELIABLE_1K_BYTES:
        raise ValueError(f"reliable_1k max payload is {RELIABLE_1K_BYTES} bytes")

    session_id = (r1k_id or uuid.uuid4().hex[:12]).lower()
    shards = encode_shards(raw, profile)
    order = interleaved_indexes(profile.total_shards) if interleave else list(range(profile.total_shards))
    crc32_hex = f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
    sha_hex = hashlib.sha256(raw).hexdigest()

    start: dict[str, Any] = {
        "type": "reliable_1k_start",
        "src": src,
        "via": via,
        "dst": dst,
        "r1k_id": session_id,
        "version": 1,
        "profile_id": profile.profile_id,
        "profile_name": profile.name,
        "data_shards": profile.data_shards,
        "parity_shards": profile.parity_shards,
        "shard_size": profile.shard_size,
        "size": len(raw),
        "crc32": crc32_hex,
        "sha256": sha_hex,
        "order_stride": 7 if interleave else 1,
        "ts_ms": now_ms(),
    }
    if ttl is not None:
        start["ttl"] = max(1, min(255, int(ttl)))
    if require_ack:
        start["need_ack"] = True
        start["e2e_id"] = f"{session_id}:s"

    packets: list[dict[str, Any]] = [start]
    for wire_idx in order:
        chunk = shards[wire_idx]
        packet: dict[str, Any] = {
            "type": "reliable_1k_chunk",
            "src": src,
            "via": via,
            "dst": dst,
            "r1k_id": session_id,
            "index": wire_idx,
            "data_b64": base64.b64encode(chunk).decode("ascii"),
            "ts_ms": now_ms(),
        }
        if ttl is not None:
            packet["ttl"] = max(1, min(255, int(ttl)))
        if require_ack:
            packet["need_ack"] = True
            packet["e2e_id"] = f"{session_id}:c:{wire_idx}"
        packets.append(packet)

    end: dict[str, Any] = {
        "type": "reliable_1k_end",
        "src": src,
        "via": via,
        "dst": dst,
        "r1k_id": session_id,
        "size": len(raw),
        "data_shards": profile.data_shards,
        "parity_shards": profile.parity_shards,
        "shard_size": profile.shard_size,
        "crc32": crc32_hex,
        "sha256": sha_hex,
        "ts_ms": now_ms(),
    }
    if ttl is not None:
        end["ttl"] = max(1, min(255, int(ttl)))
    if require_ack:
        end["need_ack"] = True
        end["e2e_id"] = f"{session_id}:e"
    packets.append(end)

    meta = {
        "r1k_id": session_id,
        "profile_id": profile.profile_id,
        "profile_name": profile.name,
        "size": len(raw),
        "total_shards": profile.total_shards,
        "data_shards": profile.data_shards,
        "parity_shards": profile.parity_shards,
        "shard_size": profile.shard_size,
        "crc32": crc32_hex,
        "sha256": sha_hex,
        "shards_b64": [base64.b64encode(chunk).decode("ascii") for chunk in shards],
        "order": order,
    }
    return packets, meta


def make_reliable_1k_nack_message(
    *,
    r1k_id: str,
    dst: str,
    missing_indexes: list[int],
    src: str = "pc",
    ttl: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "reliable_1k_nack",
        "src": src,
        "via": "wifi",
        "dst": dst,
        "r1k_id": r1k_id,
        "missing": [int(v) for v in sorted(set(missing_indexes)) if int(v) >= 0],
        "ts_ms": now_ms(),
    }
    if ttl is not None:
        payload["ttl"] = max(1, min(255, int(ttl)))
    payload["need_ack"] = True
    payload["e2e_id"] = f"{r1k_id}:n:{uuid.uuid4().hex[:6]}"
    return payload


def make_reliable_1k_repair_message(
    *,
    r1k_id: str,
    dst: str,
    index: int,
    shard_b64: str,
    src: str = "pc",
    ttl: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "reliable_1k_repair",
        "src": src,
        "via": "wifi",
        "dst": dst,
        "r1k_id": r1k_id,
        "index": int(index),
        "data_b64": shard_b64,
        "ts_ms": now_ms(),
        "need_ack": True,
        "e2e_id": f"{r1k_id}:r:{int(index)}:{uuid.uuid4().hex[:4]}",
    }
    if ttl is not None:
        payload["ttl"] = max(1, min(255, int(ttl)))
    return payload


def decode_reliable_1k_from_shards(
    *,
    shard_map_b64: dict[int, str],
    profile_id: int,
    original_size: int,
) -> bytes | None:
    profile = get_profile(profile_id)
    shard_map: dict[int, bytes] = {}
    for idx, b64 in shard_map_b64.items():
        try:
            shard = base64.b64decode(b64, validate=True)
        except Exception:
            continue
        if len(shard) != profile.shard_size:
            continue
        shard_map[int(idx)] = shard
    return decode_shards(shard_map, profile, original_size=original_size)


def missing_reliable_shards(*, present_indexes: list[int], profile_id: int) -> list[int]:
    profile = get_profile(profile_id)
    return missing_shard_indexes(profile.total_shards, present_indexes)
