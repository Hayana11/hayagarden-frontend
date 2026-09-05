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
    INTENT_FORGING,
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
from chat.context_window_forge_publish import EMPTY_SHA256
from chat.context_window_target_prepare import (
    PREPARE_STATUS_READY,
    offline_target_prepare_hooks,
    prepare_context_window_target,
)
from chat.daily_context import (
    WINDOW_MODE_MANUAL_STAGED,
    _connect,
    get_selected_carryover_messages,
)
from chat.daily_history import build_daily_window_context
from chat.session_registry import register_context_claude_session


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
        target_before = dc.get_daily_context_by_id(
            fx['failed_target_id'], db_path=self.db,
        )
        self.assertEqual(target_before['window_mode'], WINDOW_MODE_MANUAL_STAGED)
        self.assertEqual(target_before['status'], dc.STATUS_PROVISIONAL)
        mapping_before = sqlite3.connect(self.db).execute(
            '''SELECT message_id, context_id, context_epoch, resident_generation, role
               FROM daily_message_contexts
               WHERE context_id=? ORDER BY message_id''',
            (int(fx['failed_target_id']),),
        ).fetchall()
        target_registry_session = str(uuid.uuid4())
        register_context_claude_session(
            context_id=int(fx['failed_target_id']),
            context_epoch=int(target_before['context_epoch']),
            resident_generation=int(target_before['resident_generation']),
            chat_id=self.chat_id,
            claude_session_id=target_registry_session,
            cwd=str(self.cwd),
            source='fallback-test-target',
            scan_offset=0,
            claude_home=str(self.home),
            db_path=self.db,
        )
        registry_before = sqlite3.connect(self.db).execute(
            '''SELECT claude_session_id, context_id, context_epoch,
                      resident_generation, chat_id, cwd, source, scan_offset
               FROM context_claude_sessions
               WHERE context_id=? ORDER BY claude_session_id''',
            (int(fx['failed_target_id']),),
        ).fetchall()
        self.assertTrue(registry_before)
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
        self.assertEqual(int(intent['first_user_message_id']), int(fx['failed_user_message_id']))
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

        # The failed target remains as evidence, but no longer blocks prepare.
        failed_target_after = dc.get_daily_context_by_id(
            fx['failed_target_id'], db_path=self.db,
        )
        self.assertIsNotNone(failed_target_after)
        self.assertEqual(failed_target_after['window_mode'], 'manual')
        self.assertEqual(failed_target_after['status'], dc.STATUS_PROVISIONAL)
        self.assertIsNotNone(failed_target_after['closed_at'])
        self.assertEqual(failed_target_after['close_reason'], 'cold_fallback')
        self.assertEqual(
            failed_target_after['switch_request_id'],
            target_before['switch_request_id'],
        )
        conn = sqlite3.connect(self.db)
        try:
            mapping_after = conn.execute(
                '''SELECT message_id, context_id, context_epoch, resident_generation, role
                   FROM daily_message_contexts
                   WHERE context_id=? ORDER BY message_id''',
                (int(fx['failed_target_id']),),
            ).fetchall()
            registry_after = conn.execute(
                '''SELECT claude_session_id, context_id, context_epoch,
                          resident_generation, chat_id, cwd, source, scan_offset
                   FROM context_claude_sessions
                   WHERE context_id=? ORDER BY claude_session_id''',
                (int(fx['failed_target_id']),),
            ).fetchall()
            staged_n = conn.execute(
                '''SELECT COUNT(*) FROM daily_contexts
                   WHERE chat_id=? AND window_mode=?''',
                (self.chat_id, WINDOW_MODE_MANUAL_STAGED),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mapping_after, mapping_before)
        self.assertEqual(registry_after, registry_before)
        self.assertEqual(int(staged_n), 0)
        self.assertEqual(int(intent['target_context_id']), int(fx['failed_target_id']))

        # Real target-prepare path can now create a fresh staged target.
        recovery = dc.get_daily_context_by_id(
            result.fallback_context_id, db_path=self.db,
        )
        prepare_request_id = str(uuid.uuid4())
        prepare_session_id = str(uuid.uuid4())
        now_s = '2026-08-01 12:00:00'
        conn = _connect(self.db)
        try:
            conn.execute(
                '''INSERT INTO context_switch_intents (
                    request_id, chat_id, payload_hash, status,
                    source_context_id, source_context_epoch, source_version,
                    source_resident_generation, source_boundary_message_id,
                    carryover_count, selected_message_ids_json,
                    target_session_id, target_jsonl_sha256, target_jsonl_size,
                    preview_id, thinking_policy, orphan_jsonl_state,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    prepare_request_id, self.chat_id, 'prepare-regression',
                    INTENT_FORGING,
                    int(recovery['id']), int(recovery['context_epoch']),
                    int(recovery['version']), int(recovery['resident_generation']),
                    int(recovery['boundary_message_id']), 0, '[]',
                    prepare_session_id, EMPTY_SHA256, 0, prepare_request_id,
                    'drop', 'none', now_s, now_s,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        prepared = prepare_context_window_target(
            request_id=prepare_request_id,
            db_path=self.db,
            hooks=offline_target_prepare_hooks(Path(self.tmp) / 'next-prepare'),
            now=__import__('datetime').datetime(2026, 8, 1, 12, 0, 0),
        )
        self.assertEqual(prepared.prepare_status, PREPARE_STATUS_READY)
        next_target = dc.get_daily_context_by_id(
            prepared.target_context_id, db_path=self.db,
        )
        self.assertEqual(next_target['window_mode'], WINDOW_MODE_MANUAL_STAGED)

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
        unchanged_target = dc.get_daily_context_by_id(
            fx['failed_target_id'], db_path=self.db,
        )
        self.assertEqual(unchanged_target['window_mode'], WINDOW_MODE_MANUAL_STAGED)
        self.assertIsNone(unchanged_target['closed_at'])
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                '''SELECT COUNT(*) FROM daily_resident_turn_leases
                   WHERE context_id=? AND resident_generation=?''',
                (int(fx['failed_target_id']), 1),
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(get_active_switch_intent(chat_id=self.chat_id, db_path=self.db))

    def test_C_committing_target_identity_mismatch_rolls_back(self):
        """A failed target identity mismatch must leave recovery fully untouched."""
        fx = self.fx
        before_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        conn = _connect(self.db)
        try:
            conn.execute(
                '''UPDATE daily_contexts SET switch_request_id=?
                   WHERE id=? AND chat_id=?''',
                ('wrong-request-id', int(fx['failed_target_id']), self.chat_id),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(FallbackError) as ar:
            recover_from_last_good(
                request_id=fx['failed_request_id'],
                expected_status=fx['expected_status'],
                expected_first_turn_request_id=fx['expected_first_turn_request_id'],
                reason='identity mismatch',
                confirm_abandon_failed_turn=True,
                db_path=self.db,
                chat_id=self.chat_id,
                now=__import__('datetime').datetime(2026, 8, 1, 12, 0, 0),
            )
        self.assertEqual(ar.exception.error_code, FALLBACK_PRECONDITION_FAILED)
        after_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts',
        ).fetchone()[0]
        self.assertEqual(after_contexts, before_contexts)
        target = dc.get_daily_context_by_id(fx['failed_target_id'], db_path=self.db)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL_STAGED)
        self.assertIsNone(target['closed_at'])
        self.assertEqual(target['switch_request_id'], 'wrong-request-id')
        intent = sqlite3.connect(self.db).execute(
            '''SELECT status, target_context_id, fallback_context_id,
                      orphan_jsonl_state
               FROM context_switch_intents WHERE request_id=?''',
            (fx['failed_request_id'],),
        ).fetchone()
        self.assertEqual(intent[0], INTENT_COMMITTING)
        self.assertEqual(int(intent[1]), int(fx['failed_target_id']))
        self.assertIsNone(intent[2])
        self.assertEqual(intent[3], 'precommit_dirty')
        staged_n = sqlite3.connect(self.db).execute(
            '''SELECT COUNT(*) FROM daily_contexts
               WHERE chat_id=? AND window_mode=?''',
            (self.chat_id, WINDOW_MODE_MANUAL_STAGED),
        ).fetchone()[0]
        self.assertEqual(int(staged_n), 1)

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
