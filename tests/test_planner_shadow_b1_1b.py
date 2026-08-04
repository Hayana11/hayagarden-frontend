"""B1-1B｜CapabilitySkillView + non-blocking Planner Shadow — minimum cases."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
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

import drive_engine as de
from chat.capability_skill_view import freeze_capability_skill_view
from chat.planner_shadow import (
    dispatch_planner_shadow,
    observation_path,
    run_shadow_attempt,
)
from chat.planner_state_view import freeze_planner_state_view
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


class PlannerShadowB11BTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self.observe = str(Path(self.tmp.name) / 'planner_shadow.jsonl')
        os.environ['MEMORIES_DB'] = self.db_path
        os.environ['PLANNER_SHADOW_OBSERVE_PATH'] = self.observe
        de.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 12:00:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.commit()
        conn.close()
        self.view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11b-run-1',
        )
        self.skill = freeze_capability_skill_view(
            wake_run_id='b11b-run-1',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}, {'name': 'recall_photo'}],
            dry_run=False,
            captured_at=T_OBS,
        )

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('PLANNER_SHADOW_OBSERVE_PATH', None)

    def _read_observations(self):
        if not os.path.exists(self.observe):
            return []
        rows = []
        with open(self.observe, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def test_case1_valid_shadow_decision_observation(self):
        """Case 1: V+K → valid structured Decision → observation; no V3 write."""
        before_ver = int(self.view.state_version)

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'reconnect_gently',
                'action_candidate': 'message',
                'confidence': 0.72,
                'primary_drive': 'attachment',
                'contributors': ['longing', 'bond.passion'],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'b11b-run-1',
                'state_version': before_ver,
                'source': 'planner_shadow',
                'shadow_only': True,
            })

        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            legacy_provenance={
                'source': 'drive_engine.decide',
                'primary_drive': 'curiosity',
                'blocked': False,
            },
            invoke_fn=fake_invoke,
        )
        self.assertEqual(rec['shadow_status'], 'valid')
        decision = rec['shadow_decision']
        self.assertEqual(decision['wake_run_id'], 'b11b-run-1')
        self.assertEqual(decision['state_version'], before_ver)
        self.assertEqual(decision['source'], 'planner_shadow')
        self.assertTrue(decision['shadow_only'])
        self.assertEqual(decision['action_candidate'], 'message')
        self.assertEqual(decision['primary_drive'], 'attachment')
        rows = self._read_observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['shadow_status'], 'valid')
        self.assertFalse(rows[0]['authoritative'])
        # No V3 mutation from Shadow observation path.
        conn = sqlite3.connect(self.db_path)
        ver = conn.execute(
            'SELECT state_version FROM internal_state_v3 WHERE id=1'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(int(ver), before_ver)

    def test_case2_valid_none_is_success(self):
        """Case 2: action_candidate=none is a legal successful Shadow Decision."""

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'rest_quietly',
                'action_candidate': 'none',
                'confidence': 0.81,
                'primary_drive': None,
                'contributors': ['fatigue'],
                'blocked': False,
                'reason_codes': ['quiet_contentment'],
                'wake_run_id': 'b11b-run-1',
                'state_version': int(self.view.state_version),
            })

        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            invoke_fn=fake_invoke,
        )
        self.assertEqual(rec['shadow_status'], 'valid')
        self.assertEqual(rec['shadow_decision']['action_candidate'], 'none')

    def test_case3_shadow_failure_fail_open(self):
        """Case 3: timeout/error → observation error; no raise to production."""

        def boom(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            raise TimeoutError('shadow timed out')

        # Must not raise into production path.
        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            invoke_fn=boom,
        )
        self.assertEqual(rec['shadow_status'], 'error')
        self.assertEqual(rec['error_category'], 'TimeoutError')
        rows = self._read_observations()
        self.assertEqual(rows[-1]['shadow_status'], 'error')

        # Dispatch is non-blocking: returns before slow worker finishes.
        def slow_attempt(**kwargs):
            del kwargs
            time.sleep(1.5)
            return {'shadow_status': 'error'}

        t0 = time.time()
        with mock.patch(
            'chat.planner_shadow.run_shadow_attempt',
            side_effect=slow_attempt,
        ):
            dispatch_planner_shadow(
                planner_view=self.view,
                skill_view=self.skill,
                wake_run_id='b11b-run-1',
            )
        self.assertLess(time.time() - t0, 0.5)

    def test_capability_view_model_identity_and_tools(self):
        self.assertEqual(self.skill.provider, 'api_relay')
        self.assertEqual(self.skill.frozen_after, 'prepare_tools_for_provider')
        self.assertTrue(self.skill.immutable)
        self.assertIn('web_search', self.skill.tool_allowlist)
        self.assertIn('none', self.skill.resolved_action_capability)
        self.assertIn('message', self.skill.resolved_action_capability)
        with mock.patch(
            'chat.cc_model.cc_model_snapshot',
            return_value=(
                'claude-opus-4-6',
                'explicit:claude-opus-4-6',
                ['--model', 'claude-opus-4-6'],
            ),
        ):
            cc_skill = freeze_capability_skill_view(
                wake_run_id='b11b-run-1',
                provider='claude_code',
                mode='normal',
                prepared_tools=[{'name': 'recall_photo'}],
                dry_run=False,
                captured_at=T_OBS,
            )
        self.assertEqual(cc_skill.model_identity, 'explicit:claude-opus-4-6')
        self.assertEqual(cc_skill.provider, 'claude_code')

    def test_gateway_seat_non_blocking_order(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        locked = src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('freeze_capability_skill_view', locked)
        self.assertIn('dispatch_planner_shadow', locked)
        self.assertLess(
            locked.index('prepare_tools_for_provider'),
            locked.index('dispatch_planner_shadow'),
        )
        self.assertLess(
            locked.index('dispatch_planner_shadow'),
            locked.index('get_wake_runner'),
        )
        # Shadow must not use CC Wake resident.
        shadow_src = Path(ROOT, 'chat/planner_shadow.py').read_text(encoding='utf-8')
        self.assertIn('relay', shadow_src)
        self.assertNotIn('ClaudeCodeWakeRunner', shadow_src)
        self.assertNotIn('_CC_WAKE_RESIDENT', shadow_src)


if __name__ == '__main__':
    unittest.main()
