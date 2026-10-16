"""Focused diagnostics tests for fail-closed Hot source scope proof."""
from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_runtime as dr


def _plan() -> dr.DailyTurnPlan:
    return dr.DailyTurnPlan(
        request_id='hot-diagnostic-request',
        chat_id='default',
        local_day='2026-10-16',
        context_id=32,
        context_epoch=30,
        resident_generation=65,
        resident_key='daily:default:30:65',
        user_message_id=11972,
        epoch_token={
            'context_id': 32,
            'context_epoch': 30,
            'resident_generation': 65,
            'chat_id': 'default',
        },
        lease_owner='worker',
        is_cold=False,
        is_respawn=False,
        cursor_before=11971,
        assembly={},
        manifest={'provider': 'claude_code'},
    )


def _member(source_ref: str, *, kind: str = 'raw') -> SimpleNamespace:
    return SimpleNamespace(
        representation_id=source_ref,
        representation_kind=kind,
        source_ref=source_ref,
        source_revision='revision-' + source_ref,
        source_kind='raw',
        content_hash='hash-' + source_ref,
        span_start=None,
        span_end=None,
        branch_id='',
    )


def _frozen(*members, **overrides):
    result = {
        'receipt': object(),
        'receipt_members_complete': True,
        'membership_valid': True,
        'receipt_read_error': None,
        'expected_receipt_revision': 0,
        'installed_source_watermark': 11971,
        'members': tuple(members),
    }
    result.update(overrides)
    return result


def _registry():
    return {
        'scan_status': 'READY',
        'process_generation': 1,
        'last_mapped_message_id': 11971,
        'claude_session_id': 'session-redacted-by-diagnostic',
    }


class HotScopeDiagnosticsTests(unittest.TestCase):
    def test_receipt_gate_reasons_are_retained(self):
        cases = (
            (_frozen(receipt=None), 'receipt_missing'),
            (_frozen(receipt=object(), receipt_members_complete=False),
             'receipt_members_incomplete'),
            (_frozen(receipt=object(), membership_valid=False),
             'receipt_membership_hash_mismatch'),
            (_frozen(receipt=None, receipt_read_error='OperationalError'),
             'receipt_read_error'),
        )
        for frozen, reason in cases:
            with self.subTest(reason=reason):
                plan = _plan()
                result = dr._hot_canonical_source_scope(
                    plan, resident=object(), frozen=frozen,
                )
                self.assertIsNone(result)
                self.assertEqual(plan.hot_scope_diagnostic['stage'], 'receipt_gate')
                self.assertEqual(plan.hot_scope_diagnostic['reason'], reason)

    def test_live_identity_decision_and_reason_are_retained(self):
        plan = _plan()
        frozen = _frozen()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('RESPAWN', 'live_tool_surface_identity_missing', None),
        ), mock.patch.object(dr, '_hot_native_tail_proof') as native_tail:
            result = dr._hot_canonical_source_scope(
                plan, resident=object(), frozen=frozen,
            )
        self.assertIsNone(result)
        native_tail.assert_not_called()
        diag = plan.hot_scope_diagnostic
        self.assertEqual(diag['stage'], 'live_identity')
        self.assertEqual(diag['decision'], 'RESPAWN')
        self.assertEqual(diag['reason'], 'live_tool_surface_identity_missing')
        self.assertEqual(diag['message_id'], 11972)

    def test_native_tail_reasons_include_last_good_failures(self):
        for reason in (
            'same_context_last_good_missing',
            'same_context_last_good_identity_mismatch',
        ):
            with self.subTest(reason=reason):
                plan = _plan()
                with mock.patch.object(
                    dr,
                    '_hot_live_identity_decision',
                    return_value=('PASS', '', _registry()),
                ), mock.patch.object(
                    dr,
                    '_hot_native_tail_proof',
                    return_value={'status': 'missing', 'reason': reason},
                ):
                    result = dr._hot_canonical_source_scope(
                        plan, resident=object(), frozen=_frozen(),
                    )
                self.assertIsNone(result)
                diag = plan.hot_scope_diagnostic
                self.assertEqual(diag['stage'], 'native_tail')
                self.assertEqual(diag['status'], 'missing')
                self.assertEqual(diag['reason'], reason)
                self.assertEqual(diag['last_good']['status'], 'failed')

    def test_tail_identity_missing_and_duplicate_are_distinguished(self):
        plan = _plan()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', _registry()),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={'status': 'pass', 'source_member': None},
        ):
            result = dr._hot_canonical_source_scope(
                plan, resident=object(), frozen=_frozen(_member('message:11970')),
            )
        self.assertIsNone(result)
        self.assertEqual(plan.hot_scope_diagnostic['stage'], 'tail_identity')
        self.assertEqual(
            plan.hot_scope_diagnostic['reason'],
            'native_tail_source_member_missing',
        )

        plan = _plan()
        installed = _member('message:11970')
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', _registry()),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={'status': 'pass', 'source_member': installed},
        ):
            result = dr._hot_canonical_source_scope(
                plan, resident=object(), frozen=_frozen(installed),
            )
        self.assertIsNone(result)
        self.assertEqual(plan.hot_scope_diagnostic['stage'], 'tail_identity')
        self.assertEqual(
            plan.hot_scope_diagnostic['reason'],
            'native_tail_source_identity_duplicate',
        )

    def test_exception_records_stage_and_not_exception_message(self):
        plan = _plan()
        secret = 'do-not-log-chat-or-token'
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            side_effect=RuntimeError(secret),
        ):
            result = dr._hot_canonical_source_scope(
                plan, resident=object(), frozen=_frozen(),
            )
        self.assertIsNone(result)
        diag = plan.hot_scope_diagnostic
        self.assertEqual(diag['stage'], 'exception')
        self.assertEqual(diag['exception_stage'], 'live_identity')
        self.assertEqual(diag['exception_type'], 'RuntimeError')
        self.assertNotIn(secret, json.dumps(diag, ensure_ascii=False))
        self.assertNotIn('session-redacted-by-diagnostic', json.dumps(diag))

    def test_success_and_prepare_failure_remain_fail_closed(self):
        installed = _member('message:11970')
        tail = _member('message:11971')
        plan = _plan()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', _registry()),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={'status': 'pass', 'source_member': tail},
        ):
            result = dr._hot_canonical_source_scope(
                plan, resident=object(), frozen=_frozen(installed),
            )
        self.assertEqual(len(result), 2)
        self.assertIsNone(plan.hot_scope_diagnostic)

        plan = _plan()
        frozen = _frozen(installed)
        with mock.patch.object(dr, '_freeze_hot_receipt', return_value=frozen),              mock.patch.object(
                 dr,
                 '_hot_live_identity_decision',
                 return_value=('RESPAWN', 'session_registry_missing', None),
             ), mock.patch.object(
                 dr,
                 '_build_production_context_plan',
             ) as build_plan:
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._prepare_hot_context_plan(
                    plan, resident=object(), static_system='STATIC',
                )
        self.assertEqual(
            raised.exception.error_code,
            'context_plan_hot_source_scope_unavailable',
        )
        self.assertEqual(plan.hot_decision, 'BLOCKED')
        self.assertEqual(plan.hot_decision_reason, 'hot_source_scope_unproven')
        self.assertEqual(
            raised.exception.hot_scope_diagnostic['reason'],
            'session_registry_missing',
        )
        build_plan.assert_not_called()


if __name__ == '__main__':
    unittest.main()
