"""BFF for manual context window — owner auth before upstream."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.request

from flask import Flask

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from moments_auth import owner_session_digest


OWNER_TOKEN = 'test-owner-token'
SW_TOKEN = 'test-server-token'


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ContextWindowBffTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                'MOMENTS_OWNER_TOKEN': OWNER_TOKEN,
                'DAILY_SOFT_WINDOW_ENABLED': '0',
            },
            clear=False,
        )
        self._env.start()
        import moments_auth
        moments_auth._get_owner_token = moments_auth.owner_token_getter()

    def tearDown(self):
        self._env.stop()

    def _app(self, *, token=SW_TOKEN, upstream='http://127.0.0.1:5050'):
        from context_window_bff import create_context_window_bff_blueprint
        app = Flask(__name__)
        app.register_blueprint(
            create_context_window_bff_blueprint(
                token_getter=lambda: token,
                upstream_base=upstream,
            )
        )
        return app

    def test_anonymous_current_401(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            resp = app.test_client().get('/context-window/current')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls, [])

    def test_owner_flag_off_404(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            resp = client.get('/context-window/current')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get('error'), 'disabled')
        self.assertEqual(calls, [])

    def test_owner_flag_on_proxies_without_browser_bearer(self):
        app = self._app()
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['auth'] = req.get_header('Authorization')
            return _FakeResp(json.dumps({'ok': True, 'context_id': 1}).encode())

        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('chat.context_window.enabled', return_value=True):
            with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
                resp = client.get('/context-window/current')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured.get('auth'), 'Bearer ' + SW_TOKEN)

    def test_cross_origin_post_403(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('chat.context_window.enabled', return_value=True):
            with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
                resp = client.post(
                    '/context-window/switch',
                    json={
                        'source_context_id': 1,
                        'source_context_epoch': 1,
                        'count': 0,
                        'request_id': '00000000-0000-4000-8000-000000000001',
                    },
                    headers={'Origin': 'https://evil.example'},
                )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(calls, [])

    def test_candidates_proxy_passes_query(self):
        app = self._app()
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['url'] = req.full_url
            return _FakeResp(json.dumps({'ok': True, 'rounds': []}).encode())

        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('chat.context_window.enabled', return_value=True):
            with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
                resp = client.get(
                    '/context-window/carryover-candidates?source_context_id=2&source_context_epoch=3',
                )
        self.assertEqual(resp.status_code, 200)
        self.assertIn('source_context_id=2', captured.get('url', ''))
        self.assertIn('source_context_epoch=3', captured.get('url', ''))

    def test_browser_forged_authorization_replaced_by_server_bearer(self):
        app = self._app()
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['auth'] = req.get_header('Authorization')
            return _FakeResp(json.dumps({'ok': True}).encode())

        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('chat.context_window.enabled', return_value=True):
            with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
                resp = client.get(
                    '/context-window/current',
                    headers={'Authorization': 'Bearer forged-browser-token'},
                )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured.get('auth'), 'Bearer ' + SW_TOKEN)
        self.assertNotIn('forged-browser-token', captured.get('auth') or '')


if __name__ == '__main__':
    unittest.main()
