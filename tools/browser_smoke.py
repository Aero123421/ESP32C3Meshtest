"""Real browser/HTTP/controller regression; serial hardware is explicitly simulated.

Run separately from pytest: python tools/browser_smoke.py --browser chromium
Requires Playwright. Never opens physical ports or uploads firmware.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pc_app'))
from mesh_lab import controller as controller_module
from mesh_lab import server as server_module
from mesh_lab.controller import Controller
from mesh_lab.server import Server

LOCAL, REMOTE, RELAY = '0x0000A001', '0x0000A002', '0x0000A003'
PORT = '/test/mesh-simulator'
PORTS = [{'device': PORT, 'description': 'CI simulator — not a physical radio'}]


class SimulatedSerialLink:
    """Only the serial boundary is replaced; UI, HTTP and controller are real."""
    def __init__(self, port, events, generation):
        assert port == PORT
        self.events, self.generation = events, generation
        self.closed = False

    def emit(self, kind, **fields):
        self.events.put_nowait({'kind': kind, 'generation': self.generation, **fields})

    def start(self):
        self.emit('connected', port=PORT)

    def close(self):
        self.closed = True

    def send(self, packet):
        if self.closed:
            return False
        self.emit('tx', payload=packet)
        cmd = packet.get('cmd')
        if cmd == 'get_radio_profile':
            event = {'type': 'radio_profile', 'node_id': LOCAL, 'ready': True,
                     'chip': 'ESP32-S3 (SIMULATED)', 'profile': 'balanced', 'channel': 1,
                     'bandwidth_mhz': 20, 'espnow_rate_kbps': 1000,
                     'tx_power_readback_qdbm': 72, 'readback_ok': True, 'power_save': False}
        elif cmd == 'get_nodes':
            event = {'type': 'node_list', 'nodes': [
                {'node_id': n, 'is_self': n == LOCAL, 'age_ms': 0, 'rssi': None,
                 'free_heap': 120000} for n in (LOCAL, REMOTE, RELAY)]}
        elif cmd == 'get_routes':
            event = {'type': 'route_list', 'routes': [
                {'dst_node_id': REMOTE, 'next_hop_node_id': RELAY,
                 'hops': 2, 'rank': 0, 'metric_q8': 512, 'age_ms': 0}]}
        elif cmd == 'get_stats':
            event = {'type': 'stats', 'mesh': {}}
        elif cmd == 'ping_probe':
            assert packet['probe_bytes'] == 1000, 'UI must preserve the selected payload size'
            self.emit('rx', payload={'type': 'ack', 'ok': True, 'cmd': 'ping_probe'})
            # Exactly one loss, despite a successful local ACK, tests PDR semantics.
            if packet['seq'] == 2:
                return True
            event = {'type': 'pong', 'src': REMOTE, 'ping_id': packet['ping_id'],
                     'probe_hash_ok': True, 'request_hops': 1, 'reply_hops': 1}
        elif packet.get('type') == 'chat':
            self.emit('rx', payload={'type': 'delivery_ack', 'src': packet['dst'],
                                    'e2e_id': packet['e2e_id'], 'status': 'ok'})
            event = {'type': 'chat', 'src': REMOTE, 'text': packet['text'],
                     'e2e_id': packet['e2e_id']}
        else:
            return True
        self.emit('rx', payload=event)
        return True


@contextmanager
def running_app(demo=False):
    with patch.object(controller_module, 'serial_ports', return_value=PORTS), \
         patch.object(server_module, 'serial_ports', return_value=PORTS), \
         patch.object(controller_module, 'SerialLink', SimulatedSerialLink):
        controller = Controller(demo=demo)
        server = Server(controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        controller.start()
        thread.start()
        try:
            yield controller, f'http://{server.authority}/#token={server.token}'
        finally:
            server.shutdown()
            server.server_close()
            controller.close()
            thread.join(timeout=3)
            assert not thread.is_alive()


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Horizontal page overflow'


def screenshot(page, path, engine, errors, tool_warnings):
    """Keep strict app CSP; attribute only the proven Playwright WebKit injection.

    Playwright 1.57 screenshotter.inPagePrepareForScreenshots(syncAnimations)
    temporarily inserts <style>body {}</style> in WebKit. Our CSP correctly blocks
    it. Observe that exact DOM insertion, require exactly its known warning during
    this screenshot, and report it separately. Other console errors still fail.
    No bypass_csp, unsafe-inline, blanket warning filter or production change.
    """
    assert not errors, '\n'.join(errors)
    page.evaluate("""() => {
      window.__screenshotStyles = [];
      window.__screenshotObserver = new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes)
          if (node.nodeName === 'STYLE') window.__screenshotStyles.push(node.textContent);
      });
      window.__screenshotObserver.observe(document.head, {childList:true});
    }""")
    start = len(errors)
    try:
        page.screenshot(path=str(path), full_page=True, caret='initial')
    finally:
        styles = page.evaluate("""() => {
          window.__screenshotObserver.disconnect();
          const result = window.__screenshotStyles;
          delete window.__screenshotObserver;
          delete window.__screenshotStyles;
          return result;
        }""")
    known = ("Refused to apply a stylesheet because its hash, its nonce, or 'unsafe-inline' "
             "does not appear in the style-src directive of the Content Security Policy.")
    if engine == 'webkit' and styles == ['body {}'] and errors[start:] == [known]:
        tool_warnings.append({'screenshot': path.name, 'message': known,
                              'source': 'Playwright 1.57 WebKit screenshot animation synchronization',
                              'observed_injected_style': styles[0], 'app_csp': 'unchanged; injection blocked'})
        del errors[start:]
    assert not errors, '\n'.join(errors)


def main():
    from playwright.sync_api import sync_playwright, expect
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', choices=('chromium', 'webkit'), default='chromium')
    parser.add_argument('--output', type=Path, default=ROOT / 'browser-artifacts')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors, checks, tool_warnings = [], [], []
    with sync_playwright() as playwright:
        browser = getattr(playwright, args.browser).launch(headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' else None)
        try:
            with running_app(demo=True) as (_, url):
                page.goto(url)
                expect(page).to_have_title('Mesh Lab · C3 / S3')
                expect(page.locator('.map-node')).to_have_count(6)
                expect(page.locator('#demo-banner')).to_be_visible()
                expect(page.locator('#connect')).to_be_disabled()
                no_overflow(page)
                screenshot(page, args.output / 'demo-desktop.png', args.browser, errors, tool_warnings)
                for name in ('tests', 'messages', 'firmware', 'logs', 'topology'):
                    page.locator(f'[data-page="{name}"]').click()
                    expect(page.locator(f'#page-{name}')).to_be_visible()
                    no_overflow(page)
                page.locator('.map-node').nth(1).click()
                expect(page.locator('#inspector h3')).to_have_text('0x0000A002')
                before = page.locator('#map-content').get_attribute('transform')
                page.locator('#zoom-in').click()
                assert page.locator('#map-content').get_attribute('transform') != before
                page.locator('#fit').click()
                assert page.locator('#map-content').get_attribute('transform') == before
                page.set_viewport_size({'width': 390, 'height': 844})
                no_overflow(page)
                screenshot(page, args.output / 'demo-mobile.png', args.browser, errors, tool_warnings)
                checks += ['demo isolation', 'five screens', 'node selection', 'zoom/fit', 'desktop/mobile overflow']
                page.goto('about:blank')

            page.set_viewport_size({'width': 1440, 'height': 1000})
            with running_app() as (controller, url):
                page.goto(url)
                expect(page.locator('#graph-empty')).to_be_visible()
                page.locator('#port').select_option(PORT)
                page.locator('#connect').click()
                expect(page.locator('#connection-status')).to_contain_text('接続中')
                expect(page.locator('.map-node')).to_have_count(3)
                page.locator('[data-page="tests"]').click()
                form = page.locator('#test-form')
                form.locator('[name="destination"]').select_option(REMOTE)
                form.locator('[name="count"]').fill('3')
                form.locator('[name="size"]').select_option('1000')
                form.locator('[name="interval_ms"]').fill('100')
                form.locator('[name="timeout_ms"]').fill('500')
                form.locator('button[type="submit"]').click()
                expect(page.locator('#test-status')).to_have_text('完了', timeout=15000)
                expect(page.locator('#test-pdr')).to_have_text('66.67%')
                result = controller.snapshot()['test']
                assert (result['received'], result['lost'], result['pending']) == (2, 1, 0)
                screenshot(page, args.output / 'simulated-ping.png', args.browser, errors, tool_warnings)
                page.locator('[data-page="messages"]').click()
                message = '<img src=x onerror="window.__meshXss=1"> 日本語🙂'
                form = page.locator('#message-form')
                form.locator('[name="destination"]').select_option(REMOTE)
                form.locator('[name="text"]').fill(message)
                form.locator('button[type="submit"]').click()
                expect(page.locator('#transfer-status')).to_contain_text('配達ACKを確認')
                expect(page.locator('.message-item p')).to_have_text(message)
                assert page.evaluate('window.__meshXss === undefined')
                assert page.locator('#messages img').count() == 0
                with page.expect_download() as download:
                    page.locator('#export').click()
                destination = args.output / 'simulated-session.json'
                download.value.save_as(str(destination))
                exported = json.loads(destination.read_text(encoding='utf-8'))
                assert exported['test']['pdr'] == 66.67
                assert len(exported['test_samples']) == 3
                assert exported['transfer']['status'] == 'delivered'
                page.locator('#connect').click()
                expect(page.locator('#connection-status')).to_contain_text('未接続')
                assert controller.link is None
                checks += ['simulated USB connect/disconnect', '1000-byte probe request',
                           'local ACK is not delivery', '2/3 PDR', 'remote delivery ACK',
                           'Japanese/emoji text', 'HTML injection escaped', 'session download']
                page.goto('about:blank')
            assert not errors, '\n'.join(errors)
        except Exception:
            page.screenshot(path=str(args.output / 'failure.png'), full_page=True, caret='initial')
            raise
        finally:
            (args.output / 'summary.json').write_text(json.dumps({
                'browser': args.browser, 'checks': checks, 'page_errors': errors,
                'screenshot_tool_warnings': tool_warnings,
                'hardware': 'SIMULATED ONLY; not RF range or real USB validation',
                'viewports': ['1440x1000', '390x844'],
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            context.close()
            browser.close()
    print(f'{args.browser}: {len(checks)} browser checks passed; radio hardware was simulated.')
    if tool_warnings:
        print(f'{len(tool_warnings)} WebKit screenshot-tool CSP rejections recorded separately; app policy unchanged.')


if __name__ == '__main__':
    main()
