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



class ClaudeRuntimeLifecycleTests(unittest.TestCase):
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
        binary.write_text('#!/bin/sh\\necho "%s (Claude Code)"\\n' % version, encoding='utf-8')
        binary.chmod(0o755)
        return binary

    def _active(self, version='2.1.280'):
        claude_runtime_state.write_version('active-version', version)

    def test_newer_downloaded_candidate_is_selected_and_rejected_version_is_skipped(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.280')
        for version in ('2.1.280', '2.1.281', '2.1.282'):
            self._native(version)
        claude_runtime_state.reject_version('2.1.282', 'startup_canary_failed')
        with mock.patch.object(cc_runtime, 'probe_claude_version', side_effect=lambda path, **kw: Path(path).name):
            self.assertEqual(updater.discover_downloaded_candidate(home=self.home), '2.1.281')

    def test_download_discovery_never_downgrades_or_reselects_active(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.281')
        for version in ('2.1.280', '2.1.281'):
            self._native(version)
        with mock.patch.object(cc_runtime, 'probe_claude_version', side_effect=lambda path, **kw: Path(path).name):
            self.assertIsNone(updater.discover_downloaded_candidate(home=self.home))

    def test_manual_check_preserves_quarantine_without_explicit_retry(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.280')
        self._native('2.1.281')
        claude_runtime_state.write_version('candidate-version', '2.1.281')
        claude_runtime_state.reject_version('2.1.281', 'startup_canary_failed')
        claude_runtime_state.write_update_state({
            'status': 'rejected',
            'to': '2.1.281',
            'canary': 'fail',
            'last_error': 'startup_canary_failed',
        })
        with mock.patch.object(updater, '_prefs', return_value=(True, 'latest')), \\
             mock.patch.object(updater, 'sync_native_update_settings'), \\
             mock.patch.object(updater, '_native_updater', return_value=self.home / '.local/bin/claude'), \\
             mock.patch.object(updater.subprocess, 'run', return_value=mock.Mock(returncode=0)), \\
             mock.patch.object(cc_runtime, 'probe_claude_version', side_effect=lambda path, **kw: Path(path).name), \\
             mock.patch.object(updater, 'canary_candidate') as canary:
            self.assertEqual(updater.run_update_check(force=True), 'candidate_rejected_previously')
        self.assertEqual(claude_runtime_state.read_version('candidate-version'), '2.1.281')
        self.assertIn('2.1.281', claude_runtime_state.read_rejected_versions())
        canary.assert_not_called()

    def test_cli_retry_clears_quarantine_only_after_promotion(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.280')
        self._native('2.1.281')
        claude_runtime_state.reject_version('2.1.281', 'startup_canary_failed')
        with mock.patch.object(updater, '_prefs', return_value=(True, 'latest')), \\
             mock.patch.object(updater, 'sync_native_update_settings'), \\
             mock.patch.object(updater, '_native_updater', return_value=self.home / '.local/bin/claude'), \\
             mock.patch.object(updater.subprocess, 'run', return_value=mock.Mock(returncode=0)), \\
             mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.281'), \\
             mock.patch.object(updater, 'canary_candidate', return_value=True):
            self.assertEqual(
                updater.run_update_check(retry_rejected='2.1.281'),
                'promoted',
            )
        self.assertEqual(cc_runtime.active_claude_version(), '2.1.281')
        self.assertEqual(claude_runtime_state.read_version('last-good-version'), '2.1.280')
        self.assertNotIn('2.1.281', claude_runtime_state.read_rejected_versions())

    def test_candidate_canary_runs_only_metadata_and_no_input_surface(self):
        from tools import claude_runtime_updater as updater

        self._native('2.1.281')
        with mock.patch.object(cc_runtime, 'probe_claude_version', return_value='2.1.281'), \\
             mock.patch.object(updater, '_run_candidate_command') as command, \\
             mock.patch.object(updater, 'candidate_surface_canary', return_value=True) as surface:
            self.assertTrue(updater.canary_candidate('2.1.281', env={'HOME': str(self.home)}))
        self.assertEqual([call.args[1] for call in command.call_args_list], [['--help'], ['doctor']])
        self.assertEqual(surface.call_args.args[0], self.versions / '2.1.281')
        self.assertNotIn('hello', str(command.call_args_list).lower())

    def test_failed_candidate_canary_is_quarantined_without_promotion(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.280')
        self._native('2.1.281')
        with mock.patch.object(updater, '_prefs', return_value=(True, 'latest')), \\
             mock.patch.object(updater, 'sync_native_update_settings'), \\
             mock.patch.object(updater, '_native_updater', return_value=self.home / '.local/bin/claude'), \\
             mock.patch.object(updater.subprocess, 'run', return_value=mock.Mock(returncode=0)), \\
             mock.patch.object(updater, 'discover_downloaded_candidate', return_value='2.1.281'), \\
             mock.patch.object(updater, 'canary_candidate', return_value=False):
            self.assertEqual(updater.run_update_check(), 'candidate_rejected')
        self.assertEqual(cc_runtime.active_claude_version(), '2.1.280')
        self.assertEqual(claude_runtime_state.read_version('candidate-version'), '2.1.281')
        self.assertEqual(
            claude_runtime_state.read_rejected_versions()['2.1.281']['reason'],
            'startup_canary_failed',
        )
        self.assertEqual(claude_runtime_state.read_public_update_state()['status'], 'rejected')

    def test_candidate_canary_pass_promotes_and_preserves_last_good(self):
        from tools import claude_runtime_updater as updater

        self._active('2.1.280')
        self._native('2.1.281')
        with mock.patch.object(updater, '_prefs', return_value=(True, 'latest')), \\
             mock.patch.object(updater, 'sync_native_update_settings'), \\
             mock.patch.object(updater, '_native_updater', return_value=self.home / '.local/bin/claude'), \\
             mock.patch.object(updater.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \\
             mock.patch.object(updater, 'discover_downloaded_candidate', return_value='2.1.281'), \\
             mock.patch.object(updater, 'canary_candidate', return_value=True):
            self.assertEqual(updater.run_update_check(), 'promoted')
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], 'update')
        self.assertIs(run.call_args.kwargs['stdin'], updater.subprocess.DEVNULL)
        self.assertEqual(cc_runtime.active_claude_version(), '2.1.281')
        self.assertEqual(claude_runtime_state.read_version('last-good-version'), '2.1.280')
        self.assertIsNone(claude_runtime_state.read_version('candidate-version'))

    def test_verified_last_good_rollback_quarantines_bad_active(self):
        import contextlib
        from tools import claude_runtime_updater as updater

        self._active('2.1.281')
        claude_runtime_state.write_version('last-good-version', '2.1.280')
        self._native('2.1.280')
        self._native('2.1.281')
        with mock.patch.object(updater, 'update_locks', return_value=contextlib.nullcontext(True)), \\
             mock.patch.object(cc_runtime, 'probe_claude_version', side_effect=lambda path, **kw: Path(path).name):
            result = updater.rollback_active_runtime(
                expected_active='2.1.281',
                reason='provider_failure_after_stdin',
            )
        self.assertEqual(result, '2.1.280')
        self.assertEqual(cc_runtime.active_claude_version(), '2.1.280')
        self.assertEqual(
            claude_runtime_state.read_rejected_versions()['2.1.281']['reason'],
            'provider_failure_after_stdin',
        )

    def test_runtime_identity_change_is_a_respawn_reason(self):
        import cc_resident

        session = cc_resident.ResidentSession('/tmp', '', '/tmp/mcp.json')
        session._proc = mock.Mock()
        session._proc.poll.return_value = None
        session._runtime_identity = 'claude-code:2.1.280'
        session._model_identity = None
        session._effort_identity = None
        with mock.patch('chat.cc_history_rewrite.current_history_rewrite_epoch', return_value=''), \\
             mock.patch('chat.cc_history_rewrite.is_unreadable_epoch', return_value=False), \\
             mock.patch('chat.cc_history_rewrite.sanitize_bound_epoch', side_effect=lambda value: value), \\
             mock.patch('chat.cc_model.cc_model_identity', return_value=None), \\
             mock.patch('chat.cc_effort.cc_effort_identity', return_value=None), \\
             mock.patch('chat.cc_runtime.active_claude_version', return_value='2.1.281'):
            self.assertEqual(session._decide_respawn_reason('sys'), 'runtime_changed')

    def test_pre_stdin_startup_failure_rolls_back_and_spawns_last_good_once(self):
        import cc_resident
        from tools import claude_runtime_updater as updater

        session = cc_resident.ResidentSession('/tmp', '', '/tmp/mcp.json')
        calls = []

        def spawn(_system, _env, *, reason, tool_profile):
            calls.append(reason)
            if len(calls) == 1:
                raise cc_resident.ResidentError(
                    'Claude Code 启动失败',
                    error_code='claude_runtime_startup_failed',
                )
            session._runtime_identity = 'claude-code:2.1.280'

        with mock.patch.object(session, '_decide_respawn_reason', return_value='runtime_changed'), \\
             mock.patch.object(session, '_alive', return_value=False), \\
             mock.patch.object(session, '_spawn', side_effect=spawn), \\
             mock.patch('chat.cc_runtime.active_claude_version', return_value='2.1.281'), \\
             mock.patch.object(updater, 'rollback_active_runtime', return_value='2.1.280') as rollback:
            session.ensure_alive('sys', {'HOME': str(self.home)})
        self.assertEqual(calls, ['runtime_changed', 'runtime_changed'])
        rollback.assert_called_once_with(
            expected_active='2.1.281',
            reason='runtime_startup_failed',
        )
        self.assertFalse(session._last_turn_stdin_flushed)

    def test_post_stdin_runtime_failure_never_replays_turn(self):
        import cc_resident
        from tools import claude_runtime_updater as updater

        session = cc_resident.ResidentSession('/tmp', '', '/tmp/mcp.json')
        session._runtime_identity = 'claude-code:2.1.281'
        attempts = []

        def fail_after_flush(*args, **kwargs):
            attempts.append(1)
            session._turn_write_started = True
            session._turn_stdin_flushed = True
            raise cc_resident.ResidentError(
                'provider failed',
                error_code='provider_error',
            )

        session._send_turn_impl = fail_after_flush
        with mock.patch.object(updater, 'rollback_active_runtime', return_value='2.1.280'), \\
             mock.patch.object(session, '_kill'):
            with self.assertRaises(cc_resident.ResidentError) as raised:
                list(session.send_turn('same user turn'))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(raised.exception.error_code, 'claude_runtime_post_send_failure')
        self.assertIn('未自动重试', str(raised.exception))
        self.assertTrue(session._last_turn_stdin_flushed)
        self.assertEqual(session._runtime_rollback_pending, '2.1.281')
        with mock.patch('chat.cc_runtime.active_claude_version', return_value='2.1.281'), \\
             mock.patch.object(updater, 'rollback_active_runtime', return_value=None), \\
             mock.patch.object(session, '_spawn') as spawn:
            with self.assertRaises(cc_resident.ResidentError) as pending:
                session.ensure_alive('system', {'HOME': str(self.home)})
            self.assertEqual(pending.exception.error_code, 'claude_runtime_rollback_pending')
            spawn.assert_not_called()

    def test_opus_55_runtime_compatibility_and_config_write_gate(self):
        from chat import cc_model

        opus = {'id': 'claude-opus-5-5', 'min_claude_code_version': '2.1.280'}
        catalog = {'models': [opus]}
        with mock.patch.object(cc_model, 'get_cc_model_catalog', return_value=catalog), \\
             mock.patch.object(cc_model, '_active_runtime_version_for_catalog', return_value='2.1.220'), \\
             mock.patch.object(cc_model.config_store, 'set') as write:
            result = cc_model.set_cc_chat_model('claude-opus-5-5')
            self.assertEqual(result['error'], cc_model.CC_MODEL_RUNTIME_INCOMPATIBLE)
            write.assert_not_called()
        with mock.patch.object(cc_model, 'get_cc_model_catalog', return_value=catalog), \\
             mock.patch.object(cc_model, '_active_runtime_version_for_catalog', return_value='2.1.280'):
            self.assertEqual(cc_model.cc_model_runtime_compatibility('claude-opus-5-5')[0], True)


class ClaudeRuntimeArtifactContractTests(unittest.TestCase):
    def test_deploy_requires_managed_runtime_without_exact_npm_runtime_install(self):
        deploy = (Path(ROOT) / 'scripts' / 'deploy-frontend.sh').read_text(encoding='utf-8')
        ensure = (Path(ROOT) / 'scripts' / 'ensure-claude-runtime.sh').read_text(encoding='utf-8')
        self.assertIn('require_managed_claude_runtime', ensure)
        self.assertIn('active_claude_binary', ensure)
        self.assertIn('scripts/ensure-claude-runtime.sh', deploy)
        self.assertIn('require_managed_claude_runtime', deploy)
        self.assertNotIn('@anthropic-ai/claude-code@', deploy + ensure)
        self.assertNotIn('npm install', ensure)

    def test_usage_observation_records_resident_runtime_identity(self):
        from tools import cc_usage_observability

        runtime = cc_usage_observability.build_runtime(
            resident_generation=3,
            resident_pid=123,
            resident_turn_count=2,
            respawn_reason='runtime_changed',
            idle_seconds_before_turn=0,
            is_cold=True,
            static_system='system',
            runtime_identity='claude-code:2.1.281',
            claude_code_version='2.1.281',
        )
        self.assertEqual(runtime['runtime_identity'], 'claude-code:2.1.281')
        self.assertEqual(runtime['claude_code_version'], '2.1.281')
        self.assertEqual(runtime['respawn_reason'], 'runtime_changed')


if __name__ == '__main__':
    unittest.main()
