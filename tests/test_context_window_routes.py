"""HTTP routes for manual context window."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from flask import Flask

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from context_window_routes import create_context_window_blueprint


class ContextWindowRouteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.app = Flask(__name__)
        self.app.register_blueprint(create_context_window_blueprint(
            db_path=self.db_path,
            token_getter=lambda: 'route-test-token',
        ))
        self.client = self.app.test_client()

    def test_flag_off_404(self):
        with mock.patch.dict(os.environ, {'DAILY_SOFT_WINDOW_ENABLED': '0'}, clear=False):
            import config_store
            if hasattr(config_store, '_CACHE'):
                config_store._CACHE.clear()
            resp = self.client.get(
                '/api/context-window/current',
                headers={'Authorization': 'Bearer route-test-token'},
            )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json().get('error'), 'disabled')

    def test_anonymous_401(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get('/api/context-window/current')
        self.assertEqual(resp.status_code, 401)

    def test_flag_on_current_ok(self):
        with mock.patch('context_window_routes.enabled', return_value=True):
            resp = self.client.get(
                '/api/context-window/current',
                headers={'Authorization': 'Bearer route-test-token'},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get('ok'))
        self.assertIn('context_id', body)


if __name__ == '__main__':
    unittest.main()
