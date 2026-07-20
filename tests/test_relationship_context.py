"""A1 relationship continuity tests — bucket file source, cursor cadence, empty alerts."""

from __future__ import annotations

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

from chat.relationship_context import (
    build_relationship_context,
    rel_context_status,
    should_send_relationship,
)


class RelationshipFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bucket_dir = str(Path(self.tmp.name) / 'bucket')
        Path(self.bucket_dir).mkdir()
        anchor = Path(self.bucket_dir) / '01-anchor.md'
        anchor.write_text(
            '---\ntitle: anchor\n---\n我们是长期伴侣，彼此信任。',
            encoding='utf-8',
        )
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT,
                resolved INTEGER DEFAULT 0, importance INTEGER DEFAULT 0
            );
            INSERT INTO posts
                (id, type, content, tags, layer, created_at, resolved, importance)
            VALUES
                (1, 'DAILY_SUMMARY', '昨天确认先完成 provider parity。', '', '',
                 '2026-07-18 10:00:00', 0, 8);
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


class RelationshipContextTests(RelationshipFixture):
    def test_mood_jitter_within_quadrant_keeps_fingerprint_and_sends_once(self):
        """象限内抖动 5 轮 → 指纹不变 → should_send 只在冷启动与第 4 轮刷新。"""
        first = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        fps = [first.fingerprint]
        for valence, arousal in (
            (0.53, 0.34), (0.51, 0.35), (0.54, 0.32), (0.50, 0.36), (0.52, 0.31),
        ):
            with mock.patch(
                'chat.relationship_context._emotion_engine_scores',
                return_value=(valence, arousal),
            ):
                current = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
            fps.append(current.fingerprint)
            self.assertEqual(current.text, first.text)
        self.assertEqual(len(set(fps)), 1)

        last_fp = first.fingerprint
        self.assertTrue(should_send_relationship(
            is_cold=True, rel_fp=last_fp, last_fp=None, turns_since_rel_sent=0,
        ))
        for turn in range(3):
            self.assertFalse(should_send_relationship(
                is_cold=False,
                rel_fp=last_fp,
                last_fp=last_fp,
                turns_since_rel_sent=turn,
            ))
        self.assertTrue(should_send_relationship(
            is_cold=False,
            rel_fp=last_fp,
            last_fp=last_fp,
            turns_since_rel_sent=4,
        ))

    def test_fourth_turn_forces_refresh(self):
        self.assertTrue(should_send_relationship(
            is_cold=False,
            rel_fp='rel-v2:abc',
            last_fp='rel-v2:abc',
            turns_since_rel_sent=4,
        ))
        self.assertFalse(should_send_relationship(
            is_cold=False,
            rel_fp='rel-v2:abc',
            last_fp='rel-v2:abc',
            turns_since_rel_sent=3,
        ))

    def test_bucket_read_failure_degrades_to_mood_only(self):
        with mock.patch(
            'chat.relationship_context._read_relationship_anchor',
            return_value=('', None),
        ), mock.patch(
            'chat.relationship_context._latest_daily_summary_head',
            return_value='',
        ), mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.6, 0.6),
        ):
            result = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertIn('当前基调：高唤醒、偏暖', result.text)
        self.assertEqual(rel_context_status(result.text, sent=True), 'sent')
        self.assertNotEqual(rel_context_status(result.text, sent=True), 'EMPTY')

    def test_all_sources_empty_logs_warning_and_marks_empty(self):
        with mock.patch(
            'chat.relationship_context._read_relationship_anchor',
            return_value=('', None),
        ), mock.patch(
            'chat.relationship_context._latest_daily_summary_head',
            return_value='',
        ), mock.patch(
            'chat.relationship_context._quantized_mood',
            return_value='',
        ):
            with self.assertLogs('relationship_context', level='WARNING') as logs:
                result = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertEqual(result.text, '')
        self.assertEqual(rel_context_status(result.text, sent=False), 'EMPTY')
        self.assertTrue(any('EMPTY' in line for line in logs.output))


class RelationshipCursorTests(unittest.TestCase):
    def test_respawn_resets_rel_cursor_and_flush_failure_does_not_commit(self):
        from cc_resident import ResidentError, ResidentSession

        class FakeProc:
            def __init__(self):
                self.stdin = self
                self._code = None

            def write(self, _):
                raise BrokenPipeError('boom')

            def flush(self):
                pass

            def poll(self):
                return None

        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._last_rel_fingerprint = 'rel-v2:old'
        sess._turns_since_rel_sent = 3
        with mock.patch('subprocess.Popen', return_value=FakeProc()):
            sess._spawn('STATIC', {}, reason='turn_limit')
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)

        sess._proc = FakeProc()
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'rel_tick': True,
                'rel_fingerprint': 'rel-v2:new',
            }))
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)

        sess._proc = FakeProc()
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={'rel_tick': True}))
        self.assertEqual(sess.turns_since_rel_sent, 0)


if __name__ == '__main__':
    unittest.main()
