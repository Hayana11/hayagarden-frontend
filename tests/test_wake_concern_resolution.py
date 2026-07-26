"""Regression tests for Wake concern-resolution filtering (Memory Hotfix)."""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-wake-resolution-config.db'),
)

from chat import context_continuity
from wake import concern_resolution as cr


class ConcernResolutionLogicTests(unittest.TestCase):
    def test_user_resolution_detected_not_assistant(self):
        self.assertTrue(cr.is_user_resolution('伤口已经愈合，医生说不用打破伤风了。'))
        self.assertFalse(cr.is_user_resolution('嗯嗯，我知道了。'))

    def test_negated_and_rhetorical_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('才不是没事了，你别瞎担心。'))
        self.assertFalse(cr.is_user_resolution('你以为结束了？还早着呢。'))

    def test_movie_ended_is_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('电影结束了，挺好看的。'))

    def test_reopen_removes_matching_resolution(self):
        messages = [
            {'id': 1, 'content': '被猫抓伤了，有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '已经咨询医生，伤口愈合了，不用打针。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 3, 'content': '伤口又红肿了，还是得去医院。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_reopen_without_repeat_words(self):
        messages = [
            {'id': 1, 'content': '医生说不用打针，伤口已经好了。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 2, 'content': '伤口今天开始红肿了。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_unrelated_concerns_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '超市鸡蛋打折的事已经处理好了。', 'created_at': '2026-07-21 10:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        wake_text = '还在想她需不需要打破伤风针。'
        self.assertFalse(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-20 10:00:00',
        ))

    def test_superseded_wake_requires_time_before_resolution(self):
        messages = [
            {'id': 1, 'content': '医生说不用打破伤风，伤口已经愈合。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        wake_text = '醒来还在想破伤风要不要打。'
        self.assertTrue(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-20 10:00:00',
        ))
        self.assertFalse(cr.is_superseded_historical_concern(
            wake_text, state, recorded_at='2026-07-22 10:00:00',
        ))

    def test_missing_time_fail_open(self):
        messages = [
            {'id': 1, 'content': '医生说不用打破伤风，伤口已经愈合。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想破伤风', state, recorded_at='',
        ))

    def test_deictic_resolution_uses_only_one_prior_message(self):
        messages = [
            {'id': 1, 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'content': '猫抓的伤口还在疼。', 'created_at': '2026-07-20 09:00:00'},
            {'id': 3, 'content': '这件事结束了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        entry = state.active[0]
        self.assertIn('伤口', entry.topic_tokens)
        self.assertNotIn('买花', entry.topic_tokens)

    def test_express_resolution_does_not_absorb_distant_flower_topic(self):
        messages = [
            {'id': 1, 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想买花的事情。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_guard_only_for_applied_resolutions(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '电影结束了。', 'created_at': '2026-07-21 10:00:00'},
        ])
        self.assertEqual(cr.format_resolution_guard([]), '')
        self.assertEqual(cr.format_resolution_guard(state.active), '')


class WakeConcernResolutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT, resolved INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 0
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE board (
                id INTEGER PRIMARY KEY, author TEXT, tag TEXT, content TEXT,
                level TEXT, category TEXT, status TEXT
            );
            CREATE TABLE dream_events (
                id INTEGER PRIMARY KEY, type TEXT, value TEXT, created_at TEXT,
                duration_minutes INTEGER
            );
            CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT, surfaced INTEGER DEFAULT 0,
                surface_count INTEGER DEFAULT 0, created_at TEXT
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT
            );
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL, month TEXT);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, thoughts TEXT,
                woke_at TEXT, consumed INTEGER DEFAULT 0
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _bjt_now(self):
        return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    def _ts(self, *, hours_ago: float = 0.0) -> str:
        moment = self._bjt_now() - datetime.timedelta(hours=hours_ago)
        return moment.strftime('%Y-%m-%d %H:%M:%S')

    def _insert_user(self, content, *, hours_ago: float = 1.0, author: str = 'hayana'):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            (author, content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _insert_wake(self, action, content, *, hours_ago: float = 30.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (action, content, thoughts, woke_at, consumed) VALUES (?,?,?,?,0)",
            (action, content, '', self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _insert_diary(self, content, *, hours_ago: float = 20.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO posts (type, content, layer, resolved, created_at) VALUES ('DIARY', ?, 'recent', 0, ?)",
            (content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _patch_build(self):
        from chat import system_builder
        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        return mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
            mock.patch.object(system_builder, 'build_shared_context', return_value=None), \
            mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
            mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
            mock.patch.object(system_builder.config_store, 'get_bool', return_value=False)

    def _build_wake_text(self, *, wake: bool = True):
        from chat import system_builder
        patches = self._patch_build()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            blocks = system_builder.build_system(wake=wake)
        return '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))

    def test_old_wake_suppressed_after_user_resolution(self):
        self._insert_wake('explore', '还在想她需不需要打破伤风针。', hours_ago=30)
        self._insert_user('猫抓的伤口已经愈合，医生说不必打破伤风，不用再问了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_multiple_unconsumed_wakes_still_suppressed(self):
        self._insert_wake('explore', '破伤风要不要打', hours_ago=60)
        self._insert_wake('diary', '日记里还在担心破伤风', hours_ago=58)
        self._insert_wake('none', '醒来仍觉得破伤风悬而未决', hours_ago=56)
        self._insert_user('医生说破伤风不用打针，伤口已经好了。')

        text = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text)

    def test_flower_wake_survives_unrelated_express_resolution(self):
        self._insert_user('记得提醒我买花。', hours_ago=30)
        self._insert_wake('explore', '还在想买花的事情。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('买花', text)
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('用户已明确结案', text)

    def test_post_resolution_wake_not_filtered(self):
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_wake('explore', '今天伤口开始渗液了，要不要再问医生。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('渗液', text)

    def test_post_resolution_diary_not_filtered(self):
        self._insert_user('医生说破伤风不用打针，伤口已经好了。', hours_ago=48)
        self._insert_diary('今天伤口开始红肿，要不要再问医生。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('最近的日记', text)
        self.assertIn('红肿', text)

    def test_reopen_allows_wake_concern_back(self):
        self._insert_wake('explore', '还在想她需不需要打破伤风针。', hours_ago=50)
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_user('伤口又红肿发炎了，还是得去医院看看。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('破伤风', text)

    def test_reopen_without_repeat_words(self):
        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=50)
        self._insert_user('医生说不用打针，伤口已经好了。', hours_ago=48)
        self._insert_user('伤口今天开始红肿了。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)

    def test_normal_wake_actions_unaffected_without_resolution(self):
        self._insert_wake('explore', '想给她写一首小诗。')
        self._insert_wake('diary', '今天月色很好。')
        self._insert_wake('none', '决定先不打扰她。')

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('用户已明确结案', text)

    def test_movie_ended_does_not_emit_guard_in_chat(self):
        self._insert_user('电影结束了，挺好看的。')
        self._insert_wake('explore', '想给她写一首小诗。')

        text = self._build_wake_text(wake=False)
        self.assertNotIn('用户已明确结案', text)
        self.assertIn('你醒着的时候', text)

    def test_multi_topic_resolution_only_affects_matching_wake(self):
        self._insert_wake('explore', '破伤风要不要打', hours_ago=40)
        self._insert_wake('explore', '记得提醒她买花。', hours_ago=35)
        self._insert_user('医生说破伤风不用打针，伤口已经好了。', hours_ago=10)

        text = self._build_wake_text()
        self.assertIn('买花', text)
        self.assertNotIn('破伤风要不要打', text)

    def test_superseded_diary_filtered_in_wake_prompt(self):
        self._insert_diary('日记：还在担心破伤风要不要打。', hours_ago=30)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        text = self._build_wake_text()
        self.assertNotIn('最近的日记', text)

    def test_wake_log_consumed_semantics_unchanged(self):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (id, action, content, consumed, woke_at) VALUES (1, 'explore', '破伤风', 0, ?)",
            (self._ts(hours_ago=30),),
        )
        conn.execute(
            "INSERT INTO wake_log (id, action, content, consumed, woke_at) VALUES (2, 'explore', '买花', 0, ?)",
            (self._ts(hours_ago=30),),
        )
        conn.commit()
        conn.close()
        self._insert_user('医生说破伤风不用打针，伤口已经好了。')

        ids = context_continuity.capture_pending_wake_ids(self.get_db)
        self.assertEqual(ids, [1, 2])

        consumed = context_continuity.consume_wake_ids(self.get_db, ids)
        self.assertEqual(consumed, 2)

    def test_cc_one_shot_filters_superseded_wake_items(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(one_shot.get('wake_items'), [])

    def test_load_resolution_state_from_db_closes_on_error(self):
        closed = {'value': False}

        class LeakConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError('boom')

            def close(self):
                closed['value'] = True

        def get_db():
            return LeakConn()

        try:
            cr.load_resolution_state_from_db(get_db)
        except sqlite3.OperationalError:
            pass
        self.assertTrue(closed['value'])

    def test_tetanus_regression_case(self):
        self._insert_wake(
            'explore',
            '她之前被猫抓伤，我还在想破伤风针要不要打。',
            hours_ago=72,
        )
        self._insert_wake(
            'diary',
            '日记：猫抓伤口和破伤风仍让我放不下。',
            hours_ago=70,
        )
        self._insert_diary('夜里仍惦记猫抓伤和破伤风要不要打。', hours_ago=68)
        self._insert_user(
            '已经问过医生了，伤口早就愈合，不用打破伤风，这件事结束了。',
            hours_ago=6,
        )

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)
        self.assertNotIn('最近的日记', text)


if __name__ == '__main__':
    unittest.main()
