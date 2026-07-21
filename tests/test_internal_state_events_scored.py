"""Phase 1A-2 — observe_scored / plan_scored_transition（仅临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway。
"""

from __future__ import annotations

import ast
import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state as isv3
import internal_state_events as events
import internal_state_store as store


T0 = '2026-07-21 12:00:00'
T_1H = '2026-07-21 13:00:00'
T_2H = '2026-07-21 14:00:00'
T_6H = '2026-07-21 18:00:00'


def _snapshot(**overrides):
    base = SimpleNamespace(
        observed_at=T0,
        affect=SimpleNamespace(
            pa=0.55, na=0.25, valence=0.6, arousal=0.4, mood_word='平静'),
        bond=SimpleNamespace(intimacy=0.50, passion=0.60, commitment=0.70),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.50, curiosity=0.20, reflection=0.30, social=0.10,
            duty=0.15, libido=0.10, stress=0.20, fatigue=0.40),
        diagnostics=SimpleNamespace(source_timestamps={}),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _scores(**overrides):
    base = {
        'valence': 0.80,
        'arousal': 0.40,
        'mood_word': '开心',
        'passion_delta': 0.10,
        'intimacy_delta': 0.05,
        'source': 'test',
    }
    base.update(overrides)
    return base


def _expected_affect(old_pa, old_na, final_v, final_a):
    new_pa = max(0.0, min(1.0, 0.75 * old_pa + 0.25 * final_v))
    na_signal = final_a * (1.0 - final_v) * 0.5 + 0.05
    new_na = max(0.0, min(1.0, 0.75 * old_na + 0.25 * na_signal))
    new_pa = new_pa + 0.08 * (0.5 - new_pa)
    new_na = new_na + 0.08 * (0.2 - new_na)
    return round(new_pa, 4), round(new_na, 4)


class ScoredBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'scored.db')
        self.conn = store.open_store(self.db_path)
        store.bootstrap_from_snapshot(self.conn, _snapshot())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def state(self):
        return store.read_state(self.conn)


class ConditionalStoreTests(ScoredBase):
    def test_stale_skipped_consumes_key_without_version_bump(self):
        r = store.apply_conditional_state_update(
            self.conn,
            event_key='user_scored:1',
            event_type='user_scored',
            source_id='1',
            payload={'message_id': 1},
            decide=lambda s: store.ConditionalDecision(
                status='stale_skipped',
                error='stale: watermark=9',
            ),
            expected_state_version=0,
        )
        self.assertEqual(r.status, 'stale_skipped')
        self.assertEqual(r.state_version_before, 0)
        self.assertEqual(r.state_version_after, 0)
        self.assertEqual(self.state()['state_version'], 0)
        ev = store.read_event(self.conn, 'user_scored:1')
        self.assertEqual(ev['status'], 'stale_skipped')
        self.assertEqual(ev['error'], 'stale: watermark=9')
        # duplicate on retry
        r2 = store.apply_conditional_state_update(
            self.conn,
            event_key='user_scored:1',
            event_type='user_scored',
            source_id='1',
            payload={'message_id': 1},
            decide=lambda s: store.ConditionalDecision(
                status='applied', updates={'pa': 0.99}),
        )
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(self.state()['pa'], 0.55)


class AffectBondMathTests(ScoredBase):
    def test_affect_formula_and_bou(self):
        st = self.state()
        plan = events.plan_scored_transition(
            st, message_id=1, scores=_scores(), scored_at=T0,
        )
        exp_pa, exp_na = _expected_affect(0.55, 0.25, 0.80, 0.40)
        self.assertAlmostEqual(plan['updates']['pa'], exp_pa, places=4)
        self.assertAlmostEqual(plan['updates']['na'], exp_na, places=4)
        self.assertEqual(plan['updates']['valence'], 0.80)
        self.assertEqual(plan['updates']['arousal'], 0.40)
        self.assertEqual(plan['updates']['mood_word'], '开心')
        self.assertEqual(plan['updates']['mood_source_message_id'], 1)

    def test_bond_tau_materialize_then_delta(self):
        plan = events.plan_scored_transition(
            self.state(), message_id=2, scores=_scores(
                passion_delta=0.10, intimacy_delta=0.05),
            scored_at=T_6H,
        )
        mat_p = isv3.decay_exponential(0.60, 6.0, isv3.TAU_P_HOURS)
        mat_i = isv3.decay_exponential(0.50, 6.0, isv3.TAU_I_HOURS)
        self.assertAlmostEqual(
            plan['diagnostics']['materialized_bond']['passion'],
            round(mat_p, 4))
        self.assertAlmostEqual(
            plan['updates']['passion'],
            round(max(0.0, min(1.0, mat_p + 0.10)), 4))
        self.assertAlmostEqual(
            plan['updates']['intimacy'],
            round(max(0.0, min(1.0, mat_i + 0.05)), 4))
        self.assertEqual(plan['updates']['p_updated_at'], T_6H)
        self.assertEqual(plan['updates']['i_updated_at'], T_6H)
        # drives 不动
        self.assertNotIn('attachment', plan['updates'])
        self.assertNotIn('drives_updated_at', plan['updates'])


class ObserveScoredAppliedTests(ScoredBase):
    def test_watermark_advances_and_fields_update(self):
        r = events.observe_scored(
            self.conn, message_id=10, scores=_scores(), scored_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        st = self.state()
        self.assertEqual(st['last_scored_message_id'], 10)
        self.assertEqual(st['mood_word'], '开心')
        self.assertEqual(st['mood_source_message_id'], 10)
        self.assertEqual(st['state_version'], 1)
        drives_before = {
            k: st[k] for k in (
                'attachment', 'curiosity', 'fatigue', 'drives_updated_at')}
        # 再确认 drives 未改
        self.assertEqual(drives_before['drives_updated_at'], T0)
        self.assertAlmostEqual(drives_before['attachment'], 0.50)


class StaleOrderingTests(ScoredBase):
    def test_101_then_100_is_stale_skipped(self):
        r101 = events.observe_scored(
            self.conn, message_id=101, scores=_scores(mood_word='先到'),
            scored_at=T_1H,
        )
        self.assertEqual(r101.status, 'applied')
        st_after_101 = self.state()
        ver = st_after_101['state_version']
        pa = st_after_101['pa']

        r100 = events.observe_scored(
            self.conn, message_id=100, scores=_scores(
                valence=0.1, arousal=0.9, mood_word='迟到',
                passion_delta=-0.2, intimacy_delta=-0.1),
            scored_at=T_2H,
        )
        self.assertEqual(r100.status, 'stale_skipped')
        st = self.state()
        self.assertEqual(st['state_version'], ver)
        self.assertEqual(st['last_scored_message_id'], 101)
        self.assertEqual(st['pa'], pa)
        self.assertEqual(st['mood_word'], '先到')
        ev = store.read_event(self.conn, 'user_scored:100')
        self.assertEqual(ev['status'], 'stale_skipped')
        self.assertIn('last_scored_message_id=101', ev['error'])
        self.assertEqual(ev['state_version_before'], ev['state_version_after'])

    def test_102_first_then_101_real_out_of_order(self):
        r102 = events.observe_scored(
            self.conn, message_id=102, scores=_scores(mood_word='102先'),
            scored_at=T_1H,
        )
        self.assertEqual(r102.status, 'applied')
        r101 = events.observe_scored(
            self.conn, message_id=101, scores=_scores(mood_word='101晚'),
            scored_at=T_2H,
        )
        self.assertEqual(r101.status, 'stale_skipped')
        st = self.state()
        self.assertEqual(st['last_scored_message_id'], 102)
        self.assertEqual(st['mood_word'], '102先')
        stats = events.get_scored_event_stats(self.conn)
        self.assertEqual(stats['applied'], 1)
        self.assertEqual(stats['stale_skipped'], 1)
        self.assertEqual(stats['stale_score_count'], 1)
        self.assertEqual(stats['total_decided'], 2)
        self.assertAlmostEqual(stats['stale_score_rate'], 0.5)

    def test_stale_retry_is_duplicate(self):
        events.observe_scored(
            self.conn, message_id=50, scores=_scores(), scored_at=T0)
        kwargs = dict(
            message_id=40, scores=_scores(mood_word='旧'), scored_at=T_1H)
        r1 = events.observe_scored(self.conn, **kwargs)
        self.assertEqual(r1.status, 'stale_skipped')
        r2 = events.observe_scored(self.conn, **kwargs)
        self.assertEqual(r2.status, 'duplicate')


class IdempotencyAndVersionTests(ScoredBase):
    def test_same_key_different_scores_conflict(self):
        events.observe_scored(
            self.conn, message_id=7, scores=_scores(), scored_at=T0)
        r = events.observe_scored(
            self.conn, message_id=7,
            scores=_scores(valence=0.2, mood_word='另一套'),
            scored_at=T0,
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
        r = events.observe_scored(
            self.conn, message_id=8, scores=_scores(), scored_at=T0,
            expected_state_version=0,
        )
        self.assertEqual(r.status, 'version_conflict')
        self.assertIsNone(store.read_event(self.conn, 'user_scored:8'))
        # retry with current version
        r2 = events.observe_scored(
            self.conn, message_id=8, scores=_scores(), scored_at=T0,
            expected_state_version=self.state()['state_version'],
        )
        self.assertEqual(r2.status, 'applied')


class ValidationTests(ScoredBase):
    def test_nonfinite_and_oob_rejected(self):
        cases = [
            {'valence': float('nan')},
            {'arousal': float('inf')},
            {'passion_delta': 0.5},
            {'intimacy_delta': -0.5},
            {'valence': 1.5},
            {'mood_word': ''},
            {'mood_word': 'x' * 31},
        ]
        for i, over in enumerate(cases):
            with self.subTest(over=over):
                with self.assertRaises(store.StoreError):
                    events.observe_scored(
                        self.conn,
                        message_id=200 + i,
                        scores=_scores(**over),
                        scored_at=T0,
                    )
                self.assertIsNone(
                    store.read_event(self.conn, f'user_scored:{200 + i}'))
                self.assertEqual(self.state()['state_version'], 0)

    def test_bool_and_numeric_string_scores_rejected_then_retry(self):
        """布尔 / 数字字符串不得冒充评分；拒收后同 key 合法数字可重试。"""
        numeric_fields = (
            'valence', 'arousal', 'passion_delta', 'intimacy_delta',
        )
        before = dict(self.state())
        mid = 220
        # bool
        for field in numeric_fields:
            for bad in (True, False):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(store.StoreError) as ctx:
                        events.observe_scored(
                            self.conn,
                            message_id=mid,
                            scores=_scores(**{field: bad}),
                            scored_at=T0,
                        )
                    self.assertIn('int or float', str(ctx.exception))
                    self.assertIsNone(
                        store.read_event(self.conn, f'user_scored:{mid}'))
                    after = self.state()
                    self.assertEqual(after['state_version'], before['state_version'])
                    self.assertEqual(after['pa'], before['pa'])
                    self.assertEqual(after['valence'], before['valence'])
        # numeric strings
        string_values = {
            'valence': '0.8',
            'arousal': '0.4',
            'passion_delta': '0.1',
            'intimacy_delta': '0.05',
        }
        for field, bad in string_values.items():
            with self.subTest(field=field, bad=bad):
                with self.assertRaises(store.StoreError) as ctx:
                    events.observe_scored(
                        self.conn,
                        message_id=mid,
                        scores=_scores(**{field: bad}),
                        scored_at=T0,
                    )
                self.assertIn('int or float', str(ctx.exception))
                self.assertIsNone(
                    store.read_event(self.conn, f'user_scored:{mid}'))
                self.assertEqual(self.state()['state_version'], 0)

        r = events.observe_scored(
            self.conn, message_id=mid, scores=_scores(), scored_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        self.assertEqual(self.state()['last_scored_message_id'], mid)

    def test_illegal_scored_at_does_not_consume_key(self):
        with self.assertRaises(store.StoreError):
            events.observe_scored(
                self.conn, message_id=20, scores=_scores(),
                scored_at='坏时间',
            )
        self.assertIsNone(store.read_event(self.conn, 'user_scored:20'))

        with self.assertRaises(store.StoreError):
            events.observe_scored(
                self.conn, message_id=21, scores=_scores(),
                scored_at=T0 + '.100000',
            )
        self.assertIsNone(store.read_event(self.conn, 'user_scored:21'))

    def test_scored_at_before_bond_clock_rejected(self):
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_scored(
                self.conn, message_id=22, scores=_scores(),
                scored_at='2026-07-21 11:00:00',
            )
        self.assertIn('refusing to rewind', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_scored:22'))


class PayloadAndImportTests(ScoredBase):
    def test_payload_excludes_conversation_text(self):
        events.observe_scored(
            self.conn, message_id=30,
            scores=_scores(source='unit'),
            scored_at=T0,
        )
        raw = store.read_event(self.conn, 'user_scored:30')['payload_json']
        self.assertNotIn('conversation', raw)
        self.assertNotIn('prompt', raw)
        self.assertNotIn('正文', raw)
        payload = json.loads(raw)
        self.assertEqual(set(payload.keys()), {'message_id', 'scored_at', 'scores'})
        self.assertEqual(
            set(payload['scores'].keys()),
            {'valence', 'arousal', 'mood_word', 'passion_delta',
             'intimacy_delta', 'source'})


class DualChannelTests(ScoredBase):
    def test_user_rule_then_user_scored_both_apply_once(self):
        """Phase 1A 故意双通道叠加：关键词 + 异步评分各改一次 P/I。"""
        r1 = events.observe_user_message(
            self.conn,
            message_id=90,
            text='想你了抱抱',
            created_at=T0,
            previous_user_at=None,
        )
        self.assertEqual(r1.status, 'applied')
        st1 = self.state()
        p_after_rule = st1['passion']
        i_after_rule = st1['intimacy']
        self.assertGreater(p_after_rule, 0.60)  # bootstrap 0.60 + keyword

        r2 = events.observe_scored(
            self.conn,
            message_id=90,
            scores=_scores(passion_delta=0.10, intimacy_delta=0.05),
            scored_at=T0,
        )
        self.assertEqual(r2.status, 'applied')
        st2 = self.state()
        self.assertAlmostEqual(
            st2['passion'],
            round(max(0.0, min(1.0, p_after_rule + 0.10)), 4))
        self.assertAlmostEqual(
            st2['intimacy'],
            round(max(0.0, min(1.0, i_after_rule + 0.05)), 4))

        # 各事件只结算一次
        self.assertEqual(
            events.observe_user_message(
                self.conn, message_id=90, text='想你了抱抱',
                created_at=T0, previous_user_at=None,
            ).status,
            'duplicate')
        self.assertEqual(
            events.observe_scored(
                self.conn, message_id=90,
                scores=_scores(passion_delta=0.10, intimacy_delta=0.05),
                scored_at=T0,
            ).status,
            'duplicate')
        st3 = self.state()
        self.assertEqual(st3['passion'], st2['passion'])
        self.assertEqual(st3['intimacy'], st2['intimacy'])


class ConcurrencyWatermarkTests(ScoredBase):
    def test_two_connections_101_102_watermark_does_not_regress(self):
        db_path = self.db_path
        results: dict[str, store.ApplyResult] = {}
        errors: list[BaseException] = []

        def run(mid: int, label: str):
            conn = store.open_store(db_path)
            try:
                results[label] = events.observe_scored(
                    conn,
                    message_id=mid,
                    scores=_scores(mood_word=f'm{mid}'),
                    scored_at=T_1H,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=run, args=(101, 'a'))
        t2 = threading.Thread(target=run, args=(102, 'b'))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertEqual(errors, [])
        statuses = {results['a'].status, results['b'].status}
        self.assertTrue(statuses <= {'applied', 'stale_skipped'})
        self.assertIn('applied', statuses)
        st = self.state()
        # watermark 必须是两者中较大的已 applied id；不得回退到较小值独占
        self.assertEqual(st['last_scored_message_id'], 102)
        # 若 102 applied：mood 为 m102；若竞态下 101 先写但 102 后写覆盖
        self.assertEqual(st['mood_source_message_id'], 102)



class MessageIdContractTests(ScoredBase):
    def test_scored_rejects_bool_message_id_without_consuming_key(self):
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_scored(
                self.conn, message_id=True, scores=_scores(), scored_at=T0)
        self.assertIn('positive integer', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_scored:1'))
        self.assertEqual(self.state()['state_version'], 0)

    def test_scored_rejects_fractional_message_id_without_consuming_key(self):
        with self.assertRaises(store.StoreError):
            events.observe_scored(
                self.conn, message_id=101.9, scores=_scores(), scored_at=T0)
        self.assertIsNone(store.read_event(self.conn, 'user_scored:101'))
        # 真 101 仍可落账
        r = events.observe_scored(
            self.conn, message_id=101, scores=_scores(), scored_at=T0)
        self.assertEqual(r.status, 'applied')
        self.assertEqual(self.state()['last_scored_message_id'], 101)

    def test_scored_rejects_zero_and_negative_message_id(self):
        for bad in (0, -3):
            with self.subTest(bad=bad):
                with self.assertRaises(store.StoreError):
                    events.observe_scored(
                        self.conn, message_id=bad, scores=_scores(),
                        scored_at=T0)
                self.assertIsNone(
                    store.read_event(self.conn, f'user_scored:{bad}'))
        self.assertEqual(self.state()['state_version'], 0)

    def test_user_rule_and_user_scored_share_strict_message_id_contract(self):
        for bad in (True, 1.5, 0, -1, '001'):
            with self.subTest(channel='rule', bad=bad):
                with self.assertRaises(store.StoreError):
                    events.observe_user_message(
                        self.conn, message_id=bad, text='你好',
                        created_at=T0, previous_user_at=None)
            with self.subTest(channel='scored', bad=bad):
                with self.assertRaises(store.StoreError):
                    events.observe_scored(
                        self.conn, message_id=bad, scores=_scores(),
                        scored_at=T0)
        self.assertEqual(self.state()['state_version'], 0)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:1'))
        self.assertIsNone(store.read_event(self.conn, 'user_scored:1'))


class StaleVersionRetryTests(ScoredBase):
    def test_stale_with_old_expected_version_retries_to_stale_skipped(self):
        """旧 expected → version_conflict → 重读版本重试 → stale_skipped。"""
        r102 = events.observe_scored(
            self.conn, message_id=102, scores=_scores(mood_word='新'),
            scored_at=T_1H, expected_state_version=0,
        )
        self.assertEqual(r102.status, 'applied')
        current = self.state()['state_version']
        self.assertEqual(current, 1)

        # 评分任务仍拿着启动时的 expected=0
        r_conflict = events.observe_scored(
            self.conn, message_id=101, scores=_scores(mood_word='旧迟到'),
            scored_at=T_2H, expected_state_version=0,
        )
        self.assertEqual(r_conflict.status, 'version_conflict')
        self.assertIsNone(store.read_event(self.conn, 'user_scored:101'))
        empty = events.get_scored_event_stats(self.conn)
        self.assertEqual(empty['stale_skipped'], 0)
        self.assertEqual(empty['stale_score_count'], 0)

        # 重读当前版本后同 payload 重试 → 终态 stale
        r_stale = events.observe_scored(
            self.conn, message_id=101, scores=_scores(mood_word='旧迟到'),
            scored_at=T_2H, expected_state_version=current,
        )
        self.assertEqual(r_stale.status, 'stale_skipped')
        ev = store.read_event(self.conn, 'user_scored:101')
        self.assertEqual(ev['status'], 'stale_skipped')
        self.assertEqual(self.state()['last_scored_message_id'], 102)
        self.assertEqual(self.state()['mood_word'], '新')

    def test_stale_version_retry_consumes_key_once(self):
        events.observe_scored(
            self.conn, message_id=50, scores=_scores(), scored_at=T0)
        ver = self.state()['state_version']
        kwargs = dict(
            message_id=40, scores=_scores(mood_word='晚'), scored_at=T_1H,
            expected_state_version=0,
        )
        self.assertEqual(
            events.observe_scored(self.conn, **kwargs).status,
            'version_conflict')
        kwargs['expected_state_version'] = ver
        self.assertEqual(
            events.observe_scored(self.conn, **kwargs).status,
            'stale_skipped')
        self.assertEqual(
            events.observe_scored(self.conn, **kwargs).status,
            'duplicate')
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM internal_state_events "
            "WHERE event_key='user_scored:40'"
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_stale_stats_include_retried_version_conflict(self):
        events.observe_scored(
            self.conn, message_id=102, scores=_scores(), scored_at=T0)
        ver = self.state()['state_version']
        events.observe_scored(
            self.conn, message_id=101, scores=_scores(), scored_at=T_1H,
            expected_state_version=0)  # conflict, not counted
        before = events.get_scored_event_stats(self.conn)
        self.assertEqual(before['stale_score_count'], 0)
        events.observe_scored(
            self.conn, message_id=101, scores=_scores(), scored_at=T_1H,
            expected_state_version=ver)
        after = events.get_scored_event_stats(self.conn)
        self.assertEqual(after['applied'], 1)
        self.assertEqual(after['stale_score_count'], 1)
        self.assertEqual(after['stale_skipped'], 1)
        self.assertAlmostEqual(after['stale_score_rate'], 0.5)

    def test_empty_ledger_stale_rate_is_none(self):
        stats = events.get_scored_event_stats(self.conn)
        self.assertEqual(stats['applied'], 0)
        self.assertEqual(stats['stale_score_count'], 0)
        self.assertEqual(stats['total_decided'], 0)
        self.assertIsNone(stats['stale_score_rate'])


class GuardTests(unittest.TestCase):
    def test_events_module_still_forbids_legacy_imports(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {
            'emotion_engine', 'drive_engine', 'desire', 'gateway', 'app',
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split('.')[0])
        self.assertFalse(forbidden & found)

    def test_scored_tests_do_not_import_legacy(self):
        src = Path(__file__).read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {'emotion_engine', 'drive_engine', 'desire', 'gateway'}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split('.')[0])
        self.assertFalse(forbidden & found)


if __name__ == '__main__':
    unittest.main()
