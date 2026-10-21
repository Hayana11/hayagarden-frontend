"""B-3: Kernel-first writes, Ombre catch-up, and the runtime switch."""
from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from tests.test_memory_kernel_projection import ProjectionFixture, read_bucket
from tools import memory_kernel_projection as projection
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


class StoreFixture(ProjectionFixture):
    def setUp(self):
        super().setUp()
        shutil.copytree(self.vault, self.out)
        store.sync(self.kernel, self.out)


class RememberTests(StoreFixture):
    def test_kernel_first_then_projected_bucket(self):
        result = store.remember("她今天买了一只叫福禄的鹦鹉。", source="save_marker",
                                provenance={"turn_id": "t1"}, kernel_db=self.kernel, vault=self.out, now=NOW)
        self.assertEqual(result["status"], "CREATED")
        self.assertEqual(result["ombre_status"], "PROJECTED")
        state = MemoryKernel(self.kernel).get_state(result["state_id"])
        self.assertEqual(state.content, "她今天买了一只叫福禄的鹦鹉。")
        [evidence] = MemoryKernel(self.kernel).evidence_for_state(result["state_id"])
        self.assertEqual(evidence.source_ref, "t1")
        path, meta, body = read_bucket(self.out, result["bucket_id"])
        self.assertEqual(meta["kernel_state_id"], result["state_id"])
        self.assertEqual(body.strip(), "她今天买了一只叫福禄的鹦鹉。")

    def test_duplicate_is_not_written_twice(self):
        result = store.remember("[[哈娅]]养了两只猫：多乐和岁岁", source="save_marker",
                                kernel_db=self.kernel, vault=self.out)
        self.assertEqual(result["status"], "DEDUPLICATED")
        self.assertEqual(result["bucket_id"], "aaa111")

    def test_projection_failure_keeps_the_kernel_row_for_sync(self):
        with mock.patch.object(store.projection, "apply", side_effect=TimeoutError):
            result = store.remember("下雪那天她堆了一个很小的雪人。", source="save_marker",
                                    kernel_db=self.kernel, vault=self.out)
        self.assertEqual(result["ombre_status"], "PROJECTION_FAILED:TimeoutError")
        self.assertNotIn(result["bucket_id"], projection.index_vault(self.out))
        store.sync(self.kernel, self.out)
        self.assertIn(result["bucket_id"], projection.index_vault(self.out))

    def test_fact_core_and_pinned_never_become_dynamic(self):
        cases = {
            "fact": dict(memory_type="FACT", layer="core"),
            "core": dict(memory_type="MEMORY", layer="core"),
            "pinned": dict(memory_type="MEMORY", layer="recent", pinned=1),
            "plain": dict(memory_type="MEMORY", layer="long-term"),
        }
        placed = {}
        for name, semantics in cases.items():
            result = store.remember(f"{name}：她冬天手脚冰凉，要提醒她穿袜子。", source="save_memory",
                                    tags="health,daily", importance=8, kernel_db=self.kernel,
                                    vault=self.out, **semantics)
            _, meta, _ = read_bucket(self.out, result["bucket_id"])
            state = MemoryKernel(self.kernel).get_state(result["state_id"])
            placed[name] = (meta["type"], bool(meta.get("pinned")), state.structured_value["memory"])
            self.assertEqual(meta["tags"], ["health", "daily"])
            self.assertEqual(meta["importance"], 8)
        self.assertEqual(placed["fact"][0], "permanent")
        self.assertEqual(placed["core"][0], "permanent")
        self.assertEqual(placed["pinned"][:2], ("permanent", True))
        self.assertEqual(placed["plain"][0], "dynamic")
        self.assertEqual(placed["fact"][2], {"type": "FACT", "layer": "core", "pinned": False, "author": ""})
        self.assertEqual(MemoryKernel(self.kernel).get_state(
            store.remember("fact2：她不吃青椒。", source="x", memory_type="FACT", kernel_db=self.kernel,
                           vault=self.out)["state_id"]).representation, "fact")

    def test_non_memory_types_are_rejected(self):
        with self.assertRaises(ValueError):
            store.remember("日记", source="x", memory_type="DIARY", kernel_db=self.kernel, vault=self.out)

    def test_old_projection_stamp_is_rebaselined_not_treated_as_an_edit(self):
        path, meta, body = read_bucket(self.out, "aaa111")
        meta.pop("kernel_projection_version")
        meta["importance"] = 2
        path.write_text(projection._render(meta, body), encoding="utf-8")
        heads, _ = projection.select_heads(self.kernel)
        action = next(a for a in projection.plan(heads, self.out)["actions"] if a["bucket_id"] == "aaa111")
        self.assertEqual(action["op"], "update")

    def test_invalid_content(self):
        self.assertEqual(store.remember("  ", source="x", kernel_db=self.kernel, vault=self.out)["status"],
                         "INVALID_CONTENT")


class IngestTests(StoreFixture):
    def test_bucket_created_inside_ombre_becomes_a_lineage(self):
        target = self.out / "dynamic" / "日常" / "letter_fff666.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\nid: fff666\ntype: dynamic\ncreated: '2026-10-21T09:00:00'\nname: 信\n"
                          "domain:\n- 日常\n---\n\n给未来的她写了一封信。\n", encoding="utf-8")
        report = store.sync(self.kernel, self.out)
        self.assertEqual(report["ingested"]["created"], ["fff666"])
        state = MemoryKernel(self.kernel).get_state("st:ombre:fff666:v1")
        self.assertEqual(state.content, "给未来的她写了一封信。")
        [evidence] = MemoryKernel(self.kernel).evidence_for_state(state.state_id)
        self.assertEqual(evidence.occurred_at, "2026-10-21T09:00:00+00:00")
        self.assertIn("kernel_state_id", read_bucket(self.out, "fff666")[1])
        self.assertEqual(store.sync(self.kernel, self.out)["ingested"]["created"], [])

    def test_edit_inside_ombre_becomes_a_revision_with_delta(self):
        path, meta, body = read_bucket(self.out, "aaa111")
        path.write_text(projection._render(dict(meta, importance=9), body + "还有一只鹦鹉福禄。"), encoding="utf-8")
        report = store.sync(self.kernel, self.out)
        self.assertEqual(report["ingested"]["revised"], ["aaa111"])
        kernel = MemoryKernel(self.kernel)
        history = kernel.lineage_history("lin:ombre:aaa111")
        self.assertEqual([s.state_id for s in history], ["st:ombre:aaa111:v1", "st:ombre:aaa111:v2"])
        self.assertIn("鹦鹉福禄", history[-1].content)
        self.assertEqual(history[-1].structured_value["importance"], 9)
        [delta] = [d for d in kernel.transitions_for_state("st:ombre:aaa111:v2") if d.before_ref]
        self.assertEqual(delta.before_ref, "st:ombre:aaa111:v1")
        # The edit survives, now stamped with the new Kernel version.
        _, meta, body = read_bucket(self.out, "aaa111")
        self.assertIn("鹦鹉福禄", body)
        self.assertEqual(meta["kernel_state_id"], "st:ombre:aaa111:v2")
        self.assertEqual(store.status(self.kernel, self.out)["plan"], {"unchanged": 4})

    def test_failed_edit_ingest_leaves_no_orphan_and_retries_cleanly(self):
        path, meta, body = read_bucket(self.out, "aaa111")
        path.write_text(projection._render(meta, body + "还有一只鹦鹉福禄。"), encoding="utf-8")
        original = store.MemoryKernel._transition

        def crash_after_evidence(kernel, state, delta, **kwargs):
            # Fail inside the transaction, after Evidence would have been inserted.
            with kernel._write() as conn:
                for item in kwargs.get("evidence", ()):
                    kernel._insert(conn, item)
                raise sqlite3.OperationalError("disk I/O error")

        with mock.patch.object(store.MemoryKernel, "_transition", crash_after_evidence):
            first = store.sync(self.kernel, self.out)
        self.assertEqual(first["ingested"]["errors"][0]["stage"], "edited")
        conn = sqlite3.connect(self.kernel)
        orphans = conn.execute("SELECT count(*) FROM memory_kernel_evidence WHERE source_type='ombre_bucket_edit'").fetchone()[0]
        conn.close()
        self.assertEqual(orphans, 0)
        self.assertIs(store.MemoryKernel._transition, original)
        second = store.sync(self.kernel, self.out)
        self.assertEqual(second["ingested"]["revised"], ["aaa111"])

    def test_lifecycle_only_changes_never_reach_the_kernel(self):
        path, meta, body = read_bucket(self.out, "aaa111")
        path.write_text(projection._render(dict(meta, resolved=True, activation_count=1), body), encoding="utf-8")
        report = store.sync(self.kernel, self.out)
        self.assertEqual(report["ingested"], {"created": [], "revised": [], "errors": []})
        self.assertEqual(len(MemoryKernel(self.kernel).lineage_history("lin:ombre:aaa111")), 1)


class SwitchTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"MEMORY_KERNEL_DB": str(self.kernel),
                                                "MEMORY_KERNEL_OMBRE_VAULT": str(self.out)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_flag_values(self):
        self.assertFalse(store.write_enabled(self.posts))
        set_flag(self.posts, "1")
        self.assertTrue(store.write_enabled(self.posts))
        set_flag(self.posts, "0")
        self.assertFalse(store.write_enabled(self.posts))
        self.assertFalse(store.write_enabled(Path(self.tmp.name) / "missing.db"))

    def test_persist_memory_routes_to_kernel_only_when_on(self):
        from tools import memory_runtime_bridge as bridge

        set_flag(self.posts, "1")
        with mock.patch.object(bridge, "_write_legacy") as legacy, mock.patch.object(bridge, "_write_ombre") as ombre:
            result = bridge.persist_memory(str(self.posts), content="她说下次要去看海。", source="save_marker")
        legacy.assert_not_called()
        ombre.assert_not_called()
        self.assertEqual(result["status"], "CREATED")
        self.assertTrue(result["kernel_state_id"].startswith("st:k:"))
        self.assertIn(result["ombre_bucket_id"], projection.index_vault(self.out))

    def test_persist_memory_falls_back_to_legacy_when_kernel_breaks(self):
        from tools import memory_runtime_bridge as bridge

        set_flag(self.posts, "1")
        with mock.patch.object(store, "remember", side_effect=RuntimeError), \
                mock.patch.object(bridge, "_reserve", side_effect=RuntimeError("legacy path reached")):
            result = bridge.persist_memory(str(self.posts), content="她说下次要去看海。", source="save_marker")
        self.assertEqual(result["status"], "RECEIPT_FAILED")

    def test_write_memory_adapter_routes_to_kernel(self):
        from tools import memory_write_adapter

        set_flag(self.posts, "1")
        with mock.patch.object(memory_write_adapter.memory_tool, "save_memory") as legacy:
            result = memory_write_adapter.write_memory(str(self.posts), content="她最喜欢番茄味鱼粉。")
        legacy.assert_not_called()
        self.assertEqual(result["status"], "CREATED")
        self.assertFalse(result["deduplicated"])

    def test_cc_hides_ombre_creation_tools_only_when_on(self):
        from tools.memory_kernel_switch import kernel_owned_brain_writes

        env = {"HAYAGARDEN_CONFIG_DB_PATH": str(self.posts)}
        self.assertEqual(kernel_owned_brain_writes(env), ())
        set_flag(self.posts, "1")
        self.assertEqual(kernel_owned_brain_writes(env), ("mcp__brain__hold", "mcp__brain__grow"))


if __name__ == "__main__":
    unittest.main()
