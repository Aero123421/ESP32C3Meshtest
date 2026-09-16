"""Loopback-only browser UI with per-launch token, Host/Origin checks and no CORS."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .controller import Controller
from .transport import serial_ports

WEB = Path(__file__).with_name('web')
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


def authorized(headers, authority: str, token: str) -> bool:
    if headers.get('Host') != authority:
        return False
    origin = headers.get('Origin')
    if origin is not None and origin != 'http://' + authority:
        return False
    if headers.get('Sec-Fetch-Site') == 'cross-site':
        return False
    supplied = headers.get('X-Mesh-Token', '')
    return isinstance(supplied, str) and hmac.compare_digest(supplied.encode('utf-8'), token.encode('utf-8'))


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller: Controller, port: int = 0):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.authority = f'127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass  # Never write authentication tokens or message text into access logs.

    def reply(self, code: int, data: bytes, mime='application/json; charset=utf-8') -> None:
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, status: int, value) -> None:
        self.reply(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def allow(self) -> bool:
        if authorized(self.headers, self.server.authority, self.server.token):
            return True
        self.close_connection = True
        self.json_reply(403, {'error': 'Open the authenticated URL printed by Mesh Lab.'})
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if self.headers.get('Host') != self.server.authority:
            self.json_reply(403, {'error': 'Invalid Host'})
            return
        if path in ASSETS:
            filename, mime = ASSETS[path]
            self.reply(200, (WEB / filename).read_bytes(), mime)
            return
        if not self.allow():
            return
        if path == '/api/state':
            self.json_reply(200, self.server.controller.snapshot())
        elif path == '/api/ports':
            self.json_reply(200, {'ports': serial_ports()})
        elif path == '/api/export':
            self.json_reply(200, self.server.controller.export())
        else:
            self.json_reply(404, {'error': 'Not found'})

    def do_POST(self) -> None:
        if not self.allow():
            return
        if self.path != '/api/command':
            self.json_reply(404, {'error': 'Not found'})
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.close_connection = True
            self.json_reply(415, {'error': 'JSON required'})
            return
        try:
            size = int(self.headers.get('Content-Length', '-1'))
            if not 0 < size <= 65536:
                raise ValueError('Request length must be 1..65536 bytes')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request')
            value = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Non-finite number')))
            if not isinstance(value, dict):
                raise ValueError('JSON object required')
            self.json_reply(200, self.server.controller.command(value))
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            self.close_connection = True
            self.json_reply(400, {'error': str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description='Mesh Lab — local C3/S3 test console')
    parser.add_argument('--demo', action='store_true', help='Read-only synthetic topology; does not access a radio')
    parser.add_argument('--no-browser', action='store_true', help='Print the local URL without opening a browser')
    parser.add_argument('--port', type=int, default=0, help='Loopback HTTP port (default: choose an unused port)')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0..65535')
    controller = Controller(demo=args.demo)
    server = Server(controller, args.port)
    controller.start()
    url = f'http://{server.authority}/#token={server.token}'
    print(f'Mesh Lab: {url}\nPress Ctrl+C to stop. Do not share the token URL.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        server.server_close()


if __name__ == '__main__':
    main()
