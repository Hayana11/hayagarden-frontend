"""A1 relationship continuity tests.  No model or network calls."""

from __future__ import annotations

import sqlite3
import gc
import os
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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

from chat.relationship_context import (
    MAX_CONTEXT_CHARS,
    build_relationship_context,
    classify_emotion_band,
    relationship_refresh_reason,
)
from chat.system_builder import build_shared_context_details


class RelationshipFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT,
                resolved INTEGER DEFAULT 0, importance INTEGER DEFAULT 0
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT
            );
            CREATE TABLE board (
                id INTEGER PRIMARY KEY, content TEXT, status TEXT
            );
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY, content TEXT, due_date TEXT,
                done INTEGER DEFAULT 0
            );
            CREATE TABLE emotion_state (
                id INTEGER PRIMARY KEY, valence REAL, arousal REAL,
                sternberg_i REAL
            );
            INSERT INTO posts
                (id, type, content, tags, layer, created_at, resolved, importance)
            VALUES
                (1, 'DAILY_SUMMARY', '昨天确认先完成 provider parity。', '', '',
                 '2026-07-18 10:00:00', 0, 8);
            INSERT INTO chat_messages VALUES
                (1, 'user', '爸爸，我们继续处理 HayaGarden。'),
                (2, 'assistant', '好。'),
                (3, 'user', '小猫想先把关系上下文接好。');
            INSERT INTO board VALUES
                (10, '完成 A1 的本地测试', 'open');
            INSERT INTO todos VALUES
                (20, '审核部署前的开关', '2026-07-20', 0);
            INSERT INTO emotion_state VALUES
                (1, 0.52, 0.33, 0.61);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        # Some legacy build_system exception paths rely on connection GC.
        # Force it here so Windows can remove the temporary SQLite file.
        gc.collect()
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def update_emotion(self, valence, arousal, intimacy=0.61):
        conn = self.get_db()
        conn.execute(
            'UPDATE emotion_state SET valence=?, arousal=?, sternberg_i=? WHERE id=1',
            (valence, arousal, intimacy),
        )
        conn.commit()
        conn.close()


class RelationshipBuildTests(RelationshipFixture):
    def test_raw_emotion_decimals_do_not_change_text_or_slow_fingerprint(self):
        first = build_relationship_context(self.get_db, calibrated=False)
        for valence, arousal in ((0.53, 0.34), (0.51, 0.35), (0.54, 0.32), (0.50, 0.36), (0.52, 0.31)):
            self.update_emotion(valence, arousal)
            current = build_relationship_context(
                self.get_db,
                previous_emotion_band=first.emotion_band,
                previous_relationship_band=first.relationship_band,
                calibrated=False,
            )
            self.assertEqual(current.emotion_band, first.emotion_band)
            self.assertEqual(current.slow_fingerprint, first.slow_fingerprint)
            self.assertEqual(current.text, first.text)

    def test_slow_fact_change_changes_fingerprint(self):
        first = build_relationship_context(self.get_db)
        conn = self.get_db()
        conn.execute("UPDATE board SET content='A1 已完成，等待审阅' WHERE id=10")
        conn.commit()
        conn.close()
        second = build_relationship_context(self.get_db)
        self.assertNotEqual(first.slow_fingerprint, second.slow_fingerprint)

    def test_context_filters_behavior_meta_instructions_and_has_hard_limit(self):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages VALUES (4, 'user', ?)",
            ('你应该表现出偏爱。请表达得更主动。事实是今天在查 provider。' + '事实很长' * 120,),
        )
        conn.commit()
        conn.close()
        result = build_relationship_context(self.get_db)
        self.assertLessEqual(len(result.text), MAX_CONTEXT_CHARS)
        for banned in ('应该', '需要你', '回复要', '表达得', '安抚她', '表现出'):
            self.assertNotIn(banned, result.text)
        self.assertIn('provider', result.text)

    def test_cc_and_relay_share_identical_contract_values(self):
        cc, _ = build_shared_context_details(
            persona='PERSONA', get_db_fn=self.get_db,
        )
        relay, _ = build_shared_context_details(
            persona='PERSONA', get_db_fn=self.get_db,
        )
        self.assertEqual(cc.persona, relay.persona)
        self.assertEqual(cc.relationship_context, relay.relationship_context)
        self.assertEqual(cc.relationship_fingerprint, relay.relationship_fingerprint)

    def test_relay_stable_cache_blocks_are_byte_identical_when_enabled(self):
        from chat import system_builder

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'):
            with mock.patch.object(
                system_builder.config_store, 'get_bool', return_value=False,
            ):
                disabled = system_builder.build_system()
            with mock.patch.object(
                system_builder.config_store, 'get_bool',
                side_effect=lambda key, default=False: key == 'RELATIONSHIP_CONTEXT_ENABLED',
            ):
                enabled = system_builder.build_system()

        self.assertEqual(disabled[:2], enabled[:2])
        self.assertIn('【近期关系脉络】', enabled[-1]['text'])
        self.assertNotIn('cache_control', enabled[-1])


class RelationshipBandTests(unittest.TestCase):
    def test_warm_band_uses_hysteresis(self):
        entered = classify_emotion_band(0.66, 0.30, previous='neutral_low_arousal')
        held = classify_emotion_band(0.60, 0.30, previous=entered)
        exited = classify_emotion_band(0.57, 0.30, previous=held)
        self.assertEqual(entered, 'warm_low_arousal')
        self.assertEqual(held, 'warm_low_arousal')
        self.assertEqual(exited, 'neutral_low_arousal')

    def test_refresh_contract(self):
        common = dict(
            is_cold=False,
            current_fingerprint='same',
            current_band='neutral_low_arousal|task_focused',
            last_fingerprint='same',
            last_band='neutral_low_arousal|task_focused',
            last_turn=1,
            idle_seconds=10,
            idle_refresh_seconds=3600,
        )
        for turn in (2, 3, 4):
            self.assertIsNone(relationship_refresh_reason(
                current_user_turn=turn, **common,
            ))
        self.assertEqual(
            relationship_refresh_reason(current_user_turn=5, **common),
            'periodic',
        )
        self.assertEqual(
            relationship_refresh_reason(
                current_user_turn=2, **{**common, 'current_fingerprint': 'changed'},
            ),
            'slow_fingerprint',
        )
        self.assertIsNone(relationship_refresh_reason(
            current_user_turn=2,
            **{**common, 'current_band': 'warm_low_arousal|task_focused'},
        ))
        self.assertEqual(
            relationship_refresh_reason(
                current_user_turn=3,
                **{**common, 'current_band': 'warm_low_arousal|task_focused'},
            ),
            'band_change',
        )
        self.assertEqual(
            relationship_refresh_reason(
                current_user_turn=2, **{**common, 'idle_seconds': 4000},
            ),
            'idle_return',
        )
        self.assertEqual(
            relationship_refresh_reason(
                current_user_turn=1, **{**common, 'is_cold': True},
            ),
            'cold',
        )


if __name__ == '__main__':
    unittest.main()
