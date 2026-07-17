"""梦生成器 v3 第一批：纯函数回归（不调 /wake）。"""
import importlib.util
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_mod():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'dream_generator.py'
    spec = importlib.util.spec_from_file_location('dream_generator_v3', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DG = _load_mod()


class DreamGeneratorV3Tests(unittest.TestCase):
    def test_activity_never_leaks_app_name(self):
        for app in ('微信', '小红书', '未知AppXYZ'):
            with mock.patch.object(random, 'choice', side_effect=lambda xs: xs[0]):
                clue = DG._translate_activity(app if app != '未知AppXYZ' else '淘宝')
            self.assertNotIn('微信', clue)
            self.assertNotIn('小红书', clue)
            self.assertNotIn('淘宝', clue)

    def test_decontextualize_v1_replaces_entities(self):
        text = DG._decontextualize_v1('哈娅和费佳今天在聊')
        self.assertNotIn('哈娅', text)
        self.assertNotIn('费佳', text)
        self.assertIn('她', text)
        self.assertIn('我', text)

    def test_pick_traits_cap_and_floor(self):
        with mock.patch.object(random, 'random', return_value=0.0):
            # 全部命中概率 → 超过 3 条时 sample 截断
            with mock.patch.object(random, 'sample', side_effect=lambda xs, k: xs[:k]):
                picked = DG._pick_traits()
            self.assertLessEqual(len(picked), 3)
            self.assertGreaterEqual(len(picked), 1)
        with mock.patch.object(random, 'random', return_value=0.99):
            picked = DG._pick_traits()
            self.assertEqual(picked, ['unresolved_ending'])

    def test_severe_failure_reasons(self):
        self.assertEqual(DG._severe_failure('短', ''), 'too_short')
        long_ok = '雨' * 160
        self.assertIsNone(DG._severe_failure(long_ok, '碎片：无关内容'))
        pad = '空走廊里只有潮湿的脚步声在回响。' * 20
        expl = pad + '我终于明白了这一切的意义。'
        self.assertEqual(DG._severe_failure(expl, ''), 'explanatory_closure')
        anchored = '我们聊到那件事之后房间开始倾斜。' + pad
        self.assertEqual(DG._severe_failure(anchored, ''), 'reality_anchor')
        # 「最近」空间义项不应误杀
        nearest = '我走向离我最近的那扇门，把手冰得发疼。' + pad
        self.assertIsNone(DG._severe_failure(nearest, ''))
        # 时间义项仍要拦
        recent_time = '最近几天她总是不在，房间开始倾斜。' + pad
        self.assertEqual(DG._severe_failure(recent_time, ''), 'reality_anchor')

    def test_primer_copy_detects_long_verbatim(self):
        chunk = '口袋里持续的震动忽然变成了潮水声音啊'
        self.assertGreaterEqual(len(chunk), 18)
        primer = f'碎片：{chunk}还有别的'
        dream = ('天色发青，楼道没有尽头。' * 20) + chunk + ('门开了又关上。' * 10)
        self.assertEqual(DG._severe_failure(dream, primer), 'primer_copy')

    def test_build_primer_has_no_label_prefixes(self):
        materials = {
            'fragments': [{'content': '哈娅把灯关掉', 'source_id': 1}],
            'sensory': ['屏幕的冷光'],
        }
        with mock.patch.object(random, 'random', return_value=0.9):  # 不抹时间
            primer = DG._build_primer('warm', materials, ['spatial_shift'])
        self.assertIn('基调：', primer)
        self.assertIn('潜在感官：', primer)
        self.assertIn('碎片：', primer)
        self.assertIn('本场特征：', primer)
        self.assertNotIn('活动碎片', primer)
        self.assertNotIn('日记片段', primer)
        self.assertNotIn('[', primer)

    def test_fallback_dream_is_nonempty(self):
        text = DG._fallback_dream('drifting')
        self.assertGreater(len(text), 40)
        self.assertIn('我', text)

    def test_remote_shortfall_backfills_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 't.db'
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT, content TEXT, valence REAL, arousal REAL,
                    importance INTEGER, recall_count INTEGER, resolved INTEGER,
                    last_recalled_at TEXT, created_at TEXT
                );
                CREATE TABLE dream_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT, value TEXT, created_at TEXT
                );
                CREATE TABLE wake_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thoughts TEXT
                );
                INSERT INTO posts (type, content, valence, arousal, importance,
                    recall_count, resolved, created_at)
                VALUES
                ('MEMORY', '近记忆甲', 0.5, 0.3, 5, 0, 0,
                 datetime('now','+8 hours','-1 days')),
                ('MEMORY', '近记忆乙', 0.4, 0.7, 6, 1, 0,
                 datetime('now','+8 hours','-2 days')),
                ('MEMORY', '近记忆丙', 0.6, 0.2, 4, 0, 0,
                 datetime('now','+8 hours','-1 days'));
                """
            )
            # 无远期 → recent 应拿到 2+2=4 的预算（池子只有3）
            materials = DG._gather_materials(conn)
            conn.close()
        self.assertEqual(materials['source_mix']['remote'], 0)
        self.assertEqual(materials['source_mix']['recent'], 3)
        self.assertEqual(materials['source_mix']['synthetic'], 0)

    def test_diary_not_duplicated_when_already_in_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 't.db'
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT, content TEXT, valence REAL, arousal REAL,
                    importance INTEGER, recall_count INTEGER, resolved INTEGER,
                    last_recalled_at TEXT, created_at TEXT
                );
                CREATE TABLE dream_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT, value TEXT, created_at TEXT
                );
                CREATE TABLE wake_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thoughts TEXT
                );
                INSERT INTO posts (type, content, valence, arousal, importance,
                    recall_count, resolved, created_at)
                VALUES
                ('DIARY', '同一篇日记不应出现两次', 0.5, 0.3, 9, 0, 0,
                 datetime('now','+8 hours','-1 hours')),
                ('MEMORY', '近记忆甲', 0.5, 0.3, 1, 0, 0,
                 datetime('now','+8 hours','-1 days')),
                ('MEMORY', '近记忆乙', 0.4, 0.7, 1, 0, 0,
                 datetime('now','+8 hours','-2 days'));
                """
            )
            with mock.patch.object(random, 'random', return_value=0.1):  # prefer diary
                materials = DG._gather_materials(conn)
            conn.close()
        contents = [f['content'] for f in materials['fragments']]
        self.assertEqual(contents.count('同一篇日记不应出现两次'), 1)


if __name__ == '__main__':
    unittest.main()
