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
    ap.add_argument("--watch", type=float, default=12.0)
    args = ap.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.2, write_timeout=0.5) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(2.0)
        ser.reset_input_buffer()
        payload = {"type": "chat", "via": "wifi", "text": "raw-watch-test", "src": "pc"}
        wire = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        ser.write(wire)
        ser.flush()
        print(f"TX: {payload}")

        end = time.time() + args.watch
        while time.time() < end:
            line = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

