"""Rolling summary boundary alignment with production history trimming."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.history_boundary import (
    boundary_rows_for_summary,
    compute_boundary_ids,
    legacy_block_limit,
    rolling_summary_covers_boundary,
)


def _row(mid, content='msg', author='hayana'):
    return SimpleNamespace(
        id=mid, author=author, content=content, image_url='',
        created_at='2026-07-26 12:00:00', tool_calls='', file_url='', file_name='',
    )


class HistoryBoundaryTests(unittest.TestCase):
    def test_compute_boundary_ids_from_row_sets(self):
        trimmed, oldest = compute_boundary_ids([1, 2, 3, 4, 5], [4, 5])
        self.assertEqual(trimmed, 3)
        self.assertEqual(oldest, 4)

    def test_legacy_block_limit_matches_60_plus_20(self):
        self.assertEqual(legacy_block_limit(79), 79)
        self.assertEqual(legacy_block_limit(80), 60)
        self.assertEqual(legacy_block_limit(100), 60)

    def test_rolling_summary_coverage_gap_when_stale(self):
        self.assertFalse(rolling_summary_covers_boundary(10, 20))
        self.assertTrue(rolling_summary_covers_boundary(20, 20))
        self.assertTrue(rolling_summary_covers_boundary(0, 0))

    def test_token_trim_boundary_sets_trimmed_up_to_id(self):
        rows = [_row(i, 'x' * 600) for i in range(1, 41)]
        from chat.history_assembly import assemble_history_from_rows
        _, stats = assemble_history_from_rows(
            rows,
            available_count=40,
            history_token_budget=5000,
            history_mode='cc_token_budget',
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertTrue(stats.conversation_content_trimmed)
        self.assertGreater(stats.trimmed_up_to_id, 0)
        self.assertGreater(stats.oldest_retained_message_id, stats.trimmed_up_to_id)

    def test_no_trim_means_no_summary_rows(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(tmp.name) / 'memories.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            '''CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, image_url TEXT,
                created_at TEXT, tool_calls TEXT, file_url TEXT, file_name TEXT,
                attachments TEXT DEFAULT '[]'
            )'''
        )
        for i in range(1, 11):
            conn.execute(
                'INSERT INTO chat_messages (id, author, content, created_at) VALUES (?, ?, ?, ?)',
                (i, 'hayana', 'short', '2026-07-26 12:00:00'),
            )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch('chat.context_lean.lean_history_enabled', return_value=False):
            trimmed, oldest, summarize = boundary_rows_for_summary(get_db)
        self.assertEqual(trimmed, 0)
        self.assertEqual(len(summarize), 0)
        tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
