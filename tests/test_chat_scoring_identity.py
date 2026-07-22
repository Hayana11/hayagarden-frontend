"""P-ID-CHAT — chat turn scoring identity (send / redo / edit)."""

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

import internal_state_shadow as shadow
import internal_state_store as store
import moments_turn
from chat.scoring_identity import (
    find_user_message_before,
    message_already_scored,
    parse_scoring_message_id,
    trigger_turn_scoring,
)

ALL_ON = {
    shadow.SHADOW_ENABLED_ENV: '1',
    shadow.SCORE_PROOF_ENABLED_ENV: '1',
    shadow.USER_EVENTS_ENABLED_ENV: '1',
}


def _chat_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            content TEXT,
            thinking TEXT,
            tool_calls TEXT,
            branches TEXT,
            branch_idx INTEGER,
            image_url TEXT,
            file_url TEXT,
            file_name TEXT,
            created_at TEXT DEFAULT (datetime('now','+8 hours'))
        );
        CREATE TABLE chat_edit_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fork_msg_id INTEGER,
            original_content TEXT,
            messages_json TEXT
        );
        """
    )


class ParseMessageIdTests(unittest.TestCase):
    def test_accepts_positive_int(self):
        self.assertEqual(parse_scoring_message_id(42), 42)

    def test_rejects_none_bool_str(self):
        self.assertIsNone(parse_scoring_message_id(None))
        self.assertIsNone(parse_scoring_message_id(True))
        self.assertIsNone(parse_scoring_message_id('42'))
        self.assertIsNone(parse_scoring_message_id(0))
        self.assertIsNone(parse_scoring_message_id(-1))


class FindUserMessageBeforeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(str(Path(self.tmp.name) / 't.db'))
        self.conn.row_factory = sqlite3.Row
        _chat_schema(self.conn)
        self.conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', 'u1')"
        )
        self.conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', 'a1')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_finds_latest_user_before_assistant(self):
        row = self.conn.execute('SELECT MAX(id) FROM chat_messages').fetchone()[0]
        uid = find_user_message_before(self.conn, int(row))
        self.assertEqual(uid, 1)


class TriggerTurnScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'score.db')

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db
        conn = get_db()
        _chat_schema(conn)
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'hi')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_none_before_score_async(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                user_excerpt='u',
                assistant_text='a',
                message_id=None,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()

    def test_schedules_score_for_valid_id(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                user_excerpt='你好',
                assistant_text='回复',
                message_id=1,
                get_db_fn=self.get_db,
            )
        self.assertTrue(ok)
        score.assert_called_once()
        self.assertEqual(score.call_args.kwargs['message_id'], 1)

    def test_skips_when_already_scored(self):
        with mock.patch(
            'chat.scoring_identity.message_already_scored',
            return_value=True,
        ), mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                user_excerpt='u',
                assistant_text='a',
                message_id=1,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()


class RedoStreamIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'redo.db')

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db
        conn = get_db()
        _chat_schema(conn)
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', '原问题')")
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('assistant', '半截')")
        conn.commit()
        conn.close()
        moments_turn.ensure_turn_schema(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_stream_body_reuses_user_message_id(self):
        turn = moments_turn.insert_user_message(
            self.get_db,
            {'user_message_id': 1},
            '',
            memories_db_path=self.db_path,
        )
        self.assertEqual(turn['user_message_id'], 1)

    def test_redo_can_score_after_interrupt(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                user_excerpt='原问题',
                assistant_text='完整回复',
                message_id=1,
                get_db_fn=self.get_db,
            )
        self.assertTrue(ok)
        score.assert_called_once()


class EditNewIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'edit.db')
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        _chat_schema(self.conn)
        self.conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', '旧文字')"
        )
        self.conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', '旧回复')"
        )
        self.conn.commit()
        sconn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(sconn, db_path=self.db_path)
        finally:
            sconn.close()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_edit_simulation_uses_new_message_id(self):
        old_user_id = 1
        assistant_id = 2
        new_content = '新文字'
        tail = self.conn.execute(
            'SELECT * FROM chat_messages WHERE id >= ? ORDER BY id ASC',
            (old_user_id,),
        ).fetchall()
        self.assertEqual(len(tail), 2)
        self.conn.execute('DELETE FROM chat_messages WHERE id >= ?', (old_user_id,))
        cur = self.conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', ?)",
            (new_content,),
        )
        new_user_id = int(cur.lastrowid)
        self.conn.commit()
        self.assertNotEqual(new_user_id, old_user_id)
        with mock.patch.dict(__import__('os').environ, ALL_ON, clear=False), mock.patch.object(
            shadow, 'enqueue_user_rule_in_txn',
            return_value=True,
        ) as enq, mock.patch.object(shadow, 'drain_shadow_outbox_best_effort'):
            row = self.conn.execute(
                'SELECT created_at FROM chat_messages WHERE id=?',
                (new_user_id,),
            ).fetchone()
            shadow.enqueue_user_rule_in_txn(
                self.conn,
                message_id=new_user_id,
                text=new_content,
                created_at=str(row['created_at']),
                previous_user_at=None,
            )
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs['message_id'], new_user_id)
        self.assertEqual(enq.call_args.kwargs['text'], new_content)
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                user_excerpt=new_content,
                assistant_text='新回复',
                message_id=new_user_id,
                get_db_fn=lambda: sqlite3.connect(self.db_path),
            )
        self.assertTrue(ok)
        score.assert_called_once_with(mock.ANY, message_id=new_user_id)
        self.assertNotEqual(score.call_args.kwargs['message_id'], old_user_id)


class SendUserRuleScoredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'send.db')

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db
        conn = get_db()
        _chat_schema(conn)
        conn.commit()
        conn.close()
        sconn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(sconn, db_path=self.db_path)
        finally:
            sconn.close()
        moments_turn.ensure_turn_schema(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_send_enqueues_user_rule_then_scores(self):
        enqueued = []
        with mock.patch.object(
            shadow, 'enqueue_user_rule_in_txn',
            side_effect=lambda conn, **kw: enqueued.append(kw) or True,
        ), mock.patch.object(shadow, 'drain_shadow_outbox_best_effort'), mock.patch.dict(
            __import__('os').environ, ALL_ON, clear=False,
        ), mock.patch('emotion_engine.score_async') as score:
            turn = moments_turn.insert_user_message(
                self.get_db, {}, '今天天气不错',
                memories_db_path=self.db_path,
            )
            trigger_turn_scoring(
                user_excerpt='今天天气不错',
                assistant_text='是啊',
                message_id=turn['user_message_id'],
                get_db_fn=self.get_db,
            )
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]['message_id'], turn['user_message_id'])
        score.assert_called_once()
        self.assertEqual(score.call_args.kwargs['message_id'], turn['user_message_id'])


class RouteSourceTests(unittest.TestCase):
    def test_regen_prepare_returns_user_message_id(self):
        src = Path(ROOT, 'app.py').read_text(encoding='utf-8')
        block = src.split('def regen_prepare', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('find_user_message_before', block)
        self.assertIn("'user_message_id'", block)

    def test_edit_returns_new_message_id(self):
        src = Path(ROOT, 'app.py').read_text(encoding='utf-8')
        block = src.split('def edit_message', 1)[1].split('\ndef ', 1)[0]
        self.assertIn("'message_id': new_message_id", block)
        self.assertNotIn('UPDATE chat_messages SET content=?', block)

    def test_gateway_uses_trigger_turn_scoring(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('trigger_turn_scoring', src)
        self.assertIn("message_id=_turn_data.get('user_message_id')", src)
        self.assertNotIn('_ee.score_async(', src)


if __name__ == '__main__':
    unittest.main()
