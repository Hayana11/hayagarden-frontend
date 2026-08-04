"""MODEL-1A/1B: provider-aware chat model state isolation (pure, no production paths).

Red line: never create/open the production config DB, and never import app.py
(import-time migrations would touch production paths).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Isolate config_store fallback DB away from any production path.
os.environ['HAYAGARDEN_CONFIG_DB_PATH'] = str(
    Path(tempfile.gettempdir()) / 'hayagarden-test-model-state-runtime.db'
)

from chat.model_state import describe_chat_model_state  # noqa: E402
from chat.provider_router import resolve_provider  # noqa: E402
import config_store  # noqa: E402


def fake_get(values):
    def _get(key, default=None):
        return values.get(key, default)
    return _get


class DescribeChatModelStateTests(unittest.TestCase):
    def test_claude_code_default_ignores_relay(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
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

    def test_claude_code_explicit(self):
        with mock.patch.object(
            config_store, 'get',
            side_effect=fake_get({'CC_CHAT_MODEL': 'claude-sonnet-5'}),
        ):
            payload = describe_chat_model_state('claude_code')
        self.assertEqual(payload['model_mode'], 'explicit')
        self.assertEqual(payload['configured_model'], 'claude-sonnet-5')

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
        with mock.patch.object(config_store, 'get', side_effect=fake_get({'CC_CHAT_MODEL': ''})):
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


class EffectiveChatProviderTests(unittest.TestCase):
    """CHAT_PROVIDER overrides GW_PROVIDER for chat; UI model space must follow it."""

    def test_chat_provider_wins_over_gw_provider(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'GW_PROVIDER': 'api_relay',
        })):
            self.assertEqual(resolve_provider('chat'), 'claude_code')

    def test_gw_write_does_not_override_explicit_chat_provider(self):
        """Simulates: CHAT_PROVIDER=claude_code, POST GW_PROVIDER=api_relay.
        Effective chat remains claude_code → model space stays CC."""
        cfg = {
            'CHAT_PROVIDER': 'claude_code',
            'GW_PROVIDER': 'claude_code',
            'CC_CHAT_MODEL': '',
        }

        def _get(key, default=None):
            return cfg.get(key, default)

        def _set(key, value):
            cfg[key] = value

        with mock.patch.object(config_store, 'get', side_effect=_get), \
             mock.patch.object(config_store, 'set', side_effect=_set):
            # POST /api/config/provider only writes GW_PROVIDER.
            config_store.set('GW_PROVIDER', 'api_relay')
            self.assertEqual(cfg['GW_PROVIDER'], 'api_relay')
            effective = resolve_provider('chat')
            self.assertEqual(effective, 'claude_code')
            space = describe_chat_model_state(effective)
            self.assertEqual(space['provider'], 'claude_code')
            self.assertEqual(space['model_mode'], 'default')
            self.assertIsNone(space['configured_model'])


if __name__ == '__main__':
    unittest.main()
