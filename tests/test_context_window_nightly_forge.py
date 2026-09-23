"""Focused Nightly Forge Canary tests — isolation + wiring only (no live PASS claim)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import context_window_nightly_forge as nf
from chat.context_window_fallback import AUTH_PROVIDER_OVERRIDE_VARS, ENVIRONMENT_BLOCKED
from chat.daily_context import DEFAULT_DB_PATH


class NightlyForgeFocusedTests(unittest.TestCase):
    def test_01_to_06_structural_isolation_and_formal_hooks(self):
        """Items 1-6,10,11: isolation + Transform/Validator + cleanup; not live PASS."""
        transform_calls = []
        validate_calls = []

        def _wrap_transform(graph, request):
            transform_calls.append((graph, request))
            return nf.transform_transcript(graph, request)

        def _wrap_validate(events, options=None, **kwargs):
            validate_calls.append((events, options))
            return nf.validate_transcript_events(events, options=options, **kwargs)

        with mock.patch.object(nf, '_transform_impl', side_effect=_wrap_transform), \
             mock.patch.object(nf, '_validate_impl', side_effect=_wrap_validate), \
             mock.patch('chat.session_registry.register_context_claude_session') as reg, \
             mock.patch('cc_resident.ResidentSession') as resident_cls:
            report = nf.run_nightly_forge_canary(
                confirm_live=True, structural_only=True,
            )

        self.assertTrue(report.get('ok'), report)
        self.assertTrue(report['STRUCTURAL_CANARY_ONLY'])
        self.assertFalse(Path(report['temp_root']).exists())  # item 10 cleanup
        self.assertTrue(report['cleanup']['ok'])

        # 1: temp root not in repo /opt/frontend (created under /tmp)
        self.assertTrue(str(report['temp_root']).startswith('/tmp/'))
        self.assertTrue(report['isolation']['temp_outside_repo'])
        self.assertTrue(report['isolation']['temp_outside_frontend'])

        # 2: HOME + CLAUDE_CONFIG_DIR under temp
        self.assertTrue(report['isolation']['home_under_temp'])
        self.assertTrue(report['isolation']['config_dir_under_temp'])
        self.assertEqual(report['paths']['HOME'], report['paths']['home'])
        self.assertEqual(
            report['paths']['CLAUDE_CONFIG_DIR'], report['paths']['claude_home'],
        )

        # 3: production auth overlays cleared
        for name in AUTH_PROVIDER_OVERRIDE_VARS:
            self.assertTrue(
                report['env_overlays_cleared'][name], name,
            )

        # 4-5: formal Transform / Validator called
        self.assertTrue(report['forge']['transform_called'])
        self.assertTrue(report['forge']['validator_called'])
        self.assertEqual(len(transform_calls), 1)
        self.assertEqual(len(validate_calls), 1)

        # 6: production Registry / resident not used
        self.assertFalse(reg.called)
        self.assertFalse(resident_cls.called)
        self.assertNotEqual(
            Path(report['paths'].get('db', '/tmp/x')).resolve()
            if report['paths'].get('db') else Path('/tmp/x'),
            Path(DEFAULT_DB_PATH).resolve(),
        )

        # 11: structural must never claim live PASS
        self.assertTrue(report['live_resume'].get('STRUCTURAL_ONLY'))
        self.assertFalse(report['live_resume'].get('attempted'))
        self.assertNotEqual(report['live_resume'].get('ok'), True)

    def test_07_auth_failure_skips_live_resume(self):
        """Item 7: auth fail ⇒ no live resume."""
        with mock.patch.object(nf, 'check_claude_pin', return_value={
            'ok': True, 'version': '2.1.280', 'version_raw': '2.1.280 (Claude Code)', 'minimum_version': '2.1.280', 'reason': '',
        }), mock.patch.object(
            nf, 'probe_nightly_auth', return_value=(False, 'isolated_auth_not_logged_in'),
        ), mock.patch.object(
            nf, '_create_native_session',
            side_effect=AssertionError('native create must not run'),
        ), mock.patch.object(
            nf, '_resume_forged_session',
            side_effect=AssertionError('resume must not run'),
        ):
            report = nf.run_nightly_forge_canary(
                confirm_live=True, structural_only=False, login_if_needed=False,
            )
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), ENVIRONMENT_BLOCKED)
        self.assertFalse(report['live_resume']['attempted'])
        self.assertTrue(report['cleanup']['ok'])
        self.assertFalse(Path(report['temp_root']).exists())

    def test_08_model_not_started_is_environment_blocked(self):
        """Item 8: process never started ⇒ ENVIRONMENT_BLOCKED."""
        with mock.patch.object(nf, 'check_claude_pin', return_value={
            'ok': True, 'version': '2.1.280', 'version_raw': '2.1.280 (Claude Code)', 'minimum_version': '2.1.280', 'reason': '',
        }), mock.patch.object(
            nf, 'probe_nightly_auth',
            return_value=(True, 'ISOLATED_CLAUDE_APP_SUBSCRIPTION'),
        ), mock.patch.object(
            nf, '_create_native_session',
            return_value={
                'ok': False,
                'error_code': ENVIRONMENT_BLOCKED,
                'process_started': False,
                'reason': 'native_create_process_not_started',
            },
        ):
            report = nf.run_nightly_forge_canary(
                confirm_live=True, structural_only=False,
            )
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), ENVIRONMENT_BLOCKED)
        self.assertFalse(report['source_native_session']['process_started'])

    def test_09_model_started_then_fail_is_fail(self):
        """Item 9: process started but resume fails ⇒ FAIL."""
        forged_sid = '11111111-1111-1111-1111-111111111111'

        def _fake_forge(**kwargs):
            path = Path(kwargs['claude_home']) / 'projects' / 'x' / f'{forged_sid}.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"type":"user","uuid":"u"}\n', encoding='utf-8')
            before = path.read_bytes()
            return {
                'ok': True,
                'transform_called': True,
                'validator_called': True,
                'forged_session_id': forged_sid,
                'forged_jsonl_path': str(path),
                'selected_round_count': 1,
                'event_count': 1,
                'before_byte_len': len(before),
                '_before_bytes': before,
            }

        with mock.patch.object(nf, 'check_claude_pin', return_value={
            'ok': True, 'version': '2.1.280', 'version_raw': '2.1.280 (Claude Code)', 'minimum_version': '2.1.280', 'reason': '',
        }), mock.patch.object(
            nf, 'probe_nightly_auth',
            return_value=(True, 'ISOLATED_CLAUDE_APP_SUBSCRIPTION'),
        ), mock.patch.object(
            nf, '_create_native_session',
            return_value={
                'ok': True,
                'process_started': True,
                'session_id': 'native',
                'jsonl_path': '/tmp/does-not-matter.jsonl',
            },
        ), mock.patch.object(
            nf, '_forge_from_native_jsonl', side_effect=_fake_forge,
        ), mock.patch.object(
            nf, '_resume_forged_session',
            return_value={
                'ok': False,
                'error_code': 'FAIL',
                'process_started': True,
                'saw_text_delta': False,
                'result_ok': False,
                'result_is_error': True,
                'jsonl_prefix_unchanged': True,
                'jsonl_grew': False,
                'appended_user': False,
                'appended_assistant': False,
                'failures': ['missing_text_delta'],
            },
        ):
            report = nf.run_nightly_forge_canary(
                confirm_live=True, structural_only=False,
            )
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), 'FAIL')
        self.assertTrue(report['live_resume']['process_started'])
        self.assertTrue(report['cleanup']['ok'])

    def test_flag_untouched(self):
        self.assertNotIn('DAILY_SOFT_WINDOW_ENABLED', os.environ)


if __name__ == '__main__':
    unittest.main()
