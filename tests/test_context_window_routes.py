"""HTTP routes for manual context window."""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from flask import Flask

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_context as dc
from context_window_routes import create_context_window_blueprint


def _seed(db_path: str):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)
    dc.get_or_create_daily_context(
        local_day='2026-07-27',
        db_path=db_path,
        now=datetime.datetime(2026, 7, 27, 10, 0, 0),
    )


class ContextWindowRouteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        _seed(self.db_path)
        self.app = Flask(__name__)
        self.app.register_blueprint(create_context_window_blueprint(
            db_path=self.db_path,
            token_getter=lambda: 'route-test-token',
        ))
        self.client = self.app.test_client()
        self.auth = {'Authorization': 'Bearer route-test-token'}

    def test_flag_off_404(self):
        with mock.patch('context_window_routes.enabled', return_value=False):
            resp = self.client.get('/api/context-window/current', headers=self.auth)
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_401(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get('/api/context-window/current')
        self.assertEqual(resp.status_code, 401)

    def test_flag_on_current_ok(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get('/api/context-window/current', headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('ok'))

    def test_candidates_missing_source_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get('/api/context-window/carryover-candidates', headers=self.auth)
        self.assertEqual(resp.status_code, 400)

    def test_candidates_malformed_source_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get(
                '/api/context-window/carryover-candidates?source_context_id=1.9&source_context_epoch=1',
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 400)

    def test_candidates_valid_source(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            resp = self.client.get(
                '/api/context-window/carryover-candidates'
                '?source_context_id=%s&source_context_epoch=%s'
                % (cur['context_id'], cur['context_epoch']),
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('ok'))

    def test_switch_malformed_count_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': cur['context_id'],
                    'source_context_epoch': cur['context_epoch'],
                    'count': 0.9,
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 400)

    def test_switch_string_count_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': cur['context_id'],
                    'source_context_epoch': cur['context_epoch'],
                    'count': '3',
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 400)

    def test_switch_direct_route_fail_closed_without_hooks(self):
        """Production Flask route must not auto-use offline hooks."""
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            source_id = cur['context_id']
            source_epoch = cur['context_epoch']
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': source_id,
                    'source_context_epoch': source_epoch,
                    'count': 0,
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 503)
        body = resp.get_json()
        self.assertFalse(body.get('ok'))
        self.assertEqual(body.get('code'), 'switch_hooks_required')
        # Source still open; no target inserted; no committed intent.
        open_ctx = dc.get_latest_active_context('default', db_path=self.db_path)
        self.assertIsNotNone(open_ctx)
        self.assertEqual(int(open_ctx['id']), int(source_id))
        self.assertIsNone(open_ctx.get('closed_at'))
        conn = __import__('sqlite3').connect(self.db_path)
        try:
            n_manual = conn.execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual'"
            ).fetchone()[0]
            n_committed = conn.execute(
                "SELECT COUNT(*) FROM context_switch_intents WHERE status='committed'"
            ).fetchone()[0]
            n_any = conn.execute(
                'SELECT COUNT(*) FROM context_switch_intents'
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n_manual, 0)
        self.assertEqual(n_committed, 0)
        self.assertEqual(n_any, 0)

    def test_candidates_stale_source_409(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get(
                '/api/context-window/carryover-candidates'
                '?source_context_id=999&source_context_epoch=1',
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json().get('code'), 'stale_source_context')

    def test_candidates_lease_busy_423(self):
        cur = None
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
        dc.claim_daily_resident_turn(
            chat_id='default',
            context_id=int(cur['context_id']),
            expected_context_epoch=int(cur['context_epoch']),
            worker_id='w1',
            request_message_id=1,
            lease_owner='owner',
            resident_key='rk',
            db_path=self.db_path,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        with mock.patch('context_window_routes.enabled', return_value=True):
            with mock.patch(
                'chat.context_window._shanghai_now',
                return_value=datetime.datetime(2026, 7, 27, 10, 0, 0),
            ):
                resp = self.client.get(
                    '/api/context-window/carryover-candidates'
                    '?source_context_id=%s&source_context_epoch=%s'
                    % (cur['context_id'], cur['context_epoch']),
                    headers=self.auth,
                )
        self.assertEqual(resp.status_code, 423)
        self.assertEqual(resp.get_json().get('code'), 'window_busy')

    def test_switch_bool_count_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': cur['context_id'],
                    'source_context_epoch': cur['context_epoch'],
                    'count': True,
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 400)

    def test_switch_null_source_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': None,
                    'source_context_epoch': 1,
                    'count': 0,
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 400)

    def test_switch_non_object_json_400(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            for body in ('1', '[]', '"x"'):
                with self.subTest(body=body):
                    resp = self.client.post(
                        '/api/context-window/switch',
                        data=body,
                        content_type='application/json',
                        headers=self.auth,
                    )
                    self.assertEqual(resp.status_code, 400)


if __name__ == '__main__':
    unittest.main()
