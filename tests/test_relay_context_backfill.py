from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from chat.daily_context import ensure_schema
from tools.backfill_api_relay_contexts import MappingConflict, run_backfill


class RelayContextBackfillTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.db_path = str(Path(tempdir.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, '
            'thinking TEXT, created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
            'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, '
            'file_url TEXT, file_name TEXT)'
        )
        conn.commit()
        conn.close()
        ensure_schema(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO daily_contexts (chat_id,local_day,timezone,boundary_hour,context_epoch,'
            'boundary_message_id,status,carryover_count,is_backfill,resident_generation,version,'
            'window_mode,opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('default','2026-11-13','Asia/Shanghai',4,7,0,'PROVISIONAL',0,0,10,1,
             'manual','2026-11-13 10:00:00'),
        )
        conn.executemany(
            'INSERT INTO chat_messages (id,author,content,thinking,created_at,tool_calls,branches,'
            'branch_idx,cache_info,source_kind,attachments,image_url,file_url,file_name) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [
                (1,'hayana','API user 1','','2026-11-13 10:00:00','[]','[]',0,'{}','chat','[]','','',''),
                (2,'assistant','API assistant 1','','2026-11-13 10:00:00','[]','[]',0,'{"provider":"api_relay"}','chat','[]','','',''),
                (3,'hayana','API user 2','','2026-11-13 10:00:00','[]','[]',0,'{}','chat','[]','','',''),
                (4,'assistant','API assistant 2','','2026-11-13 10:00:00','[]','[]',0,'{"provider":"api_relay"}','chat','[]','','',''),
                (5,'hayana','Other user','','2026-11-13 10:00:00','[]','[]',0,'{}','chat','[]','','',''),
                (6,'assistant','Claude assistant','','2026-11-13 10:00:00','[]','[]',0,'{"provider":"claude_code"}','chat','[]','','',''),
            ],
        )
        conn.executemany(
            'INSERT INTO daily_message_contexts '
            '(message_id,context_id,context_epoch,resident_generation,role) VALUES (?,?,?,?,?)',
            [(3,1,7,9,'user'),(4,1,7,9,'assistant')],
        )
        conn.commit()
        conn.close()

    def _mapping_count(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT COUNT(*) FROM daily_message_contexts').fetchone()[0]
        finally:
            conn.close()

    def test_dry_run_apply_and_rerun_preserve_existing_mappings(self):
        preview = run_backfill(self.db_path)
        self.assertEqual(preview['api_relay_completed_turns'], 2)
        self.assertEqual(preview['fully_unmapped_turns'], 1)
        self.assertEqual(preview['pending_messages'], 2)
        self.assertEqual(preview['target_identity']['resident_generation'], 10)
        self.assertEqual(self._mapping_count(), 2)

        self.assertEqual(run_backfill(self.db_path, apply=True)['applied_messages'], 2)
        self.assertEqual(run_backfill(self.db_path, apply=True)['applied_messages'], 0)
        self.assertEqual(self._mapping_count(), 4)
        conn = sqlite3.connect(self.db_path)
        try:
            generation = conn.execute(
                'SELECT resident_generation FROM daily_message_contexts WHERE message_id=3'
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(generation, 9)

    def test_conflict_stops_before_any_pending_mapping_is_written(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT INTO daily_message_contexts '
            '(message_id,context_id,context_epoch,resident_generation,role) VALUES (?,?,?,?,?)',
            (1,99,8,1,'user'),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(MappingConflict):
            run_backfill(self.db_path, apply=True)
        self.assertEqual(self._mapping_count(), 3)


if __name__ == '__main__':
    unittest.main()
