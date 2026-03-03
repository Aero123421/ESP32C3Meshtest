#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import time

import serial


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.5, write_timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(1.8)
        ser.reset_input_buffer()
        wire = (json.dumps({"type": "nodes_request", "src": "pc"}) + "\n").encode("utf-8")
        ser.write(wire)
        ser.flush()

        end = time.time() + args.timeout
        while time.time() < end:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "node_list":
                nodes = payload.get("nodes", [])
                print(json.dumps(payload, ensure_ascii=False))
                print(f"count={len(nodes)}")
                return 0
    print("NO_NODE_LIST")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
