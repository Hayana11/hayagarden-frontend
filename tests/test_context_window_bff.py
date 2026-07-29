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

    def _switch_success_body(self, **over):
        base = {
            'ok': True,
            'source_context_id': 1,
            'source_context_epoch': 2,
            'source_resident_generation': 1,
            'target_context_id': 3,
            'target_context_epoch': 4,
            'requested_round_count': 0,
            'selected_round_count': 0,
            'selected_message_ids': [],
        }
        base.update(over)
        return json.dumps(base).encode()

    def _post_switch(self, *, status=200, body=None, callback=None):
        calls = {'callback': []}
        cb = callback if callback is not None else (lambda r: calls['callback'].append(r))
        from context_window_bff import create_context_window_bff_blueprint
        app_obj = Flask(__name__)
        app_obj.register_blueprint(
            create_context_window_bff_blueprint(
                token_getter=lambda: SW_TOKEN,
                upstream_base='http://127.0.0.1:5050',
                on_switch_success=cb,
            )
        )
        payload = body if body is not None else self._switch_success_body()

        def fake_urlopen(req, timeout=20):
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, 'err', hdrs=None, fp=io.BytesIO(payload),
                )
            return _FakeResp(payload, status=status)

        client = app_obj.test_client()
        client.set_cookie('moments_owner', owner_session_digest(OWNER_TOKEN))
        with mock.patch('chat.context_window.enabled', return_value=True):
            with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen):
                resp = client.post(
                    '/context-window/switch',
                    json={
                        'source_context_id': 1,
                        'source_context_epoch': 2,
                        'count': 0,
                        'request_id': '00000000-0000-4000-8000-000000000001',
                    },
                )
        return resp, calls

    def test_switch_success_invokes_callback_once(self):
        resp, calls = self._post_switch()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls['callback']), 1)
        payload = calls['callback'][0]
        self.assertEqual(payload['source_context_id'], 1)
        self.assertEqual(payload['source_context_epoch'], 2)
        self.assertEqual(payload['source_resident_generation'], 1)
        self.assertEqual(payload['target_context_id'], 3)
        self.assertEqual(payload['target_context_epoch'], 4)

    def test_switch_upstream_409_does_not_invoke_callback(self):
        resp, calls = self._post_switch(
            status=409,
            body=json.dumps({'ok': False, 'code': 'stale_source_context'}).encode(),
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(calls['callback'], [])

    def test_switch_upstream_423_does_not_invoke_callback(self):
        resp, calls = self._post_switch(
            status=423,
            body=json.dumps({'ok': False, 'code': 'window_busy'}).encode(),
        )
        self.assertEqual(resp.status_code, 423)
        self.assertEqual(calls['callback'], [])

    def test_switch_upstream_500_does_not_invoke_callback(self):
        resp, calls = self._post_switch(
            status=500,
            body=json.dumps({'ok': False, 'error': 'boom'}).encode(),
        )
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(calls['callback'], [])

    def test_switch_malformed_success_body_does_not_invoke_callback(self):
        resp, calls = self._post_switch(
            status=200,
            body=json.dumps({'ok': True, 'source_context_id': 1}).encode(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls['callback'], [])

    def test_switch_callback_exception_still_returns_upstream_success(self):
        seen = {'n': 0}

        def boom(_result):
            seen['n'] += 1
            raise RuntimeError('resident close failed')

        resp, _calls = self._post_switch(callback=boom)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json().get('ok'), True)
        self.assertEqual(seen['n'], 1)

    def test_switch_replay_success_callback_receives_same_payload(self):
        body = self._switch_success_body(target_context_id=9, target_context_epoch=10)
        resp1, calls1 = self._post_switch(body=body)
        resp2, calls2 = self._post_switch(body=body)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(len(calls1['callback']), 1)
        self.assertEqual(len(calls2['callback']), 1)
        self.assertEqual(calls1['callback'][0], calls2['callback'][0])

    def test_switch_replay_does_not_close_target_resident_binding(self):
        from chat import daily_context as dc
        from chat import daily_runtime as dr

        resident = mock.MagicMock()
        resident._kill = mock.MagicMock()
        target_epoch = 10
        target_key = dc.make_resident_key(
            chat_id='default', context_epoch=target_epoch, resident_generation=1,
        )
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=target_key,
            context_id=9,
            context_epoch=target_epoch,
            resident_generation=1,
            bound_cursor_message_id=None,
            process_generation=1,
            tool_profile='daily',
        ))

        def close_like_gateway(result):
            dr.close_local_resident_for_context_switch(
                resident,
                source_context_id=int(result['source_context_id']),
                source_context_epoch=int(result['source_context_epoch']),
                source_resident_generation=int(result['source_resident_generation']),
            )

        body = self._switch_success_body(
            source_context_id=1,
            source_context_epoch=2,
            source_resident_generation=1,
            target_context_id=9,
            target_context_epoch=target_epoch,
        )
        resp, calls = self._post_switch(body=body, callback=close_like_gateway)
        self.assertEqual(resp.status_code, 200)
        resident._kill.assert_not_called()
        self.assertIsNotNone(dr.get_local_binding())
        dr.reset_bindings_for_tests()


if __name__ == '__main__':
    unittest.main()
