"""B3-1｜Message takeover + Renderer + Settlement — frozen acceptance cases."""

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
    consumer_enabled as b2_consumer_enabled,
    freeze_provenance_from_planner_decision,
    plan_b2_wake_action,
)
from chat.behavior_authority_b3 import (
    consumer_enabled as b3_consumer_enabled,
    effective_b3_enabled,
    evaluate_message_gate,
    invoke_renderer,
    validate_rendered_content,
)
from chat.capability_skill_view import freeze_capability_skill_view
from chat.planner_state_view import freeze_planner_state_view
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


class BehaviorAuthorityB31Tests(unittest.TestCase):
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
            wake_run_id='b31-run-1',
        )
        self.skill = freeze_capability_skill_view(
            wake_run_id='b31-run-1',
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

    def _message_invoke_for(
        self,
        wake_run_id: str,
        intent: str = 'reconnect_gently',
        state_version: int = 1,
    ):
        def _invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': intent,
                'action_candidate': 'message',
                'confidence': 0.72,
                'primary_drive': 'attachment',
                'contributors': ['longing', 'bond.passion'],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': wake_run_id,
                'state_version': int(state_version),
                'source': 'planner_authority',
                'shadow_only': False,
                'authoritative': True,
            })
        return _invoke

    def _none_invoke_for(self, wake_run_id: str, state_version: int = 1):
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
                'state_version': int(state_version),
                'source': 'planner_authority',
                'shadow_only': False,
                'authoritative': True,
            })
        return _invoke

    def _renderer_invoke_ok(self, text: str = '你好，想你了。'):
        def _invoke(renderer_input, timeout_sec):
            del renderer_input, timeout_sec
            return {
                'text': json.dumps({'rendered_content': text}),
                'provider': 'api_relay',
                'model_identity': 'relay:test-model',
            }
        return _invoke

    def test_flags_default_off_and_effective_truth_table(self):
        self.assertFalse(b3_consumer_enabled())
        self.assertFalse(effective_b3_enabled())
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            self.assertTrue(effective_b3_enabled())

    def test_case1_message_happy_path_and_routing(self):
        """Case 1: message ALLOW → Renderer → executor → Planner provenance Settlement."""
        view1 = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b31-run-1',
        )
        skill1 = freeze_capability_skill_view(
            wake_run_id='b31-run-1',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=view1,
                skill_view=skill1,
                wake_run_id='b31-run-1',
                decision_attempt_id='da-b31-1',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for(
                    'b31-run-1', state_version=int(view1.state_version),
                ),
            )
        self.assertEqual(plan.route, 'message_takeover')
        self.assertEqual(plan.gate_reason, 'ok')
        provenance = plan.planner_provenance or {}
        self.assertEqual(provenance.get('suggested_action'), 'message')
        self.assertEqual(provenance.get('primary_drive'), 'attachment')

        # B2=1 B3=0 → message legacy
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=False,
        ):
            legacy_plan = plan_b2_wake_action(
                planner_view=view1,
                skill_view=skill1,
                wake_run_id='b31-run-1',
                decision_attempt_id='da-b31-legacy',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for(
                    'b31-run-1', state_version=int(view1.state_version),
                ),
            )
        self.assertEqual(legacy_plan.route, 'legacy')

        # B2=0 B3=1 → fail-safe; message still legacy
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=False,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            failsafe_plan = plan_b2_wake_action(
                planner_view=view1,
                skill_view=skill1,
                wake_run_id='b31-run-1',
                decision_attempt_id='da-b31-failsafe',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for(
                    'b31-run-1', state_version=int(view1.state_version),
                ),
            )
        self.assertEqual(failsafe_plan.route, 'legacy')

        # none unchanged → none_takeover
        view_none = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b31-run-none',
        )
        skill_none = freeze_capability_skill_view(
            wake_run_id='b31-run-none',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            none_plan = plan_b2_wake_action(
                planner_view=view_none,
                skill_view=skill_none,
                wake_run_id='b31-run-none',
                decision_attempt_id='da-b31-none',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._none_invoke_for(
                    'b31-run-none', state_version=int(view_none.state_version),
                ),
            )
        self.assertEqual(none_plan.route, 'none_takeover')

        locked = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        self.assertIn("route == 'message_takeover'", locked)
        self.assertLess(
            locked.index("route == 'message_takeover'"),
            locked.index('get_wake_runner'),
        )

        from chat.behavior_authority_b3 import build_renderer_input
        renderer_input = build_renderer_input(
            planner_decision=plan.planner_decision or {},
            get_db_fn=self._get_db,
            wake_run_id='b31-run-1',
            decision_attempt_id='da-b31-1',
        )
        render_out = invoke_renderer(
            renderer_input=renderer_input,
            invoke_fn=self._renderer_invoke_ok('晚安，费佳。'),
        )
        rendered = validate_rendered_content(str(render_out.get('text') or ''))
        self.assertEqual(rendered, '晚安，费佳。')

        from wake.executor import execute

        out = execute(
            'message',
            str(plan.planner_decision.get('intent') or ''),
            rendered,
            'normal',
            self._get_db,
            wake_run_id='b31-run-1',
            settle_fired_drive=provenance.get('primary_drive'),
            settle_provenance_present=True,
            settle_user_idle_hours=3.0,
            cache_info={
                'b3_authority': True,
                'renderer_provider': render_out.get('provider'),
                'renderer_model': render_out.get('model_identity'),
            },
        )
        self.assertTrue(out['delivered'])
        self.assertTrue(out['settled'])
        self.assertEqual(out['settle_status'], 'applied')

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT content FROM chat_messages WHERE author='fyodor' "
                "ORDER BY id DESC LIMIT 1",
            ).fetchone()[0],
            '晚安，费佳。',
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b31-run-1'",
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b31-run-1'",
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_gate_user_active_and_cooldown_split(self):
        """Fresh user → user_active; recent wake message + idle user → cooldown."""
        decision = {
            'action_candidate': 'message',
            'intent': 'ping',
            'primary_drive': 'attachment',
        }
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 14:50:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.commit()
        conn.close()
        verdict, reason = evaluate_message_gate(
            planner_decision=decision,
            skill_view=self.skill,
            wake_run_id='b31-gate-ua',
            get_db_fn=self._get_db,
            now=T_OBS,
            chat_busy=False,
            min_idle_minutes=30.0,
            mode='normal',
        )
        self.assertEqual(verdict, 'BLOCK')
        self.assertEqual(reason, 'user_active')

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 12:00:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.execute(
            "INSERT INTO wake_log (thoughts, action, content, consumed, woke_at) "
            "VALUES ('hi','message','recent msg',0,'2026-08-04 14:50:00')"
        )
        conn.commit()
        conn.close()
        verdict2, reason2 = evaluate_message_gate(
            planner_decision=decision,
            skill_view=self.skill,
            wake_run_id='b31-gate-cd',
            get_db_fn=self._get_db,
            now=T_OBS,
            chat_busy=False,
            min_idle_minutes=30.0,
            mode='normal',
        )
        self.assertEqual(verdict2, 'BLOCK')
        self.assertEqual(reason2, 'cooldown')

    def test_case2_renderer_failure_terminal_stop(self):
        """Case 2: Renderer failure → no executor, no Settlement, no legacy."""
        view2 = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b31-run-2',
        )
        skill2 = freeze_capability_skill_view(
            wake_run_id='b31-run-2',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=view2,
                skill_view=skill2,
                wake_run_id='b31-run-2',
                decision_attempt_id='da-b31-2',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for(
                    'b31-run-2', state_version=int(view2.state_version),
                ),
            )
        self.assertEqual(plan.route, 'message_takeover')

        from chat.behavior_authority_b3 import build_renderer_input
        renderer_input = build_renderer_input(
            planner_decision=plan.planner_decision or {},
            get_db_fn=self._get_db,
            wake_run_id='b31-run-2',
            decision_attempt_id='da-b31-2',
        )

        def _bad_renderer(renderer_input, timeout_sec):
            del renderer_input, timeout_sec
            return {
                'text': '{"action_candidate":"diary"}',
                'provider': 'api_relay',
            }

        render_out = invoke_renderer(
            renderer_input=renderer_input,
            invoke_fn=_bad_renderer,
        )
        with self.assertRaises(ValueError):
            validate_rendered_content(str(render_out.get('text') or ''))

        # Prefix control text must not be sliced away into a fake-valid JSON.
        with self.assertRaises(ValueError):
            validate_rendered_content(
                'ACTION: diary\n'
                + json.dumps({'rendered_content': '今晚想和你说句话。'}, ensure_ascii=False),
            )

        conn = sqlite3.connect(self.db_path)
        before_ver = int(
            conn.execute(
                'SELECT state_version FROM internal_state_v3 WHERE id=1',
            ).fetchone()[0],
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b31-run-2'",
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b31-run-2'",
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

        locked = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        msg_block = locked.split("route == 'message_takeover'", 1)[1]
        self.assertIn('b3_renderer_failed', msg_block)
        self.assertLess(msg_block.index('b3_renderer_failed'), msg_block.index('get_wake_runner'))
        legacy_tail = locked.split('get_wake_runner', 1)[1]
        self.assertNotIn('b3_renderer_failed', legacy_tail)

    def test_case3_executor_failure_no_forged_success(self):
        """Case 3: Renderer ok + executor failure → no committed Settlement."""
        rendered = '测试消息正文'
        view3 = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b31-run-3',
        )
        skill3 = freeze_capability_skill_view(
            wake_run_id='b31-run-3',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled', return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.consumer_enabled', return_value=True,
        ):
            plan = plan_b2_wake_action(
                planner_view=view3,
                skill_view=skill3,
                wake_run_id='b31-run-3',
                decision_attempt_id='da-b31-3',
                get_db_fn=self._get_db,
                now=T_OBS,
                invoke_fn=self._message_invoke_for(
                    'b31-run-3', state_version=int(view3.state_version),
                ),
            )
        provenance = plan.planner_provenance or {}

        class _FailSettle:
            status = 'error'
            error = 'forced_failure'

        from wake.executor import execute

        with mock.patch(
            'chat.drive_authority.apply_wake_outcome_on_conn',
            return_value=_FailSettle(),
        ):
            with self.assertRaises(RuntimeError):
                execute(
                    'message',
                    str(plan.planner_decision.get('intent') or ''),
                    rendered,
                    'normal',
                    self._get_db,
                    wake_run_id='b31-run-3',
                    settle_fired_drive=provenance.get('primary_drive'),
                    settle_provenance_present=True,
                    settle_user_idle_hours=3.0,
                )

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b31-run-3'",
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE content=?",
                (rendered,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b31-run-3'",
            ).fetchone()[0],
            0,
        )
        conn.close()

        # Soft-window normal-return failure: delivered=False must not become
        # success reality (Rendered ≠ Delivered). Gateway branch terminal-stops.
        import chat.window_identity as wi
        from chat.planner_shadow import classify_production_outcome

        soft_identity = {
            'chat_id': 'c1',
            'context_id': 1,
            'context_epoch': 1,
            'resident_generation': 1,
        }
        with mock.patch.object(wi, 'soft_window_enabled', return_value=True), \
             mock.patch.object(
                 wi, 'gate_captured_against_conn',
                 return_value=(wi.REASON_STALE, soft_identity, None),
             ), mock.patch.object(wi, 'ensure_wake_window_identity_columns'):
            soft_out = execute(
                'message',
                str(plan.planner_decision.get('intent') or ''),
                rendered,
                'normal',
                self._get_db,
                wake_run_id='b31-run-3-soft',
                window_identity=soft_identity,
                settle_fired_drive=provenance.get('primary_drive'),
                settle_provenance_present=True,
                settle_user_idle_hours=3.0,
            )
        self.assertFalse(soft_out['delivered'])
        self.assertFalse(soft_out['settled'])
        soft_status, soft_reason = classify_production_outcome(soft_out)
        self.assertEqual(soft_status, 'failed')
        self.assertTrue(soft_reason.startswith('gate_blocked:'))

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE content=?",
                (rendered,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b31-run-3-soft'",
            ).fetchone()[0],
            0,
        )
        conn.close()

        locked = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        msg_block = locked.split("route == 'message_takeover'", 1)[1]
        self.assertIn('b3_executor_failed', msg_block)
        self.assertIn("_prod_status != 'success'", msg_block)
        self.assertLess(
            msg_block.index('b3_executor_failed'),
            msg_block.index('get_wake_runner'),
        )
        # Failed response must not expose rendered_content as delivered body.
        failed_slice = msg_block.split("_prod_status != 'success'", 1)[1]
        failed_return = failed_slice.split('return jsonify', 1)[1].split(
            'return jsonify', 1,
        )[0]
        self.assertIn('skipped', failed_return)
        self.assertNotIn('rendered_content', failed_return)
        self.assertNotIn("'content':", failed_return)


if __name__ == '__main__':
    unittest.main()
