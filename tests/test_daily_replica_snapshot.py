"""9A-R read-only production material snapshot tests."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from chat.daily_replica_ab import ReplicaContractError
from chat.daily_replica_snapshot import create_daily_replica_snapshot


def _formatter(*, assembly, user_content, is_cold, is_respawn):
    history = assembly.get('current_day_history') or []
    rendered = '|'.join('%s:%s' % (m['role'], m['content']) for m in history)
    return str(assembly.get('state') or '') + '\n' + rendered + '\n' + user_content


class DailyReplicaSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'formal.sqlite3'
        conn = sqlite3.connect(str(self.source))
        conn.execute('CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)')
        conn.execute(
            'CREATE TABLE daily_message_contexts ('
            'message_id INTEGER PRIMARY KEY, context_id INTEGER, context_epoch INTEGER, '
            'resident_generation INTEGER, role TEXT)'
        )
        conn.execute("INSERT INTO chat_messages VALUES (10, 'user', '咬你！')")
        conn.execute("INSERT INTO chat_messages VALUES (11, 'assistant', '未来回复')")
        conn.execute("INSERT INTO daily_message_contexts VALUES (10, 7, 3, 2, 'user')")
        conn.execute("INSERT INTO daily_message_contexts VALUES (11, 7, 3, 2, 'assistant')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _sha(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def _mapping(self, message_id, *, db_path):
        return {
            'message_id': message_id, 'context_id': 7, 'context_epoch': 3,
            'resident_generation': 2, 'role': 'user',
        }

    def _context(self, context_id, *, db_path):
        return {
            'id': context_id, 'chat_id': 'default', 'local_day': '2026-08-02',
            'context_epoch': 3, 'resident_generation': 2, 'boundary_message_id': 0,
        }

    def _current(self, *, db_path):
        return self._context(7, db_path=db_path)

    def _assembly(self, **kwargs):
        self.assertNotEqual(Path(kwargs['db_path']), self.source)
        self.assertTrue(kwargs['is_cold'])
        self.assertTrue(kwargs['inject_handoff'])
        self.assertTrue(kwargs['inject_carryover'])
        conn = sqlite3.connect(kwargs['db_path'])
        try:
            ids = [int(r[0]) for r in conn.execute('SELECT id FROM chat_messages ORDER BY id')]
            mapped = [
                int(r[0]) for r in conn.execute(
                    'SELECT message_id FROM daily_message_contexts ORDER BY message_id'
                )
            ]
        finally:
            conn.close()
        self.assertEqual(ids, [10])
        self.assertEqual(mapped, [10])
        return {
            'state': '【当前状态】灯关着',
            'current_day_history': [
                {'role': 'user', 'content': '拒不赔偿', 'message_id': 1},
                {'role': 'assistant', 'content': '判决内容', 'message_id': 2},
            ],
            'manifest': {
                'state_injected': True,
                'legacy_cold_once_injected': False,
            },
        }

    def _create(self, **overrides):
        kwargs = dict(
            source_db_path=str(self.source), user_message_id=10,
            static_system='same-system', static_system_sha256='system-sha',
            persona_sha256='persona-sha', provider='claude_code', model='opus',
            tool_profile='text_only', allowed_tools_sha256='empty-tools-sha',
            mcp_config_sha256='unused-mcp-sha', snapshot_parent=str(self.root),
            message_context_loader=self._mapping, context_loader=self._context,
            current_context_loader=self._current, assembly_builder=self._assembly,
            formal_formatter=_formatter,
        )
        kwargs.update(overrides)
        return create_daily_replica_snapshot(**kwargs)

    def test_source_is_unchanged_and_plan_uses_snapshot(self):
        before = self._sha()
        snapshot = self._create()
        try:
            self.assertEqual(self._sha(), before)
            self.assertNotEqual(snapshot.db_path, self.source)
            self.assertTrue(snapshot.db_path.exists())
            self.assertTrue(snapshot.plan.contract_ok)
            self.assertEqual(snapshot.plan.manifest['tool_profile'], 'text_only')
            self.assertEqual(snapshot.manifest['source_open_mode'], 'ro')
            self.assertFalse(snapshot.manifest['source_db_written'])
            self.assertTrue(snapshot.manifest['snapshot_truncated_after_user'])
            root = snapshot.temp_root
        finally:
            snapshot.close()
        self.assertFalse(root.exists())

    def test_non_current_message_identity_fails_and_cleans_snapshot(self):
        def current(*, db_path):
            row = self._current(db_path=db_path)
            row['context_epoch'] = 4
            return row

        before = {p.name for p in self.root.iterdir()}
        with self.assertRaises(ReplicaContractError) as ctx:
            self._create(current_context_loader=current)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_MESSAGE_NOT_CURRENT_CONTEXT')
        self.assertEqual({p.name for p in self.root.iterdir()}, before)

    def test_non_user_message_fails_before_assembly(self):
        conn = sqlite3.connect(str(self.source))
        conn.execute("INSERT INTO chat_messages VALUES (12, 'assistant', '回应')")
        conn.commit()
        conn.close()
        with self.assertRaises(ReplicaContractError) as ctx:
            self._create(user_message_id=12)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_USER_MESSAGE_INVALID')


if __name__ == '__main__':
    unittest.main()
