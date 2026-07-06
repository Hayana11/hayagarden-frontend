import os
import sqlite3
import tempfile
import unittest


class DesireLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "ledger.db")
        os.environ["MEMORIES_DB"] = self.db_path
        self._create_base_tables()
        import importlib
        import desire_ledger
        self.dl = importlib.reload(desire_ledger)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.dl.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def _create_base_tables(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thoughts TEXT,
                action TEXT,
                content TEXT,
                consumed INTEGER DEFAULT 0,
                woke_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def test_schema_adds_wake_log_column(self):
        cols = self.conn.execute("PRAGMA table_info(wake_log)").fetchall()
        names = {r[1] for r in cols}
        self.assertIn("surfaced_desire_ids", names)

    def test_surface_pool_rules(self):
        project1 = self.dl.add_desire("项目1", track="项目", conn=self.conn)["id"]
        project2 = self.dl.add_desire("项目2", track="项目", conn=self.conn)["id"]
        cooldown = self.dl.add_desire("冷却中", track="持续", conn=self.conn)["id"]
        never1 = self.dl.add_desire("从未浮过A", track="持续", conn=self.conn)["id"]
        never2 = self.dl.add_desire("从未浮过B", track="持续", conn=self.conn)["id"]
        other1 = self.dl.add_desire("常规1", track="持续", conn=self.conn)["id"]
        other2 = self.dl.add_desire("常规2", track="一次", conn=self.conn)["id"]

        self.conn.execute(
            "UPDATE desire_ledger SET cooldown_until=datetime('now','+8 hours','+1 day') WHERE id=?",
            (cooldown,),
        )
        self.conn.execute(
            "UPDATE desire_ledger SET surfaced_count=3, last_surfaced_at=datetime('now','+8 hours','-1 day') WHERE id=?",
            (other1,),
        )
        self.conn.execute(
            "UPDATE desire_ledger SET surfaced_count=1, last_surfaced_at=datetime('now','+8 hours','-2 day') WHERE id=?",
            (other2,),
        )
        self.conn.commit()

        rows = self.dl.surface(self.conn, limit=6, bump=False)
        picked_ids = {r["id"] for r in rows}
        picked_never = [r for r in rows if not r.get("last_surfaced_at") and r["track"] != "项目"]

        self.assertIn(project1, picked_ids)
        self.assertIn(project2, picked_ids)
        self.assertNotIn(cooldown, picked_ids)
        self.assertGreaterEqual(len(picked_never), 1)
        self.assertTrue({never1, never2} & picked_ids)

    def test_mark_surfaced_bumps_counts(self):
        did = self.dl.add_desire("要被浮出的条目", conn=self.conn)["id"]
        self.dl.mark_surfaced([did], conn=self.conn)
        row = self.conn.execute(
            "SELECT surfaced_count, last_surfaced_at FROM desire_ledger WHERE id=?",
            (did,),
        ).fetchone()
        self.assertEqual(row["surfaced_count"], 1)
        self.assertIsNotNone(row["last_surfaced_at"])


if __name__ == "__main__":
    unittest.main()
