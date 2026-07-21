"""Phase 0 — Internal State Shadow 只读核心测试。

覆盖（对应验收清单）：
  1. 所有读取路径无 DB 写入（三张旧表 + chat/wake 表逐字节一致）
  2. 相同输入与 observed_at → 相同结果（纯核心确定性）
  3. user_idle_hours 与 effective_idle_hours 不混用（longing 只吃 user_idle）
  4. 三条 longing 公式各自正确
  5. Bond P/I 衰减使用各自时间戳（τ6 / τ96）
  6. fatigue 指数回归正确（含 na 微调）
  7. candidate ChatStateView 不含任何自由文风文案 / 禁词
  8. 时钟不可靠时 fail closed：longing 全空，不制造 999h 思念
  9. internal_state 不引入 gateway（无循环依赖）
 10. 未来写接口签名必须携带唯一事件 ID
"""

from __future__ import annotations

import datetime
import inspect
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import emotion_engine
import drive_engine
import desire
import internal_state as ist
from chat.interaction_state import read_interaction_clock

NOW = datetime.datetime(2026, 7, 21, 12, 0, 0)


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


class ShadowBase(unittest.TestCase):
    """临时库：三张旧状态表 + chat_messages + wake_log，全部指向同一文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')

        # 三个旧模块的 DB_PATH 打到临时库（模块属性在调用时读取）
        self._orig_paths = (emotion_engine.DB_PATH, drive_engine.DB_PATH,
                            desire.DB_PATH)
        emotion_engine.DB_PATH = self.db_path
        drive_engine.DB_PATH = self.db_path
        desire.DB_PATH = self.db_path

        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, woke_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        emotion_engine.ensure_table()
        drive_engine.ensure_table()
        desire.ensure_table()

    def tearDown(self):
        (emotion_engine.DB_PATH, drive_engine.DB_PATH,
         desire.DB_PATH) = self._orig_paths
        self.tmp.cleanup()

    # ── helpers ────────────────────────────────────────────
    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed_user_message(self, created_at: datetime.datetime):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', 'hi', _fmt(created_at)),
        )
        conn.commit()
        conn.close()

    def seed_wake_message(self, woke_at: datetime.datetime):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (action, woke_at) VALUES ('message', ?)",
            (_fmt(woke_at),),
        )
        conn.commit()
        conn.close()

    def seed_emotion(self, **cols):
        conn = self.get_db()
        sets = ', '.join(f'{k}=?' for k in cols)
        conn.execute(f'UPDATE emotion_state SET {sets} WHERE id=1',
                     tuple(cols.values()))
        conn.commit()
        conn.close()

    def seed_drive(self, **cols):
        conn = self.get_db()
        sets = ', '.join(f'{k}=?' for k in cols)
        conn.execute(f'UPDATE drive_state SET {sets} WHERE id=1',
                     tuple(cols.values()))
        conn.commit()
        conn.close()

    def dump_tables(self) -> dict:
        conn = self.get_db()
        out = {}
        for table in ('emotion_state', 'drive_state', 'desire_state',
                      'chat_messages', 'wake_log'):
            rows = conn.execute(f'SELECT * FROM {table}').fetchall()
            out[table] = [tuple(r) for r in rows]
        conn.close()
        return out

    def capture(self, now=NOW, include_legacy=True):
        return ist.capture_shadow_snapshot(self.get_db, now=now,
                                           include_legacy=include_legacy)


class ReadOnlyTests(ShadowBase):
    def test_capture_writes_nothing(self):
        """验收 1 & 10：运行前后五张表内容完全一致。"""
        self.seed_user_message(NOW - datetime.timedelta(hours=3))
        self.seed_emotion(sternberg_p=0.5, sternberg_i=0.6,
                          p_updated_at=_fmt(NOW - datetime.timedelta(hours=6)),
                          i_updated_at=_fmt(NOW - datetime.timedelta(hours=6)))
        before = self.dump_tables()
        snap = self.capture()
        ist.build_chat_view(snap)
        ist.snapshot_to_json(snap)
        after = self.dump_tables()
        self.assertEqual(before, after)

    def test_no_forbidden_write_calls_in_source(self):
        """守卫：AST 扫描，源码中不得出现任何旧系统写函数的真实调用。

        用 AST 而非字符串匹配，避免把 docstring/注释里的禁用清单误判成调用。
        """
        import ast
        src = Path(ROOT, 'internal_state.py').read_text(encoding='utf-8')
        forbidden = {'touch_interaction', 'touch_hayana', 'rest', 'discharge',
                     'discharge_by_action', 'satisfy', 'apply_desire_delta',
                     'apply_desire_delta_async', 'score_and_update',
                     'score_async', 'calibrate_va', '_flush'}
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
            if name in forbidden:
                self.fail(f'forbidden write call in source at line '
                          f'{node.lineno}: {name}()')

    def test_no_gateway_import(self):
        """验收 9：不 import gateway，无循环依赖。"""
        src = Path(ROOT, 'internal_state.py').read_text(encoding='utf-8')
        self.assertNotIn('import gateway', src)
        self.assertNotIn('from gateway', src)


class DeterminismTests(ShadowBase):
    def test_same_input_same_output(self):
        """验收 2：固定 observed_at 与输入行 → compute_snapshot 完全一致。"""
        self.seed_user_message(NOW - datetime.timedelta(hours=5))
        clock = read_interaction_clock(self.get_db, now=NOW)
        emotion_row = {'pa': 0.55, 'na': 0.25, 'valence': 0.6, 'arousal': 0.4,
                       'mood_word': '平静', 'sternberg_p': 0.4,
                       'sternberg_i': 0.5, 'sternberg_c': 0.7,
                       'p_updated_at': _fmt(NOW - datetime.timedelta(hours=3)),
                       'i_updated_at': _fmt(NOW - datetime.timedelta(hours=3)),
                       'last_interaction': _fmt(NOW - datetime.timedelta(hours=5))}
        drive_row = {k: 0.2 for k in ist.DRIVE_KEYS}
        drive_row['last_updated'] = _fmt(NOW - datetime.timedelta(hours=4))
        s1 = ist.compute_snapshot(emotion_row, drive_row, None, clock, NOW)
        s2 = ist.compute_snapshot(dict(emotion_row), dict(drive_row), None,
                                  clock, NOW)
        self.assertEqual(ist.snapshot_to_dict(s1), ist.snapshot_to_dict(s2))


class ClockSeparationTests(ShadowBase):
    def test_longing_uses_user_idle_not_effective(self):
        """验收 3：费佳自己发过 Wake 消息不得重置思念。"""
        self.seed_user_message(NOW - datetime.timedelta(hours=48))
        self.seed_wake_message(NOW - datetime.timedelta(minutes=10))
        snap = self.capture(include_legacy=False)
        d = snap.derived
        self.assertAlmostEqual(d.user_idle_hours, 48.0, places=2)
        self.assertAlmostEqual(d.effective_idle_hours, 10 / 60, places=2)
        # 三条 longing 都必须按 48h 算，而不是 10 分钟
        self.assertAlmostEqual(
            d.longing_emotion_legacy,
            ist.longing_emotion_legacy_curve(48.0), places=3)
        self.assertAlmostEqual(
            d.longing_desire_legacy,
            ist.longing_desire_legacy_curve(48.0), places=3)
        self.assertGreater(d.longing_desire_legacy, 0.4)  # 48h 明显思念
        # 若误用 effective(10min)，L 应接近 0：
        wrong = ist.longing_desire_legacy_curve(10 / 60)
        self.assertLess(wrong, 0.02)
        self.assertNotAlmostEqual(d.longing_desire_legacy, wrong, places=2)


class LongingFormulaTests(unittest.TestCase):
    def test_emotion_legacy_curve(self):
        t = 16.0
        expect = min(0.85 * (1 - (1 + t / 8) ** (-0.8)), 0.92)
        self.assertAlmostEqual(
            ist.longing_emotion_legacy_curve(t), round(expect, 3), places=3)

    def test_desire_legacy_curve_matches_module(self):
        for t in (0.5, 6.0, 18.0, 72.0, 500.0):
            L_mod, _, _ = desire.get_longing(t_hours_override=t)
            self.assertAlmostEqual(
                ist.longing_desire_legacy_curve(t), L_mod, places=3)

    def test_candidate_curve_attachment_modulates_tau(self):
        t = 24.0
        low = ist.longing_candidate_curve(t, 0.0)   # τ=21.6
        high = ist.longing_candidate_curve(t, 1.0)  # τ=14.4
        self.assertGreater(high, low)               # 依恋高 → 想得快
        att = 0.5
        tau = 18.0 * (1.2 - 0.4 * att)
        expect = min(0.85 * (1 - (1 + t / tau) ** (-0.8)), 0.90)
        self.assertAlmostEqual(
            ist.longing_candidate_curve(t, att), round(expect, 3), places=3)

    def test_none_idle_gives_none(self):
        self.assertIsNone(ist.longing_emotion_legacy_curve(None))
        self.assertIsNone(ist.longing_desire_legacy_curve(None))
        self.assertIsNone(ist.longing_candidate_curve(None, 0.5))


class BondDecayTests(unittest.TestCase):
    def test_p_and_i_use_separate_timestamps(self):
        """验收 5：P 按 p_updated_at/τ6，I 按 i_updated_at/τ96。"""
        row = {'sternberg_p': 0.8, 'sternberg_i': 0.8, 'sternberg_c': 0.7,
               'p_updated_at': _fmt(NOW - datetime.timedelta(hours=12)),
               'i_updated_at': _fmt(NOW - datetime.timedelta(hours=48))}
        bond = ist.bond_from_emotion_row(row, NOW)
        self.assertAlmostEqual(bond.passion, 0.8 * math.exp(-12 / 6.0), places=3)
        self.assertAlmostEqual(bond.intimacy, 0.8 * math.exp(-48 / 96.0), places=3)
        self.assertAlmostEqual(bond.commitment, 0.7, places=4)
        # P 衰得比 I 狠得多
        self.assertLess(bond.passion, bond.intimacy)


class DriveMathTests(unittest.TestCase):
    def test_fatigue_regression_with_na(self):
        """验收 6：fatigue = eq + (base-eq)·e^(-0.05t) + na×0.06。"""
        row = {k: 0.2 for k in ist.DRIVE_KEYS}
        row['fatigue'] = 0.8
        row['last_updated'] = _fmt(NOW - datetime.timedelta(hours=10))
        drives = ist.candidate_drives_from_raw(row, NOW, 0.0, 0.0, 0.3)
        expect = 0.35 + (0.8 - 0.35) * math.exp(-0.05 * 10) + 0.3 * 0.06
        self.assertAlmostEqual(drives.fatigue, round(expect, 4), places=3)

    def test_explicit_cap_boosts(self):
        row = {k: 0.1 for k in ist.DRIVE_KEYS}
        row['last_updated'] = _fmt(NOW - datetime.timedelta(hours=1000))
        # t→∞ 时值趋近 cap，可直接验证 boost 后的 cap
        drives = ist.candidate_drives_from_raw(
            row, NOW, longing_for_boost=0.6, passion_for_boost=0.5,
            na_for_boost=0.4)
        self.assertAlmostEqual(drives.attachment, min(0.92, 0.75 + 0.6 * 0.15), places=2)
        self.assertAlmostEqual(drives.libido, min(0.92, 0.65 + 0.5 * 0.20), places=2)
        self.assertAlmostEqual(drives.stress, min(0.92, 0.55 + 0.4 * 0.15), places=2)

    def test_fatigue_gate_blocks_intent(self):
        drives = ist.Drives(attachment=0.6, curiosity=0.2, reflection=0.2,
                            social=0.2, duty=0.2, libido=0.2, stress=0.2,
                            fatigue=0.8)
        dominant, intent = ist.pick_candidate_intent(drives, 0.5)
        self.assertEqual((dominant, intent), ('fatigue', 'none'))

    def test_attachment_splits_to_express_longing(self):
        drives = ist.Drives(attachment=0.6, curiosity=0.2, reflection=0.2,
                            social=0.2, duty=0.2, libido=0.2, stress=0.2,
                            fatigue=0.3)
        _, intent_low = ist.pick_candidate_intent(drives, 0.1)
        _, intent_high = ist.pick_candidate_intent(drives, 0.6)
        self.assertEqual(intent_low, 'reassure_attachment')
        self.assertEqual(intent_high, 'express_longing')


class ChatViewTests(ShadowBase):
    def test_view_is_structured_and_style_free(self):
        """验收 7：view 序列化后不含任何文风禁词与中文自由文案。"""
        self.seed_user_message(NOW - datetime.timedelta(hours=30))
        self.seed_drive(attachment=0.6)
        snap = self.capture(include_legacy=False)
        view = ist.build_chat_view(snap)
        self.assertIn(view.intent, ist.INTENTS)
        blob = repr(view)
        for token in ist.FORBIDDEN_STYLE_TOKENS:
            self.assertNotIn(token, blob)
        # content_targets 只允许机器 token（ASCII 标识符）
        for target in view.content_targets:
            self.assertRegex(target, r'^[a-z_]+$')
        for dim in view.source_dimensions:
            self.assertRegex(dim, r'^[a-z_]+$')

    def test_snapshot_json_has_no_style_directives(self):
        self.seed_user_message(NOW - datetime.timedelta(hours=30))
        snap = self.capture(include_legacy=False)
        js = ist.snapshot_to_json(snap)
        for token in ('话少', '安静等待', '简短回应', '语气克制'):
            self.assertNotIn(token, js)


class FailClosedTests(ShadowBase):
    def test_missing_clock_no_fake_longing(self):
        """验收 8：chat_messages 为空 → longing 全空，不出现 999h。"""
        snap = self.capture(include_legacy=True)
        d = snap.derived
        self.assertIsNone(d.user_idle_hours)
        self.assertIsNone(d.longing_emotion_legacy)
        self.assertIsNone(d.longing_desire_legacy)
        self.assertIsNone(d.longing_candidate)
        self.assertFalse(snap.diagnostics.source_health['clock_reliable'])
        self.assertTrue(any('longing' in w for w in snap.diagnostics.warnings))
        js = ist.snapshot_to_json(snap)
        self.assertNotIn('999', js.replace('1999', '').replace('2999', ''))
        # fail closed 时也不调用 desire.get_longing 的默认路径（可能读到停摆字段）
        self.assertIsNone(
            snap.diagnostics.legacy_readings.get('desire.get_longing'))


class WriteInterfaceSignatureTests(unittest.TestCase):
    def test_signatures_carry_event_ids(self):
        """验收 10（幂等前置）：三个接口必须带唯一事件 ID 参数。"""
        sig_msg = inspect.signature(ist.StateWriteInterface.observe_user_message)
        self.assertIn('message_id', sig_msg.parameters)
        self.assertIn('created_at', sig_msg.parameters)
        sig_scored = inspect.signature(ist.StateWriteInterface.observe_scored)
        self.assertIn('message_id', sig_scored.parameters)
        self.assertIn('scored_at', sig_scored.parameters)
        sig_outcome = inspect.signature(ist.StateWriteInterface.apply_outcome)
        self.assertIn('event_id', sig_outcome.parameters)


if __name__ == '__main__':
    unittest.main()
