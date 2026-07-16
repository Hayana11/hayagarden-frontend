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

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_after_assistant_persisted_requires_user_message_id(self):
        conn = self._get_db()
        try:
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'A')")
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'B')")
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('fyodor', 'reply')")
            conn.commit()
            assistant_id = conn.execute(
                "SELECT id FROM chat_messages WHERE author='fyodor' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()

        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        moments_intent.set_pending(self.db_path, turn['turn_key'], previous_turns=0, caption='应失败')
        moments_persistence.after_assistant_persisted(
            memories_db_path=self.db_path,
            turn_data={'turn_key': turn['turn_key']},
            assistant_message_id=int(assistant_id),
        )

        with sqlite3.connect(self.db_path) as count_conn:
            count = count_conn.execute(
                'SELECT COUNT(*) FROM moment_chat_collections'
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_after_assistant_persisted_uses_explicit_user_message_id(self):
        conn = self._get_db()
        try:
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'A')")
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'B')")
            conn.execute("INSERT INTO chat_messages (author, content) VALUES ('fyodor', 'reply')")
            conn.commit()
            assistant_id = conn.execute(
                "SELECT id FROM chat_messages WHERE author='fyodor' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()

        turn = moments_turn.begin_turn(
            {'user_message_id': 1},
            conversation_id='hayana-chat',
            memories_db_path=self.db_path,
        )
        moments_intent.set_pending(self.db_path, turn['turn_key'], previous_turns=0, caption='绑定 A')
        moments_persistence.after_assistant_persisted(
            memories_db_path=self.db_path,
            turn_data=turn,
            assistant_message_id=int(assistant_id),
        )

        item = moments_store.get_chat_collection(1, memories_db_path=self.db_path)
        self.assertIsNotNone(item)
        self.assertEqual(item['content'], '绑定 A')

    def test_begin_turn_clears_stale_pending(self):
        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        moments_intent.set_pending(self.db_path, turn['turn_key'], previous_turns=0, caption='failed turn intent')
        fresh = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        self.assertIsNone(moments_intent.pop_pending(self.db_path, turn['turn_key']))
        self.assertIsNotNone(fresh['turn_key'])

    def test_release_turn_clears_unpersisted_pending(self):
        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        moments_intent.set_pending(self.db_path, turn['turn_key'], previous_turns=0, caption='stale')
        moments_turn.release_turn(
            conversation_id='hayana-chat',
            memories_db_path=self.db_path,
            turn_key=turn['turn_key'],
            persisted=False,
        )
        self.assertIsNone(moments_intent.pop_pending(self.db_path, turn['turn_key']))

    def test_insert_user_message_records_lastrowid(self):
        def get_db():
            return self._get_db()

        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        turn = moments_turn.insert_user_message(
            get_db, turn, 'hello', memories_db_path=self.db_path, conversation_id='hayana-chat',
        )
        self.assertEqual(turn['user_message_id'], 1)
        active = moments_turn.get_active_turn(self.db_path, 'hayana-chat')
        self.assertEqual(active['user_message_id'], 1)

    def test_collect_chat_moment_uses_active_turn(self):
        turn = moments_turn.begin_turn({}, conversation_id='hayana-chat', memories_db_path=self.db_path)
        moments_turn.collect_chat_moment(
            self.db_path,
            conversation_id='hayana-chat',
            previous_turns=1,
            caption='via active turn',
        )
        pending = moments_intent.pop_pending(self.db_path, turn['turn_key'])
        self.assertIsNotNone(pending)
        self.assertEqual(pending.caption, 'via active turn')


if __name__ == '__main__':
    unittest.main()
