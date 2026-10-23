"""Wake ``call`` action: planner, capability, gate, B2 route, ring, executor audit."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
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

from chat import authoritative_planner as planner  # noqa: E402
from chat import wake_call  # noqa: E402
from chat.behavior_authority_b2 import plan_b2_wake_action  # noqa: E402
from chat.capability_skill_view import resolved_action_capability_for  # noqa: E402
from voice_call import store as call_store  # noqa: E402

NOW = datetime.datetime(2026, 10, 23, 23, 0, 0)  # Beijing wall clock, as the gateway passes it
EPOCH = 1_800_000_000.0


def enabled(on=True):
    return mock.patch('chat.wake_call.call_enabled', return_value=on)


class PlannerAndCapabilityTests(unittest.TestCase):
    def test_call_is_offered_only_while_enabled(self):
        with enabled(False):
            self.assertEqual(planner.basic_allowed_actions(), ('none', 'message'))
            self.assertNotIn('call', resolved_action_capability_for(provider='claude_code', mode='normal'))
        with enabled(True):
            self.assertEqual(planner.basic_allowed_actions(), ('none', 'message', 'call'))
            self.assertIn('call', resolved_action_capability_for(provider='claude_code', mode='normal'))
            payload = json.loads(planner.make_basic_wake_planner_input(
                wake_run_id='r1', observed_at=NOW, user_idle_hours=2, effective_idle_hours=2,
            ).build_user_payload())
            self.assertEqual(payload['allowed_actions'], ['none', 'message', 'call'])

    def test_call_never_reaches_other_wake_modes(self):
        with enabled(True):
            for mode in ('nightwatch', 'morning', 'summarize', 'dream'):
                self.assertNotIn('call', resolved_action_capability_for(provider='claude_code', mode=mode))

    def test_a_call_decision_is_valid_only_when_allowed(self):
        decision = {'intent': '想听她声音', 'action_candidate': 'call', 'confidence': 0.8,
                    'blocked': False, 'reason_codes': [], 'wake_run_id': 'r1'}
        with self.assertRaises(ValueError):
            planner._validate_decision(dict(decision), wake_run_id='r1')
        out = planner._validate_decision(dict(decision), wake_run_id='r1',
                                         allowed_actions=('none', 'message', 'call'))
        self.assertEqual(out['action_candidate'], 'call')

    def test_planner_prompt_defers_to_allowed_actions(self):
        self.assertIn('only from allowed_actions', planner._SYSTEM_PROMPT)
        self.assertNotIn('none or message', planner._SYSTEM_PROMPT)


class GateTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT);
            CREATE TABLE wake_log (id INTEGER PRIMARY KEY, action TEXT, content TEXT, woke_at TEXT);
        """)
        conn.execute("INSERT INTO chat_messages (author, content, created_at) VALUES ('hayana', '晚安', ?)",
                     ((NOW - datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S'),))
        conn.commit()
        conn.close()
        self.epoch = EPOCH
        self.store = call_store.CallStore(self.db, lambda: self.epoch)
        self.skill = types.SimpleNamespace(resolved_action_capability=('none', 'message', 'call'))
        self.decision = {'action_candidate': 'call', 'intent': '想听她声音'}

    def tearDown(self):
        os.unlink(self.db)

    def get_db(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def gate(self, **kw):
        args = dict(planner_decision=self.decision, skill_view=self.skill, wake_run_id='r1',
                    get_db_fn=self.get_db, now=NOW, clock=lambda: self.epoch)
        args.update(kw)
        with enabled(kw.pop('_enabled', True) if '_enabled' in kw else True):
            args.pop('_enabled', None)
            return wake_call.evaluate_call_gate(**args)

    def test_allows_a_quiet_moment(self):
        self.assertEqual(self.gate(), ('ALLOW', 'ok'))

    def test_blocks_while_she_is_active(self):
        self.assertEqual(self.gate(chat_busy=True), ('BLOCK', 'user_active'))
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO chat_messages (author, content, created_at) VALUES ('hayana', '在吗', ?)",
                     ((NOW - datetime.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'),))
        conn.commit()
        conn.close()
        self.assertEqual(self.gate(), ('BLOCK', 'user_active'), 'she wrote 5 minutes ago')

    def test_blocks_while_a_call_is_open(self):
        self.store.create_if_none_open('c1')
        self.assertEqual(self.gate(), ('BLOCK', 'call_open'))
        self.epoch += call_store.RING_TIMEOUT_S + 1  # that ring went unanswered: no longer open
        self.assertEqual(self.gate(), ('ALLOW', 'ok'))

    def test_blocks_within_three_hours_of_his_last_call(self):
        self.store.create_if_none_open('c1', caller='fyodor', opening='在吗')
        self.store.transition('c1', call_store.DECLINED)
        self.epoch += 3 * 3600 - 1
        self.assertEqual(self.gate(), ('BLOCK', 'cooldown'))
        self.epoch += 1
        self.assertEqual(self.gate(), ('ALLOW', 'ok'))

    def test_her_own_calls_do_not_start_his_cooldown(self):
        self.store.create_if_none_open('c1')
        self.store.transition('c1', call_store.CANCELED)
        self.assertEqual(self.gate(), ('ALLOW', 'ok'))

    def test_blocks_when_disabled_or_not_offered(self):
        with enabled(False):
            self.assertEqual(wake_call.evaluate_call_gate(
                planner_decision=self.decision, skill_view=self.skill, wake_run_id='r1',
                get_db_fn=self.get_db, now=NOW, clock=lambda: self.epoch), ('BLOCK', 'tool_unavailable'))
        self.skill = types.SimpleNamespace(resolved_action_capability=('none', 'message'))
        self.assertEqual(self.gate(), ('BLOCK', 'tool_unavailable'))

    def test_blocks_a_seen_run_and_a_non_call_decision(self):
        self.assertEqual(self.gate(wake_run_id_seen=lambda rid: True), ('BLOCK', 'duplicate'))
        self.assertEqual(self.gate(planner_decision={'action_candidate': 'message'}),
                         ('BLOCK', 'precondition_failed'))


class B2RouteTests(GateTests):
    def plan(self, decision, b3=True, busy=False):
        with mock.patch('chat.behavior_authority_b2.consumer_enabled', return_value=True), \
                mock.patch('chat.behavior_authority_b3.effective_b3_enabled', return_value=b3), \
                mock.patch('chat.planner_shadow.run_authoritative_planner_decision',
                           return_value=('valid', decision)), \
                mock.patch('chat.wake_call.time.time', return_value=self.epoch), \
                enabled(True):
            return plan_b2_wake_action(
                planner_view=None, skill_view=self.skill, wake_run_id='r1', decision_attempt_id='a1',
                get_db_fn=self.get_db, now=NOW, chat_busy_fn=lambda: busy, mode='normal')

    def test_an_allowed_call_routes_to_call_takeover(self):
        plan = self.plan(dict(self.decision))
        self.assertEqual((plan.route, plan.gate_reason), ('call_takeover', 'ok'))

    def test_a_blocked_call_stops_instead_of_falling_back(self):
        self.assertEqual(self.plan(dict(self.decision), busy=True).route, 'blocked')
        plan = self.plan(dict(self.decision), b3=False)
        self.assertEqual((plan.route, plan.gate_reason), ('blocked', 'b3_unavailable'))


class RingAndAuditTests(unittest.TestCase):
    def test_ring_her_reports_each_outcome(self):
        self.assertEqual(wake_call.ring_her('在吗', token_fn=lambda: ''), (False, 'internal_token_missing'))
        seen = {}

        def post(url, headers, body, timeout):
            seen.update(url=url, headers=headers, body=json.loads(body))
            return 201, json.dumps({'ok': True, 'call': {'id': 'abc'}}).encode()

        self.assertEqual(wake_call.ring_her('在吗', 'ja', post=post, token_fn=lambda: 't'), (True, 'abc'))
        self.assertEqual(seen['url'], 'http://127.0.0.1:5050/internal/voice_call/ring')
        self.assertEqual(seen['headers']['X-Voice-Call-Internal-Token'], 't')
        self.assertEqual(seen['body'], {'opening': '在吗', 'language': 'ja'})
        for status, reason in ((409, 'call_open'), (429, 'cooldown'), (500, 'ring_http_500')):
            self.assertEqual(wake_call.ring_her('在吗', post=lambda *a, s=status: (s, b''), token_fn=lambda: 't'),
                             (False, reason))

        def boom(*a):
            raise OSError('refused')

        self.assertEqual(wake_call.ring_her('在吗', post=boom, token_fn=lambda: 't'),
                         (False, 'ring_request_failed:OSError'))

    def test_opening_is_validated_and_bounded(self):
        self.assertEqual(wake_call.validate_opening('{"rendered_content": "哈娅，在做什么？"}'), '哈娅，在做什么？')
        with self.assertRaises(ValueError):
            wake_call.validate_opening(json.dumps({'rendered_content': 'x' * 201}))

    def test_the_renderer_knows_what_a_call_opening_is(self):
        from chat.behavior_authority_b3 import _CALL_OPENING_NOTE
        self.assertIn('call_opening', _CALL_OPENING_NOTE)

    def test_executor_records_a_call_as_audit_only(self):
        fd, db = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE wake_log (id INTEGER PRIMARY KEY AUTOINCREMENT, thoughts TEXT, action TEXT,
                    content TEXT, consumed INTEGER, woke_at TEXT, wake_run_id TEXT, notified INTEGER,
                    cache_info TEXT);
                CREATE TABLE chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, author TEXT,
                    content TEXT, thinking TEXT, created_at TEXT);
            """)
            conn.commit()
            conn.close()

            def get_db():
                c = sqlite3.connect(db)
                c.row_factory = sqlite3.Row
                return c

            from wake.executor import execute
            with mock.patch('chat.window_identity.soft_window_enabled', return_value=False):
                out = execute('call', '想听她声音', '哈娅，在做什么？', 'normal', get_db_fn=get_db,
                              wake_run_id='r1', settlement_required=False,
                              cache_info={'source': 'wake', 'call_id': 'abc'})
            self.assertTrue(out['delivered'])
            conn = get_db()
            row = conn.execute("SELECT action, content, consumed, notified FROM wake_log").fetchone()
            self.assertEqual(tuple(row), ('call', '哈娅，在做什么？', 1, 1))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0], 0,
                             'never written into chat')
            import wake_push
            self.assertIsNone(wake_push.lease_pending_payload(
                conn, lambda c: c.execute(
                    "SELECT * FROM wake_log WHERE action='message' AND (notified IS NULL OR notified=0) "
                    "ORDER BY id DESC LIMIT 1").fetchone(),
                False, time.time(), 'dev'), 'the push channel never hands it out')
            conn.close()
        finally:
            os.unlink(db)



class GatewayCallTakeoverContractTests(unittest.TestCase):
    """gateway.py's call_takeover, read as source like the other Wake route contracts."""

    def section(self):
        gateway = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        start = gateway.index("if _b2_plan is not None and _b2_plan.route == 'call_takeover'")
        end = gateway.index("if _b2_plan is not None and _b2_plan.route == 'message_takeover'")
        return gateway[start:end]

    def test_order_is_render_then_recheck_then_ring_then_audit(self):
        s = self.section()
        order = [s.index(marker) for marker in (
            'invoke_fn=invoke_renderer_relay',
            '_wake_call.validate_opening(',
            '_wake_call.evaluate_call_gate(',
            'chat_busy=_chat_is_generating()',
            '_wake_call.ring_her(opening)',
            "_wake_exec(\n                'call',",
        )]
        self.assertEqual(order, sorted(order))

    def test_never_uses_the_shared_resident_or_settles(self):
        s = self.section()
        self.assertNotIn('_shared_delivery_fence', s)
        self.assertNotIn('_try_invoke_shared_renderer', s)
        self.assertIn('settlement_required=False', s)

    def test_every_failure_stops_without_legacy(self):
        s = self.section()
        for reason in ('call_renderer_failed', 'call_gate_recheck', 'call_ring_failed'):
            self.assertIn(reason, s)
        self.assertNotIn('get_wake_runner', s)

if __name__ == '__main__':
    unittest.main()
