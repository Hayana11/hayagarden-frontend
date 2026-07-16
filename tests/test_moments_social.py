import os
import sqlite3
import tempfile
import unittest

import moments_social
import moments_store


class MomentsSocialTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        moments_store.ensure_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_toggle_like_and_unlike(self):
        social = moments_social.toggle_reaction(
            'thought:1',
            'like',
            memories_db_path=self.db_path,
        )
        self.assertEqual(social['likes'], 1)
        self.assertEqual(social['my_reaction'], 'like')

        social = moments_social.toggle_reaction(
            'thought:1',
            'like',
            memories_db_path=self.db_path,
        )
        self.assertEqual(social['likes'], 0)
        self.assertIsNone(social['my_reaction'])

    def test_switch_dislike_to_like(self):
        moments_social.toggle_reaction('thought:2', 'dislike', memories_db_path=self.db_path)
        social = moments_social.toggle_reaction(
            'thought:2',
            'like',
            memories_db_path=self.db_path,
        )
        self.assertEqual(social['likes'], 1)
        self.assertEqual(social['dislikes'], 0)
        self.assertEqual(social['my_reaction'], 'like')

    def test_add_comment_updates_counts(self):
        result = moments_social.add_comment(
            'thought:3',
            '好喜欢这条',
            memories_db_path=self.db_path,
        )
        self.assertEqual(result['comment']['content'], '好喜欢这条')
        self.assertEqual(result['social']['comments'], 1)

        comments = moments_social.list_comments('thought:3', memories_db_path=self.db_path)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]['content'], '好喜欢这条')

    def test_attach_social_to_items_batch(self):
        moments_social.toggle_reaction('thought:4', 'like', memories_db_path=self.db_path)
        moments_social.add_comment('thought:4', '批注', memories_db_path=self.db_path)
        items = moments_social.attach_social_to_items(
            [{'item_key': 'thought:4', 'content': 'x'}],
            memories_db_path=self.db_path,
        )
        self.assertEqual(items[0]['social']['likes'], 1)
        self.assertEqual(items[0]['social']['comments'], 1)
        self.assertEqual(items[0]['social']['my_reaction'], 'like')

    def test_feed_includes_social(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "author TEXT DEFAULT 'fyodor', "
            "created_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO posts (type, content, created_at) VALUES ('THOUGHT', '念头', '2026-07-16 09:00:00')"
        )
        conn.commit()
        conn.close()

        moments_social.toggle_reaction('thought:1', 'like', memories_db_path=self.db_path)
        payload = moments_store.get_feed(memories_db_path=self.db_path, limit=10)
        self.assertEqual(payload['items'][0]['social']['likes'], 1)
        self.assertEqual(payload['items'][0]['social']['my_reaction'], 'like')


if __name__ == '__main__':
    unittest.main()
