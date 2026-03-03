#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import uuid
from typing import Any

import serial


def send_json(ser: serial.Serial, payload: dict[str, Any]) -> None:
    wire = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    ser.write(wire)
    ser.flush()


def reader(ser: serial.Serial, out: queue.Queue[dict[str, Any]], stop: threading.Event, tag: str) -> None:
    while not stop.is_set():
        try:
            line = ser.readline().decode("utf-8", errors="replace").strip()
        except Exception:
            break
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload["_port"] = tag
            out.put(payload)


def wait_for(q: queue.Queue[dict[str, Any]], pred, timeout: float) -> dict[str, Any] | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            p = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if pred(p):
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tx", required=True)
    ap.add_argument("--rx", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--skip-ble", action="store_true")
    args = ap.parse_args()

    stop = threading.Event()
    q: queue.Queue[dict[str, Any]] = queue.Queue()
    history: list[dict[str, Any]] = []

    with (
        serial.Serial(args.tx, args.baud, timeout=0.2, write_timeout=0.5) as tx,
        serial.Serial(args.rx, args.baud, timeout=0.2, write_timeout=0.5) as rx,
    ):
        for s in (tx, rx):
            s.dtr = False
            s.rts = False
            s.reset_input_buffer()
            s.reset_output_buffer()
        time.sleep(2.0)
        for s in (tx, rx):
            s.reset_input_buffer()

        t1 = threading.Thread(target=reader, args=(tx, q, stop, "tx"), daemon=True)
        t2 = threading.Thread(target=reader, args=(rx, q, stop, "rx"), daemon=True)
        t1.start()
        t2.start()

        send_json(tx, {"cmd": "ping", "seq": 1})
        send_json(rx, {"cmd": "ping", "seq": 2})

        tx_pong = None
        rx_pong = None
        end_probe = time.time() + args.timeout
        while time.time() < end_probe and (tx_pong is None or rx_pong is None):
            item = wait_for(q, lambda _: True, 0.5)
            if item is None:
                continue
            history.append(item)
            if tx_pong is None and item.get("_port") == "tx" and item.get("type") == "pong":
                tx_pong = item
            if rx_pong is None and item.get("_port") == "rx" and item.get("type") == "pong":
                rx_pong = item
        if tx_pong is None or rx_pong is None:
            print("NG: initial ping failed")
            print(history[-12:])
            stop.set()
            return 1

        rx_node = str(rx_pong.get("src", "")).strip()
        if not rx_node:
            print("NG: rx node id missing")
            stop.set()
            return 1

        marker = f"wifi-{uuid.uuid4().hex[:6]}"
        send_json(tx, {"type": "chat", "via": "wifi", "text": marker, "src": "pc"})
        got_chat = None
        end_chat = time.time() + args.timeout
        while time.time() < end_chat and got_chat is None:
            item = wait_for(q, lambda _: True, 0.5)
            if item is None:
                continue
            history.append(item)
            if item.get("_port") == "rx" and item.get("type") == "chat" and item.get("text") == marker:
                got_chat = item
        if got_chat is None:
            print("NG: wifi chat not received on rx")
            print(history[-20:])
            stop.set()
            return 1

        send_json(
            tx,
            {
                "type": "ping",
                "via": "wifi",
                "dst": rx_node,
                "seq": 99,
                "ping_id": "smoke99",
                "ts_ms": int(time.time() * 1000),
            },
        )
        got_mesh_pong = None
        end_mesh_pong = time.time() + args.timeout
        while time.time() < end_mesh_pong and got_mesh_pong is None:
            item = wait_for(q, lambda _: True, 0.5)
            if item is None:
                continue
            history.append(item)
            if (
                item.get("_port") == "tx"
                and item.get("type") == "pong"
                and int(item.get("seq", -1)) == 99
                and str(item.get("ping_id") or "") == "smoke99"
                and str(item.get("src") or "").strip() == rx_node
            ):
                got_mesh_pong = item
        if got_mesh_pong is None:
            print("NG: mesh pong timeout")
            print(history[-20:])
            stop.set()
            return 1

        if args.skip_ble:
            print("OK: two-port mesh chat/ping")
            stop.set()
            return 0

        ble_marker = f"b{uuid.uuid4().hex[:4]}"
        send_json(tx, {"type": "chat", "via": "ble", "text": ble_marker, "src": "pc"})
        got_ble = None
        end_ble = time.time() + max(args.timeout, 25.0)
        while time.time() < end_ble and got_ble is None:
            item = wait_for(q, lambda _: True, 0.5)
            if item is None:
                continue
            history.append(item)
            if item.get("_port") == "rx" and item.get("type") == "chat" and item.get("via") == "ble" and item.get("text") == ble_marker:
                got_ble = item
        if got_ble is None:
            print("NG: ble chat timeout")
            print(history[-20:])
            stop.set()
            return 1

        print("OK: two-port mesh chat/ping + ble chat")
        stop.set()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
