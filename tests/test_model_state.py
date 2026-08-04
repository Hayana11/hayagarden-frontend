"""MODEL-1A: provider-aware chat model state isolation."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-model-state-runtime.db'),
)

from chat.model_state import (  # noqa: E402
    ACTIVE_RELAY_DELETE_NOT_ALLOWED,
    ACTIVE_RELAY_NOT_FOUND,
    CC_MODEL_SWITCH_NOT_AVAILABLE,
    describe_chat_model_state,
    reject_cc_model_switch,
)


class DescribeChatModelStateTests(unittest.TestCase):
    def test_claude_code_is_default_and_ignores_relay(self):
        payload = describe_chat_model_state(
            'claude_code',
            relay_id='2',
            relay_name='tree',
            relay_model='claude-opus-4-6',
        )
        self.assertEqual(payload['provider'], 'claude_code')
        self.assertEqual(payload['model_mode'], 'default')
        self.assertIsNone(payload['configured_model'])
        self.assertIsNone(payload['model'])
        self.assertNotIn('relay', payload)

    def test_api_relay_uses_active_relay_model(self):
        payload = describe_chat_model_state(
            'api_relay',
            relay_id='2',
            relay_name='tree',
            relay_model='claude-opus-4-6',
        )
        self.assertEqual(payload['provider'], 'api_relay')
        self.assertEqual(payload['configured_model'], 'claude-opus-4-6')
        self.assertEqual(payload['model'], 'claude-opus-4-6')
        self.assertEqual(payload['relay'], '2')
        self.assertEqual(payload['relay_name'], 'tree')
        self.assertNotIn('model_mode', payload)

    def test_provider_switch_does_not_inherit_other_space(self):
        relay = describe_chat_model_state(
            'api_relay',
            relay_id='2',
            relay_model='claude-opus-4-6',
        )
        cc = describe_chat_model_state(
            'claude_code',
            relay_id='2',
            relay_model=relay['configured_model'],
        )
        back = describe_chat_model_state(
            'api_relay',
            relay_id='2',
            relay_model='claude-opus-4-6',
        )
        self.assertIsNone(cc['configured_model'])
        self.assertEqual(back['configured_model'], 'claude-opus-4-6')


class RejectCcModelSwitchTests(unittest.TestCase):
    def test_claude_code_rejected(self):
        self.assertEqual(
            reject_cc_model_switch('claude_code'),
            {'error': CC_MODEL_SWITCH_NOT_AVAILABLE},
        )

    def test_api_relay_allowed(self):
        self.assertIsNone(reject_cc_model_switch('api_relay'))


def _ensure_app_importable() -> None:
    os.makedirs('/opt/frontend', exist_ok=True)
    env_path = '/opt/frontend/.env'
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as fh:
            fh.write('BOARD_TOKEN_FYODOR=\nCONTEXT_USAGE_REPORT_TOKEN=\n')
    db = '/opt/frontend/memories.db'
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT, content TEXT, thinking TEXT, tool_calls TEXT,
            branches TEXT, branch_idx INTEGER,
            image_url TEXT DEFAULT '', file_url TEXT DEFAULT '',
            file_name TEXT DEFAULT '', choices TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT DEFAULT (datetime('now','+8 hours'))
        )'''
    )
    # Pre-create column so import-time migration skips row['m'] on plain tuples.
    try:
        conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'chat'"
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS relay_presets (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            key TEXT DEFAULT '',
            default_model TEXT DEFAULT '',
            capabilities TEXT DEFAULT '',
            status_url TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )'''
    )
    conn.commit()
    conn.close()


class ConfigModelRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_app_importable()
        import app as app_module  # noqa: WPS433
        cls.app_module = app_module

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'model.db')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            '''CREATE TABLE relay_presets (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                key TEXT DEFAULT '',
                default_model TEXT DEFAULT '',
                capabilities TEXT DEFAULT '',
                status_url TEXT DEFAULT ''
            )'''
        )
        conn.execute(
            'INSERT INTO relay_presets (id,name,url,default_model) VALUES (?,?,?,?)',
            (2, 'tree', 'https://example.com/v1/messages', 'claude-opus-4-6'),
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self._get_db_patch = mock.patch.object(self.app_module, 'get_db', side_effect=get_db)
        self._get_db_patch.start()
        self.client = self.app_module.app.test_client()
        self._global_model = {'MODEL': '[反重力量] claude-opus-4-6-thinking [不补]'}
        self._cfg = {
            'ACTIVE_RELAY': '2',
            'CHAT_PROVIDER': '',
            'GW_PROVIDER': 'api_relay',
            'MODEL': self._global_model['MODEL'],
        }

        def cfg_get(key, default=None):
            return self._cfg.get(key, default)

        def cfg_set(key, value):
            self._cfg[key] = value
            if key == 'MODEL':
                self._global_model['MODEL'] = value

        self._cfg_get = mock.patch.object(self.app_module.config_store, 'get', side_effect=cfg_get)
        self._cfg_set = mock.patch.object(self.app_module.config_store, 'set', side_effect=cfg_set)
        self._cfg_get.start()
        self._cfg_set.start()

    def tearDown(self):
        self._cfg_set.stop()
        self._cfg_get.stop()
        self._get_db_patch.stop()
        self.tmp.cleanup()

    def _set_chat_provider(self, provider: str):
        self._cfg['CHAT_PROVIDER'] = provider
        self._cfg['GW_PROVIDER'] = provider

    def test_get_claude_code_does_not_surface_relay_model(self):
        self._set_chat_provider('claude_code')
        resp = self.client.get('/api/config/model')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['provider'], 'claude_code')
        self.assertEqual(data['model_mode'], 'default')
        self.assertIsNone(data['configured_model'])
        self.assertIsNone(data['model'])
        self.assertNotEqual(data.get('configured_model'), 'claude-opus-4-6')
        self.assertNotIn('global_model', data)

    def test_get_api_relay_returns_active_relay_model(self):
        self._set_chat_provider('api_relay')
        resp = self.client.get('/api/config/model')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['provider'], 'api_relay')
        self.assertEqual(data['configured_model'], 'claude-opus-4-6')
        self.assertEqual(data['relay'], '2')

    def test_provider_roundtrip_keeps_spaces_isolated(self):
        self._set_chat_provider('claude_code')
        cc = self.client.get('/api/config/model').get_json()
        self._set_chat_provider('api_relay')
        relay = self.client.get('/api/config/model').get_json()
        self._set_chat_provider('claude_code')
        cc_again = self.client.get('/api/config/model').get_json()
        self.assertEqual(cc['model_mode'], 'default')
        self.assertEqual(relay['configured_model'], 'claude-opus-4-6')
        self.assertEqual(cc_again['model_mode'], 'default')
        self.assertIsNone(cc_again['configured_model'])

    def test_post_claude_code_fail_closed_no_cross_write(self):
        self._set_chat_provider('claude_code')
        before_model = self._cfg['MODEL']
        conn = sqlite3.connect(self.db_path)
        before_relay = conn.execute(
            'SELECT default_model FROM relay_presets WHERE id=2'
        ).fetchone()[0]
        conn.close()

        resp = self.client.post('/api/config/model', json={'model': '[Kiro] claude-opus-4-8 [不补]'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], CC_MODEL_SWITCH_NOT_AVAILABLE)
        self.assertEqual(self._cfg['MODEL'], before_model)
        conn = sqlite3.connect(self.db_path)
        after_relay = conn.execute(
            'SELECT default_model FROM relay_presets WHERE id=2'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(after_relay, before_relay)

    def test_post_api_relay_still_updates_active_relay(self):
        self._set_chat_provider('api_relay')
        before_global = self._cfg['MODEL']
        resp = self.client.post('/api/config/model', json={'model': 'claude-sonnet-4-6'})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['configured_model'], 'claude-sonnet-4-6')
        self.assertEqual(data['scope'], 'active_relay')
        conn = sqlite3.connect(self.db_path)
        stored = conn.execute(
            'SELECT default_model FROM relay_presets WHERE id=2'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(stored, 'claude-sonnet-4-6')
        # Active-relay write must not spill into global MODEL (CC space later).
        self.assertEqual(self._cfg['MODEL'], before_global)

    def test_catalog_claude_code_does_not_surface_relay_current(self):
        self._set_chat_provider('claude_code')
        resp = self.client.get('/api/config/model-catalog')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['provider'], 'claude_code')
        self.assertEqual(data['model_mode'], 'default')
        self.assertIsNone(data['configured_model'])
        self.assertEqual(data['current'], '')
        self.assertNotEqual(data.get('current'), 'claude-opus-4-6')

    def test_catalog_api_relay_returns_active_relay_current(self):
        self._set_chat_provider('api_relay')
        resp = self.client.get('/api/config/model-catalog')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['provider'], 'api_relay')
        self.assertEqual(data['configured_model'], 'claude-opus-4-6')
        self.assertEqual(data['current'], 'claude-opus-4-6')

    def test_post_stale_active_relay_fail_closed(self):
        self._set_chat_provider('api_relay')
        self._cfg['ACTIVE_RELAY'] = '999'
        before_global = self._cfg['MODEL']
        resp = self.client.post('/api/config/model', json={'model': 'claude-sonnet-4-6'})
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data['error'], ACTIVE_RELAY_NOT_FOUND)
        self.assertEqual(data['active_relay'], '999')
        self.assertEqual(self._cfg['MODEL'], before_global)
        conn = sqlite3.connect(self.db_path)
        stored = conn.execute(
            'SELECT default_model FROM relay_presets WHERE id=2'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(stored, 'claude-opus-4-6')

    def test_delete_active_relay_fail_closed(self):
        self._cfg['ACTIVE_RELAY'] = '2'
        resp = self.client.delete('/api/config/relay-presets/2')
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data['error'], ACTIVE_RELAY_DELETE_NOT_ALLOWED)
        conn = sqlite3.connect(self.db_path)
        still = conn.execute('SELECT id FROM relay_presets WHERE id=2').fetchone()
        conn.close()
        self.assertIsNotNone(still)
        self.assertEqual(self._cfg['ACTIVE_RELAY'], '2')

    def test_delete_inactive_relay_still_allowed(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO relay_presets (id,name,url,default_model) VALUES (?,?,?,?)',
            (5, 'spare', 'https://spare.example.com/v1/messages', 'claude-haiku'),
        )
        conn.commit()
        conn.close()
        self._cfg['ACTIVE_RELAY'] = '2'
        resp = self.client.delete('/api/config/relay-presets/5')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get('ok'))
        conn = sqlite3.connect(self.db_path)
        gone = conn.execute('SELECT id FROM relay_presets WHERE id=5').fetchone()
        conn.close()
        self.assertIsNone(gone)


if __name__ == '__main__':
    unittest.main()
