"""R4-R4D1 process-local shadow receipt tests."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

from chat import daily_continuity_shadow_receipt as rs
from chat import daily_runtime as dr


def _db_with_rows(rows):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            cache_info TEXT NOT NULL DEFAULT '',
            tool_calls TEXT NOT NULL DEFAULT '',
            branches TEXT NOT NULL DEFAULT '',
            branch_idx INTEGER NOT NULL DEFAULT 0,
            attachments TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.executemany(
        """INSERT INTO chat_messages
           (id, author, content, source_kind, cache_info, created_at)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def _plan(db, *, turn_kind='cold', pending=None):
    return types.SimpleNamespace(
        context_id=7,
        context_epoch=2,
        resident_generation=3,
        resident_key='default:2:3',
        user_message_id=1,
        db_path=db,
        transcript_claude_session_id='session-1',
        transcript_process_generation=9,
        manifest={
            'turn_kind': turn_kind,
            'transcript_mapping_status': 'MAPPED',
        },
        is_cold=turn_kind in ('cold', 'respawn'),
        is_respawn=turn_kind == 'respawn',
        request_id='request-1',
        chat_id='default',
        assembly={'current_day_history': [], 'carryover_messages': []},
        _continuity_shadow_pending_receipt=pending,
    )


def _pending(kind='cold'):
    return {
        'turn_kind': kind,
        'production_content_hash': hashlib.sha256(b'prompt').hexdigest(),
        'production_content_token_estimate': 3,
        'fixed_section_fingerprints': (
            ('invariant_system', 'system:abc', 'abc', 2),
        ),
        'base_plan_id': 'plan-1',
        'base_plan_hash': 'plan-hash-1',
    }


class ReceiptContractTests(unittest.TestCase):
    def setUp(self):
        rs.clear_for_tests()

    def tearDown(self):
        rs.clear_for_tests()

    def test_receipt_is_immutable_metadata_only(self):
        member = types.SimpleNamespace(
            seq=0,
            source_ref='turn:1:2',
            source_revision='revision',
            source_kind='completed_turn',
            content_hash='content',
            span_start=None,
            span_end=None,
        )
        receipt = rs.InstalledContextShadowReceipt.build(
            context_id=1,
            context_epoch=2,
            resident_generation=3,
            resident_key='k',
            claude_session_id='sid',
            process_generation=4,
            base_plan_id='p',
            base_plan_hash='ph',
            source_members=(member,),
            fixed_section_fingerprints=(
                ('invariant_system', 'system:x', 'hash', 4),
            ),
            production_content_hash='body-hash',
            production_content_token_estimate=8,
            source_turn_kind='cold',
        )
        with self.assertRaises((AttributeError, TypeError)):
            receipt.context_id = 9
        self.assertNotIn('body', repr(receipt))
        self.assertNotIn('system text', repr(receipt))
        self.assertEqual(receipt.installed_source_members[0].source_ref, 'turn:1:2')
        self.assertEqual(receipt.measurement_semantics, rs.MEASUREMENT_SEMANTICS)

    def test_store_key_and_reset_are_process_local(self):
        receipt = rs.InstalledContextShadowReceipt.build(
            context_id=1, context_epoch=2, resident_generation=3,
            resident_key='k', claude_session_id='sid', process_generation=4,
            base_plan_id='p', base_plan_hash='ph', source_members=(),
            source_turn_kind='cold',
        )
        rs.commit(receipt)
        self.assertIs(rs.get(1, 2, 3), receipt)
        self.assertIsNone(rs.get(1, 2, 4))
        rs.clear_for_tests()
        self.assertIsNone(rs.get(1, 2, 3))

    def test_identity_match_requires_all_live_facts(self):
        receipt = rs.InstalledContextShadowReceipt.build(
            context_id=1, context_epoch=2, resident_generation=3,
            resident_key='k', claude_session_id='sid', process_generation=4,
            base_plan_id='p', base_plan_hash='ph', source_members=(),
            source_turn_kind='cold',
        )
        facts = dict(
            context_id=1, context_epoch=2, resident_generation=3,
            resident_key='k', claude_session_id='sid', process_generation=4,
        )
        self.assertTrue(receipt.matches_live(**facts))
        for key, value in (
            ('context_id', 9), ('context_epoch', 9), ('resident_generation', 9),
            ('resident_key', 'other'), ('claude_session_id', 'other'),
            ('process_generation', 9),
        ):
            altered = dict(facts)
            altered[key] = value
            self.assertFalse(receipt.matches_live(**altered))

    def test_advance_returns_new_receipt_and_keeps_old_immutable(self):
        first = types.SimpleNamespace(
            seq=0, source_ref='turn:1:2', source_revision='r1',
            source_kind='completed_turn', content_hash='h1',
        )
        second = types.SimpleNamespace(
            seq=0, source_ref='turn:3:4', source_revision='r2',
            source_kind='completed_turn', content_hash='h2',
        )
        old = rs.InstalledContextShadowReceipt.build(
            context_id=1, context_epoch=2, resident_generation=3,
            resident_key='k', claude_session_id='sid', process_generation=4,
            base_plan_id='p', base_plan_hash='ph', source_members=(first,),
            source_turn_kind='cold',
        )
        new = rs.advance(old, new_members=(second,), source_turn_kind='hot')
        self.assertIsNot(new, old)
        self.assertEqual(len(old.installed_source_members), 1)
        self.assertEqual(len(new.installed_source_members), 2)
        self.assertEqual(new.installed_source_members[-1].source_ref, 'turn:3:4')
        self.assertEqual(new.installed_source_members[-1].seq, 1)
        self.assertEqual(old.source_turn_kind, 'cold')
        self.assertEqual(new.source_turn_kind, 'hot')


class ReceiptRuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        rs.clear_for_tests()
        dr.reset_bindings_for_tests()

    def tearDown(self):
        rs.clear_for_tests()
        dr.reset_bindings_for_tests()

    def test_cold_success_commits_exact_assembly_members_without_db_write(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            with mock.patch.object(dr.logger, 'warning') as warning:
                self.assertTrue(
                    dr._commit_continuity_shadow_receipt(
                        plan, assistant_message_id=2,
                    )
                )
            self.assertIsNotNone(rs.get(7, 2, 3))
            receipt = rs.get(7, 2, 3)
            self.assertEqual(
                [item.source_ref for item in receipt.installed_source_members],
                ['turn:1:2'],
            )
            warning.assert_not_called()
            tables = sqlite3.connect(db).execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%receipt%'"
            ).fetchall()
            self.assertEqual(tables, [])
        finally:
            os.unlink(db)

    def test_mapping_failure_does_not_commit_cold_receipt(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            plan.manifest['transcript_mapping_status'] = 'BLOCKED'
            self.assertFalse(
                dr._commit_continuity_shadow_receipt(
                    plan, assistant_message_id=2,
                )
            )
            self.assertIsNone(rs.get(7, 2, 3))
        finally:
            os.unlink(db)

    def test_hot_matching_receipt_proof_and_advance(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
            (3, 'hayana', 'next', 'chat', '{}', '2026-09-10 10:01:00'),
            (4, 'fyodor', 'next reply', 'chat', '{}', '2026-09-10 10:01:01'),
        ))
        try:
            cold_plan = _plan(db, pending=_pending())
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                cold_plan, assistant_message_id=2,
            ))
            receipt = rs.get(7, 2, 3)
            resident = types.SimpleNamespace(
                session_id='session-1', generation=9,
            )
            hot_plan = _plan(
                db,
                turn_kind='hot',
                pending=_pending('hot'),
            )
            hot_plan.user_message_id = 3
            hot_plan.resident_key = 'default:2:3'
            with mock.patch.object(dr.logger, 'info') as info:
                dr._observe_continuity_shadow(
                    plan=hot_plan,
                    resident=resident,
                    static_system='STATIC',
                    content='next',
                )
            observation = json.loads(info.call_args.args[1])
            self.assertEqual(observation['source_proof_status'], 'ready')
            self.assertTrue(observation['installed_context_proven'])
            self.assertEqual(observation['budget_status'], 'blocked')
            self.assertEqual(observation['error_code'], 'hot_budget_policy_unmapped')
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                hot_plan, assistant_message_id=4,
            ))
            advanced = rs.get(7, 2, 3)
            self.assertIsNot(advanced, receipt)
            self.assertEqual(len(advanced.installed_source_members), 2)
        finally:
            os.unlink(db)

    def test_hot_missing_receipt_blocks_without_adapter(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
        ))
        try:
            plan = _plan(db, turn_kind='hot')
            resident = types.SimpleNamespace(session_id='session-1', generation=9)
            with mock.patch(
                'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
            ) as adapter, mock.patch.object(dr.logger, 'info') as info:
                dr._observe_continuity_shadow(
                    plan=plan,
                    resident=resident,
                    static_system='STATIC',
                    content='hot',
                )
            adapter.assert_not_called()
            observation = json.loads(info.call_args.args[1])
            self.assertEqual(
                observation['source_proof_error_code'],
                'installed_context_receipt_missing',
            )
            self.assertFalse(observation['installed_context_proven'])
            self.assertEqual(observation['budget_error_code'], 'hot_budget_policy_unmapped')
        finally:
            os.unlink(db)
