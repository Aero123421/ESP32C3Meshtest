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

    def expire_pending(self, seq: int) -> bool:
        return self._pending_sent_ts.pop(seq, None) is not None

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


@dataclass
class ReliableStats:
    sent_sessions: int = 0
    completed_sessions: int = 0
    failed_sessions: int = 0
    nacks_sent: int = 0
    repairs_sent: int = 0
    retry_packets: int = 0
    total_packets: int = 0
    _latencies_ms: list[float] = field(default_factory=list)
    fail_reasons: dict[str, int] = field(default_factory=dict)
    profile_usage: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.sent_sessions = 0
        self.completed_sessions = 0
        self.failed_sessions = 0
        self.nacks_sent = 0
        self.repairs_sent = 0
        self.retry_packets = 0
        self.total_packets = 0
        self._latencies_ms.clear()
        self.fail_reasons.clear()
        self.profile_usage.clear()

    def register_sent(self, *, profile_name: str, packet_count: int) -> None:
        self.sent_sessions += 1
        self.total_packets += max(0, int(packet_count))
        key = profile_name.strip() if profile_name else "unknown"
        self.profile_usage[key] = int(self.profile_usage.get(key, 0)) + 1

    def register_retry(self, retry_count: int) -> None:
        self.retry_packets += max(0, int(retry_count))

    def register_nack(self) -> None:
        self.nacks_sent += 1

    def register_repair(self, count: int = 1) -> None:
        self.repairs_sent += max(0, int(count))

    def register_success(self, *, latency_ms: float | None = None) -> None:
        self.completed_sessions += 1
        if latency_ms is not None:
            self._latencies_ms.append(max(0.0, float(latency_ms)))

    def register_failure(self, reason: str) -> None:
        self.failed_sessions += 1
        key = reason.strip() if reason else "unknown"
        self.fail_reasons[key] = int(self.fail_reasons.get(key, 0)) + 1

    def snapshot(self) -> dict[str, float | int | str]:
        total_done = self.completed_sessions + self.failed_sessions
        restore_rate = (float(self.completed_sessions) / float(total_done) * 100.0) if total_done else 0.0
        retry_rate = (float(self.retry_packets) / float(self.total_packets) * 100.0) if self.total_packets else 0.0
        top_reason = "none"
        if self.fail_reasons:
            top_reason = sorted(self.fail_reasons.items(), key=lambda item: item[1], reverse=True)[0][0]
        top_profile = "n/a"
        if self.profile_usage:
            top_profile = sorted(self.profile_usage.items(), key=lambda item: item[1], reverse=True)[0][0]
        return {
            "sent_sessions": self.sent_sessions,
            "completed_sessions": self.completed_sessions,
            "failed_sessions": self.failed_sessions,
            "restore_rate": restore_rate,
            "retry_rate": retry_rate,
            "nacks_sent": self.nacks_sent,
            "repairs_sent": self.repairs_sent,
            "p95_ms": _percentile(self._latencies_ms, 0.95),
            "avg_ms": (sum(self._latencies_ms) / len(self._latencies_ms)) if self._latencies_ms else 0.0,
            "top_reason": top_reason,
            "top_profile": top_profile,
        }
