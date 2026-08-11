"""P-CONTEXT-LEAN-C2: Handoff durable facts vs recent-scene ownership.

Cases A–D only. Does not change C1 ownership semantics or Handoff storage schema.
"""
from __future__ import annotations

import datetime
import hashlib
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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-c2-handoff-scene.db'),
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
FORGE_SESSION = 'claude-forge-session-c2'


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


def _handoff_body(*, source_day: str, source_epoch: int, boundary_message_id: int) -> dict:
    return {
        'source_day': source_day,
        'source_epoch': source_epoch,
        'boundary_message_id': boundary_message_id,
        'topics': ['猫窝'],
        'confirmed_facts': ['版本已部署'],
        'decisions': ['决定使用方案 A'],
        'open_loops': ['还需完成 B'],
        'explicit_user_requests': ['不要修改 C'],
        'last_topic': '继续讨论猫窝',
    }


def _store_ready_handoff(db: str, *, chat_id: str, src, tgt, source_message_id: int = 1) -> None:
    content = _handoff_body(
        source_day=str(src['local_day']),
        source_epoch=int(src['context_epoch']),
        boundary_message_id=int(tgt['boundary_message_id']),
    )
    mid = int(source_message_id)
    dc.store_day_handoff(
        chat_id=chat_id,
        source_day=str(src['local_day']),
        content=content,
        boundary_message_id=int(tgt['boundary_message_id']),
        source_first_message_id=mid,
        source_last_message_id=mid,
        source_message_count=1,
        source_sha256=hashlib.sha256(f'c2-{chat_id}'.encode()).hexdigest(),
        source_epoch=int(src['context_epoch']),
        db_path=db,
    )


def _assert_durable_only_prompt(prompt: str) -> None:
    for key in ('source_day', 'confirmed_facts', 'decisions', 'open_loops', 'explicit_user_requests'):
        assert f'{key}' in prompt or key + ':' in prompt, key
    assert '版本已部署' in prompt
    assert '决定使用方案 A' in prompt
    assert '还需完成 B' in prompt
    assert '不要修改 C' in prompt
    # Scene + identity omitted (not even empty labels)
    assert 'topics' not in prompt
    assert 'last_topic' not in prompt
    assert '猫窝' not in prompt
    assert '继续讨论猫窝' not in prompt
    assert 'source_epoch' not in prompt
    assert 'boundary_message_id' not in prompt


def _assert_durable_plus_scene_prompt(prompt: str) -> None:
    assert 'confirmed_facts' in prompt
    assert 'topics' in prompt
    assert 'last_topic' in prompt
    assert '猫窝' in prompt
    assert '继续讨论猫窝' in prompt
    assert 'source_epoch' not in prompt
    assert 'boundary_message_id' not in prompt


class FormatterProjectionUnitTests(unittest.TestCase):
    def test_fields_none_keeps_full_keys(self):
        data = _handoff_body(source_day='2026-08-10', source_epoch=1, boundary_message_id=9)
        text = dc.format_formal_handoff_prompt(data)
        for key in dc.HANDOFF_CONTENT_KEYS:
            self.assertIn(key, text)

    def test_fields_omits_keys_entirely(self):
        data = _handoff_body(source_day='2026-08-10', source_epoch=1, boundary_message_id=9)
        text = dc.format_formal_handoff_prompt(
            data, fields=dc.HANDOFF_PROVIDER_DURABLE_FIELDS,
        )
        self.assertIn('source_day', text)
        self.assertNotIn('topics', text)
        self.assertNotIn('last_topic', text)
        self.assertNotIn('source_epoch', text)
        self.assertNotIn('boundary_message_id', text)


class ColdHandoffSceneOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def _build(self, ctx, *, is_cold=True, is_respawn=False, provider_sid=None):
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
                inject_handoff=True,
                inject_carryover=True,
                db_path=self.db,
                provider_claude_session_id=provider_sid,
            )

    def test_case_a_legacy_carryover_handoff_durable_only(self):
        # Prev-day rounds for carryover selection
        base = datetime.datetime(2026, 8, 10, 11, 0, 0)
        for i in range(3):
            ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
            ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
            _insert(self.db, 'hayana', f'prev-U{i}', ts_u)
            _insert(self.db, 'fyodor', f'prev-A{i}', ts_a)

        src = dc.get_or_create_daily_context(
            chat_id='c2a', local_day='2026-08-10', db_path=self.db,
            now=_FIXED_NOW, allow_backfill=True,
        )
        tgt = dc.get_or_create_daily_context(
            chat_id='c2a', local_day='2026-08-11', db_path=self.db, now=_FIXED_NOW,
        )
        dc.select_carryover(int(tgt['id']), 3, db_path=self.db)
        _store_ready_handoff(self.db, chat_id='c2a', src=src, tgt=tgt)
        refreshed = dc.get_daily_context_by_id(int(tgt['id']), db_path=self.db)

        built = self._build(refreshed, is_cold=True)
        m = built['manifest']
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_CARRYOVER)
        self.assertTrue(m['carryover_injected_this_turn'])
        self.assertEqual(m['handoff_projection'], dh.HANDOFF_PROJECTION_DURABLE_ONLY)
        self.assertEqual(m['handoff_scene_owner'], dh.HANDOFF_SCENE_OWNER_CARRYOVER)
        self.assertTrue(m['handoff_injected_this_turn'])
        _assert_durable_only_prompt(built['day_handoff'])
        # Stored content still full
        stored = built['day_handoff_content']
        self.assertIsNotNone(stored)
        self.assertIn('topics', stored)
        self.assertIn('last_topic', stored)
        self.assertIn('source_epoch', stored)
        kinds = [layer.get('kind') for layer in built['layers']]
        self.assertIn('carryover', kinds)
        self.assertIn('day_handoff', kinds)

    def test_case_b_live_forge_owner_handoff_durable_only(self):
        # Prev-day message sets a non-zero boundary for handoff binding.
        prev_mid = _insert(self.db, 'hayana', 'prev', '2026-08-10 11:00:00')
        _insert(self.db, 'fyodor', 'prev reply', '2026-08-10 11:01:00')

        # Selected forge carryover rows (same-day formal rounds)
        selected = []
        base = datetime.datetime(2026, 8, 11, 5, 0, 0)
        for i in range(3):
            ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
            ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
            selected.append(_insert(self.db, 'hayana', f'U{i}', ts_u))
            selected.append(_insert(self.db, 'fyodor', f'A{i}', ts_a))

        src = dc.get_or_create_daily_context(
            chat_id='c2b', local_day='2026-08-10', db_path=self.db,
            now=_FIXED_NOW, allow_backfill=True,
        )
        day_tgt = dc.get_or_create_daily_context(
            chat_id='c2b', local_day='2026-08-11', db_path=self.db, now=_FIXED_NOW,
        )
        _store_ready_handoff(
            self.db, chat_id='c2b', src=src, tgt=day_tgt, source_message_id=prev_mid,
        )

        now_s = _FIXED_NOW.strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        refreshed_day = dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (int(day_tgt['id']),),
        ).fetchone())
        # Prefer binding the same READY handoff via previous-day lookup; set handoff_id if present.
        hid_row = conn.execute(
            "SELECT id FROM day_handoffs WHERE chat_id=? AND source_day=? AND status='READY' "
            "ORDER BY id DESC LIMIT 1",
            ('c2b', '2026-08-10'),
        ).fetchone()
        hid = int(hid_row['id']) if hid_row else None
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, carryover_requested_count,
                selection_finalized_at, is_backfill, resident_generation, version,
                created_at, updated_at, window_mode, opened_at,
                source_context_id, switch_request_id, claude_session_id, handoff_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                'c2b', '2026-08-11', 'Asia/Shanghai', 4, 3,
                int(refreshed_day['boundary_message_id']), 'active', 3, 3, now_s,
                now_s, now_s, 'manual', now_s,
                int(src['id']), 'sw-c2b', FORGE_SESSION, hid,
            ),
        )
        forge_id = int(cur.lastrowid)
        for ordinal, mid in enumerate(selected):
            conn.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                'VALUES (?,?,?)',
                (forge_id, ordinal, int(mid)),
            )
        conn.commit()
        forge_ctx = dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (forge_id,),
        ).fetchone())
        conn.close()

        built = self._build(
            forge_ctx, is_cold=True, provider_sid=FORGE_SESSION,
        )
        m = built['manifest']
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_FORGE)
        self.assertFalse(m['carryover_injected_this_turn'])
        self.assertEqual(m['handoff_projection'], dh.HANDOFF_PROJECTION_DURABLE_ONLY)
        self.assertEqual(m['handoff_scene_owner'], dh.HANDOFF_SCENE_OWNER_FORGE)
        self.assertTrue(m['handoff_injected_this_turn'])
        _assert_durable_only_prompt(built['day_handoff'])
        kinds = [layer.get('kind') for layer in built['layers']]
        self.assertNotIn('carryover', kinds)
        self.assertIn('day_handoff', kinds)

    def test_case_c_no_recent_scene_handoff_fallback(self):
        prev_mid = _insert(self.db, 'hayana', 'prev', '2026-08-10 11:00:00')
        _insert(self.db, 'fyodor', 'prev reply', '2026-08-10 11:01:00')
        src = dc.get_or_create_daily_context(
            chat_id='c2c', local_day='2026-08-10', db_path=self.db,
            now=_FIXED_NOW, allow_backfill=True,
        )
        tgt = dc.get_or_create_daily_context(
            chat_id='c2c', local_day='2026-08-11', db_path=self.db, now=_FIXED_NOW,
        )
        dc.finalize_zero_carryover(int(tgt['id']), db_path=self.db)
        _store_ready_handoff(
            self.db, chat_id='c2c', src=src, tgt=tgt, source_message_id=prev_mid,
        )
        refreshed = dc.get_daily_context_by_id(int(tgt['id']), db_path=self.db)

        built = self._build(refreshed, is_cold=True)
        m = built['manifest']
        self.assertEqual(m['cold_recent_owner'], dh.COLD_RECENT_OWNER_NONE)
        self.assertFalse(m['carryover_injected_this_turn'])
        self.assertEqual(m['handoff_projection'], dh.HANDOFF_PROJECTION_DURABLE_PLUS_SCENE)
        self.assertEqual(m['handoff_scene_owner'], dh.HANDOFF_SCENE_OWNER_HANDOFF)
        self.assertTrue(m['handoff_injected_this_turn'])
        _assert_durable_plus_scene_prompt(built['day_handoff'])

    def test_case_d_no_ready_handoff(self):
        tgt = dc.get_or_create_daily_context(
            chat_id='c2d', local_day='2026-08-11', db_path=self.db, now=_FIXED_NOW,
        )
        dc.finalize_zero_carryover(int(tgt['id']), db_path=self.db)
        refreshed = dc.get_daily_context_by_id(int(tgt['id']), db_path=self.db)
        built = self._build(refreshed, is_cold=True)
        m = built['manifest']
        self.assertFalse(m['handoff_injected_this_turn'])
        self.assertEqual(m['handoff_projection'], dh.HANDOFF_PROJECTION_NONE)
        self.assertEqual(m['handoff_scene_owner'], dh.HANDOFF_SCENE_OWNER_NONE)
        self.assertEqual(built['day_handoff'], '')
        kinds = [layer.get('kind') for layer in built['layers']]
        self.assertNotIn('day_handoff', kinds)


if __name__ == '__main__':
    unittest.main()
