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
    ProviderConfigError,
    fallback_for_http_status,
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
            self.assertEqual(resolve_provider('chat'), 'claude_code')

    def test_chat_missing_new_key_falls_back_to_legacy(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': '', 'GW_PROVIDER': 'claude_code',
        })):
            self.assertEqual(resolve_provider('chat'), 'claude_code')

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
