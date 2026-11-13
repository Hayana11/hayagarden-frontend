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
os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-cc-provider-env.db'),
)

from chat.cc_auth import CCProviderEnvError, build_cc_provider_env


class FakeRelayManager:
    api_url = 'https://relay.example/v1/messages'
    api_key = 'test-relay-secret'


class CCProviderEnvironmentTests(unittest.TestCase):
    def test_subscription_env_clears_gateway_auth_and_uses_oauth(self):
        env = build_cc_provider_env(
            'claude_code',
            'explicit:claude-opus-5',
            token_getter=lambda: 'subscription-token',
            base_env={
                'CLAUDE_CODE_OAUTH_TOKEN': 'old-oauth',
                'ANTHROPIC_API_KEY': 'old-api-key',
                'ANTHROPIC_AUTH_TOKEN': 'old-auth',
                'ANTHROPIC_BASE_URL': 'https://old-gateway',
                'ANTHROPIC_MODEL': 'old-model',
                'ANTHROPIC_DEFAULT_OPUS_MODEL': 'old-mapping',
                'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
                'KEEP_ME': 'present',
            },
        )
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'subscription-token')
        self.assertEqual(env['KEEP_ME'], 'present')
        for key in (
            'ANTHROPIC_API_KEY',
            'ANTHROPIC_AUTH_TOKEN',
            'ANTHROPIC_BASE_URL',
            'ANTHROPIC_MODEL',
            'ANTHROPIC_DEFAULT_OPUS_MODEL',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
            'CLAUDE_CODE_ATTRIBUTION_HEADER',
        ):
            self.assertNotIn(key, env)
        self.assertEqual(env['HAYAGARDEN_CC_PROVIDER'], 'claude_code')
        self.assertTrue(env['HAYAGARDEN_CC_PROVIDER_IDENTITY'])

    def test_api_relay_env_uses_active_gateway_and_exact_model(self):
        env = build_cc_provider_env(
            'api_relay',
            'explicit:claude-opus-5-5',
            token_getter=mock.Mock(side_effect=AssertionError('OAuth must not be read')),
            base_env={
                'CLAUDE_CODE_OAUTH_TOKEN': 'old-oauth',
                'ANTHROPIC_API_KEY': 'old-api-key',
                'ANTHROPIC_BASE_URL': 'https://old-gateway',
            },
            relay_factory=FakeRelayManager,
        )
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://relay.example')
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'test-relay-secret')
        self.assertEqual(env['ANTHROPIC_MODEL'], 'claude-opus-5-5')
        self.assertEqual(env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'], '1')
        self.assertEqual(env['CLAUDE_CODE_ATTRIBUTION_HEADER'], '0')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('test-relay-secret', env['HAYAGARDEN_CC_PROVIDER_IDENTITY'])
        self.assertEqual(env['HAYAGARDEN_CC_PROVIDER'], 'api_relay')

    def test_unknown_relay_model_fails_before_gateway_lookup(self):
        factory = mock.Mock()
        with self.assertRaisesRegex(CCProviderEnvError, 'api_relay_model_unavailable'):
            build_cc_provider_env(
                'api_relay',
                'unknown',
                relay_factory=factory,
            )
        factory.assert_not_called()

    def test_provider_switch_retires_existing_resident_once(self):
        import gateway

        class Resident:
            def __init__(self):
                self._cc_provider = 'claude_code'
                self._cc_provider_identity = 'old'
                self.alive = True

            def _kill(self, quiet=False):
                self.alive = False

        resident = Resident()

        class Holder:
            def get(self):
                return resident

        env = {
            'HAYAGARDEN_CC_PROVIDER': 'api_relay',
            'HAYAGARDEN_CC_PROVIDER_IDENTITY': 'new',
        }

        def retire(value, *, expected_key):
            self.assertIs(value, resident)
            self.assertIsNone(expected_key)
            value._kill(quiet=True)

        with mock.patch.object(gateway, '_CC_RESIDENT', Holder()), \
                mock.patch(
                    'chat.daily_runtime.close_local_resident_if_bound',
                    side_effect=retire,
                ) as close:
            self.assertTrue(gateway._retire_cc_resident_for_provider_change(env))
            self.assertFalse(resident.alive)
            self.assertEqual(resident._cc_provider, 'api_relay')
            self.assertEqual(resident._cc_provider_identity, 'new')
            self.assertFalse(gateway._retire_cc_resident_for_provider_change(env))
        close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
