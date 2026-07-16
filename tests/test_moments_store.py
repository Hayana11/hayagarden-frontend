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
        conn.execute(
            "CREATE TABLE chat_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id INTEGER NOT NULL DEFAULT 1, "
            "author TEXT NOT NULL, "
            "content TEXT, "
            "created_at TEXT, "
            "image_url TEXT, "
            "file_url TEXT, "
            "file_name TEXT"
            ")"
        )
        conn.commit()
        conn.close()
        moments_store.ensure_schema(self.db_path)

        handle, self.gallery_db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        gconn = sqlite3.connect(self.gallery_db_path)
        gconn.execute(
            "CREATE TABLE gallery_photos ("
            "pid TEXT PRIMARY KEY, "
            "note TEXT, "
            "summary TEXT, "
            "keywords TEXT, "
            "width INTEGER, "
            "height INTEGER, "
            "saved_at TEXT, "
            "created_at TEXT"
            ")"
        )
        gconn.commit()
        gconn.close()

    def tearDown(self):
        os.unlink(self.db_path)
        os.unlink(self.gallery_db_path)

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

    def _collect_all_keys(self, *, feed_type='all', limit=20):
        keys = []
        cursor = None
        while True:
            page = moments_store.get_feed(
                memories_db_path=self.db_path,
                gallery_db_path=self.gallery_db_path,
                limit=limit,
                cursor=cursor,
                feed_type=feed_type,
            )
            keys.extend(item['item_key'] for item in page['items'])
            if not page['has_more']:
                break
            cursor = page['next_cursor']
        return keys

    def _assert_paginated_keys(self, expected_keys, *, feed_type='all', limit=2):
        seen: list[str] = []
        cursor = None
        while True:
            page = moments_store.get_feed(
                memories_db_path=self.db_path,
                gallery_db_path=self.gallery_db_path,
                limit=limit,
                cursor=cursor,
                feed_type=feed_type,
            )
            page_keys = [item['item_key'] for item in page['items']]
            for key in page_keys:
                self.assertNotIn(key, seen)
                seen.append(key)
            if not page['has_more']:
                break
            cursor = page['next_cursor']
            self.assertIsNotNone(cursor)
        self.assertEqual(seen, expected_keys)

    def test_same_timestamp_thoughts_paginate_all_ids(self):
        for index in range(1, 13):
            self._insert_thought(f'同时间 {index}', '2026-07-16 09:00:00')
        expected = [f'thought:{index}' for index in range(12, 0, -1)]
        self._assert_paginated_keys(expected, feed_type='thought', limit=2)

    def test_same_timestamp_reposts_paginate_all_ids(self):
        for index in range(1, 13):
            user_id = self._insert_chat('hayana', f'u{index}', f'2026-07-16 09:00:{index:02d}')
            assistant_id = self._insert_chat('fyodor', f'a{index}', f'2026-07-16 09:01:{index:02d}')
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                '''INSERT INTO moment_chat_collections
                   (session_id, source_start_id, source_end_id, source_fingerprint,
                    caption, snapshot_json, collector, collected_at)
                   VALUES (1, ?, ?, ?, ?, ?, 'fyodor', ?)''',
                (
                    user_id,
                    assistant_id,
                    f'fp-{index}',
                    f'caption {index}',
                    '{"version":1,"messages":[{"message_id":1,"role":"haya","text":"hi","created_at":null},{"message_id":2,"role":"fyodor","text":"ok","created_at":null}]}',
                    '2026-07-16 09:00:00',
                ),
            )
            conn.commit()
            conn.close()
        expected = [f'chat-collection:{index}' for index in range(12, 0, -1)]
        self._assert_paginated_keys(expected, feed_type='posts', limit=2)

    def test_same_timestamp_mixed_sources_paginate_without_gaps(self):
        self._insert_thought('念头 1', '2026-07-16 09:00:00')
        self._insert_thought('念头 2', '2026-07-16 09:00:00')
        self._insert_gallery('pic-z', note='图', saved_at='2026-07-16 09:00:00')
        user_id = self._insert_chat('hayana', '你好', '2026-07-16 09:00:00')
        assistant_id = self._insert_chat('fyodor', '嗯', '2026-07-16 09:01:00')
        moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='转发',
        )
        keys = self._collect_all_keys(feed_type='all', limit=2)
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(set(keys)), 4)
        self.assertEqual(set(keys), {'thought:2', 'thought:1', 'gallery:pic-z', 'chat-collection:1'})

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

    def test_large_thought_feed_paginates_past_100(self):
        for index in range(121):
            minute = index % 60
            hour = 10 + (index // 60)
            self._insert_thought(
                f'念头 {index}',
                f'2026-07-16 {hour:02d}:{minute:02d}:00',
            )

        keys = self._collect_all_keys(feed_type='thought', limit=20)
        self.assertEqual(len(keys), 121)
        self.assertEqual(keys[0], 'thought:121')
        self.assertEqual(keys[-1], 'thought:1')

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

    def test_invalid_date_string_pagination_does_not_skip(self):
        for index in range(5):
            self._insert_thought(f'脏日期 {index}', 'not-a-date')

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=2)
        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=first['next_cursor'],
        )
        third = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=second['next_cursor'],
        )

        self.assertEqual([item['item_key'] for item in first['items']], ['thought:5', 'thought:4'])
        self.assertTrue(first['has_more'])
        self.assertIsNone(first['items'][0]['created_at'])

        self.assertEqual([item['item_key'] for item in second['items']], ['thought:3', 'thought:2'])
        self.assertTrue(second['has_more'])

        self.assertEqual([item['item_key'] for item in third['items']], ['thought:1'])
        self.assertFalse(third['has_more'])

    def test_whitespace_created_at_pagination_does_not_skip(self):
        for index in range(3):
            self._insert_thought(f'空格日期 {index}', '   ')

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=2)
        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=first['next_cursor'],
        )
        self.assertEqual([item['item_key'] for item in first['items']], ['thought:3', 'thought:2'])
        self.assertEqual([item['item_key'] for item in second['items']], ['thought:1'])
        self.assertFalse(second['has_more'])

    def test_valid_dates_then_invalid_dates_paginate_across_tiers(self):
        self._insert_thought('脏 1', 'not-a-date')
        self._insert_thought('正常 1', '2026-07-16 01:00:00')
        self._insert_thought('正常 2', '2026-07-16 02:00:00')
        self._insert_thought('脏 2', 'not-a-date')
        self._insert_thought('脏 3', 'not-a-date')

        first = moments_store.get_feed(memories_db_path=self.db_path, limit=2)
        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=first['next_cursor'],
        )
        third = moments_store.get_feed(
            memories_db_path=self.db_path,
            limit=2,
            cursor=second['next_cursor'],
        )

        self.assertEqual([item['item_key'] for item in first['items']], ['thought:3', 'thought:2'])
        self.assertEqual([item['item_key'] for item in second['items']], ['thought:5', 'thought:4'])
        self.assertEqual([item['item_key'] for item in third['items']], ['thought:1'])
        self.assertFalse(third['has_more'])

    def _assert_same_timestamp_paginates_by_id(self, timestamps):
        for index, timestamp in enumerate(timestamps):
            self._insert_thought(f'同一时刻 {index}', timestamp)

        keys = self._collect_all_keys(limit=2)
        expected = [f'thought:{item_id}' for item_id in range(len(timestamps), 0, -1)]
        self.assertEqual(keys, expected)

    def test_same_beijing_db_timestamp_paginates_by_id(self):
        self._assert_same_timestamp_paginates_by_id([
            '2026-07-16 09:00:00',
        ] * 5)

    def test_same_shanghai_iso_timestamp_paginates_by_id(self):
        self._assert_same_timestamp_paginates_by_id([
            '2026-07-16T09:00:00+08:00',
        ] * 5)

    def test_same_utc_iso_timestamp_paginates_by_id(self):
        self._assert_same_timestamp_paginates_by_id([
            '2026-07-16T01:00:00Z',
        ] * 5)

    def test_mixed_timestamp_formats_for_same_instant_paginate_by_id(self):
        self._assert_same_timestamp_paginates_by_id([
            '2026-07-16 09:00:00',
            '2026-07-16T09:00:00+08:00',
            '2026-07-16T01:00:00Z',
            '2026-07-16 09:00:00',
            '2026-07-16T09:00:00+08:00',
            '2026-07-16T01:00:00Z',
        ])

    def _insert_gallery(self, pid, *, note='', saved_at='2026-07-16 08:00:00'):
        conn = sqlite3.connect(self.gallery_db_path)
        conn.execute(
            "INSERT INTO gallery_photos (pid, note, summary, keywords, width, height, saved_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, note, note, '[]', 800, 600, saved_at, saved_at),
        )
        conn.commit()
        conn.close()

    def _insert_chat(self, author, content, created_at='2026-07-16 10:00:00', *, image_url=''):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO chat_messages (session_id, author, content, created_at, image_url) "
            "VALUES (1, ?, ?, ?, ?)",
            (author, content, created_at, image_url),
        )
        conn.commit()
        row_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return row_id

    def test_three_source_merge_orders_by_time(self):
        self._insert_thought('念头', '2026-07-16 11:00:00')
        self._insert_gallery('pic-a', note='雪', saved_at='2026-07-16 12:00:00')
        user_id = self._insert_chat('hayana', '你好', '2026-07-16 09:00:00')
        assistant_id = self._insert_chat('fyodor', '嗯', '2026-07-16 09:01:00')
        moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='一段对话',
        )

        payload = moments_store.get_feed(
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
            limit=10,
            feed_type='all',
        )
        keys = [item['item_key'] for item in payload['items']]
        self.assertEqual(set(keys), {'gallery:pic-a', 'thought:1', 'chat-collection:1'})
        self.assertLess(keys.index('gallery:pic-a'), keys.index('thought:1'))

    def test_posts_feed_excludes_gallery(self):
        self._insert_thought('念头', '2026-07-16 11:00:00')
        self._insert_gallery('pic-b', saved_at='2026-07-16 12:00:00')

        payload = moments_store.get_feed(
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
            limit=10,
            feed_type='posts',
        )
        kinds = {item['kind'] for item in payload['items']}
        self.assertEqual(kinds, {'thought'})

    def test_finalize_chat_collection_builds_snapshot(self):
        user_id = self._insert_chat('hayana', '  你好  ', '2026-07-16 09:00:00')
        assistant_id = self._insert_chat('fyodor', '嗯', '2026-07-16 09:01:00')
        collection_id = moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='附言',
        )
        self.assertEqual(collection_id, 1)
        item = moments_store.get_chat_collection(1, memories_db_path=self.db_path)
        self.assertIsNotNone(item)
        self.assertEqual(item['kind'], 'repost')
        self.assertEqual(item['content'], '附言')
        messages = item['repost']['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]['role'], 'haya')
        self.assertEqual(messages[0]['text'], '你好')
        self.assertEqual(messages[1]['role'], 'fyodor')

    def test_finalize_filters_unknown_roles_and_empty_messages(self):
        prior_user = self._insert_chat('system', 'ignored', '2026-07-16 08:58:00')
        prior_assistant = self._insert_chat('fyodor', '   ', '2026-07-16 08:59:00')
        user_id = self._insert_chat('hayana', '你好', '2026-07-16 09:00:00')
        assistant_id = self._insert_chat('fyodor', '嗯', '2026-07-16 09:01:00')
        del prior_user, prior_assistant

        collection_id = moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=1,
            caption='',
        )
        self.assertEqual(collection_id, 1)
        item = moments_store.get_chat_collection(1, memories_db_path=self.db_path)
        roles = [m['role'] for m in item['repost']['messages']]
        self.assertEqual(roles, ['haya', 'fyodor'])

    def test_finalize_is_idempotent_by_fingerprint(self):
        user_id = self._insert_chat('hayana', '重复', '2026-07-16 09:00:00')
        assistant_id = self._insert_chat('fyodor', '收到', '2026-07-16 09:01:00')
        first = moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='第一次',
        )
        second = moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='第二次',
        )
        self.assertEqual(first, second)
        conn = sqlite3.connect(self.db_path)
        count = conn.execute('SELECT COUNT(*) FROM moment_chat_collections').fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_gallery_item_includes_media(self):
        self._insert_gallery('pic-c', note='一张图', saved_at='2026-07-16 10:00:00')
        payload = moments_store.get_feed(
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
            limit=1,
            feed_type='gallery',
        )
        item = payload['items'][0]
        self.assertEqual(item['kind'], 'gallery')
        self.assertEqual(item['item_key'], 'gallery:pic-c')
        self.assertEqual(len(item['media']), 1)
        self.assertEqual(item['media'][0]['pid'], 'pic-c')
        self.assertIn('/api/gallery/photo/pic-c', item['media'][0]['url'])

    def test_gallery_iso_timestamp_paginates_without_repeat(self):
        iso_ts = '2026-07-16T09:00:00+08:00'
        for index in range(1, 5):
            self._insert_gallery(f'pic-{index}', note=f'图 {index}', saved_at=iso_ts)

        first = moments_store.get_feed(
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
            limit=2,
            feed_type='gallery',
        )
        second = moments_store.get_feed(
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
            limit=2,
            cursor=first['next_cursor'],
            feed_type='gallery',
        )

        self.assertEqual([item['item_key'] for item in first['items']], ['gallery:pic-4', 'gallery:pic-3'])
        self.assertEqual([item['item_key'] for item in second['items']], ['gallery:pic-2', 'gallery:pic-1'])
        self.assertTrue(first['has_more'])
        self.assertFalse(second['has_more'])


if __name__ == '__main__':
    unittest.main()
