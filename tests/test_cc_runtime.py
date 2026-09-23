"""Managed native Claude Code runtime contract tests (no provider requests)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import cc_runtime
from chat import claude_runtime_state


class ClaudeRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'runtime-state'
        self.home = self.root / 'service-home'
        self.versions = self.home / cc_runtime.NATIVE_VERSIONS_RELATIVE
        self.versions.mkdir(parents=True)
        self.state_patch = mock.patch.object(cc_runtime, 'RUNTIME_STATE_DIR', self.state)
        self.home_patch = mock.patch.object(cc_runtime, 'service_home', return_value=self.home)
        self.state_patch.start()
        self.home_patch.start()
        cc_runtime.clear_version_cache()

    def tearDown(self):
        self.home_patch.stop()
        self.state_patch.stop()
        cc_runtime.clear_version_cache()
        self.temp.cleanup()

    def _native(self, version):
        binary = self.versions / version
        binary.write_text('#!/bin/sh\necho "%s (Claude Code)"\n' % version, encoding='utf-8')
        binary.chmod(0o755)
        return binary

    def _active(self, version='2.1.280'):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state / 'active-version').write_text(version + '\n', encoding='ascii')

    def test_minimum_is_floor_not_exact_pin(self):
        self.assertEqual(cc_runtime.MINIMUM_CLAUDE_CODE_VERSION, '2.1.280')
        self.assertLess(
            cc_runtime.version_tuple('2.1.280'),
            cc_runtime.version_tuple('2.1.281'),
        )

    def test_missing_active_fails_closed(self):
        with self.assertRaises(cc_runtime.ClaudeRuntimeError):
            cc_runtime.active_claude_version()

    def test_invalid_active_version_fails_closed(self):
        self._active('../2.1.280')
        with self.assertRaises(cc_runtime.ClaudeRuntimeError):
            cc_runtime.active_claude_version()

    def test_active_binary_must_exist_under_native_versions(self):
        self._active()
        with self.assertRaises(cc_runtime.ClaudeRuntimeError):
            cc_runtime.active_claude_binary()

    def test_active_binary_is_exact_native_path_and_never_path_fallback(self):
        binary = self._native('2.1.281')
        self._active('2.1.281')
        self.assertEqual(cc_runtime.active_claude_binary(), binary.resolve())
        self.assertEqual(cc_runtime.claude_cmd('--help'), [str(binary.resolve()), '--help'])
        self.assertNotEqual(cc_runtime.claude_argv_prefix(), ['claude'])

    def test_below_minimum_fails_closed(self):
        self._native('2.1.220')
        self._active('2.1.220')
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.220'):
            with self.assertRaises(cc_runtime.ClaudeRuntimeError):
                cc_runtime.require_managed_claude_runtime()

    def test_active_version_must_match_binary_probe(self):
        self._native('2.1.281')
        self._active('2.1.281')
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.282'):
            with self.assertRaisesRegex(cc_runtime.ClaudeRuntimeError, 'mismatch'):
                cc_runtime.require_managed_claude_runtime()

    def test_any_verified_version_above_floor_is_accepted(self):
        self._native('2.1.281')
        self._active('2.1.281')
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.281'):
            self.assertEqual(cc_runtime.require_managed_claude_runtime(), '2.1.281')
            self.assertEqual(cc_runtime.claude_runtime_identity(), 'claude-code:2.1.281')

    def test_parse_version_text(self):
        self.assertEqual(
            cc_runtime.parse_claude_version_text('2.1.281 (Claude Code)'),
            '2.1.281',
        )
        with self.assertRaises(cc_runtime.ClaudeRuntimeError):
            cc_runtime.parse_claude_version_text('not a Claude version')

    def test_version_specific_command_cannot_fall_back_to_launcher(self):
        binary = self._native('2.1.281')
        self.assertEqual(
            cc_runtime.claude_cmd_for_version('2.1.281', '-p', env={'HOME': str(self.home)}),
            [str(binary.resolve()), '-p'],
        )

    def test_state_write_is_atomic_and_private(self):
        claude_runtime_state.write_version('active-version', '2.1.280')
        target = self.state / 'active-version'
        self.assertEqual(target.read_text(encoding='ascii'), '2.1.280\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_promotion_records_last_good_and_clears_candidate(self):
        claude_runtime_state.write_version('active-version', '2.1.280')
        result = claude_runtime_state.promote_candidate(
            '2.1.281',
            channel='latest',
            checked_at='2026-09-23T00:00:00Z',
            promoted_at='2026-09-23T00:01:00Z',
        )
        self.assertEqual(result['from'], '2.1.280')
        self.assertEqual(cc_runtime.active_claude_version(), '2.1.281')
        self.assertEqual(claude_runtime_state.read_version('last-good-version'), '2.1.280')
        self.assertIsNone(claude_runtime_state.read_version('candidate-version'))
        receipt = claude_runtime_state.read_public_update_state()
        self.assertEqual(receipt['canary'], 'pass')
        self.assertEqual(receipt['status'], 'healthy')

    def test_promotion_rejects_below_floor_and_invalid_channel(self):
        claude_runtime_state.write_version('active-version', '2.1.280')
        with self.assertRaises(ValueError):
            claude_runtime_state.promote_candidate('2.1.279', channel='latest')
        with self.assertRaises(ValueError):
            claude_runtime_state.promote_candidate('2.1.281', channel='preview')

    def test_public_runtime_state_never_includes_credentials_or_home(self):
        self._native('2.1.280')
        self._active()
        with mock.patch.object(cc_runtime, 'runtime_status_dict', return_value={
            'active_version': '2.1.280', 'status': 'healthy', 'binary_exists': True,
        }):
            public = claude_runtime_state.runtime_public_status(auto_update=True, channel='latest')
        serialized = str(public).lower()
        self.assertNotIn('oauth', serialized)
        self.assertNotIn('token', serialized)
        self.assertNotIn('credential', serialized)
        self.assertNotIn(str(self.home).lower(), serialized)


if __name__ == '__main__':
    unittest.main()
