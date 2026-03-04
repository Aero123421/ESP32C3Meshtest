from __future__ import annotations

import queue
import threading
import time
from typing import Any

from .protocol import ProtocolError, decode_json_line, encode_json_line

try:
    import serial
    from serial import SerialException
except Exception:  # pragma: no cover - pyserial not installed
    serial = None

    class SerialException(Exception):
        pass


def list_serial_ports() -> list[str]:
    if serial is None:
        return []
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return sorted(port.device for port in list_ports.comports())


class SerialWorker:
    def __init__(
        self,
        port: str,
        baudrate: int,
        incoming_queue: queue.Queue[dict[str, Any]],
        *,
        read_timeout: float = 0.1,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._incoming_queue = incoming_queue
        self._tx_queue_max = 1024
        self._tx_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._tx_queue_max)
        self._read_timeout = read_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tx_dropped = 0
        self._tx_batch_per_tick = 2
        self._worker_id = f"sw-{time.time_ns()}-{id(self)}"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def tx_queue_size(self) -> int:
        return int(self._tx_queue.qsize())

    @property
    def tx_queue_max(self) -> int:
        return int(self._tx_queue_max)

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="serial-worker", daemon=True)
        self._thread.start()

    def _clear_tx_queue(self) -> int:
        dropped = 0
        while True:
            try:
                self._tx_queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        return dropped

    def stop(self, *, join_timeout: float = 2.0, drop_pending: bool = True) -> None:
        self._stop_event.set()
        if drop_pending:
            dropped = self._clear_tx_queue()
            if dropped > 0:
                self._emit({"_event": "error", "message": f"切断により未送信キューを破棄: {dropped}件"})
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)

    def send(self, payload: dict[str, Any]) -> bool:
        try:
            self._tx_queue.put_nowait(payload)
            return True
        except queue.Full:
            self._tx_dropped += 1
            self._emit(
                {
                    "_event": "error",
                    "message": (
                        f"送信キュー満杯: 新規送信要求を破棄しました "
                        f"(dropped={self._tx_dropped}, max={self._tx_queue_max})"
                    ),
                }
            )
            return False

    def _emit(self, event: dict[str, Any]) -> None:
        emitted = dict(event)
        emitted["_worker_id"] = self._worker_id
        self._incoming_queue.put(emitted)

    def _write_all(self, ser: "serial.Serial", encoded: bytes) -> None:
        view = memoryview(encoded)
        offset = 0
        chunk_size = 64
        while offset < len(view):
            if self._stop_event.is_set():
                raise SerialException("serial worker stopped")
            end = offset + chunk_size
            if end > len(view):
                end = len(view)
            written = ser.write(view[offset:end])
            if written is None:
                written = 0
            if written <= 0:
                raise SerialException("serial write returned 0 bytes")
            offset += written
            if offset < len(view):
                time.sleep(0.001)
        ser.flush()

    def _drain_tx(self, ser: "serial.Serial", *, max_items: int) -> bool:
        sent_count = 0
        while sent_count < max_items and not self._stop_event.is_set():
            try:
                payload = self._tx_queue.get_nowait()
            except queue.Empty:
                break

            try:
                encoded = encode_json_line(payload)
                last_error: Exception | None = None
                sent = False
                for attempt in range(3):
                    try:
                        self._write_all(ser, encoded)
                        sent = True
                        break
                    except (SerialException, OSError) as exc:
                        last_error = exc
                        try:
                            ser.reset_output_buffer()
                        except Exception:
                            pass
                        time.sleep(0.05 * (attempt + 1))
                if not sent:
                    if last_error is None:
                        raise SerialException("serial write failed")
                    raise last_error
                self._emit({"_event": "tx", "payload": payload})
                sent_count += 1
            except (SerialException, OSError) as exc:
                if self._stop_event.is_set():
                    return False
                self._emit({"_event": "error", "message": f"送信失敗: {exc}"})
                return False
            except ProtocolError as exc:
                self._emit({"_event": "error", "message": f"JSON変換失敗: {exc}"})
        return True

    def _run(self) -> None:
        if serial is None:
            self._emit({"_event": "error", "message": "pyserial が見つかりません。pip install -r requirements.txt を実行してください。"})
            self._emit({"_event": "status", "status": "disconnected"})
            return

        ser: "serial.Serial" | None = None
        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self._read_timeout,
                write_timeout=0.5,
            )
            self._emit(
                {
                    "_event": "status",
                    "status": "connected",
                    "port": self.port,
                    "baudrate": self.baudrate,
                }
            )

            while not self._stop_event.is_set():
                if not self._drain_tx(ser, max_items=max(1, int(self._tx_batch_per_tick))):
                    break
                if self._stop_event.is_set():
                    break

                try:
                    raw = ser.readline()
                except (SerialException, OSError) as exc:
                    self._emit({"_event": "error", "message": f"受信失敗: {exc}"})
                    break

                if not raw:
                    continue

                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    continue

                try:
                    payload = decode_json_line(text)
                    self._emit({"_event": "rx", "payload": payload, "raw": text})
                except ProtocolError as exc:
                    self._emit({"_event": "rx_raw", "raw": text, "error": str(exc)})

        except (SerialException, OSError) as exc:
            self._emit({"_event": "error", "message": f"シリアル接続失敗 ({self.port}): {exc}"})
        except Exception as exc:
            self._emit({"_event": "error", "message": f"予期しない例外: {exc}"})
        finally:
            if ser is not None:
                try:
                    if ser.is_open:
                        ser.close()
                except Exception:
                    pass
            self._emit({"_event": "status", "status": "disconnected", "port": self.port})
