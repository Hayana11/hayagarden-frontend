"""Cold fallback recover-from-last-good — temp SQLite only (no live Claude)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import daily_context as dc
from chat.context_window import (
    INTENT_COMMITTING,
    INTENT_HANDOFF_PENDING,
    INTENT_RELEASED,
    get_active_switch_intent,
    resolve_canonical_context_row_conn,
)
from chat.context_window_fallback import (
    FALLBACK_PRECONDITION_FAILED,
    FallbackError,
    build_owner_canary_fixture,
    inspect_failed_first_turn,
    recover_from_last_good,
)
from chat.daily_context import _connect, get_selected_carryover_messages
from chat.daily_history import build_daily_window_context


class ContextWindowFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='cw-fallback-')
        self.db = os.path.join(self.tmp, 'test.db')
        self.home = Path(self.tmp) / 'claude-home'
        self.cwd = Path(self.tmp) / 'proj'
        self.chat_id = 'fallback-test'
        self.fx = build_owner_canary_fixture(
            db_path=self.db,
            claude_home=self.home,
            cwd=self.cwd,
            chat_id=self.chat_id,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_A_owner_abandon_recover_from_last_good(self):
        """Path A: abandon + recovery materials from last-good safe_cursor."""
        fx = self.fx
        insp = inspect_failed_first_turn(
            request_id=fx['failed_request_id'], db_path=self.db, chat_id=self.chat_id,
        )
        self.assertTrue(insp['ok'])
        self.assertTrue(insp['is_latest_active'])
        self.assertEqual(insp['status'], INTENT_COMMITTING)

        result = recover_from_last_good(
            request_id=fx['failed_request_id'],
            expected_status=fx['expected_status'],
            expected_first_turn_request_id=fx['expected_first_turn_request_id'],
            reason='owner abandon for test A',
            confirm_abandon_failed_turn=True,
            db_path=self.db,
            chat_id=self.chat_id,
        )
        self.assertGreaterEqual(result.safe_cursor, result.floor_cursor)
        self.assertNotIn(int(fx['failed_user_message_id']), result.carryover_message_ids)

        intent = _connect(self.db).execute(
            'SELECT * FROM context_switch_intents WHERE request_id=?',
            (fx['failed_request_id'],),
        ).fetchone()
        self.assertEqual(intent['status'], INTENT_RELEASED)
        self.assertEqual(intent['error_code'], 'FIRST_TURN_ABANDONED_BY_OWNER')
        self.assertEqual(intent['orphan_jsonl_state'], 'precommit_dirty')  # evidence kept
        self.assertIsNotNone(intent['owner_abandoned_at'])
        self.assertEqual(int(intent['fallback_context_id']), result.fallback_context_id)

        self.assertIsNone(get_active_switch_intent(chat_id=self.chat_id, db_path=self.db))
        conn = _connect(self.db)
        try:
            canonical = resolve_canonical_context_row_conn(conn, chat_id=self.chat_id)
        finally:
            conn.close()
        self.assertEqual(int(canonical['id']), result.fallback_context_id)
        self.assertEqual(canonical['window_mode'], 'manual')
        self.assertEqual(int(canonical['source_context_id']), fx['last_good_context_id'])
        self.assertEqual(int(canonical['boundary_message_id']), result.safe_cursor)
        # Recovery has no Registry / cursor / lease.
        reg_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM context_claude_sessions WHERE context_id=?',
            (result.fallback_context_id,),
        ).fetchone()[0]
        self.assertEqual(reg_n, 0)
        self.assertIsNone(dc.get_resident_history_cursor(
            result.fallback_context_id, 1, db_path=self.db,
        ))
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases WHERE context_id=?',
            (result.fallback_context_id,),
        ).fetchone()[0]
        self.assertEqual(lease_n, 0)

        carry = get_selected_carryover_messages(
            result.fallback_context_id, db_path=self.db,
        )
        carry_ids = [int(m['message_id']) for m in carry]
        self.assertEqual(carry_ids, list(result.carryover_message_ids))
        self.assertIn(result.safe_cursor, carry_ids)
        self.assertNotIn(int(fx['failed_user_message_id']), carry_ids)

        # Failed user has no assistant.
        asst_n = sqlite3.connect(self.db).execute(
            '''SELECT COUNT(*) FROM daily_message_contexts
               WHERE context_id=? AND role='assistant' AND message_id>?''',
            (int(fx['failed_target_id']), int(fx['failed_user_message_id'])),
        ).fetchone()[0]
        self.assertEqual(asst_n, 0)

        # Next new user: cold history assembly includes recovery materials only.
        conn = sqlite3.connect(self.db)
        cur = conn.execute(
            "INSERT INTO chat_messages (author, content) VALUES ('hayana','new after fallback')",
        )
        conn.commit()
        new_mid = int(cur.lastrowid)
        conn.close()
        dc.record_daily_message_context(
            new_mid,
            context_id=result.fallback_context_id,
            context_epoch=result.fallback_context_epoch,
            resident_generation=1,
            role='user',
            db_path=self.db,
        )
        built = build_daily_window_context(
            chat_id=self.chat_id,
            daily_context=dc.get_daily_context_by_id(
                result.fallback_context_id, db_path=self.db,
            ),
            current_user_message_id=new_mid,
            is_cold=True,
            db_path=self.db,
        )
        cold_ids = [int(m['message_id']) for m in built['carryover_messages']]
        self.assertEqual(cold_ids, list(result.carryover_message_ids))
        self.assertNotIn(int(fx['failed_user_message_id']), cold_ids)
        # Last-good context was closed, not reopened.
        lg = dc.get_daily_context_by_id(fx['last_good_context_id'], db_path=self.db)
        self.assertIsNotNone(lg.get('closed_at'))
        self.assertEqual(lg.get('close_reason'), 'cold_fallback')

    def test_B_locked_without_owner_confirm(self):
        """Path B: dirty/committing stay locked without full owner confirm."""
        fx = self.fx
        before_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        before_intents = sqlite3.connect(self.db).execute(
            'SELECT status, orphan_jsonl_state FROM context_switch_intents WHERE request_id=?',
            (fx['failed_request_id'],),
        ).fetchone()

        with self.subTest('no_confirm'):
            with self.assertRaises(FallbackError) as ar:
                recover_from_last_good(
                    request_id=fx['failed_request_id'],
                    expected_status=fx['expected_status'],
                    expected_first_turn_request_id=fx['expected_first_turn_request_id'],
                    reason='attempt without confirm',
                    confirm_abandon_failed_turn=False,
                    db_path=self.db,
                    chat_id=self.chat_id,
                )
            self.assertEqual(ar.exception.error_code, FALLBACK_PRECONDITION_FAILED)

        with self.subTest('empty_reason'):
            with self.assertRaises(FallbackError) as ar:
                recover_from_last_good(
                    request_id=fx['failed_request_id'],
                    expected_status=fx['expected_status'],
                    expected_first_turn_request_id=fx['expected_first_turn_request_id'],
                    reason='   ',
                    confirm_abandon_failed_turn=True,
                    db_path=self.db,
                    chat_id=self.chat_id,
                )
            self.assertEqual(ar.exception.error_code, FALLBACK_PRECONDITION_FAILED)

        with self.subTest('status_cas_mismatch'):
            with self.assertRaises(FallbackError) as ar:
                recover_from_last_good(
                    request_id=fx['failed_request_id'],
                    expected_status=INTENT_HANDOFF_PENDING,
                    expected_first_turn_request_id=fx['expected_first_turn_request_id'],
                    reason='wrong status',
                    confirm_abandon_failed_turn=True,
                    db_path=self.db,
                    chat_id=self.chat_id,
                )
            self.assertEqual(ar.exception.error_code, FALLBACK_PRECONDITION_FAILED)

        with self.subTest('unexpired_lease'):
            # Attach an unexpired lease on failed target.
            from chat.daily_context import _now_local_str
            import datetime
            exp = (datetime.datetime(2026, 8, 1, 12, 0, 0) + datetime.timedelta(hours=1)).strftime(
                '%Y-%m-%d %H:%M:%S',
            )
            conn = _connect(self.db)
            try:
                conn.execute(
                    '''INSERT INTO daily_resident_turn_leases (
                        context_id, resident_generation, lease_owner,
                        request_message_id, acquired_at, expires_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?)''',
                    (
                        int(fx['failed_target_id']), 1, 'lease-owner',
                        int(fx['failed_user_message_id']),
                        '2026-08-01 12:00:00', exp, '2026-08-01 12:00:00',
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            with self.assertRaises(FallbackError) as ar:
                recover_from_last_good(
                    request_id=fx['failed_request_id'],
                    expected_status=fx['expected_status'],
                    expected_first_turn_request_id=fx['expected_first_turn_request_id'],
                    reason='lease still live',
                    confirm_abandon_failed_turn=True,
                    db_path=self.db,
                    chat_id=self.chat_id,
                    now=__import__('datetime').datetime(2026, 8, 1, 12, 0, 0),
                )
            self.assertEqual(ar.exception.error_code, FALLBACK_PRECONDITION_FAILED)

        # Zero side effects on reject paths.
        after_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        self.assertEqual(after_contexts, before_contexts)
        after_intent = sqlite3.connect(self.db).execute(
            'SELECT status, orphan_jsonl_state FROM context_switch_intents WHERE request_id=?',
            (fx['failed_request_id'],),
        ).fetchone()
        self.assertEqual(after_intent[0], before_intents[0])
        self.assertEqual(after_intent[1], before_intents[1])
        self.assertEqual(after_intent[0], INTENT_COMMITTING)
        self.assertIsNotNone(get_active_switch_intent(chat_id=self.chat_id, db_path=self.db))

    def test_A_handoff_pending_closes_target_not_source(self):
        """handoff_pending: canonical is failed target; close target with cold_fallback."""
        fx = self.fx
        now_s = '2026-08-01 12:00:00'
        conn = _connect(self.db)
        try:
            # Simulate handoff: close source (last-good), promote staged → open manual.
            conn.execute(
                '''UPDATE daily_contexts SET closed_at=?, close_reason='soft_rotate',
                   version=version+1, updated_at=? WHERE id=? AND closed_at IS NULL''',
                (now_s, now_s, int(fx['last_good_context_id'])),
            )
            conn.execute(
                '''UPDATE daily_contexts SET window_mode='manual', updated_at=?
                   WHERE id=?''',
                (now_s, int(fx['failed_target_id'])),
            )
            conn.execute(
                '''UPDATE context_switch_intents SET status=?, updated_at=?
                   WHERE request_id=?''',
                (INTENT_HANDOFF_PENDING, now_s, fx['failed_request_id']),
            )
            conn.commit()
        finally:
            conn.close()

        result = recover_from_last_good(
            request_id=fx['failed_request_id'],
            expected_status=INTENT_HANDOFF_PENDING,
            expected_first_turn_request_id=fx['expected_first_turn_request_id'],
            reason='abandon handoff_pending',
            confirm_abandon_failed_turn=True,
            db_path=self.db,
            chat_id=self.chat_id,
            now=__import__('datetime').datetime(2026, 8, 1, 12, 0, 0),
        )
        self.assertEqual(result.closed_context_id, int(fx['failed_target_id']))
        self.assertNotIn(int(fx['failed_user_message_id']), result.carryover_message_ids)
        failed_tgt = dc.get_daily_context_by_id(fx['failed_target_id'], db_path=self.db)
        self.assertEqual(failed_tgt.get('close_reason'), 'cold_fallback')
        lg = dc.get_daily_context_by_id(fx['last_good_context_id'], db_path=self.db)
        self.assertEqual(lg.get('close_reason'), 'soft_rotate')  # not reopened / not overwritten
        conn = _connect(self.db)
        try:
            canonical = resolve_canonical_context_row_conn(conn, chat_id=self.chat_id)
        finally:
            conn.close()
        self.assertEqual(int(canonical['id']), result.fallback_context_id)


if __name__ == '__main__':
    unittest.main()
