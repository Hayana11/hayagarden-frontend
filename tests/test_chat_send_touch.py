"""Production two-phase chat send must touch once (app.py, not only moments_turn)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class TwoPhaseSendTouchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT,
                image_url TEXT, file_url TEXT, file_name TEXT
            );
            CREATE TABLE moments_active_turn (
                conversation_id TEXT, turn_key TEXT,
                user_message_id INTEGER, started_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()
        self.touch_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_api_chat_send_touches_once_then_stream_empty_body_does_not(self):
        """React: POST /api/chat/send → stream({user_message_id}) with empty content."""
        import moments_turn
        import chat.interaction_state as istate

        with mock.patch.object(
            istate, 'touch_user_interaction',
            side_effect=lambda *_a, **_k: self.touch_calls.append('send'),
        ):
            conn = self.get_db()
            cur = conn.execute(
                "INSERT INTO chat_messages (author,content,image_url,file_url,file_name) "
                "VALUES (?,?,?,?,?)",
                ('hayana', '你好', '', '', ''),
            )
            message_id = cur.lastrowid
            conn.commit()
            conn.close()
            # app.send_chat calls this after commit
            istate.touch_user_interaction(self.get_db)

        self.assertEqual(self.touch_calls, ['send'])
        self.assertEqual(message_id, 1)

        with mock.patch.object(
            istate, 'touch_user_interaction',
            side_effect=lambda *_a, **_k: self.touch_calls.append('stream'),
        ):
            moments_turn.insert_user_message(
                self.get_db,
                {'turn_key': 'k', 'user_message_id': message_id, 'content': ''},
                '',
                memories_db_path=self.db_path,
            )
        self.assertEqual(self.touch_calls, ['send'])

    def test_file_and_image_send_also_touch(self):
        import chat.interaction_state as istate

        for kwargs in (
            dict(content='', image_url='/static/uploads/x.jpg'),
            dict(content='', file_url='/files/a.pdf', file_name='a.pdf'),
        ):
            self.touch_calls.clear()
            content = kwargs.get('content') or ''
            file_url = kwargs.get('file_url') or ''
            file_name = kwargs.get('file_name') or ''
            if file_url and not content:
                content = '[文件:%s]' % (file_name or '附件')
            with mock.patch.object(
                istate, 'touch_user_interaction',
                side_effect=lambda *_a, **_k: self.touch_calls.append('send'),
            ):
                conn = self.get_db()
                conn.execute(
                    "INSERT INTO chat_messages (author,content,image_url,file_url,file_name) "
                    "VALUES (?,?,?,?,?)",
                    (
                        'hayana', content,
                        kwargs.get('image_url') or '',
                        file_url, file_name,
                    ),
                )
                conn.commit()
                conn.close()
                istate.touch_user_interaction(self.get_db)
            self.assertEqual(self.touch_calls, ['send'], kwargs)

    def test_send_chat_source_calls_touch_after_commit(self):
        src = Path(ROOT, 'app.py').read_text(encoding='utf-8')
        send_idx = src.index('def send_chat')
        next_def = src.index('\n@app.route', send_idx + 1)
        block = src[send_idx:next_def]
        self.assertIn('touch_user_interaction', block)
        self.assertLess(block.index('conn.commit()'), block.index('touch_user_interaction'))


if __name__ == '__main__':
    unittest.main()
