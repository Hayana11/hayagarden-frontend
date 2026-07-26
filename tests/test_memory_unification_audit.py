from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import frontmatter

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.memory_unification_audit import build_report, normalize_content


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MemoryUnificationAuditTests(unittest.TestCase):
    def test_audit_matches_wikilink_copy_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "memories.db"
            vault = root / "vault"
            (vault / "dynamic" / "技术").mkdir(parents=True)
            (vault / "permanent" / "未分类").mkdir(parents=True)

            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE posts (id INTEGER PRIMARY KEY, content TEXT, type TEXT, "
                "layer TEXT, tags TEXT, importance INTEGER, pinned INTEGER, resolved INTEGER, created_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (1, "我们修好了渐变脑", "MEMORY", "long-term", "技术", 8, 0, 0, "2026-07-25"),
                    (2, "只在前端", "MEMORY", "recent", "日常", 4, 0, 0, "2026-07-25"),
                    (3, "只在前端", "MEMORY", "recent", "日常", 4, 0, 0, "2026-07-25"),
                ],
            )
            conn.commit()
            conn.close()

            shared = frontmatter.Post(
                "我们修好了[[渐变脑]]",
                id="ombre-shared", name="技术修复", type="dynamic",
                domain=["技术"], tags=["渐变脑"], importance=8,
            )
            duplicate_a = frontmatter.Post(
                "Ombre duplicate",
                id="dup-a", name="aaaaaaaaaaaa", type="permanent",
                domain=["未分类"], pinned=True, importance=10,
            )
            duplicate_b = frontmatter.Post(
                "Ombre duplicate",
                id="dup-b", name="bbbbbbbbbbbb", type="dynamic",
                domain=["未分类"], importance=5,
            )
            paths = [
                vault / "dynamic" / "技术" / "shared.md",
                vault / "permanent" / "未分类" / "dup-a.md",
                vault / "dynamic" / "技术" / "dup-b.md",
            ]
            for path, post in zip(paths, [shared, duplicate_a, duplicate_b]):
                path.write_text(frontmatter.dumps(post), encoding="utf-8")

            before = {str(path): sha(path) for path in [db, *paths]}
            report = build_report(str(db), str(vault))
            after = {str(path): sha(path) for path in [db, *paths]}

            self.assertEqual(before, after)
            self.assertEqual(normalize_content("我们修好了[[渐变脑]]"), normalize_content("我们修好了渐变脑"))
            self.assertEqual(report["counts"]["sqlite"], 3)
            self.assertEqual(report["counts"]["ombre"], 3)
            self.assertEqual(report["counts"]["cross_matched_content"], 1)
            self.assertEqual(report["counts"]["sqlite_only_content"], 1)
            self.assertEqual(report["counts"]["ombre_only_content"], 1)
            self.assertEqual(report["counts"]["ombre_pinned"], 1)
            self.assertEqual(report["counts"]["ombre_hash_titles"], 2)
            self.assertEqual(report["counts"]["ombre_unclassified"], 2)
            self.assertEqual(report["sqlite_duplicate_groups"][0]["count"], 2)
            self.assertEqual(report["ombre_duplicate_groups"][0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
