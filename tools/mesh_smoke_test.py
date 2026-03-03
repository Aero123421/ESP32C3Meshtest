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


def wait_for(
    states: list[PortState],
    predicate,
    timeout_s: float,
) -> tuple[bool, list[dict[str, Any]]]:
    end = time.time() + timeout_s
    matched: list[dict[str, Any]] = []
    while time.time() < end:
        for s in states:
            try:
                payload = s.lines.get_nowait()
            except queue.Empty:
                continue
            payload["_port"] = s.port
            matched.append(payload)
            if predicate(payload, s):
                return True, matched
        time.sleep(0.02)
    return False, matched


def drain_for(states: list[PortState], seconds: float) -> list[dict[str, Any]]:
    end = time.time() + seconds
    out: list[dict[str, Any]] = []
    while time.time() < end:
        for s in states:
            try:
                payload = s.lines.get_nowait()
            except queue.Empty:
                continue
            payload["_port"] = s.port
            out.append(payload)
        time.sleep(0.02)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="ESP32-C3 mesh smoke test")
    parser.add_argument("--ports", nargs=3, required=True, help="COM ports (3 devices)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--boot-timeout", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

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
        rx1 = states[1]
        rx2 = states[2]

        print("== Request node list ==")
        send_json(tx, {"type": "nodes_request", "src": "pc"})
        ok_nodes, events = wait_for(
            states,
            lambda payload, _: payload.get("type") == "node_list"
            and isinstance(payload.get("nodes"), list),
            timeout_s=args.timeout,
        )
        if not ok_nodes:
            print("NG: node_list not observed")
            for e in events[-10:]:
                print(e)
            return 1
        node_count = 0
        for e in reversed(events):
            if e.get("type") == "node_list" and isinstance(e.get("nodes"), list):
                node_count = len(e["nodes"])
                break
        print(f"OK: node_list observed (count={node_count})")

        marker = f"smoke-wifi-{uuid.uuid4().hex[:8]}"
        print("== Wi-Fi chat broadcast ==")
        send_json(tx, {"type": "chat", "via": "wifi", "text": marker, "src": "pc"})
        observed_ports: set[str] = set()
        end = time.time() + args.timeout
        while time.time() < end and len(observed_ports) < 2:
            for event in drain_for(states, 0.3):
                if event.get("type") == "chat" and event.get("text") == marker:
                    observed_ports.add(event.get("_port", ""))
            time.sleep(0.05)
        observed_ports.discard(tx.port)
        if not (rx1.port in observed_ports and rx2.port in observed_ports):
            print(f"NG: wifi chat not seen on all receivers, observed={sorted(observed_ports)}")
            return 1
        print(f"OK: wifi chat received on {sorted(observed_ports)}")

        print("== Directed ping ==")
        seq = 1
        ping_id = uuid.uuid4().hex[:8]
        send_json(
            tx,
            {
                "type": "ping",
                "via": "wifi",
                "dst": rx1.node_id,
                "seq": seq,
                "ping_id": ping_id,
                "ts_ms": now_ms(),
                "src": "pc",
            },
        )
        ok_ping, _ = wait_for(
            [tx],
            lambda payload, _: payload.get("type") == "pong"
            and int(payload.get("seq", -1)) == seq,
            timeout_s=args.timeout,
        )
        if not ok_ping:
            print("NG: pong timeout")
            return 1
        print("OK: pong received")

        ble_marker = f"b{uuid.uuid4().hex[:4]}"[:6]
        print("== BLE short chat ==")
        send_json(tx, {"type": "chat", "via": "ble", "text": ble_marker, "src": "pc"})
        ok_ble, _ = wait_for(
            [rx1, rx2],
            lambda payload, _: payload.get("type") == "chat"
            and payload.get("via") == "ble"
            and payload.get("text") == ble_marker,
            timeout_s=max(args.timeout, 25.0),
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
