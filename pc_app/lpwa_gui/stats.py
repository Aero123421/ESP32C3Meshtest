from __future__ import annotations

import time
from dataclasses import dataclass, field


def _now_ms() -> int:
    return int(time.time() * 1000)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    blend = rank - low
    return ordered[low] * (1.0 - blend) + ordered[high] * blend


@dataclass
class PingStats:
    sent_count: int = 0
    recv_count: int = 0
    _pending_sent_ts: dict[int, int] = field(default_factory=dict)
    _latencies_ms: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.sent_count = 0
        self.recv_count = 0
        self._pending_sent_ts.clear()
        self._latencies_ms.clear()

    def register_sent(self, seq: int, sent_ts_ms: int | None = None) -> None:
        self.sent_count += 1
        self._pending_sent_ts[seq] = sent_ts_ms if sent_ts_ms is not None else _now_ms()

    def register_received(
        self,
        seq: int,
        *,
        recv_ts_ms: int | None = None,
        latency_ms: float | None = None,
    ) -> float | None:
        if seq in self._pending_sent_ts or latency_ms is not None:
            self.recv_count += 1

        measured: float | None = None
        if latency_ms is not None:
            measured = max(0.0, float(latency_ms))
        else:
            sent_ts = self._pending_sent_ts.get(seq)
            if sent_ts is not None:
                now = recv_ts_ms if recv_ts_ms is not None else _now_ms()
                measured = max(0.0, float(now - sent_ts))

        if measured is not None:
            self._latencies_ms.append(measured)

        self._pending_sent_ts.pop(seq, None)
        return measured

    def snapshot(self) -> dict[str, float | int]:
        lost = max(0, self.sent_count - self.recv_count)
        pdr = (float(self.recv_count) / float(self.sent_count) * 100.0) if self.sent_count else 0.0
        avg = (sum(self._latencies_ms) / len(self._latencies_ms)) if self._latencies_ms else 0.0
        return {
            "sent": self.sent_count,
            "received": self.recv_count,
            "lost": lost,
            "pdr": pdr,
            "avg_ms": avg,
            "min_ms": min(self._latencies_ms) if self._latencies_ms else 0.0,
            "max_ms": max(self._latencies_ms) if self._latencies_ms else 0.0,
            "p50_ms": _percentile(self._latencies_ms, 0.50),
            "p95_ms": _percentile(self._latencies_ms, 0.95),
        }
