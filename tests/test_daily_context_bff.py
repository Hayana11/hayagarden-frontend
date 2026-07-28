"""BFF proxy for Soft Window — browser never sees the Bearer token."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock
import urllib.error
import urllib.request

from flask import Flask


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


def _req_headers(req: urllib.request.Request) -> dict:
    try:
        return {k: v for k, v in req.header_items()}
    except Exception:
        return {k: v for k, v in req.headers.items()}


class DailyContextBffTests(unittest.TestCase):
    def _app(self, *, token='test-server-token', upstream='http://127.0.0.1:5050'):
        from daily_context_bff import create_daily_context_bff_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_daily_context_bff_blueprint(
                token_getter=lambda: token,
                upstream_base=upstream,
            )
        )
        return app

    def test_flag_default_still_off(self):
        self.assertNotEqual(os.environ.get('DAILY_SOFT_WINDOW_ENABLED', '0'), '1')
        from chat import daily_context as dc
        with mock.patch.dict(os.environ, {'DAILY_SOFT_WINDOW_ENABLED': '0'}, clear=False):
            self.assertFalse(dc.enabled())

    def test_bff_returns_upstream_404_when_disabled(self):
        app = self._app()
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['url'] = req.full_url
            captured['headers'] = _req_headers(req)
            raise urllib.error.HTTPError(
                req.full_url,
                404,
                'Not Found',
                hdrs={},
                fp=io.BytesIO(json.dumps({'ok': False, 'error': 'disabled'}).encode()),
            )

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get('/daily-context/current')
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body.get('error'), 'disabled')
        self.assertIn('/api/daily-context/current', captured['url'])
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer test-server-token')

    def test_bff_injects_authorization_bearer_from_token_getter(self):
        app = self._app(token='secret-from-getter')
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['url'] = req.full_url
            captured['headers'] = _req_headers(req)
            payload = json.dumps({
                'ok': True,
                'context_id': 1,
                'context_epoch': 1,
                'local_day': '2026-07-28',
                'boundary_message_id': 0,
                'carryover_unit': 'round',
                'requested_round_count': None,
                'selected_round_count': 0,
                'selected_message_count': 0,
                'selected_message_ids': [],
                'carryover_count': 0,
                'selection_finalized': False,
                'handoff_status': 'ABSENT',
                'resident_generation': 1,
            }).encode()
            return _FakeResp(payload, 200)

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get('/daily-context/current')
        self.assertEqual(resp.status_code, 200)
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer secret-from-getter')
        self.assertTrue(captured['url'].endswith('/api/daily-context/current'))

    def test_browser_authorization_header_not_forwarded(self):
        app = self._app(token='server-only-token')
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['headers'] = _req_headers(req)
            return _FakeResp(
                b'{"ok":true,"rounds":[],"carryover_unit":"round",'
                b'"available_round_count":0,"candidates":[],'
                b'"context_id":1,"context_epoch":1}',
                200,
            )

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get(
                '/daily-context/carryover-candidates',
                headers={'Authorization': 'Bearer browser-forged-token'},
            )
        self.assertEqual(resp.status_code, 200)
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer server-only-token')
        self.assertNotIn('browser-forged-token', auth)

    def test_missing_token_when_flag_off_returns_404(self):
        from daily_context_bff import create_daily_context_bff_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_daily_context_bff_blueprint(token_getter=lambda: '')
        )
        with mock.patch('chat.daily_context.enabled', return_value=False):
            client = app.test_client()
            resp = client.get('/daily-context/current')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get('error'), 'disabled')


if __name__ == '__main__':
    unittest.main()
