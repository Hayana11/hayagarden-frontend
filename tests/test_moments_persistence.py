import os
import sqlite3
import tempfile
import unittest

import moments_intent
import moments_persistence
import moments_store
import moments_turn


class MomentsPersistenceTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
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
        moments_store.ensure_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)
        moments_intent.clear_pending('hayana-chat')

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_after_assistant_persisted_requires_user_message_id(self):
        conn = self._get_db()
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'A')")
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'B')")
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('fyodor', 'reply')")
        conn.commit()
        assistant_id = conn.execute(
            "SELECT id FROM chat_messages WHERE author='fyodor' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()

        moments_intent.set_pending('hayana-chat', previous_turns=0, caption='应失败')
        moments_persistence.after_assistant_persisted(
            memories_db_path=self.db_path,
            turn_data={},
            assistant_message_id=int(assistant_id),
        )

        count = sqlite3.connect(self.db_path).execute(
            'SELECT COUNT(*) FROM moment_chat_collections'
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_after_assistant_persisted_uses_explicit_user_message_id(self):
        conn = self._get_db()
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'A')")
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'B')")
        user_b = conn.execute(
            "SELECT id FROM chat_messages WHERE content='B'"
        ).fetchone()[0]
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('fyodor', 'reply')")
        conn.commit()
        assistant_id = conn.execute(
            "SELECT id FROM chat_messages WHERE author='fyodor' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()

        moments_intent.set_pending('hayana-chat', previous_turns=0, caption='绑定 A')
        moments_persistence.after_assistant_persisted(
            memories_db_path=self.db_path,
            turn_data={'user_message_id': 1},
            assistant_message_id=int(assistant_id),
        )

        item = moments_store.get_chat_collection(1, memories_db_path=self.db_path)
        self.assertIsNotNone(item)
        self.assertEqual(item['content'], '绑定 A')
        del user_b

    def test_begin_turn_clears_stale_pending(self):
        moments_intent.set_pending('hayana-chat', previous_turns=0, caption='failed turn intent')
        moments_turn.begin_turn({}, conversation_id='hayana-chat')
        self.assertIsNone(moments_intent.pop_pending('hayana-chat'))

    def test_release_turn_clears_unpersisted_pending(self):
        moments_intent.set_pending('hayana-chat', previous_turns=0, caption='stale')
        moments_turn.release_turn(conversation_id='hayana-chat', persisted=False)
        self.assertIsNone(moments_intent.pop_pending('hayana-chat'))

    def test_insert_user_message_records_lastrowid(self):
        def get_db():
            return self._get_db()

        turn = moments_turn.begin_turn({})
        turn = moments_turn.insert_user_message(get_db, turn, 'hello')
        self.assertEqual(turn['user_message_id'], 1)


if __name__ == '__main__':
    unittest.main()
