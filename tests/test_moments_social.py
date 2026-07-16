import io
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from PIL import Image

import moments_cover
import moments_social
import moments_store

OWNER_ENV = {'MOMENTS_OWNER_TOKEN': 'test-owner-token'}


class MomentsSocialTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, OWNER_ENV, clear=False)
        self._env.start()
        import moments_auth
        moments_auth._get_owner_token = moments_auth.owner_token_getter()

        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
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
            "created_at TEXT, "
            "storage_key TEXT, "
            "mem_id INTEGER"
            ")"
        )
        gconn.commit()
        gconn.close()

    def tearDown(self):
        self._env.stop()
        os.unlink(self.db_path)
        os.unlink(self.gallery_db_path)

    def _insert_thought(self, content='念头', thought_id=None):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "author TEXT DEFAULT 'fyodor', "
            "created_at TEXT"
            ")"
        )
        if thought_id is None:
            conn.execute(
                "INSERT INTO posts (type, content, created_at) VALUES ('THOUGHT', ?, '2026-07-16 09:00:00')",
                (content,),
            )
            thought_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            conn.execute(
                "INSERT INTO posts (id, type, content, created_at) VALUES (?, 'THOUGHT', ?, '2026-07-16 09:00:00')",
                (thought_id, content),
            )
        conn.commit()
        conn.close()
        return int(thought_id)

    def _insert_gallery(self, pid, *, storage_key=None):
        conn = sqlite3.connect(self.gallery_db_path)
        conn.execute(
            "INSERT INTO gallery_photos (pid, note, summary, keywords, width, height, saved_at, created_at, storage_key, mem_id) "
            "VALUES (?, '图', '图', '[]', 100, 100, '2026-07-16 09:00:00', '2026-07-16 09:00:00', ?, NULL)",
            (pid, storage_key or f'{pid}.jpg'),
        )
        conn.commit()
        conn.close()

    def test_toggle_like_and_unlike(self):
        thought_id = self._insert_thought()
        social = moments_social.toggle_reaction(
            f'thought:{thought_id}',
            'like',
            memories_db_path=self.db_path,
        )
        self.assertEqual(social['likes'], 1)
        self.assertEqual(social['my_reaction'], 'like')

        social = moments_social.toggle_reaction(
            f'thought:{thought_id}',
            'like',
            memories_db_path=self.db_path,
        )
        self.assertEqual(social['likes'], 0)
        self.assertIsNone(social['my_reaction'])

    def test_switch_dislike_to_like(self):
        thought_id = self._insert_thought()
        key = f'thought:{thought_id}'
        moments_social.toggle_reaction(key, 'dislike', memories_db_path=self.db_path)
        social = moments_social.toggle_reaction(key, 'like', memories_db_path=self.db_path)
        self.assertEqual(social['likes'], 1)
        self.assertEqual(social['dislikes'], 0)
        self.assertEqual(social['my_reaction'], 'like')

    def test_add_comment_updates_counts(self):
        thought_id = self._insert_thought()
        key = f'thought:{thought_id}'
        result = moments_social.add_comment(key, '好喜欢这条', memories_db_path=self.db_path)
        self.assertEqual(result['comment']['content'], '好喜欢这条')
        self.assertEqual(result['social']['comments'], 1)

        comments = moments_social.list_comments(key, memories_db_path=self.db_path)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]['content'], '好喜欢这条')

    def test_attach_social_to_items_batch(self):
        thought_id = self._insert_thought()
        key = f'thought:{thought_id}'
        moments_social.toggle_reaction(key, 'like', memories_db_path=self.db_path)
        moments_social.add_comment(key, '批注', memories_db_path=self.db_path)
        items = moments_social.attach_social_to_items(
            [{'item_key': key, 'content': 'x'}],
            memories_db_path=self.db_path,
        )
        self.assertEqual(items[0]['social']['likes'], 1)
        self.assertEqual(items[0]['social']['comments'], 1)
        self.assertEqual(items[0]['social']['my_reaction'], 'like')

    def test_feed_includes_social(self):
        thought_id = self._insert_thought('念头')
        moments_social.toggle_reaction(
            f'thought:{thought_id}',
            'like',
            memories_db_path=self.db_path,
        )
        payload = moments_store.get_feed(memories_db_path=self.db_path, limit=10)
        self.assertEqual(payload['items'][0]['social']['likes'], 1)
        self.assertEqual(payload['items'][0]['social']['my_reaction'], 'like')

    def test_reject_missing_thought(self):
        with self.assertRaises(LookupError):
            moments_social.toggle_reaction(
                'thought:999999',
                'like',
                memories_db_path=self.db_path,
            )

    def test_reject_missing_gallery(self):
        with self.assertRaises(LookupError):
            moments_social.add_comment(
                'gallery:not-real',
                '评论',
                memories_db_path=self.db_path,
                gallery_db_path=self.gallery_db_path,
            )

    def test_gallery_comment_requires_existing_pid(self):
        self._insert_gallery('pic-real')
        result = moments_social.add_comment(
            'gallery:pic-real',
            '好看',
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
        )
        self.assertEqual(result['social']['comments'], 1)

    def test_concurrent_double_toggle_cancels_like(self):
        thought_id = self._insert_thought()
        key = f'thought:{thought_id}'
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            moments_social.toggle_reaction(key, 'like', memories_db_path=self.db_path)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = moments_social.get_item_social(key, memories_db_path=self.db_path)
        self.assertEqual(final['likes'], 0)
        self.assertIsNone(final['my_reaction'])

    def test_delete_chat_collection_clears_social(self):
        user_id = 1
        assistant_id = 2
        conn = sqlite3.connect(self.db_path)
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
        conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (?, 'hayana', 'A')", (user_id,))
        conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (?, 'fyodor', 'B')", (assistant_id,))
        conn.commit()
        conn.close()

        collection_id = moments_store.finalize_pending_chat_collection(
            memories_db_path=self.db_path,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            previous_turns=0,
            caption='收藏',
        )
        key = f'chat-collection:{collection_id}'
        moments_social.toggle_reaction(key, 'like', memories_db_path=self.db_path)
        moments_social.add_comment(key, '好', memories_db_path=self.db_path)

        deleted = moments_store.delete_chat_collection(collection_id, memories_db_path=self.db_path)
        self.assertTrue(deleted)

        social = moments_social.get_item_social(key, memories_db_path=self.db_path)
        self.assertEqual(social['likes'], 0)
        self.assertEqual(social['comments'], 0)

    def _reaction_count(self, item_key: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute(
                'SELECT COUNT(*) FROM moment_reactions WHERE item_key=?',
                (item_key,),
            ).fetchone()[0])
        finally:
            conn.close()

    def test_delete_race_does_not_leave_orphan_reactions(self):
        conn = sqlite3.connect(self.db_path)
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

        for round_idx in range(30):
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO chat_messages (author, content) VALUES ('hayana', ?)",
                (f'A-{round_idx}',),
            )
            user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute(
                "INSERT INTO chat_messages (author, content) VALUES ('fyodor', ?)",
                (f'B-{round_idx}',),
            )
            assistant_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.commit()
            conn.close()

            collection_id = moments_store.finalize_pending_chat_collection(
                memories_db_path=self.db_path,
                user_message_id=user_id,
                assistant_message_id=assistant_id,
                previous_turns=0,
                caption=f'收藏 {round_idx}',
            )
            key = f'chat-collection:{collection_id}'
            barrier = threading.Barrier(2)

            def delete_worker(cid=collection_id):
                barrier.wait()
                moments_store.delete_chat_collection(cid, memories_db_path=self.db_path)

            def react_worker(item_key=key):
                barrier.wait()
                try:
                    moments_social.toggle_reaction(item_key, 'like', memories_db_path=self.db_path)
                except LookupError:
                    pass

            threads = [
                threading.Thread(target=delete_worker),
                threading.Thread(target=react_worker),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(self._reaction_count(key), 0)

    def test_delete_thought_post_clears_social(self):
        thought_id = self._insert_thought()
        key = f'thought:{thought_id}'
        moments_social.toggle_reaction(key, 'like', memories_db_path=self.db_path)
        moments_social.add_comment(key, '批注', memories_db_path=self.db_path)

        deleted = moments_store.delete_post(thought_id, memories_db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(self._reaction_count(key), 0)
        conn = sqlite3.connect(self.db_path)
        try:
            comments = conn.execute(
                'SELECT COUNT(*) FROM moment_comments WHERE item_key=?',
                (key,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(comments, 0)

    def test_delete_memory_post_without_social_cleanup(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS posts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "type TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "author TEXT DEFAULT 'fyodor', "
            "created_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO posts (type, content, created_at) VALUES ('MEMORY', '核心记忆', '2026-07-16 09:00:00')"
        )
        memory_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        conn.close()

        deleted = moments_store.delete_post(memory_id, memories_db_path=self.db_path)
        self.assertTrue(deleted)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute('SELECT 1 FROM posts WHERE id=?', (memory_id,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)

    def test_delete_gallery_item_clears_social(self):
        self._insert_gallery('pic-del')
        key = 'gallery:pic-del'
        moments_social.toggle_reaction(
            key,
            'like',
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
        )

        deleted, storage_key, mem_id = moments_store.delete_gallery_item_with_social(
            'pic-del',
            memories_db_path=self.db_path,
            gallery_db_path=self.gallery_db_path,
        )
        self.assertTrue(deleted)
        self.assertEqual(storage_key, 'pic-del.jpg')
        self.assertIsNone(mem_id)
        self.assertEqual(self._reaction_count(key), 0)

    def test_delete_gallery_race_does_not_leave_orphan_reactions(self):
        for round_idx in range(20):
            pid = f'pic-race-{round_idx}'
            self._insert_gallery(pid)
            key = f'gallery:{pid}'
            barrier = threading.Barrier(2)

            def delete_worker(photo_id=pid):
                barrier.wait()
                moments_store.delete_gallery_item_with_social(
                    photo_id,
                    memories_db_path=self.db_path,
                    gallery_db_path=self.gallery_db_path,
                )

            def react_worker(item_key=key):
                barrier.wait()
                try:
                    moments_social.toggle_reaction(
                        item_key,
                        'like',
                        memories_db_path=self.db_path,
                        gallery_db_path=self.gallery_db_path,
                    )
                except LookupError:
                    pass

            threads = [
                threading.Thread(target=delete_worker),
                threading.Thread(target=react_worker),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(self._reaction_count(key), 0)


class MomentsCoverTests(unittest.TestCase):
    def test_rejects_non_image_bytes(self):
        with self.assertRaises(ValueError):
            moments_cover.encode_cover_image(b'<script>alert(1)</script>')

    def test_read_bounded_rejects_oversize(self):
        stream = io.BytesIO(b'x' * (moments_cover.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            moments_cover.read_bounded(stream)

    def test_encodes_valid_png(self):
        buf = io.BytesIO()
        Image.new('RGB', (8, 8), color=(120, 80, 60)).save(buf, format='PNG')
        encoded = moments_cover.encode_cover_image(buf.getvalue())
        self.assertTrue(encoded.startswith(b'\xff\xd8'))

    def test_rejects_decompression_bomb_dimensions(self):
        buf = io.BytesIO()
        Image.new('RGB', (5000, 5000), color=(10, 20, 30)).save(buf, format='PNG', optimize=True)
        with self.assertRaises(ValueError):
            moments_cover.encode_cover_image(buf.getvalue())


if __name__ == '__main__':
    unittest.main()
