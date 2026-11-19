from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import daily_context as dc
from chat import unified_heartbeat_a1 as uh
from chat.claude_event_mapping import (
    MappingRejected,
    ROLE_ASSISTANT,
    ROLE_PROVIDER_INPUT,
    ROLE_TOOL_RESULT_USER,
    ROLE_TOOL_USE,
    _load_auth_rounds_after,
    plan_shared_wake_native_event_mappings,
)
from chat.claude_transcript_reader import read_transcript_range
from wake import executor


class WakeNativeBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wake-native-binding-')
        self.db_path = str(Path(self.tmp.name) / 'test.db')
        self.transcript_path = str(Path(self.tmp.name) / 'wake.jsonl')
        self.context_id = 12
        self.context_epoch = 3
        self.resident_generation = 2
        self.process_generation = 4
        self.session_id = 'sid-wake-native'
        self.start_offset = 0

        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                cache_info TEXT DEFAULT '',
                source_kind TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thoughts TEXT,
                action TEXT,
                content TEXT,
                consumed INTEGER DEFAULT 0,
                woke_at TEXT,
                cache_info TEXT,
                wake_run_id TEXT,
                chat_id TEXT,
                context_id INTEGER,
                context_epoch INTEGER,
                resident_generation INTEGER
            );
            """
        )
        conn.commit()
        conn.close()
        dc.ensure_schema(self.db_path)

        self._write_transcript()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO context_claude_sessions (
                context_id, context_epoch, resident_generation, chat_id,
                claude_session_id, transcript_path, source, process_generation,
                scan_offset, scan_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.context_id,
                self.context_epoch,
                self.resident_generation,
                'default',
                self.session_id,
                self.transcript_path,
                'test',
                self.process_generation,
                self.start_offset,
                'READY',
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_transcript(self, *, with_tools=False):
        user_uuid = 'wake-provider-input'
        assistant_uuid = 'wake-assistant-output'
        rows = [{
            'type': 'user',
            'uuid': user_uuid,
            'parentUuid': None,
            'sessionId': self.session_id,
            'message': {'role': 'user', 'content': 'internal wake input'},
        }]
        if with_tools:
            tool_uuid = 'wake-tool-use'
            result_uuid = 'wake-tool-result'
            rows.extend([
                {
                    'type': 'assistant',
                    'uuid': tool_uuid,
                    'parentUuid': user_uuid,
                    'sessionId': self.session_id,
                    'message': {
                        'role': 'assistant',
                        'content': [{
                            'type': 'tool_use',
                            'id': 'tool-1',
                            'name': 'lookup',
                            'input': {'q': 'one'},
                        }],
                    },
                },
                {
                    'type': 'user',
                    'uuid': result_uuid,
                    'parentUuid': tool_uuid,
                    'sessionId': self.session_id,
                    'message': {
                        'role': 'user',
                        'content': [{
                            'type': 'tool_result',
                            'tool_use_id': 'tool-1',
                            'content': 'one',
                        }],
                    },
                },
            ])
            assistant_parent = result_uuid
        else:
            assistant_parent = user_uuid
        rows.append({
            'type': 'assistant',
            'uuid': assistant_uuid,
            'parentUuid': assistant_parent,
            'sessionId': self.session_id,
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': 'Wake response'}],
            },
        })
        raw = b''.join(
            (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8')
            for row in rows
        )
        Path(self.transcript_path).write_bytes(raw)
        self.end_offset = len(raw)
        graph = read_transcript_range(self.transcript_path, 0, self.end_offset)
        self.events = plan_shared_wake_native_event_mappings(
            graph,
            context_id=self.context_id,
            context_epoch=self.context_epoch,
            resident_generation=self.resident_generation,
            claude_session_id=self.session_id,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
        )

    def _binding(self):
        return {
            'context_id': self.context_id,
            'context_epoch': self.context_epoch,
            'resident_generation': self.resident_generation,
            'process_generation': self.process_generation,
            'claude_session_id': self.session_id,
            'transcript_path': self.transcript_path,
            'start_offset': self.start_offset,
            'end_offset': self.end_offset,
            'events': [dict(event) for event in self.events],
        }

    def _window_identity(self):
        return {
            'chat_id': 'default',
            'context_id': self.context_id,
            'context_epoch': self.context_epoch,
            'resident_generation': self.resident_generation,
        }

    def _cache_info(self):
        return {
            'provider': 'claude_code',
            'source': 'wake',
            'b3_authority': True,
            'unified_chat_resident': True,
        }

    def _execute(self, binding=None, *, window_identity=None):
        return executor.execute(
            'message',
            'intent',
            'Wake response',
            'normal',
            get_db_fn=self._get_db,
            cache_info=self._cache_info(),
            wake_run_id='wake-native-test',
            window_identity=window_identity or self._window_identity(),
            settlement_required=False,
            native_transcript_binding=(
                self._binding() if binding is None else binding
            ),
        )

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _counts(self):
        conn = self._get_db()
        try:
            return {
                'messages': int(conn.execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE source_kind='wake'",
                ).fetchone()[0]),
                'mappings': int(conn.execute(
                    'SELECT COUNT(*) FROM chat_message_claude_events',
                ).fetchone()[0]),
                'cursor': int(conn.execute(
                    'SELECT scan_offset FROM context_claude_sessions '
                    'WHERE context_id=? AND resident_generation=?',
                    (self.context_id, self.resident_generation),
                ).fetchone()[0]),
                'wake_log': int(conn.execute(
                    'SELECT COUNT(*) FROM wake_log',
                ).fetchone()[0]),
            }
        finally:
            conn.close()

    def test_prepare_proves_range_and_returns_native_events_without_advancing_cursor(self):
        resident = types.SimpleNamespace(
            session_id=self.session_id,
            generation=self.process_generation,
        )
        watermark = uh.SharedTranscriptWatermark(
            context_id=self.context_id,
            context_epoch=self.context_epoch,
            resident_generation=self.resident_generation,
            resident_key='chat:12:3:2',
            claude_session_id=self.session_id,
            transcript_path=self.transcript_path,
            expected_offset=self.start_offset,
            process_generation=self.process_generation,
        )
        binding = types.SimpleNamespace(
            context_id=self.context_id,
            context_epoch=self.context_epoch,
            resident_generation=self.resident_generation,
            resident_key='chat:12:3:2',
        )
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=binding), \
             mock.patch.object(uh, 'cas_advance_scan_offset') as cas:
            prepared = uh.prepare_shared_transcript_native_binding(
                watermark,
                resident,
                jsonl_finality={'stream_totals_match': True},
            )
        self.assertEqual(prepared['start_offset'], self.start_offset)
        self.assertEqual(prepared['end_offset'], self.end_offset)
        self.assertEqual(prepared['claude_session_id'], self.session_id)
        self.assertEqual(
            [event['role'] for event in prepared['events']],
            [ROLE_PROVIDER_INPUT, ROLE_ASSISTANT],
        )
        self.assertTrue(all(
            self.start_offset <= event['jsonl_byte_offset'] < self.end_offset
            for event in prepared['events']
        ))
        cas.assert_not_called()
        self.assertEqual(self._counts()['cursor'], self.start_offset)

    def test_provider_only_skip_still_advances_without_creating_mapping(self):
        resident = types.SimpleNamespace(
            session_id=self.session_id,
            generation=self.process_generation,
        )
        watermark = uh.SharedTranscriptWatermark(
            context_id=self.context_id,
            context_epoch=self.context_epoch,
            resident_generation=self.resident_generation,
            resident_key='chat:12:3:2',
            claude_session_id=self.session_id,
            transcript_path=self.transcript_path,
            expected_offset=self.start_offset,
            process_generation=self.process_generation,
        )
        binding = types.SimpleNamespace(
            context_id=self.context_id,
            context_epoch=self.context_epoch,
            resident_generation=self.resident_generation,
            resident_key='chat:12:3:2',
        )
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=binding):
            result = uh.commit_shared_transcript_watermark(
                watermark,
                resident,
                db_path=self.db_path,
                jsonl_finality={'stream_totals_match': True},
            )
        self.assertTrue(result['skipped_provider_round'])
        self.assertEqual(result['start_offset'], self.start_offset)
        self.assertEqual(result['end_offset'], self.end_offset)
        counts = self._counts()
        self.assertEqual(counts['cursor'], self.end_offset)
        self.assertEqual(counts['mappings'], 0)
        self.assertEqual(counts['messages'], 0)

    def test_success_commits_message_native_events_and_cursor_without_formal_pair(self):
        conn = self._get_db()
        user_id = int(conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana', 'hello')",
        ).lastrowid)
        assistant_id = int(conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('fyodor', 'reply')",
        ).lastrowid)
        conn.executemany(
            """INSERT INTO daily_message_contexts (
                message_id, context_id, context_epoch, resident_generation, role
            ) VALUES (?, ?, ?, ?, ?)""",
            [
                (
                    user_id, self.context_id, self.context_epoch,
                    self.resident_generation, 'user',
                ),
                (
                    assistant_id, self.context_id, self.context_epoch,
                    self.resident_generation, 'assistant',
                ),
            ],
        )
        conn.execute(
            'UPDATE context_claude_sessions SET last_mapped_message_id=? '
            'WHERE context_id=? AND resident_generation=?',
            (assistant_id, self.context_id, self.resident_generation),
        )
        conn.commit()
        conn.close()

        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery'):
            result = self._execute()

        self.assertTrue(result['delivered'])
        conn = self._get_db()
        try:
            wake = conn.execute(
                "SELECT id FROM chat_messages WHERE source_kind='wake'",
            ).fetchone()
            self.assertIsNotNone(wake)
            wake_id = int(wake['id'])
            rows = conn.execute(
                'SELECT event_uuid, message_id, role, claude_session_id, '
                'context_id, context_epoch, resident_generation, jsonl_byte_offset '
                'FROM chat_message_claude_events ORDER BY jsonl_byte_offset',
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual({int(row['message_id']) for row in rows}, {wake_id})
            self.assertEqual(
                [str(row['role']) for row in rows],
                [ROLE_PROVIDER_INPUT, ROLE_ASSISTANT],
            )
            self.assertEqual(
                {str(row['claude_session_id']) for row in rows},
                {self.session_id},
            )
            self.assertEqual(
                {
                    (
                        int(row['context_id']),
                        int(row['context_epoch']),
                        int(row['resident_generation']),
                    )
                    for row in rows
                },
                {(self.context_id, self.context_epoch, self.resident_generation)},
            )
            self.assertTrue(all(
                self.start_offset <= int(row['jsonl_byte_offset']) < self.end_offset
                for row in rows
            ))
            self.assertEqual(
                conn.execute(
                    'SELECT COUNT(*) FROM daily_message_contexts WHERE message_id=?',
                    (wake_id,),
                ).fetchone()[0],
                0,
            )
            registry = conn.execute(
                'SELECT scan_offset, last_mapped_message_id '
                'FROM context_claude_sessions WHERE context_id=? '
                'AND resident_generation=?',
                (self.context_id, self.resident_generation),
            ).fetchone()
            self.assertEqual(int(registry['scan_offset']), self.end_offset)
            self.assertEqual(int(registry['last_mapped_message_id']), assistant_id)

            formal = _load_auth_rounds_after(
                conn,
                context_id=self.context_id,
                context_epoch=self.context_epoch,
                resident_generation=self.resident_generation,
                after_message_id=0,
                through_assistant_id=wake_id,
            )
            self.assertEqual(len(formal), 1)
            self.assertEqual(formal[0].user_message_id, user_id)
            self.assertEqual(formal[0].assistant_message_id, assistant_id)
        finally:
            conn.close()

    def test_current_main_wake_tool_roles_can_share_the_wake_message_id(self):
        self._write_transcript(with_tools=True)
        self.assertEqual(
            [event['role'] for event in self.events],
            [
                ROLE_PROVIDER_INPUT,
                ROLE_TOOL_USE,
                ROLE_TOOL_RESULT_USER,
                ROLE_ASSISTANT,
            ],
        )
        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery'):
            result = self._execute()
        self.assertTrue(result['delivered'])
        conn = self._get_db()
        try:
            rows = conn.execute(
                'SELECT message_id, role FROM chat_message_claude_events '
                'ORDER BY jsonl_byte_offset',
            ).fetchall()
            self.assertEqual(
                [str(row['role']) for row in rows],
                [
                    ROLE_PROVIDER_INPUT,
                    ROLE_TOOL_USE,
                    ROLE_TOOL_RESULT_USER,
                    ROLE_ASSISTANT,
                ],
            )
            self.assertEqual(len({int(row['message_id']) for row in rows}), 1)
            self.assertEqual(
                int(conn.execute(
                    'SELECT scan_offset FROM context_claude_sessions '
                    'WHERE context_id=? AND resident_generation=?',
                    (self.context_id, self.resident_generation),
                ).fetchone()[0]),
                self.end_offset,
            )
        finally:
            conn.close()

    def test_commit_rechecks_exact_transcript_range_before_mapping(self):
        binding = self._binding()
        raw = Path(self.transcript_path).read_bytes()
        changed = raw.replace(
            b'wake-assistant-output',
            b'wake-assistant-change',
        )
        self.assertEqual(len(changed), len(raw))
        Path(self.transcript_path).write_bytes(changed)

        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery') as retire:
            with self.assertRaisesRegex(MappingRejected, 'transcript range changed'):
                self._execute(binding=binding)
        counts = self._counts()
        self.assertEqual(counts['messages'], 0)
        self.assertEqual(counts['mappings'], 0)
        self.assertEqual(counts['cursor'], self.start_offset)
        self.assertEqual(counts['wake_log'], 0)
        retire.assert_called_once()

    def test_mapping_failure_rolls_back_message_events_and_cursor_and_retires(self):
        from chat import claude_event_mapping as mapping

        insert = mapping._insert_mapping_row
        calls = 0

        def fail_second(conn, planned):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('mapping rejected')
            return insert(conn, planned)

        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
             mock.patch.object(mapping, '_insert_mapping_row', side_effect=fail_second), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery') as retire:
            with self.assertRaisesRegex(RuntimeError, 'mapping rejected'):
                self._execute()
        counts = self._counts()
        self.assertEqual(counts['messages'], 0)
        self.assertEqual(counts['mappings'], 0)
        self.assertEqual(counts['cursor'], self.start_offset)
        self.assertEqual(counts['wake_log'], 0)
        retire.assert_called_once()

    def test_cursor_cas_failure_rolls_back_message_and_inserted_mappings(self):
        from chat import claude_event_mapping as mapping

        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
             mock.patch.object(
                 mapping,
                 'cas_advance_scan_offset',
                 side_effect=RuntimeError('cursor CAS failed'),
             ), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery'):
            with self.assertRaisesRegex(RuntimeError, 'cursor CAS failed'):
                self._execute()
        counts = self._counts()
        self.assertEqual(counts['messages'], 0)
        self.assertEqual(counts['mappings'], 0)
        self.assertEqual(counts['cursor'], self.start_offset)
        self.assertEqual(counts['wake_log'], 0)

    def test_stale_window_writes_no_message_mapping_or_cursor_advance(self):
        import chat.window_identity as window

        identity = self._window_identity()
        changed = dict(identity, context_epoch=identity['context_epoch'] + 1)
        with mock.patch.object(window, 'soft_window_enabled', return_value=True), \
             mock.patch.object(
                 window,
                 'gate_captured_against_conn',
                 return_value=(window.REASON_STALE, identity, changed),
             ), \
             mock.patch.object(window, 'log_stale_async_result'), \
             mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery'):
            result = self._execute(window_identity=identity)
        self.assertFalse(result['delivered'])
        counts = self._counts()
        self.assertEqual(counts['messages'], 0)
        self.assertEqual(counts['mappings'], 0)
        self.assertEqual(counts['cursor'], self.start_offset)
        self.assertEqual(counts['wake_log'], 1)


if __name__ == '__main__':
    unittest.main()
