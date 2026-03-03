from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from lpwa_gui.protocol import decode_json_line, encode_json_line, make_image_messages
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
    check_ping_stats()
    print("self_check: OK")


if __name__ == "__main__":
    main()
