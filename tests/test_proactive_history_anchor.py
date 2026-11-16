"""Proactive cold start step: exclusion follows identity, history cut follows anchor."""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_context as dc
from chat import daily_history as dh
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    _init_chat_messages, _insert, _prepare_turn, _tmp_db,
)


class HistoryExcludeUpToSplitTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        _insert(self.db, 'hayana', 'q1', '2026-07-27 09:00:00')
        _insert(self.db, 'assistant', 'a1', '2026-07-27 09:01:00')
        self.latest = _insert(self.db, 'hayana', 'latest-real', '2026-07-27 09:59:00')
        plan = _prepare_turn(self.db, self.latest)
        self.ctx = dc.get_daily_context_by_id(plan.context_id, db_path=self.db)
        dr._release_lease(plan)

    def tearDown(self):
        os.unlink(self.db)

    def _ids(self, **kw):
        out = dh.build_daily_window_context(
            chat_id=self.ctx['chat_id'], daily_context=self.ctx,
            is_cold=True, db_path=self.db, **kw)
        return [m['message_id'] for m in out['current_day_history']]

    def test_chat_excludes_its_own_request(self):
        self.assertNotIn(self.latest, self._ids(current_user_message_id=self.latest))

    def test_proactive_anchor_includes_latest_real_message(self):
        ids = self._ids(current_user_message_id=None,
                        history_anchor_message_id=self.latest)
        self.assertIn(self.latest, ids)

    def test_anchor_still_bounds_history(self):
        later = _insert(self.db, 'assistant', 'after-anchor', '2026-07-27 10:05:00')
        ids = self._ids(current_user_message_id=None,
                        history_anchor_message_id=self.latest)
        self.assertNotIn(later, ids)


if __name__ == '__main__':
    unittest.main()
