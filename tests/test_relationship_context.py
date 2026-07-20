"""A1 relationship continuity tests — bucket source, cadence, EMPTY, source health."""

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
    SOURCE_ERROR,
    SOURCE_MISSING,
    SOURCE_OK,
    build_relationship_context,
    rel_context_status,
    should_send_relationship,
)


class RelationshipFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bucket_dir = str(Path(self.tmp.name) / 'bucket')
        Path(self.bucket_dir).mkdir()
        (Path(self.bucket_dir) / '01-anchor.md').write_text(
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
    def test_mood_jitter_within_quadrant_keeps_fingerprint(self):
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.52, 0.33, SOURCE_OK),
        ):
            first = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        fps = [first.fingerprint]
        for valence, arousal in (
            (0.53, 0.34), (0.51, 0.35), (0.54, 0.32), (0.50, 0.36), (0.52, 0.31),
        ):
            with mock.patch(
                'chat.relationship_context._emotion_engine_scores',
                return_value=(valence, arousal, SOURCE_OK),
            ):
                current = build_relationship_context(
                    self.get_db,
                    bucket_dir=self.bucket_dir,
                    previous_mood=first.mood_key,
                )
            fps.append(current.fingerprint)
            self.assertEqual(current.text, first.text)
        self.assertEqual(len(set(fps)), 1)

    def test_boundary_jitter_does_not_flip_with_hysteresis(self):
        """0.49/0.51 边界抖动不得反复翻转象限。"""
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.52, 0.33, SOURCE_OK),
        ):
            first = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertEqual(first.mood_key, '低唤醒|偏暖')
        fps = {first.fingerprint}
        for valence in (0.49, 0.51, 0.49, 0.51, 0.46):
            with mock.patch(
                'chat.relationship_context._emotion_engine_scores',
                return_value=(valence, 0.33, SOURCE_OK),
            ):
                current = build_relationship_context(
                    self.get_db,
                    bucket_dir=self.bucket_dir,
                    previous_mood=first.mood_key,
                )
            self.assertEqual(current.mood_key, '低唤醒|偏暖')
            fps.add(current.fingerprint)
        self.assertEqual(len(fps), 1)
        # Exit warm only below hold threshold.
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.44, 0.33, SOURCE_OK),
        ):
            exited = build_relationship_context(
                self.get_db,
                bucket_dir=self.bucket_dir,
                previous_mood=first.mood_key,
            )
        self.assertEqual(exited.mood_key, '低唤醒|偏低')
        self.assertNotEqual(exited.fingerprint, first.fingerprint)

    def test_state_machine_send_skip_skip_skip_send(self):
        """完整状态机：发送 → 跳过×3 → 发送（第 5 用户轮刷新）。"""
        fp = 'rel-v2:same'
        decisions = []
        turns_since = 0
        for i in range(5):
            is_cold = i == 0
            send = should_send_relationship(
                is_cold=is_cold,
                rel_fp=fp,
                last_fp=None if is_cold else fp,
                turns_since_rel_sent=turns_since,
                user_turn=True,
            )
            decisions.append(send)
            if send:
                turns_since = 0
            else:
                turns_since += 1
        self.assertEqual(decisions, [True, False, False, False, True])

    def test_user_turn_false_does_not_force_periodic_refresh(self):
        self.assertFalse(should_send_relationship(
            is_cold=False,
            rel_fp='rel-v2:abc',
            last_fp='rel-v2:abc',
            turns_since_rel_sent=3,
            user_turn=False,
        ))
        self.assertTrue(should_send_relationship(
            is_cold=False,
            rel_fp='rel-v2:abc',
            last_fp='rel-v2:abc',
            turns_since_rel_sent=3,
            user_turn=True,
        ))

    def test_get_db_failure_degrades_without_raising(self):
        def boom():
            raise RuntimeError('db down')

        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.6, 0.6, SOURCE_OK),
        ):
            result = build_relationship_context(boom, bucket_dir=self.bucket_dir)
        self.assertIn('我们是长期伴侣', result.text)
        self.assertIn('当前基调', result.text)
        self.assertEqual(result.sources['daily'], SOURCE_ERROR)
        self.assertEqual(result.sources['anchor'], SOURCE_OK)
        self.assertEqual(rel_context_status(
            result.text, sent=True, sources=result.sources,
        ), 'degraded')

    def test_all_sources_failed_marks_empty(self):
        missing = str(Path(self.tmp.name) / 'no-such-bucket')

        def boom():
            raise RuntimeError('db down')

        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(None, None, SOURCE_ERROR),
        ):
            with self.assertLogs('relationship_context', level='WARNING') as logs:
                result = build_relationship_context(boom, bucket_dir=missing)
        self.assertEqual(result.text, '')
        self.assertEqual(result.sources, {
            'anchor': SOURCE_MISSING,
            'daily': SOURCE_ERROR,
            'mood': SOURCE_ERROR,
        })
        self.assertEqual(
            rel_context_status(result.text, sent=False, sources=result.sources),
            'EMPTY',
        )
        self.assertTrue(any('EMPTY' in line for line in logs.output))
        self.assertTrue(any('anchor missing' in line for line in logs.output))
        self.assertTrue(any('daily source error' in line for line in logs.output))

    def test_single_source_failure_marks_degraded_not_empty(self):
        with mock.patch(
            'chat.relationship_context._read_relationship_anchor',
            return_value=('', None, SOURCE_ERROR),
        ), mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.6, 0.6, SOURCE_OK),
        ):
            result = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertIn('当前基调', result.text)
        self.assertTrue(result.degraded)
        self.assertEqual(
            rel_context_status(result.text, sent=True, sources=result.sources),
            'degraded',
        )
        self.assertNotEqual(
            rel_context_status(result.text, sent=True, sources=result.sources),
            'EMPTY',
        )

    def test_emotion_engine_failure_does_not_fake_default_mood(self):
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(None, None, SOURCE_ERROR),
        ):
            result = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertNotIn('当前基调', result.text)
        self.assertEqual(result.sources['mood'], SOURCE_ERROR)
        self.assertIn('我们是长期伴侣', result.text)

    def test_multiple_md_files_merged_in_sorted_order(self):
        (Path(self.bucket_dir) / '00-first.md').write_text(
            '先说这件事。', encoding='utf-8',
        )
        (Path(self.bucket_dir) / '02-later.md').write_text(
            '再说那件事。', encoding='utf-8',
        )
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.5, 0.3, SOURCE_OK),
        ):
            result = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertIn('先说这件事', result.text)
        self.assertIn('我们是长期伴侣', result.text)
        self.assertLess(
            result.text.index('先说这件事'),
            result.text.index('我们是长期伴侣'),
        )

    def test_content_hash_fingerprint_detects_same_size_edit(self):
        with mock.patch(
            'chat.relationship_context._emotion_engine_scores',
            return_value=(0.5, 0.3, SOURCE_OK),
        ):
            first = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
            # Same length replacement → mtime+size could miss; content hash must not.
            (Path(self.bucket_dir) / '01-anchor.md').write_text(
                '---\ntitle: anchor\n---\n我们是短期旅伴，彼此客气。',
                encoding='utf-8',
            )
            second = build_relationship_context(self.get_db, bucket_dir=self.bucket_dir)
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertIn('短期旅伴', second.text)


class RelationshipCursorTests(unittest.TestCase):
    def test_respawn_resets_rel_cursor(self):
        from cc_resident import ResidentSession

        class FakeProc:
            def __init__(self):
                self.stdin = self
                self.stdout = self
                self.stderr = self
                self._code = None

            def write(self, _):
                pass

            def flush(self):
                pass

            def close(self):
                pass

            def poll(self):
                return None

            def terminate(self):
                self._code = -1

            def kill(self):
                self._code = -9

            def wait(self, timeout=None):
                return self._code

            def readline(self):
                return ''

        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._last_rel_fingerprint = 'rel-v2:old'
        sess._turns_since_rel_sent = 3
        sess._last_rel_mood = '低唤醒|偏暖'
        with mock.patch('subprocess.Popen', return_value=FakeProc()):
            sess._spawn('STATIC', {}, reason='turn_limit')
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)
        self.assertIsNone(sess.last_rel_mood)

    def test_write_failure_does_not_commit(self):
        from cc_resident import ResidentError, ResidentSession

        class BrokenWrite:
            def write(self, _):
                raise BrokenPipeError('boom')

            def flush(self):
                pass

        class FakeProc:
            def __init__(self):
                self.stdin = BrokenWrite()
                self._code = None

            def poll(self):
                return None

        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc()
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'rel_tick': True,
                'rel_fingerprint': 'rel-v2:new',
            }))
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)

    def test_flush_oserror_does_not_commit(self):
        """flush() 本身抛 OSError 后游标不提交。"""
        from cc_resident import ResidentError, ResidentSession

        class BrokenFlush:
            def write(self, _):
                pass

            def flush(self):
                raise OSError('flush failed')

        class FakeProc:
            def __init__(self):
                self.stdin = BrokenFlush()
                self._code = None

            def poll(self):
                return None

        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc()
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'rel_tick': True,
                'rel_fingerprint': 'rel-v2:new',
                'rel_mood': '低唤醒|偏暖',
            }))
        self.assertIsNone(sess.last_rel_fingerprint)
        self.assertEqual(sess.turns_since_rel_sent, 0)
        self.assertIsNone(sess.last_rel_mood)

    def test_user_turn_skip_increments_only_via_rel_tick(self):
        from cc_resident import ResidentError, ResidentSession

        class FakeProc:
            def __init__(self, lines):
                self._lines = list(lines)
                self.stdin = self
                self.stdout = self
                self.stderr = self
                self._code = None

            def write(self, _):
                pass

            def flush(self):
                pass

            def close(self):
                pass

            def poll(self):
                return self._code

            def readline(self):
                if self._lines:
                    return self._lines.pop(0) + '\n'
                return ''

            def terminate(self):
                self._code = -1

            def kill(self):
                self._code = -9

            def wait(self, timeout=None):
                return self._code

        # EOF before result → error after flush; tick still commits.
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._proc = FakeProc([])
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={'rel_tick': True}))
        self.assertEqual(sess.turns_since_rel_sent, 1)

        sess._proc = FakeProc([])
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'rel_fingerprint': 'rel-v2:sent',
                'rel_mood': '低唤醒|偏暖',
            }))
        self.assertEqual(sess.last_rel_fingerprint, 'rel-v2:sent')
        self.assertEqual(sess.turns_since_rel_sent, 0)
        self.assertEqual(sess.last_rel_mood, '低唤醒|偏暖')


if __name__ == '__main__':
    unittest.main()
