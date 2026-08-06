"""B2-1｜Planner none takeover + deterministic Action Gate — minimum cases."""

from __future__ import annotations

import datetime
import json
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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

import drive_engine as de
from chat import drive_authority as da
from chat.behavior_authority_b2 import (
    consumer_enabled,
    evaluate_action_gate,
    freeze_provenance_from_planner_decision,
    plan_b2_wake_action,
)
from chat.capability_skill_view import freeze_capability_skill_view
from chat.planner_state_view import freeze_planner_state_view
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


class BehaviorAuthorityB21Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        os.environ['MEMORIES_DB'] = self.db_path
        de.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            DROP TABLE IF EXISTS wake_log;
            DROP TABLE IF EXISTS posts;
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thoughts TEXT, action TEXT, content TEXT, consumed INTEGER,
                woke_at TEXT, wake_run_id TEXT, notified INTEGER,
                chat_id TEXT, context_id INTEGER, context_epoch INTEGER,
                resident_generation INTEGER, cache_info TEXT,
                surfaced_desire_ids TEXT
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, content TEXT, layer TEXT, author TEXT, processed INTEGER
            );
            """
        )
        cols = {r[1] for r in conn.execute('PRAGMA table_info(chat_messages)')}
        for col, typ in (
            ('thinking', 'TEXT'),
            ('cache_info', 'TEXT'),
            ('source_kind', 'TEXT'),
        ):
            if col not in cols:
                conn.execute(f'ALTER TABLE chat_messages ADD COLUMN {col} {typ}')
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 12:00:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.commit()
        conn.close()
        self.assertTrue(da.ensure_authority_ready(self.db_path))
        self.view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b21-run-1',
        )
        self.skill = freeze_capability_skill_view(
            wake_run_id='b21-run-1',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        self._get_db = self._open_db

    def _open_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def tearDown(self):
        self.tmp.cleanup()

    def _none_invoke_for(self, wake_run_id: str):
        def _invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'rest_quietly',
                'action_candidate': 'none',
                'confidence': 0.81,
                'primary_drive': None,
                'contributors': ['fatigue'],
                'blocked': False,
                'reason_codes': ['quiet_contentment'],
                'wake_run_id': wake_run_id,
                'state_version': int(self.view.state_version),
                'source': 'planner_authority',
                'shadow_only': False,
                'authoritative': True,
            })
        return _invoke

    def _message_invoke_for(self, wake_run_id: str):
        def _invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'reconnect_gently',
                'action_candidate': 'message',
                'confidence': 0.72,
                'primary_drive': 'attachment',
                'contributors': ['longing', 'bond.passion'],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': wake_run_id,
                'state_version': int(self.view.state_version),
            })
        return _invoke

    def test_consumer_default_off(self):
        self.assertFalse(consumer_enabled())

    def test_case1_planner_none_presses_legacy(self):
        """Case 1: Planner none + Gate ALLOW → none_takeover; legacy runner not on path."""
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=self.view,
                skill_view=self.skill,
                wake_run_id='b21-run-1',
                decision_attempt_id='da-b21-1',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._none_invoke_for('b21-run-1'),
            )
        self.assertEqual(plan.route, 'none_takeover')
        self.assertEqual(plan.gate_reason, 'ok')
        self.assertEqual(
            plan.planner_decision.get('action_candidate'), 'none',
        )
        self.assertTrue(plan.planner_decision.get('authoritative'))
        self.assertFalse(plan.planner_decision.get('shadow_only'))

        # Non-owned planner action still routes legacy (legacy would run).
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ):
            legacy_plan = plan_b2_wake_action(
                planner_view=self.view,
                skill_view=self.skill,
                wake_run_id='b21-run-1',
                decision_attempt_id='da-b21-legacy',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for('b21-run-1'),
            )
        self.assertEqual(legacy_plan.route, 'legacy')

        locked = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        self.assertIn("route == 'none_takeover'", locked)
        self.assertIn("route == 'blocked'", locked)
        self.assertLess(
            locked.index("route == 'none_takeover'"),
            locked.index('get_wake_runner'),
        )
        self.assertLess(
            locked.index("route == 'blocked'"),
            locked.index('get_wake_runner'),
        )
        self.assertNotIn('b2_gate_blocked', locked.split('get_wake_runner')[1])

    def test_case2_none_allow_settlement_with_planner_provenance(self):
        """Case 2: owned none ALLOW → executor + V3 Settlement with Planner provenance."""
        view2 = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b21-run-2',
        )
        skill2 = freeze_capability_skill_view(
            wake_run_id='b21-run-2',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=view2,
                skill_view=skill2,
                wake_run_id='b21-run-2',
                decision_attempt_id='da-b21-2',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._none_invoke_for('b21-run-2'),
            )
        self.assertEqual(plan.route, 'none_takeover')
        provenance = plan.planner_provenance or {}
        self.assertEqual(provenance.get('source'), 'planner_authority')
        self.assertEqual(provenance.get('suggested_action'), 'none')
        self.assertIn('planner_decision_id', provenance)

        conn = sqlite3.connect(self.db_path)
        before_fat = float(
            conn.execute(
                'SELECT fatigue FROM internal_state_v3 WHERE id=1',
            ).fetchone()[0],
        )
        conn.close()

        from wake.executor import execute

        out = execute(
            'none',
            str(plan.planner_decision.get('intent') or ''),
            '',
            'normal',
            self._get_db,
            wake_run_id='b21-run-2',
            settle_fired_drive=provenance.get('primary_drive'),
            settle_provenance_present=True,
            settle_user_idle_hours=0.5,
            settle_outcome_at='2026-08-04 15:05:00',
            cache_info={'b2_authority': True, 'source': 'wake'},
        )
        self.assertTrue(out['delivered'])
        self.assertTrue(out['settled'])
        self.assertEqual(out['settle_status'], 'applied')

        conn = sqlite3.connect(self.db_path)
        after_fat = float(
            conn.execute(
                'SELECT fatigue FROM internal_state_v3 WHERE id=1',
            ).fetchone()[0],
        )
        self.assertLess(after_fat, before_fat)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b21-run-2'",
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b21-run-2'",
            ).fetchone()[0],
            1,
        )
        conn.close()

        frozen = freeze_provenance_from_planner_decision(plan.planner_decision)
        self.assertEqual(frozen['source'], 'planner_authority')

    def test_case3_gate_block_user_active_no_executor_no_legacy(self):
        """Case 3: Gate BLOCK (user_active) → blocked route; no settlement path."""
        decision = {
            'action_candidate': 'none',
            'intent': 'wait',
            'primary_drive': None,
            'contributors': ['fatigue'],
            'captured_at': '2026-08-04 15:00:00',
        }
        verdict, reason = evaluate_action_gate(
            planner_decision=decision,
            skill_view=self.skill,
            wake_run_id='b21-run-3',
            get_db_fn=self._get_db,
            now=T_OBS,
            chat_busy=True,
        )
        self.assertEqual(verdict, 'BLOCK')
        self.assertEqual(reason, 'user_active')

        view3 = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b21-run-3',
        )
        skill3 = freeze_capability_skill_view(
            wake_run_id='b21-run-3',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=view3,
                skill_view=skill3,
                wake_run_id='b21-run-3',
                decision_attempt_id='da-b21-3',
                get_db_fn=self._get_db,
                now=T_OBS,
                chat_busy_fn=lambda: True,
                invoke_fn=self._none_invoke_for('b21-run-3'),
            )
        self.assertEqual(plan.route, 'blocked')
        self.assertEqual(plan.gate_reason, 'user_active')

        conn = sqlite3.connect(self.db_path)
        before_ver = int(
            conn.execute(
                'SELECT state_version FROM internal_state_v3 WHERE id=1',
            ).fetchone()[0],
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b21-run-3'",
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b21-run-3'",
            ).fetchone()[0],
            0,
        )
        conn.close()
        conn = sqlite3.connect(self.db_path)
        after_ver = int(
            conn.execute(
                'SELECT state_version FROM internal_state_v3 WHERE id=1',
            ).fetchone()[0],
        )
        conn.close()
        self.assertEqual(before_ver, after_ver)


if __name__ == '__main__':
    unittest.main()
