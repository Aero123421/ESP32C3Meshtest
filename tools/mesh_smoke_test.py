#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import serial


@dataclass
class PortState:
    port: str
    ser: serial.Serial
    lines: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    raw_lines: queue.Queue[str] = field(default_factory=queue.Queue)
    node_id: str | None = None


def now_ms() -> int:
    return int(time.time() * 1000)


def reader_loop(state: PortState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            raw = state.ser.readline()
        except Exception:
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
    for _ in range(3):
        try:
            state.ser.write(wire)
            state.ser.flush()
            return
        except Exception as exc:
            last_error = exc
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


def main() -> int:
    parser = argparse.ArgumentParser(description="ESP32-C3 mesh smoke test")
    parser.add_argument("--ports", nargs="+", required=True, help="COM ports (3+ devices)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--boot-timeout", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--ack-timeout", type=float, default=8.0, help="timeout for directed delivery_ack")
    parser.add_argument("--ack-retries", type=int, default=2, help="retries for directed delivery_ack")
    parser.add_argument("--skip-ble", action="store_true", help="skip BLE short chat check")
    parser.add_argument("--min-node-count", type=int, default=0, help="expected node_list minimum count (0=port count)")
    args = parser.parse_args()
    if len(args.ports) < 3:
        parser.error("--ports は最低3台必要です")
    if args.min_node_count < 0:
        parser.error("--min-node-count は 0 以上を指定してください")
    if args.ack_timeout <= 0:
        parser.error("--ack-timeout は 0 より大きい値を指定してください")
    if args.ack_retries < 0:
        parser.error("--ack-retries は 0 以上を指定してください")

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
                        and str(ev.get("e2e_id") or "").strip() == directed_e2e_id
                        for ev in events[start_idx:]
                    )
                    and any(
                        ev.get("_port") == tx.port
                        and ev.get("type") == "delivery_ack"
                        and str(ev.get("e2e_id") or "").strip() == directed_e2e_id
                        and str(ev.get("src") or "").strip() == str(directed_target.node_id)
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

        print("== Directed ping ==")
        seq = 1
        ping_id = uuid.uuid4().hex[:8]
        start_idx = len(event_history)
        send_json(
            tx,
            {
                "type": "ping",
                "via": "wifi",
                "dst": ping_target.node_id,
                "seq": seq,
                "ping_id": ping_id,
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
                and int(ev.get("seq", -1)) == seq
                and str(ev.get("ping_id") or "") == ping_id
                and str(ev.get("src") or "").strip() == str(ping_target.node_id)
                for ev in events[start_idx:]
            ),
        )
        if not ok_ping:
            print("NG: pong timeout")
            return 1
        print("OK: pong received")

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
