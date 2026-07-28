"""BFF proxy for Soft Window — owner auth before upstream; token never leaves server."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock
import urllib.error
import urllib.request

from flask import Flask

from moments_auth import owner_session_digest


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


OWNER_TOKEN = 'test-owner-token'
SW_TOKEN = 'test-server-token'


class DailyContextBffTests(unittest.TestCase):
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
        from daily_context_bff import create_daily_context_bff_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_daily_context_bff_blueprint(
                token_getter=lambda: token,
                upstream_base=upstream,
            )
        )
        return app

    def _authed_client(self, app, extra_headers=None):
        client = app.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        return client, (extra_headers or {})

    def test_flag_default_still_off(self):
        self.assertNotEqual(os.environ.get('DAILY_SOFT_WINDOW_ENABLED', '0'), '1')
        from chat import daily_context as dc
        with mock.patch.dict(os.environ, {'DAILY_SOFT_WINDOW_ENABLED': '0'}, clear=False):
            self.assertFalse(dc.enabled())

    def test_anonymous_current_401_never_reaches_upstream(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get('/daily-context/current')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls, [])
        self.assertEqual(resp.get_json().get('error'), 'unauthorized')

    def test_anonymous_candidates_401_never_reaches_upstream(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get('/daily-context/carryover-candidates')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls, [])

    def test_anonymous_select_401_never_reaches_upstream(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.post(
                '/daily-context/select-carryover',
                data=b'{"count":3}',
                content_type='application/json',
                headers={'Origin': 'http://localhost'},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls, [])

    def test_valid_owner_cookie_reaches_upstream(self):
        app = self._app()
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
            client, headers = self._authed_client(app)
            resp = client.get('/daily-context/current', headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('/api/daily-context/current', captured['url'])
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer ' + SW_TOKEN)

    def test_forged_browser_bearer_cannot_bypass_without_owner(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client = app.test_client()
            resp = client.get(
                '/daily-context/current',
                headers={'Authorization': 'Bearer forged-soft-window-token'},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(calls, [])

    def test_browser_authorization_never_replaces_server_bearer(self):
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
            client, headers = self._authed_client(
                app, {'Authorization': 'Bearer browser-forged-token'}
            )
            resp = client.get('/daily-context/carryover-candidates', headers=headers)
        self.assertEqual(resp.status_code, 200)
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer server-only-token')
        self.assertNotIn('browser-forged-token', auth)

    def test_flag_off_authenticated_owner_receives_404(self):
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
            client, headers = self._authed_client(app)
            resp = client.get('/daily-context/current', headers=headers)
        self.assertEqual(resp.status_code, 404)
        body = resp.get_json()
        self.assertEqual(body.get('error'), 'disabled')
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer ' + SW_TOKEN)

    def test_cross_origin_post_rejected(self):
        app = self._app()
        calls = []

        def fake_urlopen(req, timeout=20):
            calls.append(req)
            raise AssertionError('upstream must not be called')

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client, headers = self._authed_client(app, {'Origin': 'https://evil.example'})
            resp = client.post(
                '/daily-context/select-carryover',
                data=b'{"count":5}',
                content_type='application/json',
                headers=headers,
            )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(calls, [])
        self.assertIn('cross-origin', resp.get_json().get('error', ''))

    def test_same_origin_post_allowed(self):
        app = self._app()
        captured = {}

        def fake_urlopen(req, timeout=20):
            captured['headers'] = _req_headers(req)
            captured['body'] = req.data
            return _FakeResp(
                b'{"ok":true,"context_id":1,"context_epoch":1,"carryover_unit":"round",'
                b'"requested_round_count":3,"selected_round_count":3,'
                b'"selected_message_count":2,"selected_message_ids":[1,2],'
                b'"carryover_count":3,"finalized_at":"2026-07-28 04:10:00"}',
                200,
            )

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
            client, headers = self._authed_client(app, {'Origin': 'http://localhost'})
            # Flask test client Host is localhost by default
            resp = client.post(
                '/daily-context/select-carryover',
                data=b'{"count":3}',
                content_type='application/json',
                headers=headers,
            )
        self.assertEqual(resp.status_code, 200)
        auth = captured['headers'].get('Authorization') or captured['headers'].get('authorization')
        self.assertEqual(auth, 'Bearer ' + SW_TOKEN)

    def test_missing_token_when_flag_off_returns_404_for_owner(self):
        from daily_context_bff import create_daily_context_bff_blueprint

        app = Flask(__name__)
        app.register_blueprint(
            create_daily_context_bff_blueprint(token_getter=lambda: '')
        )
        with mock.patch('chat.daily_context.enabled', return_value=False):
            client, headers = self._authed_client(app)
            resp = client.get('/daily-context/current', headers=headers)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get('error'), 'disabled')


if __name__ == '__main__':
    unittest.main()
