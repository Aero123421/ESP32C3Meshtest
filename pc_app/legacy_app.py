from __future__ import annotations

import base64
import csv
from collections import deque
import hashlib
import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from lpwa_gui.models import NodeInfo, NodeRegistry
from lpwa_gui.protocol import (
    RELIABLE_1K_BYTES,
    decode_reliable_1k_from_shards,
    make_chat_message,
    make_long_text_messages,
    make_nodes_request,
    make_ping_probe_command,
    make_reliable_1k_messages,
    make_reliable_1k_nack_message,
    make_reliable_1k_repair_message,
    make_routes_request,
    missing_reliable_shards,
)
from lpwa_gui.serial_worker import SerialWorker, list_serial_ports
from lpwa_gui.stats import PingStats, ReliableStats
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
E2E_RETRY_JITTER_MS = 260
E2E_MAX_RETRY_SENDS_PER_TICK = 6
E2E_RX_DEDUP_WINDOW_MS = 60000
LONG_TEXT_RX_DEDUP_WINDOW_MS = 120000
LONG_TEXT_AUTO_SPLIT_BYTES = 700
LONG_TEXT_CHUNK_BYTES = 32
RX_SESSION_TIMEOUT_MS = 30000
MAX_RX_SESSIONS = 24
MAX_CHAT_LINES = 1200
TOPOLOGY_REDRAW_INTERVAL_MS = 400
TOPOLOGY_DEFAULT_WINDOW_SEC = 30
TOPOLOGY_FLOW_EVENT_LIMIT = 240
QUALITY_GRAPH_MAX_POINTS = 240
QUALITY_GRAPH_MIN_REDRAW_INTERVAL_MS = 200
QUALITY_GRAPH_DUPLICATE_WINDOW_MS = 300
MAX_WORKER_EVENTS_PER_TICK = 320
WORKER_BACKLOG_LOG_INTERVAL_MS = 1200
LOG_WIDGET_FLUSH_INTERVAL_MS = 60
PING_PENDING_MAX_AGE_MS = 20000
PING_BROADCAST_RESPONSE_WINDOW_MS = 2600
PING_PROBE_BYTES = 1000
ROUTE_REQUEST_MIN_INTERVAL_MS = 2500
ROUTE_REQUEST_STALE_MS = 6000
MESH_STATS_REQUEST_MIN_INTERVAL_MS = 1800
MESH_STATS_STALE_MS = 5000
RELIABLE_MODE_CHOICES = ("normal", "reliable_1k")
RELIABLE_PROFILE_CHOICES = ("auto", "25+8", "25+10")
RELIABLE_PROFILE_NAME_TO_ID = {"25+8": 0, "25+10": 1}
RELIABLE_PROFILE_ID_TO_NAME = {value: key for key, value in RELIABLE_PROFILE_NAME_TO_ID.items()}
TOPOLOGY_VIEW_CHOICES = ("tree", "flow", "both")
NODE_ID_PATTERN = re.compile(r"^0x[0-9A-Fa-f]{8}$")
TOPOLOGY_KIND_CHOICES = (
    "all",
    "chat",
    "ping",
    "pong",
    "delivery_ack",
    "long_text_start",
    "long_text_chunk",
    "long_text_end",
    "reliable_1k_start",
    "reliable_1k_chunk",
    "reliable_1k_end",
    "reliable_1k_nack",
    "reliable_1k_repair",
    "reliable_1k_result",
    "binary",
    "unknown",
)
TOPOLOGY_TRACK_MESSAGE_TYPES = {
    "chat",
    "ping",
    "ping_reply",
    "pong",
    "delivery_ack",
    "long_text_start",
    "long_text_chunk",
    "long_text_end",
    "lt_s",
    "lt_c",
    "lt_e",
    "reliable_1k_start",
    "reliable_1k_chunk",
    "reliable_1k_end",
    "reliable_1k_nack",
    "reliable_1k_repair",
    "reliable_1k_result",
    "r1k_s",
    "r1k_d",
    "r1k_e",
    "r1k_n",
    "r1k_r",
    "r1k_o",
    "binary",
}
HIGH_VOLUME_MESSAGE_TYPES = {
    "delivery_ack",
    "long_text_chunk",
    "reliable_1k_chunk",
    "reliable_1k_repair",
}
TOPOLOGY_EDGE_COLORS: dict[str, tuple[str, str]] = {
    "chat": ("#22c55e", "#166534"),
    "ping": ("#38bdf8", "#1d4ed8"),
    "pong": ("#60a5fa", "#1e40af"),
    "delivery_ack": ("#f59e0b", "#b45309"),
    "long_text_start": ("#14b8a6", "#0f766e"),
    "long_text_chunk": ("#06b6d4", "#155e75"),
    "long_text_end": ("#22d3ee", "#0e7490"),
    "reliable_1k_start": ("#f97316", "#9a3412"),
    "reliable_1k_chunk": ("#fb7185", "#9f1239"),
    "reliable_1k_end": ("#f43f5e", "#881337"),
    "reliable_1k_nack": ("#facc15", "#a16207"),
    "reliable_1k_repair": ("#eab308", "#854d0e"),
    "reliable_1k_result": ("#34d399", "#047857"),
    "binary": ("#94a3b8", "#334155"),
    "unknown": ("#94a3b8", "#475569"),
}


class LPWAApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LPWA Test PC App")
        self.geometry("1340x860")
        self.minsize(1100, 700)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.worker: SerialWorker | None = None
        self.incoming_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.registry = NodeRegistry()
        self.ping_stats = PingStats()
        self.reliable_stats = ReliableStats()
        self.ping_seq = 0
        self.pending_ping_rounds: dict[int, dict[str, Any]] = {}
        self.pending_e2e: dict[str, dict[str, Any]] = {}
        self.rx_seen_e2e: dict[str, int] = {}
        self.long_text_seen: dict[str, int] = {}
        self.continuous_after_id: str | None = None
        self.continuous_remaining: int | None = None
        self.continuous_dynamic_interval_ms: int | None = None
        self.continuous_context: dict[str, Any] | None = None
        self.continuous_interval_min_ms = 400
        self.continuous_interval_max_ms = 4000
        self.continuous_interval_last_log_ms = 0
        self.log_lines: list[str] = []
        self.event_records: list[dict[str, Any]] = []
        self.max_log_lines = 3000
        self._log_widget_buffer: list[tuple[str, str]] = []
        self._log_flush_after_id: str | None = None
        self.long_text_rx_sessions: dict[str, dict[str, Any]] = {}
        self.reliable_tx_sessions: dict[str, dict[str, Any]] = {}
        self.reliable_rx_sessions: dict[str, dict[str, Any]] = {}
        self.reliable_rx_completed: dict[str, int] = {}
        self.reliable_profile_pref_by_dst: dict[str, int] = {}
        self.reliable_auto_state_by_dst: dict[str, dict[str, Any]] = {}
        self.reliable_result_deadline_ms = 25000
        self.reliable_rx_session_timeout_ms = 30000
        self.reliable_auto_up_retry_rate_pct = 20.0
        self.reliable_auto_down_retry_rate_pct = 5.0
        self.reliable_auto_down_success_streak = 3
        self.flash_busy = False
        self.flash_thread: threading.Thread | None = None
        self.project_root = Path(__file__).resolve().parents[1]
        self.local_node_id: str | None = None
        self.last_node_list_rx_ms = 0
        self.last_route_list_rx_ms = 0
        self.last_routes_request_tx_ms = 0
        self.last_stats_rx_ms = 0
        self.last_stats_request_tx_ms = 0
        self.mesh_stats_snapshot: dict[str, int] = {}
        self.mesh_stats_baseline: dict[str, int] | None = None
        self.nodes_request_retry_after_id: str | None = None
        self.topology_tracker = TopologyTracker(max_events=20000)
        self.topology_dirty = False
        self._last_worker_backlog_log_ms = 0

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.connection_var = tk.StringVar(value="未接続")
        self.self_node_var = tk.StringVar(value="未取得")
        self.flash_status_var = tk.StringVar(value="Idle")
        self.pio_env_var = tk.StringVar(value="seeed_xiao_esp32c3")
        self.chat_target_var = tk.StringVar(value=BROADCAST_LABEL)
        self.chat_input_var = tk.StringVar()
        self.ping_target_var = tk.StringVar(value=BROADCAST_LABEL)
        self.reliable_mode_var = tk.StringVar(value="normal")
        self.reliable_profile_var = tk.StringVar(value="auto")
        self.reliable_auto_var = tk.BooleanVar(value=True)
        self.selected_node_var = tk.StringVar(value="未選択")
        self.interval_var = tk.StringVar(value="1000")
        self.count_var = tk.StringVar(value="0")
        self.ttl_var = tk.StringVar(value="10")
        self.chat_via_var = tk.StringVar(value="wifi")
        self.topology_window_var = tk.StringVar(value=str(TOPOLOGY_DEFAULT_WINDOW_SEC))
        self.topology_via_var = tk.StringVar(value="all")
        self.topology_kind_var = tk.StringVar(value="all")
        self.topology_view_var = tk.StringVar(value="tree")
        self.topology_status_var = tk.StringVar(value="未更新")
        self.topology_broadcast_var = tk.BooleanVar(value=False)
        self.latest_routes: list[dict[str, Any]] = []

        self.sent_var = tk.StringVar(value="0")
        self.received_var = tk.StringVar(value="0")
        self.lost_var = tk.StringVar(value="0")
        self.pdr_var = tk.StringVar(value="0.0%")
        self.avg_var = tk.StringVar(value="0.0 ms")
        self.min_var = tk.StringVar(value="0.0 ms")
        self.max_var = tk.StringVar(value="0.0 ms")
        self.p95_var = tk.StringVar(value="0.0 ms")
        self.mesh_route_stats_var = tk.StringVar(value="経路統計: 未取得")
        self.reliable_restore_var = tk.StringVar(value="0.0%")
        self.reliable_retry_rate_var = tk.StringVar(value="0.0%")
        self.reliable_profile_used_var = tk.StringVar(value="n/a")
        self.reliable_fail_var = tk.StringVar(value="none")
        self.quality_graph_status_var = tk.StringVar(value="品質グラフ: 待機中")
        self.quality_target_var = tk.StringVar(value="all")
        self.quality_points: deque[dict[str, float | int]] = deque(maxlen=QUALITY_GRAPH_MAX_POINTS)
        self._quality_last_draw_ms = 0
        self._quality_last_signature: tuple[int, int, int, int, int] | None = None
        self._quality_target_active = "all"
        self.quality_graph_canvas: tk.Canvas | None = None
        self.quality_target_combo: ttk.Combobox | None = None
        self.interval_entry: ttk.Entry | None = None
        self.count_entry: ttk.Entry | None = None
        self.ttl_entry: ttk.Entry | None = None
        self.reliable_mode_combo: ttk.Combobox | None = None
        self.flash_port_vars: dict[str, tk.BooleanVar] = {}

        self._build_ui_tabbed()
        self.reliable_mode_var.trace_add("write", self._on_reliable_mode_changed)
        self.reliable_auto_var.trace_add("write", self._on_reliable_mode_changed)
        self.reliable_profile_var.trace_add("write", self._on_reliable_mode_changed)
        self._sync_reliable_controls()
        self.refresh_destination_choices()
        self.refresh_ports()
        self.after(100, self.poll_worker_events)
        self.after(TOPOLOGY_REDRAW_INTERVAL_MS, self.refresh_topology_view)

    def _build_ui_legacy(self) -> None:
        # 旧レイアウト。履歴参照用に残しているが現在は _build_ui_tabbed を使用する。
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
        ttk.Button(node_actions, text="宛先を全体送信", command=self.set_broadcast_targets).grid(
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
        self.interval_entry = ttk.Entry(ping_frame, textvariable=self.interval_var, width=12)
        self.interval_entry.grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="回数(0=無限)").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        self.count_entry = ttk.Entry(ping_frame, textvariable=self.count_var, width=12)
        self.count_entry.grid(row=2, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="TTL").grid(row=3, column=0, padx=4, pady=4, sticky="w")
        self.ttl_entry = ttk.Entry(ping_frame, textvariable=self.ttl_var, width=12)
        self.ttl_entry.grid(row=3, column=1, padx=4, pady=4, sticky="w")
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
            values=("1", "2", "10", "30", "60", "120", "300"),
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
            values=TOPOLOGY_KIND_CHOICES,
            width=12,
            state="readonly",
        )
        self.topology_kind_combo.grid(row=0, column=5, padx=2, sticky="w")
        self.topology_kind_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Checkbutton(
            ctrl,
            text="全体送信(Broadcast)表示",
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

    def _build_ui_tabbed(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        style = ttk.Style(self)
        style.configure("TNotebook.Tab", padding=(14, 8))

        self._build_top_bar_tabbed()

        self.main_tabs = ttk.Notebook(self)
        self.main_tabs.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        comm_tab = ttk.Frame(self.main_tabs, padding=8)
        test_tab = ttk.Frame(self.main_tabs, padding=8)
        topo_tab = ttk.Frame(self.main_tabs, padding=8)
        log_tab = ttk.Frame(self.main_tabs, padding=8)
        fw_tab = ttk.Frame(self.main_tabs, padding=8)
        self.topology_tab = topo_tab

        self.main_tabs.add(comm_tab, text="通信")
        self.main_tabs.add(test_tab, text="試験")
        self.main_tabs.add(topo_tab, text="トポロジ")
        self.main_tabs.add(log_tab, text="ログ")
        self.main_tabs.add(fw_tab, text="FW書込")

        self._build_comm_tab(comm_tab)
        self._build_test_tab(test_tab)
        self._build_topology_tab(topo_tab)
        self._build_log_tab(log_tab)
        self._build_fw_tab(fw_tab)

    def _build_top_bar_tabbed(self) -> None:
        top = ttk.LabelFrame(self, text="接続")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        top.columnconfigure(13, weight=1)

        ttk.Label(top, text="ポート").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=16, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        ttk.Button(top, text="更新", command=self.refresh_ports).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(top, text="Baud").grid(row=0, column=3, padx=4, pady=4, sticky="w")
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=4, pady=4)

        self.connect_button = ttk.Button(top, text="接続", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=5, padx=4, pady=4)
        ttk.Button(top, text="ノード要求", command=self.request_nodes).grid(row=0, column=6, padx=4, pady=4)
        ttk.Button(top, text="経路要求", command=self.request_routes).grid(row=0, column=7, padx=4, pady=4)

        ttk.Label(top, text="状態").grid(row=0, column=8, padx=4, pady=4, sticky="e")
        ttk.Label(top, textvariable=self.connection_var).grid(row=0, column=9, padx=4, pady=4, sticky="w")
        ttk.Label(top, text="自ノード").grid(row=0, column=10, padx=(16, 4), pady=4, sticky="e")
        ttk.Label(top, textvariable=self.self_node_var).grid(row=0, column=11, padx=4, pady=4, sticky="w")
        ttk.Button(top, text="選択ノード→宛先", command=self.apply_selected_node_to_targets).grid(
            row=0, column=12, padx=4, pady=4
        )
        self.broadcast_targets_btn = ttk.Button(top, text="宛先を全体送信", command=self.set_broadcast_targets)
        self.broadcast_targets_btn.grid(row=0, column=13, padx=4, pady=4)

    def _build_comm_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=5)
        parent.columnconfigure(1, weight=6)
        parent.rowconfigure(0, weight=1)

        nodes_frame = ttk.LabelFrame(parent, text="ノード一覧 / 宛先選択")
        nodes_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        nodes_frame.columnconfigure(0, weight=1)
        nodes_frame.rowconfigure(0, weight=1)

        self.node_tree = ttk.Treeview(
            nodes_frame,
            columns=("id", "rssi", "ping", "seen", "msg"),
            show="headings",
            height=18,
        )
        for key, title, width in (
            ("id", "Node", 170),
            ("rssi", "RSSI", 70),
            ("ping", "Ping(ms)", 85),
            ("seen", "Last Seen", 95),
            ("msg", "Last Msg", 320),
        ):
            self.node_tree.heading(key, text=title)
            self.node_tree.column(key, width=width, anchor="w")
        self.node_tree.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        node_scroll = ttk.Scrollbar(nodes_frame, orient=tk.VERTICAL, command=self.node_tree.yview)
        node_scroll.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)
        self.node_tree.configure(yscrollcommand=node_scroll.set)
        self.node_tree.bind("<<TreeviewSelect>>", self.on_node_tree_select)
        self.node_tree.bind("<Double-1>", self.apply_selected_node_to_targets)

        node_actions = ttk.Frame(nodes_frame)
        node_actions.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 4))
        node_actions.columnconfigure(1, weight=1)
        ttk.Label(node_actions, text="操作ヒント").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Label(
            node_actions,
            text="ノードをダブルクリックすると宛先に反映されます",
            foreground="#4b5563",
        ).grid(row=0, column=1, sticky="w")

        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)

        chat_frame = ttk.LabelFrame(right, text="チャット")
        chat_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        chat_frame.columnconfigure(1, weight=1)
        chat_frame.rowconfigure(1, weight=1)
        ttk.Label(chat_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.chat_target_combo = ttk.Combobox(chat_frame, textvariable=self.chat_target_var, width=20, state="readonly")
        self.chat_target_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Label(chat_frame, text="経路").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        ttk.Combobox(
            chat_frame,
            textvariable=self.chat_via_var,
            values=("wifi", "ble"),
            width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=4, pady=4, sticky="w")
        ttk.Label(chat_frame, text=f"※ {BROADCAST_LABEL} で全体送信", foreground="#4b5563").grid(
            row=0, column=4, padx=(8, 4), pady=4, sticky="w"
        )
        self.chat_history = ScrolledText(chat_frame, height=14, state=tk.DISABLED, wrap=tk.WORD)
        self.chat_history.grid(row=1, column=0, columnspan=5, sticky="nsew", padx=4, pady=4)
        chat_entry = ttk.Entry(chat_frame, textvariable=self.chat_input_var)
        chat_entry.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=(0, 4))
        chat_entry.bind("<Return>", lambda _: self.send_chat())
        ttk.Button(chat_frame, text="送信", command=self.send_chat).grid(row=2, column=4, padx=4, pady=(0, 4))

    def _build_test_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        parent.rowconfigure(2, weight=1)
        parent.rowconfigure(3, weight=1)
        parent.rowconfigure(4, weight=2)

        help_line = ttk.Label(
            parent,
            text="長距離試験は TTL を 10〜12 目安で設定し、宛先指定で 1KB Ping Probe と delivery_ack を確認してください。",
            foreground="#4b5563",
        )
        help_line.grid(row=0, column=0, sticky="w", pady=(0, 6))

        ping_frame = ttk.LabelFrame(parent, text="Ping / 連続試験")
        ping_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        ping_frame.columnconfigure(1, weight=1)
        ttk.Label(ping_frame, text="宛先").grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self.ping_target_combo = ttk.Combobox(ping_frame, textvariable=self.ping_target_var, state="readonly")
        self.ping_target_combo.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ttk.Button(ping_frame, text="Ping送信(1KB)", command=self.send_ping).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(ping_frame, text="間隔(ms)").grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self.interval_entry = ttk.Entry(ping_frame, textvariable=self.interval_var, width=12)
        self.interval_entry.grid(row=1, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="回数(0=無限)").grid(row=2, column=0, padx=4, pady=4, sticky="w")
        self.count_entry = ttk.Entry(ping_frame, textvariable=self.count_var, width=12)
        self.count_entry.grid(row=2, column=1, padx=4, pady=4, sticky="w")
        ttk.Label(ping_frame, text="TTL").grid(row=3, column=0, padx=4, pady=4, sticky="w")
        self.ttl_entry = ttk.Entry(ping_frame, textvariable=self.ttl_var, width=12)
        self.ttl_entry.grid(row=3, column=1, padx=4, pady=4, sticky="w")
        self.start_test_btn = ttk.Button(ping_frame, text="連続開始", command=self.start_continuous_ping)
        self.start_test_btn.grid(row=1, column=2, padx=4, pady=4)
        self.stop_test_btn = ttk.Button(ping_frame, text="停止", command=self.stop_continuous_ping, state=tk.DISABLED)
        self.stop_test_btn.grid(row=2, column=2, padx=4, pady=4)
        ttk.Button(ping_frame, text="経路要求", command=self.request_routes).grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(ping_frame, text="統計更新", command=self.request_mesh_stats).grid(row=1, column=3, padx=4, pady=4)
        ttk.Label(ping_frame, text="(10ノード目安: 10-12)", foreground="#4b5563").grid(
            row=3, column=2, padx=4, pady=4, sticky="w"
        )
        ttk.Label(ping_frame, textvariable=self.mesh_route_stats_var, foreground="#4b5563").grid(
            row=4, column=0, columnspan=4, padx=4, pady=(2, 0), sticky="w"
        )

        reliable_frame = ttk.LabelFrame(parent, text="Reliable 1KB")
        reliable_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        reliable_frame.columnconfigure(1, weight=1)
        reliable_frame.columnconfigure(3, weight=1)
        ttk.Label(reliable_frame, text="モード").grid(row=0, column=0, padx=6, pady=4, sticky="w")
        self.reliable_mode_combo = ttk.Combobox(
            reliable_frame,
            textvariable=self.reliable_mode_var,
            values=RELIABLE_MODE_CHOICES,
            width=14,
            state="readonly",
        )
        self.reliable_mode_combo.grid(row=0, column=1, padx=6, pady=4, sticky="w")
        ttk.Label(reliable_frame, text="Profile").grid(row=0, column=2, padx=6, pady=4, sticky="w")
        self.reliable_profile_combo = ttk.Combobox(
            reliable_frame,
            textvariable=self.reliable_profile_var,
            values=RELIABLE_PROFILE_CHOICES,
            width=10,
            state="readonly",
        )
        self.reliable_profile_combo.grid(row=0, column=3, padx=6, pady=4, sticky="w")
        ttk.Checkbutton(
            reliable_frame,
            text="Auto最適化",
            variable=self.reliable_auto_var,
        ).grid(row=0, column=4, padx=6, pady=4, sticky="w")
        ttk.Button(reliable_frame, text="Reliable送信(1KB)", command=self.send_reliable_1k).grid(
            row=0, column=5, padx=6, pady=4, sticky="e"
        )
        ttk.Label(reliable_frame, text="restore").grid(row=1, column=0, padx=6, pady=(2, 6), sticky="w")
        ttk.Label(reliable_frame, textvariable=self.reliable_restore_var).grid(
            row=1, column=1, padx=6, pady=(2, 6), sticky="w"
        )
        ttk.Label(reliable_frame, text="retry_rate").grid(row=1, column=2, padx=6, pady=(2, 6), sticky="w")
        ttk.Label(reliable_frame, textvariable=self.reliable_retry_rate_var).grid(
            row=1, column=3, padx=6, pady=(2, 6), sticky="w"
        )
        ttk.Label(reliable_frame, text="profile").grid(row=1, column=4, padx=6, pady=(2, 6), sticky="w")
        ttk.Label(reliable_frame, textvariable=self.reliable_profile_used_var).grid(
            row=1, column=5, padx=6, pady=(2, 6), sticky="w"
        )
        ttk.Label(reliable_frame, text="top_fail").grid(row=2, column=0, padx=6, pady=(0, 6), sticky="w")
        ttk.Label(reliable_frame, textvariable=self.reliable_fail_var).grid(
            row=2, column=1, columnspan=5, padx=6, pady=(0, 6), sticky="w"
        )

        stats_frame = ttk.LabelFrame(parent, text="PDR / 遅延統計")
        stats_frame.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        for idx in range(4):
            stats_frame.columnconfigure(idx, weight=1)
        ttk.Label(stats_frame, text="Sent").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.sent_var).grid(row=0, column=1, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="Received").grid(row=0, column=2, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.received_var).grid(row=0, column=3, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="Lost").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.lost_var).grid(row=1, column=1, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="PDR").grid(row=1, column=2, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.pdr_var).grid(row=1, column=3, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="Avg").grid(row=2, column=0, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.avg_var).grid(row=2, column=1, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="Min").grid(row=2, column=2, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.min_var).grid(row=2, column=3, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="Max").grid(row=3, column=0, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.max_var).grid(row=3, column=1, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, text="P95").grid(row=3, column=2, padx=6, pady=6, sticky="w")
        ttk.Label(stats_frame, textvariable=self.p95_var).grid(row=3, column=3, padx=6, pady=6, sticky="w")
        ttk.Button(stats_frame, text="統計リセット", command=self.reset_stats).grid(
            row=4, column=0, columnspan=4, padx=6, pady=(2, 8), sticky="e"
        )

        quality_frame = ttk.LabelFrame(parent, text="通信品質（リアルタイム）")
        quality_frame.grid(row=4, column=0, sticky="nsew")
        quality_frame.columnconfigure(0, weight=1)
        quality_frame.rowconfigure(1, weight=1)

        quality_head = ttk.Frame(quality_frame)
        quality_head.grid(row=0, column=0, sticky="ew", padx=6, pady=(4, 2))
        quality_head.columnconfigure(8, weight=1)
        ttk.Label(quality_head, text="PDR", foreground="#22c55e").grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Label(quality_head, text="Avg(ms)", foreground="#38bdf8").grid(row=0, column=1, padx=(0, 8), sticky="w")
        ttk.Label(quality_head, text="P95(ms)", foreground="#f59e0b").grid(row=0, column=2, padx=(0, 8), sticky="w")
        ttk.Label(quality_head, text="Loss", foreground="#ef4444").grid(row=0, column=3, padx=(0, 8), sticky="w")
        ttk.Label(quality_head, text="上段: PDR(0-100%) / 下段: 遅延ms・Loss", foreground="#4b5563").grid(
            row=0, column=4, padx=(0, 8), sticky="w"
        )
        ttk.Label(quality_head, text="対象").grid(row=0, column=5, padx=(8, 2), sticky="e")
        self.quality_target_combo = ttk.Combobox(
            quality_head,
            textvariable=self.quality_target_var,
            values=("all",),
            width=14,
            state="readonly",
        )
        self.quality_target_combo.grid(row=0, column=6, padx=(0, 6), sticky="e")
        self.quality_target_combo.bind("<<ComboboxSelected>>", lambda _: self.update_stats_view())
        ttk.Label(quality_head, textvariable=self.quality_graph_status_var).grid(row=0, column=8, sticky="e")

        self.quality_graph_canvas = tk.Canvas(
            quality_frame,
            bg="#0b1220",
            highlightthickness=1,
            highlightbackground="#1f2937",
            height=220,
        )
        self.quality_graph_canvas.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.quality_graph_canvas.bind("<Configure>", lambda _: self._draw_quality_graph(force=True))

    def _build_topology_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=6)
        parent.rowconfigure(2, weight=3)

        ctrl = ttk.Frame(parent)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctrl.columnconfigure(14, weight=1)
        ttk.Label(ctrl, text="窓(sec)").grid(row=0, column=0, padx=2, sticky="w")
        self.topology_window_combo = ttk.Combobox(
            ctrl,
            textvariable=self.topology_window_var,
            values=("1", "2", "10", "30", "60", "120", "300"),
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
            values=TOPOLOGY_KIND_CHOICES,
            width=12,
            state="readonly",
        )
        self.topology_kind_combo.grid(row=0, column=5, padx=2, sticky="w")
        self.topology_kind_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Label(ctrl, text="表示").grid(row=0, column=6, padx=(8, 2), sticky="w")
        self.topology_view_combo = ttk.Combobox(
            ctrl,
            textvariable=self.topology_view_var,
            values=TOPOLOGY_VIEW_CHOICES,
            width=10,
            state="readonly",
        )
        self.topology_view_combo.grid(row=0, column=7, padx=2, sticky="w")
        self.topology_view_combo.bind("<<ComboboxSelected>>", lambda _: self._mark_topology_dirty())
        ttk.Checkbutton(
            ctrl,
            text="全体送信(Broadcast)表示",
            variable=self.topology_broadcast_var,
            command=self._mark_topology_dirty,
        ).grid(row=0, column=8, padx=(8, 2), sticky="w")
        ttk.Button(ctrl, text="履歴クリア", command=self.clear_topology_history).grid(row=0, column=9, padx=(8, 2))
        ttk.Button(ctrl, text="経路要求", command=self.request_routes).grid(row=0, column=10, padx=(8, 2))
        ttk.Label(ctrl, textvariable=self.topology_status_var).grid(row=0, column=14, padx=4, sticky="e")

        self.topology_canvas = tk.Canvas(
            parent,
            bg="#0b1220",
            highlightthickness=1,
            highlightbackground="#1f2937",
        )
        self.topology_canvas.grid(row=1, column=0, sticky="nsew")
        self.topology_canvas.bind("<Configure>", lambda _: self._mark_topology_dirty())

        table_frame = ttk.Frame(parent)
        table_frame.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.topology_detail_tabs = ttk.Notebook(table_frame)
        self.topology_detail_tabs.grid(row=0, column=0, sticky="nsew")

        links_tab = ttk.Frame(self.topology_detail_tabs)
        links_tab.columnconfigure(0, weight=1)
        links_tab.rowconfigure(0, weight=1)
        self.topology_tree = ttk.Treeview(
            links_tab,
            columns=("src", "dst", "via", "type", "count", "bytes", "hops", "retry", "rssi", "last"),
            show="headings",
            height=9,
        )
        for key, title, width in (
            ("src", "Src", 120),
            ("dst", "Dst", 120),
            ("via", "Via", 62),
            ("type", "Type", 110),
            ("count", "Count", 70),
            ("bytes", "Bytes", 80),
            ("hops", "Hops", 62),
            ("retry", "Retry", 62),
            ("rssi", "RSSI", 62),
            ("last", "Last", 86),
        ):
            self.topology_tree.heading(key, text=title)
            self.topology_tree.column(key, width=width, anchor="w")
        self.topology_tree.grid(row=0, column=0, sticky="nsew")
        topo_scroll = ttk.Scrollbar(links_tab, orient=tk.VERTICAL, command=self.topology_tree.yview)
        topo_scroll.grid(row=0, column=1, sticky="ns")
        self.topology_tree.configure(yscrollcommand=topo_scroll.set)
        self.topology_detail_tabs.add(links_tab, text="リンク集計")

        flow_tab = ttk.Frame(self.topology_detail_tabs)
        flow_tab.columnconfigure(0, weight=1)
        flow_tab.rowconfigure(0, weight=1)
        self.topology_flow_tree = ttk.Treeview(
            flow_tab,
            columns=("time", "type", "src", "dst", "observer", "via_node", "path", "hops", "msg"),
            show="headings",
            height=9,
        )
        for key, title, width in (
            ("time", "Time", 90),
            ("type", "Type", 100),
            ("src", "Src", 120),
            ("dst", "Dst", 120),
            ("observer", "Observer", 120),
            ("via_node", "ViaNode", 120),
            ("path", "Path", 220),
            ("hops", "Hops", 62),
            ("msg", "Msg", 160),
        ):
            self.topology_flow_tree.heading(key, text=title)
            self.topology_flow_tree.column(key, width=width, anchor="w")
        self.topology_flow_tree.grid(row=0, column=0, sticky="nsew")
        flow_scroll = ttk.Scrollbar(flow_tab, orient=tk.VERTICAL, command=self.topology_flow_tree.yview)
        flow_scroll.grid(row=0, column=1, sticky="ns")
        self.topology_flow_tree.configure(yscrollcommand=flow_scroll.set)
        self.topology_detail_tabs.add(flow_tab, text="通信フロー")

        route_tab = ttk.Frame(self.topology_detail_tabs)
        route_tab.columnconfigure(0, weight=1)
        route_tab.rowconfigure(0, weight=1)
        self.topology_route_tree = ttk.Treeview(
            route_tab,
            columns=("dst", "next", "path", "rank", "hops", "metric", "age"),
            show="headings",
            height=9,
        )
        for key, title, width in (
            ("dst", "Dst", 120),
            ("next", "NextHop", 120),
            ("path", "Path", 250),
            ("rank", "Rank", 62),
            ("hops", "Hops", 62),
            ("metric", "Metric", 86),
            ("age", "Age", 86),
        ):
            self.topology_route_tree.heading(key, text=title)
            self.topology_route_tree.column(key, width=width, anchor="w")
        self.topology_route_tree.grid(row=0, column=0, sticky="nsew")
        route_scroll = ttk.Scrollbar(route_tab, orient=tk.VERTICAL, command=self.topology_route_tree.yview)
        route_scroll.grid(row=0, column=1, sticky="ns")
        self.topology_route_tree.configure(yscrollcommand=route_scroll.set)
        self.topology_detail_tabs.add(route_tab, text="経路")

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        log_frame = ttk.LabelFrame(parent, text="イベントログ")
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = ScrolledText(log_frame, state=tk.DISABLED, wrap=tk.NONE, font=("Consolas", 10))
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

    def _build_fw_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        flash_frame = ttk.LabelFrame(parent, text="Build / 書き込み")
        flash_frame.grid(row=0, column=0, sticky="ew")
        flash_frame.columnconfigure(8, weight=1)

        ttk.Label(flash_frame, text="FW Env").grid(row=0, column=0, padx=4, pady=6, sticky="w")
        ttk.Entry(flash_frame, textvariable=self.pio_env_var, width=24).grid(row=0, column=1, padx=4, pady=6, sticky="w")

        self.build_fw_button = ttk.Button(flash_frame, text="Build", command=self.start_build_only)
        self.build_fw_button.grid(row=0, column=2, padx=4, pady=6)
        self.flash_selected_button = ttk.Button(flash_frame, text="書込(選択COM)", command=self.start_flash_selected_port)
        self.flash_selected_button.grid(row=0, column=3, padx=4, pady=6)
        self.flash_all_button = ttk.Button(flash_frame, text="書込(複数選択)", command=self.start_flash_all_ports)
        self.flash_all_button.grid(row=0, column=4, padx=4, pady=6)
        ttk.Button(flash_frame, text="COM再取得", command=self.refresh_flash_port_selector).grid(row=0, column=5, padx=4, pady=6)

        ttk.Label(flash_frame, text="Flash状態").grid(row=0, column=6, padx=4, pady=6, sticky="e")
        ttk.Label(flash_frame, textvariable=self.flash_status_var).grid(row=0, column=8, padx=4, pady=6, sticky="w")

        selector = ttk.LabelFrame(parent, text="複数書込の対象ポート")
        selector.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        selector.columnconfigure(0, weight=1)
        self.flash_ports_frame = ttk.Frame(selector)
        self.flash_ports_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        self.refresh_flash_port_selector()

        guide = ttk.LabelFrame(parent, text="使い方")
        guide.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        guide.columnconfigure(0, weight=1)
        ttk.Label(
            guide,
            justify=tk.LEFT,
            text=(
                "1. 上部の接続エリアで COM と Baud を選択\n"
                "2. Build でビルド確認\n"
                "3. 複数書込みは対象COMをチェックして 書込(複数選択)\n"
                "4. 通信確認は 通信/試験/トポロジ タブで実施"
            ),
            foreground="#4b5563",
        ).grid(row=0, column=0, padx=8, pady=8, sticky="w")

    def append_log(self, text: str, level: str = "INFO", category: str = "APP") -> None:
        level_tag = level.upper().strip()
        if level_tag not in LOG_TAGS:
            level_tag = "INFO"
        cat = category.upper().strip() or "APP"
        now_dt = datetime.now()
        stamped = f"[{now_dt.strftime('%H:%M:%S')}][{level_tag}][{cat}] {text}"
        self.log_lines.append(stamped)
        self.event_records.append(
            {
                "ts_iso": now_dt.isoformat(timespec="milliseconds"),
                "ts_ms": self._now_ms(),
                "level": level_tag,
                "category": cat,
                "message": str(text),
            }
        )
        if len(self.log_lines) > self.max_log_lines:
            self.log_lines = self.log_lines[-self.max_log_lines :]
        if len(self.event_records) > self.max_log_lines:
            self.event_records = self.event_records[-self.max_log_lines :]

        if hasattr(self, "log_text"):
            self._log_widget_buffer.append((stamped, level_tag))
            if self._log_flush_after_id is None:
                self._log_flush_after_id = self.after(LOG_WIDGET_FLUSH_INTERVAL_MS, self._flush_log_widget)

    def _flush_log_widget(self) -> None:
        self._log_flush_after_id = None
        if not hasattr(self, "log_text") or not self._log_widget_buffer:
            return
        self.log_text.configure(state=tk.NORMAL)
        for stamped, level_tag in self._log_widget_buffer:
            self.log_text.insert(tk.END, stamped + "\n", (level_tag,))
        self._log_widget_buffer.clear()
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

    def _normalize_target(
        self,
        raw_value: str | None,
        *,
        context: str = "宛先",
        show_error: bool = False,
    ) -> str | None:
        value = (raw_value or "").strip()
        if not value:
            return None
        if value.lower() in {"*", "all", "broadcast", BROADCAST_LABEL.lower()}:
            return None
        if not NODE_ID_PATTERN.match(value):
            message = f"{context}は 0xXXXXXXXX 形式で指定してください。"
            if show_error:
                messagebox.showwarning("宛先エラー", message)
            raise ValueError(message)
        return f"0x{value[2:].upper()}"

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

    def _parse_reliable_profile_choice(self, raw_choice: str) -> int:
        choice = (raw_choice or "").strip().lower()
        if choice == "1":
            return 1
        return int(RELIABLE_PROFILE_NAME_TO_ID.get(choice, 0))

    def _resolve_reliable_profile_for_send(self, dst: str) -> int:
        selected = (self.reliable_profile_var.get() or "").strip().lower()
        if not self.reliable_auto_var.get():
            return self._parse_reliable_profile_choice(selected)
        if selected == "auto":
            if dst not in self.reliable_profile_pref_by_dst:
                self._seed_reliable_profile_pref(dst)
            return int(self.reliable_profile_pref_by_dst.get(dst, 0))
        return self._parse_reliable_profile_choice(selected)

    def _seed_reliable_profile_pref(self, dst: str) -> None:
        profile = 0
        rssi_value: int | None = None
        ping_value: float | None = None
        for node in self.registry.snapshot():
            if node.node_id != dst:
                continue
            rssi_value = node.rssi
            ping_value = node.ping_ms
            break
        ping_snapshot = self.ping_stats.snapshot()
        ping_pdr = float(ping_snapshot.get("pdr", 0.0))
        ping_sent = int(ping_snapshot.get("sent", 0))
        if (rssi_value is not None and rssi_value <= -82) or (ping_value is not None and ping_value >= 700.0):
            profile = 1
        elif ping_sent >= 20 and ping_pdr < 90.0:
            profile = 1
        self.reliable_profile_pref_by_dst[dst] = profile
        self.append_log(
            (
                f"reliable profile seed: dst={dst} profile={RELIABLE_PROFILE_ID_TO_NAME.get(profile, '25+8')} "
                f"rssi={rssi_value} ping_ms={ping_value} pdr={ping_pdr:.1f}%"
            ),
            level="SYSTEM",
            category="R1K",
        )

    def _reliable_payload_text(self, r1k_id: str, dst: str) -> str:
        header = f"R1K-{r1k_id}-{dst}-"
        pattern = "0123456789abcdef"
        base = (header + pattern) * ((RELIABLE_1K_BYTES // (len(header) + len(pattern))) + 4)
        return base[:RELIABLE_1K_BYTES]

    def _apply_reliable_adaptation(
        self,
        *,
        dst: str,
        success: bool,
        nack_count: int,
        retry_packets: int,
        total_packets: int,
    ) -> None:
        if not self.reliable_auto_var.get():
            return
        if not dst:
            return
        state = self.reliable_auto_state_by_dst.setdefault(dst, {"success_streak": 0})
        current = int(self.reliable_profile_pref_by_dst.get(dst, 0))
        retry_rate = (float(retry_packets) / float(max(1, total_packets))) * 100.0
        should_upgrade = (not success) or nack_count >= 2 or retry_rate >= self.reliable_auto_up_retry_rate_pct
        if should_upgrade:
            state["success_streak"] = 0
            if current < 1:
                self.reliable_profile_pref_by_dst[dst] = 1
                self.append_log(
                    f"reliable adaptive: dst={dst} profile 25+8 -> 25+10 (success={success} nacks={nack_count} retry={retry_rate:.1f}%)",
                    level="SYSTEM",
                    category="R1K",
                )
            return

        if retry_rate <= self.reliable_auto_down_retry_rate_pct:
            state["success_streak"] = int(state.get("success_streak") or 0) + 1
        else:
            state["success_streak"] = 0
        if current > 0 and int(state.get("success_streak") or 0) >= self.reliable_auto_down_success_streak:
            self.reliable_profile_pref_by_dst[dst] = 0
            state["success_streak"] = 0
            self.append_log(
                f"reliable adaptive: dst={dst} profile 25+10 -> 25+8 (retry={retry_rate:.1f}%)",
                level="SYSTEM",
                category="R1K",
            )

    def _send_reliable_result(
        self,
        *,
        dst: str,
        r1k_id: str,
        status: str,
        recovered: int = 0,
        missing: list[int] | None = None,
        latency_ms: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "reliable_1k_result",
            "src": "pc",
            "via": "wifi",
            "dst": dst,
            "r1k_id": r1k_id,
            "status": status,
            "recovered": max(0, int(recovered)),
            "need_ack": True,
            "e2e_id": f"{r1k_id}:o:{uuid.uuid4().hex[:6]}",
            "ts_ms": self._now_ms(),
        }
        ttl = self._current_ttl()
        if ttl > 0:
            payload["ttl"] = ttl
        if missing:
            payload["missing"] = [int(v) for v in missing if int(v) >= 0]
        if latency_ms is not None:
            payload["latency_ms"] = max(0, int(latency_ms))
        if not self.send_json(payload):
            return
        self._register_pending_e2e(payload)

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

    def _clear_pending_e2e_for_r1k(self, r1k_id: str) -> int:
        session_id = str(r1k_id or "").strip().lower()
        if not session_id:
            return 0
        removed = 0
        prefixes = (
            f"{session_id}:s",
            f"{session_id}:e",
            f"{session_id}:n:",
            f"{session_id}:r:",
            f"{session_id}:o:",
        )
        for e2e_id in list(self.pending_e2e.keys()):
            key = str(e2e_id or "").strip().lower()
            if any(key.startswith(prefix) for prefix in prefixes):
                self.pending_e2e.pop(e2e_id, None)
                removed += 1
        return removed

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
        stats_dirty = False
        retry_sent_this_tick = 0
        for e2e_id, entry in list(self.pending_e2e.items()):
            last_send_ms = int(entry.get("last_send_ms") or 0)
            jitter_ms = abs(hash(e2e_id)) % E2E_RETRY_JITTER_MS if E2E_RETRY_JITTER_MS > 0 else 0
            if (now - last_send_ms) < (E2E_ACK_TIMEOUT_MS + jitter_ms):
                continue
            if retry_sent_this_tick >= E2E_MAX_RETRY_SENDS_PER_TICK:
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
                # 送信キュー飽和時も retry 回数を進め、無限待ちを防ぐ。
                entry["payload"] = payload
                entry["attempt"] = attempt
                entry["last_send_ms"] = now
                if not self._is_high_volume_message_type(str(entry.get("type") or "").strip().lower()):
                    self.append_log(
                        (
                            f"delivery retry pending(queue full): type={entry.get('type')} "
                            f"dst={entry.get('dst')} e2e_id={e2e_id}"
                        ),
                        level="WARN",
                        category="E2E",
                    )
                retry_sent_this_tick += 1
                continue
            entry["payload"] = payload
            entry["attempt"] = attempt
            entry["last_send_ms"] = now
            payload_type = str(payload.get("type") or entry.get("type") or "").strip().lower()
            if payload_type.startswith("reliable_1k"):
                self.reliable_stats.register_retry(1)
                r1k_id = str(payload.get("r1k_id") or "").strip().lower()
                if r1k_id:
                    tx = self.reliable_tx_sessions.get(r1k_id)
                    if tx is not None:
                        tx["retry_packets"] = int(tx.get("retry_packets") or 0) + 1
                        tx["last_update_ms"] = now
                        tx["result_deadline_ms"] = now + self.reliable_result_deadline_ms
                stats_dirty = True
            if not self._is_high_volume_message_type(str(entry.get("type") or "").strip().lower()):
                self.append_log(
                    f"delivery retry#{attempt}: type={entry.get('type')} dst={entry.get('dst')} e2e_id={e2e_id}",
                    level="WARN",
                    category="E2E",
                )
            retry_sent_this_tick += 1
        if stats_dirty:
            self.update_stats_view()

    def _prune_stale_pending_pings(self) -> None:
        if not self.pending_ping_rounds:
            return
        now = self._now_ms()
        stale: list[tuple[int, int, int]] = []
        for seq, round_info in list(self.pending_ping_rounds.items()):
            sent_ms = int(round_info.get("sent_ms") or 0)
            if sent_ms <= 0:
                stale.append((seq, 0, 0))
                continue
            if bool(round_info.get("is_broadcast")):
                deadline = int(round_info.get("response_deadline_ms") or 0)
                if deadline > 0 and now >= deadline:
                    replies = len(round_info.get("responders") or set())
                    stale.append((seq, max(0, now - sent_ms), replies))
                    continue
            age = now - sent_ms
            if age > PING_PENDING_MAX_AGE_MS:
                replies = len(round_info.get("responders") or set())
                stale.append((seq, age, replies))
        for seq, _age, _replies in stale:
            self.pending_ping_rounds.pop(seq, None)
            self.ping_stats.expire_pending(seq)
        if stale:
            no_reply = sum(1 for _, _, replies in stale if replies <= 0)
            if no_reply > 0:
                with_reply = len(stale) - no_reply
                self.append_log(
                    f"ping pending prune: removed={len(stale)} no_reply={no_reply} partial_or_done={with_reply}",
                    level="WARN",
                    category="PING",
                )

    def _prune_rx_sessions(self) -> None:
        now = self._now_ms()
        cutoff = now - RX_SESSION_TIMEOUT_MS
        reliable_cutoff = now - self.reliable_rx_session_timeout_ms
        reliable_stats_dirty = False

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

        expired_reliable_rx = [
            r1k_id
            for r1k_id, session in self.reliable_rx_sessions.items()
            if int(session.get("last_update_ms") or session.get("started_ms") or 0) < reliable_cutoff
        ]
        for r1k_id in expired_reliable_rx:
            self.reliable_rx_sessions.pop(r1k_id, None)
            self.reliable_stats.register_failure("rx_timeout")
            reliable_stats_dirty = True

        expired_reliable_completed = [
            r1k_id for r1k_id, completed_ms in self.reliable_rx_completed.items() if int(completed_ms) < reliable_cutoff
        ]
        for r1k_id in expired_reliable_completed:
            self.reliable_rx_completed.pop(r1k_id, None)
            self.append_log(
                f"reliable session expired: id={r1k_id}",
                level="WARN",
                category="R1K",
            )

        expired_reliable_tx = [
            r1k_id
            for r1k_id, session in self.reliable_tx_sessions.items()
            if int(session.get("result_deadline_ms") or 0) > 0
            and int(session.get("result_deadline_ms") or 0) < now
        ]
        for r1k_id in expired_reliable_tx:
            session = self.reliable_tx_sessions.pop(r1k_id, None)
            if session is None:
                continue
            self._clear_pending_e2e_for_r1k(r1k_id)
            self.reliable_stats.register_failure("result_timeout")
            retry_packets = int(session.get("retry_packets") or 0) + int(session.get("repair_packets") or 0)
            total_packets = int(session.get("packet_count") or 0) + int(session.get("repair_packets") or 0)
            self._apply_reliable_adaptation(
                dst=str(session.get("dst") or ""),
                success=False,
                nack_count=int(session.get("nack_count") or 0),
                retry_packets=retry_packets,
                total_packets=total_packets,
            )
            reliable_stats_dirty = True
            self.append_log(
                (
                    f"reliable result timeout: id={r1k_id} dst={session.get('dst')} "
                    f"elapsed={max(0, now - int(session.get('start_ms') or now))}ms"
                ),
                level="WARN",
                category="R1K",
            )

        if reliable_stats_dirty:
            self.update_stats_view()

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
        self.ping_target_var.set(node_id)
        self.refresh_destination_choices()
        self.append_log(f"選択ノード {node_id} をチャット/Ping の宛先に設定しました。", level="SYSTEM", category="UI")

    def set_broadcast_targets(self) -> None:
        if self._is_hardened_mode_enabled():
            messagebox.showwarning("モード制約", "mode=reliable_1k では Broadcast 宛先は使用できません。")
            return
        self.chat_target_var.set(BROADCAST_LABEL)
        self.ping_target_var.set(BROADCAST_LABEL)
        self.refresh_destination_choices()
        self.append_log("宛先を Broadcast に戻しました。", level="SYSTEM", category="UI")

    def _on_reliable_mode_changed(self, *_: Any) -> None:
        self._sync_reliable_controls()

    def _is_hardened_mode_enabled(self) -> bool:
        return (self.reliable_mode_var.get() or "").strip().lower() == "reliable_1k"

    def _preferred_directed_target(self) -> str | None:
        selected = self._selected_node_id()
        if selected:
            return selected

        nodes = self.registry.snapshot()
        local = (self.local_node_id or "").strip()
        for node in nodes:
            node_id = str(node.node_id).strip()
            if not node_id:
                continue
            if local and node_id == local:
                continue
            return node_id
        return None

    def _sync_reliable_controls(self) -> None:
        auto_mode = bool(self.reliable_auto_var.get())
        profile_combo = getattr(self, "reliable_profile_combo", None)
        if auto_mode:
            if (self.reliable_profile_var.get() or "").strip().lower() != "auto":
                self.reliable_profile_var.set("auto")
            if profile_combo is not None:
                profile_combo.configure(state=tk.DISABLED)
        else:
            if profile_combo is not None:
                profile_combo.configure(state="readonly")

        broadcast_btn = getattr(self, "broadcast_targets_btn", None)
        if broadcast_btn is not None:
            if self._is_hardened_mode_enabled():
                broadcast_btn.configure(state=tk.DISABLED)
            else:
                broadcast_btn.configure(state=tk.NORMAL)

        self.refresh_destination_choices()
        if self.continuous_after_id is None:
            self._set_continuous_controls_enabled(True)

    def _ensure_directed_target(self, target: str | None, *, operation: str) -> bool:
        if self._is_hardened_mode_enabled() and target is None:
            messagebox.showwarning("宛先エラー", f"{operation} は mode=reliable_1k で宛先指定が必須です。")
            return False
        local = (self.local_node_id or "").strip().lower()
        if target and local and target.strip().lower() == local:
            messagebox.showwarning("宛先エラー", f"{operation} の宛先が自ノードになっています。別ノードを選択してください。")
            return False
        return True

    def refresh_destination_choices(self) -> None:
        allow_broadcast = not self._is_hardened_mode_enabled()
        choices: list[str] = [BROADCAST_LABEL] if allow_broadcast else []
        for node in self.registry.snapshot():
            node_id = str(node.node_id).strip()
            if not NODE_ID_PATTERN.match(node_id):
                continue
            node_id = f"0x{node_id[2:].upper()}"
            if node_id not in choices:
                choices.append(node_id)

        fallback_directed = self._preferred_directed_target()
        if fallback_directed and fallback_directed not in choices:
            choices.append(fallback_directed)
        for var in (self.chat_target_var, self.ping_target_var):
            current = var.get().strip()
            if current and current != BROADCAST_LABEL and not NODE_ID_PATTERN.match(current):
                current = ""
            if current and current not in choices and (allow_broadcast or current != BROADCAST_LABEL):
                choices.append(current)
            if not allow_broadcast and (not current or current == BROADCAST_LABEL):
                if fallback_directed:
                    var.set(fallback_directed)
                else:
                    var.set("")
            elif allow_broadcast and not current:
                var.set(BROADCAST_LABEL)

        self.chat_target_combo["values"] = choices
        self.ping_target_combo["values"] = choices
        self.refresh_quality_target_choices()

    def refresh_quality_target_choices(self) -> None:
        combo = self.quality_target_combo
        if combo is None:
            return
        values = ["all"]
        for node in self.registry.snapshot():
            node_id = str(node.node_id).strip()
            if not NODE_ID_PATTERN.match(node_id):
                continue
            node_id = f"0x{node_id[2:].upper()}"
            if node_id not in values:
                values.append(node_id)
        combo["values"] = values
        current = (self.quality_target_var.get() or "").strip()
        if not current or current not in values:
            self.quality_target_var.set("all")

    def _payload_type(self, payload: dict[str, Any]) -> str:
        return str(payload.get("type") or payload.get("event") or "payload").strip().lower()

    def _payload_named_hops(self, payload: dict[str, Any], *keys: str, allow_zero: bool = False) -> int | None:
        raw = None
        for key in keys:
            if key in payload:
                raw = payload.get(key)
                break
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw if (raw >= 0 if allow_zero else raw > 0) else None
        if isinstance(raw, float):
            hops = int(raw)
            return hops if (hops >= 0 if allow_zero else hops > 0) else None
        if isinstance(raw, str):
            text = raw.strip()
            if text and (text.isdigit() or (text.startswith("-") and text[1:].isdigit())):
                hops = int(text)
                return hops if (hops >= 0 if allow_zero else hops > 0) else None
        return None

    def _payload_hops(self, payload: dict[str, Any]) -> int | None:
        return self._payload_named_hops(payload, "reply_hops", "hops", allow_zero=True)

    def _payload_request_hops(self, payload: dict[str, Any]) -> int | None:
        return self._payload_named_hops(payload, "request_hops", allow_zero=True)

    def _hop_fields_summary(self, payload: dict[str, Any]) -> str:
        observed_hops = self._payload_hops(payload)
        request_hops = self._payload_request_hops(payload)
        parts: list[str] = []
        if request_hops is not None:
            parts.append(f"request_hops={request_hops}")
        if observed_hops is not None:
            label = "reply_hops" if (request_hops is not None or "reply_hops" in payload) else "hops"
            parts.append(f"{label}={observed_hops}")
        elif "hops" in payload:
            parts.append(f"hops={payload.get('hops')}")
        if not parts:
            return ""
        return " ".join(parts)

    def _route_stats(self, routes: list[Any] | None = None) -> tuple[int, int]:
        route_entries = routes if routes is not None else self.latest_routes
        multi_hop = 0
        max_hops = 0
        for route in route_entries:
            if not isinstance(route, dict):
                continue
            hops = _to_int(str(route.get("hops", 0)), 0)
            if hops > 1:
                multi_hop += 1
            if hops > max_hops:
                max_hops = hops
        return multi_hop, max_hops

    def _best_route_for_node(self, node_id: str) -> dict[str, Any] | None:
        target = str(node_id or "").strip().lower()
        if not target:
            return None
        candidates: list[dict[str, Any]] = []
        for route in self.latest_routes:
            if not isinstance(route, dict):
                continue
            dst = str(route.get("dst_node_id") or route.get("dst") or "").strip().lower()
            if dst == target:
                candidates.append(route)
        if not candidates:
            return None
        candidates.sort(
            key=lambda route: (
                0 if _to_int(str(route.get("rank", 0)), 0) <= 0 else 1,
                0 if _to_int(str(route.get("hops", 0)), 0) > 0 else 1,
                _to_int(str(route.get("hops", 999)), 999),
                _to_int(str(route.get("age_ms", 999999)), 999999),
            )
        )
        return candidates[0]

    def _route_hint_for_node(self, node_id: str) -> tuple[int | None, str | None]:
        route = self._best_route_for_node(node_id)
        if route is None:
            return None, None
        hops = _to_int(str(route.get("hops", 0)), 0)
        next_hop = str(route.get("next_hop_node_id") or route.get("next_hop") or "").strip() or None
        return (hops if hops > 0 else None), next_hop

    def _hop_log_suffix(self, *, src_node: str, observed_hops: int | None, request_hops: int | None = None) -> str:
        route_hops, route_next = self._route_hint_for_node(src_node)
        if observed_hops is not None:
            if request_hops is not None:
                parts = [f"request_hops={request_hops}", f"reply_hops={observed_hops}"]
            else:
                parts = [f"hops={observed_hops}"]
            if observed_hops > 1 and route_next:
                parts.append(f"next={route_next}")
            return " " + " ".join(parts)
        if request_hops is not None:
            return f" request_hops={request_hops}"
        if route_hops is not None:
            parts = [f"route_hops={route_hops}"]
            if route_hops > 1 and route_next:
                parts.append(f"next={route_next}")
            return " " + " ".join(parts)
        return ""

    def _effective_hops(self, *, src_node: str, observed_hops: int) -> tuple[int, bool]:
        if observed_hops > 0:
            return observed_hops, False
        route_hops, _route_next = self._route_hint_for_node(src_node)
        if route_hops is not None and route_hops > 0:
            return route_hops, True
        return 0, False

    def _format_hops_label(self, *, src_node: str, observed_hops: int) -> str:
        hops, inferred = self._effective_hops(src_node=src_node, observed_hops=observed_hops)
        if inferred:
            return f"~{hops}"
        return str(hops)

    def _format_route_path(self, *, dst: str, next_hop: str, hops: int) -> str:
        self_label = self._short_node_id(self.local_node_id) if self.local_node_id else "SELF"
        dst_label = self._short_node_id(dst)
        next_label = self._short_node_id(next_hop) if next_hop else "?"
        if hops <= 1 or not next_hop or next_hop.lower() == dst.lower():
            return f"{self_label} -> {dst_label}"
        if hops == 2:
            return f"{self_label} -> {next_label} -> {dst_label}"
        return f"{self_label} -> {next_label} -> ... -> {dst_label}"

    def _format_observed_event_path(self, ev: Any) -> str:
        parts: list[str] = []
        for raw_node in (ev.src, ev.via_node, ev.observer):
            node = str(raw_node or "").strip()
            if not node:
                continue
            if parts and node == parts[-1]:
                continue
            parts.append(node)
        dst = str(ev.dst or "").strip()
        if dst and dst != BROADCAST_NODE and (not parts or dst != parts[-1]):
            parts.append(dst)
        if not parts and dst:
            parts = [dst]
        if len(parts) <= 1:
            return "-"
        return " -> ".join(self._short_node_id(node_id) for node_id in parts)

    def _mesh_stats_ratio(self, numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return (float(numerator) * 100.0) / float(denominator)

    def _mesh_stats_delta(self) -> dict[str, int]:
        current = self.mesh_stats_snapshot if isinstance(self.mesh_stats_snapshot, dict) else {}
        baseline = self.mesh_stats_baseline if isinstance(self.mesh_stats_baseline, dict) else {}
        keys = {
            "route_lookup_hit",
            "route_lookup_miss",
            "route_learned",
            "route_promoted",
            "route_expired",
            "routed_unicast_attempts",
            "routed_unicast_success",
            "routed_unicast_fail",
            "routed_fallback_flood",
        }
        delta: dict[str, int] = {}
        for key in keys:
            delta[key] = max(0, _to_int(str(current.get(key, 0)), 0) - _to_int(str(baseline.get(key, 0)), 0))
        return delta

    def _update_mesh_route_stats_view(self) -> None:
        if not self.mesh_stats_snapshot:
            self.mesh_route_stats_var.set("経路統計: 未取得")
            return

        now = self._now_ms()
        age_ms = max(0, now - int(self.last_stats_rx_ms or 0))
        delta = self._mesh_stats_delta()
        has_baseline = isinstance(self.mesh_stats_baseline, dict)
        source = delta if has_baseline else self.mesh_stats_snapshot
        label = "連続Ping" if has_baseline else "累積"

        route_hit = _to_int(str(source.get("route_lookup_hit", 0)), 0)
        route_miss = _to_int(str(source.get("route_lookup_miss", 0)), 0)
        route_total = route_hit + route_miss
        route_hit_rate = self._mesh_stats_ratio(route_hit, route_total)
        routed_attempts = _to_int(str(source.get("routed_unicast_attempts", 0)), 0)
        routed_success = _to_int(str(source.get("routed_unicast_success", 0)), 0)
        routed_fail = _to_int(str(source.get("routed_unicast_fail", 0)), 0)
        fallback = _to_int(str(source.get("routed_fallback_flood", 0)), 0)
        fallback_ratio = self._mesh_stats_ratio(fallback, routed_attempts + fallback)
        learned = _to_int(str(source.get("route_learned", 0)), 0)
        promoted = _to_int(str(source.get("route_promoted", 0)), 0)
        expired = _to_int(str(source.get("route_expired", 0)), 0)
        age_label = f"{age_ms / 1000.0:.1f}s" if age_ms >= 1000 else f"{age_ms}ms"

        self.mesh_route_stats_var.set(
            (
                f"経路統計[{label}] hit={route_hit}/{route_total} ({route_hit_rate:.1f}%) "
                f"fallback={fallback} ({fallback_ratio:.1f}%) "
                f"unicast={routed_success}/{max(routed_attempts, 0)} fail={routed_fail} "
                f"learned={learned} promoted={promoted} expired={expired} age={age_label}"
            )
        )

    def _is_high_volume_message_type(self, kind: str) -> bool:
        return kind in HIGH_VOLUME_MESSAGE_TYPES

    def _should_log_worker_payload(self, *, event_type: str, kind: str) -> bool:
        del event_type
        if kind in {"mesh_observed", "mesh_trace"}:
            return False
        if self._is_high_volume_message_type(kind):
            return False
        return True

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
        if kind == "nodes_request":
            return "nodes_request"
        if kind == "routes_request":
            return "routes_request"
        if kind in {"nodes", "node_list"}:
            nodes = payload.get("nodes") or payload.get("items") or []
            count = len(nodes) if isinstance(nodes, list) else 0
            return f"node_list count={count}"
        if kind in {"routes", "route_list"}:
            routes = payload.get("routes") or payload.get("items") or []
            count = len(routes) if isinstance(routes, list) else 0
            multi_hop, max_hops = self._route_stats(routes if isinstance(routes, list) else [])
            return f"route_list count={count} multi_hop={multi_hop} max_hops={max_hops}"
        if kind == "mesh_observed":
            hop_text = self._hop_fields_summary(payload) or f"hops={payload.get('hops')}"
            return (
                f"mesh_observed app={payload.get('app_type')} src={payload.get('src')} dst={payload.get('dst')} "
                f"observer={payload.get('observer')} via_node={payload.get('via_node')} "
                f"{hop_text} rssi={payload.get('rssi')} msg_id={payload.get('msg_id')}"
            )
        if kind == "mesh_trace":
            hop_text = self._hop_fields_summary(payload) or f"hops={payload.get('hops')}"
            return (
                f"mesh_trace app={payload.get('app_type')} src={payload.get('src')} dst={payload.get('dst')} "
                f"observer={payload.get('observer')} via_node={payload.get('via_node')} "
                f"{hop_text} msg_id={payload.get('msg_id')}"
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
                f"dst={payload.get('dst') or BROADCAST_LABEL} ttl={payload.get('ttl')} "
                f"probe={payload.get('probe_bytes', '-')}B"
            )
        if kind == "pong":
            src = str(payload.get("src") or payload.get("from") or "").strip()
            return (
                f"pong seq={payload.get('seq')} src={payload.get('src')} latency={payload.get('latency_ms')}ms"
                f"{self._hop_log_suffix(src_node=src, observed_hops=self._payload_hops(payload), request_hops=self._payload_request_hops(payload))}"
            )
        if kind == "ack":
            return (
                f"ack cmd={payload.get('cmd')} ok={payload.get('ok')} "
                f"via={payload.get('via')} msg_id={payload.get('msg_id')}"
            )
        if kind == "delivery_ack":
            src = str(payload.get("src") or "").strip()
            return (
                f"delivery_ack ack_for={payload.get('ack_for')} src={payload.get('src')} "
                f"e2e_id={payload.get('e2e_id')} msg_id={payload.get('msg_id')} "
                f"status={payload.get('status')} retry={payload.get('retry_no', 0)}"
                f"{self._hop_log_suffix(src_node=src, observed_hops=self._payload_hops(payload), request_hops=self._payload_request_hops(payload))}"
            )
        if kind == "error":
            return f"fw_error code={payload.get('code')} detail={payload.get('detail')}"
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
        if kind == "reliable_1k_start":
            return (
                f"reliable_1k_start id={payload.get('r1k_id')} dst={payload.get('dst') or BROADCAST_LABEL} "
                f"profile={payload.get('profile_id')}({payload.get('profile_name')}) size={payload.get('size')} "
                f"shards={payload.get('data_shards')}+{payload.get('parity_shards')} e2e_id={payload.get('e2e_id')}"
            )
        if kind == "reliable_1k_chunk":
            data_b64 = payload.get("data_b64")
            chunk_len = len(data_b64) if isinstance(data_b64, str) else 0
            return (
                f"reliable_1k_chunk id={payload.get('r1k_id')} idx={payload.get('index')} b64={chunk_len} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "reliable_1k_end":
            return (
                f"reliable_1k_end id={payload.get('r1k_id')} size={payload.get('size')} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "reliable_1k_nack":
            missing = payload.get("missing")
            miss_count = len(missing) if isinstance(missing, list) else 0
            return (
                f"reliable_1k_nack id={payload.get('r1k_id')} src={payload.get('src')} "
                f"missing={miss_count} e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "reliable_1k_repair":
            data_b64 = payload.get("data_b64")
            chunk_len = len(data_b64) if isinstance(data_b64, str) else 0
            return (
                f"reliable_1k_repair id={payload.get('r1k_id')} idx={payload.get('index')} b64={chunk_len} "
                f"e2e_id={payload.get('e2e_id')} retry={payload.get('retry_no', 0)}"
            )
        if kind == "reliable_1k_result":
            missing = payload.get("missing")
            miss_count = len(missing) if isinstance(missing, list) else 0
            return (
                f"reliable_1k_result id={payload.get('r1k_id')} status={payload.get('status')} "
                f"recovered={payload.get('recovered')} missing={miss_count} latency={payload.get('latency_ms')}"
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

    def _should_track_topology_payload(self, payload: dict[str, Any], *, direction: str) -> bool:
        kind = self._payload_type(payload)
        if direction == "tx":
            return kind in TOPOLOGY_TRACK_MESSAGE_TYPES
        if kind in {"mesh_observed", "mesh_trace"}:
            return True
        via = str(payload.get("via") or "").strip().lower()
        # Wi-Fi受信は mesh_observed に集約し、BLEは従来イベントを採用する。
        return via == "ble" and kind in TOPOLOGY_TRACK_MESSAGE_TYPES

    def _track_topology_payload(self, payload: dict[str, Any], *, direction: str) -> None:
        if not self._should_track_topology_payload(payload, direction=direction):
            return
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

    def _infer_local_node_id_from_entries(self, entries: list[Any]) -> str | None:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get("node_id") or entry.get("id") or "").strip()
            if not node_id:
                continue
            rssi_raw = entry.get("rssi")
            rssi: int | None = None
            if isinstance(rssi_raw, bool):
                rssi = None
            elif isinstance(rssi_raw, int):
                rssi = rssi_raw
            elif isinstance(rssi_raw, float):
                rssi = int(rssi_raw)
            elif isinstance(rssi_raw, str):
                raw = rssi_raw.strip()
                if raw and (raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit())):
                    rssi = int(raw)
            if rssi == 0:
                return node_id
        return None

    def _update_topology_kind_choices(self, snapshot: TopologySnapshot) -> None:
        known = {str(kind).strip().lower() for kind in TOPOLOGY_KIND_CHOICES}
        for edge in snapshot.edges:
            kind = str(edge.kind).strip().lower()
            if kind:
                known.add(kind)
        choices = tuple(["all"] + sorted(k for k in known if k != "all"))
        try:
            current_values = tuple(self.topology_kind_combo["values"])
        except Exception:
            current_values = ()
        if current_values != choices:
            self.topology_kind_combo["values"] = choices
        current = str(self.topology_kind_var.get() or "all").strip().lower() or "all"
        if current not in known:
            self.topology_kind_var.set("all")
            self.topology_dirty = True

    def _is_topology_tab_selected(self) -> bool:
        if not hasattr(self, "main_tabs") or not hasattr(self, "topology_tab"):
            return True
        try:
            selected = str(self.main_tabs.select() or "")
            topo_widget = str(self.topology_tab)
            return selected == topo_widget
        except Exception:
            return True

    def refresh_topology_view(self) -> None:
        try:
            topology_visible = self._is_topology_tab_selected()
            if topology_visible:
                self._request_routes_if_needed(force=False)
                self._request_mesh_stats_if_needed(force=False)
            if self.topology_dirty:
                if not topology_visible:
                    return
                if hasattr(self, "main_tabs") and hasattr(self, "topology_tab"):
                    try:
                        selected = str(self.main_tabs.select() or "")
                        topo_widget = str(self.topology_tab)
                        if selected != topo_widget:
                            # 非表示タブ中は再描画を遅延してUI負荷を下げる。
                            return
                    except Exception:
                        pass
                now_ms = self._now_ms()
                window_s = max(1, _to_int(self.topology_window_var.get(), TOPOLOGY_DEFAULT_WINDOW_SEC))
                snapshot = self.topology_tracker.snapshot(
                    now_ms=now_ms,
                    window_s=window_s,
                    via_filter=str(self.topology_via_var.get() or "all").strip().lower(),
                    kind_filter=str(self.topology_kind_var.get() or "all").strip().lower(),
                    include_broadcast=bool(self.topology_broadcast_var.get()),
                )
                self._update_topology_kind_choices(snapshot)
                self._draw_topology_canvas(snapshot)
                self._refresh_topology_table(snapshot)
                self._refresh_topology_flow_table(snapshot)
                self._refresh_topology_route_table(snapshot)
                self_label = self.local_node_id if self.local_node_id else "未取得"
                mode = str(self.topology_view_var.get() or "tree").strip().lower() or "tree"
                multi_hop_edges = sum(
                    1 for edge in snapshot.edges if self._effective_hops(src_node=edge.src, observed_hops=edge.hops_max)[0] > 1
                )
                route_multi_hop, route_max_hops = self._route_stats()
                self.topology_status_var.set(
                    " ".join(
                        [
                            f"mode={mode}",
                            f"self={self_label}",
                            f"nodes={len(snapshot.nodes)}",
                            f"flow_links={len(snapshot.edges)}",
                            f"multi_hop={multi_hop_edges}",
                            f"relay_links={len(snapshot.relay_links)}",
                            f"route_multi={route_multi_hop}",
                            f"route_max={route_max_hops}",
                            f"events={snapshot.event_count}",
                        ]
                    )
                )
                self.topology_dirty = False
        finally:
            try:
                self.after(TOPOLOGY_REDRAW_INTERVAL_MS, self.refresh_topology_view)
            except tk.TclError:
                pass

    def _short_node_id(self, node_id: str) -> str:
        if node_id == BROADCAST_NODE:
            return "ALL"
        raw = node_id.strip()
        if len(raw) <= 10:
            return raw
        return f"{raw[:4]}..{raw[-4:]}"

    def _pick_focus_node(self, snapshot: TopologySnapshot, nodes: list[str]) -> str | None:
        focus_node = self.local_node_id if (self.local_node_id and self.local_node_id in nodes) else None
        if focus_node:
            return focus_node
        activity: dict[str, int] = {}
        for edge in snapshot.edges:
            activity[edge.src] = activity.get(edge.src, 0) + max(1, edge.count)
            activity[edge.dst] = activity.get(edge.dst, 0) + max(1, edge.count)
        for link in snapshot.relay_links:
            activity[link.parent] = activity.get(link.parent, 0) + max(1, link.count)
            activity[link.child] = activity.get(link.child, 0) + max(1, link.count)
        if activity:
            return max(activity.items(), key=lambda kv: kv[1])[0]
        if nodes:
            return sorted(nodes, key=lambda x: x.lower())[0]
        return None

    def _compute_ring_positions(
        self, *, nodes: list[str], width: int, height: int, focus_node: str | None
    ) -> dict[str, tuple[float, float]]:
        cx = width / 2.0
        cy = height / 2.0
        radius = max(56.0, min(width, height) * 0.34)
        positions: dict[str, tuple[float, float]] = {}
        if focus_node and focus_node in nodes:
            positions[focus_node] = (cx, cy)
        if BROADCAST_NODE in nodes:
            positions[BROADCAST_NODE] = (cx, cy - min(height * 0.32, radius))
        ring_nodes = [n for n in sorted(nodes, key=lambda x: x.lower()) if n not in positions]
        for idx, node_id in enumerate(ring_nodes):
            angle = (-math.pi / 2.0) + (2.0 * math.pi * idx / max(1, len(ring_nodes)))
            positions[node_id] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
        return positions

    def _compute_tree_positions(
        self,
        *,
        nodes: list[str],
        snapshot: TopologySnapshot,
        width: int,
        height: int,
        focus_node: str | None,
    ) -> dict[str, tuple[float, float]]:
        if not nodes:
            return {}

        children: dict[str, set[str]] = {}
        indegree: dict[str, int] = {node: 0 for node in nodes}
        for link in snapshot.relay_links:
            parent = link.parent
            child = link.child
            if parent == child:
                continue
            if parent not in indegree:
                indegree[parent] = 0
            if child not in indegree:
                indegree[child] = 0
            bucket = children.setdefault(parent, set())
            if child not in bucket:
                bucket.add(child)
                indegree[child] = indegree.get(child, 0) + 1

        root = focus_node if (focus_node and focus_node in indegree) else None
        if root is None:
            roots = sorted([node for node, deg in indegree.items() if deg == 0], key=lambda x: x.lower())
            if roots:
                root = roots[0]
            else:
                root = sorted(nodes, key=lambda x: x.lower())[0]

        levels: dict[str, int] = {root: 0}
        queue_nodes: list[str] = [root]
        while queue_nodes:
            current = queue_nodes.pop(0)
            current_level = levels.get(current, 0)
            for child in sorted(children.get(current, set()), key=lambda x: x.lower()):
                if child in levels:
                    continue
                levels[child] = current_level + 1
                queue_nodes.append(child)

        max_level = max(levels.values()) if levels else 0
        for node in sorted(nodes, key=lambda x: x.lower()):
            if node in levels:
                continue
            max_level += 1
            levels[node] = max_level

        level_to_nodes: dict[int, list[str]] = {}
        for node, level in levels.items():
            level_to_nodes.setdefault(level, []).append(node)

        margin_x = 48.0
        margin_top = 96.0
        margin_bottom = 42.0
        usable_h = max(80.0, float(height) - margin_top - margin_bottom)
        max_depth = max(level_to_nodes.keys()) if level_to_nodes else 0
        positions: dict[str, tuple[float, float]] = {}
        for level in sorted(level_to_nodes.keys()):
            row = sorted(level_to_nodes[level], key=lambda x: x.lower())
            y = margin_top + (usable_h * (float(level) / float(max(1, max_depth))))
            span = max(80.0, float(width) - (margin_x * 2.0))
            for idx, node in enumerate(row):
                x = margin_x + (span * (float(idx + 1) / float(len(row) + 1)))
                positions[node] = (x, y)

        if BROADCAST_NODE in nodes:
            positions[BROADCAST_NODE] = (float(width) / 2.0, margin_top - 42.0)
        return positions

    def _draw_topology_canvas(self, snapshot: TopologySnapshot) -> None:
        canvas = self.topology_canvas
        canvas.delete("all")
        width = max(240, int(canvas.winfo_width()))
        height = max(180, int(canvas.winfo_height()))
        if width < 20 or height < 20:
            return

        mode = str(self.topology_view_var.get() or "tree").strip().lower() or "tree"
        if mode not in TOPOLOGY_VIEW_CHOICES:
            mode = "tree"

        nodes = list(snapshot.nodes)
        if self.local_node_id and self.local_node_id not in nodes:
            nodes.append(self.local_node_id)
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

        focus_node = self._pick_focus_node(snapshot, nodes)
        selected_node = self._selected_node_id()
        if selected_node and selected_node not in nodes:
            selected_node = None
        use_tree_layout = mode in {"tree", "both"} and len(snapshot.relay_links) > 0
        if use_tree_layout:
            positions = self._compute_tree_positions(
                nodes=nodes,
                snapshot=snapshot,
                width=width,
                height=height,
                focus_node=focus_node,
            )
        else:
            positions = self._compute_ring_positions(nodes=nodes, width=width, height=height, focus_node=focus_node)

        legend_x = 10
        legend_y = 10
        legend_w = min(580, width - 20)
        legend_h = 82
        canvas.create_rectangle(
            legend_x,
            legend_y,
            legend_x + legend_w,
            legend_y + legend_h,
            fill="#0f172a",
            outline="#334155",
            width=1,
        )
        if self.local_node_id:
            focus_text = f"自ノード: {self.local_node_id}"
        else:
            focus_text = "自ノード: 未取得（接続直後はノード要求で更新）"
        canvas.create_text(
            legend_x + 10,
            legend_y + 16,
            anchor="w",
            text=focus_text,
            fill="#99f6e4",
            font=("Consolas", 10, "bold"),
        )
        mode_text = "通信フロー(src->dst)"
        if mode == "tree":
            mode_text = "系統図(親->子)"
        elif mode == "both":
            mode_text = "系統図 + 通信フロー"
        canvas.create_text(
            legend_x + 10,
            legend_y + 34,
            anchor="w",
            text=f"表示: {mode_text} / 線太さ: 回数 / 色: 種別",
            fill="#cbd5e1",
            font=("Consolas", 9),
        )
        route_multi_hop, route_max_hops = self._route_stats()
        canvas.create_text(
            legend_x + 10,
            legend_y + 50,
            anchor="w",
            text=f"solid=observed / dash=route hint / ~Hops=inferred / multi_hop_routes={route_multi_hop} max={route_max_hops}",
            fill="#cbd5e1",
            font=("Consolas", 8),
        )
        kind_counts: dict[str, int] = {}
        for edge in snapshot.edges:
            kind_counts[edge.kind] = kind_counts.get(edge.kind, 0) + edge.count
        top_kinds = sorted(kind_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        legend_cursor_x = legend_x + 10
        legend_cursor_y = legend_y + 68
        for kind, count in top_kinds:
            palette = TOPOLOGY_EDGE_COLORS.get(kind, TOPOLOGY_EDGE_COLORS["unknown"])
            canvas.create_rectangle(
                legend_cursor_x,
                legend_cursor_y - 5,
                legend_cursor_x + 10,
                legend_cursor_y + 5,
                fill=palette[0],
                outline="",
            )
            canvas.create_text(
                legend_cursor_x + 14,
                legend_cursor_y,
                anchor="w",
                text=f"{kind}:{count}",
                fill="#cbd5e1",
                font=("Consolas", 8),
            )
            legend_cursor_x += 86

        now_ms = self._now_ms()
        if mode in {"tree", "both"}:
            for link in snapshot.relay_links:
                if link.parent not in positions or link.child not in positions:
                    continue
                x1, y1 = positions[link.parent]
                x2, y2 = positions[link.child]
                width_px = min(8, max(1, 1 + link.count // 2))
                recent = (now_ms - link.last_seen_ms) <= 1500
                tree_color = "#60a5fa" if recent else "#1e3a8a"
                if mode == "both":
                    tree_color = "#334155" if recent else "#1f2937"
                canvas.create_line(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=tree_color,
                    width=width_px,
                    arrow=tk.LAST,
                    arrowshape=(10, 12, 4),
                )
                canvas.create_text(
                    x1 + (x2 - x1) * 0.58,
                    y1 + (y2 - y1) * 0.58 - 10,
                    text=f"relay x{link.count}",
                    fill="#cbd5e1",
                    font=("Consolas", 8),
                )

        if mode in {"flow", "both"}:
            for edge in snapshot.edges:
                if edge.src not in positions or edge.dst not in positions:
                    continue
                x1, y1 = positions[edge.src]
                x2, y2 = positions[edge.dst]
                width_px = min(8, max(1, 1 + edge.count // 2))
                recent = (now_ms - edge.last_seen_ms) <= 1500
                palette = TOPOLOGY_EDGE_COLORS.get(edge.kind, TOPOLOGY_EDGE_COLORS["unknown"])
                color = palette[0] if recent else palette[1]
                if selected_node and selected_node not in {edge.src, edge.dst}:
                    color = "#334155"
                    width_px = max(1, width_px - 1)
                canvas.create_line(
                    x1,
                    y1,
                    x2,
                    y2,
                    fill=color,
                    width=width_px,
                    arrow=tk.LAST,
                    arrowshape=(10, 12, 4),
                )
                mid_x = x1 + (x2 - x1) * 0.62
                mid_y = y1 + (y2 - y1) * 0.62
                canvas.create_text(
                    mid_x,
                    mid_y - 8,
                    text=f"{edge.kind} x{edge.count}",
                    fill="#e2e8f0",
                    font=("Consolas", 9),
                )

        # route_list から学習済み経路のヒント線を重ねる（local node -> dst）
        if self.local_node_id and mode in {"tree", "both"}:
            for route in (self.latest_routes or [])[:120]:
                if not isinstance(route, dict):
                    continue
                dst = str(route.get("dst_node_id") or "").strip()
                next_hop = str(route.get("next_hop_node_id") or "").strip()
                hops = _to_int(str(route.get("hops", 0)), 0)
                if not dst:
                    continue
                if self.local_node_id not in positions or dst not in positions:
                    continue
                if not next_hop:
                    next_hop = dst
                if next_hop not in positions:
                    continue
                x1, y1 = positions[self.local_node_id]
                x2, y2 = positions[next_hop]
                x3, y3 = positions[dst]
                rank = _to_int(route.get("rank"), 0)
                color = "#f59e0b" if rank <= 0 else "#a16207"
                if selected_node and selected_node not in {self.local_node_id, next_hop, dst}:
                    color = "#374151"
                if hops <= 1 or next_hop == dst:
                    canvas.create_line(
                        x1,
                        y1,
                        x3,
                        y3,
                        fill=color,
                        width=1,
                        dash=(4, 4),
                        arrow=tk.LAST,
                        arrowshape=(9, 10, 4),
                    )
                    canvas.create_text(
                        x1 + (x3 - x1) * 0.58,
                        y1 + (y3 - y1) * 0.58 - 12,
                        text="route 1 hop",
                        fill=color,
                        font=("Consolas", 8),
                    )
                else:
                    canvas.create_line(
                        x1,
                        y1,
                        x2,
                        y2,
                        fill=color,
                        width=1,
                        dash=(4, 4),
                        arrow=tk.LAST,
                        arrowshape=(9, 10, 4),
                    )
                    canvas.create_line(
                        x2,
                        y2,
                        x3,
                        y3,
                        fill=color,
                        width=1,
                        dash=((2, 4) if hops == 2 else (1, 6)),
                        arrow=tk.LAST,
                        arrowshape=(9, 10, 4),
                    )
                    route_label = f"route {hops} hops"
                    if hops > 2:
                        route_label = f"route {hops} hops via {self._short_node_id(next_hop)} -> ..."
                    canvas.create_text(
                        x2 + (x3 - x2) * 0.56,
                        y2 + (y3 - y2) * 0.56 - 12,
                        text=route_label,
                        fill=color,
                        font=("Consolas", 8),
                    )

        for node_id, (x, y) in positions.items():
            is_broadcast = node_id == BROADCAST_NODE
            fill = "#334155"
            outline = "#94a3b8"
            node_radius = 16
            role_text = ""
            if is_broadcast:
                fill = "#3f3f46"
                outline = "#f59e0b"
                role_text = "BCAST"
            elif focus_node and node_id == focus_node:
                fill = "#0f766e"
                outline = "#99f6e4"
                node_radius = 18
                canvas.create_oval(x - 23, y - 23, x + 23, y + 23, outline="#99f6e4", width=2)
                role_text = "SELF" if (self.local_node_id and node_id == self.local_node_id) else ""
            if selected_node and node_id == selected_node:
                canvas.create_oval(x - 26, y - 26, x + 26, y + 26, outline="#f59e0b", width=2)
            canvas.create_oval(
                x - node_radius,
                y - node_radius,
                x + node_radius,
                y + node_radius,
                fill=fill,
                outline=outline,
                width=2,
            )
            if role_text:
                canvas.create_text(
                    x,
                    y - 25,
                    text=role_text,
                    fill="#99f6e4",
                    font=("Consolas", 8, "bold"),
                )
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
            src_label = self._short_node_id(edge.src)
            dst_label = self._short_node_id(edge.dst)
            hops_label = self._format_hops_label(src_node=edge.src, observed_hops=edge.hops_max)
            if self.local_node_id:
                if edge.src == self.local_node_id:
                    src_label = f"★ {src_label}"
                if edge.dst == self.local_node_id:
                    dst_label = f"★ {dst_label}"
            age_ms = max(0, snapshot.generated_ms - edge.last_seen_ms)
            self.topology_tree.insert(
                "",
                tk.END,
                values=(
                    src_label,
                    dst_label,
                    edge.via,
                    edge.kind,
                    edge.count,
                    edge.bytes_size,
                    hops_label,
                    edge.retry_total,
                    rssi_label,
                    f"{age_ms}ms",
                ),
            )

    def _refresh_topology_flow_table(self, snapshot: TopologySnapshot) -> None:
        for item in self.topology_flow_tree.get_children():
            self.topology_flow_tree.delete(item)
        for ev in snapshot.flow_events[:TOPOLOGY_FLOW_EVENT_LIMIT]:
            ts_label = datetime.fromtimestamp(ev.ts_ms / 1000.0).strftime("%H:%M:%S")
            src_label = self._short_node_id(ev.src)
            dst_label = self._short_node_id(ev.dst)
            observer_label = self._short_node_id(ev.observer) if ev.observer else "-"
            via_node_label = self._short_node_id(ev.via_node) if ev.via_node else "-"
            path_label = self._format_observed_event_path(ev)
            hops_label = self._format_hops_label(src_node=ev.src, observed_hops=ev.hops)
            if self.local_node_id:
                if ev.src == self.local_node_id:
                    src_label = f"★ {src_label}"
                if ev.observer == self.local_node_id:
                    observer_label = f"★ {observer_label}"
            msg_label = ev.msg_id or ev.e2e_id or "-"
            if ev.hop_note:
                msg_label = ev.hop_note if msg_label == "-" else f"{msg_label} {ev.hop_note}"
            self.topology_flow_tree.insert(
                "",
                tk.END,
                values=(
                    ts_label,
                    ev.kind,
                    src_label,
                    dst_label,
                    observer_label,
                    via_node_label,
                    self._shorten(path_label, 36),
                    hops_label,
                    self._shorten(msg_label, 24),
                ),
            )

    def _refresh_topology_route_table(self, snapshot: TopologySnapshot) -> None:
        tree = getattr(self, "topology_route_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        routes = self.latest_routes if isinstance(self.latest_routes, list) else []
        for route in routes[:240]:
            if not isinstance(route, dict):
                continue
            dst = str(route.get("dst_node_id") or route.get("dst") or "").strip()
            next_hop = str(route.get("next_hop_node_id") or route.get("next_hop") or "").strip()
            if not dst:
                continue
            rank_value = route.get("rank", 0)
            rank = "backup" if _to_int(str(rank_value), 0) > 0 else "primary"
            hops = _to_int(route.get("hops"), 0)
            metric = _to_int(route.get("metric_q8"), 0)
            age_ms = _to_int(route.get("age_ms"), max(0, snapshot.generated_ms - _to_int(route.get("learned_ms"), 0)))
            dst_label = self._short_node_id(dst)
            next_label = self._short_node_id(next_hop) if next_hop else "-"
            path_label = self._format_route_path(dst=dst, next_hop=next_hop, hops=hops)
            if self.local_node_id:
                if dst == self.local_node_id:
                    dst_label = f"★ {dst_label}"
                if next_hop == self.local_node_id:
                    next_label = f"★ {next_label}"
            tree.insert(
                "",
                tk.END,
                values=(dst_label, next_label, path_label, rank, hops, metric, f"{max(0, age_ms)}ms"),
            )

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if ports and (self.port_var.get() not in ports):
            self.port_var.set(ports[0])
        self.refresh_flash_port_selector()
        self.append_log(f"COM一覧更新: {ports if ports else 'なし'}", level="SYSTEM", category="COM")

    def refresh_flash_port_selector(self) -> None:
        frame = getattr(self, "flash_ports_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        ports = list_serial_ports()
        self.flash_port_vars = {}
        if not ports:
            ttk.Label(frame, text="利用可能なCOMポートが見つかりません。", foreground="#6b7280").grid(
                row=0, column=0, sticky="w"
            )
            return
        for idx, port in enumerate(ports):
            var = tk.BooleanVar(value=True)
            self.flash_port_vars[port] = var
            ttk.Checkbutton(frame, text=port, variable=var).grid(
                row=idx // 6,
                column=idx % 6,
                sticky="w",
                padx=(0, 10),
                pady=2,
            )

    def _selected_flash_ports(self) -> list[str]:
        if not self.flash_port_vars:
            return list_serial_ports()
        selected = [port for port, var in self.flash_port_vars.items() if bool(var.get())]
        return sorted(selected)

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
        ports = self._selected_flash_ports()
        if not ports:
            messagebox.showwarning("ポート未選択", "書き込み対象のCOMポートを1つ以上選択してください。")
            return
        should_start = messagebox.askyesno(
            "複数ポート書き込み",
            f"以下の{len(ports)}ポートへ書き込みします。\n{', '.join(ports)}\n\n開始しますか？",
        )
        if not should_start:
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
        self.pending_ping_rounds.clear()
        self.pending_e2e.clear()
        self.long_text_seen.clear()
        self.long_text_rx_sessions.clear()
        self.reliable_tx_sessions.clear()
        self.reliable_rx_sessions.clear()
        self.reliable_auto_state_by_dst.clear()
        self.latest_routes.clear()
        self.mesh_stats_snapshot.clear()
        self.mesh_stats_baseline = None
        self.topology_tracker.clear()
        self.topology_status_var.set("未更新")
        self.mesh_route_stats_var.set("経路統計: 未取得")
        self.local_node_id = None
        self.last_route_list_rx_ms = 0
        self.last_routes_request_tx_ms = 0
        self.last_stats_rx_ms = 0
        self.last_stats_request_tx_ms = 0
        self.self_node_var.set("未取得")
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
        self.after(20 if backlog > 0 else 80, self.poll_worker_events)

    def _is_stale_worker_event(self, event: dict[str, Any]) -> bool:
        event_worker_id = str(event.get("_worker_id") or "").strip()
        if not event_worker_id:
            return False
        current = self.worker
        if current is None:
            return False
        current_worker_id = str(getattr(current, "worker_id", "") or "").strip()
        return bool(current_worker_id) and (current_worker_id != event_worker_id)

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

        if event_type in {"status", "error", "tx", "rx", "rx_raw"} and self._is_stale_worker_event(event):
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
                kind = self._payload_type(payload)
                if self._should_log_worker_payload(event_type="tx", kind=kind):
                    self.append_log(self._summarize_payload(payload), level="TX", category=kind)
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
                if self._should_log_worker_payload(event_type="rx", kind=kind):
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
                previous = self.local_node_id
                self.local_node_id = node_id
                self.self_node_var.set(node_id)
                if previous != node_id:
                    self.topology_tracker.clear()
                self.registry.upsert_from_payload({"node_id": node_id, "last_seen_ms": self._now_ms()})
                self.refresh_node_table()
                self._mark_topology_dirty()
                self.append_log(f"bridge_ready: local node={node_id}", level="SYSTEM", category="COM")
            return

        if message_type in {"mesh_observed", "mesh_trace"}:
            # トポロジ表示向けの観測イベント。チャット表示などには流さない。
            return

        if message_type in {"node_list", "nodes"}:
            nodes = payload.get("nodes") or payload.get("items")
            if isinstance(nodes, list):
                self.last_node_list_rx_ms = self._now_ms()
                if self.nodes_request_retry_after_id is not None:
                    try:
                        self.after_cancel(self.nodes_request_retry_after_id)
                    except Exception:
                        pass
                    self.nodes_request_retry_after_id = None
                count = _to_int(str(payload.get("count", len(nodes))), len(nodes))
                total = _to_int(str(payload.get("total", count)), count)
                truncated = bool(payload.get("truncated"))
                if truncated or (total > count):
                    self.append_log(
                        f"node_list truncated: count={count} total={total} (再要求推奨)",
                        level="WARN",
                        category="TOPO",
                    )
                self.registry.update_from_list(nodes)
                self.topology_tracker.update_node_records(nodes)
                if not self.local_node_id:
                    inferred = self._infer_local_node_id_from_entries(nodes)
                    if inferred:
                        self.local_node_id = inferred
                        self.self_node_var.set(f"{inferred} (推定)")
                        self._mark_topology_dirty()
                        self.append_log(f"自ノードを node_list から推定: {inferred}", level="SYSTEM", category="TOPO")
                self.refresh_node_table()
            return

        if message_type in {"route_list", "routes"}:
            routes = payload.get("routes") or payload.get("items")
            if isinstance(routes, list):
                self.latest_routes = [r for r in routes if isinstance(r, dict)]
            else:
                self.latest_routes = []
            self.last_route_list_rx_ms = self._now_ms()
            self.topology_status_var.set(f"経路情報: {len(self.latest_routes)}")
            self._mark_topology_dirty()
            return

        if message_type == "stats":
            now = self._now_ms()
            mesh_raw = payload.get("mesh")
            if isinstance(mesh_raw, dict):
                self.mesh_stats_snapshot = {
                    str(key): _to_int(str(value), 0)
                    for key, value in mesh_raw.items()
                }
                self.last_stats_rx_ms = now
                if self.continuous_after_id is not None and self.mesh_stats_baseline is None:
                    self.mesh_stats_baseline = dict(self.mesh_stats_snapshot)
                if self.continuous_after_id is None:
                    self.mesh_stats_baseline = None
                self._update_mesh_route_stats_view()
            return

        skip_node_refresh = message_type in {
            "long_text_chunk",
            "long_text_end",
            "reliable_1k_start",
            "reliable_1k_chunk",
            "reliable_1k_repair",
            "reliable_1k_end",
            "reliable_1k_nack",
            "reliable_1k_result",
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

        if message_type in {"long_text_start", "long_text_chunk", "long_text_end"}:
            self.handle_long_text_payload(payload)
            return

        if message_type in {
            "reliable_1k_start",
            "reliable_1k_chunk",
            "reliable_1k_end",
            "reliable_1k_nack",
            "reliable_1k_repair",
            "reliable_1k_result",
        }:
            self.handle_reliable_payload(payload)
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
        hop_suffix = self._hop_log_suffix(
            src_node=str(payload.get("src") or entry.get("dst") or "").strip(),
            observed_hops=self._payload_hops(payload),
            request_hops=self._payload_request_hops(payload),
        )
        expected_type = str(entry.get("type") or "").strip().lower()
        if self._is_high_volume_message_type(expected_type):
            return
        self.append_log(
            (
                f"delivery ok: type={entry.get('type')} dst={entry.get('dst')} "
                f"e2e_id={e2e_id} retries={retry_count} elapsed={elapsed_ms}ms{hop_suffix}"
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

    def handle_reliable_payload(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type") or "").strip().lower()
        r1k_id = str(payload.get("r1k_id") or "").strip().lower()
        if not r1k_id:
            return
        now = self._now_ms()
        completed_ms = int(self.reliable_rx_completed.get(r1k_id) or 0)
        if completed_ms > 0 and (now - completed_ms) <= self.reliable_rx_session_timeout_ms:
            return

        def ensure_rx_session(*, reason: str) -> dict[str, Any]:
            session = self.reliable_rx_sessions.get(r1k_id)
            if session is not None:
                return session
            self._ensure_session_capacity(self.reliable_rx_sessions, "r1k", r1k_id)
            session = {
                "r1k_id": r1k_id,
                "src": str(payload.get("src") or "").strip(),
                "dst": str(payload.get("dst") or "").strip(),
                "profile_id": 0,
                "profile_name": "",
                "size": 0,
                "data_shards": 0,
                "parity_shards": 0,
                "shard_size": 0,
                "shards": {},
                "started_ms": now,
                "last_update_ms": now,
                "start_received": False,
                "end_received": False,
                "last_nack_ms": 0,
            }
            self.reliable_rx_sessions[r1k_id] = session
            if reason != "start":
                self.append_log(
                    f"reliable session created: id={r1k_id} reason={reason}",
                    level="WARN",
                    category="R1K",
                )
            return session

        def merge_rx_meta(session: dict[str, Any]) -> None:
            src = str(payload.get("src") or "").strip()
            if src:
                session["src"] = src
            dst = str(payload.get("dst") or "").strip()
            if dst:
                session["dst"] = dst
            for key in ("profile_id", "size", "data_shards", "parity_shards", "shard_size"):
                raw = payload.get(key)
                if raw is None:
                    continue
                value = _to_int(str(raw), -1)
                if value >= 0:
                    session[key] = value
            profile_name = str(payload.get("profile_name") or "").strip()
            if profile_name:
                session["profile_name"] = profile_name

        def try_finalize_reliable(
            session: dict[str, Any],
            *,
            allow_nack: bool,
            now_ms: int,
        ) -> bool:
            profile_id = int(session.get("profile_id") or 0)
            size = int(session.get("size") or 0)
            shards_b64 = session.get("shards")
            if not isinstance(shards_b64, dict):
                shards_b64 = {}
                session["shards"] = shards_b64
            present_indexes = sorted(int(idx) for idx in shards_b64.keys())

            try:
                restored = decode_reliable_1k_from_shards(
                    shard_map_b64={int(idx): str(val) for idx, val in shards_b64.items()},
                    profile_id=profile_id,
                    original_size=size,
                )
            except Exception:
                restored = None

            if restored is not None:
                src = str(session.get("src") or payload.get("src") or "?")
                try:
                    text = restored.decode("utf-8")
                except UnicodeDecodeError:
                    text = restored.decode("utf-8", errors="replace")
                preview = self._shorten(text, 160)
                self.append_chat(f"{src}(wifi): [reliable_1k {len(restored)}B] {preview}")
                latency_ms = max(0, now_ms - int(session.get("started_ms") or now_ms))
                missing_before: list[int] = []
                try:
                    missing_before = missing_reliable_shards(present_indexes=present_indexes, profile_id=profile_id)
                except Exception:
                    missing_before = []
                self.reliable_stats.register_success(latency_ms=latency_ms)
                if src and src.lower() != "pc":
                    self._send_reliable_result(
                        dst=src,
                        r1k_id=r1k_id,
                        status="ok",
                        recovered=len(missing_before),
                        latency_ms=latency_ms,
                    )
                self.reliable_rx_sessions.pop(r1k_id, None)
                self.reliable_rx_completed[r1k_id] = now_ms
                self.append_log(
                    (
                        f"reliable_1k 復元成功: id={r1k_id} src={src} bytes={len(restored)} "
                        f"missing={len(missing_before)} latency={latency_ms}ms"
                    ),
                    level="SYSTEM",
                    category="R1K",
                )
                self.update_stats_view()
                return True

            try:
                missing_indexes = missing_reliable_shards(present_indexes=present_indexes, profile_id=profile_id)
            except Exception:
                missing_indexes = []

            src = str(session.get("src") or payload.get("src") or "").strip()
            if allow_nack and src and missing_indexes:
                last_nack_ms = int(session.get("last_nack_ms") or 0)
                if (now_ms - last_nack_ms) >= 500:
                    nack = make_reliable_1k_nack_message(
                        r1k_id=r1k_id,
                        dst=src,
                        missing_indexes=missing_indexes,
                        ttl=self._current_ttl(),
                    )
                    if self.send_json(nack):
                        self._register_pending_e2e(nack)
                        self.reliable_stats.register_nack()
                        session["last_nack_ms"] = now_ms
                        session["last_update_ms"] = now_ms
                        self.append_log(
                            (
                                f"reliable_1k NACK送信: id={r1k_id} src={src} "
                                f"missing={missing_indexes[:10]} total={len(missing_indexes)}"
                            ),
                            level="WARN",
                            category="R1K",
                        )
                        self.update_stats_view()
                return False

            if src and src.lower() != "pc":
                self._send_reliable_result(
                    dst=src,
                    r1k_id=r1k_id,
                    status="decode_failed",
                    missing=missing_indexes if missing_indexes else None,
                )
            self.reliable_stats.register_failure("decode_failed")
            self.reliable_rx_sessions.pop(r1k_id, None)
            self.update_stats_view()
            return True

        if kind == "reliable_1k_start":
            session = ensure_rx_session(reason="start")
            merge_rx_meta(session)
            first_start = not bool(session.get("start_received"))
            session["start_received"] = True
            session["last_update_ms"] = now
            if first_start:
                self.append_log(
                    (
                        f"reliable_1k 受信開始: id={r1k_id} src={session.get('src') or '?'} "
                        f"profile={session.get('profile_id')} size={session.get('size')}"
                    ),
                    level="SYSTEM",
                    category="R1K",
                )
            return

        if kind in {"reliable_1k_chunk", "reliable_1k_repair"}:
            session = ensure_rx_session(reason=("repair_before_start" if kind.endswith("repair") else "chunk_before_start"))
            merge_rx_meta(session)
            idx = payload.get("index")
            if not isinstance(idx, int):
                idx = _to_int(str(idx), -1)
            if idx < 0:
                return
            data_b64 = payload.get("data_b64")
            if not isinstance(data_b64, str) or not data_b64:
                return
            shards = session.get("shards")
            if not isinstance(shards, dict):
                shards = {}
                session["shards"] = shards
            shards[idx] = data_b64
            session["last_update_ms"] = now
            if bool(session.get("start_received")) and not bool(session.get("end_received")):
                data_shards = int(session.get("data_shards") or 0)
                if data_shards > 0 and len(shards) >= data_shards:
                    try_finalize_reliable(session, allow_nack=False, now_ms=now)
            if kind == "reliable_1k_repair" and bool(session.get("end_received")):
                try_finalize_reliable(session, allow_nack=True, now_ms=now)
            return

        if kind == "reliable_1k_end":
            session = ensure_rx_session(reason="end_before_start")
            merge_rx_meta(session)
            session["end_received"] = True
            session["last_update_ms"] = now
            try_finalize_reliable(session, allow_nack=True, now_ms=now)
            return

        if kind == "reliable_1k_nack":
            session = self.reliable_tx_sessions.get(r1k_id)
            if session is None:
                return
            missing_raw = payload.get("missing")
            if not isinstance(missing_raw, list):
                return
            missing_indexes: list[int] = []
            for value in missing_raw:
                idx = _to_int(str(value), -1)
                if idx >= 0 and idx not in missing_indexes:
                    missing_indexes.append(idx)
            if not missing_indexes:
                return
            dst = str(session.get("dst") or "").strip()
            shards_b64 = session.get("shards_b64")
            if not dst or not isinstance(shards_b64, list):
                return
            sent_repairs = 0
            for idx in missing_indexes:
                if idx < 0 or idx >= len(shards_b64):
                    continue
                repair = make_reliable_1k_repair_message(
                    r1k_id=r1k_id,
                    dst=dst,
                    index=idx,
                    shard_b64=str(shards_b64[idx]),
                    ttl=self._current_ttl(),
                )
                if not self.send_json(repair):
                    continue
                self._register_pending_e2e(repair)
                sent_repairs += 1
            if sent_repairs <= 0:
                return
            session["nack_count"] = int(session.get("nack_count") or 0) + 1
            session["repair_packets"] = int(session.get("repair_packets") or 0) + sent_repairs
            session["last_update_ms"] = now
            session["result_deadline_ms"] = now + self.reliable_result_deadline_ms
            self.reliable_stats.register_repair(sent_repairs)
            self.reliable_stats.register_retry(sent_repairs)
            self.append_log(
                f"reliable_1k repair送信: id={r1k_id} dst={dst} repairs={sent_repairs}",
                level="SYSTEM",
                category="R1K",
            )
            self.update_stats_view()
            return

        if kind == "reliable_1k_result":
            session = self.reliable_tx_sessions.pop(r1k_id, None)
            if session is None:
                return
            self._clear_pending_e2e_for_r1k(r1k_id)
            status = str(payload.get("status") or "").strip().lower()
            if not status:
                status = "unknown"
            dst = str(session.get("dst") or "").strip()
            elapsed_ms = max(0, now - int(session.get("start_ms") or now))
            retry_packets = int(session.get("retry_packets") or 0) + int(session.get("repair_packets") or 0)
            total_packets = int(session.get("packet_count") or 0) + int(session.get("repair_packets") or 0)
            latency_raw = payload.get("latency_ms")
            latency_ms = _to_int(str(latency_raw), elapsed_ms)

            if status == "ok":
                self.reliable_stats.register_success(latency_ms=latency_ms)
            else:
                self.reliable_stats.register_failure(status)
            self._apply_reliable_adaptation(
                dst=dst,
                success=(status == "ok"),
                nack_count=int(session.get("nack_count") or 0),
                retry_packets=retry_packets,
                total_packets=total_packets,
            )
            self.append_log(
                (
                    f"reliable_1k result: id={r1k_id} dst={dst} status={status} "
                    f"nacks={session.get('nack_count', 0)} retries={retry_packets} elapsed={elapsed_ms}ms"
                ),
                level=("SYSTEM" if status == "ok" else "WARN"),
                category="R1K",
            )
            self.update_stats_view()
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
        try:
            dst = self._normalize_target(self.chat_target_var.get(), context="チャット宛先", show_error=True)
        except ValueError:
            return
        via = self.chat_via_var.get().strip() or "wifi"
        if via == "wifi" and not self._ensure_directed_target(dst, operation="チャット送信"):
            return
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

    def send_reliable_1k(self) -> None:
        mode = (self.reliable_mode_var.get() or "").strip().lower()
        if mode != "reliable_1k":
            messagebox.showwarning("モード不一致", "Reliable送信には mode=reliable_1k を選択してください。")
            return
        if self.continuous_after_id is not None:
            messagebox.showwarning("実行中", "連続Ping実行中は Reliable 送信できません。先に停止してください。")
            return
        now_guard = self._now_ms()
        active_sessions = [
            r1k_id
            for r1k_id, session in self.reliable_tx_sessions.items()
            if int(session.get("result_deadline_ms") or 0) > now_guard
        ]
        if active_sessions:
            messagebox.showwarning(
                "送信中",
                f"Reliable 送信は1セッションずつ実行してください。未完了: {len(active_sessions)}件",
            )
            self.append_log(
                f"reliable_1k send blocked: pending_sessions={len(active_sessions)}",
                level="WARN",
                category="R1K",
            )
            return
        try:
            dst = self._normalize_target(self.ping_target_var.get(), context="Reliable宛先", show_error=True)
        except ValueError:
            return
        if dst is None:
            messagebox.showwarning("宛先エラー", "Reliable 1KB は Broadcast 送信できません。Ping宛先を指定してください。")
            return
        if not self._ensure_directed_target(dst, operation="Reliable 1KB送信"):
            return

        ttl = self._current_ttl()
        profile_id = self._resolve_reliable_profile_for_send(dst)
        profile_name = RELIABLE_PROFILE_ID_TO_NAME.get(profile_id, "25+8")
        r1k_id = uuid.uuid4().hex[:12]
        text = self._reliable_payload_text(r1k_id, dst)
        self.append_log(
            f"reliable_1k send start: id={r1k_id} dst={dst} mode={mode} profile={profile_name} ttl={ttl}",
            level="SYSTEM",
            category="R1K",
        )
        try:
            packets, meta = make_reliable_1k_messages(
                text=text,
                dst=dst,
                via="wifi",
                ttl=ttl,
                profile_id=profile_id,
                r1k_id=r1k_id,
                require_ack=True,
            )
        except Exception as exc:
            messagebox.showerror("Reliable送信エラー", str(exc))
            return

        now = self._now_ms()
        session_id = str(meta.get("r1k_id") or r1k_id)
        self.reliable_stats.register_sent(profile_name=profile_name, packet_count=len(packets))
        self.reliable_tx_sessions[session_id] = {
            "r1k_id": session_id,
            "dst": dst,
            "profile_id": int(meta.get("profile_id", profile_id)),
            "profile_name": str(meta.get("profile_name") or profile_name),
            "shards_b64": list(meta.get("shards_b64") or []),
            "start_ms": now,
            "last_update_ms": now,
            "packet_count": len(packets),
            "nack_count": 0,
            "repair_packets": 0,
            "retry_packets": 0,
            "ping_seq": None,
            "status": "pending",
            "result_deadline_ms": now + self.reliable_result_deadline_ms,
        }

        worker = self.worker
        for payload in packets:
            if worker is not None and worker.is_running:
                max_q = max(1, int(worker.tx_queue_max))
                if int(worker.tx_queue_size) >= int(max_q * 0.80):
                    time.sleep(0.003)
                if int(worker.tx_queue_size) >= int(max_q * 0.50):
                    time.sleep(0.006)
            if not self.send_json(payload):
                self.reliable_tx_sessions.pop(session_id, None)
                self.reliable_stats.register_failure("tx_enqueue_failed")
                self.append_log(
                    f"reliable_1k send failed: id={session_id} type={payload.get('type')}",
                    level="ERROR",
                    category="R1K",
                )
                self.update_stats_view()
                return
            if payload.get("need_ack"):
                self._register_pending_e2e(payload)
            # 送信バーストを避けて FW 側の JSON parse 崩れを防ぐ。
            time.sleep(0.004)

        probe_sent = False
        if worker is not None and worker.is_running:
            max_q = max(1, int(worker.tx_queue_max))
            if int(worker.tx_queue_size) < int(max_q * 0.25) and len(self.pending_e2e) <= 8:
                probe_sent = self.send_ping()
            else:
                self.append_log(
                    "reliable_1k: queue/pending高負荷のため probe をスキップ",
                    level="WARN",
                    category="R1K",
                )
        else:
            probe_sent = self.send_ping()
        if probe_sent:
            self.reliable_tx_sessions[session_id]["ping_seq"] = self.ping_seq
        self.append_log(
            f"reliable_1k send queued: id={session_id} packets={len(packets)} probe={'ok' if probe_sent else 'ng'}",
            level="SYSTEM",
            category="R1K",
        )
        self.update_stats_view()

    def request_nodes(self) -> None:
        self.send_json(make_nodes_request())
        sent_at_ms = self._now_ms()
        retry_delay_ms = 1200

        def _retry_once() -> None:
            self.nodes_request_retry_after_id = None
            if self.last_node_list_rx_ms >= sent_at_ms:
                return
            self.append_log("node_list応答待ちタイムアウト。再要求します。", level="WARN", category="TOPO")
            self.send_json(make_nodes_request())

        if self.nodes_request_retry_after_id is not None:
            try:
                self.after_cancel(self.nodes_request_retry_after_id)
            except Exception:
                pass
            self.nodes_request_retry_after_id = None
        self.nodes_request_retry_after_id = self.after(retry_delay_ms, _retry_once)

    def _request_routes_if_needed(self, *, force: bool, interactive: bool = False) -> bool:
        worker = self.worker
        if worker is None or not worker.is_running:
            if interactive:
                messagebox.showwarning("Not Connected", "Connect a COM port before requesting routes.")
            return False
        now_ms = self._now_ms()
        if not force:
            if (now_ms - self.last_routes_request_tx_ms) < ROUTE_REQUEST_MIN_INTERVAL_MS:
                return False
            if self.last_route_list_rx_ms > 0 and (now_ms - self.last_route_list_rx_ms) < ROUTE_REQUEST_STALE_MS:
                return False
        if not self.send_json(make_routes_request()):
            return False
        self.last_routes_request_tx_ms = now_ms
        return True

    def request_routes(self) -> None:
        self._request_routes_if_needed(force=True, interactive=True)

    def _request_mesh_stats_if_needed(self, *, force: bool, interactive: bool = False) -> bool:
        worker = self.worker
        if worker is None or not worker.is_running:
            if interactive:
                messagebox.showwarning("未接続", "先にCOMポートへ接続してください。")
            return False
        now_ms = self._now_ms()
        if not force:
            if (now_ms - self.last_stats_request_tx_ms) < MESH_STATS_REQUEST_MIN_INTERVAL_MS:
                return False
            if self.last_stats_rx_ms > 0 and (now_ms - self.last_stats_rx_ms) < MESH_STATS_REQUEST_MIN_INTERVAL_MS:
                return False
        if not self.send_json({"cmd": "get_stats"}):
            return False
        self.last_stats_request_tx_ms = now_ms
        return True

    def request_mesh_stats(self) -> None:
        self._request_mesh_stats_if_needed(force=True, interactive=True)

    def _send_ping_with_context(self, *, dst: str | None, ttl: int) -> bool:
        self.ping_seq += 1
        seq = self.ping_seq
        ping_id = uuid.uuid4().hex[:8]
        payload = make_ping_probe_command(
            seq=seq,
            dst=dst,
            ping_id=ping_id,
            via="wifi",
            ttl=ttl,
            probe_bytes=PING_PROBE_BYTES,
        )
        if not self.send_json(payload):
            return False
        sent_ms = self._now_ms()
        self.ping_stats.register_sent(seq, sent_ts_ms=sent_ms, dst=(dst or "all"))
        self.pending_ping_rounds[seq] = {
            "ping_id": ping_id,
            "sent_ms": sent_ms,
            "dst": dst or BROADCAST_LABEL,
            "is_broadcast": dst is None,
            "probe_bytes": PING_PROBE_BYTES,
            "responders": set(),
            "registered_first": False,
            "response_deadline_ms": sent_ms + PING_BROADCAST_RESPONSE_WINDOW_MS,
        }
        self.update_stats_view()
        return True

    def send_ping(self) -> bool:
        try:
            dst = self._normalize_target(self.ping_target_var.get(), context="Ping宛先", show_error=True)
        except ValueError:
            return False
        if not self._ensure_directed_target(dst, operation="Ping送信(1KB)"):
            return False
        ttl = self._current_ttl()
        return self._send_ping_with_context(dst=dst, ttl=ttl)

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

        round_info = self.pending_ping_rounds.get(seq)
        if round_info is None:
            return
        expected_ping_id = str(round_info.get("ping_id") or "")
        received_ping_id = payload.get("ping_id")
        if isinstance(expected_ping_id, str) and expected_ping_id:
            if str(received_ping_id or "") != expected_ping_id:
                sent_ms = int(round_info.get("sent_ms") or 0)
                age_ms = max(0, self._now_ms() - sent_ms) if sent_ms > 0 else None
                if age_ms is not None and age_ms > PING_PENDING_MAX_AGE_MS:
                    self.pending_ping_rounds.pop(seq, None)
                    self.ping_stats.expire_pending(seq)
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

        if payload.get("probe_hash_ok") is False:
            self.append_log(
                f"pong integrity NG: seq={seq} src={payload.get('src')} ping_id={received_ping_id}",
                level="WARN",
                category="PING",
            )
            return

        now_ms = self._now_ms()
        if bool(round_info.get("is_broadcast")):
            deadline = int(round_info.get("response_deadline_ms") or 0)
            if deadline > 0 and now_ms > deadline:
                self.pending_ping_rounds.pop(seq, None)
                self.ping_stats.expire_pending(seq)
                return

        src_raw = payload.get("src") or payload.get("from")
        src = str(src_raw).strip() if isinstance(src_raw, str) else ""
        if src and not bool(round_info.get("is_broadcast")):
            expected_dst = str(round_info.get("dst") or "").strip().lower()
            if expected_dst and src.lower() != expected_dst:
                self.append_log(
                    (
                        f"pong ignored: seq={seq} src mismatch expected={round_info.get('dst')} "
                        f"got={src} ping_id={received_ping_id}"
                    ),
                    level="WARN",
                    category="PING",
                )
                return
        responders = round_info.get("responders")
        if not isinstance(responders, set):
            responders = set()
            round_info["responders"] = responders
        if src:
            if src in responders:
                return
            responders.add(src)

        sent_ms = int(round_info.get("sent_ms") or self._now_ms())
        measured: float | None = None
        if not bool(round_info.get("registered_first")):
            measured = self.ping_stats.register_received(
                seq,
                recv_ts_ms=now_ms,
                latency_ms=None,
                dst=str(round_info.get("dst") or "all"),
            )
            round_info["registered_first"] = True
        else:
            measured = float(max(0, now_ms - sent_ms))

        if src and measured is not None:
            self.registry.upsert_from_payload({"node_id": src, "latency_ms": measured})
            self.refresh_node_table()

        if not bool(round_info.get("is_broadcast")):
            self.pending_ping_rounds.pop(seq, None)
        else:
            deadline = int(round_info.get("response_deadline_ms") or 0)
            if deadline > 0 and now_ms >= deadline:
                self.pending_ping_rounds.pop(seq, None)
                self.ping_stats.expire_pending(seq)

        self.update_stats_view()

    def start_continuous_ping(self) -> None:
        if self.continuous_after_id is not None:
            return
        interval_ms = self._clamp_continuous_interval_ms(_to_int(self.interval_var.get(), 1000), apply_to_ui=True)
        try:
            dst = self._normalize_target(self.ping_target_var.get(), context="連続Ping宛先", show_error=True)
        except ValueError:
            return
        if not self._ensure_directed_target(dst, operation="連続Ping"):
            return
        ttl = self._current_ttl()
        count = max(0, _to_int(self.count_var.get(), 0))
        self.continuous_remaining = count if count > 0 else None
        self.continuous_dynamic_interval_ms = interval_ms
        self.continuous_context = {"dst": dst, "ttl": ttl}
        self.continuous_interval_last_log_ms = self._now_ms()
        self.mesh_stats_baseline = dict(self.mesh_stats_snapshot) if self.mesh_stats_snapshot else None
        self.start_test_btn.configure(state=tk.DISABLED)
        self.stop_test_btn.configure(state=tk.NORMAL)
        self._set_continuous_controls_enabled(False)
        self.append_log(
            (
                f"連続Ping開始: interval={interval_ms}ms, "
                f"count={'∞' if self.continuous_remaining is None else self.continuous_remaining}, "
                f"dst={self._target_label(dst)}, ttl={ttl}, probe={PING_PROBE_BYTES}B"
            ),
            level="SYSTEM",
            category="PING",
        )
        self._request_mesh_stats_if_needed(force=True)
        self._run_continuous_ping(interval_ms)

    def _clamp_continuous_interval_ms(self, interval_ms: int, *, apply_to_ui: bool = False) -> int:
        interval = int(interval_ms)
        if interval < self.continuous_interval_min_ms:
            interval = self.continuous_interval_min_ms
        if interval > self.continuous_interval_max_ms:
            interval = self.continuous_interval_max_ms
        if apply_to_ui and str(interval) != self.interval_var.get().strip():
            self.interval_var.set(str(interval))
        return interval

    def _set_continuous_controls_enabled(self, enabled: bool) -> None:
        target_combo_state = "readonly" if enabled else tk.DISABLED
        entry_state = tk.NORMAL if enabled else tk.DISABLED
        mode_combo_state = "readonly" if enabled else tk.DISABLED
        profile_combo = getattr(self, "reliable_profile_combo", None)
        if self.ping_target_combo is not None:
            self.ping_target_combo.configure(state=target_combo_state)
        if self.interval_entry is not None:
            self.interval_entry.configure(state=entry_state)
        if self.count_entry is not None:
            self.count_entry.configure(state=entry_state)
        if self.ttl_entry is not None:
            self.ttl_entry.configure(state=entry_state)
        if self.reliable_mode_combo is not None:
            self.reliable_mode_combo.configure(state=mode_combo_state)
        if profile_combo is not None:
            profile_combo.configure(state=("readonly" if enabled and not self.reliable_auto_var.get() else tk.DISABLED))

    def _next_continuous_interval_ms(self, base_interval_ms: int) -> int:
        base_interval = self._clamp_continuous_interval_ms(base_interval_ms, apply_to_ui=False)
        interval = self._clamp_continuous_interval_ms(
            int(self.continuous_dynamic_interval_ms or base_interval), apply_to_ui=False
        )

        if not self._is_hardened_mode_enabled():
            self.continuous_dynamic_interval_ms = base_interval
            return base_interval

        pending_ping = len(self.pending_ping_rounds)
        pending_e2e = len(self.pending_e2e)
        queue_size = 0
        worker = self.worker
        if worker is not None and worker.is_running:
            queue_size = int(worker.tx_queue_size)
        ping_snapshot = self.ping_stats.snapshot()
        sent = int(ping_snapshot.get("sent", 0))
        pdr = float(ping_snapshot.get("pdr", 0.0))

        if queue_size >= 48 or pending_ping >= 6 or pending_e2e >= 12:
            interval = min(self.continuous_interval_max_ms, int(interval * 1.15) + 25)
        elif sent >= 20 and pdr < 88.0:
            interval = min(self.continuous_interval_max_ms, int(interval * 1.10) + 20)
        elif pending_ping == 0 and pending_e2e == 0 and sent >= 20 and pdr > 96.0:
            interval = max(base_interval, int(interval * 0.92) - 10)

        now = self._now_ms()
        if abs(interval - int(self.continuous_dynamic_interval_ms or base_interval)) >= 80 and (
            now - self.continuous_interval_last_log_ms
        ) >= 4000:
            self.continuous_interval_last_log_ms = now
            self.append_log(
                (
                    f"連続Pingレート調整: interval={interval}ms "
                    f"(queue={queue_size} pending_ping={pending_ping} pending_e2e={pending_e2e} pdr={pdr:.1f}%)"
                ),
                level="SYSTEM",
                category="PING",
            )
        self.continuous_dynamic_interval_ms = interval
        return interval

    def _run_continuous_ping(self, interval_ms: int) -> None:
        next_interval_ms = self._next_continuous_interval_ms(interval_ms)
        context = self.continuous_context or {}
        dst = context.get("dst")
        ttl = _to_int(str(context.get("ttl", self._current_ttl())), self._current_ttl())
        self._request_routes_if_needed(force=False)
        self._request_mesh_stats_if_needed(force=False)
        if not self._send_ping_with_context(dst=dst, ttl=ttl):
            self.stop_continuous_ping()
            return
        if self.continuous_remaining is not None:
            self.continuous_remaining -= 1
            if self.continuous_remaining <= 0:
                self.stop_continuous_ping()
                return
        self.continuous_after_id = self.after(next_interval_ms, lambda: self._run_continuous_ping(interval_ms))

    def stop_continuous_ping(self) -> None:
        if self.continuous_after_id is not None:
            self.after_cancel(self.continuous_after_id)
            self.continuous_after_id = None
            self.append_log("連続Ping停止", level="SYSTEM", category="PING")
        self.start_test_btn.configure(state=tk.NORMAL)
        self.stop_test_btn.configure(state=tk.DISABLED)
        self.continuous_remaining = None
        self.continuous_dynamic_interval_ms = None
        self.continuous_context = None
        self.mesh_stats_baseline = None
        self._update_mesh_route_stats_view()
        self._set_continuous_controls_enabled(True)
        self._sync_reliable_controls()

    def _record_quality_point(self, snapshot: dict[str, float | int]) -> None:
        signature = (
            int(snapshot["sent"]),
            int(snapshot["received"]),
            int(snapshot["lost"]),
            int(round(float(snapshot["avg_ms"]) * 10.0)),
            int(round(float(snapshot["p95_ms"]) * 10.0)),
        )
        now = self._now_ms()
        if self._quality_last_signature == signature and self.quality_points:
            last_ts = int(self.quality_points[-1].get("ts_ms", 0))
            if (now - last_ts) < QUALITY_GRAPH_DUPLICATE_WINDOW_MS:
                return
        self._quality_last_signature = signature
        point = {
            "ts_ms": now,
            "pdr": float(snapshot["pdr"]),
            "avg_ms": float(snapshot["avg_ms"]),
            "p95_ms": float(snapshot["p95_ms"]),
            "lost": int(snapshot["lost"]),
            "sent": int(snapshot["sent"]),
            "received": int(snapshot["received"]),
        }
        self.quality_points.append(point)
        target = (self.quality_target_var.get() or "all").strip() or "all"
        self.quality_graph_status_var.set(
            (
                f"最新[{target}] sent={point['sent']} recv={point['received']} "
                f"pdr={point['pdr']:.1f}% avg={point['avg_ms']:.1f}ms p95={point['p95_ms']:.1f}ms loss={point['lost']}"
            )
        )

    def _draw_quality_graph(self, *, force: bool = False) -> None:
        canvas = self.quality_graph_canvas
        if canvas is None:
            return
        now_ms = self._now_ms()
        if not force and (now_ms - self._quality_last_draw_ms) < QUALITY_GRAPH_MIN_REDRAW_INTERVAL_MS:
            return
        self._quality_last_draw_ms = now_ms

        canvas.delete("all")
        width = max(280, int(canvas.winfo_width()))
        height = max(180, int(canvas.winfo_height()))
        if width < 40 or height < 40:
            return

        margin_left = 48.0
        margin_right = 34.0
        margin_top = 16.0
        margin_bottom = 16.0
        panel_gap = 16.0
        panel_height = max(50.0, (height - margin_top - margin_bottom - panel_gap) / 2.0)

        x0 = margin_left
        x1 = width - margin_right
        top_y0 = margin_top
        top_y1 = top_y0 + panel_height
        bottom_y0 = top_y1 + panel_gap
        bottom_y1 = height - margin_bottom

        canvas.create_rectangle(x0, top_y0, x1, top_y1, outline="#334155", width=1)
        canvas.create_rectangle(x0, bottom_y0, x1, bottom_y1, outline="#334155", width=1)
        canvas.create_text(x0 + 4, top_y0 - 8, anchor="w", text="PDR (%)", fill="#cbd5e1", font=("Consolas", 9))
        canvas.create_text(x0 + 4, bottom_y0 - 8, anchor="w", text="Latency / Loss", fill="#cbd5e1", font=("Consolas", 9))

        if not self.quality_points:
            canvas.create_text(
                (x0 + x1) / 2.0,
                (top_y0 + bottom_y1) / 2.0,
                text="Ping実行後に品質グラフを表示します",
                fill="#94a3b8",
                font=("Consolas", 11),
            )
            return

        points = list(self.quality_points)
        count = len(points)
        plot_w = max(1.0, x1 - x0)
        top_h = max(1.0, top_y1 - top_y0)
        bottom_h = max(1.0, bottom_y1 - bottom_y0)
        lat_max = max(20.0, max(float(p["p95_ms"]) for p in points) * 1.15)
        loss_max = max(1, max(int(p["lost"]) for p in points))
        ts_min = int(points[0]["ts_ms"])
        ts_max = int(points[-1]["ts_ms"])
        ts_span = max(1, ts_max - ts_min)

        def x_at(idx: int) -> float:
            ts = int(points[idx]["ts_ms"])
            return x0 + (plot_w * float(ts - ts_min) / float(ts_span))

        def pdr_y(value: float) -> float:
            clipped = min(100.0, max(0.0, value))
            return top_y1 - ((clipped / 100.0) * top_h)

        def lat_y(value: float) -> float:
            clipped = min(lat_max, max(0.0, value))
            return bottom_y1 - ((clipped / lat_max) * bottom_h)

        def loss_y(value: int) -> float:
            clipped = min(loss_max, max(0, value))
            return bottom_y1 - ((float(clipped) / float(loss_max)) * bottom_h)

        for ratio, label in ((0.0, "0"), (0.5, "50"), (1.0, "100")):
            y = top_y1 - (top_h * ratio)
            canvas.create_line(x0, y, x1, y, fill="#1e293b", width=1)
            canvas.create_text(x0 - 6, y, anchor="e", text=label, fill="#64748b", font=("Consolas", 8))

        for ratio in (0.0, 0.5, 1.0):
            y = bottom_y1 - (bottom_h * ratio)
            canvas.create_line(x0, y, x1, y, fill="#1e293b", width=1)
            lat_label = f"{lat_max * ratio:.0f}"
            canvas.create_text(x0 - 6, y, anchor="e", text=lat_label, fill="#64748b", font=("Consolas", 8))
            loss_label = f"{int(round(loss_max * ratio))}"
            canvas.create_text(x1 + 6, y, anchor="w", text=loss_label, fill="#64748b", font=("Consolas", 8))

        x_ticks = [0.0, 0.5, 1.0]
        for ratio in x_ticks:
            x = x0 + (plot_w * ratio)
            canvas.create_line(x, bottom_y1, x, bottom_y1 + 4, fill="#475569", width=1)
            tick_ts = ts_min + int(ts_span * ratio)
            tick_label = datetime.fromtimestamp(tick_ts / 1000.0).strftime("%H:%M:%S")
            canvas.create_text(x, bottom_y1 + 10, anchor="n", text=tick_label, fill="#64748b", font=("Consolas", 8))

        pdr_coords: list[float] = []
        avg_coords: list[float] = []
        p95_coords: list[float] = []
        for idx, point in enumerate(points):
            px = x_at(idx)
            pdr_coords.extend([px, pdr_y(float(point["pdr"]))])
            avg_coords.extend([px, lat_y(float(point["avg_ms"]))])
            p95_coords.extend([px, lat_y(float(point["p95_ms"]))])
            ly = loss_y(int(point["lost"]))
            canvas.create_line(px, bottom_y1, px, ly, fill="#ef4444", width=1)

        if len(pdr_coords) >= 4:
            canvas.create_line(*pdr_coords, fill="#22c55e", width=2, smooth=True)
            canvas.create_line(*avg_coords, fill="#38bdf8", width=2, smooth=True)
            canvas.create_line(*p95_coords, fill="#f59e0b", width=2, smooth=True)
        else:
            canvas.create_oval(pdr_coords[0] - 2, pdr_coords[1] - 2, pdr_coords[0] + 2, pdr_coords[1] + 2, fill="#22c55e", outline="")
            canvas.create_oval(avg_coords[0] - 2, avg_coords[1] - 2, avg_coords[0] + 2, avg_coords[1] + 2, fill="#38bdf8", outline="")
            canvas.create_oval(p95_coords[0] - 2, p95_coords[1] - 2, p95_coords[0] + 2, p95_coords[1] + 2, fill="#f59e0b", outline="")

        latest = points[-1]
        canvas.create_text(
            x1 - 6,
            top_y0 + 8,
            anchor="ne",
            text=f"now pdr={latest['pdr']:.1f}%",
            fill="#86efac",
            font=("Consolas", 9, "bold"),
        )
        canvas.create_text(
            x1 - 6,
            bottom_y0 + 8,
            anchor="ne",
            text=f"avg={latest['avg_ms']:.1f}ms p95={latest['p95_ms']:.1f}ms loss={latest['lost']}",
            fill="#bfdbfe",
            font=("Consolas", 9),
        )

    def reset_stats(self) -> None:
        self.ping_stats.reset()
        self.reliable_stats.reset()
        self.pending_ping_rounds.clear()
        self.reliable_tx_sessions.clear()
        self.reliable_rx_sessions.clear()
        self.reliable_auto_state_by_dst.clear()
        self.quality_points.clear()
        self._quality_last_signature = None
        self.quality_graph_status_var.set("品質グラフ: リセット済み")
        self.update_stats_view()
        self._draw_quality_graph(force=True)
        self.append_log("統計情報をリセットしました。", level="SYSTEM", category="PING")

    def update_stats_view(self) -> None:
        target = (self.quality_target_var.get() or "all").strip() or "all"
        if target != self._quality_target_active:
            self._quality_target_active = target
            self.quality_points.clear()
            self._quality_last_signature = None
        snapshot = self.ping_stats.snapshot(target=target)
        self.sent_var.set(str(snapshot["sent"]))
        self.received_var.set(str(snapshot["received"]))
        self.lost_var.set(str(snapshot["lost"]))
        self.pdr_var.set(f"{snapshot['pdr']:.1f}%")
        self.avg_var.set(f"{snapshot['avg_ms']:.1f} ms")
        self.min_var.set(f"{snapshot['min_ms']:.1f} ms")
        self.max_var.set(f"{snapshot['max_ms']:.1f} ms")
        self.p95_var.set(f"{snapshot['p95_ms']:.1f} ms")
        reliable_snapshot = self.reliable_stats.snapshot()
        self.reliable_restore_var.set(f"{float(reliable_snapshot['restore_rate']):.1f}%")
        self.reliable_retry_rate_var.set(f"{float(reliable_snapshot['retry_rate']):.1f}%")
        self.reliable_profile_used_var.set(str(reliable_snapshot["top_profile"]))
        self.reliable_fail_var.set(str(reliable_snapshot["top_reason"]))
        self._record_quality_point(snapshot)
        self._draw_quality_graph()

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
            target = Path(path)
            target.write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            base = target.with_suffix("")
            jsonl_path = base.with_suffix(".jsonl")
            csv_path = base.with_suffix(".csv")
            with jsonl_path.open("w", encoding="utf-8") as jf:
                for record in self.event_records:
                    jf.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            with csv_path.open("w", encoding="utf-8", newline="") as cf:
                writer = csv.DictWriter(cf, fieldnames=["ts_iso", "ts_ms", "level", "category", "message"])
                writer.writeheader()
                for record in self.event_records:
                    writer.writerow(
                        {
                            "ts_iso": record.get("ts_iso", ""),
                            "ts_ms": record.get("ts_ms", 0),
                            "level": record.get("level", ""),
                            "category": record.get("category", ""),
                            "message": record.get("message", ""),
                        }
                    )
            self.append_log(
                f"ログ保存完了: text={target.name} jsonl={jsonl_path.name} csv={csv_path.name}",
                level="SYSTEM",
                category="LOG",
            )
        except OSError as exc:
            messagebox.showerror("保存失敗", str(exc))

    def clear_logs(self) -> None:
        self.log_lines.clear()
        self.event_records.clear()
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
