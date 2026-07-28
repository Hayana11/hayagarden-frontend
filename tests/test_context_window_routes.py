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

    def test_switch_valid(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            cur = self.client.get('/api/context-window/current', headers=self.auth).get_json()
            resp = self.client.post(
                '/api/context-window/switch',
                json={
                    'source_context_id': cur['context_id'],
                    'source_context_epoch': cur['context_epoch'],
                    'count': 0,
                    'request_id': str(uuid.uuid4()),
                },
                headers=self.auth,
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('ok'))


if __name__ == '__main__':
    unittest.main()
