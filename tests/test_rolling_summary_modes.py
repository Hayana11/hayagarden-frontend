"""Mode-keyed rolling summary isolation tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.history_boundary import boundary_rows_for_summary, legacy_block_limit, resolve_fetch_plan
from chat.rolling_summary_store import get_summary, save_summary


_FIXED_HISTORY_WHERE = "created_at >= '2026-07-25 00:00:00'"


def _row(mid, content='msg', *, file_url='', author=None):
    if author is None:
        author = 'hayana' if mid % 2 else 'assistant'
    return SimpleNamespace(
        id=mid, author=author, content=content, image_url='',
        created_at='2026-07-26 12:00:00', tool_calls='', file_url=file_url, file_name='f.txt',
    )


class RollingSummaryModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self._history_where = mock.patch(
            'chat.history_boundary._HISTORY_WHERE',
            _FIXED_HISTORY_WHERE,
        )
        self._history_where.start()

    def tearDown(self):
        self._history_where.stop()
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

    def _get_db(self):
        import sqlite3
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _lean_on(self):
        return mock.patch('chat.history_boundary.lean_history_enabled', return_value=True)

    def test_explicit_legacy_block_uses_60_plus_20_with_lean_on(self):
        rows = [_row(i) for i in range(1, 101)]
        self._make_db(rows)
        with self._lean_on():
            trimmed, oldest, summarize = boundary_rows_for_summary(
                self._get_db, history_mode='legacy_block',
            )
            with mock.patch('chat.history_boundary.lean_history_enabled', return_value=False):
                off_trimmed, off_oldest, _ = boundary_rows_for_summary(self._get_db)
        self.assertEqual(legacy_block_limit(100), 60)
        self.assertEqual(trimmed, 40)
        self.assertEqual(oldest, 41)
        self.assertGreater(len(summarize), 0)
        self.assertEqual((trimmed, oldest), (off_trimmed, off_oldest))

    def test_explicit_relay_hysteresis_uses_relay_head_and_watermarks(self):
        rows = [_row(i, 'x' * 600) for i in range(1, 101)]
        self._make_db(rows)
        with self._lean_on(), \
             mock.patch('chat.history_boundary.relay_history_high_water', return_value=8000), \
             mock.patch('chat.history_boundary.relay_history_low_water', return_value=5000), \
             mock.patch('chat.history_boundary.relay_history_head_id', return_value=1):
            trimmed, oldest, summarize = boundary_rows_for_summary(
                self._get_db, history_mode='relay_hysteresis',
            )
            legacy_trimmed, legacy_oldest, _ = boundary_rows_for_summary(
                self._get_db, history_mode='legacy_block',
            )
        self.assertGreater(trimmed, 0)
        self.assertGreater(oldest, trimmed)
        self.assertGreater(len(summarize), 0)
        self.assertNotEqual((trimmed, oldest), (legacy_trimmed, legacy_oldest))

    def test_explicit_cc_token_budget_uses_token_budget(self):
        rows = [_row(i, 'x' * 400) for i in range(1, 101)]
        self._make_db(rows)
        with self._lean_on(), \
             mock.patch('chat.history_boundary.cc_history_token_budget', return_value=3000), \
             mock.patch('chat.context_lean.lean_tool_budget_enabled', return_value=False), \
             mock.patch('chat.context_lean.lean_file_dedup_enabled', return_value=False):
            trimmed, oldest, summarize = boundary_rows_for_summary(
                self._get_db, history_mode='cc_token_budget',
            )
            legacy_trimmed, legacy_oldest, _ = boundary_rows_for_summary(
                self._get_db, history_mode='legacy_block',
            )
        self.assertGreater(trimmed, 0)
        self.assertGreater(oldest, trimmed)
        self.assertGreater(len(summarize), 0)
        self.assertNotEqual((trimmed, oldest), (legacy_trimmed, legacy_oldest))

    def test_modes_save_distinct_up_to_ids(self):
        rows = [_row(i, 'x' * 600) for i in range(1, 101)]
        self._make_db(rows)
        boundaries = {}
        with self._lean_on(), \
             mock.patch('chat.history_boundary.relay_history_high_water', return_value=8000), \
             mock.patch('chat.history_boundary.relay_history_low_water', return_value=5000), \
             mock.patch('chat.history_boundary.cc_history_token_budget', return_value=3000), \
             mock.patch('chat.context_lean.lean_tool_budget_enabled', return_value=False), \
             mock.patch('chat.context_lean.lean_file_dedup_enabled', return_value=False), \
             mock.patch('chat.history_boundary.relay_history_head_id', return_value=1), \
             mock.patch('chat.rolling_summary_store._db_path', return_value=self.db_path):
            for mode in ('legacy_block', 'relay_hysteresis', 'cc_token_budget'):
                trimmed, oldest, summarize = boundary_rows_for_summary(
                    self._get_db, history_mode=mode,
                )
                up_to = summarize[-1]['id'] if summarize else 0
                boundaries[mode] = (trimmed, oldest, up_to)
                save_summary(mode, summary='s-%s' % mode, up_to_id=up_to, oldest_retained_id=oldest)
                stored = get_summary(mode)
                self.assertEqual(stored['up_to_id'], up_to)
                self.assertEqual(stored['oldest_retained_id'], oldest)
        self.assertEqual(len({b[:2] for b in boundaries.values()}), 3)

    def test_lean_off_reads_legacy_boundary_not_relay(self):
        rows = [_row(i, 'x' * 600) for i in range(1, 101)]
        self._make_db(rows)
        with self._lean_on(), \
             mock.patch('chat.history_boundary.relay_history_high_water', return_value=8000), \
             mock.patch('chat.history_boundary.relay_history_low_water', return_value=5000), \
             mock.patch('chat.history_boundary.relay_history_head_id', return_value=1), \
             mock.patch('chat.rolling_summary_store._db_path', return_value=self.db_path):
            legacy_trimmed, legacy_oldest, legacy_rows = boundary_rows_for_summary(
                self._get_db, history_mode='legacy_block',
            )
            relay_trimmed, relay_oldest, relay_rows = boundary_rows_for_summary(
                self._get_db, history_mode='relay_hysteresis',
            )
            self.assertNotEqual((legacy_trimmed, legacy_oldest), (relay_trimmed, relay_oldest))
            save_summary(
                'legacy_block',
                summary='legacy boundary',
                up_to_id=legacy_rows[-1]['id'] if legacy_rows else 0,
                oldest_retained_id=legacy_oldest,
            )
            save_summary(
                'relay_hysteresis',
                summary='relay boundary',
                up_to_id=relay_rows[-1]['id'] if relay_rows else 0,
                oldest_retained_id=relay_oldest,
            )
            legacy_up_to = legacy_rows[-1]['id'] if legacy_rows else 0
            relay_up_to = relay_rows[-1]['id'] if relay_rows else 0
        with mock.patch('chat.history_boundary.lean_history_enabled', return_value=False), \
             mock.patch('chat.rolling_summary_store._db_path', return_value=self.db_path):
            off_trimmed, off_oldest, _ = boundary_rows_for_summary(self._get_db)
            legacy = get_summary('legacy_block')
            relay = get_summary('relay_hysteresis')
        self.assertEqual((off_trimmed, off_oldest), (legacy_trimmed, legacy_oldest))
        self.assertNotEqual(legacy_up_to, relay_up_to)
        self.assertEqual(legacy['summary'], 'legacy boundary')

    def test_worker_passes_explicit_history_mode(self):
        rows = [_row(i) for i in range(1, 81)]
        self._make_db(rows)
        calls = []

        def _capture(get_db, **kwargs):
            calls.append(kwargs.get('history_mode'))
            return 0, 0, []

        env_text = 'DEEPSEEK_API_KEY=test\n'

        def _open(path, *args, **kwargs):
            if str(path) == '/opt/frontend/.env':
                return mock.mock_open(read_data=env_text).return_value
            return open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=_open), \
             mock.patch('chat.context_lean.lean_history_enabled', return_value=True), \
             mock.patch('tools.rolling_summary._db', return_value=self._get_db()), \
             mock.patch('tools.rolling_summary._cfg.get_int', return_value=3), \
             mock.patch('chat.history_boundary.boundary_rows_for_summary', side_effect=_capture), \
             mock.patch('tools.rolling_summary._ask', return_value='summary'):
            import importlib
            import tools.rolling_summary as rolling_summary
            importlib.reload(rolling_summary)
            rolling_summary.run()
        self.assertEqual(calls, ['legacy_block', 'relay_hysteresis', 'cc_token_budget'])

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

        with self._lean_on(), \
             mock.patch('chat.history_boundary.cc_history_token_budget', return_value=3000), \
             mock.patch('chat.context_lean.lean_tool_budget_enabled', return_value=False), \
             mock.patch('chat.context_lean.lean_file_dedup_enabled', return_value=False):
            trimmed_with_file, _, _ = boundary_rows_for_summary(
                self._get_db, history_mode='cc_token_budget', static_dir='/tmp',
                read_file_fn=lambda _sd, url: files.get(url),
            )
            trimmed_no_file, _, _ = boundary_rows_for_summary(
                self._get_db, history_mode='cc_token_budget', static_dir='/tmp',
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

        with mock.patch('chat.history_boundary.lean_history_enabled', return_value=False):
            trimmed, oldest, summarize = boundary_rows_for_summary(self._get_db)
        self.assertGreater(trimmed, 0)
        self.assertGreater(len(summarize), 0)
        self.assertGreater(oldest, trimmed)

    def test_resolve_fetch_plan_explicit_mode_ignores_global_lean(self):
        with self._lean_on():
            plan = resolve_fetch_plan(available_count=100, for_cc=True, history_mode='legacy_block')
        self.assertEqual(plan['mode'], 'legacy_block')
        self.assertEqual(plan['fetch_limit'], 60)
        self.assertEqual(plan['history_token_budget'], 0)


if __name__ == '__main__':
    unittest.main()
