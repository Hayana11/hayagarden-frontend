"""Mode-keyed rolling summary isolation tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.history_boundary import boundary_rows_for_summary
from chat.rolling_summary_store import get_summary, save_summary


def _row(mid, content='msg', *, file_url='', author='hayana'):
    return SimpleNamespace(
        id=mid, author=author, content=content, image_url='',
        created_at='2026-07-26 12:00:00', tool_calls='', file_url=file_url, file_name='f.txt',
    )


class RollingSummaryModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')

    def tearDown(self):
        self.tmp.cleanup()

    def _make_db(self, rows):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            '''CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, image_url TEXT,
                created_at TEXT, tool_calls TEXT, file_url TEXT, file_name TEXT
            )'''
        )
        for r in rows:
            conn.execute(
                'INSERT INTO chat_messages (id, author, content, created_at, file_url, file_name) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (r.id, r.author, r.content, r.created_at, r.file_url, r.file_name),
            )
        conn.commit()
        conn.close()

    def test_relay_and_cc_read_different_summaries(self):
        with mock.patch('chat.rolling_summary_store._db_path', return_value=self.db_path):
            save_summary('relay_hysteresis', summary='relay 摘要', up_to_id=10)
            save_summary('cc_token_budget', summary='cc 摘要', up_to_id=20)
            relay = get_summary('relay_hysteresis')
            cc = get_summary('cc_token_budget')
            self.assertEqual(relay['summary'], 'relay 摘要')
            self.assertEqual(cc['summary'], 'cc 摘要')
            self.assertNotEqual(relay['up_to_id'], cc['up_to_id'])

    def test_cc_file_body_affects_boundary(self):
        rows = [_row(i, 'x' * 400) for i in range(1, 21)]
        rows.append(_row(21, 'file', file_url='/static/big.txt'))
        self._make_db(rows)
        files = {'/static/big.txt': 'y' * 8000}

        def get_db():
            import sqlite3
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch('chat.context_lean.lean_history_enabled', return_value=True), \
             mock.patch('chat.context_lean.cc_history_token_budget', return_value=3000), \
             mock.patch('chat.context_lean.lean_tool_budget_enabled', return_value=False), \
             mock.patch('chat.context_lean.lean_file_dedup_enabled', return_value=False):
            trimmed_with_file, _, _ = boundary_rows_for_summary(
                get_db, for_cc=True, static_dir='/tmp',
                read_file_fn=lambda _sd, url: files.get(url),
            )
            trimmed_no_file, _, _ = boundary_rows_for_summary(
                get_db, for_cc=True, static_dir='/tmp',
                read_file_fn=lambda *_a, **_k: None,
            )
        self.assertGreaterEqual(trimmed_with_file, trimmed_no_file)

    def test_provider_mode_does_not_cross_read(self):
        with mock.patch('chat.rolling_summary_store._db_path', return_value=self.db_path):
            save_summary('legacy_block', summary='legacy only', up_to_id=5)
            cc = get_summary('cc_token_budget')
            self.assertEqual(cc['summary'], '')

    def test_legacy_mode_unchanged_when_lean_off(self):
        rows = [_row(i) for i in range(1, 81)]
        self._make_db(rows)

        def get_db():
            import sqlite3
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch('chat.context_lean.lean_history_enabled', return_value=False):
            trimmed, oldest, summarize = boundary_rows_for_summary(get_db)
        self.assertGreater(trimmed, 0)
        self.assertGreater(len(summarize), 0)
        self.assertGreater(oldest, trimmed)


if __name__ == '__main__':
    unittest.main()
