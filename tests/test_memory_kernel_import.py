"""B-1 legacy import: Ombre vault + posts -> fresh Kernel, read-only sources."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools import memory_kernel_import as importer
from tools.memory_kernel import MemoryKernel

NOW = "2026-10-21T12:00:00+00:00"


def bucket(vault: Path, rel: str, bucket_id: str, body: str, **meta) -> Path:
    fields = {"id": bucket_id, "name": meta.pop("name", bucket_id), "type": meta.pop("type", "dynamic"),
              "created": meta.pop("created", "2026-06-12T03:57:30"), "domain": meta.pop("domain", ["日常"]),
              "importance": meta.pop("importance", 5), "valence": 0.6, "arousal": 0.3,
              "activation_count": 7013, "last_active": "2026-10-21T10:03:00", **meta}
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {v}" for v in value)
        else:
            lines.append(f"{key}: {value!r}" if isinstance(value, str) else f"{key}: {value}")
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n---\n\n" + body + "\n", encoding="utf-8")
    return path


def posts_db(path: Path, rows) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT, content TEXT, author TEXT,"
        " created_at TEXT, pinned INTEGER DEFAULT 0, tags TEXT, resolved INTEGER DEFAULT 0,"
        " importance INTEGER, layer TEXT, summary_title TEXT)"
    )
    conn.executemany(
        "INSERT INTO posts (id, type, content, author, created_at, resolved) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


class ImportFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.posts = root / "memories.db"
        self.kernel = root / "kernel.db"
        bucket(self.vault, "dynamic/家庭/cats.md", "aaa111", "[[哈娅]]养了两只猫：[[多乐]]和岁岁。")
        bucket(self.vault, "permanent/居家/home.md", "bbb222", "我们的小家已经很完美了。", type="permanent", pinned=True)
        bucket(self.vault, "archive/未分类/gone.md", "ccc333", "【系统自检】工具链失效的排查方法", deleted_at="2026-10-21T11:30:00")
        bucket(self.vault, "archive/家庭/cats-dup.md", "eee555", "哈娅养了两只猫：多乐和岁岁", deleted_at="2026-10-21T11:30:00")
        bucket(self.vault, "permanent/呼吸间/now.md", "ddd444", "（摘要生成失败）", domain=["呼吸间"])
        (self.vault / "_ledger").mkdir()
        (self.vault / "_ledger" / "x.md").write_text("ignored", encoding="utf-8")
        posts_db(self.posts, [
            (1, "MEMORY", "哈娅养了两只猫：多乐和岁岁。", "fyodor", "2026-06-13 10:00:00", 0),
            (2, "FACT", "哈娅不喜欢老豆腐，喜欢内酯豆腐。", "fyodor", "2026-07-04 12:00:00", 0),
            (3, "DIARY", "日记不归这次导入。", "fyodor", "2026-07-05 12:00:00", 0),
            (4, "MEMORY", "哈娅不喜欢老豆腐，喜欢内酯豆腐!", "fyodor", "2026-07-05 12:00:00", 1),
            (5, "MEMORY", "【系统自检】工具链失效的排查方法。", "fyodor", "2026-06-29 12:00:00", 1),
            (6, "MEMORY", "Production direct acceptance probe 60aa9d4b", "fyodor", "2026-10-19 12:00:00", 0),
            (7, "MEMORY", "她说今天的 smoke 味烧烤不太好吃，但还是吃完了，因为是妹妹请客。" * 8, "fyodor", "2026-10-19 13:00:00", 0),
        ])

    def tearDown(self):
        self.tmp.cleanup()


class LoadTests(ImportFixture):
    def test_ombre_skips_deleted_system_and_underscore_dirs(self):
        items, skipped = importer.load_ombre_buckets(self.vault)
        self.assertEqual({i.key for i in items}, {"ombre:aaa111", "ombre:bbb222"})
        self.assertEqual(
            {s["key"]: s["reason"] for s in skipped},
            {"ombre:ccc333": "ombre_deleted", "ombre:ddd444": "ombre_system_bucket",
             "ombre:eee555": "ombre_deleted"},
        )
        cats = next(i for i in items if i.key == "ombre:aaa111")
        self.assertEqual(cats.text, "哈娅养了两只猫：多乐和岁岁。")
        self.assertIn("[[多乐]]", cats.raw)
        self.assertEqual(cats.created_at, "2026-06-12T03:57:30+00:00")
        self.assertNotIn("activation_count", cats.meta)
        self.assertNotIn("last_active", cats.meta)

    def test_posts_only_memory_types_and_beijing_time(self):
        items, _ = importer.load_legacy_posts(self.posts)
        self.assertEqual([i.key for i in items], ["posts:1", "posts:2", "posts:4", "posts:5", "posts:6", "posts:7"])
        self.assertEqual(items[0].created_at, "2026-06-13T10:00:00+08:00")

    def test_owner_cleanup_and_probes_carry_over_to_posts(self):
        live, deleted, _ = importer._scan_ombre(self.vault)
        posts, _ = importer.load_legacy_posts(self.posts)
        kept, skipped = importer.drop_cleaned_posts(posts, deleted, live)
        self.assertEqual(
            {s["key"]: s["reason"] for s in skipped},
            {"posts:5": "matches_ombre_deleted", "posts:6": "deployment_probe"},
        )
        # A long real memory that merely mentions "smoke" is kept.
        self.assertIn("posts:7", [k.key for k in kept])
        # posts:1 matches a deleted duplicate but also the surviving live bucket.
        self.assertIn("posts:1", [k.key for k in kept])

    def test_posts_db_is_opened_read_only(self):
        before = hashlib.sha256(self.posts.read_bytes()).hexdigest()
        importer.run(self.vault, self.posts, self.kernel, now=NOW)
        self.assertEqual(before, hashlib.sha256(self.posts.read_bytes()).hexdigest())


class GroupingTests(ImportFixture):
    def test_cross_source_and_near_duplicates_group(self):
        o, _ = importer.load_ombre_buckets(self.vault)
        p, _ = importer.load_legacy_posts(self.posts)
        groups = importer.group_duplicates(o + p)
        keys = sorted(sorted(m.key for m in g) for g in groups)
        self.assertEqual(keys, [
            ["ombre:aaa111", "posts:1"], ["ombre:bbb222"], ["posts:2", "posts:4"],
            ["posts:5"], ["posts:6"], ["posts:7"],
        ])

    def test_canonical_prefers_ombre(self):
        o, _ = importer.load_ombre_buckets(self.vault)
        p, _ = importer.load_legacy_posts(self.posts)
        groups = {frozenset(m.key for m in g): g for g in importer.group_duplicates(o + p)}
        self.assertEqual(importer._canonical(groups[frozenset({"ombre:aaa111", "posts:1"})]).key, "ombre:aaa111")
        # Without an Ombre copy: same length, so the earlier post wins.
        self.assertEqual(importer._canonical(groups[frozenset({"posts:2", "posts:4"})]).key, "posts:2")

    def test_unrelated_texts_stay_apart(self):
        a = importer.SourceItem("a", "posts", "x", "她喜欢吃干煸四季豆。", NOW)
        b = importer.SourceItem("b", "posts", "x", "她讨厌下雨天出门取快递。", NOW)
        self.assertFalse(importer._similar(a, b))


class KernelWriteTests(ImportFixture):
    def test_import_writes_evidence_states_and_formation_deltas(self):
        report = importer.run(self.vault, self.posts, self.kernel, now=NOW)
        # posts:5 (cleaned junk copy) and posts:6 (probe) are skipped.
        self.assertEqual(report["states"], 4)
        self.assertEqual(report["evidence"], 6)
        self.assertEqual(report["cross_source_groups"], 1)
        kernel = MemoryKernel(self.kernel)
        cats = kernel.get_state("st:ombre:aaa111:v1")
        self.assertEqual(cats.content, "哈娅养了两只猫：多乐和岁岁。")
        self.assertEqual(cats.evidence_refs, ("ev:ombre:aaa111", "ev:posts:1"))
        self.assertEqual(cats.created_at, "2026-06-12T03:57:30+00:00")
        self.assertEqual(cats.lineage_id, "lin:ombre:aaa111")
        self.assertNotIn("activation_count", cats.structured_value)
        ev = kernel.get_evidence("ev:ombre:aaa111")
        self.assertEqual(ev.origin_kind, "derived")
        self.assertIn("[[多乐]]", ev.content)
        self.assertEqual(ev.integrity, "sha256:" + hashlib.sha256(ev.content.encode()).hexdigest())
        [formation] = kernel.transitions_for_state("st:ombre:aaa111:v1")
        self.assertIsNone(formation.before_ref)
        self.assertEqual(formation.review_status, "imported_unreviewed")

    def test_fact_representation_and_resolution(self):
        importer.run(self.vault, self.posts, self.kernel, now=NOW)
        kernel = MemoryKernel(self.kernel)
        tofu = kernel.get_state("st:posts:2:v1")
        self.assertEqual(tofu.representation, "fact")
        self.assertEqual(tofu.structured_value["memory"]["type"], "FACT")
        # The permanent Ombre bucket keeps "core" semantics.
        self.assertEqual(kernel.get_state("st:ombre:bbb222:v1").structured_value["memory"]["layer"], "core")
        # Only one of the two members was resolved, so the State stays active.
        self.assertEqual(tofu.status, "active")
        home = kernel.get_state("st:ombre:bbb222:v1")
        self.assertTrue(home.structured_value["legacy"]["pinned"])

    def test_refuses_existing_kernel_database(self):
        importer.run(self.vault, self.posts, self.kernel, now=NOW)
        with self.assertRaises(FileExistsError):
            importer.run(self.vault, self.posts, self.kernel, now=NOW)

    def test_dry_run_writes_nothing(self):
        report = importer.run(self.vault, self.posts, None, now=NOW)
        self.assertFalse(self.kernel.exists())
        self.assertEqual(report["states"], 4)

    def test_deterministic_ids_across_runs(self):
        first = importer.run(self.vault, self.posts, None, now=NOW)
        second = importer.run(self.vault, self.posts, None, now=NOW)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
