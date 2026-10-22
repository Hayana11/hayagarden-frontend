"""B-4: memory types are read from the Kernel (Ombre only ranks)."""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from unittest import mock

from tests.test_memory_kernel_store import StoreFixture
from tools import memory_kernel_read as reader
from tools import memory_kernel_store as store
from tools.memory_kernel import Delta, MemoryKernel

NOW = "2026-10-21T14:00:00+00:00"


class ReadFixture(StoreFixture):
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

    def search(self, query, ranked=(), **kwargs):
        ombre = (lambda q, n: list(ranked)) if ranked is not None else (lambda q, n: None)
        return reader.search(query, self.posts, self.kernel, ombre=ombre, **kwargs)

    def revise(self, lineage_id, content, *, value=None):
        kernel = MemoryKernel(self.kernel)
        [head] = [h for h in reader.list_current(self.kernel, active_only=False) if h["lineage_id"] == lineage_id]
        before = kernel.get_state(head["state_id"])
        after = dataclasses.replace(
            before, state_id=before.state_id + "+", content=content, created_at=NOW,
            structured_value=value if value is not None else before.structured_value)
        kernel.revise_state(after, Delta(
            delta_id="dl:" + after.state_id, target_scope=before.scope, before_ref=before.state_id,
            after_ref=after.state_id, trigger_evidence_refs=before.evidence_refs,
            derived_by={"processor": "test"}, confidence=0.5, occurred_at=NOW, review_status="accepted"))

    def fallback(self, content, memory_type="MEMORY"):
        return self.memory_tool._legacy_save(
            content, type=memory_type, author="fyodor", layer="recent", tags="", importance=0,
            pinned=0, created_at=None, valence=None, arousal=None, backfill_reason="kernel_error")

    def add_post(self, post_id, kind, content, created="2026-10-20 09:00:00"):
        conn = sqlite3.connect(self.posts)
        conn.execute("INSERT INTO posts (id, type, content, author, created_at) VALUES (?,?,?,?,?)",
                     (post_id, kind, content, "fyodor", created))
        conn.commit()
        conn.close()


class KernelReadTests(ReadFixture):
    def test_heads_are_typed_and_carry_stable_refs(self):
        items = {i["lineage_id"]: i for i in reader.list_current(self.kernel, active_only=False)}
        self.assertEqual(items["lin:posts:2"]["type"], "FACT")
        self.assertEqual(items["lin:ombre:aaa111"]["ref"], "kernel:lin:ombre:aaa111")
        self.assertEqual(items["lin:ombre:aaa111"]["bucket_id"], "aaa111")
        self.assertTrue(items["lin:ombre:aaa111"]["projected"])
        self.assertEqual(items["lin:ombre:aaa111"]["created_at"], "2026-06-12 11:57:30")  # Beijing
        self.assertEqual([i["type"] for i in reader.list_current(self.kernel, memory_type="FACT")], ["FACT"])

    def test_layer_and_tag_come_from_structured_memory(self):
        from tests.test_memory_kernel_store import set_flag

        set_flag(self.posts, "1")
        self.memory_tool.save_memory("她说网页窗口里聊了搬家的事，定在周六。", type="MEMORY", tags="memo",
                                     layer="long-term")
        self.memory_tool.save_memory("她说周六要早起。", type="MEMORY", layer="long-term")
        self.assertEqual(len(reader.list_current(self.kernel, layer="long-term")), 2)
        [memo] = reader.list_current(self.kernel, tag="memo", layer="long-term")
        self.assertEqual(memo["content"], "她说网页窗口里聊了搬家的事，定在周六。")
        self.assertEqual(memo["tag_list"], ["memo"])

    def test_old_import_without_memory_semantics_is_typed_by_representation(self):
        [cats] = [h for h in reader.list_current(self.kernel) if h["lineage_id"] == "lin:ombre:aaa111"]
        value = {k: v for k, v in MemoryKernel(self.kernel).get_state(cats["state_id"]).structured_value.items()
                 if k != "memory"}
        self.revise("lin:ombre:aaa111", "哈娅养了两只猫。", value=value)
        [cats] = [h for h in reader.list_current(self.kernel) if h["lineage_id"] == "lin:ombre:aaa111"]
        self.assertEqual((cats["type"], cats["layer"]), ("MEMORY", "recent"))

    def test_resolved_heads_are_hidden_unless_asked(self):
        kernel = MemoryKernel(self.kernel)
        [home] = [h for h in reader.list_current(self.kernel) if h["lineage_id"] == "lin:ombre:bbb222"]
        before = kernel.get_state(home["state_id"])
        kernel.revise_state(dataclasses.replace(before, state_id="st:resolved", status="resolved", created_at=NOW),
                            Delta(delta_id="dl:resolved", target_scope=before.scope, before_ref=before.state_id,
                                  after_ref="st:resolved", trigger_evidence_refs=before.evidence_refs,
                                  derived_by={"processor": "test"}, confidence=0.5, occurred_at=NOW,
                                  review_status="accepted"))
        self.assertNotIn("lin:ombre:bbb222", {h["lineage_id"] for h in reader.list_current(self.kernel)})
        self.assertIn("lin:ombre:bbb222",
                      {h["lineage_id"] for h in reader.list_current(self.kernel, active_only=False)})

    def test_plans_and_letters_are_not_memories(self):
        written = store.remember("下周三要交稿。", source="test", kernel_db=self.kernel, runtime=None)
        kernel = MemoryKernel(self.kernel)
        state = kernel.get_state(written["state_id"])
        value = dict(state.structured_value, legacy={"ombre_type": "plan"})
        self.revise(written["lineage_id"], "下周三要交稿。", value=value)
        self.assertNotIn(written["lineage_id"], {h["lineage_id"] for h in reader.list_current(self.kernel)})
        self.assertEqual(self.search("交稿", ranked=[written["bucket_id"]]), [])


class SearchTests(ReadFixture):
    def test_ombre_hit_returns_the_kernel_head_not_ombre_text(self):
        self.revise("lin:ombre:aaa111", "哈娅养了两只猫，还有一只叫福禄的鹦鹉。")
        self.assertIn("多乐", self.ombre.buckets["aaa111"]["content"])  # Ombre still lags
        [hit] = self.search("鹦鹉福禄", ranked=["aaa111"])
        self.assertEqual(hit["content"], "哈娅养了两只猫，还有一只叫福禄的鹦鹉。")
        self.assertEqual(hit["ref"], "kernel:lin:ombre:aaa111")

    def test_ombre_order_comes_first(self):
        hits = self.search("家", ranked=["bbb222", "aaa111"])
        self.assertEqual([h["bucket_id"] for h in hits[:2]], ["bbb222", "aaa111"])

    def test_kernel_written_but_ombre_pending_is_still_found(self):
        self.ombre.down = True
        written = store.remember("她给阳台的薄荷换了大盆。", source="test", kernel_db=self.kernel,
                                 runtime=self.ombre)
        self.assertEqual(written["ombre_status"], "PENDING")
        [hit] = self.search("薄荷", ranked=[])
        self.assertEqual(hit["lineage_id"], written["lineage_id"])

    def test_projected_heads_are_not_double_matched_when_ombre_answers(self):
        self.assertEqual(self.search("小家", ranked=[]), [])

    def test_ombre_unavailable_never_silently_drops_kernel_memories(self):
        [hit] = self.search("小家", ranked=None)
        self.assertEqual(hit["lineage_id"], "lin:ombre:bbb222")
        boom = mock.Mock(side_effect=RuntimeError("down"))
        [hit] = reader.search("小家", self.posts, self.kernel, ombre=boom)
        self.assertEqual(hit["lineage_id"], "lin:ombre:bbb222")

    def test_pending_fallback_is_visible_and_disappears_once_backfilled(self):
        post_id = self.fallback("她半夜给我留了一盏小灯。")
        [hit] = self.search("小灯")
        self.assertEqual((hit["ref"], hit["source"]), (f"post:{post_id}", "posts_fallback"))
        self.sync()
        [hit] = self.search("小灯", ranked=None)
        self.assertEqual(hit["source"], "kernel")
        self.assertEqual(len(reader.memory_items(self.posts, self.kernel)),
                         len({i["ref"] for i in reader.memory_items(self.posts, self.kernel)}))

    def test_kernel_copy_of_a_diary_is_dropped(self):
        self.add_post(30, "DIARY", "今天和费佳一起去看了海。")
        store.remember("今天和费佳一起去看了海。", source="ombre_ingest", kernel_db=self.kernel, runtime=None)
        self.assertEqual(self.search("看了海", ranked=None), [])
        self.assertNotIn("今天和费佳一起去看了海。", [i["content"] for i in reader.memory_items(self.posts, self.kernel)])

    def test_never_touches(self):
        self.ombre.calls.clear()
        self.ombre.runs = 0
        self.search("小家", ranked=["bbb222"])
        reader.memory_items(self.posts, self.kernel)
        self.assertEqual(self.ombre.calls, [])
        self.assertEqual(self.ombre.runs, 0)


class TouchTests(ReadFixture):
    def test_touch_goes_through_the_official_runtime_once(self):
        runtime = mock.Mock()
        reader.touch_buckets(["bbb222", "aaa111", "aaa111", ""], runtime=runtime, wait=True)
        runtime.batch.assert_called_once_with([{"op": "touch", "bucket_ids": ["aaa111", "bbb222"]}])

    def test_no_runtime_means_no_reinforcement(self):
        self.assertIsNone(reader.touch_buckets(["aaa111"], runtime=None))
        self.assertIsNone(reader.touch_buckets([], runtime=mock.Mock()))

    def test_runtime_failure_is_swallowed(self):
        runtime = mock.Mock()
        runtime.batch.side_effect = RuntimeError("worker down")
        reader.touch_buckets(["aaa111"], runtime=runtime, wait=True)


class SwitchTests(ReadFixture):
    def test_cutover_reads_the_same_flag_as_writes(self):
        from tests.test_memory_kernel_store import set_flag

        self.assertFalse(reader.cutover_enabled(self.posts))
        set_flag(self.posts, "1")
        self.assertTrue(reader.cutover_enabled(self.posts))
        self.assertEqual(json.dumps(store.FLAG_KEY), '"memory.kernel_write"')


class LifecycleTests(ReadFixture):
    """A memory Ombre retired (archived, deleted, dont_surface, digested) never comes back."""

    def visible(self, lineage_id):
        everywhere = [
            {h["lineage_id"] for h in reader.list_current(self.kernel, active_only=False)},
            {i.get("lineage_id") for i in reader.memory_items(self.posts, self.kernel, active_only=False)},
            {h["lineage_id"] for h in self.search("小家", ranked=None)},  # Ombre down: every head lexical
            {h["lineage_id"] for h in self.search("小家", ranked=["bbb222"])},
            {k for k, v in reader.get_current([lineage_id], self.kernel).items() if v},
        ]
        return [lineage_id in seen for seen in everywhere]

    def test_soft_deleted_in_ombre_is_never_read_back(self):
        self.assertEqual(self.visible("lin:ombre:bbb222"), [True] * 5)
        self.ombre.edit("bbb222", deleted_at="2026-10-22T03:00:00")
        self.sync()
        self.assertEqual(self.visible("lin:ombre:bbb222"), [False] * 5)
        self.assertEqual(self.state.get("lin:ombre:bbb222")["status"], "conflict")  # not revived either

    def test_archived_in_ombre_is_never_read_back(self):
        self.ombre.edit("bbb222", type="archived")
        self.sync()
        self.assertEqual(self.visible("lin:ombre:bbb222"), [False] * 5)

    def test_dont_surface_and_digested_retire(self):
        self.ombre.edit("bbb222", dont_surface=True)
        self.ombre.edit("aaa111", digested=True)
        self.sync()
        self.assertEqual(self.visible("lin:ombre:bbb222"), [False] * 5)
        self.assertNotIn("lin:ombre:aaa111", {h["lineage_id"] for h in reader.list_current(self.kernel)})

    def test_ombre_outage_keeps_the_last_verdict(self):
        self.ombre.edit("bbb222", deleted_at="2026-10-22T03:00:00")
        self.sync()
        self.ombre.down = True
        self.sync()  # cannot ask Ombre: nothing is re-decided
        self.assertEqual(self.visible("lin:ombre:bbb222"), [False] * 5)

    def test_official_verdict_wins_over_metadata(self):
        # Ombre's policy exempts core rules from digested; its verdict is what counts.
        self.ombre.edit("bbb222", digested=True, lifecycle_verdict={"allowed": True, "reasons": []})
        self.sync()
        self.assertEqual(self.visible("lin:ombre:bbb222"), [True] * 5)
        [row] = [r for b, r in self.state.lifecycles().items() if b == "bbb222"]
        self.assertEqual(row["source"], "ombre_surface_policy")

    def test_restored_in_ombre_surfaces_again(self):
        self.ombre.edit("bbb222", type="archived")
        self.sync()
        self.ombre.edit("bbb222", type="permanent")
        self.sync()
        self.assertEqual(self.visible("lin:ombre:bbb222"), [True] * 5)

    def test_missing_bucket_is_rebuilt_not_retired(self):
        del self.ombre.buckets["bbb222"]
        self.sync()
        self.assertIn("bbb222", self.ombre.buckets)
        self.assertEqual(self.visible("lin:ombre:bbb222"), [True] * 5)

    def test_archived_at_import_and_never_observed_stays_hidden(self):
        written = store.remember("很久以前的一次散步。", source="test", kernel_db=self.kernel, runtime=None)
        state = MemoryKernel(self.kernel).get_state(written["state_id"])
        self.revise(written["lineage_id"], "很久以前的一次散步。", value=dict(
            state.structured_value, legacy={"ombre_type": "archived"}))
        self.assertNotIn(written["lineage_id"],
                         {h["lineage_id"] for h in reader.list_current(self.kernel, active_only=False)})
