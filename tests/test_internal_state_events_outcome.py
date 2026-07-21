"""Phase 1A-3 — apply_outcome / plan_outcome_transition（仅临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway / wake。
旧常量经 AST + literal_eval 读取，避免与源码漂移。
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state_events as events
import internal_state_store as store


T0 = '2026-07-21 12:00:00'
T_1H = '2026-07-21 13:00:00'
T_6H = '2026-07-21 18:00:00'
T_12H = '2026-07-22 00:00:00'


def _load_drive_engine_constants() -> dict:
    src = Path(ROOT, 'drive_engine.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    wanted = ('DISCHARGE', 'FATIGUE_COST')
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                out[target.id] = ast.literal_eval(node.value)
    missing = set(wanted) - set(out)
    if missing:
        raise AssertionError(f'missing constants in drive_engine.py: {missing}')
    return out


def _load_desire_constants() -> dict:
    src = Path(ROOT, 'desire.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    wanted = ('ACTION_SATISFY', 'FATIGUE_COST')
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                out[target.id] = ast.literal_eval(node.value)
    missing = set(wanted) - set(out)
    if missing:
        raise AssertionError(f'missing constants in desire.py: {missing}')
    return out


_DRIVE_CONST = _load_drive_engine_constants()
_DESIRE_CONST = _load_desire_constants()
FIXED = {
    k: v for k, v in _DRIVE_CONST['DISCHARGE'].items() if k != 'fatigue'
}
FIXED_FATIGUE = _DRIVE_CONST['FATIGUE_COST']
DESIRE_RATIO = _DESIRE_CONST['ACTION_SATISFY']
DESIRE_FATIGUE = _DESIRE_CONST['FATIGUE_COST']


def _snapshot(**overrides):
    base = SimpleNamespace(
        observed_at=T0,
        affect=SimpleNamespace(
            pa=0.55, na=0.25, valence=0.6, arousal=0.4, mood_word='平静'),
        bond=SimpleNamespace(intimacy=0.50, passion=0.60, commitment=0.70),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.80, curiosity=0.70, reflection=0.65, social=0.60,
            duty=0.55, libido=0.75, stress=0.50, fatigue=0.30),
        diagnostics=SimpleNamespace(source_timestamps={}),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _outcome_kwargs(**overrides):
    base = dict(
        wake_run_id='wake-001',
        executor_action='explore',
        desire_action='github',
        fired_drive='curiosity',
        desire_driven=False,
        longing_for_boost=0.0,
        outcome_at=T0,
    )
    base.update(overrides)
    return base


class OutcomeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'outcome.db')
        self.conn = store.open_store(self.db_path)
        store.bootstrap_from_snapshot(self.conn, _snapshot())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def state(self):
        return store.read_state(self.conn)

    def plan(self, **overrides):
        return events.plan_outcome_transition(
            self.state(), **_outcome_kwargs(**overrides))


class LegacyConstantDriftTests(unittest.TestCase):
    def test_events_fixed_matches_drive_engine_ast(self):
        self.assertEqual(FIXED_FATIGUE, 0.04)
        self.assertEqual(
            events._LEGACY_FIXED_DISCHARGE,  # noqa: SLF001
            FIXED,
        )
        self.assertEqual(
            events._LEGACY_FATIGUE_BEHAVIOR_COST,  # noqa: SLF001
            FIXED_FATIGUE,
        )

    def test_events_desire_ratio_matches_desire_ast(self):
        self.assertEqual(DESIRE_FATIGUE, 0.08)
        self.assertEqual(
            events._LEGACY_DESIRE_ACTION_SATISFY,  # noqa: SLF001
            DESIRE_RATIO,
        )
        self.assertEqual(
            events._LEGACY_DESIRE_FATIGUE_COST,  # noqa: SLF001
            DESIRE_FATIGUE,
        )


class FixedDischargeTests(OutcomeBase):
    def test_seven_fired_drives_fixed_subtraction(self):
        for drive, sub in FIXED.items():
            with self.subTest(drive=drive):
                st = self.state()
                base = st[drive]
                plan = self.plan(
                    wake_run_id=f'w-{drive}',
                    executor_action='message',
                    desire_action=None,
                    fired_drive=drive,
                    desire_driven=False,
                    outcome_at=T0,
                )
                self.assertAlmostEqual(
                    plan['updates'][drive],
                    round(max(0.0, base - sub), 4),
                    places=4,
                )
                for other in FIXED:
                    if other == drive:
                        continue
                    self.assertAlmostEqual(
                        plan['updates'][other], st[other], places=4)

    def test_attachment_strictly_subtracts_fixed(self):
        plan = self.plan(
            executor_action='message',
            desire_action=None,
            fired_drive='attachment',
            desire_driven=False,
            outcome_at=T0,
        )
        expect = round(max(0.0, 0.80 - FIXED['attachment']), 4)
        self.assertAlmostEqual(plan['updates']['attachment'], expect, places=4)
        self.assertAlmostEqual(
            plan['diagnostics']['candidate_attachment_ratio_after'],
            round(0.80 * 0.45, 4),
        )
        self.assertNotAlmostEqual(
            plan['updates']['attachment'],
            plan['diagnostics']['candidate_attachment_ratio_after'],
        )

    def test_action_none_only_restores_fatigue(self):
        plan = self.plan(
            executor_action='none',
            desire_action='none',
            fired_drive=None,
            desire_driven=False,
            outcome_at=T0,
        )
        st = self.state()
        for k in FIXED:
            self.assertAlmostEqual(plan['updates'][k], st[k], places=4)
        self.assertAlmostEqual(
            plan['updates']['fatigue'],
            round(max(0.0, 0.30 - FIXED_FATIGUE), 4),
            places=4,
        )

    def test_none_with_fired_drive_duty_still_restores_only(self):
        """desire duty→none 合法；固定路径仍按 none/rest，不扣 duty。"""
        plan = self.plan(
            executor_action='none',
            desire_action='none',
            fired_drive='duty',
            desire_driven=True,
            outcome_at=T0,
        )
        self.assertAlmostEqual(plan['updates']['duty'], 0.55, places=4)
        self.assertAlmostEqual(
            plan['updates']['fatigue'],
            round(max(0.0, 0.30 - FIXED_FATIGUE), 4),
            places=4,
        )
        # desire ratio 诊断仍可按 none 扣 duty
        ratio = plan['diagnostics']['legacy_desire_ratio_after']
        self.assertAlmostEqual(
            ratio['duty'],
            round(0.55 * DESIRE_RATIO['none']['duty'], 4),
            places=4,
        )

    def test_non_none_fatigue_only_plus_fixed_cost(self):
        plan = self.plan(
            executor_action='explore',
            desire_action='github',
            fired_drive='curiosity',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(
            plan['updates']['fatigue'],
            round(min(1.0, 0.30 + FIXED_FATIGUE), 4),
            places=4,
        )
        self.assertAlmostEqual(
            plan['diagnostics']['legacy_desire_ratio_after']['fatigue'],
            round(min(1.0, 0.30 + DESIRE_FATIGUE), 4),
            places=4,
        )

    def test_does_not_re_infer_highest_drive(self):
        store.apply_state_update(
            self.conn,
            event_key='noise:bump',
            event_type='noise',
            source_id='bump',
            payload={'n': 1},
            mutator=lambda s: {'curiosity': 0.95, 'attachment': 0.40},
        )
        plan = self.plan(
            wake_run_id='no-guess',
            executor_action='explore',
            desire_action='github',
            fired_drive='attachment',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(
            plan['updates']['attachment'],
            round(max(0.0, 0.40 - FIXED['attachment']), 4),
            places=4,
        )
        self.assertAlmostEqual(plan['updates']['curiosity'], 0.95, places=4)

    def test_non_none_without_fired_drive_rejected(self):
        with self.assertRaises(store.StoreError) as ctx:
            events.apply_outcome(
                self.conn,
                **_outcome_kwargs(
                    wake_run_id='no-fire',
                    executor_action='message',
                    fired_drive=None,
                ),
            )
        self.assertIn('fired_drive is required', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'wake_outcome:no-fire'))
        self.assertEqual(self.state()['state_version'], 0)


class LongingMaterializeTests(OutcomeBase):
    def test_different_longing_changes_attachment_materialized(self):
        plan0 = self.plan(
            wake_run_id='l0',
            longing_for_boost=0.0,
            outcome_at=T_12H,
            executor_action='message',
            desire_action=None,
            fired_drive='attachment',
        )
        plan_hi = self.plan(
            wake_run_id='l1',
            longing_for_boost=0.8,
            outcome_at=T_12H,
            executor_action='message',
            desire_action=None,
            fired_drive='attachment',
        )
        att0 = plan0['diagnostics']['materialized_before']['attachment']
        att1 = plan_hi['diagnostics']['materialized_before']['attachment']
        self.assertNotAlmostEqual(att0, att1, places=4)
        self.assertGreater(att1, att0)
        # fixed / candidate 均从提高后的 attachment 起算
        self.assertAlmostEqual(
            plan_hi['updates']['attachment'],
            round(max(0.0, att1 - FIXED['attachment']), 4),
            places=4,
        )
        self.assertAlmostEqual(
            plan_hi['diagnostics']['candidate_attachment_ratio_after'],
            round(att1 * 0.45, 4),
            places=4,
        )

    def test_illegal_longing_does_not_consume_key(self):
        for bad in (None, True, '0.5', float('nan'), -0.1, 1.1):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'lg-{id(bad)}',
                            longing_for_boost=bad,  # type: ignore[arg-type]
                        ),
                    )
                self.assertIsNone(
                    store.read_event(
                        self.conn, f'wake_outcome:lg-{id(bad)}'))
        self.assertEqual(self.state()['state_version'], 0)


class MaterializeThenSettleTests(OutcomeBase):
    def test_materialize_to_outcome_at_before_settlement(self):
        store.apply_state_update(
            self.conn,
            event_key='noise:lower',
            event_type='noise',
            source_id='lower',
            payload={'n': 1},
            mutator=lambda s: {'curiosity': 0.40, 'duty': 0.40},
        )
        plan = self.plan(
            wake_run_id='mat-1',
            executor_action='explore',
            desire_action='github',
            fired_drive='curiosity',
            desire_driven=False,
            longing_for_boost=0.0,
            outcome_at=T_6H,
        )
        mat = plan['diagnostics']['materialized_before']
        self.assertGreater(mat['curiosity'], 0.40)
        self.assertGreater(mat['duty'], 0.40)
        expected = round(max(0.0, mat['curiosity'] - FIXED['curiosity']), 4)
        self.assertAlmostEqual(plan['updates']['curiosity'], expected, places=4)
        self.assertAlmostEqual(plan['updates']['duty'], mat['duty'], places=4)
        self.assertEqual(plan['updates']['drives_updated_at'], T_6H)

    def test_drives_updated_at_advances_on_apply(self):
        r = events.apply_outcome(
            self.conn, **_outcome_kwargs(outcome_at=T_1H))
        self.assertEqual(r.status, 'applied')
        st = self.state()
        self.assertEqual(st['drives_updated_at'], T_1H)
        self.assertEqual(st['state_version'], 1)

    def test_affect_bond_watermark_untouched(self):
        before = self.state()
        events.apply_outcome(
            self.conn,
            **_outcome_kwargs(
                wake_run_id='untouch',
                executor_action='message',
                desire_action='vent',
                fired_drive='stress',
                outcome_at=T_1H,
            ),
        )
        after = self.state()
        for key in (
            'pa', 'na', 'valence', 'arousal', 'mood_word',
            'mood_source_message_id',
            'intimacy', 'passion', 'commitment',
            'p_updated_at', 'i_updated_at',
            'last_scored_message_id',
        ):
            self.assertEqual(after[key], before[key], msg=key)


class DesireDiagnosticsTests(OutcomeBase):
    def test_desire_ratio_per_action(self):
        for action, ratios in DESIRE_RATIO.items():
            with self.subTest(action=action):
                plan = self.plan(
                    wake_run_id=f'ratio-{action}',
                    executor_action='none' if action == 'none' else 'explore',
                    desire_action=action,
                    fired_drive=None if action == 'none' else 'curiosity',
                    desire_driven=False,
                    outcome_at=T0,
                )
                ratio_after = plan['diagnostics']['legacy_desire_ratio_after']
                mat = plan['diagnostics']['materialized_before']
                for key, ratio in ratios.items():
                    self.assertAlmostEqual(
                        ratio_after[key],
                        round(max(0.0, mat[key] * ratio), 4),
                        places=4,
                    )
                self.assertAlmostEqual(
                    ratio_after['fatigue'],
                    round(min(1.0, mat['fatigue'] + DESIRE_FATIGUE), 4),
                    places=4,
                )

    def test_desire_driven_live_double_diagnostics_only(self):
        plan = self.plan(
            executor_action='explore',
            desire_action='github',
            fired_drive='curiosity',
            desire_driven=True,
            outcome_at=T0,
        )
        fixed = plan['diagnostics']['legacy_fixed_after']
        double = plan['diagnostics']['legacy_live_double_after']
        self.assertIsNotNone(double)
        expect_cur = round(max(0.0, 0.70 - FIXED['curiosity']), 4)
        self.assertAlmostEqual(fixed['curiosity'], expect_cur, places=4)
        self.assertAlmostEqual(
            double['curiosity'],
            round(expect_cur * DESIRE_RATIO['github']['curiosity'], 4),
            places=4,
        )
        self.assertEqual(plan['updates']['curiosity'], fixed['curiosity'])
        self.assertNotEqual(plan['updates']['curiosity'], double['curiosity'])

        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(desire_driven=True, outcome_at=T0),
        )
        self.assertEqual(r.status, 'applied')
        st = self.state()
        self.assertAlmostEqual(st['curiosity'], expect_cur, places=4)

    def test_desire_driven_false_double_is_none(self):
        plan = self.plan(desire_driven=False)
        self.assertIsNone(plan['diagnostics']['legacy_live_double_after'])

    def test_explore_without_desire_action_ratio_is_fatigue_only(self):
        """executor explore 不在 desire 表：ratio 只加 fatigue。"""
        plan = self.plan(
            executor_action='explore',
            desire_action=None,
            fired_drive='curiosity',
            desire_driven=False,
            outcome_at=T0,
        )
        ratio = plan['diagnostics']['legacy_desire_ratio_after']
        mat = plan['diagnostics']['materialized_before']
        for k in FIXED:
            self.assertAlmostEqual(ratio[k], mat[k], places=4)
        self.assertAlmostEqual(
            ratio['fatigue'],
            round(min(1.0, mat['fatigue'] + DESIRE_FATIGUE), 4),
            places=4,
        )


class ResultJsonAuditTests(OutcomeBase):
    def test_apply_persists_full_diagnostics_in_result_json(self):
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='audit-1', desire_driven=True),
        )
        self.assertEqual(r.status, 'applied')
        self.assertIsNotNone(r.result)
        ev = store.read_event(self.conn, 'wake_outcome:audit-1')
        stored = json.loads(ev['result_json'])
        self.assertEqual(stored, r.result)
        for key in (
            'materialized_before',
            'legacy_fixed_after',
            'legacy_desire_ratio_after',
            'legacy_live_double_after',
            'candidate_attachment_ratio_after',
        ):
            self.assertIn(key, stored)
        # payload 仍只有稳定输入
        payload = json.loads(ev['payload_json'])
        self.assertEqual(
            set(payload.keys()),
            {
                'wake_run_id', 'executor_action', 'desire_action',
                'fired_drive', 'desire_driven', 'longing_for_boost',
                'outcome_at',
            },
        )
        self.assertNotIn('materialized_before', ev['payload_json'])

    def test_diagnostics_share_materialized_before_with_updates(self):
        r = events.apply_outcome(
            self.conn, **_outcome_kwargs(wake_run_id='same-mat'))
        diag = r.result
        assert diag is not None
        mat = diag['materialized_before']
        fixed = diag['legacy_fixed_after']
        st = self.state()
        expect = round(max(0.0, mat['curiosity'] - FIXED['curiosity']), 4)
        self.assertAlmostEqual(fixed['curiosity'], expect, places=4)
        self.assertAlmostEqual(st['curiosity'], expect, places=4)
        self.assertAlmostEqual(st['fatigue'], fixed['fatigue'], places=4)

    def test_duplicate_replays_result_without_recompute_overwrite(self):
        kwargs = _outcome_kwargs(wake_run_id='dup-audit', desire_driven=True)
        r1 = events.apply_outcome(self.conn, **kwargs)
        first_raw = store.read_event(
            self.conn, 'wake_outcome:dup-audit')['result_json']
        # 污染状态后 duplicate 不得改写 result_json
        store.apply_state_update(
            self.conn,
            event_key='noise:pollute',
            event_type='noise',
            source_id='p',
            payload={'n': 1},
            mutator=lambda s: {'curiosity': 0.99, 'attachment': 0.99},
        )
        r2 = events.apply_outcome(self.conn, **kwargs)
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(r2.result, r1.result)
        second_raw = store.read_event(
            self.conn, 'wake_outcome:dup-audit')['result_json']
        self.assertEqual(second_raw, first_raw)
        # 状态保持污染后的值（duplicate 不结算）
        self.assertAlmostEqual(self.state()['curiosity'], 0.99, places=4)

    def test_concurrent_applied_duplicate_single_audit_result(self):
        db_path = self.db_path
        results: dict[str, store.ApplyResult] = {}
        errors: list[BaseException] = []

        def run(label: str):
            conn = store.open_store(db_path)
            try:
                results[label] = events.apply_outcome(
                    conn,
                    **_outcome_kwargs(wake_run_id='concurrent-audit'),
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=run, args=('a',))
        t2 = threading.Thread(target=run, args=('b',))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertEqual(errors, [])
        statuses = {results['a'].status, results['b'].status}
        self.assertEqual(statuses, {'applied', 'duplicate'})
        applied = results['a'] if results['a'].status == 'applied' else results['b']
        dup = results['b'] if results['a'].status == 'applied' else results['a']
        self.assertEqual(dup.result, applied.result)
        rows = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT result_json) "
            "FROM internal_state_events "
            "WHERE event_key='wake_outcome:concurrent-audit'"
        ).fetchone()
        self.assertEqual(rows[0], 1)
        self.assertEqual(rows[1], 1)


class IdempotencyTests(OutcomeBase):
    def test_same_wake_run_id_retry_duplicate(self):
        kwargs = _outcome_kwargs(wake_run_id='dup-1')
        r1 = events.apply_outcome(self.conn, **kwargs)
        r2 = events.apply_outcome(self.conn, **kwargs)
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(self.state()['state_version'], 1)

    def test_same_key_different_executor_action_conflict(self):
        events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c1', executor_action='explore'),
        )
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c1', executor_action='message'),
        )
        self.assertEqual(r.status, 'idempotency_conflict')

    def test_same_key_different_fired_drive_conflict(self):
        events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c2', fired_drive='curiosity'),
        )
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c2', fired_drive='stress'),
        )
        self.assertEqual(r.status, 'idempotency_conflict')

    def test_same_key_different_longing_conflict(self):
        events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c3', longing_for_boost=0.0),
        )
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='c3', longing_for_boost=0.5),
        )
        self.assertEqual(r.status, 'idempotency_conflict')

    def test_version_conflict_does_not_consume_key(self):
        store.apply_state_update(
            self.conn,
            event_key='noise:1',
            event_type='noise',
            source_id='1',
            payload={'n': 1},
            mutator=lambda s: {'pa': 0.61},
            expected_state_version=0,
        )
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='vc-1'),
            expected_state_version=0,
        )
        self.assertEqual(r.status, 'version_conflict')
        self.assertIsNone(store.read_event(self.conn, 'wake_outcome:vc-1'))
        r2 = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='vc-1'),
            expected_state_version=self.state()['state_version'],
        )
        self.assertEqual(r2.status, 'applied')
        self.assertIsNotNone(r2.result)


class ConcurrencyTests(OutcomeBase):
    def test_two_connections_same_wake_run_id_settle_once(self):
        db_path = self.db_path
        results: dict[str, store.ApplyResult] = {}
        errors: list[BaseException] = []

        def run(label: str):
            conn = store.open_store(db_path)
            try:
                results[label] = events.apply_outcome(
                    conn,
                    **_outcome_kwargs(wake_run_id='concurrent-1'),
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=run, args=('a',))
        t2 = threading.Thread(target=run, args=('b',))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertEqual(errors, [])
        statuses = {results['a'].status, results['b'].status}
        self.assertEqual(statuses, {'applied', 'duplicate'})
        st = self.state()
        self.assertEqual(st['state_version'], 1)
        self.assertAlmostEqual(
            st['curiosity'],
            round(max(0.0, 0.70 - FIXED['curiosity']), 4),
            places=4,
        )


class ValidationTests(OutcomeBase):
    def test_illegal_wake_run_id_rejected(self):
        cases = [
            '', '   ', True, False, 1, 1.5, b'wake', ['x'], {'a': 1},
            'x' * 129,
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(wake_run_id=bad),  # type: ignore[arg-type]
                    )
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_executor_action_rejected(self):
        for bad in ('', 'github', 'co_read', 'MESSAGE', True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'ea-{id(bad)}',
                            executor_action=bad,  # type: ignore[arg-type]
                        ),
                    )
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_desire_action_rejected(self):
        for bad in ('explore', 'message', 'GITHUB', True, 1):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'da-{id(bad)}',
                            desire_action=bad,  # type: ignore[arg-type]
                        ),
                    )
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_fired_drive_rejected(self):
        for bad in ('fatigue', 'unknown', '', True, 1, 'Attachment'):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'fd-{id(bad)}',
                            fired_drive=bad,  # type: ignore[arg-type]
                        ),
                    )
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_desire_driven_rejected(self):
        for bad in (0, 1, 'true', 'false', None, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'dd-{id(bad)}',
                            desire_driven=bad,  # type: ignore[arg-type]
                        ),
                    )
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_and_rewound_outcome_at_do_not_consume_key(self):
        with self.assertRaises(store.StoreError):
            events.apply_outcome(
                self.conn,
                **_outcome_kwargs(
                    wake_run_id='bad-ts', outcome_at='坏时间'))
        self.assertIsNone(store.read_event(self.conn, 'wake_outcome:bad-ts'))

        with self.assertRaises(store.StoreError):
            events.apply_outcome(
                self.conn,
                **_outcome_kwargs(
                    wake_run_id='micro',
                    outcome_at=T0 + '.100000',
                ),
            )
        self.assertIsNone(store.read_event(self.conn, 'wake_outcome:micro'))

        with self.assertRaises(store.StoreError) as ctx:
            events.apply_outcome(
                self.conn,
                **_outcome_kwargs(
                    wake_run_id='rewind',
                    outcome_at='2026-07-21 11:00:00',
                ),
            )
        self.assertIn('refusing to rewind', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'wake_outcome:rewind'))

        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='rewind', outcome_at=T0),
        )
        self.assertEqual(r.status, 'applied')

    def test_outcome_at_dot_zero_normalized(self):
        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(
                wake_run_id='norm',
                outcome_at=T0 + '.000000',
            ),
        )
        self.assertEqual(r.status, 'applied')
        payload = json.loads(
            store.read_event(self.conn, 'wake_outcome:norm')['payload_json'])
        self.assertEqual(payload['outcome_at'], T0)


class PayloadAndGuardTests(OutcomeBase):
    def test_payload_excludes_thoughts_and_content(self):
        events.apply_outcome(
            self.conn,
            **_outcome_kwargs(wake_run_id='pay-1', desire_driven=True),
        )
        raw = store.read_event(self.conn, 'wake_outcome:pay-1')['payload_json']
        for forbidden in (
            'thoughts', 'content', 'prompt', '正文', 'diagnostics',
            'materialized', 'assistant',
        ):
            self.assertNotIn(forbidden, raw)

    def test_three_settlement_examples_side_by_side(self):
        plan = self.plan(
            wake_run_id='demo',
            executor_action='explore',
            desire_action='github',
            fired_drive='curiosity',
            desire_driven=True,
            outcome_at=T0,
        )
        fixed_c = round(max(0.0, 0.70 - FIXED['curiosity']), 4)
        ratio_c = round(0.70 * DESIRE_RATIO['github']['curiosity'], 4)
        double_c = round(fixed_c * DESIRE_RATIO['github']['curiosity'], 4)
        self.assertEqual(
            plan['diagnostics']['legacy_fixed_after']['curiosity'], fixed_c)
        self.assertEqual(
            plan['diagnostics']['legacy_desire_ratio_after']['curiosity'],
            ratio_c,
        )
        self.assertEqual(
            plan['diagnostics']['legacy_live_double_after']['curiosity'],
            double_c,
        )
        self.assertEqual(plan['updates']['curiosity'], fixed_c)


class GuardTests(unittest.TestCase):
    def test_events_module_forbids_legacy_and_wake_imports(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {
            'emotion_engine', 'drive_engine', 'desire', 'gateway', 'app',
            'wake',
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split('.')[0])
        self.assertFalse(forbidden & found, msg=f'{forbidden & found}')

    def test_outcome_tests_do_not_import_legacy(self):
        src = Path(__file__).read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {
            'emotion_engine', 'drive_engine', 'desire', 'gateway', 'wake',
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split('.')[0])
        self.assertFalse(forbidden & found)

    def test_no_module_level_db_writes_in_events(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail(f'module-level call forbidden: line {node.lineno}')

    def test_temp_sqlite_only_in_this_suite(self):
        src = Path(__file__).read_text(encoding='utf-8')
        self.assertIn('TemporaryDirectory', src)
        self.assertIn('outcome.db', src)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name != 'open_store':
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith('/') or arg.value.endswith('.db'):
                        self.fail(
                            f'open_store must use tempfile path, got {arg.value!r}'
                        )


if __name__ == '__main__':
    unittest.main()
