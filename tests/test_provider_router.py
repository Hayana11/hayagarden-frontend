"""A1 provider routing and fail-closed fallback tests."""

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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

import config_store
from chat.provider_router import (
    GenerationClass,
    ProviderConfigError,
    capture_generation_authority,
    fallback_for_http_status,
    resolve_generation_provider,
    resolve_provider,
)


def fake_get(values):
    def _get(key, default=None):
        return values.get(key, default)
    return _get


class ProviderRouterTests(unittest.TestCase):
    def test_chat_new_key_wins(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code', 'GW_PROVIDER': 'api_relay',
        })):
            self.assertEqual(resolve_generation_provider(), 'claude_code')
            self.assertEqual(resolve_provider('chat'), 'claude_code')

    def test_chat_missing_new_key_falls_back_to_legacy(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': '', 'GW_PROVIDER': 'claude_code',
        })):
            self.assertEqual(resolve_generation_provider(), 'claude_code')
            self.assertEqual(resolve_provider('chat'), 'claude_code')

    def test_chat_api_relay_is_generation_authority(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'api_relay', 'GW_PROVIDER': 'claude_code',
        })):
            self.assertEqual(resolve_generation_provider(), 'api_relay')
            self.assertEqual(resolve_provider('chat'), 'api_relay')

    def test_wake_and_background_never_influence_generation_authority(self):
        for wake, background in (
            ('api_relay', 'claude_code'),
            ('claude_code', 'api_relay'),
            ('inherit', 'claude_code'),
        ):
            with self.subTest(wake=wake, background=background), \
                    mock.patch.object(config_store, 'get', side_effect=fake_get({
                        'CHAT_PROVIDER': 'claude_code',
                        'GW_PROVIDER': 'api_relay',
                        'WAKE_PROVIDER': wake,
                        'BACKGROUND_PROVIDER': background,
                    })):
                self.assertEqual(resolve_generation_provider(), 'claude_code')

    def test_snapshot_does_not_read_legacy_surface_or_fallback_keys(self):
        calls = []

        def _get(key, default=None):
            calls.append(key)
            return {'CHAT_PROVIDER': 'claude_code'}.get(key, default)

        with mock.patch.object(config_store, 'get', side_effect=_get), \
                mock.patch('chat.cc_model.cc_model_identity', return_value='default'):
            capture_generation_authority()
        self.assertNotIn('WAKE_PROVIDER', calls)
        self.assertNotIn('BACKGROUND_PROVIDER', calls)
        self.assertNotIn('FALLBACK_PROVIDER', calls)

    def test_chat_scope_is_compatibility_delegation(self):
        with mock.patch(
            'chat.provider_router.resolve_generation_provider',
            return_value='api_relay',
        ) as resolver:
            self.assertEqual(resolve_provider('chat'), 'api_relay')
        resolver.assert_called_once_with()

    def test_snapshot_is_frozen_after_runtime_config_changes(self):
        values = {
            'CHAT_PROVIDER': 'claude_code',
            'GW_PROVIDER': 'api_relay',
        }
        with mock.patch.object(config_store, 'get', side_effect=fake_get(values)), \
                mock.patch('chat.cc_model.cc_model_identity', return_value='explicit:claude-sonnet-5'):
            snapshot = capture_generation_authority()
            values['CHAT_PROVIDER'] = 'api_relay'
            self.assertEqual(snapshot.provider, 'claude_code')
            self.assertEqual(snapshot.model_identity, 'explicit:claude-sonnet-5')

    def test_cc_snapshot_uses_official_cc_model_resolver(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
        })), mock.patch(
            'chat.cc_model.cc_model_identity',
            return_value='explicit:claude-opus-5',
        ) as cc_resolver:
            snapshot = capture_generation_authority()
        self.assertEqual(snapshot.provider, 'claude_code')
        self.assertEqual(snapshot.model_identity, 'explicit:claude-opus-5')
        cc_resolver.assert_called_once_with()

    def test_relay_snapshot_uses_active_relay_model_authority(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'api_relay',
            'MODEL': 'must-not-be-used',
            'WS_MODEL': 'must-not-be-used',
        })), mock.patch(
            'relay.manager.resolve_active_relay_model_identity',
            return_value='claude-opus-5',
        ) as relay_resolver:
            snapshot = capture_generation_authority()
        self.assertEqual(snapshot.provider, 'api_relay')
        self.assertEqual(snapshot.model_identity, 'claude-opus-5')
        relay_resolver.assert_called_once_with()

    def test_relay_snapshot_preserves_explicit_unknown_model(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'api_relay',
        })), mock.patch(
            'relay.manager.resolve_active_relay_model_identity',
            return_value='unknown',
        ):
            self.assertEqual(capture_generation_authority().model_identity, 'unknown')

    def test_relay_model_authority_uses_only_active_preset_default(self):
        from relay.manager import resolve_active_relay_model_identity

        with mock.patch(
            'relay.manager._lookup_active_relay',
            return_value={'default_model': 'claude-opus-5'},
        ):
            self.assertEqual(resolve_active_relay_model_identity(), 'claude-opus-5')
        with mock.patch('relay.manager._lookup_active_relay', return_value=None):
            self.assertEqual(resolve_active_relay_model_identity(), 'unknown')

    def test_generation_classification_contract(self):
        self.assertEqual(GenerationClass.IDENTITY_BEARING.value, 'identity_bearing')
        self.assertEqual(GenerationClass.CONTINUITY_AUTHORING.value, 'continuity_authoring')
        self.assertEqual(GenerationClass.INFRASTRUCTURE_HELPER.value, 'infrastructure_helper')

    def test_wake_inherit_and_background_are_explicit(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(resolve_provider('wake'), 'claude_code')
            self.assertEqual(resolve_provider('background'), 'api_relay')

    def test_invalid_provider_fails_closed(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'treegpt',
        })):
            with self.assertRaises(ProviderConfigError):
                resolve_provider('chat')

    def test_default_none_never_silently_uses_deepseek(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'FALLBACK_PROVIDER': 'none',
        })):
            for status in (401, 403, 429, 500, 503):
                self.assertEqual(fallback_for_http_status(status), 'none')

    def test_explicit_deepseek_is_limited_to_supported_statuses(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'FALLBACK_PROVIDER': 'deepseek',
        })):
            for status in (401, 403, 503):
                self.assertEqual(fallback_for_http_status(status), 'deepseek')
            for status in (400, 429, 500):
                self.assertEqual(fallback_for_http_status(status), 'none')


if __name__ == '__main__':
    unittest.main()
