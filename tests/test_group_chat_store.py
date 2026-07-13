import os
import tempfile
import unittest

import group_chat_store


class GroupChatStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        group_chat_store.ensure_schema(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_rooms_have_independent_timelines(self):
        group = group_chat_store.add_message(
            'group', 'user', '一起说话', db_path=self.db_path
        )
        private = group_chat_store.add_message(
            'claude', 'user', '悄悄话', db_path=self.db_path
        )

        group_rows, _ = group_chat_store.list_messages(
            'group', db_path=self.db_path
        )
        private_rows, _ = group_chat_store.list_messages(
            'claude', db_path=self.db_path
        )

        self.assertEqual([row['id'] for row in group_rows], [group['id']])
        self.assertEqual([row['id'] for row in private_rows], [private['id']])

    def test_pagination_is_ascending_and_reports_more(self):
        for index in range(5):
            group_chat_store.add_message(
                'group', 'user', f'message {index}', db_path=self.db_path
            )

        latest, has_more = group_chat_store.list_messages(
            'group', limit=3, db_path=self.db_path
        )
        older, older_has_more = group_chat_store.list_messages(
            'group', limit=3, before=latest[0]['id'], db_path=self.db_path
        )

        self.assertEqual([row['content'] for row in latest], [
            'message 2', 'message 3', 'message 4'
        ])
        self.assertTrue(has_more)
        self.assertEqual([row['content'] for row in older], ['message 0', 'message 1'])
        self.assertFalse(older_has_more)

    def test_rejects_unknown_room_author_and_empty_content(self):
        with self.assertRaises(ValueError):
            group_chat_store.add_message(
                'somewhere', 'user', 'hello', db_path=self.db_path
            )
        with self.assertRaises(ValueError):
            group_chat_store.add_message(
                'group', 'stranger', 'hello', db_path=self.db_path
            )
        with self.assertRaises(ValueError):
            group_chat_store.add_message(
                'group', 'user', '  ', db_path=self.db_path
            )

    def test_clear_only_deletes_selected_room(self):
        group_chat_store.add_message('group', 'user', 'one', db_path=self.db_path)
        group_chat_store.add_message('codex', 'user', 'two', db_path=self.db_path)

        self.assertEqual(group_chat_store.clear_room('group', self.db_path), 1)
        group_rows, _ = group_chat_store.list_messages('group', db_path=self.db_path)
        codex_rows, _ = group_chat_store.list_messages('codex', db_path=self.db_path)
        self.assertEqual(group_rows, [])
        self.assertEqual(len(codex_rows), 1)


if __name__ == '__main__':
    unittest.main()
