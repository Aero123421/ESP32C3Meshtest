from __future__ import annotations

import base64
import hashlib
import json
import math
import queue
import shutil
import subprocess
import threading
import time
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
    make_long_text_messages,
    make_nodes_request,
    make_ping_message,
)
from lpwa_gui.serial_worker import SerialWorker, list_serial_ports
from lpwa_gui.stats import PingStats
from lpwa_gui.topology import BROADCAST_NODE, TopologySnapshot, TopologyTracker


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


BROADCAST_LABEL = "(broadcast)"
LOG_TAGS = ("INFO", "WARN", "ERROR", "TX", "RX", "SYSTEM")
LOG_PREVIEW_MAX = 220
E2E_ACK_TIMEOUT_MS = 2200
E2E_ACK_MAX_RETRIES = 4
E2E_RX_DEDUP_WINDOW_MS = 60000
LONG_TEXT_RX_DEDUP_WINDOW_MS = 120000
LONG_TEXT_AUTO_SPLIT_BYTES = 700
LONG_TEXT_CHUNK_BYTES = 32
RX_SESSION_TIMEOUT_MS = 30000
MAX_RX_SESSIONS = 24
MAX_CHAT_LINES = 1200
TOPOLOGY_REDRAW_INTERVAL_MS = 400
TOPOLOGY_DEFAULT_WINDOW_SEC = 30
MAX_WORKER_EVENTS_PER_TICK = 120
WORKER_BACKLOG_LOG_INTERVAL_MS = 1200
PING_PENDING_MAX_AGE_MS = 20000


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
        self.pending_ping_ids: dict[int, str] = {}
        self.pending_ping_sent_ms: dict[int, int] = {}
        self.pending_e2e: dict[str, dict[str, Any]] = {}
        self.rx_seen_e2e: dict[str, int] = {}
        self.long_text_seen: dict[str, int] = {}
        self.continuous_after_id: str | None = None
        self.continuous_remaining: int | None = None
        self.log_lines: list[str] = []
        self.max_log_lines = 3000
        self.image_rx_sessions: dict[str, dict[str, Any]] = {}
        self.long_text_rx_sessions: dict[str, dict[str, Any]] = {}
        self.flash_busy = False
        self.flash_thread: threading.Thread | None = None
        self.project_root = Path(__file__).resolve().parents[1]
        self.local_node_id: str | None = None
        self.topology_tracker = TopologyTracker(max_events=20000)
        self.topology_dirty = False
        self._last_worker_backlog_log_ms = 0

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.connection_var = tk.StringVar(value="未接続")
        self.flash_status_var = tk.StringVar(value="Idle")
        self.pio_env_var = tk.StringVar(value="seeed_xiao_esp32c3")
        self.chat_target_var = tk.StringVar(value=BROADCAST_LABEL)
        self.chat_input_var = tk.StringVar()
        self.image_target_var = tk.StringVar(value=BROADCAST_LABEL)
        self.image_path_var = tk.StringVar()
        self.ping_target_var = tk.StringVar(value=BROADCAST_LABEL)
        self.selected_node_var = tk.StringVar(value="未選択")
        self.interval_var = tk.StringVar(value="1000")
        self.count_var = tk.StringVar(value="0")
        self.ttl_var = tk.StringVar(value="10")
        self.chat_via_var = tk.StringVar(value="wifi")
        self.topology_window_var = tk.StringVar(value=str(TOPOLOGY_DEFAULT_WINDOW_SEC))
        self.topology_via_var = tk.StringVar(value="all")
        self.topology_kind_var = tk.StringVar(value="all")
        self.topology_status_var = tk.StringVar(value="未更新")
        self.topology_broadcast_var = tk.BooleanVar(value=False)

        self.sent_var = tk.StringVar(value="0")
        self.received_var = tk.StringVar(value="0")
        self.lost_var = tk.StringVar(value="0")
        self.pdr_var = tk.StringVar(value="0.0%")
        self.avg_var = tk.StringVar(value="0.0 ms")
        self.min_var = tk.StringVar(value="0.0 ms")
        self.max_var = tk.StringVar(value="0.0 ms")
        self.p95_var = tk.StringVar(value="0.0 ms")

        self._build_ui()
        self.refresh_destination_choices()
        self.refresh_ports()
        self.after(100, self.poll_worker_events)
        self.after(TOPOLOGY_REDRAW_INTERVAL_MS, self.refresh_topology_view)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(self, text="COM接続")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(8, weight=1)
        top.columnconfigure(12, weight=1)

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

        ttk.Separator(top, orient=tk.HORIZONTAL).grid(row=1, column=0, columnspan=13, sticky="ew", pady=4)
        ttk.Label(top, text="FW Env").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(top, textvariable=self.pio_env_var, width=20).grid(row=2, column=1, padx=4, pady=4, sticky="w")

        self.build_fw_button = ttk.Button(top, text="Build", command=self.start_build_only)
        self.build_fw_button.grid(row=2, column=2, padx=4, pady=4)

        self.flash_selected_button = ttk.Button(top, text="書込(選択COM)", command=self.start_flash_selected_port)
        self.flash_selected_button.grid(row=2, column=3, padx=4, pady=4)

        self.flash_all_button = ttk.Button(top, text="書込(全COM)", command=self.start_flash_all_ports)
        self.flash_all_button.grid(row=2, column=4, padx=4, pady=4)

        ttk.Label(top, text="Flash状態").grid(row=2, column=5, padx=4, pady=4, sticky="e")
        ttk.Label(top, textvariable=self.flash_status_var).grid(row=2, column=6, columnspan=7, padx=4, pady=4, sticky="w")

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
        self.node_tree.bind("<<TreeviewSelect>>", self.on_node_tree_select)
        self.node_tree.bind("<Double-1>", self.apply_selected_node_to_targets)

        node_actions = ttk.Frame(nodes_frame)
        node_actions.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 2))
        node_actions.columnconfigure(1, weight=1)
        ttk.Label(node_actions, text="選択ノード").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(node_actions, textvariable=self.selected_node_var).grid(row=0, column=1, sticky="w")
        ttk.Button(node_actions, text="選択ノード→宛先", command=self.apply_selected_node_to_targets).grid(
            row=0, column=2, padx=4
        )
        ttk.Button(node_actions, text="宛先をBroadcast", command=self.set_broadcast_targets).grid(
            row=0, column=3, padx=(2, 0)
        )

        ping_frame = ttk.LabelFrame(left, text="Ping / 連続試験")
        ping_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        ping_frame.columnconfigure(1, weight=1)
        ttk.Label(ping_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.ping_target_combo = ttk.Combobox(ping_frame, textvariable=self.ping_target_var, state="readonly")
        self.ping_target_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(ping_frame, text="Ping送信", command=self.send_ping).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(ping_frame, text="間隔(ms)").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.interval_var, width=12).grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="回数(0=無限)").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.count_var, width=12).grid(row=2, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="TTL").grid(row=3, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(ping_frame, textvariable=self.ttl_var, width=12).grid(row=3, column=1, padx=4, pady=4, sticky="w")
        self.start_test_btn = ttk.Button(ping_frame, text="連続開始", command=self.start_continuous_ping)
        self.start_test_btn.grid(row=1, column=2, padx=4, pady=4)
        self.stop_test_btn = ttk.Button(ping_frame, text="停止", command=self.stop_continuous_ping, state=tk.DISABLED)
        self.stop_test_btn.grid(row=2, column=2, padx=4, pady=4)
        ttk.Label(ping_frame, text="(10ノード目安: 10-12)").grid(row=3, column=2, padx=4, pady=4, sticky="w")

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
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=2)
        right.rowconfigure(3, weight=3)

        chat_frame = ttk.LabelFrame(right, text="チャット")
        chat_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        chat_frame.columnconfigure(1, weight=1)
        chat_frame.rowconfigure(1, weight=1)
        ttk.Label(chat_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.chat_target_combo = ttk.Combobox(chat_frame, textvariable=self.chat_target_var, width=18, state="readonly")
        self.chat_target_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Label(chat_frame, text="経路").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        ttk.Combobox(
            chat_frame,
            textvariable=self.chat_via_var,
            values=("wifi", "ble"),
            width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=4, pady=4, sticky="w")
        ttk.Label(chat_frame, text=f"※ {BROADCAST_LABEL} で全体送信").grid(
            row=0, column=4, padx=(8, 4), pady=4, sticky="w"
        )
        self.chat_history = ScrolledText(chat_frame, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_history.grid(row=1, column=0, columnspan=5, sticky="nsew", padx=4, pady=4)
        chat_entry = ttk.Entry(chat_frame, textvariable=self.chat_input_var)
        chat_entry.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
        chat_entry.bind("<Return>", lambda _: self.send_chat())
        ttk.Button(chat_frame, text="送信", command=self.send_chat).grid(row=2, column=4, padx=4, pady=4)

        image_frame = ttk.LabelFrame(right, text="画像送信")
        image_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        image_frame.columnconfigure(1, weight=1)
        ttk.Label(image_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.image_target_combo = ttk.Combobox(image_frame, textvariable=self.image_target_var, width=18, state="readonly")
        self.image_target_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Label(image_frame, text="ファイル").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        ttk.Entry(image_frame, textvariable=self.image_path_var).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(image_frame, text="参照", command=self.browse_image).grid(row=1, column=2, padx=4, pady=4)
        ttk.Button(image_frame, text="画像送信", command=self.send_image).grid(row=2, column=2, padx=4, pady=4, sticky="e")

        log_frame = ttk.LabelFrame(right, text="イベントログ")
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(log_frame, height=10, state=tk.DISABLED, wrap=tk.NONE, font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=4, pady=(4, 2))
        log_x_scroll = ttk.Scrollbar(log_frame, orient=tk.HORIZONTAL, command=self.log_text.xview)
        log_x_scroll.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(0, 2))
        self.log_text.configure(xscrollcommand=log_x_scroll.set)
        self.log_text.tag_configure("INFO", foreground="#1f2937")
        self.log_text.tag_configure("WARN", foreground="#9a6700")
        self.log_text.tag_configure("ERROR", foreground="#b42318")
        self.log_text.tag_configure("TX", foreground="#0f5132")
        self.log_text.tag_configure("RX", foreground="#0c4a6e")
        self.log_text.tag_configure("SYSTEM", foreground="#4b5563")
        ttk.Button(log_frame, text="ログ保存", command=self.save_logs).grid(row=2, column=1, padx=4, pady=4, sticky="e")
        ttk.Button(log_frame, text="クリア", command=self.clear_logs).grid(row=2, column=2, padx=4, pady=4, sticky="e")

        topology_frame = ttk.LabelFrame(right, text="通信トポロジ (リアルタイム)")
        topology_frame.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        topology_frame.columnconfigure(0, weight=3)
        topology_frame.columnconfigure(1, weight=2)
        topology_frame.rowconfigure(1, weight=1)

        ctrl = ttk.Frame(topology_frame)
        ctrl.grid(row=0, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        ctrl.columnconfigure(10, weight=1)
        ttk.Label(ctrl, text="窓(sec)").grid(row=0, column=0, padx=2, sticky="w")
        self.topology_window_combo = ttk.Combobox(
            ctrl,
            textvariable=self.topology_window_var,
            values=("10", "30", "60", "120", "300"),
            width=6,
            state="readonly",
        )
        self.topology_window_combo.grid(row=0, column=1, padx=2, sticky="w")
        self.topology_window_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Label(ctrl, text="経路").grid(row=0, column=2, padx=(8, 2), sticky="w")
        self.topology_via_combo = ttk.Combobox(
            ctrl,
            textvariable=self.topology_via_var,
            values=("all", "wifi", "ble"),
            width=8,
            state="readonly",
        )
        self.topology_via_combo.grid(row=0, column=3, padx=2, sticky="w")
        self.topology_via_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Label(ctrl, text="種別").grid(row=0, column=4, padx=(8, 2), sticky="w")
        self.topology_kind_combo = ttk.Combobox(
            ctrl,
            textvariable=self.topology_kind_var,
            values=("all", "chat", "ping", "pong", "delivery_ack", "long_text", "image"),
            width=12,
            state="readonly",
        )
        self.topology_kind_combo.grid(row=0, column=5, padx=2, sticky="w")
        self.topology_kind_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Checkbutton(
            ctrl,
            text="Broadcast表示",
            variable=self.topology_broadcast_var,
            command=self._mark_topology_dirty,
        ).grid(row=0, column=6, padx=(8, 2), sticky="w")
        ttk.Button(ctrl, text="履歴クリア", command=self.clear_topology_history).grid(row=0, column=7, padx=(8, 2))
        ttk.Label(ctrl, textvariable=self.topology_status_var).grid(row=0, column=10, padx=4, sticky="e")

        self.topology_canvas = tk.Canvas(
            topology_frame,
            bg="#0b1220",
            highlightthickness=1,
            highlightbackground="#1f2937",
        )
        self.topology_canvas.grid(row=1, column=0, sticky="nsew", padx=(4, 2), pady=(0, 4))
        self.topology_canvas.bind("<Configure>", lambda _: self._mark_topology_dirty())

        self.topology_tree = ttk.Treeview(
            topology_frame,
            columns=("src", "dst", "via", "type", "count", "bytes", "hops", "retry", "rssi", "last"),
            show="headings",
            height=8,
        )
        for key, title, width in (
            ("src", "Src", 100),
            ("dst", "Dst", 100),
            ("via", "Via", 52),
            ("type", "Type", 90),
            ("count", "Count", 60),
            ("bytes", "Bytes", 70),
            ("hops", "Hops", 55),
            ("retry", "Retry", 55),
            ("rssi", "RSSI", 55),
            ("last", "Last", 78),
        ):
            self.topology_tree.heading(key, text=title)
            self.topology_tree.column(key, width=width, anchor="w")
        self.topology_tree.grid(row=1, column=1, sticky="nsew", padx=(2, 4), pady=(0, 4))

        body.add(left, weight=1)
        body.add(right, weight=1)

    def append_log(self, text: str, level: str = "INFO", category: str = "APP") -> None:
        level_tag = level.upper().strip()
        if level_tag not in LOG_TAGS:
            level_tag = "INFO"
        cat = category.upper().strip() or "APP"
        stamped = f"[{datetime.now().strftime('%H:%M:%S')}][{level_tag}][{cat}] {text}"
        self.log_lines.append(stamped)
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines :]

        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, stamped + "\n", (level_tag,))
        self._trim_log_widget()
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _trim_log_widget(self) -> None:
        line_count_str = self.log_text.index("end-1c").split(".")[0]
        try:
            line_count = int(line_count_str)
        except ValueError:
            return
        if line_count <= self.max_log_lines:
            return
        drop_count = line_count - self.max_log_lines
        self.log_text.delete("1.0", f"{drop_count + 1}.0")

    def _normalize_target(self, raw_value: str | None) -> str | None:
        value = (raw_value or "").strip()
        if not value:
            return None
        if value.lower() in {"*", "all", "broadcast", BROADCAST_LABEL.lower()}:
            return None
        return value

    def _target_label(self, target: str | None) -> str:
        return target if target else BROADCAST_LABEL

    def _current_ttl(self) -> int:
        raw = self.ttl_var.get().strip()
        value = _to_int(raw, 10)
        if value < 1:
            value = 1
        if value > 255:
            value = 255
        if str(value) != raw:
            self.ttl_var.set(str(value))
        return value

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _is_reliable_target(self, *, via: str, dst: str | None) -> bool:
        return via == "wifi" and dst is not None

    def _remember_rx_e2e(self, src: str, e2e_id: str) -> bool:
        if not src or not e2e_id:
            return False
        now = self._now_ms()
        cutoff = now - E2E_RX_DEDUP_WINDOW_MS
        stale_keys = [key for key, ts in self.rx_seen_e2e.items() if ts < cutoff]
        for key in stale_keys:
            self.rx_seen_e2e.pop(key, None)

        dedup_key = f"{src}:{e2e_id}"
        previous = self.rx_seen_e2e.get(dedup_key)
        self.rx_seen_e2e[dedup_key] = now
        return previous is not None and (now - previous) <= E2E_RX_DEDUP_WINDOW_MS

    def _is_recent_long_text(self, src: str, text_id: str) -> bool:
        if not src or not text_id:
            return False
        now = self._now_ms()
        cutoff = now - LONG_TEXT_RX_DEDUP_WINDOW_MS
        stale_keys = [key for key, ts in self.long_text_seen.items() if ts < cutoff]
        for key in stale_keys:
            self.long_text_seen.pop(key, None)
        key = f"{src}:{text_id}"
        ts = self.long_text_seen.get(key)
        return ts is not None and (now - ts) <= LONG_TEXT_RX_DEDUP_WINDOW_MS

    def _remember_long_text(self, src: str, text_id: str) -> None:
        if not src or not text_id:
            return
        now = self._now_ms()
        self.long_text_seen[f"{src}:{text_id}"] = now

    def _register_pending_e2e(self, payload: dict[str, Any]) -> None:
        e2e_id = str(payload.get("e2e_id") or "").strip()
        if not e2e_id:
            return
        now = self._now_ms()
        self.pending_e2e[e2e_id] = {
            "payload": dict(payload),
            "attempt": int(payload.get("retry_no") or 0),
            "created_ms": now,
            "last_send_ms": now,
            "type": str(payload.get("type") or ""),
            "dst": str(payload.get("dst") or BROADCAST_LABEL),
        }

    def _process_e2e_retries(self) -> None:
        if not self.pending_e2e:
            return
        worker = self.worker
        if worker is None or not worker.is_running:
            dropped = len(self.pending_e2e)
            self.pending_e2e.clear()
            if dropped > 0:
                self.append_log(f"E2E pending cleared: connection lost ({dropped}件)", level="WARN", category="E2E")
            return

        now = self._now_ms()
        for e2e_id, entry in list(self.pending_e2e.items()):
            last_send_ms = int(entry.get("last_send_ms") or 0)
            if (now - last_send_ms) < E2E_ACK_TIMEOUT_MS:
                continue

            attempt = int(entry.get("attempt") or 0)
            if attempt >= E2E_ACK_MAX_RETRIES:
                self.pending_e2e.pop(e2e_id, None)
                self.append_log(
                    f"delivery timeout: type={entry.get('type')} dst={entry.get('dst')} e2e_id={e2e_id}",
                    level="ERROR",
                    category="E2E",
                )
                continue

            payload = dict(entry.get("payload") or {})
            if not payload:
                self.pending_e2e.pop(e2e_id, None)
                continue
            attempt += 1
            payload["retry_no"] = attempt
            payload["ts_ms"] = now
            if not worker.send(payload):
                entry["last_send_ms"] = now
                self.append_log(
                    (
                        f"delivery retry pending(queue full): type={entry.get('type')} "
                        f"dst={entry.get('dst')} e2e_id={e2e_id}"
                    ),
                    level="WARN",
                    category="E2E",
                )
                continue
            entry["payload"] = payload
            entry["attempt"] = attempt
            entry["last_send_ms"] = now
            self.append_log(
                f"delivery retry#{attempt}: type={entry.get('type')} dst={entry.get('dst')} e2e_id={e2e_id}",
                level="WARN",
                category="E2E",
            )

    def _prune_stale_pending_pings(self) -> None:
        if not self.pending_ping_ids:
            return
        now = self._now_ms()
        stale = []
        for seq, ping_id in list(self.pending_ping_ids.items()):
            sent_ms = self.pending_ping_sent_ms.get(seq)
            if sent_ms is None:
                stale.append((seq, ping_id, None))
                continue
            if (now - sent_ms) > PING_PENDING_MAX_AGE_MS:
                stale.append((seq, ping_id, now - sent_ms))
        for seq, _ping_id, _age in stale:
            self.pending_ping_ids.pop(seq, None)
            self.pending_ping_sent_ms.pop(seq, None)
        if stale:
            self.append_log(f"ping pending prune: removed={len(stale)}", level="WARN", category="PING")

    def _prune_rx_sessions(self) -> None:
        now = self._now_ms()
        cutoff = now - RX_SESSION_TIMEOUT_MS

        expired_images = [
            image_id
            for image_id, session in self.image_rx_sessions.items()
            if int(session.get("last_update_ms") or session.get("started_ms") or 0) < cutoff
        ]
        for image_id in expired_images:
            self.image_rx_sessions.pop(image_id, None)
            self.append_log(
                f"image session expired: id={image_id}",
                level="WARN",
                category="IMAGE",
            )

        expired_texts = [
            text_id
            for text_id, session in self.long_text_rx_sessions.items()
            if int(session.get("last_update_ms") or session.get("started_ms") or 0) < cutoff
        ]
        for text_id in expired_texts:
            self.long_text_rx_sessions.pop(text_id, None)
            self.append_log(
                f"long_text session expired: id={text_id}",
                level="WARN",
                category="LONGTXT",
            )

    def _ensure_session_capacity(self, sessions: dict[str, dict[str, Any]], kind: str, incoming_id: str) -> None:
        if incoming_id in sessions or len(sessions) < MAX_RX_SESSIONS:
            return
        oldest_id: str | None = None
        oldest_ts = self._now_ms()
        for sid, session in sessions.items():
            ts = int(session.get("last_update_ms") or session.get("started_ms") or 0)
            if oldest_id is None or ts < oldest_ts:
                oldest_id = sid
                oldest_ts = ts
        if oldest_id is None:
            return
        sessions.pop(oldest_id, None)
        self.append_log(
            f"{kind} session evicted: id={oldest_id} (capacity={MAX_RX_SESSIONS})",
            level="WARN",
            category=kind.upper(),
        )

    def _selected_node_id(self) -> str | None:
        selected = self.node_tree.selection()
        if not selected:
            return None
        values = self.node_tree.item(selected[0], "values")
        if not values:
            return None
        node_id = str(values[0]).strip()
        return node_id or None

    def on_node_tree_select(self, _: Any | None = None) -> None:
        node_id = self._selected_node_id()
        self.selected_node_var.set(node_id if node_id else "未選択")

    def apply_selected_node_to_targets(self, _: Any | None = None) -> None:
        node_id = self._selected_node_id()
        if not node_id:
            messagebox.showinfo("ノード未選択", "ノード一覧から宛先に使いたいノードを選択してください。")
            return
        self.chat_target_var.set(node_id)
        self.image_target_var.set(node_id)
        self.ping_target_var.set(node_id)
        self.refresh_destination_choices()
        self.append_log(f"選択ノード {node_id} をチャット/画像/Ping の宛先に設定しました。", level="SYSTEM", category="UI")

    def set_broadcast_targets(self) -> None:
        self.chat_target_var.set(BROADCAST_LABEL)
        self.image_target_var.set(BROADCAST_LABEL)
        self.ping_target_var.set(BROADCAST_LABEL)
        self.refresh_destination_choices()
        self.append_log("宛先を Broadcast に戻しました。", level="SYSTEM", category="UI")

    def refresh_destination_choices(self) -> None:
        choices: list[str] = [BROADCAST_LABEL]
        for node in self.registry.snapshot():
            if node.node_id not in choices:
                choices.append(node.node_id)

        for var in (self.chat_target_var, self.image_target_var, self.ping_target_var):
            current = var.get().strip()
            if current and current not in choices:
                choices.append(current)
            if not current:
                var.set(BROADCAST_LABEL)

        self.chat_target_combo["values"] = choices
        self.image_target_combo["values"] = choices
        self.ping_target_combo["values"] = choices

    def _payload_type(self, payload: dict[str, Any]) -> str:
        return str(payload.get("type") or payload.get("event") or "payload").strip().lower()

    def _shorten(self, text: str, max_len: int = LOG_PREVIEW_MAX) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _compact_json(self, payload: dict[str, Any]) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(payload)

    def _summarize_payload(self, payload: dict[str, Any]) -> str:
        kind = self._payload_type(payload)
        if kind in {"nodes", "node_list"}:
            nodes = payload.get("nodes") or payload.get("items") or []
            count = len(nodes) if isinstance(nodes, list) else 0
            return f"node_list count={count}"
        if kind == "mesh_observed":
            return (
                f"mesh_observed app={payload.get('app_type')} src={payload.get('src')} dst={payload.get('dst')} "
                f"hops={payload.get('hops')} rssi={payload.get('rssi')} msg_id={payload.get('msg_id')}"
            )
        if kind == "chat":
            via = payload.get("via", "wifi")
            src = payload.get("src") or payload.get("from") or "?"
            dst = payload.get("dst") or BROADCAST_LABEL
            text = self._shorten(str(payload.get("text", "")), 120)
            return (
                f"chat via={via} src={src} dst={dst} ttl={payload.get('ttl')} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)} text={text}"
            )
        if kind == "ping":
            return (
                f"ping seq={payload.get('seq')} ping_id={payload.get('ping_id')} "
                f"dst={payload.get('dst') or BROADCAST_LABEL} ttl={payload.get('ttl')}"
            )
        if kind == "pong":
            return f"pong seq={payload.get('seq')} src={payload.get('src')} latency={payload.get('latency_ms')}ms"
        if kind == "ack":
            return (
                f"ack cmd={payload.get('cmd')} ok={payload.get('ok')} "
                f"via={payload.get('via')} msg_id={payload.get('msg_id')}"
            )
        if kind == "delivery_ack":
            return (
                f"delivery_ack ack_for={payload.get('ack_for')} src={payload.get('src')} "
                f"e2e_id={payload.get('e2e_id')} msg_id={payload.get('msg_id')} "
                f"status={payload.get('status')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "error":
            return f"fw_error code={payload.get('code')} detail={payload.get('detail')}"
        if kind == "image_start":
            return (
                f"image_start id={payload.get('image_id')} name={payload.get('name')} "
                f"size={payload.get('size')} chunks={payload.get('chunks')} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "image_chunk":
            data_b64 = payload.get("data_b64")
            chunk_len = len(data_b64) if isinstance(data_b64, str) else 0
            return (
                f"image_chunk id={payload.get('image_id')} idx={payload.get('index')} "
                f"b64={chunk_len} e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "image_end":
            return (
                f"image_end id={payload.get('image_id')} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "long_text_start":
            return (
                f"long_text_start id={payload.get('text_id')} size={payload.get('size')} "
                f"chunks={payload.get('chunks')} e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "long_text_chunk":
            data_b64 = payload.get("data_b64")
            chunk_len = len(data_b64) if isinstance(data_b64, str) else 0
            return (
                f"long_text_chunk id={payload.get('text_id')} idx={payload.get('index')} "
                f"b64={chunk_len} e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "long_text_end":
            return (
                f"long_text_end id={payload.get('text_id')} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "binary":
            data_b64 = payload.get("data_b64")
            size = len(data_b64) if isinstance(data_b64, str) else 0
            return f"binary src={payload.get('src')} b64={size}"
        return self._shorten(self._compact_json(payload))

    def append_chat(self, text: str) -> None:
        self.chat_history.configure(state=tk.NORMAL)
        self.chat_history.insert(tk.END, text + "\n")
        line_count_str = self.chat_history.index("end-1c").split(".")[0]
        try:
            line_count = int(line_count_str)
        except ValueError:
            line_count = 0
        if line_count > MAX_CHAT_LINES:
            drop_count = line_count - MAX_CHAT_LINES
            self.chat_history.delete("1.0", f"{drop_count + 1}.0")
        self.chat_history.see(tk.END)
        self.chat_history.configure(state=tk.DISABLED)

    def _mark_topology_dirty(self) -> None:
        self.topology_dirty = True

    def clear_topology_history(self) -> None:
        self.topology_tracker.clear()
        self.topology_dirty = True
        self.topology_status_var.set("履歴クリア")
        self.append_log("トポロジ履歴をクリアしました。", level="SYSTEM", category="TOPO")

    def _track_topology_payload(self, payload: dict[str, Any], *, direction: str) -> None:
        try:
            self.topology_tracker.ingest(
                payload,
                direction=direction,
                local_node_id=self.local_node_id,
                now_ms=self._now_ms(),
            )
            self.topology_dirty = True
        except Exception as exc:
            self.append_log(f"topology ingest error: {exc}", level="WARN", category="TOPO")

    def refresh_topology_view(self) -> None:
        try:
            if self.topology_dirty:
                now_ms = self._now_ms()
                window_s = max(1, _to_int(self.topology_window_var.get(), TOPOLOGY_DEFAULT_WINDOW_SEC))
                snapshot = self.topology_tracker.snapshot(
                    now_ms=now_ms,
                    window_s=window_s,
                    via_filter=str(self.topology_via_var.get() or "all").strip().lower(),
                    kind_filter=str(self.topology_kind_var.get() or "all").strip().lower(),
                    include_broadcast=bool(self.topology_broadcast_var.get()),
                )
                self._draw_topology_canvas(snapshot)
                self._refresh_topology_table(snapshot)
                self.topology_status_var.set(
                    f"nodes={len(snapshot.nodes)} links={len(snapshot.edges)} events={snapshot.event_count}"
                )
                self.topology_dirty = False
        finally:
            try:
                self.after(TOPOLOGY_REDRAW_INTERVAL_MS, self.refresh_topology_view)
            except tk.TclError:
                pass

    def _short_node_id(self, node_id: str) -> str:
        if node_id == BROADCAST_NODE:
            return "BROADCAST"
        raw = node_id.strip()
        if len(raw) <= 10:
            return raw
        return f"{raw[:4]}..{raw[-4:]}"

    def _draw_topology_canvas(self, snapshot: TopologySnapshot) -> None:
        canvas = self.topology_canvas
        canvas.delete("all")
        width = max(240, int(canvas.winfo_width()))
        height = max(180, int(canvas.winfo_height()))
        if width < 20 or height < 20:
            return

        nodes = list(snapshot.nodes)
        if bool(self.topology_broadcast_var.get()):
            for edge in snapshot.edges:
                if edge.dst == BROADCAST_NODE and BROADCAST_NODE not in nodes:
                    nodes.append(BROADCAST_NODE)
        if not nodes:
            canvas.create_text(
                width // 2,
                height // 2,
                text="通信イベント待機中...",
                fill="#94a3b8",
                font=("Consolas", 11),
            )
            return

        cx = width / 2.0
        cy = height / 2.0
        radius = max(40.0, min(width, height) * 0.38)
        positions: dict[str, tuple[float, float]] = {}
        count = len(nodes)
        for idx, node_id in enumerate(sorted(nodes, key=lambda x: x.lower())):
            if node_id == BROADCAST_NODE:
                positions[node_id] = (cx, cy)
                continue
            angle = (2.0 * math.pi * idx) / max(1, count)
            positions[node_id] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

        now_ms = self._now_ms()
        for edge in snapshot.edges:
            if edge.src not in positions or edge.dst not in positions:
                continue
            x1, y1 = positions[edge.src]
            x2, y2 = positions[edge.dst]
            width_px = min(8, max(1, 1 + edge.count // 2))
            recent = (now_ms - edge.last_seen_ms) <= 1500
            if edge.via == "ble":
                color = "#16a34a" if recent else "#14532d"
            else:
                color = "#38bdf8" if recent else "#1d4ed8"
            if edge.kind == "delivery_ack":
                color = "#f59e0b" if recent else "#b45309"
            canvas.create_line(
                x1,
                y1,
                x2,
                y2,
                fill=color,
                width=width_px,
                arrow=tk.LAST,
                smooth=True,
            )
            mid_x = (x1 + x2) / 2.0
            mid_y = (y1 + y2) / 2.0
            label = f"{edge.kind}:{edge.count}"
            canvas.create_text(
                mid_x,
                mid_y - 10,
                text=label,
                fill="#e2e8f0",
                font=("Consolas", 9),
            )

        for node_id, (x, y) in positions.items():
            is_broadcast = node_id == BROADCAST_NODE
            fill = "#334155"
            outline = "#94a3b8"
            if is_broadcast:
                fill = "#3f3f46"
                outline = "#f59e0b"
            elif self.local_node_id and node_id == self.local_node_id:
                fill = "#0f766e"
                outline = "#99f6e4"
            canvas.create_oval(x - 16, y - 16, x + 16, y + 16, fill=fill, outline=outline, width=2)
            canvas.create_text(
                x,
                y + 24,
                text=self._short_node_id(node_id),
                fill="#e5e7eb",
                font=("Consolas", 9),
            )

    def _refresh_topology_table(self, snapshot: TopologySnapshot) -> None:
        for item in self.topology_tree.get_children():
            self.topology_tree.delete(item)
        for edge in snapshot.edges[:160]:
            if edge.rssi_avg is None:
                rssi_label = "-"
            else:
                rssi_label = f"{edge.rssi_avg:.1f}"
            age_ms = max(0, snapshot.generated_ms - edge.last_seen_ms)
            self.topology_tree.insert(
                "",
                tk.END,
                values=(
                    edge.src,
                    edge.dst,
                    edge.via,
                    edge.kind,
                    edge.count,
                    edge.bytes_size,
                    edge.hops_max,
                    edge.retry_total,
                    rssi_label,
                    f"{age_ms}ms",
                ),
            )

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if ports and (self.port_var.get() not in ports):
            self.port_var.set(ports[0])
        self.append_log(f"COM一覧更新: {ports if ports else 'なし'}", level="SYSTEM", category="COM")

    def _set_flash_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        for widget in (self.build_fw_button, self.flash_selected_button, self.flash_all_button):
            widget.configure(state=state)

    def _set_flash_busy(self, busy: bool) -> None:
        self.flash_busy = busy
        self._set_flash_controls_enabled(not busy)
        self.flash_status_var.set("Running..." if busy else "Idle")

    def _detect_platformio_runner(self) -> list[str] | None:
        candidates: list[list[str]] = []
        if shutil.which("pio"):
            candidates.append(["pio"])
        if shutil.which("python"):
            candidates.append(["python", "-m", "platformio"])
        if shutil.which("py"):
            candidates.append(["py", "-3", "-m", "platformio"])

        for base in candidates:
            try:
                probe = subprocess.run(
                    [*base, "--version"],
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=12,
                )
            except (subprocess.SubprocessError, OSError):
                continue
            if probe.returncode == 0:
                return base
        return None

    def start_build_only(self) -> None:
        self._start_flash_job(mode="build", ports=[])

    def start_flash_selected_port(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("入力不足", "COMポートを選択してください。")
            return
        self._start_flash_job(mode="upload", ports=[port])

    def start_flash_all_ports(self) -> None:
        ports = list_serial_ports()
        if not ports:
            messagebox.showwarning("ポートなし", "書き込み対象のCOMポートが見つかりません。")
            return
        self._start_flash_job(mode="upload", ports=ports)

    def _start_flash_job(self, mode: str, ports: list[str]) -> None:
        if self.flash_busy:
            messagebox.showinfo("実行中", "現在、Build/書き込み処理が進行中です。")
            return

        env_name = self.pio_env_var.get().strip()
        if not env_name:
            messagebox.showwarning("入力不足", "PlatformIOのEnv名を指定してください。")
            return

        if self.worker and self.worker.is_running:
            should_disconnect = messagebox.askyesno(
                "シリアル接続中",
                "現在シリアル接続中です。書き込み前に切断しますか？\n"
                "（ポート占有による書き込み失敗を防ぐため推奨）",
            )
            if not should_disconnect:
                return
            self.disconnect_serial()

        job_ports = list(ports)
        self._set_flash_busy(True)
        self.append_log(
            f"Build/書き込み開始 mode={mode} env={env_name} ports={job_ports if job_ports else '-'}",
            level="SYSTEM",
            category="FLASH",
        )

        self.flash_thread = threading.Thread(
            target=self._flash_worker,
            args=(mode, env_name, job_ports),
            daemon=True,
        )
        self.flash_thread.start()

    def _emit_flash_event(self, action: str, **kwargs: Any) -> None:
        payload: dict[str, Any] = {"_event": "flash", "action": action}
        payload.update(kwargs)
        self.incoming_queue.put(payload)

    def _run_flash_command(self, cmd: list[str]) -> int:
        self._emit_flash_event("log", level="SYSTEM", text=f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            self._emit_flash_event("log", level="ERROR", text=f"コマンド起動失敗: {exc}")
            return 1

        assert proc.stdout is not None
        for line in proc.stdout:
            line_str = line.rstrip()
            if not line_str:
                continue
            lower = line_str.lower()
            level = "ERROR" if ("error" in lower or "failed" in lower) else "INFO"
            self._emit_flash_event("log", level=level, text=line_str)
        return proc.wait()

    def _flash_worker(self, mode: str, env_name: str, ports: list[str]) -> None:
        runner = self._detect_platformio_runner()
        if runner is None:
            self._emit_flash_event(
                "done",
                ok=False,
                summary="PlatformIO実行環境が見つかりません（pio / python -m platformio）。",
            )
            return

        rc = self._run_flash_command([*runner, "run", "-e", env_name])
        if rc != 0:
            self._emit_flash_event("done", ok=False, summary="Buildに失敗しました。")
            return

        if mode == "build":
            self._emit_flash_event("done", ok=True, summary="Build完了。")
            return

        success_ports: list[str] = []
        failed_ports: list[str] = []
        for port in ports:
            rc_up = self._run_flash_command([*runner, "run", "-e", env_name, "-t", "upload", "--upload-port", port])
            if rc_up == 0:
                success_ports.append(port)
            else:
                failed_ports.append(port)

        if failed_ports:
            self._emit_flash_event(
                "done",
                ok=False,
                summary=f"書き込み失敗 ports={failed_ports} 成功={success_ports}",
            )
            return

        self._emit_flash_event("done", ok=True, summary=f"書き込み完了 ports={success_ports}")

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
        self.append_log(f"接続開始: {port} @ {baud}", level="SYSTEM", category="COM")

    def _clear_runtime_state(self) -> None:
        self.stop_continuous_ping()
        self.pending_ping_ids.clear()
        self.pending_ping_sent_ms.clear()
        self.pending_e2e.clear()
        self.long_text_seen.clear()
        self.image_rx_sessions.clear()
        self.long_text_rx_sessions.clear()
        self.topology_tracker.clear()
        self.topology_status_var.set("未更新")
        self.local_node_id = None
        self._mark_topology_dirty()

    def disconnect_serial(self) -> None:
        self._clear_runtime_state()
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.connection_var.set("未接続")
        self.connect_button.configure(text="接続", state=tk.NORMAL)
        self.append_log("切断しました。", level="SYSTEM", category="COM")

    def poll_worker_events(self) -> None:
        processed = 0
        while processed < MAX_WORKER_EVENTS_PER_TICK:
            try:
                event = self.incoming_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_worker_event(event)
            processed += 1
        backlog = 0
        try:
            backlog = self.incoming_queue.qsize()
        except NotImplementedError:
            backlog = 0
        if backlog > 0 and processed >= MAX_WORKER_EVENTS_PER_TICK:
            now = self._now_ms()
            if (now - self._last_worker_backlog_log_ms) >= WORKER_BACKLOG_LOG_INTERVAL_MS:
                self._last_worker_backlog_log_ms = now
                self.append_log(f"worker event backlog={backlog} (UI処理を分割中)", level="WARN", category="EVENT")
        self._process_e2e_retries()
        self._prune_stale_pending_pings()
        self._prune_rx_sessions()
        self.after(30 if backlog > 0 else 100, self.poll_worker_events)

    def handle_worker_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("_event")
        if event_type == "flash":
            action = str(event.get("action") or "")
            if action == "log":
                level = str(event.get("level") or "INFO")
                text = str(event.get("text") or "")
                if text:
                    self.append_log(text, level=level, category="FLASH")
            elif action == "done":
                ok = bool(event.get("ok"))
                summary = str(event.get("summary") or ("完了" if ok else "失敗"))
                self._set_flash_busy(False)
                self.flash_thread = None
                self.flash_status_var.set("Success" if ok else "Failed")
                self.append_log(summary, level=("SYSTEM" if ok else "ERROR"), category="FLASH")
                if ok:
                    messagebox.showinfo("Build/書き込み", summary)
                else:
                    messagebox.showerror("Build/書き込み", summary)
            return

        if event_type == "status":
            status = event.get("status")
            if status == "connected":
                self.connection_var.set(f"接続中: {event.get('port')}")
                self.connect_button.configure(text="切断", state=tk.NORMAL)
                self.append_log(
                    f"接続成功: {event.get('port')} @ {event.get('baudrate')}",
                    level="SYSTEM",
                    category="COM",
                )
            elif status == "disconnected":
                self._clear_runtime_state()
                self.connection_var.set("未接続")
                self.connect_button.configure(text="接続", state=tk.NORMAL)
                self.worker = None
                self.append_log("シリアル接続が切断されました。", level="WARN", category="COM")
            return

        if event_type == "error":
            self.append_log(str(event.get("message") or "unknown serial error"), level="ERROR", category="SERIAL")
            self.connect_button.configure(state=tk.NORMAL)
            return

        if event_type == "tx":
            payload = event.get("payload")
            if isinstance(payload, dict):
                self._track_topology_payload(payload, direction="tx")
                self.append_log(self._summarize_payload(payload), level="TX", category=self._payload_type(payload))
            else:
                self.append_log(f"tx_raw {payload}", level="TX", category="RAW")
            return

        if event_type == "rx":
            payload = event.get("payload")
            if isinstance(payload, dict):
                self._track_topology_payload(payload, direction="rx")
                kind = self._payload_type(payload)
                level = "RX"
                if kind == "error":
                    level = "ERROR"
                if kind != "mesh_observed":
                    self.append_log(self._summarize_payload(payload), level=level, category=kind)
                self.handle_payload(payload)
            else:
                self.append_log(f"rx_invalid {payload}", level="WARN", category="RAW")
            return

        if event_type == "rx_raw":
            raw = str(event.get("raw") or "")
            raw_short = self._shorten(raw, 140)
            self.append_log(
                f"rx_raw parse_error={event.get('error', 'parse error')} data={raw_short}",
                level="WARN",
                category="RAW",
            )
            return

        self.append_log(self._shorten(str(event), 180), level="WARN", category="EVENT")

    def handle_payload(self, payload: dict[str, Any]) -> None:
        message_type = str(payload.get("type") or payload.get("event") or "").strip().lower()

        if message_type == "bridge_ready":
            node_id = str(payload.get("node_id") or "").strip()
            if node_id:
                self.local_node_id = node_id
                self.registry.upsert_from_payload({"node_id": node_id, "last_seen_ms": self._now_ms()})
                self.refresh_node_table()
                self._mark_topology_dirty()
                self.append_log(f"bridge_ready: local node={node_id}", level="SYSTEM", category="COM")
            return

        if message_type == "mesh_observed":
            # トポロジ表示向けの観測イベント。チャット表示などには流さない。
            return

        if message_type in {"node_list", "nodes"}:
            nodes = payload.get("nodes") or payload.get("items")
            if isinstance(nodes, list):
                self.registry.update_from_list(nodes)
                self.refresh_node_table()
            return

        skip_node_refresh = message_type in {
            "long_text_chunk",
            "long_text_end",
            "image_chunk",
            "image_end",
            "delivery_ack",
            "ack",
            "mesh_observed",
        }
        if not skip_node_refresh:
            maybe_node = self.registry.upsert_from_payload(payload)
            if maybe_node is not None:
                self.refresh_node_table()

        if message_type == "chat":
            src = str(payload.get("src") or payload.get("from") or payload.get("origin") or "?")
            text = str(payload.get("text", ""))
            via = str(payload.get("via", "wifi"))
            e2e_id = str(payload.get("e2e_id") or "").strip()
            if e2e_id and self._remember_rx_e2e(src, e2e_id):
                self.append_log(
                    f"duplicate chat suppressed: src={src} e2e_id={e2e_id}",
                    level="WARN",
                    category="E2E",
                )
                return
            self.append_chat(f"{src}({via}): {text}")
            return

        if message_type in {"pong", "ping_reply"}:
            self.handle_pong(payload)
            return

        if message_type in {"image_start", "image_chunk", "image_end"}:
            self.handle_image_payload(payload)
            return

        if message_type in {"long_text_start", "long_text_chunk", "long_text_end"}:
            self.handle_long_text_payload(payload)
            return

        if message_type == "delivery_ack":
            self.handle_delivery_ack(payload)
            return

        if message_type in {"binary", "ack", "error"}:
            return

    def handle_delivery_ack(self, payload: dict[str, Any]) -> None:
        e2e_id = str(payload.get("e2e_id") or "").strip()
        if not e2e_id:
            return
        entry = self.pending_e2e.get(e2e_id)
        if entry is None:
            return

        status = str(payload.get("status") or "ok").strip().lower()
        if status != "ok":
            self.pending_e2e.pop(e2e_id, None)
            self.append_log(
                (
                    f"delivery failed: type={entry.get('type')} dst={entry.get('dst')} "
                    f"e2e_id={e2e_id} status={status}"
                ),
                level="ERROR",
                category="E2E",
            )
            return

        expected_type = str(entry.get("type") or "").strip().lower()
        ack_for = str(payload.get("ack_for") or "").strip().lower()
        if expected_type and not ack_for:
            self.append_log(
                f"delivery_ack ignored: missing ack_for expected={expected_type} e2e_id={e2e_id}",
                level="WARN",
                category="E2E",
            )
            return
        if expected_type and ack_for != expected_type:
            self.append_log(
                f"delivery_ack ignored: ack_for mismatch expected={expected_type} got={ack_for} e2e_id={e2e_id}",
                level="WARN",
                category="E2E",
            )
            return

        expected_dst = str(entry.get("dst") or "").strip()
        ack_src = str(payload.get("src") or "").strip()
        if expected_dst and expected_dst != BROADCAST_LABEL and ack_src and ack_src.lower() != expected_dst.lower():
            self.append_log(
                f"delivery_ack ignored: src mismatch expected={expected_dst} got={ack_src} e2e_id={e2e_id}",
                level="WARN",
                category="E2E",
            )
            return
        ack_dst = str(payload.get("dst") or "").strip()
        if self.local_node_id and ack_dst and ack_dst.lower() != self.local_node_id.lower():
            self.append_log(
                (
                    f"delivery_ack ignored: dst mismatch expected_local={self.local_node_id} "
                    f"got={ack_dst} e2e_id={e2e_id}"
                ),
                level="WARN",
                category="E2E",
            )
            return

        entry = self.pending_e2e.pop(e2e_id, None)
        if entry is None:
            return
        elapsed_ms = self._now_ms() - int(entry.get("created_ms") or self._now_ms())
        retry_count = int(entry.get("attempt") or 0)
        self.append_log(
            (
                f"delivery ok: type={entry.get('type')} dst={entry.get('dst')} "
                f"e2e_id={e2e_id} retries={retry_count} elapsed={elapsed_ms}ms"
            ),
            level="SYSTEM",
            category="E2E",
        )

    def handle_long_text_payload(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", "")).strip().lower()
        text_id = str(payload.get("text_id", "")).strip()
        src = str(payload.get("src", "")).strip()
        if not text_id:
            return
        if src and self._is_recent_long_text(src, text_id):
            return

        def ensure_session(*, reason: str) -> dict[str, Any]:
            session = self.long_text_rx_sessions.get(text_id)
            if session is not None:
                return session
            now = self._now_ms()
            self._ensure_session_capacity(self.long_text_rx_sessions, "longtxt", text_id)
            session = {
                "chunks": 0,
                "size": 0,
                "sha256": "",
                "encoding": "utf-8",
                "parts": {},
                "src": str(payload.get("src", "?")),
                "started_ms": now,
                "last_update_ms": now,
                "start_received": False,
                "end_received": False,
            }
            self.long_text_rx_sessions[text_id] = session
            self.append_log(
                f"long_text session created: id={text_id} reason={reason}",
                level="WARN",
                category="LONGTXT",
            )
            return session

        def merge_meta(session: dict[str, Any]) -> None:
            src = str(payload.get("src", "")).strip()
            if src:
                session["src"] = src
            encoding = str(payload.get("encoding") or "").strip()
            if encoding:
                session["encoding"] = encoding
            size_raw = payload.get("size")
            if size_raw is not None:
                size = _to_int(str(size_raw), -1)
                if size >= 0:
                    session["size"] = size
            chunks_raw = payload.get("chunks")
            if chunks_raw is not None:
                chunks = _to_int(str(chunks_raw), -1)
                if chunks >= 0:
                    session["chunks"] = chunks
                    if chunks > 0:
                        parts = session.get("parts")
                        if isinstance(parts, dict):
                            invalid_indexes = [idx for idx in list(parts.keys()) if idx < 0 or idx >= chunks]
                            for idx in invalid_indexes:
                                parts.pop(idx, None)
                            if invalid_indexes:
                                self.append_log(
                                    f"long_text invalid chunks dropped: id={text_id} count={len(invalid_indexes)}",
                                    level="WARN",
                                    category="LONGTXT",
                                )
            sha256 = str(payload.get("sha256") or "").strip().lower()
            if sha256:
                session["sha256"] = sha256

        def try_finalize(session: dict[str, Any]) -> bool:
            parts: dict[int, bytes] = session.get("parts", {})
            if not parts:
                return False

            expected_chunks = _to_int(str(session.get("chunks", "0")), 0)
            if expected_chunks > 0:
                missing = [i for i in range(expected_chunks) if i not in parts]
                if missing:
                    return False

            merged = b"".join(parts[i] for i in sorted(parts.keys()))
            expected_size = _to_int(str(session.get("size", "0")), 0)
            if expected_size > 0 and expected_size != len(merged):
                self.append_log(
                    f"long_text_end rejected: id={text_id} size_mismatch expected={expected_size} got={len(merged)}",
                    level="ERROR",
                    category="LONGTXT",
                )
                return False

            expected_hash = str(session.get("sha256", "")).strip().lower()
            actual_hash = hashlib.sha256(merged).hexdigest()
            if expected_hash and expected_hash != actual_hash:
                self.append_log(
                    f"long_text_end rejected: id={text_id} sha256 mismatch",
                    level="ERROR",
                    category="LONGTXT",
                )
                return False

            encoding = str(session.get("encoding") or "utf-8")
            try:
                text = merged.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                self.append_log(
                    f"long_text_end rejected: id={text_id} decode_failed encoding={encoding}",
                    level="ERROR",
                    category="LONGTXT",
                )
                return False

            src = str(session.get("src", "?"))
            self.append_chat(f"{src}(wifi): {text}")
            self.append_log(
                f"長文受信完了: id={text_id} bytes={len(merged)} chars={len(text)}",
                level="SYSTEM",
                category="LONGTXT",
            )
            self._remember_long_text(src, text_id)
            self.long_text_rx_sessions.pop(text_id, None)
            return True

        if kind == "long_text_start":
            session = ensure_session(reason="start")
            is_first_start = not bool(session.get("start_received"))
            merge_meta(session)
            session["start_received"] = True
            session["last_update_ms"] = self._now_ms()
            if is_first_start:
                self.append_log(
                    f"長文受信開始: id={text_id} chunks={session.get('chunks', 0)}",
                    level="SYSTEM",
                    category="LONGTXT",
                )
            if session.get("end_received"):
                try_finalize(session)
            return

        if kind == "long_text_chunk":
            session = ensure_session(reason="chunk_before_start")
            idx = payload.get("index")
            if not isinstance(idx, int):
                idx = _to_int(str(idx), -1)
            if idx < 0:
                return
            expected_chunks = _to_int(str(session.get("chunks", "0")), 0)
            if expected_chunks > 0 and idx >= expected_chunks:
                self.append_log(
                    f"long_text_chunk out of range: id={text_id} index={idx} expected<={expected_chunks - 1}",
                    level="WARN",
                    category="LONGTXT",
                )
                return
            data_b64 = payload.get("data_b64")
            if not isinstance(data_b64, str) or not data_b64:
                return
            try:
                chunk = base64.b64decode(data_b64, validate=True)
            except Exception:
                self.append_log(
                    f"long_text_chunk decode error: id={text_id} index={idx}",
                    level="WARN",
                    category="LONGTXT",
                )
                return
            session["parts"][idx] = chunk
            session["last_update_ms"] = self._now_ms()
            if session.get("end_received"):
                try_finalize(session)
            return

        if kind == "long_text_end":
            session = ensure_session(reason="end_before_start")
            merge_meta(session)
            session["end_received"] = True
            session["last_update_ms"] = self._now_ms()
            if try_finalize(session):
                return
            expected_chunks = _to_int(str(session.get("chunks", "0")), 0)
            parts: dict[int, bytes] = session.get("parts", {})
            if not parts:
                self.append_log(f"長文受信終了待機(空): id={text_id}", level="WARN", category="LONGTXT")
                return
            if expected_chunks > 0:
                missing = [i for i in range(expected_chunks) if i not in parts]
                if missing:
                    self.append_log(
                        f"long_text_end waiting: id={text_id} missing={len(missing)}",
                        level="WARN",
                        category="LONGTXT",
                    )
            return

    def handle_image_payload(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", "")).strip().lower()
        image_id = str(payload.get("image_id", "")).strip()
        if not image_id:
            return

        if kind == "image_start":
            if image_id not in self.image_rx_sessions:
                now = self._now_ms()
                self._ensure_session_capacity(self.image_rx_sessions, "image", image_id)
                self.image_rx_sessions[image_id] = {
                    "name": str(payload.get("name") or f"{image_id}.bin"),
                    "chunks": _to_int(str(payload.get("chunks", "0")), 0),
                    "size": _to_int(str(payload.get("size", "0")), 0),
                    "sha256": str(payload.get("sha256", "")).strip().lower(),
                    "parts": {},
                    "src": str(payload.get("src", "?")),
                    "started_ms": now,
                    "last_update_ms": now,
                }
                self.append_log(
                    f"画像受信開始: id={image_id} name={self.image_rx_sessions[image_id]['name']}",
                    level="SYSTEM",
                    category="IMAGE",
                )
            return

        if kind == "image_chunk":
            session = self.image_rx_sessions.get(image_id)
            if session is None:
                self.append_log(f"image_chunk dropped (missing start): id={image_id}", level="WARN", category="IMAGE")
                return
            idx = payload.get("index")
            if not isinstance(idx, int):
                idx = _to_int(str(idx), -1)
            if idx < 0:
                return
            expected_chunks = _to_int(str(session.get("chunks", "0")), 0)
            if expected_chunks > 0 and idx >= expected_chunks:
                self.append_log(
                    f"image_chunk out of range: id={image_id} index={idx} expected<={expected_chunks - 1}",
                    level="WARN",
                    category="IMAGE",
                )
                return
            chunk_b64 = payload.get("data_b64")
            if not isinstance(chunk_b64, str) or not chunk_b64:
                return
            try:
                chunk = base64.b64decode(chunk_b64, validate=True)
            except Exception:
                self.append_log(f"画像チャンク破損: id={image_id} index={idx}", level="WARN", category="IMAGE")
                return
            session["parts"][idx] = chunk
            session["last_update_ms"] = self._now_ms()
            return

        if kind == "image_end":
            session = self.image_rx_sessions.get(image_id)
            if session is None:
                return
            parts: dict[int, bytes] = session.get("parts", {})
            if not parts:
                self.append_log(f"画像受信終了(空): id={image_id}", level="WARN", category="IMAGE")
                return

            expected_chunks = _to_int(str(session.get("chunks", "0")), 0)
            if expected_chunks > 0:
                missing = [i for i in range(expected_chunks) if i not in parts]
                if missing:
                    self.append_log(
                        f"image_end rejected: id={image_id} missing_chunks={missing[:10]} total_missing={len(missing)}",
                        level="ERROR",
                        category="IMAGE",
                    )
                    return

            ordered = [parts[i] for i in sorted(parts.keys())]
            merged = b"".join(ordered)

            expected_size = _to_int(str(session.get("size", "0")), 0)
            if expected_size > 0 and expected_size != len(merged):
                self.append_log(
                    f"image_end rejected: id={image_id} size_mismatch expected={expected_size} got={len(merged)}",
                    level="ERROR",
                    category="IMAGE",
                )
                return

            expected_hash = str(session.get("sha256", "")).strip().lower()
            actual_hash = hashlib.sha256(merged).hexdigest()
            hash_ok = (not expected_hash) or (expected_hash == actual_hash)
            if not hash_ok:
                self.append_log(
                    f"image_end rejected: id={image_id} sha256 mismatch expected={expected_hash} got={actual_hash}",
                    level="ERROR",
                    category="IMAGE",
                )
                return

            recv_dir = Path.cwd() / "received_images"
            recv_dir.mkdir(parents=True, exist_ok=True)
            name = str(session.get("name", f"{image_id}.bin")).strip() or f"{image_id}.bin"
            safe_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{name}"
            out_path = recv_dir / safe_name
            try:
                out_path.write_bytes(merged)
            except OSError as exc:
                self.append_log(
                    f"image save failed: id={image_id} path={out_path} error={exc}",
                    level="ERROR",
                    category="IMAGE",
                )
                self.image_rx_sessions.pop(image_id, None)
                return

            self.append_log(
                f"画像保存: {out_path} bytes={len(merged)} chunks={len(parts)} hash_ok={hash_ok} src={session.get('src')}",
                level="SYSTEM",
                category="IMAGE",
            )
            self.image_rx_sessions.pop(image_id, None)
            return

    def refresh_node_table(self) -> None:
        selected_before = self._selected_node_id()
        selected_iid: str | None = None

        for item in self.node_tree.get_children():
            self.node_tree.delete(item)

        for node in self.registry.snapshot():
            iid = self.node_tree.insert(
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
            if selected_before and node.node_id == selected_before:
                selected_iid = iid

        if selected_iid is not None:
            self.node_tree.selection_set(selected_iid)
            self.node_tree.focus(selected_iid)

        self.refresh_destination_choices()
        self.on_node_tree_select()

    def send_json(self, payload: dict[str, Any]) -> bool:
        worker = self.worker
        if worker is None or not worker.is_running:
            messagebox.showwarning("未接続", "先にCOMポートへ接続してください。")
            return False
        if not worker.send(payload):
            self.append_log("送信キュー満杯のため送信を保留/破棄しました。", level="WARN", category="SERIAL")
            return False
        return True

    def send_chat(self) -> None:
        text = self.chat_input_var.get().strip()
        if not text:
            return
        dst = self._normalize_target(self.chat_target_var.get())
        via = self.chat_via_var.get().strip() or "wifi"
        ttl = self._current_ttl()
        reliable = self._is_reliable_target(via=via, dst=dst)
        text_bytes = text.encode("utf-8")
        if via == "wifi" and len(text_bytes) > LONG_TEXT_AUTO_SPLIT_BYTES:
            try:
                packets = make_long_text_messages(
                    text=text,
                    dst=dst,
                    via="wifi",
                    ttl=ttl,
                    chunk_size=LONG_TEXT_CHUNK_BYTES,
                    require_ack=reliable,
                )
            except Exception as exc:
                messagebox.showerror("長文送信エラー", str(exc))
                return
            for payload in packets:
                if not self.send_json(payload):
                    return
                if reliable:
                    self._register_pending_e2e(payload)
            shown_dst = self._target_label(dst)
            chunk_count = max(0, len(packets) - 2)
            self.append_chat(
                f"me({via}) -> {shown_dst}: [long_text {len(text_bytes)} bytes / {chunk_count} chunks]"
            )
            self.append_log(
                (
                    f"長文送信キュー投入: bytes={len(text_bytes)} chunks={chunk_count} "
                    f"dst={shown_dst} reliable={'on' if reliable else 'off'}"
                ),
                level="SYSTEM",
                category="LONGTXT",
            )
            self.chat_input_var.set("")
            return

        payload = make_chat_message(text=text, dst=dst, via=via, ttl=ttl, require_ack=reliable)
        if not self.send_json(payload):
            return
        if reliable:
            self._register_pending_e2e(payload)
        shown_dst = self._target_label(dst)
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

        dst = self._normalize_target(self.image_target_var.get())
        ttl = self._current_ttl()
        reliable = self._is_reliable_target(via="wifi", dst=dst)
        file_size = path.stat().st_size
        if dst is None:
            do_broadcast = messagebox.askyesno(
                "Broadcast画像送信",
                "宛先が Broadcast です。複数ノードへ画像を送るため通信負荷が高くなります。\n"
                "このまま送信しますか？",
            )
            if not do_broadcast:
                return
        if file_size > 512 * 1024:
            proceed_large = messagebox.askyesno(
                "大きい画像ファイル",
                f"ファイルサイズが {file_size} bytes です。\n"
                "送信キューとメッシュ負荷が高くなる可能性があります。続行しますか？",
            )
            if not proceed_large:
                return
        try:
            messages = make_image_messages(path=path, dst=dst, via="wifi", ttl=ttl, require_ack=reliable)
        except Exception as exc:
            messagebox.showerror("画像送信エラー", str(exc))
            return

        for payload in messages:
            if not self.send_json(payload):
                return
            if reliable:
                self._register_pending_e2e(payload)

        chunk_count = max(0, len(messages) - 2)
        self.append_log(
            (
                f"画像送信キュー投入: {path.name} ({chunk_count} chunk) dst={self._target_label(dst)} "
                f"reliable={'on' if reliable else 'off'}"
            ),
            level="SYSTEM",
            category="IMAGE",
        )

    def request_nodes(self) -> None:
        self.send_json(make_nodes_request())

    def send_ping(self) -> bool:
        self.ping_seq += 1
        seq = self.ping_seq
        dst = self._normalize_target(self.ping_target_var.get())
        ttl = self._current_ttl()
        payload = make_ping_message(seq=seq, dst=dst, ping_id=self.current_ping_id, via="wifi", ttl=ttl)
        if not self.send_json(payload):
            return False
        sent_ms = self._now_ms()
        self.ping_stats.register_sent(seq)
        self.pending_ping_ids[seq] = self.current_ping_id
        self.pending_ping_sent_ms[seq] = sent_ms
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

        expected_ping_id = self.pending_ping_ids.get(seq)
        if expected_ping_id is None:
            self.append_log(f"pong ignored: no pending seq={seq}", level="WARN", category="PING")
            return
        received_ping_id = payload.get("ping_id")
        if isinstance(expected_ping_id, str) and expected_ping_id:
            if str(received_ping_id or "") != expected_ping_id:
                sent_ms = self.pending_ping_sent_ms.get(seq)
                age_ms = None if sent_ms is None else max(0, self._now_ms() - sent_ms)
                if age_ms is not None and age_ms > PING_PENDING_MAX_AGE_MS:
                    self.pending_ping_ids.pop(seq, None)
                    self.pending_ping_sent_ms.pop(seq, None)
                    self.append_log(
                        (
                            f"pong stale pending dropped: seq={seq} expected={expected_ping_id} "
                            f"got={received_ping_id} age={age_ms}ms"
                        ),
                        level="WARN",
                        category="PING",
                    )
                    return
                self.append_log(
                    (
                        f"pong ignored: seq={seq} ping_id mismatch expected={expected_ping_id} "
                        f"got={received_ping_id} age={age_ms if age_ms is not None else '-'}ms"
                    ),
                    level="WARN",
                    category="PING",
                )
                return

        measured = self.ping_stats.register_received(seq, recv_ts_ms=int(time.time() * 1000), latency_ms=None)
        self.pending_ping_ids.pop(seq, None)
        self.pending_ping_sent_ms.pop(seq, None)
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
            (
                f"連続Ping開始: interval={interval_ms}ms, "
                f"count={'∞' if self.continuous_remaining is None else self.continuous_remaining}, "
                f"ttl={self._current_ttl()}"
            ),
            level="SYSTEM",
            category="PING",
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
            self.append_log("連続Ping停止", level="SYSTEM", category="PING")
        self.start_test_btn.configure(state=tk.NORMAL)
        self.stop_test_btn.configure(state=tk.DISABLED)
        self.continuous_remaining = None

    def reset_stats(self) -> None:
        self.ping_stats.reset()
        self.pending_ping_ids.clear()
        self.pending_ping_sent_ms.clear()
        self.current_ping_id = uuid.uuid4().hex[:8]
        self.update_stats_view()
        self.append_log("統計情報をリセットしました。", level="SYSTEM", category="PING")

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
            self.append_log(f"ログ保存完了: {path}", level="SYSTEM", category="LOG")
        except OSError as exc:
            messagebox.showerror("保存失敗", str(exc))

    def clear_logs(self) -> None:
        self.log_lines.clear()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_close(self) -> None:
        if self.flash_busy:
            should_close = messagebox.askyesno(
                "処理中",
                "Build/書き込み処理が実行中です。終了すると進行中の処理ログ確認ができなくなります。\n終了しますか？",
            )
            if not should_close:
                return
        self._clear_runtime_state()
        if self.worker:
            self.worker.stop()
            self.worker = None
        self.destroy()


def main() -> None:
    app = LPWAApp()
    app.mainloop()


if __name__ == "__main__":
    main()
