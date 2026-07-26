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

    def test_comfort_phrases_still_count_as_resolution(self):
        self.assertTrue(cr.is_user_resolution('别担心，伤口已经好了。'))
        self.assertTrue(cr.is_user_resolution('没有啦，已经没事了。'))

    def test_question_form_is_not_resolution(self):
        self.assertFalse(cr.is_user_resolution('医生说不用打针吗？'))
        self.assertFalse(cr.is_user_resolution('医生说不用打针吗？我没听清。'))
        self.assertFalse(cr.is_user_resolution('我忘了，医生说不用打针？后来怎么说的。'))

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

    def test_express_delivery_resolution_filters_matching_worry(self):
        messages = [
            {'id': 1, 'content': '快递还没取，我有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 1)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 11:00:00',
        ))

    def test_factory_progress_resolution(self):
        messages = [
            {'id': 1, 'content': '工厂订单的进度还没跟完。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '工厂订单的进度已经跟完了，不用再催了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想工厂订单的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_software_fault_reopen_after_resolution(self):
        messages = [
            {'id': 1, 'content': '服务故障已经修好了，可以正常用了。', 'created_at': '2026-07-21 12:00:00'},
            {'id': 2, 'content': '服务故障又报错了，还得排查。', 'created_at': '2026-07-22 09:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertEqual(len(state.active), 0)

    def test_adjacent_unrelated_concerns_no_cross_supersede(self):
        messages = [
            {'id': 1, 'content': '快递还没取，我有点担心。', 'created_at': '2026-07-20 10:00:00'},
            {'id': 2, 'content': '工厂进度还没跟完。', 'created_at': '2026-07-20 11:00:00'},
            {'id': 3, 'content': '快递已经取完，不用再跑了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想工厂订单进度。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想快递有没有取。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_deictic_resolution_uses_immediate_prior_chat(self):
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'author': 'assistant', 'content': '好的，我记下了。', 'created_at': '2026-07-20 08:05:00'},
            {'id': 3, 'author': 'assistant', 'content': '还担心你的猫抓伤。', 'created_at': '2026-07-20 09:00:00'},
            {'id': 4, 'author': 'hayana', 'content': '这件事结束了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
        entry = state.active[0]
        self.assertTrue(any('抓伤' in token for token in entry.topic_tokens))
        self.assertNotIn('买花', entry.topic_tokens)

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
        chat_messages = [
            {'id': 1, 'author': 'hayana', 'content': '记得提醒我买花。', 'created_at': '2026-07-20 08:00:00'},
            {'id': 2, 'author': 'hayana', 'content': '猫抓的伤口还在疼。', 'created_at': '2026-07-20 09:00:00'},
            {'id': 3, 'author': 'hayana', 'content': '这件事结束了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        user_messages = [m for m in chat_messages if m['author'] == 'hayana']
        state = cr.build_resolution_state(user_messages, chat_messages)
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
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想要不要提醒她买花。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_guard_only_for_applied_resolutions(self):
        state = cr.build_resolution_state([
            {'id': 1, 'content': '电影结束了。', 'created_at': '2026-07-21 10:00:00'},
        ])
        self.assertEqual(cr.format_resolution_guard([]), '')
        self.assertEqual(cr.format_resolution_guard(state.active), '')

    def test_positive_you_completion_is_resolution_not_reopen(self):
        self.assertTrue(cr.is_user_resolution('服务又修好了，可以正常用了。'))
        self.assertFalse(cr.is_user_reopen('服务又修好了，可以正常用了。'))
        self.assertTrue(cr.is_user_resolution('快递又送到了，不用再取了。'))
        self.assertFalse(cr.is_user_reopen('快递又送到了，不用再取了。'))

    def test_distinct_factory_lines_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '一厂衬衫的进度已经跟完了，不用再催了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想二厂磁吸尾的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想一厂衬衫的进度。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_distinct_orders_do_not_cross(self):
        messages = [
            {'id': 1, 'content': '订单a已经发货，不用再问了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想订单b有没有发货。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想订单a有没有发货。', state, recorded_at='2026-07-20 18:00:00',
        ))

    def test_frontend_vs_frontend_gw_do_not_cross(self):
        messages = [
            {'id': 1, 'content': 'frontend 故障已经修好了，不用再看了。', 'created_at': '2026-07-21 12:00:00'},
        ]
        state = cr.build_resolution_state(messages)
        self.assertFalse(cr.is_superseded_historical_concern(
            '还在想 frontend-gw 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))
        self.assertTrue(cr.is_superseded_historical_concern(
            '还在想 frontend 故障要不要继续排查。', state, recorded_at='2026-07-20 18:00:00',
        ))


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

    def _insert_assistant(self, content, *, hours_ago: float = 1.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('assistant', content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def _age_out_resolution_messages(self, *, hours_ago: float = 200.0):
        conn = self.get_db()
        conn.execute(
            """
            UPDATE chat_messages
            SET created_at=?
            WHERE author IN ('hayana', 'haya', 'user')
              AND (
                content LIKE '%不用再%'
                OR content LIKE '%已经%好了%'
                OR content LIKE '%已经取完%'
                OR content LIKE '%可以放下%'
                OR content LIKE '%结束了%'
                OR content LIKE '%不用打针%'
                OR content LIKE '%不用打破伤风%'
              )
            """,
            (self._ts(hours_ago=hours_ago),),
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

    def test_flower_reminder_wake_survives_express_processed_ok(self):
        self._insert_user('记得提醒我买花。', hours_ago=30)
        self._insert_wake('explore', '还在想要不要提醒她买花。', hours_ago=20)
        self._insert_user('快递已经处理好了。')

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
        self.assertEqual(one_shot.get('suppressed_wake_ids'), [1])
        self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))

    def test_cc_suppressed_wake_ids_are_consumable(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。')

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(one_shot.get('wake_ids'), [1])
        consumed = context_continuity.consume_wake_ids(self.get_db, one_shot['wake_ids'])
        self.assertEqual(consumed, 1)

    def test_consumed_suppressed_wake_does_not_revive_after_resolution_window(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想破伤风要不要打。', hours_ago=24)
        self._insert_user('医生说破伤风不用打针，伤口已经愈合。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            one_shot = system_builder.build_cc_one_shot(include_wake=True)
            self.assertEqual(one_shot.get('wake_items'), [])
            self.assertEqual(one_shot.get('suppressed_wake_ids'), [1])
            context_continuity.consume_wake_ids(self.get_db, one_shot['wake_ids'])

            conn = self.get_db()
            conn.execute(
                "UPDATE chat_messages SET created_at=? WHERE id=1",
                (self._ts(hours_ago=200),),
            )
            conn.commit()
            conn.close()

            revived = system_builder.build_cc_one_shot(include_wake=True)

        self.assertEqual(revived.get('wake_items'), [])
        self.assertEqual(revived.get('suppressed_wake_ids'), [])
        self.assertEqual(revived.get('wake_resolution_guard'), '')
        conn = self.get_db()
        pending = conn.execute(
            "SELECT COUNT(*) FROM wake_log WHERE consumed=0"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(pending, 0)

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

    def test_concern_closures_persist_in_database(self):
        self._insert_user('快递已经取完，不用再跑了。')
        cr.load_resolution_state_from_db(self.get_db)
        conn = self.get_db()
        row = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(row, 1)

    def test_closure_persists_beyond_168h_wake_and_diary(self):
        self._insert_diary('还在担心快递有没有取。', hours_ago=200)
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=200)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)
        self.assertNotIn('最近的日记', text)

        self._age_out_resolution_messages(hours_ago=200)

        text_after = self._build_wake_text()
        self.assertNotIn('你醒着的时候', text_after)
        self.assertNotIn('最近的日记', text_after)
        self.assertIn('用户已明确结案', text_after)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)

    def test_reopen_after_168h_allows_matching_wake_back(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=200)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)
        self._build_wake_text()
        self._age_out_resolution_messages(hours_ago=200)
        self._insert_user('快递又找不到了，还得再去拿。', hours_ago=0.5)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('快递', text)

    def test_express_delivery_wake_suppressed(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_factory_progress_wake_suppressed(self):
        self._insert_wake('explore', '还在想工厂订单的进度。', hours_ago=20)
        self._insert_user('工厂订单的进度已经跟完了，不用再催了。')

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_software_fault_reopen_shows_wake_again(self):
        self._insert_wake('explore', '还在想服务故障要不要继续排查。', hours_ago=50)
        self._insert_user('服务故障已经修好了，可以正常用了。', hours_ago=48)
        self._insert_user('服务故障又报错了，还得排查。', hours_ago=1)

        text = self._build_wake_text()
        self.assertIn('你醒着的时候', text)
        self.assertIn('服务故障', text)

    def test_adjacent_unrelated_wake_only_partially_suppressed(self):
        self._insert_wake('explore', '还在想工厂订单进度。', hours_ago=30)
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=28)
        self._insert_user('快递已经取完，不用再跑了。')

        text = self._build_wake_text()
        self.assertIn('工厂', text)
        self.assertIn('你醒着的时候', text)
        self.assertNotIn('快递有没有取', text)

    def test_deictic_resolution_across_assistant_message(self):
        self._insert_user('提醒我买花。', hours_ago=10)
        self._insert_assistant('好的，我记下了。', hours_ago=9)
        self._insert_assistant('还担心你的猫抓伤。', hours_ago=8)
        self._insert_wake('explore', '还在想猫抓伤要不要打破伤风。', hours_ago=7)
        self._insert_user('这件事结束了。', hours_ago=1)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('破伤风', text)
        self.assertNotIn('你醒着的时候', text)

    def test_cold_once_excludes_resolution_guard(self):
        from chat import system_builder

        self._insert_diary('还在担心快递有没有取。', hours_ago=20)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            cold = system_builder._cc_collect_cold_once(self.get_db)

        self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))
        self.assertNotIn('最近的日记', cold.get('long_term_memory', ''))

    def test_cold_resolved_then_reopen_drops_stale_guard_in_one_shot(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=2)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            cold = system_builder._cc_collect_cold_once(self.get_db)
            self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))

            one_shot = system_builder.build_cc_one_shot(include_wake=True)
            self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))
            self.assertEqual(one_shot.get('wake_items'), [])

            self._insert_user('快递又找不到了，还得再去拿。', hours_ago=0.2)
            reopened = system_builder.build_cc_one_shot(include_wake=True)

        formatted = system_builder.format_one_shot(reopened)
        self.assertNotIn('用户已明确结案', formatted)
        self.assertEqual(len(reopened.get('wake_items') or []), 1)
        self.assertIn('快递', formatted)

    def test_finalize_cc_wake_one_shot_fail_open_on_merge_error(self):
        from chat import system_builder

        hot = {
            'wake_items': [
                {'id': 9, 'action': 'explore', 'content': '还在想快递。', 'woke_at': '2026-07-20 10:00:00'},
            ],
            'suppressed_wake_ids': [10],
        }
        with mock.patch('wake.concern_resolution.merge_wake_consume_ids', side_effect=RuntimeError('boom')):
            result = system_builder.finalize_cc_wake_one_shot(hot, is_cold=False)
        self.assertEqual(result['wake_ids'], [9])

    def _insert_user_at(self, content, *, message_id: int, hours_ago: float = 1.0):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) VALUES (?,?,?,?)",
            (message_id, 'hayana', content, self._ts(hours_ago=hours_ago)),
        )
        conn.commit()
        conn.close()

    def test_sync_idempotent_resolve_reopen_resolve(self):
        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=30)
        self._insert_user_at('快递已经取完，不用再跑了。', message_id=1, hours_ago=20)
        self._insert_user_at('快递又找不到了，还得再去拿。', message_id=2, hours_ago=10)
        self._insert_user_at('快递已经取到了，不用再跑了。', message_id=3, hours_ago=1)

        states = [cr.load_resolution_state_from_db(self.get_db) for _ in range(3)]
        for state in states:
            self.assertEqual(len(state.active), 1)
            self.assertIn('取到', state.active[0].summary)

        conn = self.get_db()
        active = conn.execute(
            "SELECT COUNT(*) FROM concern_closures WHERE active=1"
        ).fetchone()[0]
        cursor = conn.execute(
            "SELECT last_processed_message_id FROM concern_closure_sync WHERE id=1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1)
        self.assertEqual(cursor, 3)

        text = self._build_wake_text()
        self.assertIn('用户已明确结案', text)
        self.assertNotIn('你醒着的时候', text)

    def test_sync_state_survives_service_restart(self):
        self._insert_user('快递已经取完，不用再跑了。')
        first = cr.load_resolution_state_from_db(self.get_db)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        second = cr.load_resolution_state(conn)
        conn.close()

        self.assertEqual(len(first.active), len(second.active))
        self.assertEqual(first.active[0].summary, second.active[0].summary)

    def test_cold_once_and_one_shot_share_persistent_state(self):
        from chat import system_builder

        self._insert_wake('explore', '还在想快递有没有取。', hours_ago=20)
        self._insert_diary('还在担心快递有没有取。', hours_ago=18)
        self._insert_user('快递已经取完，不用再跑了。', hours_ago=1)

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}):
            for _ in range(3):
                cr.load_resolution_state_from_db(self.get_db)
            cold = system_builder._cc_collect_cold_once(self.get_db)
            one_shot = system_builder.build_cc_one_shot(include_wake=True)

        self.assertNotIn('最近的日记', cold.get('long_term_memory', ''))
        self.assertNotIn('用户已明确结案', cold.get('long_term_memory', ''))
        self.assertEqual(one_shot.get('wake_items'), [])
        self.assertIn('用户已明确结案', system_builder.format_one_shot(one_shot))


if __name__ == '__main__':
    unittest.main()
