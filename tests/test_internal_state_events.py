"""Phase 1A-1 — observe_user_message / plan_user_message_transition（仅临时 SQLite）。"""

from __future__ import annotations

import ast
import hashlib
import importlib
import math
import sys
import tempfile
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
T_2H = '2026-07-21 14:00:00'
T_24H = '2026-07-22 12:00:00'
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


class EventsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'events.db')
        self.conn = store.open_store(self.db_path)
        store.bootstrap_from_snapshot(self.conn, _snapshot())

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def state(self):
        return store.read_state(self.conn)


class LongingClockTests(EventsBase):
    def test_longing_0h_2h_24h(self):
        cases = [
            (T0, 0.0),
            (T_2H, None),  # filled below
            (T_24H, None),
        ]
        # expected via frozen τ18 curve
        expect_2h = isv3.longing_desire_legacy_curve(2.0)
        expect_24h = isv3.longing_desire_legacy_curve(24.0)
        cases[1] = (T_2H, expect_2h)
        cases[2] = (T_24H, expect_24h)

        for created_at, expect in cases:
            plan = events.plan_user_message_transition(
                self.state(),
                message_id=1,
                text='你好',
                created_at=created_at,
                previous_user_at=T0,
            )
            self.assertAlmostEqual(
                plan['payload']['longing_before_reunion'], expect, places=3,
                msg=f'created_at={created_at}')
            self.assertFalse(plan['payload']['clock_missing'])

    def test_previous_none_is_clock_missing_zero_longing(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=2,
            text='你好',
            created_at=T_24H,
            previous_user_at=None,
        )
        self.assertTrue(plan['payload']['clock_missing'])
        self.assertEqual(plan['payload']['longing_before_reunion'], 0.0)

    def test_current_message_time_not_used_as_previous(self):
        """显式 previous=T0；若误用 created_at 当 previous，24h longing 会变 0。"""
        created = T_24H
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=3,
            text='回来了',
            created_at=created,
            previous_user_at=T0,
        )
        expect = isv3.longing_desire_legacy_curve(24.0)
        self.assertAlmostEqual(plan['payload']['longing_before_reunion'], expect)
        self.assertNotAlmostEqual(plan['payload']['longing_before_reunion'], 0.0)
        # 证明未把 created_at 当作 previous：idle 应为 24 而非 0
        self.assertAlmostEqual(plan['diagnostics']['user_idle_hours'], 24.0)

    def test_observe_does_not_query_chat_messages(self):
        # 即便临时库里有误导性「当前消息时间」，也不得读取
        self.conn.execute(
            'CREATE TABLE chat_messages ('
            'id INTEGER PRIMARY KEY, author TEXT, created_at TEXT)')
        self.conn.execute(
            "INSERT INTO chat_messages(id, author, created_at) "
            "VALUES (99, 'user', ?)", (T_24H,))
        self.conn.commit()
        r = events.observe_user_message(
            self.conn,
            message_id=99,
            text='你好',
            created_at=T_24H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        ev = store.read_event(self.conn, 'user_rule:99')
        payload = __import__('json').loads(ev['payload_json'])
        self.assertEqual(payload['previous_user_at'], T0)
        self.assertAlmostEqual(
            payload['longing_before_reunion'],
            isv3.longing_desire_legacy_curve(24.0))


class BondMaterializeTests(EventsBase):
    def test_passion_tau6_intimacy_tau96(self):
        # bootstrap 后 p/i_updated_at = T0；passion=0.60 intimacy=0.50
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=10,
            text='无关键词消息xyz',
            created_at=T_6H,
            previous_user_at=T0,
        )
        expect_p = isv3.decay_exponential(0.60, 6.0, isv3.TAU_P_HOURS)
        expect_i = isv3.decay_exponential(0.50, 6.0, isv3.TAU_I_HOURS)
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['passion'],
            round(expect_p, 4), places=4)
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['intimacy'],
            round(expect_i, 4), places=4)
        # 无关键词 → delta 0 → updates 等于物化值
        self.assertAlmostEqual(plan['updates']['passion'], round(expect_p, 4))
        self.assertAlmostEqual(plan['updates']['intimacy'], round(expect_i, 4))
        self.assertEqual(plan['updates']['p_updated_at'], T_6H)
        self.assertEqual(plan['updates']['i_updated_at'], T_6H)

    def test_negative_time_fail_closed_no_reverse_decay(self):
        st = self.state()
        # 人为把基准时间推到未来
        self.conn.execute(
            "UPDATE internal_state_v3 SET p_updated_at=?, i_updated_at=? WHERE id=1",
            (T_24H, T_24H))
        self.conn.commit()
        st = self.state()
        plan = events.plan_user_message_transition(
            st,
            message_id=11,
            text='无关键词',
            created_at=T0,  # 早于 p_updated_at
            previous_user_at=None,
        )
        # 不倒着衰减：hours=0，passion/intimacy 保持原值
        self.assertAlmostEqual(plan['payload']['materialized_before']['passion'], 0.60)
        self.assertAlmostEqual(plan['payload']['materialized_before']['intimacy'], 0.50)


class DrivesMaterializeTests(EventsBase):
    def test_drives_materialize_to_created_at(self):
        st = self.state()
        longing = isv3.longing_desire_legacy_curve(6.0)
        plan = events.plan_user_message_transition(
            st,
            message_id=20,
            text='无关键词',
            created_at=T_6H,
            previous_user_at=T0,
        )
        # 手工对齐 v3 解析解
        bond_p = isv3.decay_exponential(0.60, 6.0, isv3.TAU_P_HOURS)
        t = 6.0
        base_att = 0.50
        cap = min(isv3.CAP_BOOST_LIMIT, isv3.DRIVE_CAP['attachment'] + longing * 0.15)
        expect_att = round(
            max(0.0, min(1.0, cap - (cap - base_att) * math.exp(
                -isv3.DRIVE_GROWTH_K['attachment'] * t))), 4)
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['attachment'], expect_att)
        self.assertEqual(plan['updates']['drives_updated_at'], T_6H)
        # libido cap 使用物化后 passion
        base_lib = 0.10
        cap_l = min(isv3.CAP_BOOST_LIMIT, isv3.DRIVE_CAP['libido'] + bond_p * 0.20)
        expect_lib = round(
            max(0.0, min(1.0, cap_l - (cap_l - base_lib) * math.exp(
                -isv3.DRIVE_GROWTH_K['libido'] * t))), 4)
        self.assertAlmostEqual(plan['updates']['libido'], expect_lib)


class KeywordParityTests(unittest.TestCase):
    def test_parity_with_emotion_engine_samples(self):
        import emotion_engine as ee
        samples = [
            '今天天气不错',
            '想你了抱抱',
            '摸摸抱抱亲亲',
            '害怕哭了难受',
            '恨你走开滚',
            '想你想你想你抱抱陪我',
        ]
        for text in samples:
            legacy = ee.rule_score_desire(text)
            ours = events.rule_score_desire(text)
            self.assertAlmostEqual(
                ours['passion_delta'], legacy['p_delta'], places=4, msg=text)
            self.assertAlmostEqual(
                ours['intimacy_delta'], legacy['i_delta'], places=4, msg=text)

    def test_no_keyword_delta_zero(self):
        r = events.rule_score_desire('今天天气不错xyz')
        self.assertEqual(r['passion_delta'], 0.0)
        self.assertEqual(r['intimacy_delta'], 0.0)
        self.assertEqual(r['hit_categories'], [])

    def test_multi_hit_log1p_diminishing(self):
        # physical 多词命中应小于线性叠加
        one = events.rule_score_desire('摸')
        multi = events.rule_score_desire('摸摸抱抱亲亲')
        self.assertGreater(multi['passion_delta'], one['passion_delta'])
        # 线性 3*0.18=0.54 会被 cap 到 0.40；log1p 路径应与 emotion 一致
        import emotion_engine as ee
        self.assertAlmostEqual(
            multi['passion_delta'], ee.rule_score_desire('摸摸抱抱亲亲')['p_delta'])


class SettlementTests(EventsBase):
    def test_attachment_above_threshold_subtracts(self):
        # attachment 物化后仍 >0.3
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=30,
            text='你好',
            created_at=T0,  # 无时间流逝
            previous_user_at=T0,
        )
        mat = plan['payload']['materialized_before']['attachment']
        self.assertGreater(mat, 0.3)
        self.assertAlmostEqual(
            plan['payload']['legacy_settlement']['attachment_subtract_applied'],
            0.55)
        self.assertAlmostEqual(
            plan['updates']['attachment'],
            max(0.0, mat - 0.55), places=4)

    def test_attachment_at_or_below_threshold_no_discharge(self):
        self.conn.execute(
            'UPDATE internal_state_v3 SET attachment=0.25 WHERE id=1')
        self.conn.commit()
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=31,
            text='你好',
            created_at=T0,
            previous_user_at=T0,
        )
        mat = plan['payload']['materialized_before']['attachment']
        self.assertLessEqual(mat, 0.3)
        self.assertEqual(
            plan['payload']['legacy_settlement']['attachment_subtract_applied'],
            0.0)
        self.assertAlmostEqual(plan['updates']['attachment'], mat)

    def test_fatigue_restores_012(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=32,
            text='你好',
            created_at=T0,
            previous_user_at=T0,
        )
        mat_f = plan['payload']['materialized_before']['fatigue']
        self.assertAlmostEqual(
            plan['updates']['fatigue'], max(0.0, mat_f - 0.12), places=4)
        self.assertEqual(
            plan['payload']['legacy_settlement']['fatigue_restore'], 0.12)

    def test_multiplicative_attachment_recorded_not_applied(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=33,
            text='你好',
            created_at=T0,
            previous_user_at=T0,
        )
        mat = plan['payload']['materialized_before']['attachment']
        cand = round(mat * events.CANDIDATE_ATTACHMENT_SATISFY_RATIO, 4)
        self.assertEqual(
            plan['payload']['candidate_settlement']['mode'], 'shadow_only')
        self.assertAlmostEqual(
            plan['payload']['candidate_settlement']['result_attachment'], cand)
        # 权威路径是减法，不是乘性
        self.assertNotAlmostEqual(plan['updates']['attachment'], cand)

    def test_reunion_boost_recorded_not_applied(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=34,
            text='你好',
            created_at=T_24H,
            previous_user_at=T0,
        )
        longing = plan['payload']['longing_before_reunion']
        expect_boost = round(0.05 + longing * 0.10, 3)
        self.assertAlmostEqual(
            plan['payload']['candidate_reunion_boost'], expect_boost)
        # intimacy 不含 boost
        mat_i = plan['payload']['materialized_before']['intimacy']
        self.assertAlmostEqual(plan['updates']['intimacy'], mat_i)


class ObserveIdempotencyTests(EventsBase):
    def test_duplicate_does_not_double_settle(self):
        kwargs = dict(
            message_id=100,
            text='想你了抱抱',
            created_at=T_2H,
            previous_user_at=T0,
        )
        r1 = events.observe_user_message(self.conn, **kwargs)
        self.assertEqual(r1.status, 'applied')
        st1 = self.state()
        r2 = events.observe_user_message(self.conn, **kwargs)
        self.assertEqual(r2.status, 'duplicate')
        st2 = self.state()
        self.assertEqual(st1['state_version'], st2['state_version'])
        self.assertEqual(st1['passion'], st2['passion'])
        self.assertEqual(st1['attachment'], st2['attachment'])

    def test_same_key_different_payload_idempotency_conflict(self):
        r1 = events.observe_user_message(
            self.conn,
            message_id=101,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r1.status, 'applied')
        ver = self.state()['state_version']
        r2 = events.observe_user_message(
            self.conn,
            message_id=101,
            text='另一句完全不同的话',
            created_at=T_6H,
            previous_user_at=T0,
        )
        self.assertEqual(r2.status, 'idempotency_conflict')
        self.assertEqual(self.state()['state_version'], ver)

    def test_version_conflict_then_same_key_retry(self):
        # 先用 store 推高 version
        store.apply_state_update(
            self.conn,
            event_key='noise:1',
            event_type='noise',
            source_id='1',
            payload={'n': 1},
            mutator=lambda s: {'pa': 0.61},
            expected_state_version=0,
        )
        # 用过期 expected → conflict，且不消费 user_rule key
        r1 = events.observe_user_message(
            self.conn,
            message_id=102,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
            expected_state_version=0,
        )
        self.assertEqual(r1.status, 'version_conflict')
        self.assertIsNone(store.read_event(self.conn, 'user_rule:102'))

        # 重读版本后同 key 重试成功（重新 plan）
        latest = self.state()['state_version']
        r2 = events.observe_user_message(
            self.conn,
            message_id=102,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
            expected_state_version=latest,
        )
        self.assertEqual(r2.status, 'applied')
        self.assertIsNotNone(store.read_event(self.conn, 'user_rule:102'))


class PayloadPrivacyTests(EventsBase):
    def test_payload_excludes_full_text(self):
        secret = '这是私密聊天正文想你抱抱'
        r = events.observe_user_message(
            self.conn,
            message_id=200,
            text=secret,
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        ev = store.read_event(self.conn, 'user_rule:200')
        raw = ev['payload_json']
        self.assertNotIn(secret, raw)
        self.assertNotIn('"text":', raw)
        payload = __import__('json').loads(raw)
        self.assertEqual(
            payload['text_hash'],
            hashlib.sha256(secret.encode('utf-8')).hexdigest())
        self.assertEqual(payload['text_length'], len(secret))
        self.assertIn('affectionate', payload['rule_hit_categories'])


class ImportGuardTests(unittest.TestCase):
    def test_events_module_forbidden_imports(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {
            'emotion_engine', 'drive_engine', 'desire', 'gateway',
            'app', 'moments_turn', 'chat.interaction_state',
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.add(node.module.split('.')[0])
                    found.add(node.module)
        self.assertFalse(forbidden & found, msg=f'forbidden imports: {forbidden & found}')

    def test_no_module_level_side_effects(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail(f'module-level call forbidden: line {node.lineno}')

    def test_runtime_import_does_not_load_legacy_engines(self):
        # 清掉可能已加载的模块后，只加载 events，确认未拉起旧引擎
        # （internal_state 允许；旧引擎禁止）
        for name in list(sys.modules):
            if name in ('internal_state_events',) or name.startswith(
                    'internal_state_events.'):
                del sys.modules[name]
        before = {
            k for k in ('emotion_engine', 'drive_engine', 'desire', 'gateway')
            if k in sys.modules
        }
        importlib.reload(events) if 'internal_state_events' in sys.modules else \
            importlib.import_module('internal_state_events')
        after = {
            k for k in ('emotion_engine', 'drive_engine', 'desire', 'gateway')
            if k in sys.modules
        }
        self.assertEqual(before, after)

    def test_temp_sqlite_only_smoke(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'only.db')
            conn = store.open_store(path)
            store.bootstrap_from_snapshot(conn, _snapshot())
            r = events.observe_user_message(
                conn,
                message_id=1,
                text='你好',
                created_at=T0,
                previous_user_at=None,
            )
            self.assertEqual(r.status, 'applied')
            conn.close()


class PurePlanDeterminismTests(EventsBase):
    def test_same_input_same_output(self):
        st = self.state()
        a = events.plan_user_message_transition(
            st, message_id=1, text='想你', created_at=T_2H, previous_user_at=T0)
        b = events.plan_user_message_transition(
            st, message_id=1, text='想你', created_at=T_2H, previous_user_at=T0)
        self.assertEqual(a['updates'], b['updates'])
        self.assertEqual(a['payload'], b['payload'])


if __name__ == '__main__':
    unittest.main()
