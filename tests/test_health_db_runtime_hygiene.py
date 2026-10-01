from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class HealthDbRuntimeHygieneTests(unittest.TestCase):
    def test_gitignore_covers_health_db_and_wal_companions(self):
        lines = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        }
        self.assertIn("/health.db", lines)
        self.assertIn("/health.db-shm", lines)
        self.assertIn("/health.db-wal", lines)
        self.assertNotIn("*.db", lines)

    def test_backup_uses_sqlite_online_backup_for_health_db(self):
        source = (ROOT / "tools" / "backup.sh").read_text(encoding="utf-8")
        self.assertIn(
            'if [[ -f /opt/frontend/health.db ]]; then',
            source,
        )
        expected = "sqlite3 /opt/frontend/health.db " + chr(34) + ".backup '$TMP/health.db'" + chr(34)
        self.assertIn(expected, source)
        self.assertNotIn("cp /opt/frontend/health.db", source)


if __name__ == "__main__":
    unittest.main()
