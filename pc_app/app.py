from __future__ import annotations

import base64
import hashlib
import queue
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from lpwa_gui.models import NodeInfo, NodeRegistry
from lpwa_gui.protocol import (
    make_chat_message,
    make_image_messages,
    make_nodes_request,
    make_ping_message,
)
from lpwa_gui.serial_worker import SerialWorker, list_serial_ports
from lpwa_gui.stats import PingStats


def _format_seen_time(seen_ms: int) -> str:
    if seen_ms <= 0:
        return "-"
    if seen_ms > 10_000_000_000:
        return datetime.fromtimestamp(seen_ms / 1000.0).strftime("%H:%M:%S")
    return f"{seen_ms} ms"


def _to_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except (AttributeError, ValueError):
        return default


class LPWAApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LPWA Test PC App")
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.worker: SerialWorker | None = None
        self.incoming_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.registry = NodeRegistry()
        self.ping_stats = PingStats()
        self.ping_seq = 0
        self.current_ping_id = uuid.uuid4().hex[:8]
        self.continuous_after_id: str | None = None
        self.continuous_remaining: int | None = None
        self.log_lines: list[str] = []
        self.max_log_lines = 3000
        self.image_rx_sessions: dict[str, dict[str, Any]] = {}

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.connection_var = tk.StringVar(value="未接続")
        self.chat_target_var = tk.StringVar()
        self.chat_input_var = tk.StringVar()
        self.image_target_var = tk.StringVar()
        self.image_path_var = tk.StringVar()
        self.ping_target_var = tk.StringVar()
        self.interval_var = tk.StringVar(value="1000")
        self.count_var = tk.StringVar(value="0")
        self.chat_via_var = tk.StringVar(value="wifi")

        self.sent_var = tk.StringVar(value="0")
        self.received_var = tk.StringVar(value="0")
        self.lost_var = tk.StringVar(value="0")
        self.pdr_var = tk.StringVar(value="0.0%")
        self.avg_var = tk.StringVar(value="0.0 ms")
        self.min_var = tk.StringVar(value="0.0 ms")
        self.max_var = tk.StringVar(value="0.0 ms")
        self.p95_var = tk.StringVar(value="0.0 ms")

        self._build_ui()
        self.refresh_ports()
        self.after(100, self.poll_worker_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(self, text="COM接続")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(8, weight=1)

        ttk.Label(top, text="ポート").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=16, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Button(top, text="更新", command=self.refresh_ports).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(top, text="Baud").grid(row=0, column=3, padx=4, pady=4, sticky="w")
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=4, pady=4)

        self.connect_button = ttk.Button(top, text="接続", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=5, padx=4, pady=4)
        ttk.Button(top, text="ノード要求", command=self.request_nodes).grid(row=0, column=6, padx=4, pady=4)

        ttk.Label(top, text="状態").grid(row=0, column=7, padx=4, pady=4, sticky="e")
        ttk.Label(top, textvariable=self.connection_var).grid(row=0, column=8, padx=4, pady=4, sticky="w")

        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        left = ttk.Frame(body)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)
        left.rowconfigure(2, weight=2)

        nodes_frame = ttk.LabelFrame(left, text="ノード一覧")
        nodes_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        nodes_frame.columnconfigure(0, weight=1)
        nodes_frame.rowconfigure(0, weight=1)
        self.node_tree = ttk.Treeview(
            nodes_frame,
            columns=("id", "rssi", "ping", "seen", "msg"),
            show="headings",
            height=12,
        )
        for key, title, width in (
            ("id", "Node", 150),
            ("rssi", "RSSI", 70),
            ("ping", "Ping(ms)", 80),
            ("seen", "Last Seen", 90),
            ("msg", "Last Msg", 260),
        ):
            self.node_tree.heading(key, text=title)
            self.node_tree.column(key, width=width, anchor="w")
        self.node_tree.grid(row=0, column=0, sticky="nsew")
        node_scroll = ttk.Scrollbar(nodes_frame, orient=tk.VERTICAL, command=self.node_tree.yview)
        node_scroll.grid(row=0, column=1, sticky="ns")
        self.node_tree.configure(yscrollcommand=node_scroll.set)

        ping_frame = ttk.LabelFrame(left, text="Ping / 連続試験")
        ping_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        ping_frame.columnconfigure(1, weight=1)
        ttk.Label(ping_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.ping_target_var).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(ping_frame, text="Ping送信", command=self.send_ping).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(ping_frame, text="間隔(ms)").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.interval_var, width=12).grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="回数(0=無限)").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.count_var, width=12).grid(row=2, column=1, padx=4, pady=4, sticky="w")
        self.start_test_btn = ttk.Button(ping_frame, text="連続開始", command=self.start_continuous_ping)
        self.start_test_btn.grid(row=1, column=2, padx=4, pady=4)
        self.stop_test_btn = ttk.Button(ping_frame, text="停止", command=self.stop_continuous_ping, state=tk.DISABLED)
        self.stop_test_btn.grid(row=2, column=2, padx=4, pady=4)

        stats_frame = ttk.LabelFrame(left, text="PDR / 遅延統計")
        stats_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        for idx in range(4):
            stats_frame.columnconfigure(idx, weight=1)
        ttk.Label(stats_frame, text="Sent").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.sent_var).grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, text="Received").grid(row=0, column=2, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.received_var).grid(row=0, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(stats_frame, text="Lost").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.lost_var).grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, text="PDR").grid(row=1, column=2, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.pdr_var).grid(row=1, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(stats_frame, text="Avg").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.avg_var).grid(row=2, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, text="Min").grid(row=2, column=2, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.min_var).grid(row=2, column=3, padx=4, pady=4, sticky="w")

        ttk.Label(stats_frame, text="Max").grid(row=3, column=0, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.max_var).grid(row=3, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, text="P95").grid(row=3, column=2, padx=4, pady=4, sticky="w")
        ttk.Label(stats_frame, textvariable=self.p95_var).grid(row=3, column=3, padx=4, pady=4, sticky="w")

        ttk.Button(stats_frame, text="統計リセット", command=self.reset_stats).grid(
            row=4, column=0, columnspan=4, padx=4, pady=6, sticky="e"
        )

        right = ttk.Frame(body)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=3)

        chat_frame = ttk.LabelFrame(right, text="チャット")
        chat_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        chat_frame.columnconfigure(1, weight=1)
        chat_frame.rowconfigure(1, weight=1)
        ttk.Label(chat_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(chat_frame, textvariable=self.chat_target_var, width=18).grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(chat_frame, text="経路").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        ttk.Combobox(
            chat_frame,
            textvariable=self.chat_via_var,
            values=("wifi", "ble"),
            width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=4, pady=4, sticky="w")
        self.chat_history = ScrolledText(chat_frame, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_history.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=4, pady=4)
        chat_entry = ttk.Entry(chat_frame, textvariable=self.chat_input_var)
        chat_entry.grid(row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=4)
        chat_entry.bind("<Return>", lambda _: self.send_chat())
        ttk.Button(chat_frame, text="送信", command=self.send_chat).grid(row=2, column=3, padx=4, pady=4)

        image_frame = ttk.LabelFrame(right, text="画像送信")
        image_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        image_frame.columnconfigure(1, weight=1)
        ttk.Label(image_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(image_frame, textvariable=self.image_target_var, width=18).grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(image_frame, text="ファイル").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(image_frame, textvariable=self.image_path_var).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(image_frame, text="参照", command=self.browse_image).grid(row=1, column=2, padx=4, pady=4)
        ttk.Button(image_frame, text="画像送信", command=self.send_image).grid(row=2, column=2, padx=4, pady=4, sticky="e")

        log_frame = ttk.LabelFrame(right, text="イベントログ")
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(log_frame, height=10, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)
        ttk.Button(log_frame, text="ログ保存", command=self.save_logs).grid(row=1, column=1, padx=4, pady=4, sticky="e")
        ttk.Button(log_frame, text="クリア", command=self.clear_logs).grid(row=1, column=2, padx=4, pady=4, sticky="e")

        body.add(left, weight=1)
        body.add(right, weight=1)

    def append_log(self, text: str) -> None:
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        self.log_lines.append(stamped)
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines :]

        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, stamped + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def append_chat(self, text: str) -> None:
        self.chat_history.configure(state=tk.NORMAL)
        self.chat_history.insert(tk.END, text + "\n")
        self.chat_history.see(tk.END)
        self.chat_history.configure(state=tk.DISABLED)

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if ports and (self.port_var.get() not in ports):
            self.port_var.set(ports[0])
        self.append_log(f"COM一覧更新: {ports if ports else 'なし'}")

    def toggle_connection(self) -> None:
        if self.worker and self.worker.is_running:
            self.disconnect_serial()
        else:
            self.connect_serial()

    def connect_serial(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("入力不足", "COMポートを選択してください。")
            return
        baud = _to_int(self.baud_var.get(), 115200)
        if baud <= 0:
            messagebox.showwarning("入力不正", "Baudは正の整数を指定してください。")
            return

        self.worker = SerialWorker(port=port, baudrate=baud, incoming_queue=self.incoming_queue)
        self.worker.start()
        self.connect_button.configure(state=tk.DISABLED)
        self.connection_var.set("接続処理中")
        self.append_log(f"接続開始: {port} @ {baud}")

    def disconnect_serial(self) -> None:
        self.stop_continuous_ping()
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.connection_var.set("未接続")
        self.connect_button.configure(text="接続", state=tk.NORMAL)
        self.append_log("切断しました。")

    def poll_worker_events(self) -> None:
        while True:
            try:
                event = self.incoming_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_worker_event(event)
        self.after(100, self.poll_worker_events)

    def handle_worker_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("_event")
        if event_type == "status":
            status = event.get("status")
            if status == "connected":
                self.connection_var.set(f"接続中: {event.get('port')}")
                self.connect_button.configure(text="切断", state=tk.NORMAL)
                self.append_log(f"接続成功: {event.get('port')} @ {event.get('baudrate')}")
            elif status == "disconnected":
                self.connection_var.set("未接続")
                self.connect_button.configure(text="接続", state=tk.NORMAL)
                self.stop_continuous_ping()
                if self.worker and not self.worker.is_running:
                    self.worker = None
                self.append_log("シリアル接続が切断されました。")
            return

        if event_type == "error":
            self.append_log(f"ERROR: {event.get('message')}")
            self.connect_button.configure(state=tk.NORMAL)
            return

        if event_type == "tx":
            self.append_log(f"TX: {event.get('payload')}")
            return

        if event_type == "rx":
            payload = event.get("payload")
            if isinstance(payload, dict):
                self.append_log(f"RX: {payload}")
                self.handle_payload(payload)
            else:
                self.append_log(f"RX(不正): {payload}")
            return

        if event_type == "rx_raw":
            self.append_log(f"RX RAW: {event.get('raw')} ({event.get('error', 'parse error')})")
            return

        self.append_log(f"未知イベント: {event}")

    def handle_payload(self, payload: dict[str, Any]) -> None:
        message_type = str(payload.get("type") or payload.get("event") or "").strip().lower()

        if message_type in {"node_list", "nodes"}:
            nodes = payload.get("nodes") or payload.get("items")
            if isinstance(nodes, list):
                self.registry.update_from_list(nodes)
                self.refresh_node_table()
            return

        maybe_node = self.registry.upsert_from_payload(payload)
        if maybe_node is not None:
            self.refresh_node_table()

        if message_type == "chat":
            src = str(payload.get("src") or payload.get("from") or payload.get("origin") or "?")
            text = str(payload.get("text", ""))
            via = str(payload.get("via", "wifi"))
            self.append_chat(f"{src}({via}): {text}")
            return

        if message_type in {"pong", "ping_reply"}:
            self.handle_pong(payload)
            return

        if message_type in {"image_start", "image_chunk", "image_end"}:
            self.handle_image_payload(payload)
            return

        if message_type == "binary":
            data_b64 = str(payload.get("data_b64", ""))
            self.append_log(f"バイナリ受信: {len(data_b64)} chars(base64)")
            return

        if message_type == "ack":
            self.append_log(f"ACK: cmd={payload.get('cmd')} ok={payload.get('ok')} via={payload.get('via')}")
            return

        if message_type == "error":
            self.append_log(f"FW ERROR: {payload.get('code')} {payload.get('detail')}")
            return

    def handle_image_payload(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", "")).strip().lower()
        image_id = str(payload.get("image_id", "")).strip()
        if not image_id:
            return

        if kind == "image_start":
            self.image_rx_sessions[image_id] = {
                "name": str(payload.get("name") or f"{image_id}.bin"),
                "chunks": _to_int(str(payload.get("chunks", "0")), 0),
                "sha256": str(payload.get("sha256", "")).strip().lower(),
                "parts": {},
                "src": str(payload.get("src", "?")),
            }
            self.append_log(f"画像受信開始: id={image_id} name={self.image_rx_sessions[image_id]['name']}")
            return

        if kind == "image_chunk":
            session = self.image_rx_sessions.setdefault(
                image_id, {"name": f"{image_id}.bin", "chunks": 0, "sha256": "", "parts": {}, "src": "?"}
            )
            idx = payload.get("index")
            if not isinstance(idx, int):
                idx = _to_int(str(idx), -1)
            if idx < 0:
                return
            chunk_b64 = payload.get("data_b64")
            if not isinstance(chunk_b64, str) or not chunk_b64:
                return
            try:
                chunk = base64.b64decode(chunk_b64, validate=True)
            except Exception:
                self.append_log(f"画像チャンク破損: id={image_id} index={idx}")
                return
            session["parts"][idx] = chunk
            return

        if kind == "image_end":
            session = self.image_rx_sessions.pop(image_id, None)
            if session is None:
                return
            parts: dict[int, bytes] = session.get("parts", {})
            if not parts:
                self.append_log(f"画像受信終了(空): id={image_id}")
                return

            ordered = [parts[i] for i in sorted(parts.keys())]
            merged = b"".join(ordered)

            expected_hash = str(session.get("sha256", "")).strip().lower()
            actual_hash = hashlib.sha256(merged).hexdigest()
            hash_ok = (not expected_hash) or (expected_hash == actual_hash)

            recv_dir = Path.cwd() / "received_images"
            recv_dir.mkdir(parents=True, exist_ok=True)
            name = str(session.get("name", f"{image_id}.bin")).strip() or f"{image_id}.bin"
            safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
            out_path = recv_dir / safe_name
            out_path.write_bytes(merged)

            self.append_log(
                f"画像保存: {out_path} bytes={len(merged)} chunks={len(parts)} hash_ok={hash_ok} src={session.get('src')}"
            )
            return

    def refresh_node_table(self) -> None:
        for item in self.node_tree.get_children():
            self.node_tree.delete(item)

        for node in self.registry.snapshot():
            self.node_tree.insert(
                "",
                tk.END,
                values=(
                    node.node_id,
                    "-" if node.rssi is None else str(node.rssi),
                    "-" if node.ping_ms is None else f"{node.ping_ms:.1f}",
                    _format_seen_time(node.last_seen_ms),
                    node.last_message,
                ),
            )

    def send_json(self, payload: dict[str, Any]) -> bool:
        worker = self.worker
        if worker is None or not worker.is_running:
            messagebox.showwarning("未接続", "先にCOMポートへ接続してください。")
            return False
        worker.send(payload)
        return True

    def send_chat(self) -> None:
        text = self.chat_input_var.get().strip()
        if not text:
            return
        dst = self.chat_target_var.get().strip() or None
        via = self.chat_via_var.get().strip() or "wifi"
        payload = make_chat_message(text=text, dst=dst, via=via)
        if not self.send_json(payload):
            return
        shown_dst = dst if dst else "broadcast"
        self.append_chat(f"me({via}) -> {shown_dst}: {text}")
        self.chat_input_var.set("")

    def browse_image(self) -> None:
        selected = filedialog.askopenfilename(
            title="送信する画像を選択",
            filetypes=[
                ("Image files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"),
                ("All files", "*.*"),
            ],
        )
        if selected:
            self.image_path_var.set(selected)

    def send_image(self) -> None:
        raw_path = self.image_path_var.get().strip()
        if not raw_path:
            messagebox.showwarning("入力不足", "画像ファイルを指定してください。")
            return
        path = Path(raw_path)
        if not path.exists():
            messagebox.showerror("ファイルなし", f"ファイルが存在しません: {path}")
            return

        dst = self.image_target_var.get().strip() or None
        try:
            messages = make_image_messages(path=path, dst=dst, via="wifi")
        except Exception as exc:
            messagebox.showerror("画像送信エラー", str(exc))
            return

        for payload in messages:
            if not self.send_json(payload):
                return

        chunk_count = max(0, len(messages) - 2)
        self.append_log(f"画像送信キュー投入: {path.name} ({chunk_count} chunk)")

    def request_nodes(self) -> None:
        self.send_json(make_nodes_request())

    def send_ping(self) -> bool:
        self.ping_seq += 1
        seq = self.ping_seq
        dst = self.ping_target_var.get().strip() or None
        payload = make_ping_message(seq=seq, dst=dst, ping_id=self.current_ping_id, via="wifi")
        if not self.send_json(payload):
            return False
        self.ping_stats.register_sent(seq)
        self.update_stats_view()
        return True

    def handle_pong(self, payload: dict[str, Any]) -> None:
        seq_raw = payload.get("seq")
        seq: int | None
        if isinstance(seq_raw, int):
            seq = seq_raw
        elif isinstance(seq_raw, str) and seq_raw.strip().isdigit():
            seq = int(seq_raw.strip())
        else:
            seq = None

        if seq is None:
            return

        latency_raw = payload.get("latency_ms")
        latency: float | None
        if isinstance(latency_raw, (int, float)):
            latency = float(latency_raw)
        elif isinstance(latency_raw, str):
            try:
                latency = float(latency_raw.strip())
            except ValueError:
                latency = None
        else:
            latency = None

        measured = self.ping_stats.register_received(seq, latency_ms=latency)
        if measured is not None:
            src = payload.get("src") or payload.get("from")
            if isinstance(src, str) and src.strip():
                self.registry.upsert_from_payload({"node_id": src, "latency_ms": measured})
                self.refresh_node_table()

        self.update_stats_view()

    def start_continuous_ping(self) -> None:
        if self.continuous_after_id is not None:
            return
        interval_ms = max(1, _to_int(self.interval_var.get(), 1000))
        count = max(0, _to_int(self.count_var.get(), 0))
        self.continuous_remaining = count if count > 0 else None
        self.start_test_btn.configure(state=tk.DISABLED)
        self.stop_test_btn.configure(state=tk.NORMAL)
        self.append_log(
            f"連続Ping開始: interval={interval_ms}ms, count={'∞' if self.continuous_remaining is None else self.continuous_remaining}"
        )
        self._run_continuous_ping(interval_ms)

    def _run_continuous_ping(self, interval_ms: int) -> None:
        if not self.send_ping():
            self.stop_continuous_ping()
            return
        if self.continuous_remaining is not None:
            self.continuous_remaining -= 1
            if self.continuous_remaining <= 0:
                self.stop_continuous_ping()
                return
        self.continuous_after_id = self.after(interval_ms, lambda: self._run_continuous_ping(interval_ms))

    def stop_continuous_ping(self) -> None:
        if self.continuous_after_id is not None:
            self.after_cancel(self.continuous_after_id)
            self.continuous_after_id = None
            self.append_log("連続Ping停止")
        self.start_test_btn.configure(state=tk.NORMAL)
        self.stop_test_btn.configure(state=tk.DISABLED)
        self.continuous_remaining = None

    def reset_stats(self) -> None:
        self.ping_stats.reset()
        self.current_ping_id = uuid.uuid4().hex[:8]
        self.update_stats_view()
        self.append_log("統計情報をリセットしました。")

    def update_stats_view(self) -> None:
        snapshot = self.ping_stats.snapshot()
        self.sent_var.set(str(snapshot["sent"]))
        self.received_var.set(str(snapshot["received"]))
        self.lost_var.set(str(snapshot["lost"]))
        self.pdr_var.set(f"{snapshot['pdr']:.1f}%")
        self.avg_var.set(f"{snapshot['avg_ms']:.1f} ms")
        self.min_var.set(f"{snapshot['min_ms']:.1f} ms")
        self.max_var.set(f"{snapshot['max_ms']:.1f} ms")
        self.p95_var.set(f"{snapshot['p95_ms']:.1f} ms")

    def save_logs(self) -> None:
        if not self.log_lines:
            messagebox.showinfo("ログなし", "保存対象のログがありません。")
            return
        path = filedialog.asksaveasfilename(
            title="ログ保存",
            defaultextension=".log",
            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            self.append_log(f"ログ保存完了: {path}")
        except OSError as exc:
            messagebox.showerror("保存失敗", str(exc))

    def clear_logs(self) -> None:
        self.log_lines.clear()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_close(self) -> None:
        self.stop_continuous_ping()
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.destroy()


def main() -> None:
    app = LPWAApp()
    app.mainloop()


if __name__ == "__main__":
    main()
