#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import time

import serial


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--min-count", type=int, default=2)
    args = ap.parse_args()

    req = (json.dumps({"type": "nodes_request", "src": "pc"}) + "\n").encode("utf-8")
    with serial.Serial(args.port, args.baud, timeout=0.4, write_timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(2.0)
        ser.reset_input_buffer()

        end = time.time() + args.timeout
        last_send = 0.0
        best_count = 0
        while time.time() < end:
            if (time.time() - last_send) > 2.0:
                ser.write(req)
                ser.flush()
                last_send = time.time()

            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "node_list":
                continue
            nodes = payload.get("nodes", [])
            if isinstance(nodes, list):
                count = len(nodes)
                best_count = max(best_count, count)
                if count >= args.min_count:
                    print(json.dumps(payload, ensure_ascii=False))
                    print(f"count={count}")
                    return 0
        print(f"NO_NODE_LIST_MIN_COUNT best={best_count}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

