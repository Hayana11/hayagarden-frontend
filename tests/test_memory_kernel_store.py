"""B-3: Kernel-first writes, Ombre catch-up, and the runtime switch."""
from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests.test_memory_kernel_projection import ProjectionFixture
from tools import memory_kernel_store as store
from tools.memory_kernel import MemoryKernel

NOW = "2026-10-21T13:00:00+00:00"


def set_flag(db: Path, value: str | None) -> None:
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS runtime_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    if value is None:
        conn.execute("DELETE FROM runtime_config WHERE key=?", (store.FLAG_KEY,))
    else:
        conn.execute("INSERT OR REPLACE INTO runtime_config (key, value) VALUES (?, ?)", (store.FLAG_KEY, value))
    conn.commit()
    conn.close()


def count_posts(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM posts").fetchone()[0]
    finally:
        conn.close()


class StoreFixture(ProjectionFixture):
    def setUp(self):
        super().setUp()
        store.sync(self.kernel, self.posts, runtime=self.ombre)
        self.ombre.calls.clear()
        self.ombre.created.clear()

    def remember(self, content, **kwargs):
        kwargs.setdefault("source", "save_marker")
        kwargs.setdefault("runtime", self.ombre)
        return store.remember(content, kernel_db=self.kernel, **kwargs)

    def sync(self):
        return store.sync(self.kernel, self.posts, runtime=self.ombre)


class RememberTests(StoreFixture):
    def test_kernel_first_then_projected_bucket(self):
        result = self.remember("她今天买了一只叫福禄的鹦鹉。", provenance={"turn_id": "t1"}, now=NOW)
        self.assertEqual((result["status"], result["ombre_status"]), ("CREATED", "PROJECTED"))
        kernel = MemoryKernel(self.kernel)
        self.assertEqual(kernel.get_state(result["state_id"]).content, "她今天买了一只叫福禄的鹦鹉。")
        [evidence] = kernel.evidence_for_state(result["state_id"])
        self.assertEqual(evidence.source_ref, "t1")
        bucket = self.ombre.buckets[result["bucket_id"]]
        self.assertEqual(bucket["content"], "她今天买了一只叫福禄的鹦鹉。")
        self.assertEqual(bucket["metadata"]["source_tool"], "kernel_projection")
        self.assertEqual(self.ombre.calls, [("create_if_absent", result["bucket_id"])])  # one worker run
        self.assertEqual(self.state.get(result["lineage_id"])["projected_state_id"], result["state_id"])
        self.assertNotIn("name", bucket["metadata"])  # untitled: Ombre picks the name

    def test_duplicate_is_not_written_twice(self):
        result = self.remember("[[哈娅]]养了两只猫：多乐和岁岁")
        self.assertEqual((result["status"], result["bucket_id"]), ("DEDUPLICATED", "aaa111"))
        self.assertEqual(self.ombre.calls, [])

    def test_ombre_down_or_unconfigured_leaves_pending_never_posts(self):
        posts_before = count_posts(self.posts)
        self.ombre.down = True
        down = self.remember("下雪那天她堆了一个很小的雪人。")
        unconfigured = self.remember("她把雪人的鼻子做成了胡萝卜。", runtime=None)
        self.assertEqual((down["status"], down["ombre_status"]), ("CREATED", "PENDING"))
        self.assertEqual(unconfigured["ombre_status"], "PENDING:unconfigured")
        self.assertEqual(store.status(self.kernel)["projection"].get("pending"), 2)
        self.assertEqual(count_posts(self.posts), posts_before)
        self.ombre.down = False
        self.assertEqual(self.sync()["projected"]["projected"], 2)
        self.assertIn(down["bucket_id"], self.ombre.buckets)
        self.assertIn(unconfigured["bucket_id"], self.ombre.buckets)

    def test_bookkeeping_crash_after_commit_never_raises(self):
        with mock.patch.object(store.projection.ProjectionState, "projected", side_effect=sqlite3.OperationalError):
            result = self.remember("她说冬天要一起去看雪。")
        self.assertEqual((result["status"], result["ombre_status"]), ("CREATED", "PENDING:OperationalError"))
        self.sync()
        self.assertEqual(self.ombre.creates(), [result["bucket_id"]])
        self.assertEqual(self.state.get(result["lineage_id"])["status"], "projected")

    def test_fact_core_and_pinned_never_become_dynamic(self):
        cases = {
            "fact": dict(memory_type="FACT", layer="core"),
            "core": dict(memory_type="MEMORY", layer="core"),
            "pinned": dict(memory_type="MEMORY", layer="recent", pinned=1),
            "plain": dict(memory_type="MEMORY", layer="long-term"),
        }
        placed = {}
        for name, semantics in cases.items():
            result = self.remember(f"{name}：她冬天手脚冰凉，要提醒她穿袜子。", source="save_memory",
                                   tags="health,daily", importance=8, **semantics)
            meta = self.ombre.buckets[result["bucket_id"]]["metadata"]
            state = MemoryKernel(self.kernel).get_state(result["state_id"])
            placed[name] = (meta["type"], bool(meta.get("pinned")), meta["importance"], state.structured_value["memory"])
            self.assertEqual(meta["tags"], ["health", "daily"])
        self.assertEqual(placed["fact"][:3], ("permanent", False, 8))
        self.assertEqual(placed["core"][0], "permanent")
        self.assertEqual(placed["pinned"][:3], ("permanent", True, 10))  # Ombre's normalisation
        self.assertEqual(placed["plain"][:3], ("dynamic", False, 8))
        self.assertEqual(placed["fact"][3], {"type": "FACT", "layer": "core", "pinned": False, "author": ""})
        fact = self.remember("fact2：她不吃青椒。", memory_type="FACT")
        self.assertEqual(MemoryKernel(self.kernel).get_state(fact["state_id"]).representation, "fact")

    def test_non_memory_types_are_rejected(self):
        with self.assertRaises(ValueError):
            self.remember("日记", memory_type="DIARY")

    def test_invalid_content(self):
        self.assertEqual(self.remember("  ")["status"], "INVALID_CONTENT")


class IngestTests(StoreFixture):
    def test_bucket_created_inside_ombre_becomes_a_lineage(self):
        self.ombre.buckets["fff666"] = {"id": "fff666", "content": "给未来的她写了一封信。", "metadata": {
            "id": "fff666", "type": "dynamic", "created": "2026-10-21T09:00:00", "name": "信", "domain": ["日常"],
            "importance": 6, "valence": 0.7, "arousal": 0.4}}
        report = self.sync()
        self.assertEqual(report["ingested"]["created"], ["fff666"])
        state = MemoryKernel(self.kernel).get_state("st:ombre:fff666:v1")
        self.assertEqual(state.content, "给未来的她写了一封信。")
        [evidence] = MemoryKernel(self.kernel).evidence_for_state(state.state_id)
        self.assertEqual(evidence.occurred_at, "2026-10-21T09:00:00+00:00")
        self.assertEqual(self.state.get("lin:ombre:fff666")["status"], "projected")
        self.assertNotIn("fff666", self.ombre.creates())
        self.assertEqual(self.sync()["ingested"]["created"], [])

    def test_edit_inside_ombre_becomes_a_revision_with_delta(self):
        self.ombre.edit("aaa111", content="[[哈娅]]养了两只猫：[[多乐]]和岁岁。还有一只鹦鹉福禄。", importance=9)
        report = self.sync()
        self.assertEqual(report["ingested"]["revised"], ["aaa111"])
        kernel = MemoryKernel(self.kernel)
        history = kernel.lineage_history("lin:ombre:aaa111")
        self.assertEqual([s.state_id for s in history], ["st:ombre:aaa111:v1", "st:ombre:aaa111:v2"])
        self.assertIn("鹦鹉福禄", history[-1].content)
        self.assertEqual(history[-1].structured_value["importance"], 9)
        [delta] = [d for d in kernel.transitions_for_state("st:ombre:aaa111:v2") if d.before_ref]
        self.assertEqual(delta.before_ref, "st:ombre:aaa111:v1")
        # The edit survives untouched and is now the observed baseline.
        self.assertNotIn(("update", "aaa111"), self.ombre.calls)
        self.assertIn("[[多乐]]", self.ombre.buckets["aaa111"]["content"])
        self.assertEqual(self.state.get("lin:ombre:aaa111")["projected_state_id"], "st:ombre:aaa111:v2")
        self.assertEqual(store.status(self.kernel)["projection"], {"projected": len(self.heads())})
        self.assertEqual(self.sync()["ingested"]["revised"], [])

    def test_failed_edit_ingest_leaves_no_orphan_and_retries_cleanly(self):
        self.ombre.edit("aaa111", content="哈娅养了两只猫：多乐和岁岁。还有一只鹦鹉福禄。")
        original = store.MemoryKernel._transition

        def crash_after_evidence(kernel, state, delta, **kwargs):
            # Fail inside the transaction, after Evidence would have been inserted.
            with kernel._write() as conn:
                for item in kwargs.get("evidence", ()):
                    kernel._insert(conn, item)
                raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(store.MemoryKernel, "_transition", crash_after_evidence):
            first = self.sync()
        self.assertEqual(first["ingested"]["errors"][0]["stage"], "edited")
        conn = sqlite3.connect(self.kernel)
        orphans = conn.execute("SELECT count(*) FROM memory_kernel_evidence WHERE source_type='ombre_bucket_edit'").fetchone()[0]
        conn.close()
        self.assertEqual(orphans, 0)
        self.assertIs(store.MemoryKernel._transition, original)
        self.assertEqual(self.sync()["ingested"]["revised"], ["aaa111"])

    def test_lifecycle_only_changes_never_reach_the_kernel(self):
        self.ombre.edit("aaa111", resolved=True, activation_count=1)
        report = self.sync()
        self.assertEqual(report["ingested"], {"created": [], "revised": [], "deleted": [], "errors": []})
        self.assertEqual(len(MemoryKernel(self.kernel).lineage_history("lin:ombre:aaa111")), 1)

    def test_crash_after_our_update_is_not_ingested_as_an_ombre_edit(self):
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        with mock.patch.object(store.projection.ProjectionState, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                self.sync()
        report = self.sync()
        self.assertEqual(report["ingested"]["revised"], [])
        self.assertEqual(len(MemoryKernel(self.kernel).lineage_history("lin:ombre:aaa111")), 2)
        self.assertEqual(self.state.get("lin:ombre:aaa111")["projected_state_id"], "st:ombre:aaa111:v2")

    def test_ombre_unavailable_during_sync_is_reported_not_raised(self):
        self.ombre.down = True
        self.assertTrue(self.sync()["ombre"].startswith("unavailable:"))
        self.assertEqual(store.sync(self.kernel, self.posts, runtime=None)["ombre"], "unconfigured")


class RouterTests(StoreFixture):
    """memory_tool.save_memory is the single write router for memory types."""

    def setUp(self):
        super().setUp()
        from tools import memory_tool, summary_title

        self.memory_tool = memory_tool
        for patcher in (
            mock.patch.dict(os.environ, {"MEMORY_KERNEL_DB": str(self.kernel)}),
            mock.patch.object(store, "default_runtime", return_value=self.ombre),
            mock.patch.object(memory_tool, "DB_PATH", str(self.posts)),
            mock.patch.object(summary_title, "generate_summary_title", return_value=""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def posts_rows(self, where="1=1"):
        conn = sqlite3.connect(self.posts)
        try:
            return conn.execute(f"SELECT id, type, content FROM posts WHERE {where}").fetchall()
        finally:
            conn.close()

    def test_flag_values(self):
        self.assertFalse(store.write_enabled(self.posts))
        set_flag(self.posts, "1")
        self.assertTrue(store.write_enabled(self.posts))
        set_flag(self.posts, "0")
        self.assertFalse(store.write_enabled(self.posts))
        self.assertFalse(store.write_enabled(Path(self.tmp.name) / "missing.db"))

    def test_switch_off_keeps_posts_and_returns_post_id(self):
        post_id = self.memory_tool.save_memory("她说下次要去看海。", type="MEMORY")
        self.assertIsInstance(post_id, int)
        self.assertEqual(self.posts_rows(f"id={post_id}")[0][2], "她说下次要去看海。")

    def test_memory_types_go_to_the_kernel_with_their_semantics(self):
        set_flag(self.posts, "1")
        before = len(self.posts_rows())
        written = self.memory_tool.save_memory(
            "她不吃青椒，嫌味道怪。", type="FACT", layer="core", tags="food", importance=8,
            pinned=1, author="fyodor", created_at="2026-10-15 23:10:00", source="fact_extractor")
        self.assertIsInstance(written, self.memory_tool.KernelWrite)
        self.assertEqual(written.ombre_status, "PROJECTED")
        self.assertEqual(len(self.posts_rows()), before)
        state = MemoryKernel(self.kernel).get_state(written.state_id)
        self.assertEqual(state.representation, "fact")
        self.assertEqual(state.structured_value["memory"],
                         {"type": "FACT", "layer": "core", "pinned": True, "author": "fyodor"})
        self.assertEqual(state.created_at, "2026-10-15T23:10:00+08:00")
        meta = self.ombre.buckets[written.bucket_id]["metadata"]
        self.assertEqual((meta["type"], meta["pinned"]), ("permanent", True))

    def test_ombre_down_is_still_a_kernel_write_not_a_posts_fallback(self):
        set_flag(self.posts, "1")
        self.ombre.down = True
        written = self.memory_tool.save_memory("她把伞借给了邻居。", type="MEMORY")
        self.assertIsInstance(written, self.memory_tool.KernelWrite)
        self.assertEqual(written.ombre_status, "PENDING")
        self.assertEqual(self.posts_rows("content='她把伞借给了邻居。'"), [])

    def test_non_memory_types_stay_in_posts_even_when_on(self):
        set_flag(self.posts, "1")
        for kind in ("DIARY", "DREAM", "THOUGHT", "DAILY_SUMMARY", "WEEKLY_SUMMARY", "PHOTO"):
            post_id = self.memory_tool.save_memory(f"{kind} 的内容", type=kind)
            self.assertIsInstance(post_id, int, kind)

    def test_kernel_failure_falls_back_once_and_is_backfilled_by_sync(self):
        set_flag(self.posts, "1")
        with mock.patch.object(store, "remember", side_effect=RuntimeError("kernel down")) as kernel, \
                mock.patch.object(self.memory_tool, "save_memory", wraps=self.memory_tool.save_memory) as router:
            post_id = self.memory_tool.save_memory("她把围巾落在了公交车上。", type="MEMORY", layer="long-term")
        self.assertIsInstance(post_id, int)
        self.assertEqual(kernel.call_count, 1)
        self.assertEqual(router.call_count, 1)  # the fallback never re-enters the router
        self.assertEqual(len(self.posts_rows("content='她把围巾落在了公交车上。'")), 1)
        conn = sqlite3.connect(self.posts)
        queued = conn.execute("SELECT post_id, done_at FROM memory_kernel_backfill").fetchall()
        conn.close()
        self.assertEqual(queued, [(post_id, None)])

        report = self.sync()
        self.assertEqual(report["backfilled"]["done"], [post_id])
        conn = sqlite3.connect(self.posts)
        state_id = conn.execute("SELECT state_id FROM memory_kernel_backfill").fetchone()[0]
        conn.close()
        [evidence] = MemoryKernel(self.kernel).evidence_for_state(state_id)
        self.assertEqual(evidence.source_ref, f"posts:{post_id}")
        self.assertEqual(MemoryKernel(self.kernel).get_state(state_id).structured_value["memory"]["layer"],
                         "long-term")
        self.assertEqual(self.sync()["backfilled"]["done"], [])

    def test_fallback_post_and_backfill_entry_commit_together(self):
        set_flag(self.posts, "1")
        conn = sqlite3.connect(self.posts)
        conn.execute("CREATE TABLE memory_kernel_backfill (post_id INTEGER PRIMARY KEY)")  # entry can't be written
        conn.commit()
        conn.close()
        before = len(self.posts_rows())
        with mock.patch.object(store, "remember", side_effect=RuntimeError("kernel down")):
            with self.assertRaises(sqlite3.OperationalError):
                self.memory_tool.save_memory("她把钥匙放在了鞋柜上。", type="MEMORY")
        # The post rolled back with it: no memory stranded in posts without a backfill entry.
        self.assertEqual(len(self.posts_rows()), before)

    def test_receipt_recovers_after_crash_between_kernel_commit_and_receipt(self):
        from tools import memory_runtime_bridge as bridge

        set_flag(self.posts, "1")
        kwargs = dict(content="她说周末想去爬山。", source="save_marker", provenance={"turn_id": "t9"})
        with mock.patch.object(bridge, "_commit_kernel_receipt", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):  # the process dies here
                bridge.persist_memory(str(self.posts), **kwargs)
        retry = bridge.persist_memory(str(self.posts), **kwargs)
        self.assertEqual(retry["status"], "DEDUPLICATED")
        heads = [h for h in self.heads() if h.state.content == "她说周末想去爬山。"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(retry["kernel_state_id"], heads[0].state.state_id)
        self.assertEqual(bridge.persist_memory(str(self.posts), **kwargs)["status"], "DEDUPLICATED")

    def test_receipt_does_not_claim_kernel_content_while_the_switch_is_off(self):
        from tools import memory_runtime_bridge as bridge

        set_flag(self.posts, "1")
        self.memory_tool.save_memory("她喜欢下雨天听歌。", type="MEMORY")
        set_flag(self.posts, "0")
        self.assertIsNone(self.memory_tool.find_kernel_memory("她喜欢下雨天听歌。"))
        result = bridge.persist_memory(str(self.posts), content="她喜欢下雨天听歌。", source="save_marker")
        self.assertIsInstance(result["legacy_memory_id"], int)

    def test_persist_memory_goes_through_the_router(self):
        from tools import memory_runtime_bridge as bridge

        set_flag(self.posts, "1")
        with mock.patch.object(bridge, "_write_ombre") as ombre:
            result = bridge.persist_memory(str(self.posts), content="她说下次要去看海。", source="save_marker",
                                           tags="save-marker", importance=4)
        ombre.assert_not_called()  # the Kernel projects; no second Ombre write
        self.assertEqual(result["status"], "CREATED")
        self.assertIsNone(result["legacy_memory_id"])
        self.assertIn(result["ombre_bucket_id"], self.ombre.buckets)
        self.assertEqual(self.posts_rows("content='她说下次要去看海。'"), [])
        [evidence] = MemoryKernel(self.kernel).evidence_for_state(result["kernel_state_id"])
        self.assertEqual(evidence.source_type, "memory_write:save_marker")

    def test_write_memory_adapter_goes_through_the_router(self):
        from tools import memory_write_adapter

        set_flag(self.posts, "1")
        result = memory_write_adapter.write_memory(str(self.posts), content="她最喜欢番茄味鱼粉。")
        self.assertEqual((result["status"], result["store"]), ("CREATED", "kernel"))
        self.assertNotIn("id", result)
        self.assertEqual(self.posts_rows("content='她最喜欢番茄味鱼粉。'"), [])

    def test_cc_hides_ombre_creation_tools_only_when_on(self):
        from tools.memory_kernel_switch import kernel_owned_brain_writes

        env = {"HAYAGARDEN_CONFIG_DB_PATH": str(self.posts)}
        self.assertEqual(kernel_owned_brain_writes(env), ())
        set_flag(self.posts, "1")
        self.assertEqual(kernel_owned_brain_writes(env), ("mcp__brain__hold", "mcp__brain__grow"))


if __name__ == "__main__":
    unittest.main()
