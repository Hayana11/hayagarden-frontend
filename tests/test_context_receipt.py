"""R5-R2A durable ContextReceipt storage contract tests."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from chat import daily_context
from chat.context_receipt import (
    RECEIPT_RESULT_INSTALLED,
    RECEIPT_RESULT_SUPERSEDED,
    ContextReceipt,
    ContextReceiptConflict,
    ContextReceiptMember,
    create_receipt,
    ensure_context_receipt_schema,
    get_receipt,
    get_receipt_members,
    hot_advance_receipt,
    mark_superseded,
)


def _member(
    order: int,
    *,
    source_ref: str | None = None,
    representation_id: str | None = None,
    content_hash: str | None = None,
) -> ContextReceiptMember:
    return ContextReceiptMember(
        installed_order=order,
        representation_id=representation_id or 'raw:%d' % order,
        representation_kind='raw',
        source_ref=source_ref or 'turn:%d' % order,
        source_revision='revision:%d' % order,
        source_kind='completed_turn',
        content_hash=content_hash or 'content:%d' % order,
        span_start=None,
        span_end=None,
        branch_id='active-transcript',
    )


def _receipt(
    members: tuple[ContextReceiptMember, ...] = (_member(0), _member(1)),
    *,
    watermark: int = 10,
    plan_hash: str = 'plan-hash-1',
    provider: str = 'claude_code',
    model_identity: str = 'claude-sonnet',
    session_id: str = 'session-1',
    generation: int = 1,
) -> ContextReceipt:
    return ContextReceipt.build(
        context_id=7,
        context_epoch=3,
        resident_generation=generation,
        resident_key='default:3:%d' % generation,
        provider=provider,
        model_identity=model_identity,
        session_id=session_id,
        process_generation=11,
        plan_id='plan-id-1',
        plan_hash=plan_hash,
        budget_policy_version='continuity_context_budget_v1',
        measurement_semantics='heuristic_cjk1_ascii4_v1',
        installed_source_watermark=watermark,
        members=members,
        committed_at='2026-09-12T10:00:00+00:00',
        updated_at='2026-09-12T10:00:00+00:00',
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    ensure_context_receipt_schema(conn)
    return conn


class ContextReceiptSchemaTests(unittest.TestCase):
    def test_schema_is_additive_idempotent_and_contains_only_two_receipt_tables(self):
        conn = _connection()
        ensure_context_receipt_schema(conn)
        tables = {
            row['name']
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'context_receipt%'"
            )
        }
        self.assertEqual(
            tables,
            {'context_receipts', 'context_receipt_members'},
        )
        receipt_columns = {
            row['name'] for row in conn.execute('PRAGMA table_info(context_receipts)')
        }
        member_columns = {
            row['name'] for row in conn.execute(
                'PRAGMA table_info(context_receipt_members)'
            )
        }
        for forbidden in (
            'body',
            'prompt',
            'context_plan_json',
            'source_snapshot_json',
            'chunk_body',
            'provider_output',
        ):
            self.assertNotIn(forbidden, receipt_columns | member_columns)
        conn.close()

    def test_daily_context_schema_registers_the_same_two_tables(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            daily_context.ensure_schema(path)
            daily_context.ensure_schema(path)
            conn = sqlite3.connect(path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'context_receipt%'"
                )
            }
            self.assertEqual(
                tables,
                {'context_receipts', 'context_receipt_members'},
            )
            conn.close()
        finally:
            os.unlink(path)


class ContextReceiptWriteTests(unittest.TestCase):
    def test_create_receipt_and_members_are_atomic_and_persist_across_reopen(self):
        conn = _connection()
        receipt = _receipt()
        self.assertEqual(create_receipt(conn, receipt, receipt_members := (_member(0), _member(1))), receipt)
        self.assertEqual(get_receipt(conn, context_id=7, context_epoch=3, resident_generation=1), receipt)
        self.assertEqual(get_receipt_members(conn, context_id=7, context_epoch=3, resident_generation=1), receipt_members)
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            file_conn = sqlite3.connect(path)
            file_conn.row_factory = sqlite3.Row
            file_conn.execute('PRAGMA foreign_keys = ON')
            ensure_context_receipt_schema(file_conn)
            create_receipt(file_conn, receipt, receipt_members)
            file_conn.close()
            reopened = sqlite3.connect(path)
            reopened.row_factory = sqlite3.Row
            self.assertEqual(
                get_receipt(reopened, context_id=7, context_epoch=3, resident_generation=1),
                receipt,
            )
            self.assertEqual(
                get_receipt_members(
                    reopened,
                    context_id=7,
                    context_epoch=3,
                    resident_generation=1,
                ),
                receipt_members,
            )
            reopened.close()
        finally:
            os.unlink(path)
        conn.close()

    def test_same_create_exact_proof_is_idempotent(self):
        conn = _connection()
        receipt = _receipt()
        members = (_member(0), _member(1))
        first = create_receipt(conn, receipt, members)
        second = create_receipt(
            conn,
            replace(receipt, committed_at='2026-09-12T10:01:00+00:00'),
            members,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0],
            1,
        )
        conn.close()

    def test_same_boundary_different_proof_conflicts(self):
        conn = _connection()
        receipt = _receipt()
        members = (_member(0), _member(1))
        create_receipt(conn, receipt, members)
        variants = (
            replace(receipt, plan_hash='different-plan'),
            _receipt(
                members=(_member(0), _member(1, content_hash='different-content')),
            ),
            replace(receipt, provider='api_relay'),
            replace(receipt, session_id='different-session'),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ContextReceiptConflict):
                    create_receipt(conn, variant, members if variant.plan_hash != receipt.plan_hash else (
                        _member(0), _member(1, content_hash='different-content')
                    ))
        conn.close()


class ContextReceiptCasTests(unittest.TestCase):
    def test_hot_advance_rejects_revision_mismatch_and_lower_watermark(self):
        conn = _connection()
        receipt = _receipt(watermark=10)
        members = (_member(0), _member(1))
        create_receipt(conn, receipt, members)
        with self.assertRaises(ContextReceiptConflict):
            hot_advance_receipt(
                conn,
                expected_receipt_revision=1,
                receipt=_receipt(watermark=11, plan_hash='plan-2'),
                members=members,
            )
        with self.assertRaises(ContextReceiptConflict):
            hot_advance_receipt(
                conn,
                expected_receipt_revision=0,
                receipt=_receipt(watermark=9, plan_hash='plan-2'),
                members=members,
            )
        conn.close()

    def test_same_watermark_same_proof_is_noop_and_changed_proof_conflicts(self):
        conn = _connection()
        receipt = _receipt(watermark=10)
        members = (_member(0), _member(1))
        create_receipt(conn, receipt, members)
        no_op = hot_advance_receipt(
            conn,
            expected_receipt_revision=0,
            receipt=receipt,
            members=members,
        )
        self.assertEqual(no_op, receipt)
        with self.assertRaises(ContextReceiptConflict):
            hot_advance_receipt(
                conn,
                expected_receipt_revision=0,
                receipt=_receipt(watermark=10, plan_hash='different-plan'),
                members=members,
            )
        conn.close()

    def test_greater_watermark_allows_new_plan_and_replaces_members_atomically(self):
        conn = _connection()
        old_members = (_member(0), _member(1))
        receipt = _receipt(watermark=10, members=old_members)
        create_receipt(conn, receipt, old_members)
        new_members = (_member(2, source_ref='turn:2'),)
        advanced = hot_advance_receipt(
            conn,
            expected_receipt_revision=0,
            receipt=_receipt(
                watermark=12,
                plan_hash='plan-2',
                members=new_members,
            ),
            members=new_members,
        )
        self.assertEqual(advanced.receipt_revision, 1)
        self.assertEqual(advanced.plan_hash, 'plan-2')
        self.assertEqual(
            get_receipt_members(
                conn,
                context_id=7,
                context_epoch=3,
                resident_generation=1,
            ),
            new_members,
        )
        conn.close()

    def test_failed_member_replacement_preserves_old_receipt_and_members(self):
        conn = _connection()
        old_members = (_member(0), _member(1))
        receipt = _receipt(watermark=10, members=old_members)
        create_receipt(conn, receipt, old_members)
        conn.execute(
            '''CREATE TRIGGER fail_receipt_member_insert
               BEFORE INSERT ON context_receipt_members
               BEGIN SELECT RAISE(ABORT, 'injected member failure'); END'''
        )
        new_members = (_member(2, source_ref='turn:2'),)
        with self.assertRaises(sqlite3.IntegrityError):
            hot_advance_receipt(
                conn,
                expected_receipt_revision=0,
                receipt=_receipt(
                    watermark=12,
                    plan_hash='plan-2',
                    members=new_members,
                ),
                members=new_members,
            )
        self.assertEqual(
            get_receipt(conn, context_id=7, context_epoch=3, resident_generation=1),
            receipt,
        )
        self.assertEqual(
            get_receipt_members(
                conn,
                context_id=7,
                context_epoch=3,
                resident_generation=1,
            ),
            old_members,
        )
        conn.close()


class ContextReceiptSupersedeTests(unittest.TestCase):
    def test_supersede_requires_explicit_call_and_is_idempotent(self):
        conn = _connection()
        receipt = _receipt()
        create_receipt(conn, receipt, (_member(0), _member(1)))
        superseded = mark_superseded(
            conn,
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            expected_receipt_revision=0,
            target_generation=2,
        )
        self.assertEqual(superseded.result, RECEIPT_RESULT_SUPERSEDED)
        self.assertEqual(superseded.superseded_by_generation, 2)
        self.assertEqual(superseded.receipt_revision, 1)
        self.assertEqual(
            mark_superseded(
                conn,
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                expected_receipt_revision=0,
                target_generation=2,
            ),
            superseded,
        )
        with self.assertRaises(ContextReceiptConflict):
            mark_superseded(
                conn,
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                expected_receipt_revision=1,
                target_generation=3,
            )
        conn.close()

    def test_missing_receipt_returns_none_and_empty_members(self):
        conn = _connection()
        self.assertIsNone(
            get_receipt(conn, context_id=99, context_epoch=1, resident_generation=1)
        )
        self.assertEqual(
            get_receipt_members(
                conn,
                context_id=99,
                context_epoch=1,
                resident_generation=1,
            ),
            (),
        )
        conn.close()


if __name__ == '__main__':
    unittest.main()
