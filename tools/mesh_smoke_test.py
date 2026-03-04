#!/usr/bin/env python
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import serial


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PC_APP_DIR = PROJECT_ROOT / "pc_app"
import sys

if str(PC_APP_DIR) not in sys.path:
    sys.path.insert(0, str(PC_APP_DIR))

from lpwa_gui.protocol import (
    RELIABLE_1K_BYTES,
    decode_reliable_1k_from_shards,
    make_reliable_1k_messages,
)


@dataclass
class PortState:
    port: str
    ser: serial.Serial
    lines: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    raw_lines: queue.Queue[str] = field(default_factory=queue.Queue)
    node_id: str | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def wait_for_event(
    states: list[PortState],
    history: list[dict[str, Any]],
    timeout_s: float,
    matcher: Callable[[dict[str, Any]], bool],
    start_index: int | None = None,
) -> dict[str, Any] | None:
    begin_idx = len(history) if start_index is None else max(0, int(start_index))
    for ev in history[begin_idx:]:
        if matcher(ev):
            return ev
    end = time.time() + timeout_s
    while time.time() < end:
        before = len(history)
        history.extend(drain_available(states))
        scan_begin = before if before > begin_idx else begin_idx
        for ev in history[scan_begin:]:
            if matcher(ev):
                return ev
        time.sleep(0.02)
    return None


def extract_mesh_counters(payload: dict[str, Any]) -> dict[str, int] | None:
    mesh_obj = payload.get("mesh")
    if not isinstance(mesh_obj, dict):
        return None
    counters: dict[str, int] = {}
    for key, raw_value in mesh_obj.items():
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, (int, float)):
            counters[str(key)] = int(raw_value)
    return counters


def request_stats_mesh_counters(
    *,
    target: PortState,
    states: list[PortState],
    history: list[dict[str, Any]],
    timeout_s: float,
) -> dict[str, int] | None:
    history.extend(drain_available(states))
    start_index = len(history)
    send_json(target, {"cmd": "get_stats"})
    ev = wait_for_event(
        states,
        history,
        timeout_s=timeout_s,
        matcher=lambda e: e.get("_port") == target.port and e.get("type") == "stats",
        start_index=start_index,
    )
    if ev is None:
        return None
    return extract_mesh_counters(ev)


def compute_counter_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = sorted(set(before.keys()) | set(after.keys()))
    out: dict[str, int] = {}
    for key in keys:
        out[key] = int(after.get(key, 0)) - int(before.get(key, 0))
    return out


def calc_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    if numerator <= 0:
        return 0.0
    return float(numerator) / float(denominator)


def parse_threshold_file(path: Path | None) -> dict[str, float | int | None]:
    allowed_keys = {
        "min_success_rate",
        "max_latency_ms",
        "max_retry_rate",
        "max_rx_queue_drop_ratio",
        "require_min_hops",
    }
    out: dict[str, float | int | None] = {
        "min_success_rate": None,
        "max_latency_ms": None,
        "max_retry_rate": None,
        "max_rx_queue_drop_ratio": None,
        "require_min_hops": None,
    }
    if path is None:
        return out
    if not path.exists():
        raise ValueError(f"threshold file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"threshold file read error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"threshold JSON parse error: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("threshold file must be a JSON object")
    unknown_keys = sorted(str(k) for k in payload.keys() if str(k) not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"unknown threshold keys: {unknown_keys}")

    if "min_success_rate" in payload and payload["min_success_rate"] is not None:
        v = to_float(payload["min_success_rate"], -1.0)
        if not math.isfinite(v):
            raise ValueError("min_success_rate must be a finite number")
        if v < 0.0 or v > 1.0:
            raise ValueError("min_success_rate must be in 0.0..1.0")
        out["min_success_rate"] = v
    if "max_latency_ms" in payload and payload["max_latency_ms"] is not None:
        v = to_float(payload["max_latency_ms"], -1.0)
        if not math.isfinite(v):
            raise ValueError("max_latency_ms must be a finite number")
        if v <= 0.0:
            raise ValueError("max_latency_ms must be > 0")
        out["max_latency_ms"] = v
    if "max_retry_rate" in payload and payload["max_retry_rate"] is not None:
        v = to_float(payload["max_retry_rate"], -1.0)
        if not math.isfinite(v):
            raise ValueError("max_retry_rate must be a finite number")
        if v < 0.0 or v > 1.0:
            raise ValueError("max_retry_rate must be in 0.0..1.0")
        out["max_retry_rate"] = v
    if "max_rx_queue_drop_ratio" in payload and payload["max_rx_queue_drop_ratio"] is not None:
        v = to_float(payload["max_rx_queue_drop_ratio"], -1.0)
        if not math.isfinite(v):
            raise ValueError("max_rx_queue_drop_ratio must be a finite number")
        if v < 0.0 or v > 1.0:
            raise ValueError("max_rx_queue_drop_ratio must be in 0.0..1.0")
        out["max_rx_queue_drop_ratio"] = v
    if "require_min_hops" in payload and payload["require_min_hops"] is not None:
        v = to_int(payload["require_min_hops"], -1)
        if v < 0:
            raise ValueError("require_min_hops must be >= 0")
        out["require_min_hops"] = v
    return out


def combine_optional_max(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def combine_thresholds(
    *,
    cli_require_min_hops: int,
    cli_max_latency_ms: int,
    cli_max_retry_rate: float,
    from_file: dict[str, float | int | None],
) -> dict[str, float | int | None]:
    file_min_success = from_file.get("min_success_rate")
    file_max_latency = from_file.get("max_latency_ms")
    file_max_retry = from_file.get("max_retry_rate")
    file_max_drop = from_file.get("max_rx_queue_drop_ratio")
    file_min_hops = to_int(from_file.get("require_min_hops"), 0) if from_file.get("require_min_hops") is not None else 0

    cli_latency = float(cli_max_latency_ms) if cli_max_latency_ms > 0 else None
    cli_retry = float(cli_max_retry_rate) if cli_max_retry_rate >= 0 else None

    return {
        "min_success_rate": file_min_success,
        "max_latency_ms": combine_optional_max(cli_latency, file_max_latency if isinstance(file_max_latency, (int, float)) else None),
        "max_retry_rate": combine_optional_max(cli_retry, file_max_retry if isinstance(file_max_retry, (int, float)) else None),
        "max_rx_queue_drop_ratio": file_max_drop if isinstance(file_max_drop, (int, float)) else None,
        "require_min_hops": max(0, int(cli_require_min_hops), int(file_min_hops)),
    }


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        f.write("\n")


def write_summary_json(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def reader_loop(state: PortState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            raw = state.ser.readline()
        except Exception as exc:
            state.lines.put({"event": "reader_error", "type": "reader_error", "detail": str(exc)})
            break
        if not raw:
            continue
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        state.raw_lines.put(text)
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                state.lines.put(payload)
        except json.JSONDecodeError:
            continue


def send_json(state: PortState, payload: dict[str, Any]) -> None:
    wire = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            view = memoryview(wire)
            offset = 0
            chunk_size = 64
            while offset < len(view):
                end = offset + chunk_size
                if end > len(view):
                    end = len(view)
                written = state.ser.write(view[offset:end])
                if written is None:
                    written = 0
                if written <= 0:
                    raise serial.SerialTimeoutException("serial write returned 0 bytes")
                offset += int(written)
                if offset < len(view):
                    time.sleep(0.001)
            state.ser.flush()
            return
        except Exception as exc:
            last_error = exc
            print(
                f"WARN: write failed port={state.port} attempt={attempt + 1} "
                f"type={payload.get('type')} err={exc}"
            )
            try:
                state.ser.reset_output_buffer()
            except Exception:
                pass
            time.sleep(0.2)
    if last_error is not None:
        raise last_error


def probe_node_id(port: str, baud: int, timeout_s: float) -> str | None:
    wire = (json.dumps({"cmd": "ping", "seq": 0}) + "\n").encode("utf-8")
    with serial.Serial(port=port, baudrate=baud, timeout=0.5, write_timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(1.8)
        ser.reset_input_buffer()
        ser.write(wire)
        ser.flush()

        end = time.time() + timeout_s
        while time.time() < end:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "pong" and isinstance(payload.get("src"), str):
                node = payload["src"].strip()
                if node:
                    return node
    return None


def drain_available(states: list[PortState]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    had_data = True
    while had_data:
        had_data = False
        for s in states:
            try:
                payload = s.lines.get_nowait()
            except queue.Empty:
                continue
            payload["_port"] = s.port
            out.append(payload)
            had_data = True
    return out


def wait_for_condition(
    states: list[PortState],
    history: list[dict[str, Any]],
    timeout_s: float,
    condition,
) -> bool:
    if condition(history):
        return True
    end = time.time() + timeout_s
    while time.time() < end:
        history.extend(drain_available(states))
        if condition(history):
            return True
        time.sleep(0.02)
    return False


def send_with_delivery_retry(
    *,
    tx: PortState,
    dst: PortState,
    states: list[PortState],
    history: list[dict[str, Any]],
    payload: dict[str, Any],
    ack_timeout: float,
    ack_retries: int,
    rx_match,
) -> tuple[bool, int]:
    packet_type = str(payload.get("type") or "")
    e2e_id = str(payload.get("e2e_id") or "")
    seen_ack = False
    seen_delivery = False
    seen_rx = False
    for retry_no in range(ack_retries + 1):
        start_idx = len(history)
        packet = dict(payload)
        packet["retry_no"] = retry_no
        packet["ts_ms"] = now_ms()
        send_json(tx, packet)
        deadline = time.time() + ack_timeout
        while time.time() < deadline:
            before = len(history)
            history.extend(drain_available(states))
            if len(history) == before:
                time.sleep(0.05)
                continue
            for ev in history[before:]:
                if (
                    ev.get("_port") == tx.port
                    and ev.get("type") == "ack"
                    and ev.get("cmd") == packet_type
                    and bool(ev.get("ok"))
                ):
                    seen_ack = True
                if (
                    ev.get("_port") == tx.port
                    and ev.get("type") == "delivery_ack"
                    and str(ev.get("e2e_id") or "").strip() == e2e_id
                    and str(ev.get("src") or "").strip() == str(dst.node_id)
                    and str(ev.get("dst") or "").strip() == str(tx.node_id)
                    and str(ev.get("ack_for") or "").strip() == packet_type
                    and str(ev.get("status") or "").strip().lower() == "ok"
                ):
                    seen_delivery = True
                if rx_match(ev):
                    seen_rx = True
            if seen_ack and seen_delivery and seen_rx:
                return True, retry_no
        if seen_ack and seen_delivery and seen_rx:
            return True, retry_no
    return False, ack_retries


def main() -> int:
    parser = argparse.ArgumentParser(description="ESP32-C3 mesh smoke test")
    parser.add_argument("--ports", nargs="+", required=True, help="COM ports (3+ devices)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--boot-timeout", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--ack-timeout", type=float, default=4.0, help="timeout for directed delivery_ack")
    parser.add_argument("--ack-retries", type=int, default=6, help="retries for directed delivery_ack")
    parser.add_argument("--skip-ble", action="store_true", help="skip BLE short chat check")
    parser.add_argument("--skip-r1k", action="store_true", help="skip reliable_1k FEC check")
    parser.add_argument("--min-node-count", type=int, default=0, help="expected node_list minimum count (0=port count)")
    parser.add_argument("--r1k-profile", type=int, default=0, help="reliable_1k profile id (default: 0=25+8)")
    parser.add_argument("--r1k-max-retry-rate", type=float, default=-1.0, help="fail if reliable retry rate exceeds this value")
    parser.add_argument("--r1k-max-latency-ms", type=int, default=0, help="fail if reliable pong latency exceeds this value (0=disabled)")
    parser.add_argument("--rounds", type=int, default=1, help="directed ping_probe rounds")
    parser.add_argument("--interval-ms", type=int, default=600, help="interval between rounds in ms")
    parser.add_argument("--rotate-tx", action="store_true", help="rotate tx node on each round")
    parser.add_argument("--collect-stats", action="store_true", help="collect get_stats before/after each round")
    parser.add_argument("--threshold-file", type=Path, default=None, help="JSON file for round thresholds")
    parser.add_argument("--strict-pass", action="store_true", help="exit 1 when any threshold is violated")
    parser.add_argument("--require-min-hops", type=int, default=0, help="required minimum hops for successful rounds")
    parser.add_argument("--jsonl-out", type=Path, default=None, help="write per-round records as JSONL")
    parser.add_argument("--summary-json", type=Path, default=None, help="write round summary JSON")
    args = parser.parse_args()
    if len(args.ports) < 3:
        parser.error("--ports は最低3台必要です")
    if args.min_node_count < 0:
        parser.error("--min-node-count は 0 以上を指定してください")
    if args.ack_timeout <= 0:
        parser.error("--ack-timeout は 0 より大きい値を指定してください")
    if args.ack_retries < 0:
        parser.error("--ack-retries は 0 以上を指定してください")
    if args.r1k_profile < 0:
        parser.error("--r1k-profile は 0 以上を指定してください")
    if args.r1k_max_retry_rate >= 0 and args.r1k_max_retry_rate > 1.0:
        parser.error("--r1k-max-retry-rate は 0.0..1.0 (または -1 で無効) を指定してください")
    if args.r1k_max_latency_ms < 0:
        parser.error("--r1k-max-latency-ms は 0 以上を指定してください")
    if args.rounds <= 0:
        parser.error("--rounds は 1 以上を指定してください")
    if args.interval_ms < 0:
        parser.error("--interval-ms は 0 以上を指定してください")
    if args.require_min_hops < 0:
        parser.error("--require-min-hops は 0 以上を指定してください")

    try:
        threshold_from_file = parse_threshold_file(args.threshold_file)
    except ValueError as exc:
        print(f"NG: invalid threshold file: {exc}")
        return 1
    effective_thresholds = combine_thresholds(
        cli_require_min_hops=args.require_min_hops,
        cli_max_latency_ms=args.r1k_max_latency_ms,
        cli_max_retry_rate=args.r1k_max_retry_rate,
        from_file=threshold_from_file,
    )

    states: list[PortState] = []
    readers: list[threading.Thread] = []
    stop_event = threading.Event()

    try:
        print("== Probe node ids ==")
        node_by_port: dict[str, str] = {}
        for p in args.ports:
            node: str | None = None
            for _ in range(3):
                node = probe_node_id(p, args.baud, args.boot_timeout)
                if node is not None:
                    break
                time.sleep(0.8)
            if node is None:
                print(f"NG: node_id probe failed port={p}")
                return 1
            node_by_port[p] = node
            print(f"[{p}] node_id={node}")

        for p in args.ports:
            ser = serial.Serial(port=p, baudrate=args.baud, timeout=0.2, write_timeout=0.5)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            st = PortState(port=p, ser=ser, node_id=node_by_port.get(p))
            states.append(st)

        for st in states:
            t = threading.Thread(target=reader_loop, args=(st, stop_event), daemon=True)
            t.start()
            readers.append(t)

        # ポート再オープン直後はリセットや再初期化中のことがあるため、少し待機する。
        time.sleep(2.0)

        tx = states[0]
        receivers = states[1:]
        ping_target = receivers[0]
        directed_target = receivers[-1]
        event_history: list[dict[str, Any]] = []
        event_history.extend(drain_available(states))

        print("== Request node list ==")
        start_idx = len(event_history)
        expected_nodes = args.min_node_count if args.min_node_count > 0 else len(args.ports)
        deadline = time.time() + args.timeout
        next_request = 0.0
        node_count = 0
        while time.time() < deadline:
            now = time.time()
            if now >= next_request:
                send_json(tx, {"type": "nodes_request", "src": "pc"})
                next_request = now + 2.0
            event_history.extend(drain_available(states))
            for ev in event_history[start_idx:]:
                if ev.get("_port") == tx.port and ev.get("type") == "node_list" and isinstance(ev.get("nodes"), list):
                    node_count = max(node_count, len(ev["nodes"]))
            if node_count >= expected_nodes:
                break
            time.sleep(0.05)
        if node_count < expected_nodes:
            print(f"NG: node_list count too small count={node_count} expected>={expected_nodes}")
            for e in event_history[-10:]:
                print(e)
            return 1
        print(f"OK: node_list observed (count={node_count})")

        marker = f"smoke-wifi-{uuid.uuid4().hex[:8]}"
        print("== Wi-Fi chat broadcast ==")
        start_idx = len(event_history)
        send_json(tx, {"type": "chat", "via": "wifi", "text": marker, "src": "pc"})
        expected_ports = {s.port for s in receivers}
        ok_broadcast = wait_for_condition(
            states,
            event_history,
            timeout_s=args.timeout,
            condition=lambda events: {
                ev.get("_port")
                for ev in events[start_idx:]
                if ev.get("type") == "chat" and ev.get("text") == marker and ev.get("_port") in expected_ports
            }
            == expected_ports,
        )
        observed_ports = {
            ev.get("_port")
            for ev in event_history[start_idx:]
            if ev.get("type") == "chat" and ev.get("text") == marker and ev.get("_port") in expected_ports
        }
        if not ok_broadcast:
            observed_ports = {str(p) for p in observed_ports}
        if observed_ports != expected_ports:
            missing = sorted(expected_ports - {str(p) for p in observed_ports})
            print(f"NG: wifi chat not seen on all receivers, missing={missing} observed={sorted(observed_ports)}")
            return 1
        print(f"OK: wifi chat received on {sorted(observed_ports)}")

        print("== Directed Wi-Fi chat + delivery_ack ==")
        directed_marker = f"smoke-directed-{uuid.uuid4().hex[:8]}"
        directed_e2e_id = f"smoke-e2e-{uuid.uuid4().hex[:10]}"
        directed_ok = False
        for retry_no in range(args.ack_retries + 1):
            start_idx = len(event_history)
            send_json(
                tx,
                {
                    "type": "chat",
                    "via": "wifi",
                    "dst": directed_target.node_id,
                    "text": directed_marker,
                    "src": "pc",
                    "ts_ms": now_ms(),
                    "need_ack": True,
                    "e2e_id": directed_e2e_id,
                    "retry_no": retry_no,
                },
            )
            directed_ok = wait_for_condition(
                states,
                event_history,
                timeout_s=args.ack_timeout,
                condition=lambda events: (
                    any(
                        ev.get("_port") == tx.port
                        and ev.get("type") == "ack"
                        and ev.get("cmd") == "chat"
                        and bool(ev.get("ok"))
                        for ev in events[start_idx:]
                    )
                    and any(
                        ev.get("_port") == directed_target.port
                        and ev.get("type") == "chat"
                        and ev.get("text") == directed_marker
                        and str(ev.get("src") or "").strip() == str(tx.node_id)
                        and str(ev.get("dst") or "").strip() == str(directed_target.node_id)
                        and str(ev.get("e2e_id") or "").strip() == directed_e2e_id
                        and isinstance(ev.get("rssi"), int)
                        for ev in events[start_idx:]
                    )
                    and any(
                        ev.get("_port") == tx.port
                        and ev.get("type") == "delivery_ack"
                        and str(ev.get("e2e_id") or "").strip() == directed_e2e_id
                        and str(ev.get("src") or "").strip() == str(directed_target.node_id)
                        and str(ev.get("dst") or "").strip() == str(tx.node_id)
                        and str(ev.get("ack_for") or "").strip() == "chat"
                        and str(ev.get("status") or "").strip().lower() == "ok"
                        and isinstance(ev.get("rssi"), int)
                        for ev in events[start_idx:]
                    )
                ),
            )
            if directed_ok:
                print(f"OK: directed delivery_ack received (retry={retry_no})")
                break

        if not directed_ok:
            print("NG: directed delivery_ack timeout")
            for e in event_history[-15:]:
                print(e)
            return 1

        print("== Directed long text (chunk) + delivery_ack ==")
        long_text = ("R1K-LTXT-" + ("0123456789abcdef" * 80))[:RELIABLE_1K_BYTES]
        long_raw = long_text.encode("utf-8")
        text_id = f"ltxt-{uuid.uuid4().hex[:8]}"
        chunk_size = 32
        total_chunks = 0 if len(long_raw) == 0 else ((len(long_raw) - 1) // chunk_size) + 1
        long_hash = hashlib.sha256(long_raw).hexdigest()

        start_payload = {
            "type": "long_text_start",
            "via": "wifi",
            "dst": directed_target.node_id,
            "src": "pc",
            "text_id": text_id,
            "encoding": "utf-8",
            "size": len(long_raw),
            "chunks": total_chunks,
            "need_ack": True,
            "e2e_id": f"{text_id}:s",
        }
        ok_start, retry_start = send_with_delivery_retry(
            tx=tx,
            dst=directed_target,
            states=states,
            history=event_history,
            payload=start_payload,
            ack_timeout=args.ack_timeout,
            ack_retries=args.ack_retries,
            rx_match=lambda ev: (
                ev.get("_port") == directed_target.port
                and ev.get("type") == "long_text_start"
                and str(ev.get("text_id") or "") == text_id
                and str(ev.get("dst") or "").strip() == str(directed_target.node_id)
                and isinstance(ev.get("rssi"), int)
            ),
        )
        if not ok_start:
            print("NG: long_text_start delivery_ack timeout")
            for e in event_history[-20:]:
                print(e)
            return 1

        chunk_retry_total = 0
        for idx, offset in enumerate(range(0, len(long_raw), chunk_size)):
            chunk = long_raw[offset : offset + chunk_size]
            chunk_payload = {
                "type": "long_text_chunk",
                "via": "wifi",
                "dst": directed_target.node_id,
                "src": "pc",
                "text_id": text_id,
                "index": idx,
                "data_b64": base64.b64encode(chunk).decode("ascii"),
                "need_ack": True,
                "e2e_id": f"{text_id}:c:{idx}",
            }
            ok_chunk, retry_chunk = send_with_delivery_retry(
                tx=tx,
                dst=directed_target,
                states=states,
                history=event_history,
                payload=chunk_payload,
                ack_timeout=args.ack_timeout,
                ack_retries=args.ack_retries,
                rx_match=lambda ev, expected_idx=idx: (
                    ev.get("_port") == directed_target.port
                    and ev.get("type") == "long_text_chunk"
                    and str(ev.get("text_id") or "") == text_id
                    and to_int(ev.get("index", -1), -1) == expected_idx
                    and str(ev.get("dst") or "").strip() == str(directed_target.node_id)
                    and isinstance(ev.get("rssi"), int)
                ),
            )
            if not ok_chunk:
                print(f"NG: long_text_chunk delivery_ack timeout index={idx}")
                for e in event_history[-20:]:
                    print(e)
                return 1
            if retry_chunk > 0:
                print(f"INFO: long_text_chunk retry index={idx} retry={retry_chunk}")
            chunk_retry_total += max(0, int(retry_chunk))

        end_payload = {
            "type": "long_text_end",
            "via": "wifi",
            "dst": directed_target.node_id,
            "src": "pc",
            "text_id": text_id,
            "need_ack": True,
            "e2e_id": f"{text_id}:e",
        }
        ok_end, retry_end = send_with_delivery_retry(
            tx=tx,
            dst=directed_target,
            states=states,
            history=event_history,
            payload=end_payload,
            ack_timeout=args.ack_timeout,
            ack_retries=args.ack_retries,
            rx_match=lambda ev: (
                ev.get("_port") == directed_target.port
                and ev.get("type") == "long_text_end"
                and str(ev.get("text_id") or "") == text_id
                and str(ev.get("dst") or "").strip() == str(directed_target.node_id)
                and isinstance(ev.get("rssi"), int)
            ),
        )
        if not ok_end:
            print("NG: long_text_end delivery_ack timeout")
            for e in event_history[-20:]:
                print(e)
            return 1

        received_parts: dict[int, bytes] = {}
        for ev in event_history:
            if ev.get("_port") != directed_target.port:
                continue
            if ev.get("type") != "long_text_chunk":
                continue
            if str(ev.get("text_id") or "") != text_id:
                continue
            idx = to_int(ev.get("index", -1), -1)
            if idx < 0:
                continue
            data_b64 = ev.get("data_b64")
            if not isinstance(data_b64, str):
                continue
            try:
                received_parts[idx] = base64.b64decode(data_b64, validate=True)
            except Exception:
                print(f"NG: invalid base64 in long_text_chunk index={idx}")
                return 1

        if len(received_parts) < total_chunks:
            print(f"NG: long text missing chunks received={len(received_parts)} expected={total_chunks}")
            return 1
        missing_indexes = [i for i in range(total_chunks) if i not in received_parts]
        if missing_indexes:
            print(f"NG: long text chunk holes={missing_indexes[:10]} total_missing={len(missing_indexes)}")
            return 1
        merged = b"".join(received_parts[i] for i in range(total_chunks))
        if hashlib.sha256(merged).hexdigest() != long_hash:
            print("NG: long text hash mismatch")
            return 1
        if merged.decode("utf-8") != long_text:
            print("NG: long text decode mismatch")
            return 1
        print(
            f"OK: directed long text delivered bytes={len(long_raw)} chunks={total_chunks} "
            f"start_retry={retry_start} end_retry={retry_end}"
        )

        print("== Directed ping_probe (1KB) ==")
        seq = 1
        ping_id = uuid.uuid4().hex[:8]
        start_idx = len(event_history)
        send_json(
            tx,
            {
                "cmd": "ping_probe",
                "type": "ping",
                "via": "wifi",
                "dst": ping_target.node_id,
                "seq": seq,
                "ping_id": ping_id,
                "probe_bytes": 1000,
                "ts_ms": now_ms(),
                "src": "pc",
            },
        )
        ok_ping = wait_for_condition(
            states,
            event_history,
            timeout_s=args.timeout,
            condition=lambda events: any(
                ev.get("_port") == tx.port
                and ev.get("type") == "pong"
                and to_int(ev.get("seq", -1), -1) == seq
                and str(ev.get("ping_id") or "") == ping_id
                and str(ev.get("src") or "").strip() == str(ping_target.node_id)
                and to_int(ev.get("probe_bytes", -1), -1) == RELIABLE_1K_BYTES
                and bool(ev.get("probe_hash_ok"))
                for ev in events[start_idx:]
            ),
        )
        if not ok_ping:
            print("NG: pong timeout")
            return 1
        pong_event = next(
            (
                ev
                for ev in reversed(event_history[start_idx:])
                if ev.get("_port") == tx.port
                and ev.get("type") == "pong"
                and to_int(ev.get("seq", -1), -1) == seq
                and str(ev.get("ping_id") or "") == ping_id
                and str(ev.get("src") or "").strip() == str(ping_target.node_id)
            ),
            None,
        )
        ping_latency_ms = to_int((pong_event or {}).get("latency_ms", -1), -1)
        if args.r1k_max_latency_ms > 0 and (ping_latency_ms < 0 or ping_latency_ms > args.r1k_max_latency_ms):
            print(f"NG: reliable_1k latency too high latency_ms={ping_latency_ms} limit={args.r1k_max_latency_ms}")
            return 1
        packet_count = total_chunks + 2
        retry_total = max(0, int(retry_start)) + chunk_retry_total + max(0, int(retry_end))
        retry_rate = (float(retry_total) / float(packet_count)) if packet_count > 0 else 0.0
        if args.r1k_max_retry_rate >= 0 and retry_rate > args.r1k_max_retry_rate:
            print(
                f"NG: reliable_1k retry_rate too high retry_rate={retry_rate:.3f} "
                f"limit={args.r1k_max_retry_rate:.3f}"
            )
            return 1
        single_ping_summary = {
            "seq": seq,
            "ping_id": ping_id,
            "latency_ms": ping_latency_ms,
            "retry_rate": retry_rate,
            "dst_node": ping_target.node_id,
            "tx_node": tx.node_id,
        }
        print(f"OK: pong received latency={ping_latency_ms}ms retry_rate={retry_rate:.3f}")

        if args.skip_r1k:
            print("SKIP: Directed reliable_1k (FEC)")
        else:
            print("== Directed reliable_1k (FEC) ==")
            reliable_text = ("R1K-FEC-" + ("0123456789abcdef" * 80))[:RELIABLE_1K_BYTES]
            r1k_packets, r1k_meta = make_reliable_1k_messages(
                text=reliable_text,
                dst=directed_target.node_id,
                ttl=max(1, min(255, 10)),
                profile_id=int(args.r1k_profile),
                require_ack=True,
                interleave=True,
            )
            r1k_id = str(r1k_meta.get("r1k_id") or "")
            if not r1k_id:
                print("NG: reliable_1k missing session id")
                return 1
            r1k_retry_total = 0
            for packet in r1k_packets:
                packet_type = str(packet.get("type") or "")
                expected_idx = to_int(packet.get("index", -1), -1)
                ok_packet, retry_packet = send_with_delivery_retry(
                    tx=tx,
                    dst=directed_target,
                    states=states,
                    history=event_history,
                    payload=packet,
                    ack_timeout=args.ack_timeout,
                    ack_retries=args.ack_retries,
                    rx_match=lambda ev, expected_type=packet_type, expected_index=expected_idx: (
                        ev.get("_port") == directed_target.port
                        and str(ev.get("type") or "") == expected_type
                        and str(ev.get("r1k_id") or "") == r1k_id
                        and (
                            expected_index < 0
                            or to_int(ev.get("index", -1), -1) == expected_index
                        )
                    ),
                )
                if not ok_packet:
                    print(f"NG: reliable_1k delivery_ack timeout type={packet_type} index={expected_idx}")
                    for e in event_history[-25:]:
                        print(e)
                    return 1
                r1k_retry_total += max(0, int(retry_packet))

            shard_map_b64: dict[int, str] = {}
            for ev in event_history:
                if ev.get("_port") != directed_target.port:
                    continue
                typ = str(ev.get("type") or "")
                if typ not in {"reliable_1k_chunk", "reliable_1k_repair"}:
                    continue
                if str(ev.get("r1k_id") or "") != r1k_id:
                    continue
                idx = to_int(ev.get("index", -1), -1)
                data_b64 = ev.get("data_b64")
                if idx < 0 or not isinstance(data_b64, str) or not data_b64:
                    continue
                shard_map_b64[idx] = data_b64

            decoded = decode_reliable_1k_from_shards(
                shard_map_b64=shard_map_b64,
                profile_id=int(r1k_meta.get("profile_id") or 0),
                original_size=int(r1k_meta.get("size") or 0),
            )
            if decoded is None:
                print(
                    f"NG: reliable_1k decode failed received_shards={len(shard_map_b64)} "
                    f"required={int(r1k_meta.get('data_shards') or 0)}"
                )
                return 1
            try:
                decoded_text = decoded.decode("utf-8")
            except UnicodeDecodeError:
                print("NG: reliable_1k decode utf-8 failed")
                return 1
            if decoded_text != reliable_text:
                print("NG: reliable_1k payload mismatch")
                return 1
            total_packet_count = len(r1k_packets)
            r1k_retry_rate = (
                float(r1k_retry_total) / float(total_packet_count)
                if total_packet_count > 0
                else 0.0
            )
            if args.r1k_max_retry_rate >= 0 and r1k_retry_rate > args.r1k_max_retry_rate:
                print(
                    f"NG: reliable_1k retry_rate too high retry_rate={r1k_retry_rate:.3f} "
                    f"limit={args.r1k_max_retry_rate:.3f}"
                )
                return 1
            print(
                f"OK: reliable_1k delivered profile={r1k_meta.get('profile_name')} "
                f"shards={len(shard_map_b64)}/{int(r1k_meta.get('total_shards') or 0)} "
                f"retry_rate={r1k_retry_rate:.3f}"
            )

        if args.skip_ble:
            print("SKIP: BLE short chat")
        else:
            ble_marker = f"b{uuid.uuid4().hex[:4]}"[:6]
            print("== BLE short chat ==")
            start_idx = len(event_history)
            send_json(tx, {"type": "chat", "via": "ble", "text": ble_marker, "src": "pc"})
            ok_ble = wait_for_condition(
                states,
                event_history,
                timeout_s=max(args.timeout, 25.0),
                condition=lambda events: any(
                    ev.get("_port") in expected_ports
                    and ev.get("type") == "chat"
                    and ev.get("via") == "ble"
                    and ev.get("text") == ble_marker
                    for ev in events[start_idx:]
                ),
            )
            if not ok_ble:
                print("NG: ble chat timeout")
                return 1
            print("OK: ble chat received")

        print(
            "== Directed ping_probe rounds "
            f"(rounds={args.rounds} interval_ms={args.interval_ms} rotate_tx={args.rotate_tx} "
            f"collect_stats={args.collect_stats}) =="
        )
        if args.jsonl_out is not None:
            try:
                args.jsonl_out.parent.mkdir(parents=True, exist_ok=True)
                args.jsonl_out.write_text("", encoding="utf-8")
            except OSError as exc:
                print(f"NG: failed to prepare jsonl output path={args.jsonl_out} err={exc}")
                return 1

        round_results: list[dict[str, Any]] = []
        stats_timeout_s = min(max(args.ack_timeout, 1.0), max(args.timeout, 1.0))
        round_seq_base = 1000
        for round_idx in range(args.rounds):
            round_no = round_idx + 1
            round_tx = tx
            round_dst = ping_target
            if args.rotate_tx:
                round_tx = states[round_idx % len(states)]
                if round_tx.port == ping_target.port:
                    round_dst = states[(round_idx + 1) % len(states)]

            seq_round = round_seq_base + round_no
            ping_id_round = uuid.uuid4().hex[:8]
            round_error_tags: list[str] = []

            stats_before: dict[str, int] | None = None
            stats_after: dict[str, int] | None = None
            mesh_delta: dict[str, int] | None = None
            retry_rate_round: float | None = None
            rx_drop_ratio_round: float | None = None
            if args.collect_stats:
                stats_before = request_stats_mesh_counters(
                    target=round_tx,
                    states=states,
                    history=event_history,
                    timeout_s=stats_timeout_s,
                )
                if stats_before is None:
                    round_error_tags.append("stats_before_timeout")

            event_history.extend(drain_available(states))
            round_start_index = len(event_history)
            send_json(
                round_tx,
                {
                    "cmd": "ping_probe",
                    "type": "ping",
                    "via": "wifi",
                    "dst": round_dst.node_id,
                    "seq": seq_round,
                    "ping_id": ping_id_round,
                    "probe_bytes": RELIABLE_1K_BYTES,
                    "ts_ms": now_ms(),
                    "src": "pc",
                },
            )
            pong_event_round = wait_for_event(
                states,
                event_history,
                timeout_s=args.timeout,
                matcher=lambda ev, tx_port=round_tx.port, seq_value=seq_round, ping_token=ping_id_round, expected_src=round_dst.node_id: (
                    ev.get("_port") == tx_port
                    and ev.get("type") == "pong"
                    and to_int(ev.get("seq", -1), -1) == seq_value
                    and str(ev.get("ping_id") or "") == ping_token
                    and str(ev.get("src") or "").strip() == str(expected_src)
                ),
                start_index=round_start_index,
            )

            if args.collect_stats:
                stats_after = request_stats_mesh_counters(
                    target=round_tx,
                    states=states,
                    history=event_history,
                    timeout_s=stats_timeout_s,
                )
                if stats_after is None:
                    round_error_tags.append("stats_after_timeout")
                if stats_before is not None and stats_after is not None:
                    mesh_delta = compute_counter_delta(stats_before, stats_after)
                    retry_rate_round = calc_ratio(
                        max(0, to_int(mesh_delta.get("tx_no_mem_retries", 0), 0)),
                        max(0, to_int(mesh_delta.get("tx_frames", 0), 0)),
                    )
                    rx_drop_ratio_round = calc_ratio(
                        max(0, to_int(mesh_delta.get("rx_queue_dropped", 0), 0)),
                        max(0, to_int(mesh_delta.get("rx_frames", 0), 0)),
                    )

            latency_round = None
            hops_round = None
            probe_hash_ok_round = None
            if pong_event_round is None:
                round_error_tags.append("pong_timeout")
            else:
                latency_value = to_int(pong_event_round.get("latency_ms"), -1)
                if latency_value >= 0:
                    latency_round = latency_value
                hops_value = to_int(pong_event_round.get("hops"), -1)
                if hops_value >= 0:
                    hops_round = hops_value
                if "probe_hash_ok" in pong_event_round:
                    probe_hash_ok_round = bool(pong_event_round.get("probe_hash_ok"))
                else:
                    round_error_tags.append("missing_probe_hash_ok")
            success_round = bool(pong_event_round is not None and probe_hash_ok_round is True)

            round_record: dict[str, Any] = {
                "round": round_no,
                "tx_port": round_tx.port,
                "tx_node": round_tx.node_id,
                "dst_port": round_dst.port,
                "dst_node": round_dst.node_id,
                "seq": seq_round,
                "ping_id": ping_id_round,
                "success": success_round,
                "latency_ms": latency_round,
                "hops": hops_round,
                "probe_hash_ok": probe_hash_ok_round,
                "mesh_delta": mesh_delta,
                "retry_rate": retry_rate_round,
                "rx_queue_drop_ratio": rx_drop_ratio_round,
                "errors": round_error_tags,
            }
            round_results.append(round_record)

            if args.jsonl_out is not None:
                try:
                    append_jsonl(args.jsonl_out, round_record)
                except OSError as exc:
                    print(f"NG: failed to write jsonl path={args.jsonl_out} err={exc}")
                    return 1

            latency_text = "n/a" if latency_round is None else str(latency_round)
            hops_text = "n/a" if hops_round is None else str(hops_round)
            hash_text = "n/a"
            if probe_hash_ok_round is True:
                hash_text = "ok"
            elif probe_hash_ok_round is False:
                hash_text = "ng"
            extras: list[str] = []
            if retry_rate_round is not None:
                extras.append(f"retry_rate={retry_rate_round:.3f}")
            if rx_drop_ratio_round is not None:
                extras.append(f"rx_drop_ratio={rx_drop_ratio_round:.3f}")
            if round_error_tags:
                extras.append(f"errors={','.join(round_error_tags)}")
            extra_text = (" " + " ".join(extras)) if extras else ""
            print(
                f"ROUND {round_no}/{args.rounds}: tx={round_tx.port} dst={round_dst.port} "
                f"success={success_round} latency_ms={latency_text} hops={hops_text} "
                f"probe_hash={hash_text}{extra_text}"
            )

            if round_no < args.rounds and args.interval_ms > 0:
                time.sleep(float(args.interval_ms) / 1000.0)

        round_success_count = sum(1 for r in round_results if bool(r.get("success")))
        success_rate = float(round_success_count) / float(args.rounds) if args.rounds > 0 else 0.0
        latency_samples = [
            int(r["latency_ms"])
            for r in round_results
            if bool(r.get("success")) and isinstance(r.get("latency_ms"), int) and int(r.get("latency_ms")) >= 0
        ]
        hops_samples = [
            int(r["hops"])
            for r in round_results
            if bool(r.get("success")) and isinstance(r.get("hops"), int) and int(r.get("hops")) >= 0
        ]
        retry_rate_samples = [float(r["retry_rate"]) for r in round_results if isinstance(r.get("retry_rate"), (int, float))]
        rx_drop_ratio_samples = [
            float(r["rx_queue_drop_ratio"])
            for r in round_results
            if isinstance(r.get("rx_queue_drop_ratio"), (int, float))
        ]
        max_latency_observed = max(latency_samples) if latency_samples else None
        min_hops_observed = min(hops_samples) if hops_samples else None
        max_retry_rate_observed = max(retry_rate_samples) if retry_rate_samples else None
        max_rx_drop_ratio_observed = max(rx_drop_ratio_samples) if rx_drop_ratio_samples else None
        probe_hash_ok_count = sum(1 for r in round_results if r.get("probe_hash_ok") is True)

        threshold_violations: list[dict[str, Any]] = []
        min_success_rate_limit = effective_thresholds.get("min_success_rate")
        if isinstance(min_success_rate_limit, (int, float)) and success_rate < float(min_success_rate_limit):
            threshold_violations.append(
                {
                    "metric": "min_success_rate",
                    "actual": success_rate,
                    "limit": float(min_success_rate_limit),
                }
            )
        max_latency_limit = effective_thresholds.get("max_latency_ms")
        if isinstance(max_latency_limit, (int, float)):
            if max_latency_observed is None:
                threshold_violations.append(
                    {
                        "metric": "max_latency_ms",
                        "actual": None,
                        "limit": float(max_latency_limit),
                        "reason": "no_success_latency_samples",
                    }
                )
            elif float(max_latency_observed) > float(max_latency_limit):
                threshold_violations.append(
                    {
                        "metric": "max_latency_ms",
                        "actual": float(max_latency_observed),
                        "limit": float(max_latency_limit),
                    }
                )
        max_retry_limit = effective_thresholds.get("max_retry_rate")
        if isinstance(max_retry_limit, (int, float)):
            if not args.collect_stats:
                threshold_violations.append(
                    {
                        "metric": "max_retry_rate",
                        "actual": None,
                        "limit": float(max_retry_limit),
                        "reason": "--collect-stats is required",
                    }
                )
            elif max_retry_rate_observed is None:
                threshold_violations.append(
                    {
                        "metric": "max_retry_rate",
                        "actual": None,
                        "limit": float(max_retry_limit),
                        "reason": "no_stats_delta_samples",
                    }
                )
            elif float(max_retry_rate_observed) > float(max_retry_limit):
                threshold_violations.append(
                    {
                        "metric": "max_retry_rate",
                        "actual": float(max_retry_rate_observed),
                        "limit": float(max_retry_limit),
                    }
                )
        max_drop_limit = effective_thresholds.get("max_rx_queue_drop_ratio")
        if isinstance(max_drop_limit, (int, float)):
            if not args.collect_stats:
                threshold_violations.append(
                    {
                        "metric": "max_rx_queue_drop_ratio",
                        "actual": None,
                        "limit": float(max_drop_limit),
                        "reason": "--collect-stats is required",
                    }
                )
            elif max_rx_drop_ratio_observed is None:
                threshold_violations.append(
                    {
                        "metric": "max_rx_queue_drop_ratio",
                        "actual": None,
                        "limit": float(max_drop_limit),
                        "reason": "no_stats_delta_samples",
                    }
                )
            elif float(max_rx_drop_ratio_observed) > float(max_drop_limit):
                threshold_violations.append(
                    {
                        "metric": "max_rx_queue_drop_ratio",
                        "actual": float(max_rx_drop_ratio_observed),
                        "limit": float(max_drop_limit),
                    }
                )
        require_min_hops_limit = to_int(effective_thresholds.get("require_min_hops"), 0)
        if require_min_hops_limit > 0:
            too_low_rounds = [
                int(r["round"])
                for r in round_results
                if bool(r.get("success")) and (not isinstance(r.get("hops"), int) or int(r.get("hops")) < require_min_hops_limit)
            ]
            if too_low_rounds:
                threshold_violations.append(
                    {
                        "metric": "require_min_hops",
                        "actual": too_low_rounds,
                        "limit": require_min_hops_limit,
                    }
                )

        round_summary: dict[str, Any] = {
            "rounds": args.rounds,
            "interval_ms": args.interval_ms,
            "rotate_tx": bool(args.rotate_tx),
            "collect_stats": bool(args.collect_stats),
            "success_count": round_success_count,
            "failure_count": args.rounds - round_success_count,
            "success_rate": success_rate,
            "probe_hash_ok_count": probe_hash_ok_count,
            "latency_ms": {
                "min": min(latency_samples) if latency_samples else None,
                "max": max_latency_observed,
                "avg": (sum(latency_samples) / len(latency_samples)) if latency_samples else None,
            },
            "hops": {
                "min": min_hops_observed,
                "max": max(hops_samples) if hops_samples else None,
            },
            "max_retry_rate_observed": max_retry_rate_observed,
            "max_rx_queue_drop_ratio_observed": max_rx_drop_ratio_observed,
            "thresholds": effective_thresholds,
            "threshold_violations": threshold_violations,
            "threshold_pass": len(threshold_violations) == 0,
            "strict_pass": bool(args.strict_pass),
            "threshold_enforced": bool(args.strict_pass or (args.threshold_file is not None)),
        }
        summary_payload: dict[str, Any] = {
            "timestamp_ms": now_ms(),
            "ports": args.ports,
            "threshold_file": str(args.threshold_file) if args.threshold_file is not None else None,
            "threshold_file_values": threshold_from_file,
            "single_ping_probe": single_ping_summary,
            "round_summary": round_summary,
        }

        if args.summary_json is not None:
            try:
                write_summary_json(args.summary_json, summary_payload)
            except OSError as exc:
                print(f"NG: failed to write summary json path={args.summary_json} err={exc}")
                return 1
            print(f"INFO: summary json written path={args.summary_json}")
        if args.jsonl_out is not None:
            print(f"INFO: round jsonl written path={args.jsonl_out}")

        print(
            f"ROUND SUMMARY: success={round_success_count}/{args.rounds} "
            f"success_rate={success_rate:.3f} "
            f"latency_max_ms={max_latency_observed if max_latency_observed is not None else 'n/a'} "
            f"hops_min={min_hops_observed if min_hops_observed is not None else 'n/a'} "
            f"probe_hash_ok={probe_hash_ok_count}/{args.rounds}"
        )
        if threshold_violations:
            print(f"WARN: threshold violations detected count={len(threshold_violations)}")
            for violation in threshold_violations:
                print(
                    "WARN: threshold metric={metric} actual={actual} limit={limit} reason={reason}".format(
                        metric=violation.get("metric"),
                        actual=violation.get("actual"),
                        limit=violation.get("limit"),
                        reason=violation.get("reason", "-"),
                    )
                )

        if round_success_count == 0:
            print("NG: all round ping_probe attempts failed")
            return 1
        enforce_threshold_fail = bool(args.strict_pass or (args.threshold_file is not None))
        if enforce_threshold_fail and threshold_violations:
            print("NG: threshold violation found (enforced by strict-pass or threshold-file)")
            return 1

        if threshold_violations:
            print("ALL TESTS PASSED (WITH THRESHOLD WARNINGS)")
            return 0
        print("ALL TESTS PASSED")
        return 0
    finally:
        stop_event.set()
        for t in readers:
            t.join(timeout=0.5)
        for st in states:
            try:
                st.ser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
