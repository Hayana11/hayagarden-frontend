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
        _resident_close_fn=None,
        lease_released=False,
        lease_owner='test-lease',
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


def _capacity_pending(
    *,
    status='ANCHOR_RETAINED',
    selected=(1, 2, 3),
    anchor_id=1,
    source_generation=3,
):
    return {
        'source_context_id': 7,
        'source_context_epoch': 2,
        'source_resident_generation': source_generation,
        'target_resident_generation': 4,
        'candidate_session_id': 'session-new',
        'selected_message_ids': tuple(selected),
        'anchor_status': status,
        'anchor_message_id': anchor_id,
        'capacity_baseline_sha256': 'published-jsonl-sha',
        'trigger_reason': 'soft_context',
        'fixed_section_fingerprints': (
            ('invariant_system', 'system:x', 'hash', 2),
            ('accepted_state', 'state:x', 'hash2', 1),
        ),
        'production_content_hash': 'prompt-hash',
        'production_content_token_estimate': 4,
    }

class ReceiptContractTests(unittest.TestCase):
    def setUp(self):
        rs.clear_for_tests()

    def tearDown(self):
        rs.clear_for_tests()

    def test_receipt_is_immutable_metadata_only(self):
        member = types.SimpleNamespace(
            seq=42,
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
            production_content_hash='0123456789abcdef',
            production_content_token_estimate=8,
            source_turn_kind='cold',
        )
        with self.assertRaises((AttributeError, TypeError)):
            receipt.context_id = 9
        self.assertNotIn('body', repr(receipt))
        self.assertNotIn('system text', repr(receipt))
        self.assertEqual(receipt.installed_source_members[0].source_ref, 'turn:1:2')
        self.assertEqual(receipt.installed_source_members[0].installed_order, 0)
        self.assertEqual(receipt.installed_source_members[0].branch_id, 'active-transcript')
        self.assertFalse(hasattr(receipt.installed_source_members[0], 'evidence_refs'))
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
        self.assertEqual(new.installed_source_members[-1].installed_order, 1)
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


    def test_cold_receipt_uses_exact_installed_ids_only(self):
        db = _db_with_rows((
            (1, 'hayana', 'older', 'chat', '{}', '2026-09-10 09:00:00'),
            (2, 'fyodor', 'older reply', 'chat', '{}', '2026-09-10 09:00:01'),
            (3, 'hayana', 'newer', 'chat', '{}', '2026-09-10 10:00:00'),
            (4, 'fyodor', 'newer reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            plan.user_message_id = 3
            plan.assembly['current_day_history'] = [{'id': 3}, {'id': 4}]
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=4,
            ))
            receipt = rs.get(7, 2, 3)
            self.assertEqual(
                [item.source_ref for item in receipt.installed_source_members],
                ['turn:3:4'],
            )
            self.assertEqual(
                [item.installed_order for item in receipt.installed_source_members],
                [0],
            )
        finally:
            os.unlink(db)

    def test_carryover_round_enters_cold_receipt(self):
        db = _db_with_rows((
            (1, 'hayana', 'carry user', 'chat', '{}', '2026-09-10 09:00:00'),
            (2, 'fyodor', 'carry reply', 'chat', '{}', '2026-09-10 09:00:01'),
            (3, 'hayana', 'current user', 'chat', '{}', '2026-09-10 10:00:00'),
            (4, 'fyodor', 'current reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            plan.user_message_id = 3
            plan.assembly['carryover_messages'] = [{'id': 1}, {'id': 2}]
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=4,
            ))
            receipt = rs.get(7, 2, 3)
            self.assertEqual(
                [item.source_ref for item in receipt.installed_source_members],
                ['turn:1:2', 'turn:3:4'],
            )
        finally:
            os.unlink(db)

    def test_canonical_wake_enters_as_autonomous_event(self):
        wake_cache = json.dumps({
            'wake_mode': 'normal',
            'canonical_chat_history': True,
            'unified_chat_resident': True,
            'b3_authority': True,
            'source': 'wake',
            'provider': 'claude_code',
        })
        db = _db_with_rows((
            (1, 'hayana', 'current user', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'current reply', 'chat', '{}', '2026-09-10 10:00:01'),
            (5, 'fyodor', 'wake content', 'wake', wake_cache, '2026-09-10 11:00:00'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            plan.assembly['current_day_history'] = [{'id': 5}]
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=2,
            ))
            receipt = rs.get(7, 2, 3)
            wake = [
                item for item in receipt.installed_source_members
                if item.source_ref == 'wake:5'
            ]
            self.assertEqual(len(wake), 1)
            self.assertEqual(wake[0].source_kind, 'autonomous_event')
        finally:
            os.unlink(db)

    def test_hot_identity_mismatch_fails_closed_without_adapter(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            cold_plan = _plan(db, pending=_pending())
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                cold_plan, assistant_message_id=2,
            ))
            hot_plan = _plan(db, turn_kind='hot')
            resident = types.SimpleNamespace(session_id='different-session', generation=9)
            with mock.patch(
                'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
            ) as adapter, mock.patch.object(dr.logger, 'info') as info:
                dr._observe_continuity_shadow(
                    plan=hot_plan,
                    resident=resident,
                    static_system='STATIC',
                    content='hot',
                )
            adapter.assert_not_called()
            observations = [
                json.loads(call.args[1])
                for call in info.call_args_list
                if call.args and call.args[0] == 'continuity_shadow_observation %s'
            ]
            self.assertEqual(
                observations[-1]['source_proof_error_code'],
                'installed_context_receipt_identity_mismatch',
            )
            self.assertFalse(observations[-1]['installed_context_proven'])
        finally:
            os.unlink(db)

    def test_incomplete_hot_turn_does_not_advance_receipt(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
            (3, 'hayana', 'next', 'chat', '{}', '2026-09-10 10:01:00'),
            (4, 'fyodor', 'partial', 'chat',
             '{"turn_incomplete": true}', '2026-09-10 10:01:01'),
        ))
        try:
            cold_plan = _plan(db, pending=_pending())
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                cold_plan, assistant_message_id=2,
            ))
            prior = rs.get(7, 2, 3)
            hot_plan = _plan(db, turn_kind='hot', pending=_pending('hot'))
            hot_plan.user_message_id = 3
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                hot_plan, assistant_message_id=4,
            ))
            self.assertIs(rs.get(7, 2, 3), prior)
        finally:
            os.unlink(db)

    def test_hot_mapping_not_mapped_does_not_advance_receipt(self):
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
            prior = rs.get(7, 2, 3)
            hot_plan = _plan(db, turn_kind='hot', pending=_pending('hot'))
            hot_plan.user_message_id = 3
            hot_plan.manifest['transcript_mapping_status'] = 'BLOCKED'
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                hot_plan, assistant_message_id=4,
            ))
            self.assertIs(rs.get(7, 2, 3), prior)
        finally:
            os.unlink(db)

    def test_receipt_commit_isolated_from_production_manifest(self):
        db = _db_with_rows((
            (1, 'hayana', 'hello', 'chat', '{}', '2026-09-10 10:00:00'),
            (2, 'fyodor', 'reply', 'chat', '{}', '2026-09-10 10:00:01'),
        ))
        try:
            plan = _plan(db, pending=_pending())
            before = dict(plan.manifest)
            with mock.patch.object(dr.logger, 'info') as info:
                self.assertTrue(dr._commit_continuity_shadow_receipt(
                    plan, assistant_message_id=2,
                ))
            self.assertEqual(plan.manifest, before)
            event_calls = [
                call for call in info.call_args_list
                if call.args and call.args[0] == 'continuity_shadow_receipt_commit %s'
            ]
            self.assertEqual(len(event_calls), 1)
            event = json.loads(event_calls[0].args[1])
            self.assertEqual(event['status'], 'committed')
            self.assertEqual(event['member_count'], 1)
            self.assertIn('membership_hash', event)
            self.assertNotIn('production_content_hash', event)
        finally:
            os.unlink(db)

    def test_receipt_commit_failure_isolated_from_production_manifest(self):
        missing = os.path.join(tempfile.gettempdir(), 'r4d1-missing-source.db')
        if os.path.exists(missing):
            os.unlink(missing)
        plan = _plan(missing, pending=_pending())
        before = dict(plan.manifest)
        with mock.patch.object(dr.logger, 'info') as info:
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=2,
            ))
        self.assertEqual(plan.manifest, before)
        event_calls = [
            call for call in info.call_args_list
            if call.args and call.args[0] == 'continuity_shadow_receipt_commit %s'
        ]
        self.assertEqual(len(event_calls), 1)
        event = json.loads(event_calls[0].args[1])
        self.assertEqual(event['status'], 'skipped')
        self.assertEqual(event['error_code'], 'installed_source_projection_unavailable')
        self.assertFalse(os.path.exists(missing))

    def test_capacity_swap_does_not_create_or_advance_receipt(self):
        plan = _plan('/tmp/unused-r4d1-source.db', turn_kind='capacity_swap')
        plan.is_cold = False
        plan.is_respawn = False
        before = rs.get(7, 2, 3)
        resident = types.SimpleNamespace(session_id='session-1', generation=9)
        with mock.patch(
            'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
        ) as adapter:
            dr._observe_continuity_shadow(
                plan=plan,
                resident=resident,
                static_system='STATIC',
                content='capacity',
            )
        adapter.assert_not_called()
        self.assertIs(rs.get(7, 2, 3), before)


class CapacitySwapReceiptContractTests(unittest.TestCase):
    def setUp(self):
        rs.clear_for_tests()

    def tearDown(self):
        rs.clear_for_tests()

    def _capacity_plan(self, db, pending, *, selected_user=False):
        plan = _plan(db, turn_kind='capacity_swap')
        plan.context_id = 7
        plan.context_epoch = 2
        plan.resident_generation = 4
        plan.resident_key = 'default:2:4'
        plan.user_message_id = 4
        plan.transcript_claude_session_id = 'session-new'
        plan.transcript_process_generation = 10
        plan._continuity_shadow_capacity_pending = dict(pending)
        if selected_user:
            plan._continuity_shadow_capacity_pending['selected_message_ids'] = (
                1, 2, 3, 4,
            )
        return plan

    def _rows(self):
        return _db_with_rows((
            (1, 'hayana', 'anchor', 'chat', '{}', '2026-09-10 09:00:00'),
            (2, 'hayana', 'tail user', 'chat', '{}', '2026-09-10 09:01:00'),
            (3, 'fyodor', 'tail reply', 'chat', '{}', '2026-09-10 09:01:01'),
            (4, 'hayana', 'current user', 'chat', '{}', '2026-09-10 09:02:00'),
            (5, 'fyodor', 'current reply', 'chat', '{}', '2026-09-10 09:02:01'),
        ))

    def test_retained_anchor_is_explicit_and_excluded_from_membership_hash(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(selected=(1, 2, 3), anchor_id=1),
            )
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            receipt = rs.get(7, 2, 4)
            self.assertIsNotNone(receipt.capacity_anchor)
            self.assertEqual(receipt.capacity_anchor.source_ref, 'message:1')
            self.assertEqual(receipt.capacity_anchor.source_revision,
                             receipt.capacity_anchor.source_content_hash)
            self.assertEqual(
                [m.source_ref for m in receipt.installed_source_members],
                ['turn:2:3', 'turn:4:5'],
            )
            self.assertEqual(receipt.capacity_baseline_sha256, 'published-jsonl-sha')
            self.assertEqual(receipt.capacity_source_generation, 3)
            self.assertEqual(
                [f[0] for f in receipt.fixed_section_fingerprints],
                ['invariant_system', 'accepted_state'],
            )
        finally:
            os.unlink(db)

    def test_degraded_anchor_is_retained_with_status(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(
                    status='ANCHOR_IMAGE_DEGRADED',
                    selected=(1, 2, 3),
                    anchor_id=1,
                ),
            )
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertEqual(
                rs.get(7, 2, 4).capacity_anchor.anchor_status,
                'ANCHOR_IMAGE_DEGRADED',
            )
        finally:
            os.unlink(db)

    def test_unavailable_and_too_large_anchor_leave_no_anchor_evidence(self):
        for status in ('ANCHOR_UNAVAILABLE', 'ANCHOR_TOO_LARGE'):
            db = self._rows()
            try:
                plan = self._capacity_plan(
                    db, _capacity_pending(
                        status=status,
                        selected=(2, 3),
                        anchor_id=0,
                    ),
                )
                self.assertTrue(dr._commit_continuity_shadow_receipt(
                    plan, assistant_message_id=5,
                ))
                self.assertIsNone(rs.get(7, 2, 4).capacity_anchor)
            finally:
                os.unlink(db)
            rs.clear_for_tests()

    def test_anchor_requires_selected_formal_user(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(selected=(2, 3), anchor_id=1),
            )
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 4))
        finally:
            os.unlink(db)

    def test_partial_tail_and_current_user_candidate_fail_closed(self):
        db = self._rows()
        try:
            partial = self._capacity_plan(
                db, _capacity_pending(selected=(2,), anchor_id=0,
                                      status='ANCHOR_UNAVAILABLE'),
            )
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                partial, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 4))
            selected = self._capacity_plan(
                db, _capacity_pending(selected=(1, 2, 3, 4), anchor_id=1),
                selected_user=True,
            )
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                selected, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 4))
        finally:
            os.unlink(db)

    def test_capacity_success_drops_source_receipt_and_pending(self):
        db = self._rows()
        try:
            source = rs.InstalledContextShadowReceipt.build(
                context_id=7, context_epoch=2, resident_generation=3,
                resident_key='default:2:3', claude_session_id='session-old',
                process_generation=9, base_plan_id='p', base_plan_hash='h',
                source_members=(), source_turn_kind='cold',
            )
            rs.commit(source)
            plan = self._capacity_plan(
                db, _capacity_pending(selected=(1, 2, 3), anchor_id=1),
            )
            self.assertTrue(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 3))
            self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        finally:
            os.unlink(db)


    def test_anchor_status_and_selected_membership_contradiction_fails_closed(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(
                    status='ANCHOR_UNAVAILABLE',
                    selected=(1, 2, 3),
                    anchor_id=1,
                ),
            )
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 4))
        finally:
            os.unlink(db)

    def test_missing_anchor_row_fails_closed(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(
                    status='ANCHOR_RETAINED',
                    selected=(99, 2, 3),
                    anchor_id=99,
                ),
            )
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertIsNone(rs.get(7, 2, 4))
        finally:
            os.unlink(db)

    def test_target_identity_mismatch_clears_pending_without_target(self):
        db = self._rows()
        try:
            pending = _capacity_pending(selected=(1, 2, 3), anchor_id=1)
            pending['candidate_session_id'] = 'different-session'
            plan = self._capacity_plan(db, pending)
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
            self.assertIsNone(rs.get(7, 2, 4))
        finally:
            os.unlink(db)

    def test_mapping_failure_clears_capacity_pending(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(selected=(1, 2, 3), anchor_id=1),
            )
            plan.manifest['transcript_mapping_status'] = 'BLOCKED'
            self.assertFalse(dr._commit_continuity_shadow_receipt(
                plan, assistant_message_id=5,
            ))
            self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        finally:
            os.unlink(db)

    def test_abort_clears_capacity_pending(self):
        db = self._rows()
        try:
            plan = self._capacity_plan(
                db, _capacity_pending(selected=(1, 2, 3), anchor_id=1),
            )
            with mock.patch.object(dr, 'is_epoch_token_current', return_value=False):
                dr.abort_daily_turn(
                    plan,
                    error_code='provider_failed',
                    resident=types.SimpleNamespace(),
                    respawn=False,
                )
            self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        finally:
            os.unlink(db)

    def test_capacity_observation_marks_pending_commit_without_adapter(self):
        plan = _plan('/tmp/unused-r4d2-source.db', turn_kind='capacity_swap')
        plan.is_cold = False
        plan.is_respawn = False
        plan._continuity_shadow_capacity_pending = _capacity_pending()
        resident = types.SimpleNamespace(session_id='session-new', generation=10)
        with mock.patch(
            'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
        ) as adapter, mock.patch.object(dr.logger, 'info') as info:
            dr._observe_continuity_shadow(
                plan=plan,
                resident=resident,
                static_system='STATIC',
                content='capacity',
            )
        adapter.assert_not_called()
        observation = json.loads(info.call_args.args[1])
        self.assertEqual(
            observation['source_proof_error_code'],
            'capacity_receipt_pending_commit',
        )
        self.assertFalse(observation['installed_context_proven'])
        self.assertEqual(
            plan._continuity_shadow_capacity_pending['production_content_hash'],
            observation['production_content_hash'],
        )

    def test_capacity_observation_without_pending_is_deferred(self):
        plan = _plan('/tmp/unused-r4d2-source.db', turn_kind='capacity_swap')
        plan.is_cold = False
        plan.is_respawn = False
        resident = types.SimpleNamespace(session_id='session-new', generation=10)
        with mock.patch.object(dr.logger, 'info') as info:
            dr._observe_continuity_shadow(
                plan=plan,
                resident=resident,
                static_system='STATIC',
                content='capacity',
            )
        observation = json.loads(info.call_args.args[1])
        self.assertEqual(
            observation['source_proof_error_code'],
            'capacity_receipt_deferred',
        )


class CapacitySwapAttemptIsolationTests(unittest.TestCase):
    def setUp(self):
        rs.clear_for_tests()
        dr.reset_bindings_for_tests()

    def tearDown(self):
        rs.clear_for_tests()
        dr.reset_bindings_for_tests()

    def _plan_for_attempt(self):
        plan = _plan('/tmp/r4d2-capacity-source.db', turn_kind='capacity_swap')
        plan.cursor_before = None
        plan.transcript_cwd = ''
        plan.resident_generation = 3
        plan.resident_key = 'default:2:3'
        return plan

    def _handoff(self, *, published_sha='published-jsonl-sha',
                 candidate_session_id='candidate-session'):
        candidate = types.SimpleNamespace(
            candidate_session_id=candidate_session_id,
            selected_message_ids=(1, 2, 3),
            anchor_status='ANCHOR_RETAINED',
            anchor_message_id=1,
            output_sha256='candidate-output-sha',
        )
        return types.SimpleNamespace(
            ok=True,
            candidate=candidate,
            staged_resident=types.SimpleNamespace(),
            source_context_id=7,
            source_context_epoch=2,
            source_resident_generation=3,
            target_resident_generation=4,
            candidate_session_id=candidate_session_id,
            jsonl_path=None,
            jsonl_sha256=published_sha,
            effective_system='EFFECTIVE',
            warnings=[],
        )

    def _run_attempt(self, plan, handoff, *, fixed_sections=None,
                     fixed_error=None, register_error=None):
        resident = types.SimpleNamespace(
            generation=10,
            session_id='session-new',
            cwd='',
        )
        install_state = {'old_proc': None, 'old_binding': None, 'old_attrs': {}}

        def reprepare(*args, **kwargs):
            plan.resident_generation = 4
            plan.resident_key = 'default:2:4'
            return plan

        def fixed(*args, **kwargs):
            if fixed_error is not None:
                raise fixed_error
            return tuple(fixed_sections or ())

        register = mock.Mock()
        if register_error is not None:
            register.side_effect = register_error

        with (
            mock.patch.object(dr, 'is_capacity_swap_reason', return_value=True),
            mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff),
            mock.patch.object(dr, 'is_epoch_token_current', return_value=False),
            mock.patch.object(
                dr.dc,
                'get_daily_context_by_id',
                return_value={
                    'id': 7,
                    'context_epoch': 2,
                    'resident_generation': 4,
                },
            ),
            mock.patch.object(
                dr,
                'install_capacity_swap_into_live_resident',
                return_value=install_state,
            ),
            mock.patch.object(
                dr,
                'reprepare_after_capacity_swap',
                side_effect=reprepare,
            ),
            mock.patch.object(dr, '_adopt_reprepared_plan_in_place'),
            mock.patch.object(
                dr,
                'register_capacity_swap_generation',
                register,
            ),
            mock.patch.object(
                dr,
                '_build_continuity_shadow_fixed_sections',
                side_effect=fixed,
            ),
            mock.patch.object(
                dr,
                '_shadow_fixed_section_fingerprints',
                return_value=(('invariant_system', 'system', 'hash', 1),),
            ),
            mock.patch.object(dr, 'rollback_capacity_swap_install') as rollback,
            mock.patch.object(dr.logger, 'exception') as log_exception,
        ):
            result = dr._attempt_capacity_swap_before_stdin(
                plan,
                resident=resident,
                static_system='STATIC',
                env={},
                trigger_reason='soft_context',
                staged_hooks=object(),
            )
        return result, resident, rollback, log_exception

    def test_shadow_fixed_section_failure_is_fail_open_after_production_commit(self):
        plan = self._plan_for_attempt()
        result, resident, rollback, log_exception = self._run_attempt(
            plan,
            self._handoff(),
            fixed_error=RuntimeError('shadow fixed section bug'),
        )
        self.assertTrue(result['ok'])
        self.assertEqual(dr.get_local_binding().resident_generation, 4)
        rollback.assert_not_called()
        self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        self.assertEqual(
            plan._continuity_shadow_capacity_pending_error,
            'capacity_shadow_pending_build_failed',
        )
        log_exception.assert_called_once()

    def test_valid_shadow_metadata_creates_pending(self):
        plan = self._plan_for_attempt()
        result, _, rollback, _ = self._run_attempt(
            plan,
            self._handoff(),
            fixed_sections=(),
        )
        self.assertTrue(result['ok'])
        self.assertIsInstance(
            plan._continuity_shadow_capacity_pending,
            dict,
        )
        self.assertEqual(
            plan._continuity_shadow_capacity_pending['capacity_baseline_sha256'],
            'published-jsonl-sha',
        )
        rollback.assert_not_called()
        self.assertFalse(
            hasattr(plan, '_continuity_shadow_capacity_pending_error')
        )

    def test_missing_published_sha_is_fail_open_without_candidate_fallback(self):
        plan = self._plan_for_attempt()
        result, resident, rollback, _ = self._run_attempt(
            plan,
            self._handoff(published_sha=''),
        )
        self.assertTrue(result['ok'])
        self.assertEqual(dr.get_local_binding().resident_generation, 4)
        rollback.assert_not_called()
        self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        self.assertEqual(
            plan._continuity_shadow_capacity_pending_error,
            'capacity_baseline_sha_missing',
        )

    def test_production_registry_failure_still_rolls_back(self):
        plan = self._plan_for_attempt()
        result, _, rollback, _ = self._run_attempt(
            plan,
            self._handoff(),
            register_error=RuntimeError('registry failed'),
        )
        self.assertFalse(result['ok'])
        rollback.assert_called_once()
        self.assertFalse(hasattr(plan, '_continuity_shadow_capacity_pending'))
        self.assertFalse(
            hasattr(plan, '_continuity_shadow_capacity_pending_error')
        )

    def _strict_plan(self, pending):
        plan = _plan('/tmp/r4d2-strict-source.db', turn_kind='capacity_swap')
        plan.context_id = 7
        plan.context_epoch = 2
        plan.resident_generation = 4
        plan.resident_key = 'default:2:4'
        plan.user_message_id = 4
        plan.transcript_claude_session_id = 'session-new'
        plan.transcript_process_generation = 10
        plan._continuity_shadow_capacity_pending = dict(pending)
        return plan

    def test_strict_pending_identity_rejects_empty_session(self):
        pending = _capacity_pending()
        pending['candidate_session_id'] = ''
        plan = self._strict_plan(pending)
        self.assertFalse(dr._commit_continuity_shadow_receipt(plan, assistant_message_id=5))
        self.assertIsNone(rs.get(7, 2, 4))

    def test_strict_pending_identity_rejects_generation_mismatch(self):
        pending = _capacity_pending()
        pending['target_resident_generation'] = 5
        plan = self._strict_plan(pending)
        self.assertFalse(dr._commit_continuity_shadow_receipt(plan, assistant_message_id=5))
        self.assertIsNone(rs.get(7, 2, 4))

    def test_strict_pending_identity_rejects_source_context_mismatch(self):
        pending = _capacity_pending()
        pending['source_context_epoch'] = 99
        plan = self._strict_plan(pending)
        self.assertFalse(dr._commit_continuity_shadow_receipt(plan, assistant_message_id=5))
        self.assertIsNone(rs.get(7, 2, 4))

    def test_abort_clears_capacity_pending_error(self):
        plan = self._strict_plan(_capacity_pending())
        plan._continuity_shadow_capacity_pending_error = (
            'capacity_shadow_pending_build_failed'
        )
        with mock.patch.object(dr, 'is_epoch_token_current', return_value=False):
            dr.abort_daily_turn(
                plan,
                error_code='provider_failed',
                resident=types.SimpleNamespace(),
                respawn=False,
            )
        self.assertFalse(
            hasattr(plan, '_continuity_shadow_capacity_pending_error')
        )
