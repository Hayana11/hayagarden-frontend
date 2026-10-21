"""B-2 projection: Kernel heads -> Ombre, only through Ombre's runtime API."""
from __future__ import annotations

import copy
import dataclasses
import os
import sqlite3
import stat
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_memory_kernel_import import ImportFixture
from tools import memory_kernel_import as importer
from tools import memory_kernel_projection as projection
from tools.memory_kernel import Delta, MemoryKernel

NOW = "2026-10-21T12:00:00+00:00"
LIFECYCLE = ("activation_count", "last_active", "resolved", "created", "type")


class FakeOmbreRuntime:
    """Stands in for the worker + BucketManager: Ombre owns ids, normalisation and lifecycle.

    Mirrors the official contract: update() returns a bool (False = nothing committed,
    which the worker turns into a failed request), and create picks another id on a
    collision instead of failing.
    """

    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.runs = 0
        self.down = False
        self.reject_updates = False
        self.collide_ids: set[str] = set()  # ids another writer takes just before our create

    def seed_from_vault(self, vault: Path) -> "FakeOmbreRuntime":
        for path in sorted(vault.rglob("*.md")):
            if any(part.startswith(("_", ".")) for part in path.relative_to(vault).parts):
                continue
            meta, body = importer._split_frontmatter(path.read_text(encoding="utf-8"))
            self.buckets[str(meta["id"])] = {"id": str(meta["id"]), "content": body.strip(), "metadata": meta}
        return self

    def batch(self, requests):
        if self.down:
            raise projection.RuntimeUnavailable("worker_unreachable:ConnectionRefusedError")
        self.runs += 1
        results = []
        for request in requests:
            op, args = request["op"], [request[k] for k in ("bucket_id", "fields") if k in request]
            try:
                value = getattr(self, op)(*args)
                if op == "update":
                    if not value:
                        raise RuntimeError("ombre_update_not_committed")
                    value = {"updated": True}
                results.append(value)
            except Exception as exc:  # the real worker reports per-request failures the same way
                results.append(projection.RequestFailed(f"{type(exc).__name__}:{exc}"))
        return results

    def _check(self, op, bucket_id=""):
        if self.down:
            raise projection.RuntimeUnavailable("worker_unreachable:ConnectionRefusedError")
        self.calls.append((op, bucket_id))

    @staticmethod
    def _normalise(meta):
        if meta.get("pinned"):
            meta["importance"] = 10  # Ombre's own rule: pinned buckets are maximally important
        meta["domain"] = list(meta.get("domain") or ["未分类"])

    def _view(self, bucket):
        if bucket is None:
            return None
        meta = bucket["metadata"]
        return {"id": bucket["id"], "content": bucket["content"], "metadata": copy.deepcopy(meta),
                "deleted": bool(meta.get("deleted_at"))}

    def get(self, bucket_id):
        self._check("get", bucket_id)
        return self._view(self.buckets.get(bucket_id))

    def create_if_absent(self, bucket_id, fields):
        self._check("create_if_absent", bucket_id)
        existing = self._view(self.buckets.get(bucket_id))
        if existing is not None:
            return {"created": False, "bucket_id": bucket_id, "bucket": existing}
        actual = bucket_id
        if bucket_id in self.collide_ids:
            # Another writer takes the id between our get and our create; Ombre then
            # silently gives our bucket a suffixed id (3.6.14 behaviour).
            self.buckets[bucket_id] = {"id": bucket_id, "content": "别人刚写进来的桶。",
                                       "metadata": {"id": bucket_id, "type": "dynamic", "source_tool": "hold"}}
            actual = bucket_id + "_07"
        meta = {k: v for k, v in fields.items() if k not in ("content", "bucket_type")}
        meta.update(id=actual, type=fields["bucket_type"], activation_count=0, source_tool="kernel_projection")
        self._normalise(meta)
        self.buckets[actual] = {"id": actual, "content": fields["content"], "metadata": meta}
        self.created.append(actual)
        return {"created": True, "bucket_id": actual, "bucket": self._view(self.buckets[actual])}

    def update(self, bucket_id, fields):
        self._check("update", bucket_id)
        bucket = self.buckets.get(bucket_id)
        if bucket is None or self.reject_updates:
            return False
        for key, value in fields.items():
            if key == "content":
                bucket["content"] = value
            else:
                bucket["metadata"][key] = value
        self._normalise(bucket["metadata"])
        return True

    def list(self, *, include_archive=True):
        self._check("list")
        return [self._view(b) for b in self.buckets.values()
                if include_archive or not b["metadata"].get("deleted_at")]

    # Simulate Ombre-side activity (trace, dashboard, decay, its own hold).
    def edit(self, bucket_id, content=None, **meta):
        bucket = self.buckets[bucket_id]
        if content is not None:
            bucket["content"] = content
        bucket["metadata"].update(meta)

    def creates(self):
        return list(self.created)


class ProjectionFixture(ImportFixture):
    def setUp(self):
        super().setUp()
        importer.run(self.vault, self.posts, self.kernel, now=NOW)
        self.ombre = FakeOmbreRuntime().seed_from_vault(self.vault)
        self.state = projection.ProjectionState(projection.state_path_for(self.kernel))

    def revise(self, state_id: str, content: str, new_id: str):
        kernel = MemoryKernel(self.kernel)
        before = kernel.get_state(state_id)
        after = dataclasses.replace(before, state_id=new_id, content=content, created_at=NOW)
        kernel.revise_state(after, Delta(
            delta_id="dl:" + new_id, target_scope=before.scope, before_ref=state_id,
            after_ref=new_id, trigger_evidence_refs=before.evidence_refs,
            derived_by={"processor": "test"}, confidence=0.5, occurred_at=NOW,
            review_status="accepted",
        ))

    def heads(self):
        heads, problems = projection.select_heads(self.kernel)
        self.assertEqual(problems, [])
        return heads

    def head(self, lineage_id):
        return next(h for h in self.heads() if h.lineage_id == lineage_id)

    def project_all(self):
        return projection.project_heads(self.heads(), self.ombre, self.state, kernel_db=self.kernel)


class HeadTests(ProjectionFixture):
    def test_one_head_per_lineage_and_stable_bucket_ids(self):
        ids = {h.lineage_id: h.bucket_id for h in self.heads()}
        self.assertEqual(ids["lin:ombre:aaa111"], "aaa111")
        self.assertEqual(len(ids["lin:posts:2"]), 12)

    def test_revision_moves_the_head(self):
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        self.assertEqual(self.head("lin:ombre:aaa111").state.state_id, "st:ombre:aaa111:v2")

    def test_branched_lineage_is_reported_not_guessed(self):
        self.revise("st:ombre:aaa111:v1", "版本甲", "st:ombre:aaa111:v2a")
        self.revise("st:ombre:aaa111:v1", "版本乙", "st:ombre:aaa111:v2b")
        heads, problems = projection.select_heads(self.kernel)
        self.assertNotIn("lin:ombre:aaa111", {h.lineage_id for h in heads})
        self.assertEqual(problems[0]["reason"], "branched_heads")

    def test_unset_legacy_importance_becomes_neutral(self):
        head = self.heads()[0]
        for raw, expected in ((0, 5), (None, 5), (7, 7), (12, 10)):
            value = dict(head.state.structured_value, importance=raw)
            state = dataclasses.replace(head.state, structured_value=value)
            self.assertEqual(projection.target_fields(projection.Head(head.lineage_id, state))["importance"], expected)

    def test_fact_core_and_pinned_are_permanent(self):
        self.assertEqual(projection.bucket_type_for(self.head("lin:posts:2")), "permanent")  # legacy FACT
        self.assertEqual(projection.bucket_type_for(self.head("lin:ombre:bbb222")), "permanent")
        self.assertEqual(projection.bucket_type_for(self.head("lin:ombre:aaa111")), "dynamic")


class ProjectTests(ProjectionFixture):
    def test_rebuild_into_an_empty_ombre_then_idempotence(self):
        self.ombre.buckets.clear()
        counts = self.project_all()
        heads = self.heads()
        self.assertEqual(counts["projected"], len(heads))
        self.assertEqual(sorted(self.ombre.creates()), sorted(h.bucket_id for h in heads))
        tofu = self.ombre.buckets[self.head("lin:posts:2").bucket_id]["metadata"]
        self.assertEqual(tofu["type"], "permanent")
        self.assertIsInstance(tofu["tags"], list)
        self.assertEqual(self.project_all()["unchanged"], len(heads))

    def test_lineages_are_batched_into_few_worker_runs(self):
        self.ombre.buckets.clear()
        heads = self.heads()
        counts = projection.project_heads(heads, self.ombre, self.state, kernel_db=self.kernel, batch_size=2)
        self.assertEqual(counts["projected"], len(heads))
        self.assertEqual(self.ombre.runs, -(-len(heads) // 2))  # one run per batch for fresh creates
        self.ombre.runs = 0
        for head in heads:
            self.revise(head.state.state_id, head.state.content + "（补记）", head.state.state_id + "-r")
        self.assertEqual(self.project_all()["projected"], len(heads))
        self.assertEqual(self.ombre.runs, 2)  # gets, then update+get pairs

    def test_one_failing_request_does_not_sink_the_batch(self):
        self.ombre.buckets.clear()
        bad = self.head("lin:posts:2").bucket_id
        real = self.ombre.create_if_absent

        def reject(bucket_id, fields):
            if bucket_id == bad:
                raise ValueError("ombre validation")
            return real(bucket_id, fields)

        with mock.patch.object(self.ombre, "create_if_absent", side_effect=reject):
            counts = self.project_all()
        self.assertEqual((counts["pending"], counts["projected"]), (1, len(self.heads()) - 1))
        self.assertIn("ValueError:ombre validation", self.state.get("lin:posts:2")["error"])
        self.assertEqual(self.project_all()["projected"], 1)

    def test_bucket_lost_entirely_is_recreated_on_the_next_run(self):
        self.project_all()
        del self.ombre.buckets["aaa111"]  # e.g. a vault rebuilt from scratch
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫。", "st:ombre:aaa111:v2")
        self.assertEqual(self.project_all()["pending"], 1)
        self.assertEqual(self.project_all()["projected"], 1)
        self.assertEqual(self.ombre.buckets["aaa111"]["content"], "哈娅养了两只猫。")

    def test_create_is_read_back_and_ombre_normalisation_is_the_baseline(self):
        self.ombre.buckets.clear()
        bbb = self.head("lin:ombre:bbb222")
        self.assertEqual(projection.target_fields(bbb)["importance"], 5)
        self.assertEqual(projection.project_lineage(bbb, self.ombre, self.state, kernel_db=self.kernel), "projected")
        # One worker run: get, create, get back.
        self.assertEqual(self.ombre.calls, [("create_if_absent", "bbb222")])
        row = self.state.get("lin:ombre:bbb222")
        # pinned -> importance 10 is Ombre's rule: recorded as success, never drift.
        self.assertEqual((row["status"], row["projected_state_id"]), ("projected", "st:ombre:bbb222:v1"))
        self.assertIn('"importance": 10', row["observed_json"])
        self.assertEqual(projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"], [])

    def test_existing_buckets_keep_lifecycle_and_are_not_rewritten(self):
        before = copy.deepcopy(self.ombre.buckets["aaa111"])
        self.project_all()
        self.assertNotIn("aaa111", self.ombre.creates())
        self.assertNotIn(("update", "aaa111"), self.ombre.calls)  # [[wikilink]] body is semantically equal
        self.assertEqual(self.ombre.buckets["aaa111"], before)

    def test_revision_updates_only_changed_semantics(self):
        self.project_all()
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        with mock.patch.object(self.ombre, "update", wraps=self.ombre.update) as update:
            self.assertEqual(self.project_all()["projected"], 1)
        update.assert_called_once_with("aaa111", {"content": "哈娅养了两只猫，还有一只鹦鹉福禄。"})
        meta = self.ombre.buckets["aaa111"]["metadata"]
        self.assertEqual(meta["activation_count"], 7013)
        self.assertEqual(self.state.get("lin:ombre:aaa111")["projected_state_id"], "st:ombre:aaa111:v2")

    def test_ombre_unavailable_leaves_pending_and_retry_recovers(self):
        self.ombre.buckets.clear()
        self.ombre.down = True
        counts = self.project_all()
        self.assertEqual(counts["pending"], len(self.heads()))
        row = self.state.get("lin:posts:2")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertIn("worker_unreachable", row["error"])
        self.ombre.down = False
        self.assertEqual(self.project_all()["projected"], len(self.heads()))
        self.assertEqual(self.state.get("lin:posts:2")["attempts"], 0)

    def test_crash_after_ombre_wrote_retries_without_a_second_create(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.assertIn(head.bucket_id, self.ombre.buckets)
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "projected")
        self.assertEqual(self.ombre.creates(), [head.bucket_id])  # retry did get first

    def test_edit_inside_ombre_is_diverged_not_overwritten(self):
        self.project_all()
        self.ombre.edit("aaa111", importance=9)  # e.g. trace or the dashboard
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫。", "st:ombre:aaa111:v2")
        self.assertEqual(self.project_all()["diverged"], 1)
        self.assertEqual(self.ombre.buckets["aaa111"]["metadata"]["importance"], 9)
        self.assertEqual(self.state.get("lin:ombre:aaa111")["status"], "diverged")

    def test_lifecycle_changes_are_not_divergence(self):
        self.project_all()
        self.ombre.edit("aaa111", resolved=True, activation_count=9000, last_active=NOW)
        self.assertEqual(projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"], [])

    def test_unmanaged_deleted_and_system_buckets(self):
        self.project_all()
        self.ombre.buckets["fff666"] = {"id": "fff666", "content": "CC 直接写进 Ombre 的新记忆。",
                                        "metadata": {"id": "fff666", "type": "dynamic", "domain": ["日常"]}}
        self.ombre.edit("bbb222", deleted_at=NOW)
        scan = projection.scan_ombre(self.heads(), self.ombre, self.state)
        # ddd444 (呼吸间) is Ombre's own; ccc333/eee555 were already deleted before import.
        self.assertEqual([b["id"] for b in scan["unmanaged"]], ["fff666"])
        self.assertEqual(scan["soft_deleted"], ["lin:ombre:bbb222"])
        self.assertEqual(scan["missing"], [])
        self.revise("st:ombre:bbb222:v1", "我们的小家很完美。", "st:ombre:bbb222:v2")
        self.assertEqual(self.project_all()["conflict"], 1)
        self.assertTrue(self.ombre.buckets["bbb222"]["metadata"]["deleted_at"])  # never resurrected
        self.assertNotIn("bbb222", self.ombre.creates())


class SeamTests(ProjectionFixture):
    """The gaps between "Ombre wrote" and "we recorded it"."""

    def revise_cats(self, content="哈娅养了两只猫，还有一只鹦鹉福禄。"):
        self.revise("st:ombre:aaa111:v1", content, "st:ombre:aaa111:v2")
        return self.head("lin:ombre:aaa111")

    def test_update_refused_by_ombre_is_pending_not_projected(self):
        self.project_all()
        self.revise_cats()
        self.ombre.reject_updates = True
        self.assertEqual(self.project_all()["pending"], 1)
        row = self.state.get("lin:ombre:aaa111")
        self.assertEqual((row["status"], row["projected_state_id"]), ("pending", "st:ombre:aaa111:v1"))
        self.assertIn("ombre_update_not_committed", row["error"])
        self.assertNotIn("鹦鹉", self.ombre.buckets["aaa111"]["content"])
        self.ombre.reject_updates = False
        self.assertEqual(self.project_all()["projected"], 1)
        self.assertIn("鹦鹉", self.ombre.buckets["aaa111"]["content"])

    def test_crash_after_update_landed_is_recognised_as_our_own_write(self):
        self.project_all()
        head = self.revise_cats()
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.assertIn("鹦鹉", self.ombre.buckets["aaa111"]["content"])  # Ombre applied it
        # Neither the scan nor the retry may read our own write as an edit inside Ombre.
        self.assertEqual(projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"], [])
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "projected")
        self.assertEqual(self.state.get("lin:ombre:aaa111")["projected_state_id"], "st:ombre:aaa111:v2")

    def test_crash_then_a_newer_head_still_projects_the_newer_head(self):
        self.project_all()
        head = self.revise_cats()
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.revise("st:ombre:aaa111:v2", "哈娅养了两只猫和一只鹦鹉，鹦鹉叫福禄。", "st:ombre:aaa111:v3")
        newer = self.head("lin:ombre:aaa111")
        self.assertEqual(projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"], [])
        self.assertEqual(projection.project_lineage(newer, self.ombre, self.state, kernel_db=self.kernel), "projected")
        self.assertEqual(self.ombre.buckets["aaa111"]["content"], "哈娅养了两只猫和一只鹦鹉，鹦鹉叫福禄。")

    def test_refused_update_leaves_no_intent_behind(self):
        self.project_all()
        self.revise_cats()
        self.ombre.reject_updates = True
        self.project_all()
        self.assertIsNone(self.state.get("lin:ombre:aaa111")["intent_json"])

    def test_crash_after_update_with_ombre_normalisation_is_still_ours(self):
        self.project_all()
        kernel = MemoryKernel(self.kernel)
        before = kernel.get_state("st:ombre:aaa111:v1")
        pinned = dict(before.structured_value, memory={**before.structured_value.get("memory", {}), "pinned": True})
        kernel.revise_state(dataclasses.replace(before, state_id="st:ombre:aaa111:v2", structured_value=pinned,
                                                created_at=NOW), Delta(
            delta_id="dl:pin", target_scope=before.scope, before_ref=before.state_id, after_ref="st:ombre:aaa111:v2",
            trigger_evidence_refs=before.evidence_refs, derived_by={"processor": "test"}, confidence=0.5,
            occurred_at=NOW, review_status="accepted"))
        head = self.head("lin:ombre:aaa111")
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        # Ombre raised importance to 10 on its own when we pinned: still our write.
        self.assertEqual(self.ombre.buckets["aaa111"]["metadata"]["importance"], 10)
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "projected")

    def test_outside_content_edit_after_our_crash_is_still_external(self):
        self.project_all()
        head = self.revise_cats()
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.ombre.edit("aaa111", content="她又在面板里改了一遍。")  # before our retry
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "diverged")

    def test_edit_neither_baseline_nor_target_is_external(self):
        self.project_all()
        head = self.revise_cats()
        self.ombre.edit("aaa111", content="她在面板里改成了别的内容。")
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "diverged")

    def test_ombre_already_holding_the_target_is_just_recorded(self):
        self.project_all()
        head = self.revise_cats()
        self.ombre.edit("aaa111", content="哈娅养了两只猫，还有一只鹦鹉福禄。")
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "projected")
        self.assertNotIn(("update", "aaa111"), self.ombre.calls)

    def test_pinned_importance_normalisation_is_not_resent_every_revision(self):
        self.project_all()
        self.revise("st:ombre:bbb222:v1", "我们的小家已经很完美了！", "st:ombre:bbb222:v2")
        with mock.patch.object(self.ombre, "update", wraps=self.ombre.update) as update:
            self.project_all()
        update.assert_called_once_with("bbb222", {"content": "我们的小家已经很完美了！"})

    def test_ombre_minting_another_id_is_a_conflict_not_success(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        self.ombre.collide_ids.add(head.bucket_id)
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "conflict")
        row = self.state.get("lin:posts:2")
        self.assertEqual(row["status"], "conflict")
        self.assertIn(f"ombre_assigned_other_id:{head.bucket_id}_07", row["error"])
        self.assertIsNone(row["observed_sha"])
        self.assertIn(f"{head.bucket_id}_07", self.state.quarantined())

    def test_id_taken_by_someone_elses_bucket_is_a_conflict_and_left_untouched(self):
        head = self.head("lin:posts:2")
        foreign = {"id": head.bucket_id, "content": "别人的桶，与这条记忆无关。",
                   "metadata": {"id": head.bucket_id, "type": "dynamic", "source_tool": "hold", "importance": 3}}
        self.ombre.buckets[head.bucket_id] = copy.deepcopy(foreign)
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "conflict")
        self.assertEqual(self.ombre.buckets[head.bucket_id], foreign)
        self.assertNotIn(("update", head.bucket_id), self.ombre.calls)
        self.assertEqual(self.state.get("lin:posts:2")["error"], "bucket_id_taken_by_unowned_bucket")
        self.assertEqual(self.project_all()["conflict"], 1)  # stays refused on every run
        self.assertEqual(self.ombre.buckets[head.bucket_id], foreign)

    def test_existing_bucket_equal_to_the_target_is_adopted(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        fields = projection.target_fields(head)
        self.ombre.buckets[head.bucket_id] = {"id": head.bucket_id, "content": fields["content"],
                                              "metadata": {k: v for k, v in fields.items() if k != "content"}}
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "projected")
        self.assertNotIn(("update", head.bucket_id), self.ombre.calls)

    def test_worker_marked_bucket_without_bookkeeping_is_never_overwritten(self):
        # Bookkeeping lost or rebuilt; the bucket was made by our worker and then edited
        # in Ombre's dashboard. The marker proves who made it, not that nobody changed it.
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        self.ombre.buckets[head.bucket_id] = {"id": head.bucket_id, "content": "在面板里改过的版本。",
                                              "metadata": {"id": head.bucket_id, "source_tool": "kernel_projection"}}
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "diverged")
        self.assertEqual(self.ombre.buckets[head.bucket_id]["content"], "在面板里改过的版本。")
        self.assertNotIn(("update", head.bucket_id), self.ombre.calls)
        [(found, _)] = projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"]
        self.assertEqual(found.lineage_id, "lin:posts:2")  # the edit is ingested, not lost

    def test_edit_between_our_create_and_its_bookkeeping_is_never_overwritten(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.assertEqual(self.ombre.creates(), [head.bucket_id])  # our create landed
        self.ombre.edit(head.bucket_id, content="她在我们记账之前就改了这条。")
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "diverged")
        self.assertEqual(self.ombre.buckets[head.bucket_id]["content"], "她在我们记账之前就改了这条。")
        self.assertNotIn(("update", head.bucket_id), self.ombre.calls)

    def test_unedited_create_of_ours_is_recovered_and_a_newer_head_still_projects(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        with mock.patch.object(self.state, "projected", side_effect=sqlite3.OperationalError):
            with self.assertRaises(sqlite3.OperationalError):
                projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel)
        self.revise(head.state.state_id, "哈娅不吃老豆腐，只吃内酯豆腐。", head.state.state_id + "-v2")
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel),
                         "projected")
        self.assertEqual(self.ombre.buckets[head.bucket_id]["content"], "哈娅不吃老豆腐，只吃内酯豆腐。")
        self.assertEqual(self.ombre.creates(), [head.bucket_id])

    def test_old_projector_stamp_with_later_edit_is_diverged_not_overwritten(self):
        self.ombre.buckets.clear()
        head = self.head("lin:posts:2")
        self.ombre.buckets[head.bucket_id] = {"id": head.bucket_id, "content": "主人在面板里改过的版本。",
                                              "metadata": {"id": head.bucket_id, "source_tool": "memory_kernel_projection",
                                                           "kernel_lineage_id": "lin:posts:2"}}
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state, kernel_db=self.kernel), "diverged")
        self.assertEqual(self.ombre.buckets[head.bucket_id]["content"], "主人在面板里改过的版本。")
        self.assertNotIn(("update", head.bucket_id), self.ombre.calls)
        [(found, _)] = projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"]
        self.assertEqual(found.lineage_id, "lin:posts:2")

    def test_imported_bucket_edited_before_any_baseline_is_diverged_not_overwritten(self):
        self.ombre.edit("aaa111", content="[[哈娅]]养了两只猫，后来又收养了一只。")
        self.assertEqual(projection.project_lineage(self.head("lin:ombre:aaa111"), self.ombre, self.state, kernel_db=self.kernel),
                         "diverged")
        self.assertIn("又收养", self.ombre.buckets["aaa111"]["content"])
        [(head, _)] = projection.scan_ombre(self.heads(), self.ombre, self.state)["diverged"]
        self.assertEqual(head.lineage_id, "lin:ombre:aaa111")  # the next sync ingests it

    def test_batch_bookkeeping_is_all_or_nothing_and_retry_never_duplicates(self):
        self.ombre.buckets.clear()
        real = projection.ProjectionState.projected
        calls = []

        def crash_on_second(state, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real(state, *args, **kwargs)

        with mock.patch.object(projection.ProjectionState, "projected", crash_on_second):
            with self.assertRaises(sqlite3.OperationalError):
                self.project_all()
        # The first lineage's record went down with the batch's single commit.
        self.assertFalse(any(r["status"] == "projected" for r in self.state.rows()))
        created = list(self.ombre.creates())
        self.assertEqual(self.project_all()["projected"], len(self.heads()))
        self.assertEqual(sorted(self.ombre.creates()), sorted(created))  # no second create

    def test_stale_sync_snapshot_never_downgrades_a_newer_head(self):
        self.project_all()
        stale_heads = self.heads()  # sync captures v1
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        self.assertEqual(projection.project_lineage(self.head("lin:ombre:aaa111"), self.ombre, self.state,
                                                    kernel_db=self.kernel), "projected")  # request projects v2
        # sync resumes with its v1 snapshot and gets the lock
        projection.project_heads(stale_heads, self.ombre, self.state, kernel_db=self.kernel)
        self.assertIn("鹦鹉", self.ombre.buckets["aaa111"]["content"])
        row = self.state.get("lin:ombre:aaa111")
        self.assertEqual((row["status"], row["projected_state_id"]), ("projected", "st:ombre:aaa111:v2"))

    def test_stale_snapshot_projects_the_current_head_when_it_was_not_yet_projected(self):
        self.project_all()
        stale = self.head("lin:ombre:aaa111")
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        self.assertEqual(projection.project_lineage(stale, self.ombre, self.state, kernel_db=self.kernel),
                         "projected")
        self.assertIn("鹦鹉", self.ombre.buckets["aaa111"]["content"])
        self.assertEqual(self.state.get("lin:ombre:aaa111")["projected_state_id"], "st:ombre:aaa111:v2")

    def test_lineage_lock_serialises_processes(self):
        head = self.head("lin:posts:2")
        other = projection.ProjectionState(self.state.path)  # e.g. sync in another process
        entered = threading.Event()
        release = threading.Event()
        order = []

        def holder():
            with self.state.locked([head.lineage_id]):
                entered.set()
                order.append("first")
                release.wait(5)

        thread = threading.Thread(target=holder)
        thread.start()
        entered.wait(5)
        def waiter_body():
            with other.locked([head.lineage_id]):
                order.append("second")

        waiter = threading.Thread(target=waiter_body)
        waiter.start()
        waiter.join(0.3)
        self.assertEqual(order, ["first"])  # blocked while the first holds the lineage
        release.set()
        thread.join(5)
        waiter.join(5)
        self.assertEqual(order, ["first", "second"])


class StateTests(unittest.TestCase):
    def test_projection_state_is_private_and_unique_per_bucket(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            old = os.umask(0o022)
            try:
                state = projection.ProjectionState(Path(folder) / "p.db")
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(os.stat(state.path).st_mode), 0o600)
            state.target("lin:a", "b1", "st:1")
            with self.assertRaises(sqlite3.IntegrityError):
                state.target("lin:b", "b1", "st:2")

    def test_runtime_needs_explicit_configuration(self):
        self.assertIsNone(projection.OmbreRuntime.from_env({}))
        self.assertIsNone(projection.OmbreRuntime.from_env({"OMBRE_RUNTIME_PYTHON": "/x"}))
        runtime = projection.OmbreRuntime.from_env({
            "OMBRE_RUNTIME_PYTHON": "/py", "OMBRE_RUNTIME_SRC": "/src", "OMBRE_RUNTIME_ENV_FILE": "/env",
            "OMBRE_RUNTIME_TIMEOUT": "5"})
        self.assertEqual((runtime.python, runtime.src_dir, runtime.timeout), ("/py", "/src", 5.0))

    def test_no_file_level_access_to_ombre(self):
        source = Path(projection.__file__).read_text(encoding="utf-8")
        # Its own lineage lock lives beside the projection-state DB; Ombre's per-bucket
        # lease ("bucket-<id>" under the vault's .locks) is never imitated.
        for forbidden in ("import yaml", "frontmatter", '"bucket-', "write_text", "/var/lib/hayagarden/ombre",
                          ".hold(", '"hold"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
