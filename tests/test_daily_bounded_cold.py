"""P0.5 Daily Soft Window bounded cold recovery tests — no real Claude calls.

T1  long context cold uses newest-suffix under budget (not full epoch)
T2  short context cold keeps all formal history
T3  complete-round boundaries (no assistant-leading / half-round cuts)
T4  current user sent once (absent from history replay)
T5  hot path unchanged after cold
T6  whole-prompt Fence B deterministic rebuild
T7  unshrinkable fail closed before send_turn
T8  hard_context no-benefit / genuine hot-growth allow
T9  DB history + membership untouched by trim
T10 Daily path does not pollute classic #218 history-rewrite fence
"""
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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-daily-bounded-cold-config.db'),
)

from chat import daily_context as dc
from chat import daily_history as dh
from chat import daily_runtime as dr
from tools.lease_signer import issue_turn_lease
from chat.cold_bootstrap_budget import ColdBootstrapOverflow, NoBenefitRespawnError
from chat.daily_cold_history import (
    group_formal_history_rounds,
    select_newest_complete_rounds_under_budget,
)


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            cache_info TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS wake_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thoughts TEXT, action TEXT, content TEXT, consumed INTEGER,
            woke_at TEXT, cache_info TEXT, wake_run_id TEXT
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


def _insert(
    db_path: str,
    author: str,
    content: str,
    created_at: str,
    *,
    source_kind: str = 'chat',
    cache_info=None,
) -> int:
    conn = sqlite3.connect(db_path)
    if isinstance(cache_info, dict):
        cache_info = json.dumps(cache_info, ensure_ascii=False)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, source_kind, cache_info, created_at) VALUES (?,?,?,?,?)',
        (author, content, source_kind, cache_info or '', created_at),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


_NORMAL_WAKE_CACHE_INFO = {
    'provider': 'claude_code',
    'source': 'wake',
    'b3_authority': True,
    'unified_chat_resident': True,
    'wake_mode': 'normal',
    'canonical_chat_history': True,
}

_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)
_real_current_chat_day = dc._current_chat_day


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


def _ctx(db: str, day: str = '2026-07-27', *, chat_id: str = 'default', **kwargs):
    return dc.get_or_create_daily_context(
        chat_id=chat_id,
        local_day=day,
        db_path=db,
        now=kwargs.pop('now', _FIXED_NOW),
        **kwargs,
    )


def _seed_long_history(db: str, n_rounds: int = 80, *, row_chars: int = 200) -> list[int]:
    ids = []
    base = datetime.datetime(2026, 7, 27, 5, 0, 0)
    for i in range(n_rounds):
        ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
        ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
        ids.append(_insert(db, 'hayana', 'U%d ' % i + 'x' * row_chars, ts_u))
        ids.append(_insert(db, 'fyodor', 'A%d ' % i + 'y' * row_chars, ts_a))
    return ids


class RoundSelectionUnitTests(unittest.TestCase):
    def test_t3_complete_round_boundaries(self):
        msgs = []
        for i in range(6):
            msgs.append({'message_id': i * 2 + 1, 'role': 'user', 'content': 'U%d ' % i + 'x' * 40})
            msgs.append({'message_id': i * 2 + 2, 'role': 'assistant', 'content': 'A%d ' % i + 'y' * 40})
        retained, stats = select_newest_complete_rounds_under_budget(
            msgs, history_token_budget=80,
        )
        self.assertTrue(stats['cold_history_trimmed'])
        self.assertEqual(retained[0]['role'], 'user')
        rounds = group_formal_history_rounds(retained)
        for rnd in rounds:
            self.assertEqual(rnd[0]['role'], 'user')
            self.assertTrue(any(m['role'] == 'assistant' for m in rnd))


class DailyBoundedColdAssemblyTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, 'db', None):
            os.unlink(self.db)

    def test_t1_long_context_cold_is_bounded_newest_suffix(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        ctx = _ctx(self.db, chat_id='longcold')
        ids = _seed_long_history(self.db, n_rounds=80, row_chars=200)
        current_user = _insert(self.db, 'hayana', 'CURRENT_USER_ONLY', '2026-07-27 09:59:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            built = dh.build_daily_window_context(
                chat_id='longcold',
                daily_context=ctx,
                current_user_message_id=current_user,
                static_system='S',
                is_cold=True,
                db_path=self.db,
                history_token_budget=800,
            )
        history = built['current_day_history']
        hist_ids = [m['message_id'] for m in history]
        self.assertTrue(built['manifest']['cold_history_trimmed'])
        self.assertLess(len(history), len(ids))
        self.assertLessEqual(
            built['manifest']['cold_history_tokens_after'],
            built['manifest']['cold_history_budget'],
        )
        # Newest suffix: last retained id is the tip of eligible history (before current user)
        self.assertEqual(hist_ids[-1], ids[-1])
        self.assertNotIn(current_user, hist_ids)
        self.assertNotIn(ids[0], hist_ids)
        self.assertEqual(history[0]['role'], 'user')

    def test_t2_short_context_not_trimmed(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        ctx = _ctx(self.db, chat_id='shortcold')
        ids = []
        for i in range(3):
            ids.append(_insert(self.db, 'hayana', 'u%d' % i, '2026-07-27 09:0%d:00' % i))
            ids.append(_insert(self.db, 'fyodor', 'a%d' % i, '2026-07-27 09:0%d:30' % i))
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch('config_store.get_int', side_effect=lambda k, d=0: {
                 'HISTORY_TOKEN_BUDGET': 24_000,
             }.get(k, d)):
            built = dh.build_daily_window_context(
                chat_id='shortcold',
                daily_context=ctx,
                static_system='S',
                is_cold=True,
                db_path=self.db,
            )
        hist_ids = [m['message_id'] for m in built['current_day_history']]
        self.assertEqual(hist_ids, ids)
        self.assertFalse(built['manifest'].get('cold_history_trimmed'))

    def test_t4_current_user_only_once_in_send_payload(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        ctx = _ctx(self.db, chat_id='once')
        _seed_long_history(self.db, n_rounds=20, row_chars=80)
        current_user = _insert(self.db, 'hayana', 'UNIQUE_CURRENT_USER_TEXT', '2026-07-27 09:59:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            built = dh.build_daily_window_context(
                chat_id='once',
                daily_context=ctx,
                current_user_message_id=current_user,
                static_system='S',
                is_cold=True,
                db_path=self.db,
                history_token_budget=600,
            )
        hist_ids = [m['message_id'] for m in built['current_day_history']]
        self.assertNotIn(current_user, hist_ids)
        content = dr.format_resident_turn_content(
            assembly=built,
            user_content='UNIQUE_CURRENT_USER_TEXT',
            is_cold=True,
            is_respawn=False,
        )
        self.assertEqual(content.count('UNIQUE_CURRENT_USER_TEXT'), 1)

    def test_t5_hot_path_unchanged_after_cold(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        ctx = _ctx(self.db, chat_id='hotpath')
        ctx_id = int(ctx['id'])
        gen = int(ctx['resident_generation'])
        ids = _seed_long_history(self.db, n_rounds=40, row_chars=100)
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            cold = dh.build_daily_window_context(
                chat_id='hotpath',
                daily_context=ctx,
                static_system='S',
                is_cold=True,
                db_path=self.db,
                history_token_budget=500,
            )
        self.assertTrue(cold['manifest']['cold_history_trimmed'])
        tip = ids[-1]
        dc.advance_resident_history_cursor(ctx_id, gen, tip, db_path=self.db)
        new_u = _insert(self.db, 'hayana', 'hot-delta-user', '2026-07-27 09:50:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            hot = dh.build_daily_window_context(
                chat_id='hotpath',
                daily_context=dc.get_daily_context_by_id(ctx_id, db_path=self.db),
                current_user_message_id=new_u,
                static_system='S',
                is_cold=False,
                db_path=self.db,
            )
        hot_ids = [m['message_id'] for m in hot['current_day_history']]
        self.assertEqual(hot_ids, [])
        self.assertFalse(hot['manifest'].get('handoff_injected_this_turn'))
        self.assertFalse(hot['manifest'].get('carryover_injected_this_turn'))
        self.assertNotIn('cold_history_trimmed', hot['manifest'])

    def test_t9_db_history_and_membership_untouched(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        ctx = _ctx(self.db, chat_id='dbintact')
        ids = _seed_long_history(self.db, n_rounds=30, row_chars=120)
        before = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages'
        ).fetchone()[0]
        membership_before = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_message_contexts'
        ).fetchone()[0]
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            built = dh.build_daily_window_context(
                chat_id='dbintact',
                daily_context=ctx,
                static_system='S',
                is_cold=True,
                db_path=self.db,
                history_token_budget=400,
            )
        self.assertTrue(built['manifest']['cold_history_trimmed'])
        after = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages'
        ).fetchone()[0]
        membership_after = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_message_contexts'
        ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(membership_before, membership_after)
        # Oldest message still present in DB
        row = sqlite3.connect(self.db).execute(
            'SELECT content FROM chat_messages WHERE id=?', (ids[0],),
        ).fetchone()
        self.assertIsNotNone(row)


class DailyColdFenceTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        self.ctx = _ctx(self.db, chat_id='fence')
        self.send_calls = []

    def tearDown(self):
        os.unlink(self.db)
        dr.reset_bindings_for_tests()

    def _fake_resident(self, **kwargs):
        class R:
            pass
        r = R()
        r.generation = kwargs.get('generation', 2)
        r.last_cold_bootstrap_estimate = kwargs.get('last_cold_bootstrap_estimate', 0)
        r.last_cold_bootstrap_generation = kwargs.get('last_cold_bootstrap_generation', 0)
        r.hard_context_pre_spawn_turns = kwargs.get('hard_context_pre_spawn_turns', None)
        r.pending_respawn_reason = kwargs.get('pending_respawn_reason', None)
        r.session_id = 's'
        r.last_state_snapshot = {}
        calls = self.send_calls

        def ensure_alive(*a, **k):
            return True

        def note_cold_bootstrap_estimate(est):
            r.last_cold_bootstrap_estimate = int(est or 0)
            r.last_cold_bootstrap_generation = int(r.generation or 0)

        def clear_hard_context_pre_spawn_turns():
            r.hard_context_pre_spawn_turns = None

        def send_turn(content, commit_meta=None, turn_lease=None):
            calls.append(content)
            yield ('done', ('ok', '', {'v': 2, 'provider': 'claude_code'}, {}))

        r.ensure_alive = ensure_alive
        r.note_cold_bootstrap_estimate = note_cold_bootstrap_estimate
        r.clear_hard_context_pre_spawn_turns = clear_hard_context_pre_spawn_turns
        r.send_turn = send_turn
        r._kill = lambda quiet=True: None
        return r

    def _make_cold_plan(self, *, history_budget=500, user_text='CURRENT'):
        ids = _seed_long_history(self.db, n_rounds=40, row_chars=120)
        uid = _insert(self.db, 'hayana', user_text, '2026-07-27 09:59:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch('config_store.get_int', side_effect=lambda k, d=0: {
                 'HISTORY_TOKEN_BUDGET': history_budget,
                 'CC_CONTEXT_HARD_LIMIT': 120_000,
                 'CC_CONTEXT_SOFT_LIMIT': 90_000,
                 'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 8000,
             }.get(k, d)):
            assembly = dh.build_daily_window_context(
                chat_id='fence',
                daily_context=self.ctx,
                current_user_message_id=uid,
                static_system='STATIC',
                is_cold=True,
                db_path=self.db,
            )
        plan = dr.DailyTurnPlan(
            request_id='req',
            chat_id='fence',
            local_day='2026-07-27',
            context_id=int(self.ctx['id']),
            context_epoch=int(self.ctx['context_epoch']),
            resident_generation=int(self.ctx['resident_generation']),
            resident_key='fence:1:1',
            user_message_id=uid,
            epoch_token={
                'chat_id': 'fence',
                'context_id': int(self.ctx['id']),
                'context_epoch': int(self.ctx['context_epoch']),
                'resident_generation': int(self.ctx['resident_generation']),
            },
            lease_owner='test',
            is_cold=True,
            is_respawn=False,
            cursor_before=None,
            assembly=assembly,
            manifest=dict(assembly.get('manifest') or {}),
            user_content=user_text,
            db_path=self.db,
            turn_lease=issue_turn_lease(
                turn_id='req',
                turn_mode='chat',
                issued_from='default_policy',
            ),
        )
        return plan, ids

    def test_t5_75k_is_allowed_below_90k_target(self):
        plan, _ids = self._make_cold_plan(history_budget=24_000)
        resident = self._fake_resident()
        with mock.patch(
            'chat.cold_bootstrap_budget.estimate_whole_prompt',
            return_value=75_000,
        ) as estimate, mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard',
            return_value=70_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90_000,
        ), mock.patch.object(dr, '_registered_generation_requires_respawn', return_value=False), \
           mock.patch.object(dr, 'verify_epoch_token'), \
           mock.patch.object(dr.LeaseHeartbeat, 'start'), \
           mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False), \
           mock.patch.object(dr, '_capture_transcript_start'), \
           mock.patch.object(dr, '_capture_transcript_end'):
            list(dr.ensure_resident_and_stream(
                plan, resident=resident, env={}, static_system='STATIC',
            ))
        self.assertEqual(estimate.call_count, 1)
        self.assertEqual(len(self.send_calls), 1)
        self.assertFalse(plan.manifest.get('cold_budget_overflow'))
        self.assertNotIn('cold_rebuild_guard_overflow', plan.manifest)
        self.assertEqual(plan.manifest.get('cold_prompt_target'), 90_000)


    def test_t5_guard_value_does_not_change_75k_admission(self):
        for guard_value in (60_000, 70_000, 80_000):
            with self.subTest(guard=guard_value):
                plan, _ids = self._make_cold_plan(history_budget=24_000)
                resident = self._fake_resident()
                with mock.patch(
                    'chat.cold_bootstrap_budget.estimate_whole_prompt',
                    return_value=75_000,
                ) as estimate, mock.patch(
                    'chat.cold_bootstrap_budget.cold_rebuild_guard',
                    return_value=guard_value,
                ), mock.patch(
                    'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
                    return_value=90_000,
                ), mock.patch.object(
                    dr,
                    '_registered_generation_requires_respawn',
                    return_value=False,
                ), mock.patch.object(dr, 'verify_epoch_token'),                    mock.patch.object(dr.LeaseHeartbeat, 'start'),                    mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False),                    mock.patch.object(dr, '_capture_transcript_start'),                    mock.patch.object(dr, '_capture_transcript_end'):
                    list(dr.ensure_resident_and_stream(
                        plan, resident=resident, env={}, static_system='STATIC',
                    ))
                self.assertEqual(estimate.call_count, 1)
                self.assertEqual(len(self.send_calls), 1)
                self.assertFalse(plan.manifest.get('cold_budget_overflow'))
                self.assertNotIn('cold_rebuild_guard_overflow', plan.manifest)
                self.assertEqual(plan.manifest.get('cold_prompt_target'), 90_000)
                self.send_calls.clear()

    def test_t6_whole_prompt_rebuild_shrinks_history(self):
        plan, _ids = self._make_cold_plan(history_budget=24_000)
        resident = self._fake_resident()
        # Force first estimate over target, then rebuild with tiny budget.
        estimates = {'n': 0}

        def fake_estimate(system, content):
            estimates['n'] += 1
            if estimates['n'] == 1:
                return 91_000
            return 60_000

        with mock.patch('chat.cold_bootstrap_budget.estimate_whole_prompt', side_effect=fake_estimate), \
             mock.patch('chat.cold_bootstrap_budget.cold_rebuild_guard', return_value=70_000), \
             mock.patch(
                 'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
                 return_value=90_000,
             ), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch('config_store.get_int', side_effect=lambda k, d=0: {
                 'HISTORY_TOKEN_BUDGET': 24_000,
                 'CC_CONTEXT_HARD_LIMIT': 120_000,
                 'CC_CONTEXT_SOFT_LIMIT': 90_000,
                 'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 8000,
             }.get(k, d)), \
             mock.patch.object(dr, '_registered_generation_requires_respawn', return_value=False), \
             mock.patch.object(dr, 'verify_epoch_token'), \
             mock.patch.object(dr.LeaseHeartbeat, 'start'), \
             mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False), \
             mock.patch.object(dr, '_capture_transcript_start'), \
             mock.patch.object(dr, '_capture_transcript_end'):
            events = list(dr.ensure_resident_and_stream(
                plan, resident=resident, env={}, static_system='STATIC',
            ))
        self.assertEqual(estimates['n'], 2)
        self.assertEqual(len(self.send_calls), 1)
        self.assertTrue(any(e == 'done' for e, _ in events))
        self.assertLessEqual(plan.manifest['cold_history_budget'], 24_000)
        self.assertFalse(plan.manifest.get('cold_budget_overflow'))
        self.assertFalse(plan.manifest.get('cold_budget_overflow'))
        self.assertNotIn('cold_rebuild_guard_overflow', plan.manifest)
        self.assertEqual(plan.manifest.get('cold_prompt_target'), 90_000)

    def test_t7_unshrinkable_fails_before_send(self):
        plan, _ids = self._make_cold_plan(history_budget=24_000)
        resident = self._fake_resident()
        with mock.patch(
            'chat.cold_bootstrap_budget.estimate_whole_prompt', return_value=91_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard', return_value=70_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90_000,
        ), mock.patch(
            'chat.daily_history._build_state_text', return_value=('', 'none', {}),
        ), mock.patch(
            'config_store.get_int',
            side_effect=lambda k, d=0: {
                'HISTORY_TOKEN_BUDGET': 24_000,
                'CC_CONTEXT_HARD_LIMIT': 120_000,
                'CC_CONTEXT_SOFT_LIMIT': 90_000,
                'CC_COLD_BOOTSTRAP_SAFETY_MARGIN': 8000,
            }.get(k, d),
        ), mock.patch.object(dr, '_registered_generation_requires_respawn', return_value=False), \
           mock.patch.object(dr, 'verify_epoch_token'), \
           mock.patch.object(dr.LeaseHeartbeat, 'start'), \
           mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False):
            with self.assertRaises(ColdBootstrapOverflow) as ctx:
                list(dr.ensure_resident_and_stream(
                    plan, resident=resident, env={}, static_system='S' * 400,
                ))
        self.assertEqual(ctx.exception.error_code, 'cold_bootstrap_overflow')
        self.assertTrue(ctx.exception.usage.get('cold_budget_overflow'))
        self.assertFalse(ctx.exception.usage.get('cold_rebuild_guard_overflow', False))
        self.assertEqual(ctx.exception.usage.get('cold_prompt_target'), 90_000)
        self.assertEqual(self.send_calls, [])
        cursor = dc.get_resident_history_cursor(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNone(cursor)

    def test_t8_hard_context_no_benefit_and_genuine_growth(self):
        plan, _ids = self._make_cold_plan(history_budget=800)
        # Immediate post-cold storm: refuse
        resident = self._fake_resident(
            generation=2,
            last_cold_bootstrap_estimate=50_000,
            last_cold_bootstrap_generation=1,
            hard_context_pre_spawn_turns=1,
            pending_respawn_reason='hard_context',
        )
        with mock.patch(
            'chat.cold_bootstrap_budget.estimate_whole_prompt', return_value=50_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard', return_value=70_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90_000,
        ), mock.patch.object(dr, '_registered_generation_requires_respawn', return_value=False), \
           mock.patch.object(dr, 'verify_epoch_token'), \
           mock.patch.object(dr.LeaseHeartbeat, 'start'), \
           mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False):
            with self.assertRaises(NoBenefitRespawnError):
                list(dr.ensure_resident_and_stream(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
        self.assertEqual(self.send_calls, [])

        # Genuine hot growth: allow
        self.send_calls.clear()
        resident2 = self._fake_resident(
            generation=12,
            last_cold_bootstrap_estimate=50_000,
            last_cold_bootstrap_generation=1,
            hard_context_pre_spawn_turns=15,
            pending_respawn_reason='hard_context',
        )
        with mock.patch(
            'chat.cold_bootstrap_budget.estimate_whole_prompt', return_value=8_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard', return_value=70_000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90_000,
        ), mock.patch.object(dr, '_registered_generation_requires_respawn', return_value=False), \
           mock.patch.object(dr, 'verify_epoch_token'), \
           mock.patch.object(dr.LeaseHeartbeat, 'start'), \
           mock.patch.object(dr.LeaseHeartbeat, 'stop', return_value=False), \
           mock.patch.object(dr, '_capture_transcript_start'), \
           mock.patch.object(dr, '_capture_transcript_end'):
            list(dr.ensure_resident_and_stream(
                plan, resident=resident2, env={}, static_system='STATIC',
            ))
        self.assertEqual(len(self.send_calls), 1)


class IsolationFromClassicPathTests(unittest.TestCase):
    def test_t10_classic_cold_budget_module_unchanged_for_rewrite(self):
        # Smoke: classic helpers still importable and used by gateway classic path.
        from chat.cold_bootstrap_budget import (
            cold_prompt_target,
            effective_history_budget,
            should_refuse_no_benefit_hard_context_respawn,
        )
        self.assertEqual(effective_history_budget(
            default_history_budget=24000, non_history_estimate=90000, cold_target=70000,
        ), 1)
        self.assertGreater(cold_prompt_target(), 0)
        self.assertFalse(should_refuse_no_benefit_hard_context_respawn(
            pre_spawn_turns=15,
            cold_prompt_estimate=80000,
            last_cold_bootstrap_estimate=70000,
            last_cold_bootstrap_generation=1,
            current_generation=12,
        ))


class CanonicalWakeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        self.ctx = _ctx(self.db, chat_id='canonical-wake')

    def tearDown(self):
        os.unlink(self.db)

    def _build(self, *, cold: bool):
        return dh.build_daily_window_context(
            chat_id='canonical-wake',
            daily_context=self.ctx,
            static_system='STATIC',
            is_cold=cold,
            db_path=self.db,
            history_token_budget=24_000,
        )

    def test_cold_rebuild_restores_wake_in_id_order_once_as_assistant(self):
        _insert(self.db, 'hayana', 'chat-user-1', '2026-07-27 05:00:00')
        _insert(self.db, 'fyodor', 'chat-asst-1', '2026-07-27 05:01:00')
        wake_id = _insert(
            self.db, 'fyodor', 'wake-one', '2026-07-27 05:02:00',
            source_kind='wake',
            cache_info=_NORMAL_WAKE_CACHE_INFO,
        )
        _insert(self.db, 'hayana', 'chat-user-2', '2026-07-27 05:03:00')
        _insert(self.db, 'fyodor', 'chat-asst-2', '2026-07-27 05:04:00')

        history = self._build(cold=True)['current_day_history']

        self.assertEqual(
            [(m['message_id'], m['role'], m['content']) for m in history],
            [
                (1, 'user', 'chat-user-1'),
                (2, 'assistant', 'chat-asst-1'),
                (wake_id, 'assistant', 'wake-one'),
                (4, 'user', 'chat-user-2'),
                (5, 'assistant', 'chat-asst-2'),
            ],
        )
        self.assertEqual(sum(m['content'] == 'wake-one' for m in history), 1)

    def test_hot_path_does_not_replay_durable_wake(self):
        _insert(self.db, 'hayana', 'chat-user-1', '2026-07-27 05:00:00')
        cursor_id = _insert(self.db, 'fyodor', 'chat-asst-1', '2026-07-27 05:01:00')
        _insert(
            self.db, 'fyodor', 'wake-one', '2026-07-27 05:02:00',
            source_kind='wake',
            cache_info=_NORMAL_WAKE_CACHE_INFO,
        )
        _insert(self.db, 'hayana', 'chat-user-2', '2026-07-27 05:03:00')
        _insert(self.db, 'fyodor', 'chat-asst-2', '2026-07-27 05:04:00')
        dc.advance_resident_history_cursor(
            int(self.ctx['id']), int(self.ctx['resident_generation']),
            cursor_id, db_path=self.db,
        )

        history = self._build(cold=False)['current_day_history']

        self.assertEqual([m['content'] for m in history], ['chat-user-2', 'chat-asst-2'])
        self.assertNotIn('wake-one', [m['content'] for m in history])

    def test_failed_wake_has_no_canonical_row(self):
        _insert(self.db, 'hayana', 'chat-user-1', '2026-07-27 05:00:00')
        _insert(self.db, 'fyodor', 'chat-asst-1', '2026-07-27 05:01:00')

        history = self._build(cold=True)['current_day_history']

        self.assertEqual([m['content'] for m in history], ['chat-user-1', 'chat-asst-1'])
        self.assertFalse(any(m['role'] == 'assistant' and m['content'].startswith('wake-') for m in history))

    def test_consecutive_wakes_keep_order_without_fake_user(self):
        _insert(self.db, 'hayana', 'chat-user-1', '2026-07-27 05:00:00')
        _insert(self.db, 'fyodor', 'chat-asst-1', '2026-07-27 05:01:00')
        wake_one = _insert(
            self.db, 'fyodor', 'wake-one', '2026-07-27 05:02:00',
            source_kind='wake',
            cache_info=_NORMAL_WAKE_CACHE_INFO,
        )
        wake_two = _insert(
            self.db, 'fyodor', 'wake-two', '2026-07-27 05:03:00',
            source_kind='wake',
            cache_info=_NORMAL_WAKE_CACHE_INFO,
        )

        history = self._build(cold=True)['current_day_history']

        self.assertEqual([m['content'] for m in history], ['chat-user-1', 'chat-asst-1', 'wake-one', 'wake-two'])
        self.assertEqual([m['role'] for m in history[-2:]], ['assistant', 'assistant'])
        self.assertEqual([m['message_id'] for m in history[-2:]], [wake_one, wake_two])

    def test_control_rows_never_enter_canonical_history(self):
        _insert(
            self.db, 'system', 'selected_intent Planner Gate Settlement',
            '2026-07-27 05:00:00', source_kind='system',
        )
        _insert(
            self.db, 'fyodor', 'wake-rendered-final',
            '2026-07-27 05:01:00',
            source_kind='wake',
            cache_info=_NORMAL_WAKE_CACHE_INFO,
        )
        _insert(
            self.db, 'fyodor', 'wake-nightwatch',
            '2026-07-27 05:02:00',
            source_kind='wake',
            cache_info={
                'wake_mode': 'nightwatch',
                'canonical_chat_history': True,
            },
        )
        _insert(
            self.db, 'fyodor', 'wake-malformed',
            '2026-07-27 05:03:00',
            source_kind='wake',
            cache_info='{not-json',
        )
        _insert(
            self.db, 'fyodor', 'wake-missing-provenance',
            '2026-07-27 05:04:00',
            source_kind='wake',
            cache_info={
                'provider': 'claude_code',
                'source': 'wake',
                'b3_authority': True,
                'wake_mode': 'normal',
                'canonical_chat_history': True,
            },
        )

        history = self._build(cold=True)['current_day_history']
        contents = '\n'.join(m['content'] for m in history)

        self.assertEqual(contents, 'wake-rendered-final')
        for marker in (
            'selected_intent', 'Planner', 'Gate', 'Settlement',
            'wake-nightwatch', 'wake-malformed', 'wake-missing-provenance',
        ):
            self.assertNotIn(marker, contents)


if __name__ == '__main__':
    unittest.main()
