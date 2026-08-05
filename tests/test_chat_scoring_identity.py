"""P-ID-CHAT — chat turn scoring identity (send / redo / edit)."""

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

import internal_state_shadow as shadow
import internal_state_store as store
import moments_turn
from chat.scoring_identity import (
    find_user_message_before,
    parse_scoring_message_id,
    resolve_scoring_user_message,
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
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            content TEXT,
            thinking TEXT,
            tool_calls TEXT,
            branches TEXT,
            branch_idx INTEGER,
            image_url TEXT DEFAULT '',
            file_url TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            choices TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now','+8 hours'))
        );
        CREATE TABLE IF NOT EXISTS chat_edit_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fork_msg_id INTEGER,
            original_content TEXT,
            messages_json TEXT
        );
        """
    )


def _make_get_db(db_path: str):
    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return get_db


def _ensure_app_importable() -> None:
    db = '/opt/frontend/memories.db'
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    _chat_schema(conn)
    conn.commit()
    conn.close()


_ensure_app_importable()
import app as app_module  # noqa: E402


class ParseMessageIdTests(unittest.TestCase):
    def test_accepts_positive_int(self):
        self.assertEqual(parse_scoring_message_id(42), 42)

    def test_rejects_none_bool_str(self):
        self.assertIsNone(parse_scoring_message_id(None))
        self.assertIsNone(parse_scoring_message_id(True))
        self.assertIsNone(parse_scoring_message_id('42'))
        self.assertIsNone(parse_scoring_message_id(0))
        self.assertIsNone(parse_scoring_message_id(-1))


class ResolveScoringUserMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'resolve.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', '用户原文')"
        )
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', 'AI')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_loads_user_row_by_id(self):
        resolved = resolve_scoring_user_message(self.get_db, 1)
        self.assertEqual(resolved, (1, '用户原文'))

    def test_rejects_assistant_row_id(self):
        self.assertIsNone(resolve_scoring_user_message(self.get_db, 2))

    def test_rejects_missing_id(self):
        self.assertIsNone(resolve_scoring_user_message(self.get_db, 999))


class TwoPhaseStreamScoringTests(unittest.TestCase):
    """Production: send persists text, stream body is only user_message_id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'twophase.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', '今天天气不错')"
        )
        conn.commit()
        conn.close()
        moments_turn.ensure_turn_schema(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_stream_body_scores_db_user_text(self):
        turn = moments_turn.insert_user_message(
            self.get_db,
            {'user_message_id': 1},
            '',
            memories_db_path=self.db_path,
        )
        self.assertEqual(turn['user_message_id'], 1)
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='是啊',
                message_id=turn['user_message_id'],
                get_db_fn=self.get_db,
            )
        self.assertTrue(ok)
        score.assert_called_once()
        excerpt = score.call_args.args[0]
        self.assertIn('今天天气不错', excerpt)
        self.assertIn('是啊', excerpt)
        self.assertTrue(excerpt.startswith('今天天气不错'))


class RedoScoringBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'redo.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', '原问题')"
        )
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', '半截')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_redo_scores_original_question_from_db(self):
        conn = self.get_db()
        assistant_id = conn.execute(
            'SELECT MAX(id) FROM chat_messages'
        ).fetchone()[0]
        user_message_id = find_user_message_before(conn, int(assistant_id))
        conn.execute('DELETE FROM chat_messages WHERE id=?', (assistant_id,))
        conn.commit()
        conn.close()
        self.assertEqual(user_message_id, 1)
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='完整回复',
                message_id=user_message_id,
                get_db_fn=self.get_db,
            )
        self.assertTrue(ok)
        excerpt = score.call_args.args[0]
        self.assertIn('原问题', excerpt)
        self.assertIn('完整回复', excerpt)


class InvalidIdScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'invalid.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', 'AI only')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_id_does_not_call_score_async(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='回复',
                message_id=999,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()

    def test_assistant_row_id_does_not_call_score_async(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='回复',
                message_id=1,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()

    def test_none_id_does_not_call_score_async(self):
        with mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='回复',
                message_id=None,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()


class EditRouteBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'edit_route.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', '旧文字')"
        )
        conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('assistant', '旧回复')"
        )
        conn.commit()
        conn.close()
        sconn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(sconn, db_path=self.db_path)
        finally:
            sconn.close()
        self.db_patch = mock.patch.object(app_module, 'DB_PATH', self.db_path)
        self.get_db_patch = mock.patch.object(app_module, 'get_db', self.get_db)
        self.db_patch.start()
        self.get_db_patch.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.get_db_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_edit_route_returns_new_id_user_rule_and_scores_edited_text(self):
        from chat import rewrite_staging as _rw
        enqueued = []
        with mock.patch.dict(os.environ, ALL_ON, clear=False), mock.patch.object(
            shadow, 'enqueue_user_rule_in_txn',
            side_effect=lambda conn, **kw: enqueued.append(kw) or True,
        ), mock.patch.object(shadow, 'drain_shadow_outbox_best_effort'), mock.patch(
            'emotion_engine.score_async',
        ) as score, mock.patch.object(
            app_module, 'invalidate_cc_resident_for_history_rewrite', return_value=True,
        ), mock.patch.object(
            _rw, 'replay_side_effects_after_activate',
        ):
            prep = self.client.post(
                '/api/chat/edit',
                json={'msg_id': 1, 'content': '新文字'},
            )
            self.assertEqual(prep.status_code, 200)
            rewrite_id = prep.get_json()['rewrite_id']
            # Active transcript still has old text until finalize.
            conn = self.get_db()
            old = conn.execute('SELECT content FROM chat_messages WHERE id=1').fetchone()
            conn.close()
            self.assertEqual(old['content'], '旧文字')
            conn = self.get_db()
            _rw.store_candidate(conn, rewrite_id, content='新回复')
            conn.commit()
            conn.close()
            resp = self.client.post(
                '/api/chat/edit/finalize', json={'rewrite_id': rewrite_id},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload['ok'])
            new_id = int(payload['message_id'])
            self.assertNotEqual(new_id, 1)
            conn = self.get_db()
            row = conn.execute(
                'SELECT content FROM chat_messages WHERE id=?', (new_id,)
            ).fetchone()
            conn.close()
            self.assertEqual(row['content'], '新文字')
            # Activation must preserve prior edit-route user_rule capture.
            self.assertEqual(len(enqueued), 1)
            self.assertEqual(enqueued[0]['message_id'], new_id)
            self.assertEqual(enqueued[0]['text'], '新文字')
            shadow.drain_shadow_outbox_best_effort.assert_called()
            # Finalize itself must score the new user identity (not the test).
            score.assert_called_once()
            excerpt = score.call_args.args[0]
            self.assertIn('新文字', excerpt)
            self.assertNotIn('旧文字', excerpt)


class TriggerTurnScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'score.db')
        self.get_db = _make_get_db(self.db_path)
        conn = self.get_db()
        _chat_schema(conn)
        conn.execute("INSERT INTO chat_messages (author, content) VALUES ('hayana', 'hi')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_when_already_scored(self):
        with mock.patch(
            'chat.scoring_identity.message_already_scored',
            return_value=True,
        ), mock.patch('emotion_engine.score_async') as score:
            ok = trigger_turn_scoring(
                assistant_text='a',
                message_id=1,
                get_db_fn=self.get_db,
            )
        self.assertFalse(ok)
        score.assert_not_called()


if __name__ == '__main__':
    unittest.main()
