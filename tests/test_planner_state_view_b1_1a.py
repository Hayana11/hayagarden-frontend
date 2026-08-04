"""B1-1A｜Decision-time comparison baseline freeze plumbing — minimum cases."""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

import drive_engine as de
import internal_state_shadow as shadow
import internal_state_store as store
from chat import drive_authority as da
from chat.planner_state_view import (
    BEHAVIOR_DECISION_MODES,
    freeze_planner_state_view,
)
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


class PlannerStateViewB11ATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        os.environ['MEMORIES_DB'] = self.db_path
        de.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        # Move last user message earlier so DecisionClock idle is non-zero.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 12:00:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_freeze_same_sqlite_snapshot_and_releases_conn(self):
        view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11a-snap-1',
        )
        self.assertTrue(view.immutable)
        self.assertEqual(view.wake_run_id, 'b11a-snap-1')
        self.assertEqual(view.observed_at, '2026-08-04 15:00:00')
        self.assertGreaterEqual(int(view.state_version), 0)
        self.assertIn('attachment', view.drives)
        self.assertIn('fatigue', view.drives)
        self.assertIn('intimacy', view.bond)
        self.assertIn('valence', view.affect)
        # 3h idle from 12:00 → 15:00 — proves DecisionClock came from snapshot.
        self.assertAlmostEqual(view.user_idle_hours, 3.0, places=3)
        self.assertAlmostEqual(view.effective_idle_hours, 3.0, places=3)
        self.assertTrue(view.clock_reliable)
        self.assertEqual(view.interaction_clock.get('reason'), 'ok')
        self.assertGreater(float(view.longing_derived), 0.0)
        # Not the all-0.1 fail-closed placeholder map.
        self.assertNotEqual(
            {k: float(view.drives[k]) for k in view.drives},
            {k: 0.1 for k in view.drives},
        )

        # MappingProxy / frozen: mutation must fail.
        with self.assertRaises(TypeError):
            view.drives['attachment'] = 0.99  # type: ignore[index]

    def test_legacy_decide_consumes_v_drives_without_get_drive(self):
        view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11a-decide-1',
        )
        with mock.patch.object(de, 'get_drive', side_effect=AssertionError('get_drive')):
            decision = de.decide(drive=view.drives_for_engine())
            prov = de.freeze_decision_provenance(decision)
            snip = de.get_wake_snippet(decision=decision)
        self.assertEqual(decision['drive']['attachment'], view.drives['attachment'])
        for key in view.drives:
            self.assertEqual(decision['drive'][key], view.drives[key])
        self.assertIn(prov['source'], ('drive_engine.decide',))
        self.assertIn('内在需求', snip)
        # Without drive arg, get_drive would be called — keep that path for
        # non-Wake callers, but Wake must pass V.drives.
        with mock.patch.object(de, 'get_drive', return_value=view.drives_for_engine()) as gd:
            de.decide()
            gd.assert_called_once()

    def test_inject_snippets_uses_v_for_decision_and_longing(self):
        from wake.builder import inject_snippets

        view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11a-inject-1',
        )
        with mock.patch.object(de, 'get_drive', side_effect=AssertionError('get_drive')):
            system, prov = inject_snippets(
                'base',
                'normal',
                desire_driven=True,
                longing_enabled=True,
                planner_state_view=view,
            )
        self.assertIsNotNone(prov)
        self.assertIn('内在需求', system)
        # Longing fact uses DecisionClock idle (3h) — not a second clock read.
        if 'Longing' in system:
            self.assertIn('距上次互动=3.0h', system)

    def test_recall_photo_path_no_post_freeze_get_drive(self):
        """Static + behavioral: gateway recall_photo binds V.attachment."""
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        build = src.split('def _wake_build_system_for_plan', 1)[1].split(
            '\ndef _wake_trigger_message', 1,
        )[0]
        self.assertIn('planner_state_view.attachment()', build)
        self.assertNotIn('drive_engine as _de_ph', build)
        recall_block = build.split('recall_photo nudge', 1)[1]
        self.assertIn('planner_state_view.attachment()', recall_block)
        self.assertNotIn('drive_engine', recall_block)
        self.assertNotIn('.get_drive(', recall_block)

        locked = src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('guard_clock', locked)
        self.assertIn('freeze_planner_state_view', locked)
        self.assertIn('decision_hours_from_view', locked)
        # Pre-build psychological get_drive for flush must be gone.
        self.assertNotIn('_de_flush.get_drive', locked)
        # GuardClock must not be the source name reused as Decision hours
        # after freeze_planner_state_view for behavior modes.
        self.assertLess(
            locked.index('wake_guard_reason'),
            locked.index('freeze_planner_state_view'),
        )
        self.assertLess(
            locked.index('freeze_planner_state_view'),
            locked.index('_wake_build_system_for_plan'),
        )

    def test_behavior_modes_include_comparison_eligible_set(self):
        self.assertEqual(
            BEHAVIOR_DECISION_MODES,
            frozenset({
                'normal', 'morning', 'nightwatch', 'ritual', 'self_trigger',
            }),
        )
        self.assertNotIn('dream', BEHAVIOR_DECISION_MODES)
        self.assertNotIn('summarize', BEHAVIOR_DECISION_MODES)


if __name__ == '__main__':
    unittest.main()
