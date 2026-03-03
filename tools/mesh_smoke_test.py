#!/usr/bin/env python
from __future__ import annotations

import argparse
import base64
import hashlib
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


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
            state.ser.write(wire)
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
        long_text = "LTXT-" + ("0123456789abcdef" * 65)
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
                and to_int(ev.get("seq", -1), -1) == seq
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
