from __future__ import annotations

import base64
import hashlib
import http.client
import json
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc_app'))
from mesh_lab.controller import Controller, text_packets
from mesh_lab.model import Network, ProbeRun, integer, node_id
from mesh_lab.server import Server, authorized
from mesh_lab.transport import LineBuffer

A, B, C = '0x00000001', '0x00000002', '0x00000003'


def probe(**overrides):
    return ProbeRun(**({'destination': B, 'count': 2, 'interval_ms': 1000, 'timeout_ms': 2000,
                        'size': 64, 'ttl': 6, 'now': 0} | overrides))


@pytest.mark.parametrize('value', ['', 'pc', '*', '0x00000000', '0x1234', 1234, None, '0x0000000G'])
def test_invalid_node_id(value):
    with pytest.raises(ValueError):
        node_id(value)


def test_normalized_node():
    assert node_id('0xabcdef01') == '0xABCDEF01'


@pytest.mark.parametrize('value', [True, 1.5, '3', None, -1, 100001])
def test_bounded_integer(value):
    with pytest.raises(ValueError):
        integer(value, 1, 10000, 'test')


@pytest.mark.parametrize('split', range(1, 16))
def test_fragmented_utf8_json_lines(split):
    raw = ('{"text":"通信テスト🙂"}\r\n{"type":"pong"}\n').encode('utf-8')
    buffer = LineBuffer()
    lines = []
    for offset in range(0, len(raw), split):
        lines += buffer.feed(raw[offset:offset + split])
    assert [json.loads(x) for x in lines] == [{'text': '通信テスト🙂'}, {'type': 'pong'}]
    assert not buffer.buffer


def test_oversized_line_discards_until_lf_and_recovers():
    buffer = LineBuffer(limit=16)
    assert buffer.feed(b'x' * 17) == []
    assert buffer.feed(b'payload-tail') == []
    assert buffer.feed(b'\n{"ok":1}\n') == [b'{"ok":1}']
    assert buffer.dropped == 1


def test_no_newline_no_packet():
    b = LineBuffer()
    assert b.feed(b'{"type":"pong"}') == []
    assert b.feed(b'\n') == [b'{"type":"pong"}']


def test_local_ack_not_radio_success():
    run = probe()
    run.submitted('12345678', 0)
    assert not run.receive({'type': 'ack', 'ok': True}, 0.01)
    assert run.snapshot()['pdr'] is None
    run.tick(2)
    assert run.snapshot()['pdr'] == 0


def test_pong_must_match_destination_and_transaction():
    run = probe()
    run.submitted('12345678', 0)
    assert not run.receive({'type': 'pong', 'src': C, 'ping_id': '12345678'}, .1)
    assert not run.receive({'type': 'pong', 'src': B, 'ping_id': '87654321'}, .1)
    assert run.receive({'type': 'pong', 'src': B, 'ping_id': '12345678'}, .12)
    assert run.snapshot()['p95_ms'] == 120
    assert not run.receive({'type': 'pong', 'src': B, 'ping_id': '12345678'}, .14)
    assert run.snapshot()['received'] == 1
    assert run.snapshot()['duplicates_or_late'] == 1


def test_late_pong_cannot_improve_pdr():
    run = probe(count=1)
    run.submitted('12345678', 0)
    run.tick(2.01)
    assert not run.receive({'type': 'pong', 'src': B, 'ping_id': '12345678'}, 3)
    assert run.snapshot()['pdr'] == 0
    assert run.snapshot()['status'] == 'complete'


def test_corrupt_probe_is_failure():
    run = probe(count=1)
    run.submitted('12345678', 0)
    run.receive({'type': 'pong', 'src': B, 'ping_id': '12345678', 'probe_hash_ok': False}, .1)
    assert run.snapshot()['pdr'] == 0


def test_stop_excludes_pending_from_loss_denominator():
    run = probe()
    run.submitted('a', 0)
    run.receive({'type': 'pong', 'src': B, 'ping_id': 'a'}, .1)
    run.submitted('b', 1)
    run.cancel()
    assert run.snapshot()['pdr'] == 100
    assert run.snapshot()['cancelled'] == 1
    assert run.snapshot()['lost'] == 0


def test_stop_and_wait_probe_pacing():
    run = probe()
    assert run.due(0)
    run.submitted('a', 0)
    assert not run.due(1.5)
    run.tick(2)
    assert run.due(2)


def test_routes_do_not_invent_unknown_physical_hops():
    n = Network()
    n.receive({'type': 'radio_profile', 'node_id': A}, 0)
    n.receive({'type': 'route_list', 'routes': [
        {'dst_node_id': C, 'next_hop_node_id': B, 'hops': 4, 'rank': 0, 'age_ms': 0}]}, 0)
    edges = n.snapshot(1)['edges']
    assert len(edges) == 1
    assert edges[0]['from'] == A and edges[0]['to'] == B
    assert not any(e['from'] == B and e['to'] == C for e in edges)
    assert n.snapshot(46)['routes'] == []


def test_observed_last_hop_is_not_end_to_end_source():
    n = Network()
    n.receive({'type': 'mesh_observed', 'observer': A, 'via_node': B, 'src': C, 'rssi': 0}, 0)
    edge = n.snapshot(1)['edges'][0]
    assert (edge['from'], edge['to']) == (B, A)
    assert edge['rssi'] is None


def test_node_age_readback_does_not_refresh_stale_nodes():
    n = Network()
    n.receive({'type': 'node_list', 'nodes': [{'node_id': B, 'age_ms': 90000, 'rssi': 0}]}, 100)
    assert not n.snapshot(100)['nodes'][0]['online']
    assert n.snapshot(100)['nodes'][0]['rssi'] is None


def test_demo_static_and_read_only():
    now = [0.0]
    c = Controller(demo=True, clock=lambda: now[0])
    now[0] = 1000
    assert len(c.snapshot()['network']['routes']) == 5
    assert all(n['online'] for n in c.snapshot()['network']['nodes'])
    with pytest.raises(ValueError, match='read-only'):
        c.command({'action': 'connect', 'port': 'COM1'})


def test_text_split_is_bytes_not_characters():
    text = '日本語🙂' * 60
    packets = text_packets(text, B, 6)
    chunks = [base64.b64decode(p['data_b64']) for p in packets[1:-1]]
    assert all(len(chunk) <= 32 for chunk in chunks)
    assert b''.join(chunks).decode() == text
    assert packets[-1]['sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert len({p['e2e_id'] for p in packets}) == len(packets)


@pytest.mark.parametrize('text', ['', 'a' * 8193, None])
def test_text_limits(text):
    with pytest.raises(ValueError):
        text_packets(text, B, 6)


def test_long_text_receiver_duplicate_start_and_integrity():
    c = Controller()
    text = '試験🙂' * 80
    packets = text_packets(text, B, 6)
    c.receive_long_text(packets[0] | {'src': A}, 0)
    c.receive_long_text(packets[1] | {'src': A}, .1)
    c.receive_long_text(packets[0] | {'src': A}, .2)
    for p in packets[2:]:
        c.receive_long_text(p | {'src': A}, 1)
    assert c.messages[-1]['text'] == text
    assert c.messages[-1]['status'] == 'SHA-256 verified'


class FakeLink:
    def __init__(self):
        self.sent = []
    def send(self, p):
        self.sent.append(p)
        return True
    def close(self):
        pass


def test_message_waits_for_matching_remote_ack_and_retries():
    c = Controller(clock=lambda: 0)
    c.link = FakeLink()
    c.connection = 'connected'
    c.network.local = A
    c.command({'action': 'message', 'destination': B, 'text': 'hello'})
    c.tick(0)
    p = c.transfer['packets'][0]
    c.receive({'type': 'ack', 'ok': True}, .1)
    assert c.transfer['status'] == 'sending'
    c.receive({'type': 'delivery_ack', 'src': C, 'e2e_id': p['e2e_id'], 'status': 'ok'}, .2)
    assert c.transfer['status'] == 'sending'
    c.tick(5.1)
    c.tick(5.4)
    assert c.transfer['retry'] == 1
    c.receive({'type': 'delivery_ack', 'src': B, 'e2e_id': p['e2e_id'], 'status': 'ok'}, 5.5)
    assert c.transfer['status'] == 'delivered'


def test_disconnected_transport_cancels_not_silently_resumes():
    c = Controller()
    c.link = FakeLink()
    c.connection = 'connected'
    c.command({'action': 'test', 'destination': B})
    c.run.submitted('a', 0)
    c.disconnect()
    assert c.run.status == 'stopped'
    assert c.run.snapshot()['cancelled'] == 1
    assert c.connection == 'disconnected'


def test_reject_local_gateway_as_rf_destination():
    c = Controller()
    c.connection = 'connected'
    c.network.local = A
    with pytest.raises(ValueError, match='remote node'):
        c.command({'action': 'test', 'destination': A})


@pytest.mark.parametrize('change', [
    {'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
    {'X-Mesh-Token': 'wrong'}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}])
def test_reject_cross_origin_and_bad_token(change):
    headers = {'Host': '127.0.0.1:1234', 'X-Mesh-Token': 'secret'} | change
    assert not authorized(headers, '127.0.0.1:1234', 'secret')


def test_allow_only_valid_loopback_authority():
    assert authorized({'Host': '127.0.0.1:1234', 'X-Mesh-Token': 'secret', 'Origin': 'http://127.0.0.1:1234'}, '127.0.0.1:1234', 'secret')


def test_real_http_server_auth_validation_and_static_assets():
    c = Controller(demo=True)
    server = Server(c)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, token=True, extra=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Mesh-Token'] = server.token
        headers.update(extra or {})
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return result
    try:
        assert request('GET', '/api/state', token=False)[0] == 403
        status, data, headers = request('GET', '/api/state')
        assert status == 200 and json.loads(data)['demo']
        assert headers['Cache-Control'] == 'no-store'
        assert request('GET', '/')[0] == 200
        assert request('GET', '/../../etc/passwd')[0] == 404
        assert request('POST', '/api/command', '{"action":"metadata","distance_m":NaN}')[0] == 400
        assert request('POST', '/api/command', '[]')[0] == 400
        assert request('POST', '/api/command', '{}', extra={'Origin': 'https://evil.example'})[0] == 403
        assert request('GET', '/api/export')[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        c.close()
        thread.join(timeout=2)


def test_observed_edges_expire_without_new_device_events():
    n = Network()
    n.receive({'type': 'mesh_observed', 'observer': A, 'via_node': B}, 0)
    assert n.snapshot(1)['edges']
    assert n.snapshot(121)['edges'] == []


def test_completed_delivery_survives_disconnect_and_stop():
    c = Controller()
    c.transfer = {'status': 'delivered'}
    c.disconnect()
    assert c.transfer['status'] == 'delivered'
    c.command({'action': 'stop'})
    assert c.transfer['status'] == 'delivered'


def test_not_ready_radio_cannot_start_measurement():
    c = Controller()
    c.connection = 'connected'
    c.network.radio = {'ready': False}
    with pytest.raises(ValueError, match='not ready'):
        c.command({'action': 'test', 'destination': B})


def test_non_ascii_token_is_rejected_without_crash():
    assert not authorized({'Host': '127.0.0.1:1234', 'X-Mesh-Token': 'é'},
                          '127.0.0.1:1234', 'secret')
