"""Phase 1A-1 — observe_user_message / plan_user_message_transition（仅临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway：
parity 通过 AST + literal_eval 读取旧源码常量，不触发旧模块 ensure_table。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state as isv3
import internal_state_events as events
import internal_state_store as store


T0 = '2026-07-21 12:00:00'
T_MINUS_1H = '2026-07-21 11:00:00'
T_2H = '2026-07-21 14:00:00'
T_24H = '2026-07-22 12:00:00'
T_6H = '2026-07-21 18:00:00'
T_13H = '2026-07-21 13:00:00'


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


def _load_emotion_desire_constants() -> dict:
    """从 emotion_engine.py 源码 AST 提取常量；绝不 import 该模块。"""
    src = Path(ROOT, 'emotion_engine.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    wanted = ('DESIRE_LEXICON', 'DESIRE_WEIGHTS', 'DESIRE_CAP')
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                out[target.id] = ast.literal_eval(node.value)
    missing = set(wanted) - set(out)
    if missing:
        raise AssertionError(f'missing constants in emotion_engine.py: {missing}')
    return out


def _score_with_constants(text: str, lexicon, weights, cap) -> dict:
    """用提取出的常量本地重算，避免 import emotion_engine。"""
    p_delta = 0.0
    i_delta = 0.0
    for category, words in lexicon.items():
        hits = sum(1 for w in words if w in text)
        if hits > 0:
            effective = math.log1p(hits)
            p_delta += weights[category]['p'] * effective
            i_delta += weights[category]['i'] * effective
    p_delta = max(-cap['p'], min(cap['p'], p_delta))
    i_delta = max(-cap['i'], min(cap['i'], i_delta))
    return {'p_delta': round(p_delta, 4), 'i_delta': round(i_delta, 4)}


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
        expect = {
            T0: isv3.longing_desire_legacy_curve(0.0),
            T_2H: isv3.longing_desire_legacy_curve(2.0),
            T_24H: isv3.longing_desire_legacy_curve(24.0),
        }
        for created_at, want in expect.items():
            plan = events.plan_user_message_transition(
                self.state(),
                message_id=1,
                text='你好',
                created_at=created_at,
                previous_user_at=T0,
            )
            self.assertAlmostEqual(
                plan['payload']['longing_before_reunion'], want, places=3,
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
        self.assertAlmostEqual(plan['diagnostics']['user_idle_hours'], 24.0)

    def test_observe_does_not_query_chat_messages(self):
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
        payload = json.loads(ev['payload_json'])
        self.assertEqual(payload['previous_user_at'], T0)
        self.assertAlmostEqual(
            payload['longing_before_reunion'],
            isv3.longing_desire_legacy_curve(24.0))

    def test_previous_user_after_created_at_is_invalid(self):
        with self.assertRaises(store.StoreError) as ctx:
            events.plan_user_message_transition(
                self.state(),
                message_id=4,
                text='你好',
                created_at=T0,
                previous_user_at=T_2H,
            )
        self.assertIn('previous_user_at', str(ctx.exception))
        with self.assertRaises(store.StoreError):
            events.observe_user_message(
                self.conn,
                message_id=4,
                text='你好',
                created_at=T0,
                previous_user_at=T_2H,
            )
        self.assertIsNone(store.read_event(self.conn, 'user_rule:4'))
        self.assertEqual(self.state()['state_version'], 0)


class ClockRejectTests(EventsBase):
    def test_invalid_created_at_rejected_without_consuming_key(self):
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_user_message(
                self.conn,
                message_id=50,
                text='你好',
                created_at='not-a-timestamp',
                previous_user_at=T0,
            )
        self.assertIn('canonical timestamp', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_rule:50'))
        st = self.state()
        self.assertEqual(st['state_version'], 0)
        self.assertEqual(st['p_updated_at'], T0)
        self.assertEqual(st['passion'], 0.60)

    def test_out_of_order_observe_does_not_rewind_state_clocks(self):
        before = self.state()
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_user_message(
                self.conn,
                message_id=51,
                text='迟到的消息',
                created_at=T_MINUS_1H,
                previous_user_at=None,
            )
        self.assertIn('refusing to rewind', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_rule:51'))
        after = self.state()
        self.assertEqual(after['p_updated_at'], before['p_updated_at'])
        self.assertEqual(after['i_updated_at'], before['i_updated_at'])
        self.assertEqual(after['drives_updated_at'], before['drives_updated_at'])
        self.assertEqual(after['passion'], before['passion'])
        self.assertEqual(after['state_version'], before['state_version'])

    def test_event_after_rejected_out_of_order_counts_only_new_interval(self):
        """倒序被拒后，下一条正常消息只按真实新区间衰减，不双倍。"""
        with self.assertRaises(store.StoreError):
            events.observe_user_message(
                self.conn,
                message_id=52,
                text='倒序',
                created_at=T_MINUS_1H,
                previous_user_at=None,
            )
        # 正常消息：12:00 → 13:00，应只衰减 1h
        r = events.observe_user_message(
            self.conn,
            message_id=53,
            text='无关键词消息',
            created_at=T_13H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        st = self.state()
        expect_p = round(isv3.decay_exponential(0.60, 1.0, isv3.TAU_P_HOURS), 4)
        self.assertAlmostEqual(st['passion'], expect_p, places=4)
        self.assertEqual(st['p_updated_at'], T_13H)
        # 若曾被拨回 11:00，则 13:00 会按 2h 衰减
        wrong_2h = round(isv3.decay_exponential(0.60, 2.0, isv3.TAU_P_HOURS), 4)
        self.assertNotAlmostEqual(st['passion'], wrong_2h, places=4)



    def test_malformed_previous_user_at_rejected_then_same_key_retries(self):
        """非 None 不可解析不得伪装 clock_missing；同 key 正确时间可重试。"""
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_user_message(
                self.conn,
                message_id=60,
                text='你好',
                created_at=T_2H,
                previous_user_at='坏掉的时间',
            )
        self.assertIn('previous_user_at', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_rule:60'))
        self.assertEqual(self.state()['state_version'], 0)

        r = events.observe_user_message(
            self.conn,
            message_id=60,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'applied')
        payload = json.loads(store.read_event(self.conn, 'user_rule:60')['payload_json'])
        self.assertFalse(payload['clock_missing'])
        self.assertEqual(payload['previous_user_at'], T0)

    def test_corrupted_state_clocks_fail_closed(self):
        """三锚点分别损坏：不更新状态、不消费事件、不洗白。"""
        for field in ('p_updated_at', 'i_updated_at', 'drives_updated_at'):
            with self.subTest(field=field):
                self.conn.execute(
                    f'UPDATE internal_state_v3 SET {field}=? WHERE id=1',
                    ('不是时间',))
                self.conn.commit()
                before = dict(self.state())
                with self.assertRaises(store.StoreError) as ctx:
                    events.observe_user_message(
                        self.conn,
                        message_id=70 + hash(field) % 1000,
                        text='你好',
                        created_at=T_2H,
                        previous_user_at=T0,
                    )
                self.assertIn(field, str(ctx.exception))
                after = self.state()
                self.assertEqual(after['state_version'], before['state_version'])
                self.assertEqual(after[field], '不是时间')
                self.assertEqual(after['passion'], before['passion'])
                # restore for next subTest
                self.conn.execute(
                    f'UPDATE internal_state_v3 SET {field}=? WHERE id=1', (T0,))
                self.conn.commit()

    def test_missing_state_clock_rejected(self):
        self.conn.execute(
            'UPDATE internal_state_v3 SET drives_updated_at=NULL WHERE id=1')
        self.conn.commit()
        with self.assertRaises(store.StoreError) as ctx:
            events.observe_user_message(
                self.conn,
                message_id=71,
                text='你好',
                created_at=T_2H,
                previous_user_at=None,
            )
        self.assertIn('missing', str(ctx.exception))
        self.assertIsNone(store.read_event(self.conn, 'user_rule:71'))
        self.assertEqual(self.state()['state_version'], 0)

    def test_trailing_garbage_timestamp_rejected(self):
        garbage = T0 + '坏猫乱写'
        with self.assertRaises(store.StoreError):
            events.observe_user_message(
                self.conn,
                message_id=72,
                text='你好',
                created_at=garbage,
                previous_user_at=None,
            )
        self.assertIsNone(store.read_event(self.conn, 'user_rule:72'))
        with self.assertRaises(store.StoreError):
            events.observe_user_message(
                self.conn,
                message_id=73,
                text='你好',
                created_at=T_2H,
                previous_user_at=T0 + '尾巴',
            )
        self.assertIsNone(store.read_event(self.conn, 'user_rule:73'))

    def test_equivalent_timestamp_forms_share_identity(self):
        """微秒形式 canonicalize 后与标准形式同身份，不产生假 conflict。"""
        r1 = events.observe_user_message(
            self.conn,
            message_id=74,
            text='你好',
            created_at=T_2H + '.000000',
            previous_user_at=T0 + '.000000',
        )
        self.assertEqual(r1.status, 'applied')
        ev = store.read_event(self.conn, 'user_rule:74')
        payload = json.loads(ev['payload_json'])
        self.assertEqual(payload['created_at'], T_2H)
        self.assertEqual(payload['previous_user_at'], T0)
        st = self.state()
        self.assertEqual(st['p_updated_at'], T_2H)
        self.assertEqual(st['i_updated_at'], T_2H)
        self.assertEqual(st['drives_updated_at'], T_2H)

        r2 = events.observe_user_message(
            self.conn,
            message_id=74,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r2.status, 'duplicate')



class BondMaterializeTests(EventsBase):
    def test_passion_tau6_intimacy_tau96(self):
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
        self.assertAlmostEqual(plan['updates']['passion'], round(expect_p, 4))
        self.assertAlmostEqual(plan['updates']['intimacy'], round(expect_i, 4))
        self.assertEqual(plan['updates']['p_updated_at'], T_6H)
        self.assertEqual(plan['updates']['i_updated_at'], T_6H)


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
        base_lib = 0.10
        cap_l = min(isv3.CAP_BOOST_LIMIT, isv3.DRIVE_CAP['libido'] + bond_p * 0.20)
        expect_lib = round(
            max(0.0, min(1.0, cap_l - (cap_l - base_lib) * math.exp(
                -isv3.DRIVE_GROWTH_K['libido'] * t))), 4)
        self.assertAlmostEqual(plan['updates']['libido'], expect_lib)

    def test_zero_elapsed_drive_materialization_is_identity(self):
        st = self.state()
        plan = events.plan_user_message_transition(
            st,
            message_id=21,
            text='无关键词',
            created_at=T0,
            previous_user_at=T0,
        )
        mat = plan['diagnostics']['drives_materialized']
        for key in isv3.DRIVE_KEYS:
            self.assertAlmostEqual(
                mat[key], float(st[key]), places=4, msg=key)
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['fatigue'], 0.40, places=4)
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['attachment'], 0.50, places=4)

    def test_zero_elapsed_user_message_only_applies_fatigue_restore(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=22,
            text='无关键词',
            created_at=T0,
            previous_user_at=T0,
        )
        # t=0：fatigue 物化恒等 0.40，仅 rest −0.12 → 0.28
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['fatigue'], 0.40)
        self.assertAlmostEqual(plan['updates']['fatigue'], 0.28)

    def test_fatigue_does_not_reapply_na_on_every_message(self):
        # 连续两条零间隔消息：不应每次 +na*0.06
        r1 = events.observe_user_message(
            self.conn,
            message_id=23,
            text='第一句',
            created_at=T0,
            previous_user_at=None,
        )
        self.assertEqual(r1.status, 'applied')
        f1 = self.state()['fatigue']
        self.assertAlmostEqual(f1, 0.28, places=4)

        # 同刻第二条：t=0 相对新锚点，物化恒等后再 −0.12
        r2 = events.observe_user_message(
            self.conn,
            message_id=24,
            text='第二句',
            created_at=T0,
            previous_user_at=T0,
        )
        self.assertEqual(r2.status, 'applied')
        f2 = self.state()['fatigue']
        self.assertAlmostEqual(f2, max(0.0, 0.28 - 0.12), places=4)
        # 旧错误路径：0.40+0.015-0.12=0.295，再 +0.015-0.12…
        self.assertNotAlmostEqual(f1, 0.295, places=4)



class FatigueContinuityTests(EventsBase):
    def test_phase0_raw_to_v3_t0_fatigue_is_continuous(self):
        """Phase 0 raw 物化 → bootstrap → t=0 transition：fatigue 数值连续。

        说明：Phase 0 对 raw 加一次 NA；Phase 1A-1 对 materialized base
        使用 effective equilibrium，t=0 恒等，不得再加 NA。
        """
        import datetime as _dt
        observed = _dt.datetime.strptime(T0, '%Y-%m-%d %H:%M:%S')
        last_updated = '2026-07-21 06:00:00'  # 6h earlier
        raw = {
            'attachment': 0.20, 'curiosity': 0.20, 'reflection': 0.20,
            'social': 0.10, 'duty': 0.15, 'libido': 0.05, 'stress': 0.10,
            'fatigue': 0.80,
            'last_updated': last_updated,
        }
        na = 0.25
        phase0 = isv3.drives_from_raw(
            raw, observed, longing_for_boost=0.0,
            passion_for_boost=0.0, na_for_boost=na,
        )
        # 重建库：用 Phase 0 物化值 bootstrap
        self.conn.close()
        self.tmp.cleanup()
        self.tmp = __import__('tempfile').TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'cont.db')
        self.conn = store.open_store(self.db_path)
        snap = _snapshot(
            observed_at=T0,
            affect=__import__('types').SimpleNamespace(
                pa=0.55, na=na, valence=0.6, arousal=0.4, mood_word='平静'),
            candidate_unified_drives=__import__('types').SimpleNamespace(
                **{k: getattr(phase0, k) for k in isv3.DRIVE_KEYS}),
        )
        store.bootstrap_from_snapshot(self.conn, snap)
        st = self.state()
        self.assertAlmostEqual(st['fatigue'], phase0.fatigue, places=4)
        self.assertEqual(st['drives_updated_at'], T0)

        plan = events.plan_user_message_transition(
            st,
            message_id=80,
            text='无关键词',
            created_at=T0,
            previous_user_at=T0,
        )
        # t=0 物化恒等 Phase 0 / bootstrap fatigue
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['fatigue'],
            phase0.fatigue, places=4)
        # 权威写入仅 rest −0.12
        self.assertAlmostEqual(
            plan['updates']['fatigue'],
            max(0.0, phase0.fatigue - 0.12), places=4)


class KeywordParityTests(unittest.TestCase):
    def test_lexicon_weights_cap_match_emotion_engine_source(self):
        legacy = _load_emotion_desire_constants()
        self.assertEqual(events.DESIRE_LEXICON, legacy['DESIRE_LEXICON'])
        self.assertEqual(events.DESIRE_WEIGHTS, legacy['DESIRE_WEIGHTS'])
        self.assertEqual(events.DESIRE_CAP, legacy['DESIRE_CAP'])

    def test_parity_samples_against_extracted_constants(self):
        legacy = _load_emotion_desire_constants()
        samples = [
            '今天天气不错',
            '想你了抱抱',
            '摸摸抱抱亲亲',
            '害怕哭了难受',
            '恨你走开滚',
            '想你想你想你抱抱陪我',
            '摸摸摸摸摸摸',  # 多命中递减
            '费佳陪我爱你',
        ]
        for text in samples:
            expect = _score_with_constants(
                text,
                legacy['DESIRE_LEXICON'],
                legacy['DESIRE_WEIGHTS'],
                legacy['DESIRE_CAP'],
            )
            ours = events.rule_score_desire(text)
            self.assertAlmostEqual(
                ours['passion_delta'], expect['p_delta'], places=4, msg=text)
            self.assertAlmostEqual(
                ours['intimacy_delta'], expect['i_delta'], places=4, msg=text)

    def test_no_keyword_delta_zero(self):
        r = events.rule_score_desire('今天天气不错xyz')
        self.assertEqual(r['passion_delta'], 0.0)
        self.assertEqual(r['intimacy_delta'], 0.0)
        self.assertEqual(r['hit_categories'], [])

    def test_multi_hit_log1p_diminishing(self):
        one = events.rule_score_desire('摸')
        multi = events.rule_score_desire('摸摸抱抱亲亲')
        self.assertGreater(multi['passion_delta'], one['passion_delta'])
        # 线性 3×0.18 会超 cap；log1p 路径应低于线性且与提取常量一致
        legacy = _load_emotion_desire_constants()
        expect = _score_with_constants(
            '摸摸抱抱亲亲',
            legacy['DESIRE_LEXICON'],
            legacy['DESIRE_WEIGHTS'],
            legacy['DESIRE_CAP'],
        )
        self.assertAlmostEqual(multi['passion_delta'], expect['p_delta'])


class SettlementTests(EventsBase):
    def test_attachment_above_threshold_subtracts(self):
        plan = events.plan_user_message_transition(
            self.state(),
            message_id=30,
            text='你好',
            created_at=T0,
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
        self.assertAlmostEqual(
            plan['payload']['materialized_before']['fatigue'], 0.40)
        self.assertAlmostEqual(plan['updates']['fatigue'], 0.28)
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
        store.apply_state_update(
            self.conn,
            event_key='noise:1',
            event_type='noise',
            source_id='1',
            payload={'n': 1},
            mutator=lambda s: {'pa': 0.61},
            expected_state_version=0,
        )
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

    def test_existing_wrong_event_type_is_conflict(self):
        self.conn.execute(
            """
            INSERT INTO internal_state_events (
                event_key, event_type, source_id, payload_json, payload_hash,
                status, state_version_before, state_version_after, applied_at
            ) VALUES (?, ?, ?, ?, ?, 'applied', 0, 0, ?)
            """,
            (
                'user_rule:103',
                'wrong_type',
                '103',
                json.dumps({
                    'message_id': 103,
                    'created_at': T_2H,
                    'previous_user_at': T0,
                    'text_hash': hashlib.sha256('你好'.encode('utf-8')).hexdigest(),
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                'deadbeef',
                T0,
            ),
        )
        self.conn.commit()
        r = events.observe_user_message(
            self.conn,
            message_id=103,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'idempotency_conflict')
        self.assertEqual(self.state()['state_version'], 0)

    def test_existing_wrong_source_id_is_conflict(self):
        self.conn.execute(
            """
            INSERT INTO internal_state_events (
                event_key, event_type, source_id, payload_json, payload_hash,
                status, state_version_before, state_version_after, applied_at
            ) VALUES (?, ?, ?, ?, ?, 'applied', 0, 0, ?)
            """,
            (
                'user_rule:104',
                'user_rule',
                'not-104',
                json.dumps({
                    'message_id': 104,
                    'created_at': T_2H,
                    'previous_user_at': T0,
                    'text_hash': hashlib.sha256('你好'.encode('utf-8')).hexdigest(),
                }, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                'deadbeef',
                T0,
            ),
        )
        self.conn.commit()
        r = events.observe_user_message(
            self.conn,
            message_id=104,
            text='你好',
            created_at=T_2H,
            previous_user_at=T0,
        )
        self.assertEqual(r.status, 'idempotency_conflict')

    def test_same_observation_race_with_different_planned_state_is_duplicate(self):
        """A plan 后、apply 前被抢先；store 可能报 conflict，业务校正为 duplicate。"""
        a_entered_apply = threading.Event()
        b_finished = threading.Event()
        results: dict[str, store.ApplyResult] = {}
        errors: list[BaseException] = []
        db_path = self.db_path
        holder: dict[str, int | None] = {'a_ident': None}

        real_apply = store.apply_state_update

        def gated_apply(*args, **kwargs):
            # 仅拦截 A 线程对同一观察的 apply；B 走真实路径
            if (kwargs.get('event_key') == 'user_rule:105'
                    and threading.get_ident() == holder['a_ident']):
                a_entered_apply.set()
                if not b_finished.wait(timeout=5.0):
                    raise TimeoutError('B did not finish before A apply resumed')
            return real_apply(*args, **kwargs)

        obs_kwargs = dict(
            message_id=105,
            text='你好竞态',
            created_at=T_2H,
            previous_user_at=T0,
        )

        def thread_a():
            holder['a_ident'] = threading.get_ident()
            conn = store.open_store(db_path)
            try:
                with mock.patch(
                        'internal_state_events.apply_state_update',
                        side_effect=gated_apply):
                    results['a'] = events.observe_user_message(
                        conn, **obs_kwargs)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        def thread_b():
            conn = store.open_store(db_path)
            try:
                if not a_entered_apply.wait(timeout=5.0):
                    raise TimeoutError('A never entered apply')
                store.apply_state_update(
                    conn,
                    event_key='noise:race',
                    event_type='noise',
                    source_id='race',
                    payload={'n': 1},
                    mutator=lambda s: {'pa': 0.66, 'passion': 0.55},
                )
                results['b'] = events.observe_user_message(
                    conn, **obs_kwargs)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                b_finished.set()
                conn.close()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=10.0)
        tb.join(timeout=10.0)

        self.assertEqual(errors, [])
        self.assertEqual(results['b'].status, 'applied')
        self.assertEqual(results['a'].status, 'duplicate')
        ev = store.read_event(self.conn, 'user_rule:105')
        self.assertIsNotNone(ev)
        st = store.read_state(self.conn)
        self.assertEqual(st['state_version'], 2)  # noise + one user_rule


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
        payload = json.loads(raw)
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

    def test_events_tests_do_not_import_legacy_engines(self):
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

    def test_no_module_level_side_effects(self):
        src = Path(ROOT, 'internal_state_events.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail(f'module-level call forbidden: line {node.lineno}')

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
