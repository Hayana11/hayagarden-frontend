"""Commit-1 refactor guard: history anchor split must not change Chat output."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_context as dc
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    _init_chat_messages, _insert, _prepare_turn, _tmp_db,
)

_VOLATILE = {
    'request_id', 'lease_owner', 'turn_lease', 'created_at', 'worker_id_hash',
}


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in sorted(value.items()) if k not in _VOLATILE}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _snapshot(db, plan):
    conn = sqlite3.connect(db)
    lease = conn.execute(
        'SELECT request_message_id, resident_generation FROM daily_resident_turn_leases'
    ).fetchall()
    ctxs = conn.execute(
        'SELECT message_id, role FROM daily_message_contexts ORDER BY message_id'
    ).fetchall()
    conn.close()
    return json.loads(json.dumps(_scrub({
        'manifest': plan.manifest,
        'assembly': plan.assembly,
        'is_cold': plan.is_cold,
        'cursor_before': plan.cursor_before,
        'lease': lease,
        'contexts': ctxs,
    }), ensure_ascii=False, default=str))


def _run_chat_turn():
    dr.reset_bindings_for_tests()
    db = _tmp_db()
    _init_chat_messages(db)
    _insert(db, 'hayana', 'earlier question', '2026-07-27 09:00:00')
    _insert(db, 'assistant', 'earlier answer', '2026-07-27 09:01:00')
    uid = _insert(db, 'hayana', 'now', '2026-07-27 09:59:00')
    try:
        plan = _prepare_turn(db, uid, request_id='fixed-req', lease_owner='fixed-owner')
        snap = _snapshot(db, plan)
        dr._release_lease(plan)
        return uid, plan, snap
    finally:
        os.unlink(db)


GOLDEN_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'history_anchor_chat_golden.json')


class HistoryAnchorChatEquivalenceTests(unittest.TestCase):
    def test_chat_plan_anchor_equals_user_message_id(self):
        uid, plan, _snap = _run_chat_turn()
        self.assertEqual(plan.user_message_id, uid)
        self.assertEqual(plan.history_anchor_message_id, plan.user_message_id)

    def test_chat_plan_output_matches_pre_refactor_golden(self):
        _uid, _plan, snap = _run_chat_turn()
        if os.environ.get('CAPTURE_ANCHOR_GOLDEN'):
            os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
            with open(GOLDEN_PATH, 'w', encoding='utf-8') as fh:
                json.dump(snap, fh, ensure_ascii=False, indent=1, sort_keys=True)
            self.skipTest('golden captured')
        with open(GOLDEN_PATH, encoding='utf-8') as fh:
            self.assertEqual(json.load(fh), snap)


class RequestIdentityGuardTests(unittest.TestCase):
    def _plan(self, **kw):
        return dr.DailyTurnPlan(
            request_id='r', chat_id='c', local_day='2026-07-27', context_id=1,
            context_epoch=1, resident_generation=1, resident_key='k',
            epoch_token={}, lease_owner='o', is_cold=True, is_respawn=False,
            cursor_before=None, assembly={}, manifest={}, **kw,
        )

    def test_require_request_message_id_raises_for_none(self):
        plan = self._plan(user_message_id=None, history_anchor_message_id=9)
        with self.assertRaises(dr.DailyRuntimeError) as ctx:
            dr._require_request_message_id(plan)
        self.assertEqual(ctx.exception.error_code, 'request_message_id_missing')

    def test_require_request_message_id_returns_int(self):
        self.assertEqual(
            dr._require_request_message_id(self._plan(user_message_id=7)), 7)

    def test_history_anchor_prefers_explicit_then_falls_back(self):
        self.assertEqual(dr._history_anchor(self._plan(
            user_message_id=7, history_anchor_message_id=5)), 5)
        self.assertEqual(dr._history_anchor(self._plan(user_message_id=7)), 7)
        self.assertEqual(dr._history_anchor(self._plan(
            user_message_id=None, history_anchor_message_id=5)), 5)

    def test_window_context_accepts_optional_anchor(self):
        import inspect
        from chat import daily_history as dh
        param = inspect.signature(dh.build_daily_window_context).parameters[
            'history_anchor_message_id']
        self.assertIsNone(param.default)


if __name__ == '__main__':
    unittest.main()
