"""Owner Canary isolation — structural + live auth preflight wiring."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import context_window_fallback as fb
from chat.daily_context import DEFAULT_DB_PATH
from chat.context_window_fallback import (
    ENVIRONMENT_BLOCKED,
    FallbackError,
    ISOLATED_SUBSCRIPTION_AUTH_SOURCE,
    probe_isolated_subscription_auth,
)
from tools import context_window_admin as admin
from tools.context_window_admin import run_owner_canary, _assert_isolated_temp_root


def _auth_proc(payload: dict, *, returncode: int = 0):
    return mock.Mock(returncode=returncode, stdout=json.dumps(payload), stderr='')


class OwnerCanaryIsolationTests(unittest.TestCase):
    def test_C_structural_owner_canary_isolation_and_cleanup(self):
        """Path C: success recover + reject path; temp-only; cleanup removes root."""
        prod_db = Path(DEFAULT_DB_PATH).resolve()
        prod_db_meta = None
        if prod_db.exists():
            st = prod_db.stat()
            prod_db_meta = (st.st_mtime_ns, st.st_size, st.st_ino)

        report = run_owner_canary(confirm_live=True, structural_only=True)
        self.assertTrue(report.get('ok'), report)
        self.assertTrue(report['success_path']['ok'])
        self.assertTrue(report['reject_path']['ok'])
        self.assertFalse(report['success_path']['carryover_contains_failed_user'])
        self.assertEqual(report['success_path']['failed_user_assistant_count'], 0)
        self.assertTrue(report['cleanup']['ok'])
        self.assertFalse(Path(report['temp_root']).exists())
        self.assertIsNone(report.get('auth_preflight'))
        self.assertIsNone(report.get('auth_login'))
        self.assertIsNone(report.get('live_turn'))

        self.assertNotIn('DAILY_SOFT_WINDOW_ENABLED', os.environ)

        if prod_db_meta is not None and prod_db.exists():
            st2 = prod_db.stat()
            self.assertEqual(
                (st2.st_mtime_ns, st2.st_size, st2.st_ino), prod_db_meta,
            )

        self.assertNotEqual(
            Path(report['paths']['db']).resolve(), prod_db,
        )

    def test_isolation_rejects_repo_temp_root(self):
        inside = Path(tempfile.mkdtemp(prefix='bad-', dir=str(ROOT)))
        try:
            with self.assertRaises(FallbackError) as ar:
                _assert_isolated_temp_root(inside)
            self.assertEqual(ar.exception.error_code, 'ISOLATION_ESCAPE')
        finally:
            import shutil
            shutil.rmtree(inside, ignore_errors=True)

    def test_confirm_live_required_without_structural(self):
        report = run_owner_canary(confirm_live=False, structural_only=False)
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), 'CONFIRM_LIVE_REQUIRED')

    def test_auth_preflight_probe_not_logged_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'claude-home'
            cwd = Path(tmp) / 'cwd'
            home.mkdir()
            cwd.mkdir()
            with mock.patch.object(
                fb, '_auth_status_runner',
                return_value=_auth_proc({'loggedIn': False, 'authMethod': 'none'}, returncode=1),
            ):
                ok, reason = probe_isolated_subscription_auth(home, cwd)
            self.assertFalse(ok)
            self.assertEqual(reason, 'isolated_auth_not_logged_in')

    def test_auth_preflight_probe_accepts_pro(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'claude-home'
            cwd = Path(tmp) / 'cwd'
            home.mkdir()
            cwd.mkdir()
            with mock.patch.object(
                fb, '_auth_status_runner',
                return_value=_auth_proc(
                    {'loggedIn': True, 'subscriptionType': 'pro'},
                ),
            ):
                ok, reason = probe_isolated_subscription_auth(home, cwd)
            self.assertTrue(ok)
            self.assertEqual(reason, ISOLATED_SUBSCRIPTION_AUTH_SOURCE)

    def test_confirm_live_environment_blocked_from_real_probe(self):
        """--confirm-live must call auth preflight; blocked reason is probe-derived."""
        with mock.patch.object(
            admin, 'probe_isolated_subscription_auth',
            return_value=(False, 'isolated_auth_not_logged_in'),
        ) as probe:
            with mock.patch.object(
                admin, 'run_isolated_cold_turn_after_recover',
                side_effect=AssertionError('cold turn must not run without auth'),
            ):
                report = run_owner_canary(confirm_live=True, structural_only=False)
        self.assertTrue(probe.called)
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), ENVIRONMENT_BLOCKED)
        self.assertTrue(report['live_turn']['ENVIRONMENT_BLOCKED'])
        self.assertEqual(report['live_turn']['reason'], 'isolated_auth_not_logged_in')
        self.assertFalse(report['live_turn']['attempted'])
        self.assertIsNone(report.get('auth_login'))
        self.assertTrue(report['success_path']['ok'])
        self.assertTrue(report['reject_path']['ok'])
        self.assertTrue(report['cleanup']['ok'])
        self.assertNotIn('OWNER_CANARY_NOT_IMPLEMENTED_LIVE', report)

    def test_confirm_live_runs_cold_turn_when_auth_ok(self):
        live_ok = {
            'ok': True,
            'ENVIRONMENT_BLOCKED': False,
            'is_cold': True,
            'failed_user_resent': False,
            'failed_user_assistant_forged': False,
            'carryover_contains_failed_user': False,
        }
        with mock.patch.object(
            admin, 'probe_isolated_subscription_auth',
            return_value=(True, ISOLATED_SUBSCRIPTION_AUTH_SOURCE),
        ) as probe:
            with mock.patch.object(
                admin, 'run_isolated_cold_turn_after_recover',
                return_value=live_ok,
            ) as cold:
                report = run_owner_canary(confirm_live=True, structural_only=False)
        self.assertTrue(probe.called)
        self.assertTrue(cold.called)
        self.assertTrue(report.get('ok'), report)
        self.assertEqual(
            report['auth_preflight']['reason'], ISOLATED_SUBSCRIPTION_AUTH_SOURCE,
        )
        self.assertFalse(report['auth_preflight']['login_attempted'])
        self.assertIsNone(report.get('auth_login'))
        self.assertTrue(report['live_turn']['ok'])
        self.assertFalse(report['live_turn']['ENVIRONMENT_BLOCKED'])

    def test_confirm_live_login_reprobes_then_runs_cold_turn(self):
        """Empty temp home may login natively, then the same home must prove Pro/Max."""
        live_ok = {
            'ok': True,
            'ENVIRONMENT_BLOCKED': False,
            'is_cold': True,
            'failed_user_resent': False,
            'failed_user_assistant_forged': False,
            'carryover_contains_failed_user': False,
        }
        with mock.patch.object(
            admin, 'probe_isolated_subscription_auth',
            side_effect=[
                (False, 'isolated_auth_not_logged_in'),
                (True, ISOLATED_SUBSCRIPTION_AUTH_SOURCE),
            ],
        ) as probe:
            with mock.patch.object(
                admin, '_run_isolated_subscription_login',
                return_value={
                    'ok': True,
                    'reason': 'isolated_auth_login_completed',
                    'exit_code': 0,
                },
            ) as login:
                with mock.patch.object(
                    admin, 'run_isolated_cold_turn_after_recover',
                    return_value=live_ok,
                ) as cold:
                    report = run_owner_canary(
                        confirm_live=True,
                        structural_only=False,
                        login_if_needed=True,
                    )
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(login.call_count, 1)
        self.assertEqual(cold.call_count, 1)
        self.assertTrue(report.get('ok'), report)
        self.assertTrue(report['auth_login']['ok'])
        self.assertTrue(report['auth_preflight']['login_attempted'])
        self.assertEqual(
            report['auth_preflight']['initial_reason'],
            'isolated_auth_not_logged_in',
        )
        self.assertEqual(
            report['auth_preflight']['reason'],
            ISOLATED_SUBSCRIPTION_AUTH_SOURCE,
        )
        self.assertTrue(report['cleanup']['ok'])
        self.assertFalse(Path(report['temp_root']).exists())

    def test_confirm_live_login_failure_blocks_without_cold_turn(self):
        with mock.patch.object(
            admin, 'probe_isolated_subscription_auth',
            return_value=(False, 'isolated_auth_not_logged_in'),
        ) as probe:
            with mock.patch.object(
                admin, '_run_isolated_subscription_login',
                return_value={
                    'ok': False,
                    'reason': 'isolated_auth_login_failed',
                    'exit_code': 1,
                },
            ) as login:
                with mock.patch.object(
                    admin, 'run_isolated_cold_turn_after_recover',
                    side_effect=AssertionError('cold turn must not run after login failure'),
                ):
                    report = run_owner_canary(
                        confirm_live=True,
                        structural_only=False,
                        login_if_needed=True,
                    )
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(login.call_count, 1)
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), ENVIRONMENT_BLOCKED)
        self.assertEqual(report['live_turn']['reason'], 'isolated_auth_login_failed')
        self.assertFalse(report['live_turn']['attempted'])
        self.assertTrue(report['cleanup']['ok'])
        self.assertFalse(Path(report['temp_root']).exists())

    def test_ephemeral_login_uses_isolated_env_and_inherits_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'claude-home'
            cwd = Path(tmp) / 'cwd'
            home.mkdir()
            cwd.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    'CLAUDE_CODE_OAUTH_TOKEN': 'PROD_TOKEN_MUST_NOT_LEAK',
                    'ANTHROPIC_API_KEY': 'PROD_API_MUST_NOT_LEAK',
                    'CLAUDE_CODE_USE_BEDROCK': '1',
                },
                clear=False,
            ):
                with mock.patch.object(
                    admin, '_auth_login_runner',
                    return_value=mock.Mock(returncode=0),
                ) as runner:
                    result = admin._run_isolated_subscription_login(home, cwd)
            self.assertTrue(result['ok'])
            args, kwargs = runner.call_args
            self.assertIn('auth', args[0])
            self.assertIn('login', args[0])
            self.assertIn('--claudeai', args[0])
            env = kwargs['env']
            self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(home))
            self.assertTrue(env['HOME'].startswith(str(home.parent)))
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
            self.assertNotIn('ANTHROPIC_API_KEY', env)
            self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', env)
            self.assertNotIn('capture_output', kwargs)
            self.assertNotIn('stdin', kwargs)
            self.assertNotIn('stdout', kwargs)
            self.assertNotIn('stderr', kwargs)


if __name__ == '__main__':
    unittest.main()
