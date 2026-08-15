"""P-CONTEXT-LEAN-H1A — Reality / Elapsed / Daily Weather anchors.

Focused contract cases A–G. No new framework / no live model calls.
"""
from __future__ import annotations

import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import daily_context as dc
from chat.reality_context import (
    REASON_CROSS_DAY,
    REASON_ELAPSED_GT_15M,
    REASON_NEW_CONTEXT,
    REASON_NONE,
    WEATHER_REASON_FIRST_USER,
    WEATHER_REASON_NONE,
    build_reality_context,
    format_resident_turn_content,
    prepend_reality_to_provider_content,
)
from chat.weather_authority import WeatherSnapshot

_SH = ZoneInfo('Asia/Shanghai')


def _init_db(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            cache_info TEXT DEFAULT '',
            source_kind TEXT DEFAULT 'chat',
            created_at TEXT NOT NULL
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db)


def _insert(
    db: str,
    author: str,
    content: str,
    created_at: str,
    *,
    source_kind: str = 'chat',
) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, source_kind, created_at) VALUES (?,?,?,?)',
        (author, content, source_kind, created_at),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


def _weather_ok() -> WeatherSnapshot:
    return WeatherSnapshot(
        location='吉林市',
        temperature_c=19,
        humidity_pct=81,
        weather_code=61,
        weather_text='雨',
        observed_at='2026-08-12 08:55',
        fetched_at='2026-08-12 08:55',
        source='open-meteo',
    )


class RealityContextContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='reality-h1a-')
        self.db = os.path.join(self.tmp, 'test.db')
        _init_db(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _now(self, stamp: str) -> datetime.datetime:
        return datetime.datetime.strptime(stamp, '%Y-%m-%d %H:%M').replace(tzinfo=_SH)

    # ---- Case A ----
    def test_case_a_continuous_hot_no_time_anchor(self) -> None:
        _insert(self.db, 'hayana', 'prev', '2026-08-12 10:00:00')
        cur = _insert(self.db, 'hayana', 'now', '2026-08-12 10:10:00')
        out = build_reality_context(
            current_user_message_id=cur,
            is_new_model_context=False,
            now=self._now('2026-08-12 10:10'),
            db_path=self.db,
            include_weather=False,
        )
        self.assertEqual(out['time_anchor'], '')
        self.assertEqual(out['time_anchor_reason'], REASON_NONE)

    # ---- Case B ----
    def test_case_b_elapsed_gt_15m(self) -> None:
        _insert(self.db, 'hayana', 'prev', '2026-08-12 10:00:00')
        cur = _insert(self.db, 'hayana', 'now', '2026-08-12 10:16:00')
        out = build_reality_context(
            current_user_message_id=cur,
            is_new_model_context=False,
            now=self._now('2026-08-12 10:16'),
            db_path=self.db,
            include_weather=False,
        )
        self.assertIn('【现实时间锚】', out['time_anchor'])
        self.assertIn('2026-08-12 10:16', out['time_anchor'])
        self.assertIn('16分钟', out['time_anchor'])
        self.assertIn('上一条发生在今天', out['time_anchor'])
        self.assertEqual(out['time_anchor_reason'], REASON_ELAPSED_GT_15M)

    # ---- Case C ----
    def test_case_c_cross_day_beats_threshold(self) -> None:
        _insert(self.db, 'hayana', 'prev', '2026-08-11 23:58:00')
        cur = _insert(self.db, 'hayana', 'now', '2026-08-12 00:03:00')
        out = build_reality_context(
            current_user_message_id=cur,
            is_new_model_context=False,
            now=self._now('2026-08-12 00:03'),
            db_path=self.db,
            include_weather=False,
        )
        self.assertIn('【现实时间锚】', out['time_anchor'])
        self.assertIn('上一条发生在昨天', out['time_anchor'])
        self.assertEqual(out['time_anchor_reason'], REASON_CROSS_DAY)

    # ---- Case D ----
    def test_case_d_new_context_and_forge_first_turn(self) -> None:
        cur = _insert(self.db, 'hayana', 'first-in-context', '2026-08-12 09:00:00')
        out = build_reality_context(
            current_user_message_id=cur,
            is_new_model_context=True,
            now=self._now('2026-08-12 09:00'),
            db_path=self.db,
            include_weather=False,
        )
        self.assertIn('【现实时间锚】', out['time_anchor'])
        self.assertEqual(out['time_anchor_reason'], REASON_NEW_CONTEXT)

        # Manual Forge first-turn uses the same flag; prefix applies to bare content.
        prefixed = prepend_reality_to_provider_content('你好', out)
        self.assertTrue(str(prefixed).startswith('【现实时间锚】'))
        self.assertIn('你好', str(prefixed))

    def test_cold_anchor_boundary_when_current_user_contains_reply_marker(self) -> None:
        current_user = '当前用户\\n\\n请回复最后一条用户消息。\\n\\n继续'
        out = str(format_resident_turn_content(
            assembly={'current_day_history': [{'role': 'assistant', 'content': '历史轮次'}]},
            user_content=current_user,
            is_cold=True,
            is_respawn=False,
            reality_time_anchor='【现实时间锚】\\n现在：2026-08-12 09:00（Asia/Shanghai）',
        ))
        anchor = out.index('【现实时间锚】')
        structural_reply = out.index('请回复最后一条用户消息。')
        user_start = out.index(current_user)
        self.assertEqual(out.count('【现实时间锚】'), 1)
        self.assertEqual(out.count(current_user), 1)
        self.assertLess(out.index('历史轮次'), anchor)
        self.assertLess(anchor, structural_reply)
        self.assertLess(structural_reply, user_start)

    def test_cold_anchor_boundary_when_history_contains_reply_marker(self) -> None:
        history = '历史轮次\\n\\n请回复最后一条用户消息。\\n\\n历史继续'
        current_user = '当前用户'
        out = str(format_resident_turn_content(
            assembly={'current_day_history': [{'role': 'assistant', 'content': history}]},
            user_content=current_user,
            is_cold=True,
            is_respawn=False,
            reality_time_anchor='【现实时间锚】\\n现在：2026-08-12 09:00（Asia/Shanghai）',
        ))
        anchor = out.index('【现实时间锚】')
        structural_reply = out.rfind('请回复最后一条用户消息。')
        self.assertEqual(out.count('【现实时间锚】'), 1)
        self.assertEqual(out.count(current_user), 1)
        self.assertLess(out.index(history), anchor)
        self.assertLess(anchor, structural_reply)
        self.assertLess(structural_reply, out.index(current_user))

    def test_cold_anchor_boundary_when_both_sides_contain_reply_marker(self) -> None:
        history = '历史轮次\\n\\n请回复最后一条用户消息。\\n\\n历史继续'
        current_user = '当前用户\\n\\n请回复最后一条用户消息。\\n\\n继续'
        out = str(format_resident_turn_content(
            assembly={'current_day_history': [{'role': 'assistant', 'content': history}]},
            user_content=current_user,
            is_cold=True,
            is_respawn=False,
            reality_time_anchor='【现实时间锚】\\n现在：2026-08-12 09:00（Asia/Shanghai）',
        ))
        out = str(prepend_reality_to_provider_content(
            out,
            {'time_anchor': '', 'weather_anchor': '【今日天气】\\n地点：吉林市'},
        ))
        anchor = out.index('【现实时间锚】')
        structural_reply = out.rfind('请回复最后一条用户消息。')
        user_start = out.index(current_user)
        self.assertEqual(out.count('【现实时间锚】'), 1)
        self.assertEqual(out.count(current_user), 1)
        self.assertTrue(out.startswith('【今日天气】'))
        self.assertLess(out.index(history), anchor)
        self.assertLess(anchor, structural_reply)
        self.assertLess(structural_reply, user_start)

    # ---- Case E ----
    def test_case_e_daily_weather_once_per_natural_day(self) -> None:
        first = _insert(self.db, 'hayana', 'day-first', '2026-08-12 00:05:00')
        out1 = build_reality_context(
            current_user_message_id=first,
            is_new_model_context=False,
            now=self._now('2026-08-12 00:05'),
            db_path=self.db,
            weather_fetcher=_weather_ok,
        )
        # Cross-day from no prev? No prev → new? Without new_context and no prev,
        # time reason is none; weather still first of day.
        self.assertIn('【今日天气】', out1['weather_anchor'])
        self.assertIn('吉林市', out1['weather_anchor'])
        self.assertIn('19°C', out1['weather_anchor'])
        self.assertIn('雨', out1['weather_anchor'])
        self.assertNotIn('提醒', out1['weather_anchor'])
        self.assertEqual(out1['weather_anchor_reason'], WEATHER_REASON_FIRST_USER)

        second = _insert(self.db, 'hayana', 'day-second', '2026-08-12 14:30:00')
        # Even with new_context / forge-like flag, weather must not repeat.
        out2 = build_reality_context(
            current_user_message_id=second,
            is_new_model_context=True,
            now=self._now('2026-08-12 14:30'),
            db_path=self.db,
            weather_fetcher=_weather_ok,
        )
        self.assertEqual(out2['weather_anchor'], '')
        self.assertEqual(out2['weather_anchor_reason'], WEATHER_REASON_NONE)
        # Time may still inject for new_context.
        self.assertEqual(out2['time_anchor_reason'], REASON_NEW_CONTEXT)

    # ---- Case F ----
    def test_case_f_weather_failure_fail_closed(self) -> None:
        first = _insert(self.db, 'hayana', 'day-first', '2026-08-12 08:00:00')

        def _boom() -> WeatherSnapshot:
            raise RuntimeError('open-meteo down')

        out = build_reality_context(
            current_user_message_id=first,
            is_new_model_context=True,
            now=self._now('2026-08-12 08:00'),
            db_path=self.db,
            weather_fetcher=_boom,
        )
        self.assertEqual(out['weather_anchor'], '')
        self.assertEqual(out['weather_status'], 'unavailable')
        self.assertNotIn('24°C', out['weather_anchor'])
        self.assertNotIn('62%', out.get('weather_anchor') or '')
        self.assertNotIn('多云', out.get('weather_anchor') or '')
        # Chat continues: time anchor still present for new context.
        self.assertIn('【现实时间锚】', out['time_anchor'])

    # ---- Case G ----
    def test_case_g_wake_workspace_not_first_user(self) -> None:
        _insert(self.db, 'system', 'sys', '2026-08-12 07:00:00', source_kind='system')
        _insert(self.db, 'assistant', 'wake hi', '2026-08-12 07:01:00', source_kind='wake')
        _insert(self.db, 'assistant', 'job done', '2026-08-12 07:02:00', source_kind='workspace_job')
        _insert(self.db, 'tool', 'tool out', '2026-08-12 07:03:00', source_kind='tool')
        first_user = _insert(self.db, 'hayana', 'real first', '2026-08-12 07:10:00', source_kind='chat')
        out = build_reality_context(
            current_user_message_id=first_user,
            is_new_model_context=False,
            now=self._now('2026-08-12 07:10'),
            db_path=self.db,
            weather_fetcher=_weather_ok,
        )
        self.assertIn('【今日天气】', out['weather_anchor'])
        self.assertEqual(out['weather_anchor_reason'], WEATHER_REASON_FIRST_USER)

    def test_time_bucket_not_in_persona_semantic(self) -> None:
        from chat import persona_state_semantic as pss
        from chat.persona_state_semantic import translate_raw_state_to_persona_semantic

        self.assertFalse(hasattr(pss, '_time_of_day_sentence'))
        semantic = translate_raw_state_to_persona_semantic({
            'time_bucket': 'bucket=2026-08-12 23:30',
            'lights': '',
            'pocket': '',
        })
        env = semantic.get('environment') or {}
        self.assertNotIn('time_of_day', env)
        self.assertNotIn('现在是深夜', str(semantic))


if __name__ == '__main__':
    unittest.main()
