"""潜梦库 dream_latents 第二批单测。"""
import importlib.util
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _load_mod():
    path = Path(__file__).resolve().parents[1] / 'tools' / 'dream_latents.py'
    spec = importlib.util.spec_from_file_location('dream_latents_mod', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DL = _load_mod()


class DreamLatentsTests(unittest.TestCase):
    def _conn(self):
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        conn = sqlite3.connect(tmp.name)
        conn.row_factory = sqlite3.Row
        DL.ensure_table(conn)
        return conn

    def test_ensure_table_idempotent(self):
        conn = self._conn()
        DL.ensure_table(conn)
        DL.ensure_table(conn)
        n = conn.execute('SELECT COUNT(*) c FROM sqlite_master WHERE name="dream_latents"').fetchone()['c']
        self.assertEqual(n, 1)
        conn.close()

    def test_seed_synthetic_once(self):
        conn = self._conn()
        a1 = DL.seed_synthetic_if_empty(conn)
        a2 = DL.seed_synthetic_if_empty(conn)
        self.assertGreater(a1, 0)
        self.assertEqual(a2, 0)
        rows = DL.pick_synthetic(conn, 2)
        self.assertEqual(len(rows), 2)
        conn.close()

    def test_extract_shards_different_source_ids(self):
        frags = [
            {'source_id': 1, 'content': '走廊尽头那盏灯一直在闪'},
            {'source_id': 1, 'content': '同一来源不该再出残渣啊啊'},
            {'source_id': 2, 'content': '口袋震动了一下却没有名字'},
        ]
        with mock.patch.object(random, 'shuffle', side_effect=lambda xs: None):
            with mock.patch.object(DL, 'random_cut', side_effect=lambda t, **k: t[:12]):
                with mock.patch.object(DL, 'contains_banned', return_value=False):
                    shards = DL.extract_shards(frags, max_shards=2)
        self.assertLessEqual(len(shards), 2)
        # 两个不同 source 才能凑满；source_id=1 只贡献一条
        self.assertEqual(len(shards), 2)

    def test_contains_banned_names_and_apps(self):
        self.assertTrue(DL.contains_banned('哈娅在场'))
        self.assertTrue(DL.contains_banned('打开了微信'))
        self.assertFalse(DL.contains_banned('潮水退到膝边'))

    def test_fallback_weight_skips_most(self):
        conn = self._conn()
        items = [{'type': 'phrase', 'content': '停在半空的秒针甲'}]
        with mock.patch.object(random, 'random', return_value=0.9):
            added = DL.insert_latents(
                conn, items, origin='fallback_dream', weight=0.2
            )
        self.assertEqual(added, 0)
        with mock.patch.object(random, 'random', return_value=0.1):
            added = DL.insert_latents(
                conn, items, origin='fallback_dream', weight=0.2
            )
        self.assertEqual(added, 1)
        conn.close()

    def test_get_fallback_latents(self):
        conn = self._conn()
        DL.insert_latents(
            conn,
            [{'type': 'place', 'content': '室内还在下雪的房间甲'}],
            origin='dream',
        )
        conn.execute(
            "UPDATE dream_latents SET recurrence=3, last_used_at=NULL"
        )
        conn.commit()
        rows = DL.get_fallback_latents(conn, limit=3)
        self.assertGreaterEqual(len(rows), 1)
        conn.close()


if __name__ == '__main__':
    unittest.main()
