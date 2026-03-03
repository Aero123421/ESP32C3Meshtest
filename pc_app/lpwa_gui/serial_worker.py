from __future__ import annotations

import queue
import threading
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
        self._tx_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._read_timeout = read_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="serial-worker", daemon=True)
        self._thread.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=join_timeout)

    def send(self, payload: dict[str, Any]) -> None:
        self._tx_queue.put(payload)

    def _emit(self, event: dict[str, Any]) -> None:
        self._incoming_queue.put(event)

    def _drain_tx(self, ser: "serial.Serial") -> None:
        while True:
            try:
                payload = self._tx_queue.get_nowait()
            except queue.Empty:
                break

            try:
                encoded = encode_json_line(payload)
                ser.write(encoded)
                ser.flush()
                self._emit({"_event": "tx", "payload": payload})
            except (SerialException, OSError) as exc:
                self._emit({"_event": "error", "message": f"送信失敗: {exc}"})
                break
            except ProtocolError as exc:
                self._emit({"_event": "error", "message": f"JSON変換失敗: {exc}"})

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
                self._drain_tx(ser)

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
