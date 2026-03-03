from __future__ import annotations

import base64
import hashlib
import tempfile
from pathlib import Path

from lpwa_gui.protocol import (
    MAX_LONG_TEXT_CHUNK_BYTES,
    decode_json_line,
    encode_json_line,
    make_chat_message,
    make_image_messages,
    make_long_text_messages,
)
from lpwa_gui.stats import PingStats


def check_json_roundtrip() -> None:
    payload = {"type": "chat", "text": "hello", "seq": 3}
    encoded = encode_json_line(payload)
    decoded = decode_json_line(encoded.decode("utf-8"))
    assert decoded == payload, "JSON Lines roundtrip failed"


def check_image_chunking() -> None:
    content = bytes(range(256)) * 4
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sample.bin"
        path.write_bytes(content)
        messages = make_image_messages(path=path, dst="node-1", chunk_size=100)
        assert messages[0]["type"] == "image_start", "missing image_start"
        assert messages[-1]["type"] == "image_end", "missing image_end"

        chunks = [m for m in messages if m.get("type") == "image_chunk"]
        restored = b"".join(base64.b64decode(m["data_b64"]) for m in chunks)
        assert restored == content, "image chunk reconstruction failed"


def check_chat_e2e_fields() -> None:
    payload = make_chat_message(
        text="hello",
        dst="0x00112233",
        via="wifi",
        require_ack=True,
        e2e_id="chat-fixed-id",
        retry_no=2,
    )
    assert payload.get("need_ack") is True, "need_ack missing in chat payload"
    assert payload.get("e2e_id") == "chat-fixed-id", "e2e_id mismatch in chat payload"
    assert payload.get("retry_no") == 2, "retry_no mismatch in chat payload"


def check_image_e2e_fields() -> None:
    content = b"mesh-image-payload" * 16
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sample.bin"
        path.write_bytes(content)
        messages = make_image_messages(path=path, dst="0x0099AABB", require_ack=True)
        for payload in messages:
            assert payload.get("need_ack") is True, "need_ack missing in image payload"
            assert isinstance(payload.get("e2e_id"), str) and payload["e2e_id"], "e2e_id missing in image payload"


def check_retry_id_stability() -> None:
    first = make_chat_message(text="retry", dst="0x0099AABB", require_ack=True, e2e_id="stable-id", retry_no=0)
    retry = dict(first)
    retry["retry_no"] = 1
    assert first.get("e2e_id") == retry.get("e2e_id") == "stable-id", "retry e2e_id must be stable"


def check_long_text_chunking() -> None:
    text = "長文テキスト検証-" + ("ABC123" * 240)
    packets = make_long_text_messages(
        text=text,
        dst="0x0099AABB",
        require_ack=True,
        chunk_size=MAX_LONG_TEXT_CHUNK_BYTES,
    )
    assert packets[0]["type"] == "long_text_start", "missing long_text_start"
    assert packets[-1]["type"] == "long_text_end", "missing long_text_end"
    chunks = [p for p in packets if p.get("type") == "long_text_chunk"]
    restored = b"".join(base64.b64decode(p["data_b64"]) for p in chunks).decode("utf-8")
    assert restored == text, "long text reconstruction failed"
    for p in packets:
        assert p.get("need_ack") is True, "need_ack missing in long text payload"
        assert isinstance(p.get("e2e_id"), str) and p["e2e_id"], "e2e_id missing in long text payload"
    end = packets[-1]
    assert end.get("encoding") == "utf-8", "long_text_end encoding missing"
    assert int(end.get("size") or -1) == len(text.encode("utf-8")), "long_text_end size mismatch"
    assert int(end.get("chunks") or -1) == len(chunks), "long_text_end chunks mismatch"
    assert end.get("sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest(), "long_text_end hash mismatch"


def check_ping_stats() -> None:
    stats = PingStats()
    stats.register_sent(1, sent_ts_ms=1000)
    stats.register_sent(2, sent_ts_ms=2000)
    stats.register_received(1, recv_ts_ms=1105)
    stats.register_received(2, latency_ms=80.0)
    snap = stats.snapshot()
    assert snap["sent"] == 2, "sent count mismatch"
    assert snap["received"] == 2, "received count mismatch"
    assert abs(float(snap["pdr"]) - 100.0) < 1e-6, "PDR mismatch"
    assert float(snap["avg_ms"]) > 0.0, "latency average must be > 0"


def main() -> None:
    check_json_roundtrip()
    check_image_chunking()
    check_chat_e2e_fields()
    check_image_e2e_fields()
    check_retry_id_stability()
    check_long_text_chunking()
    check_ping_stats()
    print("self_check: OK")


if __name__ == "__main__":
    main()
