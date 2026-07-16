import os
import sqlite3
import tempfile
import unittest

import moments_store
from valence_scale import normalize_valence


class MomentsStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "author TEXT DEFAULT 'fyodor', "
            "created_at TEXT, "
            "tags TEXT, "
            "processed INTEGER DEFAULT 1"
            ")"
        )
        conn.commit()
        conn.close()
        moments_store.ensure_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def _insert_thought(self, content, created_at, *, processed=1, tags=''):
        conn = sqlite3.connect(self.db_path)
        if created_at is None:
            conn.execute(
                "INSERT INTO posts (type, content, author, processed, tags) "
                "VALUES ('THOUGHT', ?, 'fyodor', ?, ?)",
                (content, processed, tags),
            )
        else:
            conn.execute(
                "INSERT INTO posts (type, content, author, created_at, processed, tags) "
                "VALUES ('THOUGHT', ?, 'fyodor', ?, ?, ?)",
                (content, created_at, processed, tags),
            )
        conn.commit()
        conn.close()

    def test_feed_returns_iso_timestamps_and_item_keys(self):
        self._insert_thought('昨天的念头', '2026-07-15 23:59:00')
        self._insert_thought('今天的念头', '2026-07-16 09:15:00')

        payload = moments_store.get_feed(memories_db_path=self.db_path, limit=20)
        self.assertEqual(len(payload['items']), 2)
        self.assertEqual(payload['items'][0]['item_key'], 'thought:2')
        self.assertEqual(payload['items'][0]['created_at'], '2026-07-16T09:15:00+08:00')
        self.assertEqual(payload['items'][1]['created_at'], '2026-07-15T23:59:00+08:00')

    def test_cursor_pagination_is_stable(self):
        for index in range(5):
            self._insert_thought(
                f'念头 {index}',
                f'2026-07-16 0{index}:00:00',
            )

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=2)
        self.assertTrue(first['has_more'])
        self.assertEqual([item['item_key'] for item in first['items']], ['thought:5', 'thought:4'])

        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=first['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in second['items']], ['thought:3', 'thought:2'])

        third = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=second['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in third['items']], ['thought:1'])
        self.assertFalse(third['has_more'])

    def test_invalid_cursor_returns_error(self):
        with self.assertRaises(ValueError):
            moments_store.get_feed(memories_db_path=self.db_path, cursor='not-a-cursor')

    def test_brewing_maps_processed_zero(self):
        self._insert_thought('酝酿中', '2026-07-16 10:00:00', processed=0)
        payload = moments_store.get_feed(memories_db_path=self.db_path, limit=1)
        self.assertTrue(payload['items'][0]['brewing'])

    def test_valence_scale_converts_legacy_unipolar(self):
        self.assertAlmostEqual(normalize_valence(0.5), 0.0)
        self.assertAlmostEqual(normalize_valence(0.8), 0.6)
        self.assertAlmostEqual(normalize_valence(0.2, scale='bipolar'), 0.2)

    def test_null_created_at_pagination_does_not_repeat(self):
        for index in range(8):
            self._insert_thought(f'空时间 {index}', None)

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=3)
        self.assertEqual([item['item_key'] for item in first['items']], [
            'thought:8', 'thought:7', 'thought:6',
        ])
        self.assertIsNone(first['items'][0]['created_at'])
        self.assertTrue(first['has_more'])

        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=3,
            cursor=first['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in second['items']], [
            'thought:5', 'thought:4', 'thought:3',
        ])
        self.assertTrue(second['has_more'])

        third = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=3,
            cursor=second['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in third['items']], ['thought:2', 'thought:1'])
        self.assertFalse(third['has_more'])

    def test_empty_string_created_at_pagination_does_not_repeat(self):
        for index in range(4):
            self._insert_thought(f'空字符串 {index}', '')

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=2)
        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=first['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in first['items']], ['thought:4', 'thought:3'])
        self.assertEqual([item['item_key'] for item in second['items']], ['thought:2', 'thought:1'])
        self.assertFalse(second['has_more'])


if __name__ == '__main__':
    unittest.main()
