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
from chat.planner_state_view import (
    BEHAVIOR_DECISION_MODES,
    PlannerStateView,
    PlannerStateViewUnavailable,
    b1_1a_v_plumbing_eligible,
    freeze_planner_state_view,
)
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


def _make_view(**drive_overrides) -> PlannerStateView:
    drives = {
        'attachment': 0.5, 'curiosity': 0.4, 'reflection': 0.3, 'social': 0.2,
        'duty': 0.2, 'libido': 0.0, 'stress': 0.0, 'fatigue': 0.1,
    }
    drives.update(drive_overrides)
    return PlannerStateView(
        state_version=1,
        observed_at='2026-08-04 15:00:00',
        wake_run_id='unit',
        affect={'pa': 0.5, 'na': 0.2, 'valence': 0.6, 'arousal': 0.3, 'mood_word': '平静'},
        bond={'intimacy': 0.3, 'passion': 0.0, 'commitment': 0.7},
        drives=drives,
        longing_derived=0.1,
        interaction_clock={
            'user_idle_hours': 3.0,
            'effective_idle_hours': 3.0,
            'reliable': True,
            'reason': 'ok',
        },
        immutable=True,
    )


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
        opened = []
        real_open = __import__(
            'internal_state_store', fromlist=['open_store']
        ).open_store

        def tracking_open(db_path, busy_timeout_ms=5000):
            conn = real_open(db_path, busy_timeout_ms=busy_timeout_ms)
            opened.append(conn)
            return conn

        with mock.patch('internal_state_store.open_store', side_effect=tracking_open):
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
        self.assertAlmostEqual(view.user_idle_hours, 3.0, places=3)
        self.assertTrue(view.clock_reliable)
        self.assertGreater(float(view.longing_derived), 0.0)
        self.assertEqual(len(opened), 1)
        # Connection must be closed after freeze (no live txn carried).
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

        with self.assertRaises(TypeError):
            view.drives['attachment'] = 0.99  # type: ignore[index]

    def test_exact_zero_drives_preserved_for_legacy_decide(self):
        """A1: legitimate 0.0 must not become 0.1 via `x or 0.1`."""
        view = _make_view(libido=0.0, stress=0.0)
        engine = view.drives_for_engine()
        self.assertEqual(engine['libido'], 0.0)
        self.assertEqual(engine['stress'], 0.0)
        with mock.patch.object(de, 'get_drive', side_effect=AssertionError('get_drive')):
            decision = de.decide(drive=engine)
        self.assertEqual(decision['drive']['libido'], 0.0)
        self.assertEqual(decision['drive']['stress'], 0.0)

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
        for key in view.drives:
            self.assertEqual(decision['drive'][key], view.drives[key])
        self.assertEqual(prov['source'], 'drive_engine.decide')
        self.assertIn('内在需求', snip)

    def test_inject_snippets_uses_v_for_decision_and_longing(self):
        from wake.builder import inject_snippets
        import desire as real_desire

        view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11a-inject-1',
        )
        with mock.patch.object(de, 'get_drive', side_effect=AssertionError('get_drive')):
            with mock.patch.object(
                real_desire, 'get_longing_wake_fact',
                wraps=real_desire.get_longing_wake_fact,
            ) as longing_fact:
                system, prov = inject_snippets(
                    'base',
                    'normal',
                    desire_driven=True,
                    longing_enabled=True,
                    planner_state_view=view,
                )
        self.assertIsNotNone(prov)
        self.assertIn('内在需求', system)
        longing_fact.assert_called_once_with(t_hours_override=view.user_idle_hours)

    def test_snapshot_unavailable_fail_closed_no_synthetic_v(self):
        """A3: cutover/missing S ⇒ raise; never invent default persona V."""
        with mock.patch(
            'chat.affect_bond_authority.check_cutover_ready_on_conn',
            return_value=type('R', (), {'ok': False, 'status': 'proof_gap', 'error': 'x'})(),
        ):
            with self.assertRaises(PlannerStateViewUnavailable) as ctx:
                freeze_planner_state_view(
                    db_path=self.db_path,
                    observed_at=T_OBS,
                    wake_run_id='b11a-fail',
                )
        self.assertIn('cutover_not_ready', ctx.exception.reason)

    def test_b8_scope_live_only_not_dry_run_or_inspect(self):
        """A2: V plumbing only for live Behavior-Decision attempts."""
        self.assertTrue(b1_1a_v_plumbing_eligible(mode='normal', live=True))
        self.assertFalse(b1_1a_v_plumbing_eligible(mode='normal', live=False))
        self.assertFalse(b1_1a_v_plumbing_eligible(mode='dream', live=True))
        self.assertFalse(b1_1a_v_plumbing_eligible(mode='summarize', live=True))

        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        inspect = src.split('def _wake_inspect_only', 1)[1].split(
            '\ndef wake_decide', 1,
        )[0]
        self.assertNotIn('freeze_planner_state_view', inspect)
        self.assertIn('planner_state_view=None', inspect)

        locked = src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('b1_1a_v_plumbing_eligible', locked)
        self.assertIn('PlannerStateViewUnavailable', locked)
        self.assertIn("'reason': 'planner_state_view_unavailable'", locked)
        self.assertLess(
            locked.index('wake_guard_reason'),
            locked.index('freeze_planner_state_view'),
        )

    def test_recall_photo_path_no_post_freeze_get_drive(self):
        """Static: gateway recall_photo binds V.attachment."""
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
        self.assertNotIn('_de_flush.get_drive', locked)

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
