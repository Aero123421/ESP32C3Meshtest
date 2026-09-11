"""Serial ownership, repeatable probe sessions and stop-and-wait delivery."""
from __future__ import annotations

import base64
import hashlib
import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .model import Network, ProbeRun, integer, node_id
from .transport import SerialLink, serial_ports

ENVS = tuple('seeed_xiao_esp32' + chip + suffix for chip in ('c3', 's3')
             for suffix in ('', '_lr', '_coexist'))
ROOT = Path(__file__).resolve().parents[2]


def text_packets(text: str, dst: str, ttl: int) -> list[dict]:
    """Keep the original wire protocol, including UTF-8 byte-based chunking."""
    dst = node_id(dst)
    integer(ttl, 1, 14, 'ttl')
    if not isinstance(text, str) or not text or len(text.encode('utf-8')) > 8192:
        raise ValueError('Message must contain 1..8192 UTF-8 bytes')
    raw = text.encode('utf-8')
    ident = uuid.uuid4().hex[:12]
    common = {'src': 'pc', 'via': 'wifi', 'dst': dst, 'ttl': ttl, 'need_ack': True}
    if len(raw) <= 400:
        return [common | {'type': 'chat', 'text': text, 'e2e_id': ident}]
    chunks = [raw[i:i + 32] for i in range(0, len(raw), 32)]
    meta = {'text_id': ident, 'encoding': 'utf-8', 'size': len(raw), 'chunks': len(chunks)}
    packets = [common | meta | {'type': 'long_text_start', 'e2e_id': ident + ':s'}]
    packets += [common | {'type': 'long_text_chunk', 'text_id': ident, 'index': i,
                          'data_b64': base64.b64encode(chunk).decode(), 'e2e_id': f'{ident}:c:{i}'}
                for i, chunk in enumerate(chunks)]
    packets.append(common | meta | {'type': 'long_text_end', 'sha256': hashlib.sha256(raw).hexdigest(),
                                    'e2e_id': ident + ':e'})
    return packets


class Controller:
    def __init__(self, demo: bool = False, clock=time.monotonic) -> None:
        self.demo, self.clock = demo, clock
        self.demo_time = clock()
        self.lock = threading.RLock()
        self.events: queue.Queue = queue.Queue(maxsize=2048)
        self.stop_event = threading.Event()
        self.link = None
        self.generation = ''
        self.connection = 'demo' if demo else 'disconnected'
        self.port = ''
        self.network = Network()
        self.run: ProbeRun | None = None
        self.runs = deque(maxlen=20)
        self.logs = deque(maxlen=2000)
        self.log_count = 0
        self.messages = deque(maxlen=200)
        self.transfer = None
        self.assemblies = {}
        self.chat_seen = deque(maxlen=512)
        self.job = {'status': 'idle', 'log': []}
        self.job_process = None
        self.metadata = {'site': '', 'distance_m': None, 'antenna': '', 'height_m': None, 'notes': ''}
        self.next_poll = 0.0
        self.poll_index = 0
        self.probe_tag = int(uuid.uuid4().hex[:8], 16)
        self.thread = threading.Thread(target=self._loop, daemon=True, name='mesh-controller')
        if demo:
            self.seed_demo()

    def start(self) -> None:
        self.thread.start()

    def log(self, kind: str, data) -> None:
        self.log_count += 1
        self.logs.append({'id': self.log_count, 'time': time.time(), 'kind': kind, 'data': data})

    def close(self) -> None:
        self.stop_event.set()
        if self.link:
            self.link.close()
        if self.job_process and self.job_process.poll() is None:
            self.job_process.terminate()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def disconnect(self) -> None:
        if self.run and self.run.status == 'running':
            self.run.cancel()
        if self.transfer and self.transfer['status'] == 'sending':
            self.transfer['status'] = 'cancelled'
        if self.link:
            self.link.close()
            self.link = None
        self.generation = uuid.uuid4().hex
        self.connection = 'disconnected'
        self.log('connection', 'Disconnected; pending transmissions cancelled')

    def send(self, payload: dict) -> bool:
        return self.connection == 'connected' and self.link is not None and self.link.send(payload)

    def receive(self, event: dict, now: float) -> None:
        self.network.receive(event, now)
        if self.run:
            self.run.receive(event, now)
        kind = event.get('type', event.get('event'))
        if kind == 'delivery_ack' and self.transfer and self.transfer['status'] == 'sending':
            t = self.transfer
            packet = t['packets'][t['index']]
            try:
                same_src = node_id(event.get('src')) == packet['dst']
            except ValueError:
                same_src = False
            if (same_src and event.get('e2e_id') == packet['e2e_id'] and event.get('status') == 'ok'
                    and t['waiting']):
                t['index'] += 1
                t['waiting'] = False
                t['retry'] = 0
                t['next_at'] = now + 0.08
                if t['index'] == len(t['packets']):
                    t['status'] = 'delivered'
                    self.log('delivery', {'id': t['id'], 'status': 'remote firmware ACK received'})
        if kind == 'chat' and isinstance(event.get('text'), str):
            key = (event.get('src'), event.get('e2e_id') or event.get('msg_id'))
            if key not in self.chat_seen:
                self.chat_seen.append(key)
                self.messages.append({'direction': 'rx', 'peer': event.get('src'),
                                      'text': event['text'], 'time': time.time(), 'status': 'received'})
        if kind in ('long_text_start', 'long_text_chunk', 'long_text_end'):
            self.receive_long_text(event, now)

    def receive_long_text(self, e: dict, now: float) -> None:
        ident = (e.get('src'), e.get('text_id'))
        if not all(isinstance(x, str) and 0 < len(x) <= 64 for x in ident):
            return
        kind = e['type']
        if kind == 'long_text_start':
            size, count = e.get('size'), e.get('chunks')
            if not isinstance(size, int) or not 0 <= size <= 8192 or not isinstance(count, int) or not 0 <= count <= 256:
                return
            if ident not in self.assemblies and len(self.assemblies) >= 8:
                return
            # Repeated start after an ACK loss must not erase received chunks.
            a = self.assemblies.setdefault(ident, {'chunks': {}, 'size': size, 'count': count})
            a['at'] = now
        elif ident in self.assemblies:
            a = self.assemblies[ident]
            a['at'] = now
            if kind == 'long_text_chunk':
                index = e.get('index')
                if not isinstance(index, int) or not 0 <= index < a['count']:
                    return
                try:
                    chunk = base64.b64decode(e.get('data_b64', ''), validate=True)
                except (ValueError, TypeError):
                    return
                if len(chunk) <= 32:
                    a['chunks'][index] = chunk
            elif len(a['chunks']) == a['count']:
                raw = b''.join(a['chunks'][i] for i in range(a['count']))
                if len(raw) == a['size'] and hashlib.sha256(raw).hexdigest() == e.get('sha256'):
                    try:
                        text = raw.decode('utf-8')
                    except UnicodeError:
                        return
                    self.messages.append({'direction': 'rx', 'peer': ident[0], 'text': text,
                                          'time': time.time(), 'status': 'SHA-256 verified'})
                else:
                    self.log('error', 'Long text integrity check failed')
                del self.assemblies[ident]

    def command(self, data: dict) -> dict:
        with self.lock:
            action = data.get('action')
            if action == 'metadata':
                for key in ('site', 'antenna', 'notes'):
                    val = data.get(key, '')
                    if not isinstance(val, str) or len(val) > 500:
                        raise ValueError(f'Invalid {key}')
                    self.metadata[key] = val
                for key in ('distance_m', 'height_m'):
                    val = data.get(key)
                    if val is not None and (isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 <= val <= 100000):
                        raise ValueError(f'Invalid {key}')
                    self.metadata[key] = val
                return {'ok': True}
            if self.demo:
                raise ValueError('Demo is read-only. Restart without --demo to connect real hardware.')
            if action == 'connect':
                if self.job['status'] == 'running':
                    raise ValueError('Wait for the firmware operation to finish before connecting')
                port = data.get('port')
                if port not in {p['device'] for p in serial_ports()}:
                    raise ValueError('Select a currently enumerated serial port')
                self.disconnect()
                self.network = Network()
                self.port, self.connection = port, 'connecting'
                self.link = SerialLink(port, self.events, self.generation)
                self.link.start()
            elif action == 'disconnect':
                self.disconnect()
            elif action == 'stop':
                if self.run and self.run.status == 'running':
                    self.run.cancel()
                if self.transfer and self.transfer['status'] == 'sending':
                    self.transfer['status'] = 'cancelled'
            elif action in ('test', 'message'):
                if self.connection != 'connected':
                    raise ValueError('Connect a board first')
                if self.network.radio.get('ready') is False:
                    raise ValueError('The connected firmware reports that its radio is not ready')
                if self.run and self.run.status == 'running' or self.transfer and self.transfer['status'] == 'sending':
                    raise ValueError('Stop the current test or transfer first')
                dst = node_id(data.get('destination'))
                if dst == self.network.local:
                    raise ValueError('Select a remote node, not the local USB gateway')
                if action == 'test':
                    if self.run:
                        self.runs.append(self.run.snapshot() | {'samples': list(self.run.samples)})
                    self.run = ProbeRun(dst, data.get('count', 100), data.get('interval_ms', 1000),
                                        data.get('timeout_ms', 4000), data.get('size', 64), data.get('ttl', 6), self.clock())
                    self.run.context = {'metadata': dict(self.metadata), 'radio': dict(self.network.radio),
                                        'port': self.port, 'firmware_sha256': self.job.get('sha256')}
                    self.log('test_start', self.run.snapshot())
                else:
                    packets = text_packets(data.get('text'), dst, data.get('ttl', 6))
                    self.transfer = {'id': uuid.uuid4().hex[:8], 'packets': packets, 'index': 0,
                                     'waiting': False, 'retry': 0, 'next_at': 0, 'status': 'sending',
                                     'text': data['text'], 'peer': dst, 'time': time.time()}
            elif action == 'firmware':
                self.firmware(data)
            elif action == 'refresh':
                self.next_poll = 0
            else:
                raise ValueError('Unknown action')
            return {'ok': True}

    def firmware(self, data: dict) -> None:
        env, operation = data.get('environment'), data.get('operation')
        if env not in ENVS or operation not in ('build', 'upload'):
            raise ValueError('Invalid firmware target or operation')
        if self.job['status'] == 'running':
            raise ValueError('A firmware operation is already running')
        if env.endswith('_lr') and data.get('lr_authorized') is not True:
            raise ValueError('LR requires explicit confirmation of board/antenna/mode authorization')
        args = [sys.executable, '-m', 'platformio', 'run', '-e', env]
        if operation == 'upload':
            port = data.get('port')
            if port not in {p['device'] for p in serial_ports()}:
                raise ValueError('Select a currently enumerated upload port')
            self.disconnect()
            args += ['-t', 'upload', '--upload-port', port]
        self.job = {'status': 'running', 'environment': env, 'operation': operation, 'log': [], 'exit_code': None}
        def execute() -> None:
            process = None
            try:
                process = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8', errors='replace', shell=False)
                with self.lock:
                    self.job_process = process
                for line in process.stdout:
                    with self.lock:
                        self.job['log'] = (self.job['log'] + [line.rstrip()])[-600:]
                code = process.wait()
                with self.lock:
                    self.job['exit_code'] = code
                    self.job['status'] = 'success' if code == 0 else 'failed'
                    binary = ROOT / '.pio' / 'build' / env / 'firmware.bin'
                    if code == 0 and binary.is_file():
                        self.job['sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
            except OSError as exc:
                with self.lock:
                    self.job.update(status='failed', log=[str(exc)])
            finally:
                with self.lock:
                    self.job_process = None
        threading.Thread(target=execute, daemon=True, name='mesh-build').start()

    def tick(self, now: float) -> None:
        if self.run:
            self.run.tick(now)
            if self.connection == 'connected' and self.run.due(now):
                self.probe_tag = (self.probe_tag + 1) & 0xFFFFFFFF
                tag = f'{self.probe_tag:08x}'
                p = {'cmd': 'ping_probe', 'type': 'ping', 'via': 'wifi', 'src': 'pc',
                     'dst': self.run.destination, 'seq': self.run.sent + 1, 'ping_id': tag,
                     'probe_bytes': self.run.size, 'ttl': self.run.ttl, 'ts_ms': int(time.time() * 1000)}
                if self.send(p):
                    self.run.submitted(tag, now)
        t = self.transfer
        if t and t['status'] == 'sending':
            if t['waiting'] and now >= t['deadline']:
                t['waiting'] = False
                t['retry'] += 1
                t['next_at'] = now + 0.2 * t['retry']
                if t['retry'] > 4:
                    t['status'] = 'failed'
                    self.log('error', 'Remote delivery ACK timed out; transfer stopped')
            if t['status'] == 'sending' and not t['waiting'] and now >= t['next_at']:
                packet = t['packets'][t['index']] | {'retry_no': t['retry'], 'ts_ms': int(time.time() * 1000)}
                if self.send(packet):
                    t['waiting'], t['deadline'] = True, now + 5 + t['retry']
        if self.connection == 'connected' and now >= self.next_poll:
            commands = ('get_radio_profile', 'get_nodes', 'get_routes', 'get_stats')
            if self.send({'cmd': commands[self.poll_index]}):
                self.poll_index = (self.poll_index + 1) % len(commands)
                self.next_poll = now + (10 if self.poll_index == 0 else 0.3)
        self.assemblies = {k: a for k, a in self.assemblies.items() if now - a['at'] < 120}

    def _loop(self) -> None:
        while not self.stop_event.wait(0.025):
            with self.lock:
                for _ in range(100):
                    try:
                        e = self.events.get_nowait()
                    except queue.Empty:
                        break
                    if e.get('generation') != self.generation:
                        continue
                    kind = e.get('kind')
                    self.log(kind, e.get('payload', e.get('detail', e.get('text', e))))
                    if kind == 'connected':
                        self.connection, self.next_poll, self.poll_index = 'connected', 0, 0
                    elif kind == 'disconnected':
                        self.connection = 'disconnected'
                        if self.run and self.run.status == 'running':
                            self.run.cancel()
                        if self.transfer and self.transfer['status'] == 'sending':
                            self.transfer['status'] = 'cancelled'
                    elif kind == 'rx':
                        try:
                            self.receive(e['payload'], self.clock())
                        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                            self.log('error', f'Invalid device event ignored: {exc}')
                self.tick(self.clock())

    def snapshot(self) -> dict:
        with self.lock:
            t = self.transfer
            return {'demo': self.demo, 'connection': self.connection, 'port': self.port,
                    'network': self.network.snapshot(self.demo_time if self.demo else self.clock()),
                    'test': self.run.snapshot() if self.run else None,
                    'transfer': {k: v for k, v in t.items() if k not in ('packets', 'waiting', 'deadline', 'next_at')}
                                | {'total': len(t['packets'])} if t else None,
                    'messages': list(self.messages), 'logs': list(self.logs)[-160:], 'log_count': self.log_count,
                    'metadata': dict(self.metadata), 'job': dict(self.job), 'environments': ENVS}

    def export(self) -> dict:
        with self.lock:
            return self.snapshot() | {'format': 'mesh-lab-session-v1', 'exported_at': time.time(),
                     'logs': list(self.logs), 'logs_truncated': self.log_count > len(self.logs),
                     'test_samples': list(self.run.samples) if self.run else [], 'previous_tests': list(self.runs),
                     'notice': 'Demo data is synthetic' if self.demo else 'RF range and certification are not inferred from software tests'}

    def seed_demo(self) -> None:
        now = self.clock()
        ids = [f'0x{v:08X}' for v in (0xA001, 0xA002, 0xA003, 0xA004, 0xA005, 0xA006)]
        self.network.receive({'type': 'radio_profile', 'node_id': ids[0], 'ready': True, 'chip': 'ESP32-S3',
            'profile': 'long_range', 'channel': 1, 'bandwidth_mhz': 20, 'espnow_rate_kbps': 250,
            'tx_power_readback_qdbm': 72, 'readback_ok': True, 'power_save': False,
            'rssi_source': 'synthetic_demo'}, now)
        for i, ident in enumerate(ids):
            self.network.touch(ident, now, chip='ESP32-C3' if i % 2 else 'ESP32-S3',
                               role='gateway' if i == 0 else 'relay', rssi=None)
        for a, b in ((1, 0), (2, 0), (3, 1), (4, 1), (5, 2)):
            self.network.receive({'type': 'mesh_trace', 'observer': ids[b], 'via_node': ids[a], 'src': ids[a], 'rssi': -58 - a * 4}, now)
        self.network.receive({'type': 'route_list', 'routes': [
            {'dst_node_id': ids[i], 'next_hop_node_id': ids[1 if i in (1, 3, 4) else 2],
             'hops': 1 if i in (1, 2) else 2, 'rank': 0, 'metric_q8': 256 + i * 80, 'age_ms': 240}
            for i in range(1, 6)]}, now)
        self.log('demo', 'Synthetic topology. No radio is connected; metrics are not measurements.')
