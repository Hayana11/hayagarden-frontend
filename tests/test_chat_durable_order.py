import datetime
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = __import__('importlib.util').util.spec_from_file_location(
    'chat_display_segments_under_test', ROOT / 'chat' / 'display_segments.py',
)
_module = __import__('importlib.util').util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
DisplaySegmentAccumulator = _module.DisplaySegmentAccumulator
finalize_display_segments = _module.finalize_display_segments
finalize_display_segments_json = _module.finalize_display_segments_json

from chat import daily_context as dc


class ChatDurableOrderTests(unittest.TestCase):
    def test_t1_exact_interleaving_order(self):
        acc = DisplaySegmentAccumulator()
        acc.append_thinking('thinking A')
        acc.append_text('text B')
        acc.append_tool(0)
        acc.append_text('text C')
        acc.append_tool(1)
        acc.append_text('text D')
        self.assertEqual(acc.as_list(), [
            {'type': 'thinking', 'text': 'thinking A'},
            {'type': 'text', 'text': 'text B'},
            {'type': 'tool', 'tool_index': 0},
            {'type': 'text', 'text': 'text C'},
            {'type': 'tool', 'tool_index': 1},
            {'type': 'text', 'text': 'text D'},
        ])

    def test_t2_adjacent_deltas_merge_non_adjacent_split(self):
        acc = DisplaySegmentAccumulator()
        acc.append_text('a')
        acc.append_text('b')
        acc.append_thinking('x')
        acc.append_text('c')
        acc.append_text('d')
        self.assertEqual(acc.as_list(), [
            {'type': 'text', 'text': 'ab'},
            {'type': 'thinking', 'text': 'x'},
            {'type': 'text', 'text': 'cd'},
        ])

    def test_t3_tool_result_does_not_change_order(self):
        acc = DisplaySegmentAccumulator()
        acc.append_text('before')
        acc.append_tool(0)
        before = acc.as_list()
        acc.apply_tool_result(0)
        self.assertEqual(acc.as_list(), before)

    def test_t7_high_frequency_delta_accumulation(self):
        acc = DisplaySegmentAccumulator()
        for _ in range(5000):
            acc.append_text('x')
        self.assertEqual(acc.as_list(), [{'type': 'text', 'text': 'x' * 5000}])
        self.assertTrue(acc.to_json())

    def test_t8_staging_candidate_contract(self):
        staging = (ROOT / 'chat' / 'rewrite_staging.py').read_text(encoding='utf-8')
        self.assertIn('candidate_display_segments TEXT', staging)
        self.assertIn('candidate_display_segments=display_segments or', staging)
        self.assertIn("'display_segments': new_branch['display_segments']", staging)
        self.assertIn("updates['display_segments']", staging)
        self.assertIn("('display_segments', row.get('candidate_display_segments') or '')", staging)

    def test_t9_additive_migration(self):
        app = (ROOT / 'app.py').read_text(encoding='utf-8')
        schema = (ROOT / 'chat' / 'daily_schema.py').read_text(encoding='utf-8')
        self.assertIn('def ensure_chat_messages_display_segments', schema)
        self.assertIn('ensure_chat_messages_display_segments(conn)', app)
        migration = re.search(r"def _migrate_chat_columns\(\):([\s\S]*?)\n\n_migrate_chat_columns", app)
        self.assertIsNotNone(migration)
        body = migration.group(1)
        self.assertNotIn('DROP TABLE', body)
        self.assertNotRegex(body, r'UPDATE\\s+chat_messages', re.I)

    def test_t10_all_persistence_paths_carry_display_segments(self):
        gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('from chat.display_segments import (', gateway)
        self.assertGreaterEqual(gateway.count('display_segments.to_json()'), 5)
        self.assertIn('display_segments_json=display_segments.to_json()', gateway)
        self.assertIn('display_segments=display_segments_json', gateway)
        for marker in ('_stream_cc_deferred_confirmation', '_stream_cc_first_turn',
                       '_stream_cc_daily_soft_window', "phase = 'resident_stream'",
                       'think_acc, text_acc, tool_calls_acc'):
            self.assertIn(marker, gateway)

    def test_t11_live_handoff_contract(self):
        screen = (ROOT / 'app' / 'src' / 'screens' / 'ChatScreen.tsx').read_text(encoding='utf-8')
        self.assertIn('const clearLivePresentation = useCallback', screen)
        self.assertIn('if (!res.ok || res.deferredTool) clearLivePresentation()', screen)
        self.assertGreaterEqual(screen.count('await refetchLatest();\n    clearLivePresentation();'), 2)

    def test_t4_json_is_compact_and_parseable(self):
        acc = DisplaySegmentAccumulator()
        acc.append_thinking('t')
        acc.append_tool(3)
        payload = acc.to_json()
        self.assertEqual(json.loads(payload), acc.as_list())
        self.assertNotIn(' ', payload)


    def test_t5_canonical_cleanup_and_choices_are_pure(self):
        raw = [
            {'type': 'text', 'text': '前文 [[SAVE:秘密]] 中段 [choices]A|B[/choices] 后文'},
        ]
        visible = '前文  中段  后文'
        final = finalize_display_segments(raw, visible_text=visible)
        text = ''.join(item.get('text', '') for item in final if item['type'] == 'text')
        self.assertEqual(text, visible)
        self.assertNotIn('[[SAVE:', text)
        self.assertNotIn('[choices]', text)
        self.assertNotIn('[/choices]', text)
        self.assertNotIn('memory_tool', (_module.__file__ or ''))
        self.assertEqual(
            json.loads(finalize_display_segments_json(json.dumps(raw), visible)),
            final,
        )

    def test_t6_cleanup_preserves_tool_interleaving(self):
        raw = [
            {'type': 'text', 'text': 'text A'},
            {'type': 'tool', 'tool_index': 0},
            {'type': 'text', 'text': 'text-with-marker [[SAVE:secret]]'},
            {'type': 'tool', 'tool_index': 1},
            {'type': 'text', 'text': 'text B [choices]A|B[/choices]'},
        ]
        final = finalize_display_segments(raw, visible_text='text Atext-with-marker text B')
        self.assertEqual(
            [item['type'] for item in final],
            ['text', 'tool', 'text', 'tool', 'text'],
        )
        self.assertEqual(
            [item['tool_index'] for item in final if item['type'] == 'tool'],
            [0, 1],
        )
        joined = ''.join(item.get('text', '') for item in final if item['type'] == 'text')
        self.assertEqual(joined, 'text Atext-with-marker text B')

    def test_t12_old_schema_migration_and_daily_insert(self):
        fd, path = tempfile.mkstemp(prefix='durable-order-', suffix='.db')
        os.close(fd)
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                'CREATE TABLE chat_messages ('
                'id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'author TEXT NOT NULL, content TEXT NOT NULL DEFAULT "", '
                'thinking TEXT DEFAULT "", tool_calls TEXT DEFAULT "", '
                'cache_info TEXT DEFAULT "", choices TEXT DEFAULT "", '
                'source_kind TEXT NOT NULL DEFAULT "chat", '
                'created_at TEXT NOT NULL DEFAULT "2026-01-01 12:00:00")'
            )
            conn.commit()
            conn.close()
            dc.ensure_schema(path)
            conn = sqlite3.connect(path)
            cols = {row[1] for row in conn.execute('PRAGMA table_info(chat_messages)')}
            self.assertIn('display_segments', cols)
            conn.close()

            now = datetime.datetime(2026, 1, 1, 12, 0, 0)
            conn = sqlite3.connect(path)
            conn.execute(
                'INSERT INTO daily_contexts '
                '(chat_id, local_day, context_epoch, status, resident_generation) '
                'VALUES (?, ?, ?, ?, ?)',
                ('default', '2026-01-01', 1, dc.STATUS_FINALIZED, 1),
            )
            context_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            conn.execute(
                'INSERT INTO daily_resident_turn_leases '
                '(context_id, resident_generation, lease_owner, request_message_id, acquired_at, expires_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (context_id, 1, 'test-owner', 1,
                 '2026-01-01 11:59:00', '2026-01-01 13:00:00'),
            )
            conn.commit()
            conn.close()

            raw = json.dumps([
                {'type': 'text', 'text': '前文 [[SAVE:秘密]] 中段 [choices]A|B[/choices] 后文'},
            ], ensure_ascii=False)
            aid = dc.persist_daily_assistant_if_current(
                chat_id='default',
                context_id=context_id,
                context_epoch=1,
                resident_generation=1,
                lease_owner='test-owner',
                content='前文  中段  后文',
                choices='["A","B"]',
                display_segments=raw,
                db_path=path,
                now=now,
            )
            conn = sqlite3.connect(path)
            row = conn.execute(
                'SELECT content, choices, display_segments FROM chat_messages WHERE id=?',
                (aid,),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], '前文  中段  后文')
            self.assertEqual(json.loads(row[1]), ['A', 'B'])
            self.assertEqual(json.loads(row[2]), [
                {'type': 'text', 'text': '前文  中段  后文'},
            ])
        finally:
            dc._SCHEMA_READY.discard(path)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_t13_rewrite_schema_and_branch_payload_keep_display_segments(self):
        staging = (ROOT / 'chat' / 'rewrite_staging.py').read_text(encoding='utf-8')
        self.assertIn('display_segments', staging)
        self.assertIn('candidate_display_segments', staging)


if __name__ == '__main__':
    unittest.main()
