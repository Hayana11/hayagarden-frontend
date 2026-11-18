"""2b: proactive cold Wake receipt, Wake-tail hot proof, one-shot, finality (fake resident, temp DB)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident
import config_store
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.capacity_swap_runtime import note_same_context_last_good
from chat.session_registry import register_context_claude_session
from chat.mail_notice_store import peek_pending_mail_notice_ids, record_pending_mail_notice
from tests.test_proactive_cold_prepare import NOW, TRIGGER, _ColdResident, _GatewayBase, _insert

SHA = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
TOOLS = [
    ('tool_use', {'id': 't1', 'name': 'lookup', 'args': {'q': 1}}),
    ('tool_result', {'tool_use_id': 't1', 'result': 'found', 'is_error': False}),
]


class _SessionResident(_ColdResident):
    """Cold fake that writes a real Claude JSONL, so the native mapping is genuinely proven."""
    cwd, generation, model_identity = '/work', 1, 'm-1'
    bound_tool_surface_fingerprint = 'fp'

    def __init__(self):
        super().__init__()
        self.script, self.finality = [('text', 'spoken reply')], {'stream_totals_match': True}
        self.n, self.on_stream = 0, None

    @property
    def path(self):
        from chat.session_registry import derive_transcript_path
        return derive_transcript_path(cwd=self.cwd, claude_session_id='sess-1', claude_home=None)

    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        self.system_text = system_text
        return super().ensure_alive(system_text, env, tool_profile=tool_profile)

    def peek_respawn_reason(self, *a, **k):
        return ''

    def _rows(self, content):
        self.n += 1
        rows, parent = [], None
        steps = [('user', str(content)[:30])] + [
            ('assistant', e[1]) if e[0] == 'text' else ('assistant', e[1]) if e[0] == 'tool_use'
            else ('user', e[1]) for e in self.script if e[0] in ('text', 'tool_use', 'tool_result')]
        for i, (role, body) in enumerate(steps):
            if isinstance(body, dict) and 'name' in body:
                body = [{'type': 'tool_use', 'id': body['id'], 'name': body['name'], 'input': body['args']}]
            elif isinstance(body, dict):
                body = [{'type': 'tool_result', 'tool_use_id': body['tool_use_id'], 'content': body['result']}]
            elif role == 'assistant':
                body = [{'type': 'text', 'text': body}]
            uid = 'u%d-%d' % (self.n, i)
            rows.append({'type': role, 'uuid': uid, 'parentUuid': parent, 'sessionId': self.session_id,
                         'message': {'role': role, 'content': body}})
            parent = uid
        return rows

    def send_turn(self, content, commit_meta=None, turn_lease=None, **kw):
        self.sent.append(str(content))
        if self.fail_at == 'before_flush':
            raise RuntimeError('stdin never reached the model')
        for hook in ('on_stdin_begin', 'on_stdin_flushed'):
            if kw.get(hook):
                kw[hook]()
        if self.fail_at == 'after_flush':
            raise RuntimeError('provider died after flush')
        self.session_id = self.session_id or 'sess-1'
        if self.on_stream:
            self.on_stream()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'ab') as fh:
            fh.write(b''.join((json.dumps(r) + '\n').encode() for r in self._rows(content)))
        for item in self.script:
            if item[0] == 'raise':
                raise RuntimeError('boom mid-stream')
            if item[0] != 'nodone':
                yield item
        if not any(e[0] in ('done', 'nodone') for e in self.script):
            text = ''.join(p for e, p in self.script if e == 'text')
            usage = {'input_tokens': 3}
            if self.finality is not None:
                usage['jsonl_usage'] = dict(self.finality)
            claims = {'pending_mail_ids': list((commit_meta or {}).get('pending_mail_ids') or [])}
            yield ('done', (text, '', usage, claims))


class _World(_GatewayBase):
    """One cold Wake driven through the real gateway glue (_wake_decide_locked) and executor."""

    def setUp(self):
        super().setUp()
        from chat import behavior_authority_b3 as b3, window_identity, interaction_state
        from wake import unified_modes
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack.enter_context(mock.patch.dict(os.environ, {'HOME': self.tmp.name}))
        self.resident = _SessionResident()
        ident = {'chat_id': 'default', 'context_id': 1, 'context_epoch': 1, 'resident_generation': 1}
        clock = types.SimpleNamespace(reliable=True, user_idle_hours=1.0, effective_idle_hours=1.0, last_user_at=None)
        gw = self.gw
        for patch in (
            mock.patch.object(gw, '_CC_RESIDENT', self.resident),
            mock.patch.object(gw, '_wake_run_id_seen', return_value=False),
            mock.patch.object(gw, '_wake_run_id_mark'),
            mock.patch.object(gw, '_chat_is_generating', return_value=False),
            mock.patch.object(gw, '_ensure_wake_runners', return_value=mock.Mock(select_wake_provider=lambda m: 'claude_code')),
            mock.patch.object(config_store, 'get_float', return_value=30.0),
            mock.patch.object(interaction_state, 'read_interaction_clock', return_value=clock),
            mock.patch.object(interaction_state, 'wake_guard_reason', return_value=None),
            mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True),
            mock.patch.object(unified_modes, 'is_unified_main_chat_wake_mode', return_value=True),
            mock.patch.object(window_identity, 'soft_window_enabled', return_value=True),
            mock.patch.object(window_identity, 'capture_current_window_identity', return_value=ident),
            mock.patch.object(window_identity, 'read_current_window_identity_conn', return_value=ident),
            mock.patch('chat.wake_read_observation.collect_wake_read_observation',
                       return_value={'started': False, 'bundle': None, 'watermark_skip': None}),
        ):
            self.stack.enter_context(patch)

    def decide(self, rid='rid-w'):
        with self.gw.app.app_context():
            return self.gw._wake_decide_locked({'mode': 'normal', 'wake_run_id': rid}, 'normal', '', '')

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def exec_sql(self, sql, *args):
        conn = sqlite3.connect(self.db)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def spoken_cold_wake(self):
        out = self.decide()
        body = out.get_json() if hasattr(out, 'get_json') else out
        self.assertEqual(body.get('action'), 'message', body)
        self.wake_id = self.q("SELECT id FROM chat_messages WHERE source_kind='wake'")[0][0]
        self.exec_sql("UPDATE chat_messages SET created_at='2026-07-27 10:05:00' WHERE id=?", self.wake_id)
        return self.wake_id

    def next_chat(self, text='next user message'):
        uid = _insert(self.db, 'hayana', text, '2026-07-27 10:30:00')
        return dr.prepare_daily_turn(
            user_message_id=uid, db_path=self.db, now=NOW.replace(hour=10, minute=30),
            wall_now=NOW.replace(hour=10, minute=30), resident=self.resident, static_system='STATIC',
            static_system_sha256=SHA('STATIC'), persona_sha256=SHA('P'), provider='claude_code', model='m-1')

    def proof_inputs(self):
        plan = self.next_chat()
        dr._freeze_hot_receipt(plan, resident=self.resident)
        registry = dr.get_context_claude_session(plan.context_id, plan.resident_generation, db_path=self.db)
        return plan, plan.hot_receipt_frozen, registry

    def proof(self, plan=None, frozen=None, registry=None):
        p, f, r = self.proof_inputs() if plan is None else (plan, frozen, registry)
        return dr._hot_native_tail_proof(p, frozen=f, registry=r)


class ColdWakeHappyPathTests(_World):
    def test_cold_wake_success_then_chat_hot_noop_from_cursor_A(self):
        before = self.counts()
        a = self.spoken_cold_wake()
        self.assertEqual(self.counts()[1:], before[1:])                       # no user row, no user context
        self.assertEqual(self.counts()[0], before[0] + 1)                     # only the assistant row
        self.assertEqual(dc.get_resident_history_cursor(1, 1, db_path=self.db), a)
        self.assertEqual(dr.get_local_binding().bound_cursor_message_id, a)
        receipt = self.q('SELECT installed_source_watermark, resident_generation FROM context_receipts')
        self.assertEqual(receipt, [(a, 1)])
        plan = self.next_chat()
        events = list(dr.stream_daily_resident_turn(plan, resident=self.resident, env={}, static_system='STATIC'))
        self.assertEqual((plan.hot_decision, plan.hot_decision_reason), ('NO_OP', ''))
        self.assertTrue(any(e == 'done' for e, _ in events))
        self.assertEqual((plan.context_id, plan.context_epoch, plan.resident_generation), (1, 1, 1))
        self.assertEqual(plan.cursor_before, a)
        sent = self.resident.sent[-1]
        self.assertEqual(len(self.resident.sent), 2)                           # same live process, no respawn
        self.assertIn('next user message', sent)
        self.assertNotIn(TRIGGER, sent)                                        # not resent
        self.assertNotIn('spoken reply', sent)
        self.assertEqual(self.resident.killed, 0)
        dr._release_lease(plan)


class ColdWakeGenerationHandoffTests(_World):
    def setUp(self):
        super().setUp()
        from chat import window_identity

        ctx = dc.get_or_create_daily_context(now=NOW, db_path=self.db)
        register_context_claude_session(
            context_id=ctx['id'],
            context_epoch=ctx['context_epoch'],
            resident_generation=ctx['resident_generation'],
            chat_id=ctx['chat_id'],
            claude_session_id='old-session',
            cwd='/work',
            source='daily_runtime',
            scan_offset=0,
            process_generation=1,
            db_path=self.db,
        )

        def current_identity(conn=None):
            owned = conn is None
            if owned:
                conn = sqlite3.connect(self.db)
                conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT id, chat_id, context_epoch, resident_generation "
                    "FROM daily_contexts WHERE chat_id='default' "
                    "ORDER BY context_epoch DESC LIMIT 1"
                ).fetchone()
                return {
                    'chat_id': row['chat_id'],
                    'context_id': row['id'],
                    'context_epoch': row['context_epoch'],
                    'resident_generation': row['resident_generation'],
                }
            finally:
                if owned:
                    conn.close()

        self.stack.enter_context(mock.patch.object(
            window_identity, 'capture_current_window_identity',
            side_effect=lambda **kwargs: current_identity(),
        ))
        self.stack.enter_context(mock.patch.object(
            window_identity, 'read_current_window_identity_conn',
            side_effect=lambda conn, **kwargs: current_identity(conn),
        ))

    def test_cold_respawn_promotes_delivery_identity_to_new_generation(self):
        old_generation = self.q('SELECT resident_generation FROM daily_contexts')[0][0]
        a = self.spoken_cold_wake()
        new_generation = self.q('SELECT resident_generation FROM daily_contexts')[0][0]
        self.assertEqual(new_generation, old_generation + 1)
        self.assertEqual(
            self.q('SELECT resident_generation FROM daily_message_contexts WHERE message_id=?', a),
            [(new_generation,)],
        )

    def test_cold_respawn_does_not_promote_across_epoch_change(self):
        def change_epoch():
            self.exec_sql(
                'UPDATE daily_contexts SET context_epoch=context_epoch+1'
            )

        self.resident.on_stream = change_epoch
        out = self.decide('rid-window-drift')
        body = out.get_json() if hasattr(out, 'get_json') else out
        self.assertEqual(body.get('reason'), 'NORMAL_WAKE_MAIN_CHAT_FAILED', body)
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM chat_messages WHERE source_kind='wake'"),
            [(0,)],
        )


class WakeTailProofTests(_World):
    """Table 5 a-h: each row tampers one real piece of data and must stop the proof."""

    def setUp(self):
        super().setUp()
        self.a = self.spoken_cold_wake()
        self.plan, self.frozen, self.registry = self.proof_inputs()
        base = self.proof(self.plan, self.frozen, self.registry)
        self.assertEqual((base['status'], base['assistant_message_id'], base['wake_skip_count']), ('pass', self.a, 0))
        self.end = base['transcript_end_offset']

    def fails(self, reason, frozen=None, registry=None):
        res = self.proof(self.plan, frozen or self.frozen, registry or self.registry)
        self.assertNotEqual(res['status'], 'pass')
        self.assertEqual(res['reason'], reason)

    def member_swap(self, **changes):
        import dataclasses
        members = [dataclasses.replace(m, **changes) if m.source_kind == 'current_request' else m
                   for m in self.frozen['members']]
        return {**self.frozen, 'members': tuple(members)}

    def cache(self, key, value):
        self.exec_sql("UPDATE chat_messages SET cache_info=json_set(cache_info, ?, json(?)) WHERE id=?",
                      '$.' + key, json.dumps(value), self.a)

    def test_a_receipt_request_member(self):
        self.fails('wake_request_proof_missing', self.member_swap(source_ref='wake_request:'))
        extra = [m for m in self.frozen['members'] if m.source_kind == 'current_request']
        self.fails('wake_request_proof_ambiguous', {**self.frozen, 'members': tuple(self.frozen['members']) + tuple(extra)})

    def test_b_watermark_must_precede_this_turn(self):
        self.plan.history_anchor_message_id = self.a
        self.fails('wake_tail_is_not_previous')

    def test_c_assistant_anchor(self):
        self.cache('wake_run_id', 'someone-else')
        self.fails('wake_tail_anchor_mismatch')

    def test_c_anchor_flags_and_context_row(self):
        self.cache('proactive_cold_start', False)
        self.fails('wake_tail_anchor_mismatch')
        self.cache('proactive_cold_start', True)
        self.exec_sql('UPDATE daily_message_contexts SET context_epoch=9 WHERE message_id=?', self.a)
        self.fails('wake_tail_anchor_mismatch')

    def test_d_wake_log_row(self):
        self.exec_sql("DELETE FROM wake_log WHERE wake_run_id='rid-w'")
        self.fails('wake_tail_log_missing')

    def test_e_message_between_anchor_and_reply(self):
        self.cache('history_anchor_message_id', self.a - 2)              # (H, W) now contains a real row
        self.fails('wake_tail_foreign_rows')

    def test_f_mapping(self):
        sql = 'UPDATE chat_message_claude_events SET %s WHERE message_id=? AND role=?'
        for change, args, reason in (
            ("claude_session_id='other'", ('assistant',), 'wake_tail_mapping_session_mismatch'),
            ('jsonl_byte_offset=-1', ('assistant',), 'wake_tail_mapping_order_invalid'),
            ('jsonl_byte_offset=999999999', ('assistant',), 'wake_tail_mapping_offset_invalid'),
        ):
            rows = self.q('SELECT event_uuid, claude_session_id, jsonl_byte_offset FROM chat_message_claude_events WHERE message_id=?', self.a)
            self.exec_sql(sql % change, self.a, *args)
            self.fails(reason)
            for uid, sid, off in rows:
                self.exec_sql('UPDATE chat_message_claude_events SET claude_session_id=?, jsonl_byte_offset=? WHERE event_uuid=?', sid, off, uid)
        self.exec_sql("DELETE FROM chat_message_claude_events WHERE message_id=? AND role='provider_input'", self.a)
        self.fails('wake_tail_mapping_incomplete')

    def test_g_last_good_and_registry_offsets(self):
        self.fails('native_tail_offset_mismatch', registry={**self.registry, 'scan_offset': self.end + 5})
        note_same_context_last_good(context_id=1, context_epoch=1, resident_generation=1, claude_session_id='sess-1',
                                    source='daily_runtime', transcript_end_offset=self.end + 7)
        self.fails('native_tail_offset_mismatch')
        note_same_context_last_good(context_id=1, context_epoch=1, resident_generation=1, claude_session_id='other',
                                    source='daily_runtime', transcript_end_offset=self.end)
        self.fails('same_context_last_good_identity_mismatch')

    def test_h_tail_member_is_derived_from_the_row(self):
        self.cache('wake_mode', 'not-a-unified-mode')
        self.fails('wake_tail_member_mismatch')

    def test_turn_kind_disguise_does_not_bypass_chat_proof(self):
        self.plan.turn_mode = 'wake'
        self.plan.manifest['turn_kind'] = 'wake'
        chat_request = self.member_swap(source_ref='message:%d' % (self.a - 1))
        with mock.patch.object(dr, '_hot_wake_tail_proof') as wake_proof:
            res = self.proof(self.plan, chat_request, self.registry)
        wake_proof.assert_not_called()
        self.assertNotEqual(res['status'], 'pass')
        self.assertNotIn('wake_', res['reason'])
        other = self.member_swap(source_ref='diary:7')
        self.assertEqual(self.proof(self.plan, other, self.registry)['status'], 'ambiguous')


class ForeignRowTests(_World):
    def test_e_message_arriving_during_the_wake_blocks_the_hot_proof(self):
        self.resident.on_stream = lambda: _insert(self.db, 'hayana', 'sent while she was thinking', '2026-07-27 10:02:00')
        a = self.spoken_cold_wake()
        res = self.proof()
        self.assertEqual((res['status'], res['reason']), ('missing', 'wake_tail_foreign_rows'))
        self.assertEqual(self.q('SELECT COUNT(*) FROM chat_messages WHERE id>? AND id<?', self.latest, a)[0][0], 1)


class ColdWakeOutcomeTests(_World):
    MAIL = '7'

    def setUp(self):
        super().setUp()
        record_pending_mail_notice(self.gw.get_db, self.MAIL)

    def pending(self):
        return peek_pending_mail_notice_ids(self.gw.get_db)

    def tables(self):
        def count(table):
            try:
                return self.q('SELECT COUNT(*) FROM %s' % table)[0][0]
            except sqlite3.OperationalError:
                return 0
        return {t: count(t) for t in ('context_receipts', 'chat_message_claude_events', 'context_claude_sessions')}

    def assert_untouched_failure(self, body, reason):
        self.assertEqual((body['skipped'], body['reason']), (True, reason))
        self.assertEqual(self.pending(), [self.MAIL])                          # one-shot kept for next time
        self.assertIsNone(dr.get_local_binding())
        self.assertEqual(self.counts()[0], self.total)                          # nothing delivered
        self.assertEqual(self.tables(), dict.fromkeys(self.tables(), 0))        # no receipt, no mapping, no registry
        self.assertEqual(self.q('SELECT COUNT(*) FROM wake_log')[0][0], 0)

    def test_one_shot_injected_once_and_consumed_once_on_success(self):
        self.spoken_cold_wake()
        self.assertEqual(self.resident.sent[0].count('【邮箱提示】'), 1)
        self.assertEqual(self.pending(), [])
        plan = self.next_chat()
        list(dr.stream_daily_resident_turn(plan, resident=self.resident, env={}, static_system='STATIC'))
        self.assertNotIn('【邮箱提示】', self.resident.sent[-1])                   # not injected twice
        dr._release_lease(plan)

    def test_binding_installed_before_stdin_but_flush_never_happens(self):
        self.resident.fail_at = 'before_flush'
        with mock.patch.object(dr, 'set_local_binding', wraps=dr.set_local_binding) as spy:
            body = self.decide()
        self.assertTrue(any(c.args[0] is not None for c in spy.call_args_list))   # it was installed ...
        self.assert_untouched_failure(body, 'NORMAL_WAKE_PRE_MODEL_FAILED')       # ... and fully restored
        self.assertIsNone(dc.get_resident_history_cursor(1, 1, db_path=self.db))
        self.assertEqual(self.q('SELECT COUNT(*) FROM daily_resident_turn_leases')[0][0], 0)
        self.assertEqual(dc.get_daily_context_by_id(1, db_path=self.db)['resident_generation'], 1)

    def test_failure_after_flush_keeps_one_shot_and_retires(self):
        self.resident.fail_at = 'after_flush'
        self.assert_untouched_failure(self.decide(), 'NORMAL_WAKE_MAIN_CHAT_FAILED')
        self.assertGreaterEqual(self.resident.killed, 1)

    def test_unconfirmed_jsonl_finality_installs_nothing(self):
        for finality in (None, {}, {'stream_totals_match': False}, {'stream_totals_match': 'yes'}):
            dr.reset_bindings_for_tests()
            self.resident.finality = finality
            self.assert_untouched_failure(self.decide('rid-%s' % json.dumps(finality)), 'NORMAL_WAKE_MAIN_CHAT_FAILED')
            self.assertGreaterEqual(self.resident.killed, 1)
            self.resident.session_id = None

    def test_executor_failure_keeps_one_shot(self):
        with mock.patch('wake.executor.execute', side_effect=RuntimeError('db down')):
            body = self.decide()
        self.assertEqual(body['reason'], 'NORMAL_WAKE_MAIN_CHAT_FAILED')
        self.assertEqual(self.pending(), [self.MAIL])
        self.assertIsNone(dr.get_local_binding())
        self.assertEqual(self.tables()['context_receipts'], 0)

    def test_receipt_failure_keeps_one_shot_and_forces_cold_next(self):
        with mock.patch.object(dr, '_commit_production_context_receipt', return_value=False):
            body = self.decide()
        self.assertEqual(body['action'], 'message')                             # it was delivered
        self.assertEqual(self.pending(), [self.MAIL])
        self.assertEqual(self.tables()['context_receipts'], 0)
        self.assertIsNone(dr.get_local_binding())
        self.assertEqual(dc.get_daily_context_by_id(1, db_path=self.db)['resident_generation'], 2)
        self.assertTrue(self.next_chat().is_cold)

    def test_anchor_context_row_commits_with_the_message(self):
        with mock.patch.object(dr.dc, 'record_daily_message_context', side_effect=RuntimeError('crash')):
            body = self.decide()
        self.assertEqual(body['action'], 'message')
        a = self.q("SELECT id FROM chat_messages WHERE source_kind='wake'")[0][0]
        self.assertEqual(self.q('SELECT context_id, context_epoch, resident_generation, role '
                                'FROM daily_message_contexts WHERE message_id=?', a), [(1, 1, 1, 'assistant')])

    def test_silent_cold_wake_retires_resident_without_receipt(self):
        self.resident.script = [('text', '[[SILENT]]')]
        body = self.decide()
        self.assertEqual((body['action'], body['content']), ('none', ''))
        self.assertEqual(self.counts()[0], self.total)                          # no assistant anchor
        skip = json.loads(self.q('SELECT cache_info FROM wake_log')[0][0])['transcript_skip']
        self.assertTrue(skip['skipped_provider_round'])
        self.assertEqual(self.tables()['context_receipts'], 0)
        self.assertIsNone(dr.get_local_binding())
        self.assertGreaterEqual(self.resident.killed, 1)
        self.assertEqual(self.pending(), [self.MAIL])
        self.assertTrue(self.next_chat().is_cold)

    def test_tool_use_round_maps_as_one_turn_and_next_chat_is_hot(self):
        from chat.claude_event_mapping import ROLE_ASSISTANT, ROLE_PROVIDER_INPUT, ROLE_TOOL_RESULT_USER, ROLE_TOOL_USE
        self.resident.script = TOOLS + [('text', 'found it')]
        a = self.spoken_cold_wake()
        roles = {r[0] for r in self.q('SELECT role FROM chat_message_claude_events WHERE message_id=?', a)}
        self.assertEqual(roles, {ROLE_PROVIDER_INPUT, ROLE_TOOL_USE, ROLE_TOOL_RESULT_USER, ROLE_ASSISTANT})
        plan = self.next_chat()
        list(dr.stream_daily_resident_turn(plan, resident=self.resident, env={}, static_system='STATIC'))
        self.assertEqual((plan.hot_decision, plan.hot_decision_reason), ('NO_OP', ''))
        dr._release_lease(plan)


class LoopContractTests(_World):
    """The hot and the cold event loops are two copies: lock their observable behaviour together."""
    USAGE = {'jsonl_usage': {'stream_totals_match': True}}

    def setUp(self):
        super().setUp()
        from chat import unified_heartbeat_a1 as uh
        wm = types.SimpleNamespace(context_id=1, context_epoch=1, resident_generation=1,
                                 process_generation=1, expected_offset=0)
        self.hot_events = []

        def hot_stream(*args, **kwargs):
            for item in self.hot_events:
                if item[0] == 'raise':
                    raise RuntimeError('boom mid-stream')
                if item[0] != 'nodone':
                    yield item
        for patch in (
            mock.patch.object(self.gw, '_cc_resident_stream_gen', side_effect=hot_stream),
            mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(wm, 'ok')),
            mock.patch.object(uh, 'prepare_shared_transcript_native_binding', return_value={'start_offset': 0, 'end_offset': 1}),
            mock.patch.object(uh, 'commit_shared_transcript_watermark', return_value={'start_offset': 0, 'end_offset': 1}),
        ):
            self.stack.enter_context(patch)

    def both(self, events):
        """Run one event script through the hot loop and through the cold loop; return both results."""
        self.hot_events, self.resident.script = list(events), list(events)
        self.ready = (True, 'ok')
        hot = self.run_wake('normal', 'rid-hot')
        self.ready = (False, 'no_local_binding')
        cold = self.run_wake('normal', 'rid-cold')
        self.gw._proactive_cold_abort(cold, 'contract')
        return hot, cold

    def done(self, text, think=''):
        return ('done', (text, think, dict(self.USAGE), {}))

    def test_same_events_give_same_accumulation(self):
        events = [('think', 'pl'), ('text', 'Hel'), ('self_state', 'calm'), *TOOLS, ('text', 'lo'), ('think', 'an'), self.done('Hello', 'plan')]
        hot, cold = self.both(events)
        for receipt in (hot, cold):
            for call in receipt['tool_calls']:
                observed_at = datetime.fromisoformat(call.pop('time'))
                self.assertEqual(observed_at.tzinfo, timezone.utc)
        keys = ('text', 'silent', 'thinking', 'self_state', 'display_segments', 'tool_calls')
        self.assertEqual({k: hot[k] for k in keys}, {k: cold[k] for k in keys})
        self.assertEqual((cold['text'], cold['thinking'], cold['self_state']), ('Hello', 'plan', 'calm'))
        self.assertEqual(cold['tool_calls'], [{'id': 't1', 'name': 'lookup', 'args': {'q': 1}, 'result': 'found', 'success': True}])
        kinds = [seg.get('type') for seg in json.loads(cold['display_segments'])]
        self.assertEqual(kinds, [seg.get('type') for seg in json.loads(hot['display_segments'])])
        self.assertIn('tool', kinds)

    def test_done_payload_and_streamed_text_resolve_the_same_way(self):
        for events, expected in (
            ([('text', 'partial'), ('think', 'draft'), self.done('FINAL', 'final thought')], ('partial', 'draft')),
            ([self.done('FINAL', 'final thought')], ('FINAL', 'final thought')),            # nothing streamed
        ):
            hot, cold = self.both(events)
            self.assertEqual((hot['text'], hot['thinking']), (cold['text'], cold['thinking']))
            self.assertEqual((cold['text'], cold['thinking']), expected)

    def test_silent_marker_splits_the_same_way(self):
        for text, silent in (('[[SILENT]]', True), ('quiet [[SILENT]]', False)):
            hot, cold = self.both([('text', text), self.done(text)])
            self.assertEqual((hot['text'], hot['silent']), (cold['text'], cold['silent']))
            self.assertEqual(cold['silent'], silent)

    def test_missing_done_and_empty_reply_raise_in_both(self):
        for events in ([('text', 'no done'), ('nodone', None)], [self.done('')]):
            self.hot_events, self.resident.script = list(events), list(events)
            for ready in ((True, 'ok'), (False, 'no_local_binding')):
                self.ready = ready
                with self.assertRaises(RuntimeError):
                    self.run_wake('normal', 'rid-x')
            dr.reset_bindings_for_tests()
            self.resident.session_id = None

    def test_mid_stream_failure_cleans_up_in_both(self):
        events = [('text', 'half'), ('raise', None)]
        for ready in ((True, 'ok'), (False, 'no_local_binding')):
            self.ready = ready
            self.fence.finish.reset_mock()
            self.hot_events, self.resident.script = list(events), list(events)
            with self.assertRaises(RuntimeError):
                self.run_wake('normal', 'rid-fail')
            self.assertEqual([c.args[0] for c in self.fence.finish.call_args_list], [False])   # fence closed once, undelivered
            self.assertEqual(self.counts()[0], self.total)
        self.assertIsNone(dr.get_local_binding())                   # cold: binding cleared ...
        self.assertGreaterEqual(self.resident.killed, 1)            # ... resident killed
        self.assertEqual(self.q('SELECT COUNT(*) FROM daily_resident_turn_leases')[0][0], 0)   # ... lease released
