"""One-shot final source edits on the isolated feature branch; deleted after use."""
from pathlib import Path

def replace(path, old, new, count=1):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    assert text.count(old) == count, (path, old, text.count(old))
    p.write_text(text.replace(old, new), encoding='utf-8')

replace('pc_app/mesh_lab/model.py',
    "edges = [{k: v for k, v in e.items() if k != 'seen'} for e in self.edges.values()]",
    "edges = [{k: v for k, v in e.items() if k != 'seen'} for e in self.edges.values() if now - e['seen'] < 120]")
replace('pc_app/mesh_lab/controller.py',
    "if self.transfer:\n            self.transfer['status'] = 'cancelled'",
    "if self.transfer and self.transfer['status'] == 'sending':\n            self.transfer['status'] = 'cancelled'")
replace('pc_app/mesh_lab/controller.py',
    "if self.transfer:\n                    self.transfer['status'] = 'cancelled'",
    "if self.transfer and self.transfer['status'] == 'sending':\n                    self.transfer['status'] = 'cancelled'")
replace('pc_app/mesh_lab/controller.py',
    "if self.connection != 'connected':\n                    raise ValueError('Connect a board first')",
    "if self.connection != 'connected':\n                    raise ValueError('Connect a board first')\n                if self.network.radio.get('ready') is False:\n                    raise ValueError('The connected firmware reports that its radio is not ready')")
replace('pc_app/mesh_lab/server.py',
    'hmac.compare_digest(supplied, token)',
    "hmac.compare_digest(supplied.encode('utf-8'), token.encode('utf-8'))")

p = Path('tests/test_mesh_lab.py')
p.write_text(p.read_text(encoding='utf-8') + '''

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
''', encoding='utf-8')
