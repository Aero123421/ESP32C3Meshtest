"""Deterministic measurement model: local ACKs never count as wireless delivery."""
from __future__ import annotations

import math
import re
import time
from collections import deque
from typing import Any

NODE = re.compile(r'^0x[0-9a-fA-F]{8}$')


def node_id(value: Any) -> str:
    if not isinstance(value, str) or not NODE.fullmatch(value) or int(value[2:], 16) == 0:
        raise ValueError('Destination must be a nonzero 0xXXXXXXXX node ID')
    return '0x' + value[2:].upper()


def integer(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name}: integer {low}..{high} required')
    return value


class ProbeRun:
    def __init__(self, destination: str, count: int, interval_ms: int,
                 timeout_ms: int, size: int, ttl: int, now: float) -> None:
        self.destination = node_id(destination)
        self.count = integer(count, 1, 10000, 'count')
        self.interval = integer(interval_ms, 100, 60000, 'interval_ms') / 1000
        self.timeout = integer(timeout_ms, 500, 60000, 'timeout_ms') / 1000
        self.size = integer(size, 1, 1000, 'size')
        self.ttl = integer(ttl, 1, 14, 'ttl')
        self.started_at = time.time()
        self.next_at = now
        self.status = 'running'
        self.sent = 0
        self.pending: dict[str, float] = {}
        self.finished: set[str] = set()
        self.samples: list[dict] = []
        self.late = 0
        self.cancelled = 0
        self.context: dict = {}

    def due(self, now: float) -> bool:
        # One outstanding probe avoids making the test itself a flood generator.
        return self.status == 'running' and self.sent < self.count and not self.pending and now >= self.next_at

    def submitted(self, tag: str, now: float) -> None:
        self.pending[tag] = now
        self.sent += 1
        self.next_at = now + self.interval

    def receive(self, event: dict, now: float) -> bool:
        if event.get('type', event.get('event')) != 'pong':
            return False
        try:
            src = node_id(event.get('src'))
        except ValueError:
            return False
        if src != self.destination:
            return False
        tag = str(event.get('ping_id', '')).lower()
        if tag not in self.pending:
            if tag in self.finished:
                self.late += 1
            return False
        start = self.pending.pop(tag)
        ok = (now - start <= self.timeout and event.get('probe_hash_ok') is not False
              and event.get('status', 'ok') not in ('bad', 'error', 'corrupt'))
        self.finished.add(tag)
        rssi = event.get('rssi')
        self.samples.append({'seq': len(self.samples) + 1, 'ok': ok,
                             'rtt_ms': round((now - start) * 1000, 2) if ok else None,
                             'request_hops': event.get('request_hops'),
                             'reply_hops': event.get('reply_hops', event.get('hops')),
                             'rssi': rssi if isinstance(rssi, (int, float)) and -128 <= rssi < 0 else None})
        self.finish_if_done()
        return True

    def tick(self, now: float) -> None:
        for tag, start in list(self.pending.items()):
            if now - start >= self.timeout:
                del self.pending[tag]
                self.finished.add(tag)
                self.samples.append({'seq': len(self.samples) + 1, 'ok': False, 'rtt_ms': None})
        self.finish_if_done()

    def finish_if_done(self) -> None:
        if self.status == 'running' and self.sent >= self.count and not self.pending:
            self.status = 'complete'

    def cancel(self) -> None:
        self.cancelled += len(self.pending)
        self.finished.update(self.pending)
        self.pending.clear()
        self.status = 'stopped'

    def snapshot(self) -> dict:
        successes = [x['rtt_ms'] for x in self.samples if x['ok']]
        n = len(self.samples)
        p95 = sorted(successes)[math.ceil(0.95 * len(successes)) - 1] if successes else None
        return {'status': self.status, 'destination': self.destination, 'target': self.count,
                'sent': self.sent, 'pending': len(self.pending), 'finalized': n,
                'received': len(successes), 'lost': n - len(successes),
                'pdr': round(100 * len(successes) / n, 2) if n else None,
                'p95_ms': p95, 'duplicates_or_late': self.late, 'cancelled': self.cancelled,
                'size': self.size, 'ttl': self.ttl, 'interval_ms': int(self.interval * 1000),
                'timeout_ms': int(self.timeout * 1000), 'context': dict(self.context), 'samples': self.samples[-400:]}


class Network:
    def __init__(self) -> None:
        self.local = None
        self.nodes: dict[str, dict] = {}
        self.routes: list[dict] = []
        self.edges: dict[tuple, dict] = {}
        self.radio: dict = {}
        self.stats: dict = {}
        self.routes_at = 0.0
        self.route_truncated = False

    def touch(self, value: Any, now: float, **fields: Any) -> str | None:
        try:
            ident = node_id(value)
        except ValueError:
            return None
        if ident not in self.nodes and len(self.nodes) >= 128:
            oldest = min(self.nodes, key=lambda k: self.nodes[k]['seen'])
            del self.nodes[oldest]
        node = self.nodes.setdefault(ident, {'id': ident})
        node.update(fields)
        node['seen'] = now
        return ident

    def receive(self, e: dict, now: float) -> None:
        kind = e.get('type', e.get('event'))
        if kind in ('boot', 'radio_profile'):
            ident = self.touch(e.get('node_id'), now, role='gateway')
            if ident:
                self.local = ident
            if kind == 'radio_profile':
                self.radio = dict(e)
                if ident:
                    self.nodes[ident]['chip'] = e.get('chip')
        elif kind == 'node_list':
            for n in e.get('nodes', [])[:128]:
                if not isinstance(n, dict):
                    continue
                age = 0 if n.get('is_self') or n.get('node_id') == self.local else n.get('age_ms', 0)
                age = age if isinstance(age, (float, int)) and age >= 0 else 0
                rssi = n.get('rssi')
                rssi = rssi if isinstance(rssi, (float, int)) and -128 <= rssi < 0 else None
                ident = self.touch(n.get('node_id'), now - age / 1000,
                                   rssi=rssi, mac=n.get('mac'), heap=n.get('free_heap'))
                if ident and n.get('is_self'):
                    self.local = ident
        elif kind == 'route_list':
            self.routes = [dict(r) for r in e.get('routes', [])[:192] if isinstance(r, dict)]
            self.routes_at = now
            self.route_truncated = bool(e.get('truncated'))
        elif kind == 'stats':
            self.stats = dict(e.get('mesh', {}))
        if kind in ('mesh_observed', 'mesh_trace', 'pong', 'delivery_ack', 'chat'):
            observer = e.get('observer') or self.local
            via = e.get('via_node')
            # Only the explicitly observed last hop is a physical edge.
            # An end-to-end origin and destination do NOT imply direct reception.
            a = self.touch(via, now)
            z = self.touch(observer, now)
            self.touch(e.get('src'), now)
            if a and z and a != z:
                rssi = e.get('rssi')
                self.edges[(a, z)] = {'from': a, 'to': z, 'kind': 'observed', 'seen': now,
                                      'rssi': rssi if isinstance(rssi, (float, int)) and -128 <= rssi < 0 else None}
        self.edges = {k: v for k, v in self.edges.items() if now - v['seen'] < 120}

    def snapshot(self, now: float) -> dict:
        nodes = [{k: v for k, v in n.items() if k != 'seen'} |
                 {'age_s': max(0, round(now - n['seen'], 1)), 'online': now - n['seen'] < 60}
                 for n in self.nodes.values()]
        edges = [{k: v for k, v in e.items() if k != 'seen'} for e in self.edges.values()]
        routes = []
        for r in self.routes:
            age = r.get('age_ms', 0)
            if not isinstance(age, (float, int)) or age + (now - self.routes_at) * 1000 > 45000:
                continue
            routes.append(r)
            hop = r.get('next_hop_node_id')
            dst = r.get('dst_node_id')
            if self.local and hop and hop != self.local:
                edges.append({'from': self.local, 'to': hop,
                              'kind': 'backup' if r.get('rank') == 1 else 'route', 'destination': dst})
            # The rest of a multi-hop route stays UNKNOWN. Never invent hop->dst.
        return {'local': self.local, 'nodes': nodes, 'edges': edges, 'routes': routes,
                'route_truncated': self.route_truncated, 'radio': self.radio, 'stats': self.stats}
