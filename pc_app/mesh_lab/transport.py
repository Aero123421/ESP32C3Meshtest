"""Bounded JSONL serial transport; no UI or platform-specific dependencies."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import Any


class LineBuffer:
    """Preserve fragmented UTF-8 and drop an oversized line through its newline."""
    def __init__(self, limit: int = 65536) -> None:
        self.limit = limit
        self.buffer = bytearray()
        self.discarding = False
        self.dropped = 0

    def feed(self, data: bytes) -> list[bytes]:
        lines = []
        for part in data.splitlines(keepends=True):
            # splitlines also treats CR as a delimiter; only LF completes JSONL.
            if self.discarding:
                if part.endswith(b'\n'):
                    self.discarding = False
                continue
            self.buffer.extend(part)
            if len(self.buffer) > self.limit:
                self.buffer.clear()
                self.dropped += 1
                self.discarding = not part.endswith(b'\n')
                continue
            if part.endswith(b'\n'):
                lines.append(bytes(self.buffer).rstrip(b'\r\n'))
                self.buffer.clear()
        return lines


def serial_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    result = []
    seen = set()
    ports = list(list_ports.comports())
    devices = {p.device for p in ports}
    for p in ports:
        device = p.device
        if sys.platform == 'darwin' and device.startswith('/dev/tty.'):
            alternative = device.replace('/dev/tty.', '/dev/cu.', 1)
            if alternative in devices:
                continue
        if device not in seen:
            result.append({'device': device, 'description': p.description or device})
            seen.add(device)
    return sorted(result, key=lambda p: p['device'])


class SerialLink:
    """Exactly one owning thread. Writes are never blindly replayed after a partial write."""
    def __init__(self, port: str, events: queue.Queue, generation: str) -> None:
        self.port, self.events, self.generation = port, events, generation
        self.outgoing: queue.Queue[dict] = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name='mesh-serial')
        self.dropped_events = 0

    def start(self) -> None:
        self.thread.start()

    def send(self, payload: dict) -> bool:
        if self.stop_event.is_set() or not self.thread.is_alive():
            return False
        try:
            self.outgoing.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError('Serial worker did not stop; do not open the port again.')

    def emit(self, kind: str, **fields: Any) -> None:
        event = {'kind': kind, 'generation': self.generation, **fields}
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.dropped_events += 1

    def _run(self) -> None:
        ser = None
        try:
            import serial
            ser = serial.Serial(port=None, baudrate=115200, timeout=0.025, write_timeout=1)
            ser.dtr = False
            ser.rts = False
            if sys.platform != 'win32':
                ser.exclusive = True
            ser.port = self.port
            ser.open()
            framer = LineBuffer()
            self.emit('connected', port=self.port)
            while not self.stop_event.is_set():
                try:
                    payload = self.outgoing.get_nowait()
                except queue.Empty:
                    payload = None
                if payload is not None:
                    raw = (json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                      separators=(',', ':')) + '\n').encode('utf-8')
                    offset = 0
                    while offset < len(raw) and not self.stop_event.is_set():
                        n = ser.write(raw[offset:offset + 128])
                        if not n:
                            raise OSError('Serial write made no progress')
                        offset += n
                    if offset != len(raw):
                        break
                    self.emit('tx', payload=payload)
                raw = ser.read(min(max(1, ser.in_waiting), 4096))
                before = framer.dropped
                for line in framer.feed(raw):
                    if not line:
                        continue
                    try:
                        value = json.loads(line.decode('utf-8'))
                        json.dumps(value, allow_nan=False)  # reject NaN/Infinity, including 1e999
                        if not isinstance(value, dict):
                            raise ValueError('Expected a JSON object')
                        self.emit('rx', payload=value)
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        self.emit('raw', text=line.decode('utf-8', errors='replace')[:1000], error=str(exc))
                if framer.dropped != before:
                    self.emit('error', detail='Oversized serial line discarded')
        except Exception as exc:
            self.emit('error', detail=f'{type(exc).__name__}: {exc}')
        finally:
            if ser is not None:
                try:
                    ser.close()
                except OSError:
                    pass
            self.emit('disconnected', port=self.port, dropped_events=self.dropped_events)
