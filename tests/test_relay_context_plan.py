from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chat import relay_context_plan
from continuity.context_plan import ContextBudgetPolicy
from continuity.producer import run_continuity_producer
from continuity.sealing import SealingPolicy


class RelayContextPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'memories.db'
        self.identity = {
            'chat_id': 'default',
            'context_id': 7,
            'context_epoch': 3,
            'resident_generation': 9,
        }
        conn = sqlite3.connect(self.db)
        conn.execute(
            'CREATE TABLE chat_messages ('
            'id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, '
            'created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
            'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, '
            'file_url TEXT, file_name TEXT)'
        )
        conn.execute(
            'CREATE TABLE daily_message_contexts ('
            'message_id INTEGER PRIMARY KEY, context_id INTEGER NOT NULL, '
            'context_epoch INTEGER NOT NULL, resident_generation INTEGER NOT NULL, '
            'role TEXT NOT NULL)'
        )
        conn.execute(
            'CREATE TABLE wake_log ('
            'id INTEGER PRIMARY KEY, wake_run_id TEXT, chat_id TEXT, '
            'context_id INTEGER, context_epoch INTEGER, resident_generation INTEGER)'
        )
        conn.commit()
        conn.close()
        self._add_turn(1, 'old user one', 'old assistant one')
        self._add_turn(3, 'old user two', 'old assistant two')

        result = run_continuity_producer(
            source_db_path=self.db,
            continuity_store_path=self.db,
            policy=SealingPolicy(target_logical_size=1, max_completed_turns=1),
            window_identity_reader=lambda _conn, _chat_id: self.identity,
            capture_authority=lambda: SimpleNamespace(
                provider='api_relay', model_identity='claude-opus-5-5'
            ),
            generate_fn=lambda _request, _authority: SimpleNamespace(
                text='shared canonical compressed chunk',
                provider='api_relay',
                model_identity='claude-opus-5-5',
                actual_executor='test-relay',
                usage=None,
            ),
            request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            persona_text='\n'.join(f'## {x}\nsection {x}' for x in 'ABCDEF'),
            now='2026-11-13T00:00:00+00:00',
        )
        self.assertEqual(result.status, 'ready')

        self._insert_message(5, 'hayana', 'current relay user')
        self._map(5, 'user')

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_message(self, mid: int, author: str, content: str) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                mid, author, content, '', f'2026-11-13 00:0{mid}:00', '', '', 0,
                '', 'chat', '[]', '', '', '',
            ),
        )
        conn.commit()
        conn.close()

    def _map(self, mid: int, role: str) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO daily_message_contexts VALUES (?,?,?,?,?)',
            (mid, 7, 3, 9, role),
        )
        conn.commit()
        conn.close()

    def _add_turn(self, user_id: int, user: str, assistant: str) -> None:
        self._insert_message(user_id, 'hayana', user)
        self._insert_message(user_id + 1, 'assistant', assistant)
        self._map(user_id, 'user')
        self._map(user_id + 1, 'assistant')

    def test_build_messages_consumes_ready_chunk_and_current_raw_request(self):
        policy = ContextBudgetPolicy(
            token_budget=100000,
            reserve_budget=1000,
            recent_raw_target=0,
            history_budget_limit=100000,
        )
        with patch.object(relay_context_plan, 'bind_current_user', return_value=self.identity), \
             patch('chat.daily_runtime._context_plan_policy', return_value=(policy, 'test-policy')):
            messages, identity, meta = relay_context_plan.build_messages(
                user_message_id=5,
                db_path=str(self.db),
                system=[{'type': 'text', 'text': 'stable system'}],
                static_dir=self.tmp.name,
                read_file_fn=lambda _static, _url: None,
                img_block_fn=lambda _url: None,
            )
        self.assertEqual(identity, self.identity)
        self.assertGreaterEqual(meta['chunk_count'], 1)
        joined = '\n'.join(
            str(m.get('content') or '') for m in messages
            if isinstance(m.get('content'), str)
        )
        self.assertIn('shared canonical compressed chunk', joined)
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertIn('current relay user', str(messages[-1]['content']))

    def test_bind_helpers_use_same_canonical_identity(self):
        with patch(
            'chat.window_identity.capture_current_window_identity',
            return_value=self.identity,
        ) as capture, patch(
            'chat.daily_context.record_daily_message_context'
        ) as record:
            identity = relay_context_plan.bind_current_user(
                message_id=99, db_path=str(self.db)
            )
        self.assertEqual(identity, self.identity)
        capture.assert_called_once_with(db_path=str(self.db))
        record.assert_called_once_with(
            99, context_id=7, context_epoch=3, resident_generation=9,
            role='user', db_path=str(self.db),
        )

        with patch('chat.daily_context.record_daily_message_context') as record:
            relay_context_plan.bind_assistant(
                message_id=100, identity=self.identity, db_path=str(self.db)
            )
        record.assert_called_once_with(
            100, context_id=7, context_epoch=3, resident_generation=9,
            role='assistant', db_path=str(self.db),
        )


if __name__ == '__main__':
    unittest.main()
