"""Phase 1A-3 — apply_outcome / plan_outcome_transition（仅临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway / wake。
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

FIXED = {
    'attachment': 0.55,
    'curiosity': 0.45,
    'reflection': 0.40,
    'social': 0.40,
    'duty': 0.50,
    'libido': 0.60,
    'stress': 0.50,
}

DESIRE_RATIO = {
    'co_read': {'reflection': 0.45, 'curiosity': 0.85},
    'github': {'curiosity': 0.50},
    'web_search': {'curiosity': 0.48},
    'web_browse': {'social': 0.48, 'curiosity': 0.82},
    'none': {'duty': 0.80},
    'tease': {'libido': 0.55},
    'vent': {'stress': 0.45},
}


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
        action='github',
        fired_drive='curiosity',
        desire_driven=False,
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
        return events.plan_outcome_transition(self.state(), **_outcome_kwargs(**overrides))


class FixedDischargeTests(OutcomeBase):
    def test_seven_fired_drives_fixed_subtraction(self):
        for drive, sub in FIXED.items():
            with self.subTest(drive=drive):
                st = self.state()
                base = st[drive]
                plan = self.plan(
                    wake_run_id=f'w-{drive}',
                    action='custom_act',
                    fired_drive=drive,
                    desire_driven=False,
                    outcome_at=T0,
                )
                self.assertAlmostEqual(
                    plan['updates'][drive],
                    round(max(0.0, base - sub), 4),
                    places=4,
                )
                # 其他普通 drive 不动（同刻物化 = bootstrap）
                for other in FIXED:
                    if other == drive:
                        continue
                    self.assertAlmostEqual(
                        plan['updates'][other], st[other], places=4)

    def test_attachment_strictly_subtracts_0_55(self):
        plan = self.plan(
            action='reply',
            fired_drive='attachment',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(plan['updates']['attachment'], 0.25, places=4)
        self.assertAlmostEqual(
            plan['diagnostics']['legacy_fixed_after']['attachment'], 0.25)
        # candidate ×0.45 仅诊断
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
            action='none',
            fired_drive=None,
            desire_driven=False,
            outcome_at=T0,
        )
        st = self.state()
        for k in FIXED:
            self.assertAlmostEqual(plan['updates'][k], st[k], places=4)
        self.assertAlmostEqual(plan['updates']['fatigue'], 0.26, places=4)
        self.assertEqual(plan['updates']['drives_updated_at'], T0)

    def test_non_none_fatigue_only_plus_0_04(self):
        plan = self.plan(
            action='github',
            fired_drive='curiosity',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(plan['updates']['fatigue'], 0.34, places=4)
        # desire ratio 诊断里是 +0.08，不得写进 updates
        self.assertAlmostEqual(
            plan['diagnostics']['legacy_desire_ratio_after']['fatigue'],
            0.38,
            places=4,
        )
        self.assertNotEqual(
            plan['updates']['fatigue'],
            plan['diagnostics']['legacy_desire_ratio_after']['fatigue'],
        )

    def test_does_not_re_infer_highest_drive(self):
        """curiosity 更高，但 fired_drive=attachment 时只扣 attachment。"""
        # bootstrap: attachment=0.80, curiosity=0.70 — 人为拉高 curiosity
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
            action='github',
            fired_drive='attachment',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(plan['updates']['attachment'], 0.0, places=4)
        self.assertAlmostEqual(plan['updates']['curiosity'], 0.95, places=4)


class MaterializeThenSettleTests(OutcomeBase):
    def test_materialize_to_outcome_at_before_settlement(self):
        # curiosity 已贴 cap=0.70；先压低 duty/curiosity，确认先物化再结算
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
            action='github',
            fired_drive='curiosity',
            desire_driven=False,
            outcome_at=T_6H,
        )
        mat = plan['diagnostics']['materialized_before']
        self.assertGreater(mat['curiosity'], 0.40)
        self.assertGreater(mat['duty'], 0.40)
        expected = round(max(0.0, mat['curiosity'] - 0.45), 4)
        self.assertAlmostEqual(plan['updates']['curiosity'], expected, places=4)
        # duty 只物化、不被 curiosity 结算改写
        self.assertAlmostEqual(plan['updates']['duty'], mat['duty'], places=4)
        self.assertEqual(plan['updates']['drives_updated_at'], T_6H)

    def test_drives_updated_at_advances_on_apply(self):
        r = events.apply_outcome(self.conn, **_outcome_kwargs(outcome_at=T_1H))
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
                action='vent',
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
                    action=action,
                    fired_drive='curiosity' if action != 'none' else None,
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
                    round(min(1.0, mat['fatigue'] + 0.08), 4),
                    places=4,
                )
                # 未列入 ratio 的普通 drive 保持物化值
                for key in FIXED:
                    if key in ratios:
                        continue
                    self.assertAlmostEqual(ratio_after[key], mat[key], places=4)

    def test_desire_driven_live_double_diagnostics_only(self):
        plan = self.plan(
            action='github',
            fired_drive='curiosity',
            desire_driven=True,
            outcome_at=T0,
        )
        fixed = plan['diagnostics']['legacy_fixed_after']
        double = plan['diagnostics']['legacy_live_double_after']
        self.assertIsNotNone(double)
        # fixed: curiosity 0.70-0.45=0.25, fatigue 0.34
        self.assertAlmostEqual(fixed['curiosity'], 0.25, places=4)
        self.assertAlmostEqual(fixed['fatigue'], 0.34, places=4)
        # then desire: curiosity ×0.50, fatigue +0.08
        self.assertAlmostEqual(double['curiosity'], 0.125, places=4)
        self.assertAlmostEqual(double['fatigue'], 0.42, places=4)
        # state updates = fixed only
        self.assertEqual(plan['updates']['curiosity'], fixed['curiosity'])
        self.assertEqual(plan['updates']['fatigue'], fixed['fatigue'])
        self.assertNotEqual(plan['updates']['curiosity'], double['curiosity'])
        self.assertNotEqual(plan['updates']['fatigue'], double['fatigue'])

        r = events.apply_outcome(
            self.conn,
            **_outcome_kwargs(desire_driven=True, outcome_at=T0),
        )
        self.assertEqual(r.status, 'applied')
        st = self.state()
        self.assertAlmostEqual(st['curiosity'], 0.25, places=4)
        self.assertAlmostEqual(st['fatigue'], 0.34, places=4)

    def test_desire_driven_false_double_is_none(self):
        plan = self.plan(desire_driven=False)
        self.assertIsNone(plan['diagnostics']['legacy_live_double_after'])

    def test_candidate_attachment_ratio_recorded_only(self):
        plan = self.plan(
            action='reply',
            fired_drive='curiosity',
            desire_driven=False,
            outcome_at=T0,
        )
        self.assertAlmostEqual(
            plan['diagnostics']['candidate_attachment_ratio_after'],
            0.36,
            places=4,
        )
        self.assertAlmostEqual(plan['updates']['attachment'], 0.80, places=4)


class IdempotencyTests(OutcomeBase):
    def test_same_wake_run_id_retry_duplicate(self):
        kwargs = _outcome_kwargs(wake_run_id='dup-1')
        r1 = events.apply_outcome(self.conn, **kwargs)
        r2 = events.apply_outcome(self.conn, **kwargs)
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(self.state()['state_version'], 1)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM internal_state_events "
            "WHERE event_key='wake_outcome:dup-1'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_same_key_different_action_conflict(self):
        events.apply_outcome(
            self.conn, **_outcome_kwargs(wake_run_id='c1', action='github'))
        r = events.apply_outcome(
            self.conn, **_outcome_kwargs(wake_run_id='c1', action='vent'))
        self.assertEqual(r.status, 'idempotency_conflict')
        self.assertAlmostEqual(self.state()['curiosity'], 0.25, places=4)

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
        self.assertEqual(self.state()['drives_updated_at'], T0)


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
        self.assertAlmostEqual(st['curiosity'], 0.25, places=4)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM internal_state_events "
            "WHERE event_key='wake_outcome:concurrent-1'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)


class ValidationTests(OutcomeBase):
    def test_illegal_wake_run_id_rejected(self):
        cases = [
            '', '   ', True, False, 1, 1.5, b'wake', ['x'], {'a': 1},
            'x' * 129,
        ]
        for i, bad in enumerate(cases):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(wake_run_id=bad),  # type: ignore[arg-type]
                    )
                # 非 str 无法构造 key；str 非法也不消费
                if isinstance(bad, str):
                    rid = bad.strip() or bad
                    self.assertIsNone(
                        store.read_event(
                            self.conn, f'wake_outcome:{rid}'))
        self.assertEqual(self.state()['state_version'], 0)

    def test_illegal_action_rejected(self):
        for bad in ('', '   ', True, 1, None, 'x' * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.apply_outcome(
                        self.conn,
                        **_outcome_kwargs(
                            wake_run_id=f'act-{id(bad)}',
                            action=bad,  # type: ignore[arg-type]
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
                self.assertIsNone(
                    store.read_event(
                        self.conn, f'wake_outcome:fd-{id(bad)}'))
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
                self.assertIsNone(
                    store.read_event(
                        self.conn, f'wake_outcome:dd-{id(bad)}'))
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

        # 修正后同 key 可重试
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
        ev = store.read_event(self.conn, 'wake_outcome:norm')
        payload = json.loads(ev['payload_json'])
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
        payload = json.loads(raw)
        self.assertEqual(
            set(payload.keys()),
            {
                'wake_run_id', 'action', 'fired_drive',
                'desire_driven', 'outcome_at',
            },
        )
        self.assertTrue(payload['desire_driven'] is True)

    def test_three_settlement_examples_side_by_side(self):
        """固定 / ratio / live-double 三组对照示例（报告用）。"""
        plan = self.plan(
            wake_run_id='demo',
            action='github',
            fired_drive='curiosity',
            desire_driven=True,
            outcome_at=T0,
        )
        self.assertEqual(
            plan['diagnostics']['legacy_fixed_after']['curiosity'], 0.25)
        self.assertEqual(
            plan['diagnostics']['legacy_desire_ratio_after']['curiosity'],
            0.35,  # 0.70 * 0.50
        )
        self.assertEqual(
            plan['diagnostics']['legacy_live_double_after']['curiosity'],
            0.125,  # (0.70-0.45)*0.50
        )
        self.assertEqual(plan['updates']['curiosity'], 0.25)


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
        # setUp 只用 tempfile 路径；open_store 的实参不得是字面量绝对路径
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
