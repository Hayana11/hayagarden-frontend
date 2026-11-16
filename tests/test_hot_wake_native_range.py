"""Section 6: a spoken hot Wake must not break the previous Chat tail's offset proof."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_runtime as dr
from chat import unified_heartbeat_a1 as uh
from tests import test_daily_runtime as tdr
from wake import executor


class HotWakeNativeRangeTests(unittest.TestCase):
    def test_spoken_hot_wake_keeps_chat_tail_provable(self):
        fixture = tdr.ContextPlanConsumerTests('test_native_tail_user_one_assistant_one_passes')
        db, plan, frozen, _ = fixture._native_tail_fixture(last_good_end=400)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'session-1.jsonl')
        rows = [
            {'type': 'user', 'uuid': 'w-in', 'parentUuid': None, 'sessionId': 'session-1',
             'message': {'role': 'user', 'content': 'internal wake input'}},
            {'type': 'assistant', 'uuid': 'w-out', 'parentUuid': 'w-in', 'sessionId': 'session-1',
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hot wake words'}]}},
        ]
        with open(path, 'wb') as fh:       # 400 bytes of earlier Chat history, then the Wake round
            fh.write(b'x' * 399 + b'\n' + b''.join((json.dumps(r) + '\n').encode() for r in rows))
        watermark_id = frozen['receipt'].installed_source_watermark
        conn = sqlite3.connect(db)
        conn.execute(
            'INSERT INTO context_claude_sessions (context_id, context_epoch, resident_generation, chat_id, '
            'claude_session_id, transcript_path, source, process_generation, scan_offset, scan_status, '
            "last_mapped_message_id) VALUES (7, 3, 1, 'default', 'session-1', ?, 'test', 1, 400, 'READY', ?)",
            (path, watermark_id))
        conn.commit()
        conn.close()
        local = types.SimpleNamespace(context_id=7, context_epoch=3, resident_generation=1, resident_key='default:e3:g1')
        wm = uh.SharedTranscriptWatermark(7, 3, 1, 'default:e3:g1', 'session-1', path, 400, 1)
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=local):
            binding = uh.prepare_shared_transcript_native_binding(
                wm, types.SimpleNamespace(session_id='session-1', generation=1),
                jsonl_finality={'stream_totals_match': True})

        def get_db():
            c = sqlite3.connect(db)
            c.row_factory = sqlite3.Row
            return c
        with mock.patch('chat.window_identity.soft_window_enabled', return_value=False), \
                mock.patch.object(executor, '_retire_uh_a1_after_failed_delivery'):
            out = executor.execute(
                'message', 'thought', 'hot wake words', 'normal', get_db_fn=get_db,
                cache_info={'provider': 'claude_code', 'source': 'wake', 'b3_authority': True,
                            'unified_chat_resident': True, 'unified_main_chat_proactive': True},
                wake_run_id='hot-1', settlement_required=False, native_transcript_binding=binding,
                window_identity={'chat_id': 'default', 'context_id': 7, 'context_epoch': 3, 'resident_generation': 1})
        self.assertTrue(out['delivered'])
        registry = dr.get_context_claude_session(7, 1, db_path=db)
        self.assertEqual(registry['scan_offset'], binding['end_offset'])            # the cursor moved past last-good
        last_good = {'context_id': 7, 'context_epoch': 3, 'resident_generation': 1,
                     'claude_session_id': 'session-1', 'transcript_end_offset': 400}
        with mock.patch.object(dr, 'get_same_context_last_good', return_value=last_good):
            proof = dr._hot_native_tail_proof(plan, frozen=frozen, registry=registry)
        self.assertEqual((proof['status'], proof['reason']), ('pass', None), proof)
        self.assertEqual(proof['wake_skip_count'], 1)
        os.unlink(db)


if __name__ == '__main__':
    unittest.main()
