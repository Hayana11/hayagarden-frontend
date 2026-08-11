"""P-CONTEXT-LEAN-C1: Manual Forge selected rounds exact-once vs Daily carryover.

COLD_EXACT_ONCE_RULE:
  suppress only when live provider still holds the Forge transcript;
  fresh spawn / unproven ownership → KEEP carryover.
"""
from __future__ import annotations

import datetime
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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-c1-forge-carryover.db'),
)

from chat import daily_context as dc
from chat import daily_history as dh


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db_path: str) -> None:
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
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


def _insert(db_path: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)',
        (author, content, created_at),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


_FIXED_NOW = datetime.datetime(2026, 8, 11, 12, 0, 0)
_real_current_chat_day = dc._current_chat_day
FORGE_SESSION = 'claude-forge-session-c1'


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


def _seed_three_rounds(db: str) -> list[int]:
    ids = []
    base = datetime.datetime(2026, 8, 11, 5, 0, 0)
    for i in range(3):
        ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
        ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
        ids.append(_insert(db, 'hayana', f'U{i}', ts_u))
        ids.append(_insert(db, 'fyodor', f'A{i}', ts_a))
    return ids


def _legacy_ctx(db: str) -> dict:
    return dc.get_or_create_daily_context(
        chat_id='c1-legacy',
        local_day='2026-08-11',
        db_path=db,
        now=_FIXED_NOW,
    )


def _forge_target_ctx(
    db: str,
    *,
    selected_ids: list[int],
    complete_identity: bool = True,
    forge_session_id: str = FORGE_SESSION,
) -> dict:
    """Create a Manual Forge target with selected carryover rows + identity evidence."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    now_s = _FIXED_NOW.strftime('%Y-%m-%d %H:%M:%S')
    source_id = 1 if complete_identity else 0
    switch_rid = 'sw-c1-req-1' if complete_identity else ''
    session_id = forge_session_id if complete_identity else ''
    cur = conn.execute(
        '''INSERT INTO daily_contexts (
            chat_id, local_day, timezone, boundary_hour, context_epoch,
            boundary_message_id, status, carryover_count, carryover_requested_count,
            selection_finalized_at, is_backfill, resident_generation, version,
            created_at, updated_at, window_mode, opened_at,
            source_context_id, switch_request_id, claude_session_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, ?, ?, ?)''',
        (
            'c1-forge', '2026-08-11', 'Asia/Shanghai', 4, 2,
            0, 'active', 3, 3, now_s,
            now_s, now_s, 'manual', now_s,
            source_id if source_id else None,
            switch_rid or None,
            session_id or None,
        ),
    )
    target_id = int(cur.lastrowid)
    for ordinal, mid in enumerate(selected_ids):
        conn.execute(
            'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
            'VALUES (?,?,?)',
            (target_id, ordinal, int(mid)),
        )
    conn.commit()
    row = dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (target_id,),
    ).fetchone())
    conn.close()
    return row


def _assert_no_carryover_layer(built: dict) -> None:
    kinds = [layer.get('kind') for layer in built.get('layers') or []]
    assert 'carryover' not in kinds, 'provider layers must not include carryover'
    assert built.get('carryover_messages') == []


def _assert_has_carryover_layer(built: dict) -> None:
    kinds = [layer.get('kind') for layer in built.get('layers') or []]
    assert 'carryover' in kinds
    assert built.get('carryover_messages')


class ForgeCarryoverOwnershipUnitTests(unittest.TestCase):
    def test_requires_live_provider_session_match(self):
        base = {
            'window_mode': 'manual',
            'source_context_id': 9,
            'switch_request_id': 'r1',
            'claude_session_id': FORGE_SESSION,
        }
        # Forge-created alone is NOT enough
        self.assertFalse(dh.forge_transcript_owns_selected_carryover(base))
        self.assertFalse(dh.forge_transcript_owns_selected_carryover(
            base, provider_claude_session_id='',
        ))
        self.assertFalse(dh.forge_transcript_owns_selected_carryover(
            base, provider_claude_session_id='fresh-other-session',
        ))
        # Live provider still on forge transcript → suppress allowed
        self.assertTrue(dh.forge_transcript_owns_selected_carryover(
            base, provider_claude_session_id=FORGE_SESSION,
        ))
        self.assertTrue(dh.forge_transcript_owns_selected_carryover(
            {**base, 'window_mode': 'manual_staged'},
            provider_claude_session_id=FORGE_SESSION,
        ))
        self.assertFalse(dh.forge_transcript_owns_selected_carryover({
            'window_mode': 'legacy_daily',
            'source_context_id': 9,
            'switch_request_id': 'r1',
            'claude_session_id': FORGE_SESSION,
        }, provider_claude_session_id=FORGE_SESSION))


class ColdForgeCarryoverDedupTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def _build(
        self,
        ctx,
        *,
        is_cold=True,
        is_respawn=False,
        inject_carryover=True,
        provider_claude_session_id=None,
    ):
        with mock.patch(
            'chat.daily_history._build_state_text',
            return_value=('', 'none', {}),
        ):
            return dh.build_daily_window_context(
                chat_id=str(ctx.get('chat_id') or 'default'),
                daily_context=ctx,
                static_system='STATIC',
                is_cold=is_cold,
                is_respawn=is_respawn,
                inject_handoff=False,
                inject_carryover=inject_carryover,
                db_path=self.db,
                provider_claude_session_id=provider_claude_session_id,
            )

    def test_case_a_live_forge_session_suppresses_carryover(self):
        """Provider still holds Forge transcript (--resume same session)."""
        selected = _seed_three_rounds(self.db)
        ctx = _forge_target_ctx(self.db, selected_ids=selected, complete_identity=True)
        rows = dc.get_selected_carryover_messages(int(ctx['id']), db_path=self.db)
        self.assertEqual(len(rows), 6)

        built = self._build(
            ctx,
            is_cold=True,
            provider_claude_session_id=FORGE_SESSION,
        )
        m = built['manifest']
        self.assertFalse(m['carryover_injected_this_turn'])
        self.assertEqual(built['carryover_messages'], [])
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_FORGE)
        self.assertEqual(
            m['carryover_suppressed_reason'],
            dh.CARRYOVER_SUPPRESSED_FORGE_OWNS,
        )
        self.assertEqual(m['carryover_message_ids'], selected)
        _assert_no_carryover_layer(built)

    def test_case_b_real_fresh_respawn_keeps_carryover(self):
        """Forge resident dead → fresh _spawn: provider no longer owns Forge transcript."""
        selected = _seed_three_rounds(self.db)
        ctx = _forge_target_ctx(self.db, selected_ids=selected, complete_identity=True)

        # No live provider session (dead resident / not yet spawned)
        built_dead = self._build(ctx, is_cold=False, is_respawn=True)
        m = built_dead['manifest']
        self.assertTrue(m['carryover_injected_this_turn'])
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_CARRYOVER)
        self.assertEqual(m['carryover_suppressed_reason'], dh.CARRYOVER_SUPPRESSED_NONE)
        _assert_has_carryover_layer(built_dead)

        # Fresh spawn session ≠ forge session
        built_fresh = self._build(
            ctx,
            is_cold=False,
            is_respawn=True,
            provider_claude_session_id='fresh-spawn-session-xyz',
        )
        m2 = built_fresh['manifest']
        self.assertTrue(m2['carryover_injected_this_turn'])
        self.assertEqual(m2['cold_recent_owner'], dh.COLD_RECENT_OWNER_CARRYOVER)
        self.assertEqual(m2['carryover_suppressed_reason'], dh.CARRYOVER_SUPPRESSED_NONE)
        _assert_has_carryover_layer(built_fresh)
        # selected_round_provider_copy_count = 1 (carryover only; forge transcript gone)
        self.assertEqual(len(built_fresh['carryover_messages']), 6)
        # DB selection evidence still present
        self.assertEqual(
            len(dc.get_selected_carryover_messages(int(ctx['id']), db_path=self.db)),
            6,
        )

    def test_case_b_resumable_same_forge_session_still_suppresses(self):
        """spawn_resumable(--resume forge session): still Exact-Once suppress."""
        selected = _seed_three_rounds(self.db)
        ctx = _forge_target_ctx(self.db, selected_ids=selected, complete_identity=True)
        built = self._build(
            ctx,
            is_cold=False,
            is_respawn=True,
            provider_claude_session_id=FORGE_SESSION,
        )
        m = built['manifest']
        self.assertFalse(m['carryover_injected_this_turn'])
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_FORGE)
        _assert_no_carryover_layer(built)

    def test_case_c_legacy_daily_carryover_still_injects(self):
        base = datetime.datetime(2026, 8, 10, 11, 0, 0)
        for i in range(3):
            ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
            ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
            _insert(self.db, 'hayana', f'prev-U{i}', ts_u)
            _insert(self.db, 'fyodor', f'prev-A{i}', ts_a)
        ctx = _legacy_ctx(self.db)
        dc.select_carryover(int(ctx['id']), 3, db_path=self.db)
        refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=self.db)
        self.assertIsNotNone(refreshed)
        built = self._build(refreshed, is_cold=True)
        m = built['manifest']
        self.assertTrue(m['carryover_injected_this_turn'])
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_CARRYOVER)
        self.assertEqual(m['carryover_suppressed_reason'], dh.CARRYOVER_SUPPRESSED_NONE)
        _assert_has_carryover_layer(built)

    def test_case_d_incomplete_identity_keeps_carryover(self):
        selected = _seed_three_rounds(self.db)
        ctx = _forge_target_ctx(
            self.db, selected_ids=selected, complete_identity=False,
        )
        self.assertFalse(dh.forge_transcript_owns_selected_carryover(
            ctx, provider_claude_session_id=FORGE_SESSION,
        ))
        built = self._build(
            ctx, is_cold=True, provider_claude_session_id=FORGE_SESSION,
        )
        m = built['manifest']
        self.assertTrue(m['carryover_injected_this_turn'])
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_CARRYOVER)
        self.assertEqual(m['carryover_suppressed_reason'], dh.CARRYOVER_SUPPRESSED_NONE)
        _assert_has_carryover_layer(built)

    def test_capacity_swap_hot_like_still_skips_carryover(self):
        """Regression lock: C1 must not invent carryover when inject_carryover=False."""
        selected = _seed_three_rounds(self.db)
        ctx = _forge_target_ctx(self.db, selected_ids=selected, complete_identity=True)
        built = self._build(
            ctx,
            is_cold=True,
            is_respawn=False,
            inject_carryover=False,
            provider_claude_session_id=FORGE_SESSION,
        )
        self.assertFalse(built['manifest']['carryover_injected_this_turn'])
        self.assertEqual(built['manifest']['cold_recent_owner'], dh.COLD_RECENT_OWNER_NONE)
        _assert_no_carryover_layer(built)


if __name__ == '__main__':
    unittest.main()
