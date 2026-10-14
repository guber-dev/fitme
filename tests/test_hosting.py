import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import AppServer, Store


class HostingTests(unittest.TestCase):
    def test_cors_allows_only_configured_frontend_and_no_preflight_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.db')
            server = AppServer(('127.0.0.1', 0), store, 'http://127.0.0.1', 'https://fitme.lol')
            server.origin = f'http://127.0.0.1:{server.server_port}'
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                for origin, expected in [('https://fitme.lol', 204), ('https://evil.example', 403)]:
                    request = urllib.request.Request(server.origin + '/api/session', method='OPTIONS', headers={'Origin': origin})
                    try:
                        response = urllib.request.urlopen(request)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        self.assertEqual(response.status, expected)
                        self.assertIsNone(response.headers.get('Set-Cookie'))
                        self.assertEqual(response.headers.get('Access-Control-Allow-Origin'), origin if expected == 204 else None)
                        response.read()
                # A cross-origin frontend can create and retain the browser session.
                request = urllib.request.Request(server.origin + '/api/session', headers={'Origin': 'https://fitme.lol'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.headers.get('Access-Control-Allow-Credentials'), 'true')
                    self.assertIn('HttpOnly', response.headers['Set-Cookie'])
                    self.assertFalse(json.load(response)['generation_enabled'])
            finally:
                store.stop.set()
                store.worker.join(5)
                server.shutdown()
                server.server_close()
