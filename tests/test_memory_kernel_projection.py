"""B-2 projection: Kernel heads -> Ombre vault, lifecycle left to Ombre."""
from __future__ import annotations

import dataclasses
import shutil
import unittest
from pathlib import Path

import yaml

from tests.test_memory_kernel_import import ImportFixture
from tools import memory_kernel_import as importer
from tools import memory_kernel_projection as projection
from tools.memory_kernel import Delta, MemoryKernel

NOW = "2026-10-21T12:00:00+00:00"


def read_bucket(vault: Path, bucket_id: str):
    path, meta, body = projection.index_vault(vault)[bucket_id]
    return path, meta, body


class ProjectionFixture(ImportFixture):
    def setUp(self):
        super().setUp()
        importer.run(self.vault, self.posts, self.kernel, now=NOW)
        self.out = Path(self.tmp.name) / "projected"

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


class HeadTests(ProjectionFixture):
    def test_one_head_per_lineage_and_stable_bucket_ids(self):
        heads, problems = projection.select_heads(self.kernel)
        self.assertEqual(problems, [])
        ids = {h.lineage_id: h.bucket_id for h in heads}
        self.assertEqual(ids["lin:ombre:aaa111"], "aaa111")
        self.assertEqual(len(ids["lin:posts:2"]), 12)

    def test_revision_moves_the_head(self):
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        heads, _ = projection.select_heads(self.kernel)
        head = next(h for h in heads if h.lineage_id == "lin:ombre:aaa111")
        self.assertEqual(head.state.state_id, "st:ombre:aaa111:v2")

    def test_branched_lineage_is_reported_not_guessed(self):
        self.revise("st:ombre:aaa111:v1", "版本甲", "st:ombre:aaa111:v2a")
        self.revise("st:ombre:aaa111:v1", "版本乙", "st:ombre:aaa111:v2b")
        heads, problems = projection.select_heads(self.kernel)
        self.assertNotIn("lin:ombre:aaa111", {h.lineage_id for h in heads})
        self.assertEqual(problems[0]["reason"], "branched_heads")


class RebuildTests(ProjectionFixture):
    def test_rebuild_into_empty_vault_then_verify_and_idempotence(self):
        heads, _ = projection.select_heads(self.kernel)
        counts = projection.apply(projection.plan(heads, self.out), self.out)
        self.assertEqual(counts["create"], len(heads))
        self.assertEqual(projection.verify(heads, self.out)["matched"], len(heads))
        again = projection.plan(heads, self.out)
        self.assertEqual({a["op"] for a in again["actions"]}, {"unchanged"})

    def test_new_bucket_layout_and_fields(self):
        heads, _ = projection.select_heads(self.kernel)
        projection.apply(projection.plan(heads, self.out), self.out)
        path, meta, body = read_bucket(self.out, "bbb222")
        self.assertEqual(path.relative_to(self.out).parts[0], "permanent")
        self.assertEqual(meta["kernel_lineage_id"], "lin:ombre:bbb222")
        self.assertEqual(meta["activation_count"], 0)
        tofu = next(h for h in heads if h.lineage_id == "lin:posts:2")
        path, meta, _ = read_bucket(self.out, tofu.bucket_id)
        # A legacy FACT with no Ombre copy lands as a permanent bucket.
        self.assertEqual(meta["type"], "permanent")
        self.assertIsInstance(meta["tags"], list)

    def test_unset_legacy_importance_becomes_neutral(self):
        heads, _ = projection.select_heads(self.kernel)
        head = heads[0]
        for raw, expected in ((0, 5), (None, 5), (7, 7), (12, 10)):
            value = dict(head.state.structured_value, importance=raw)
            state = dataclasses.replace(head.state, structured_value=value)
            self.assertEqual(projection.bucket_semantics(projection.Head(head.lineage_id, state))["importance"], expected)

    def test_verify_against_the_original_vault(self):
        heads, _ = projection.select_heads(self.kernel)
        report = projection.verify(heads, self.vault)
        # Two imported Ombre buckets match; posts-only lineages are new.
        self.assertEqual(report["matched"], 2)
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(len(report["missing_in_reference"]), len(heads) - 2)


class IncrementalTests(ProjectionFixture):
    def setUp(self):
        super().setUp()
        shutil.copytree(self.vault, self.out)

    def test_existing_buckets_keep_lifecycle_and_path(self):
        heads, _ = projection.select_heads(self.kernel)
        before_path, before, _ = read_bucket(self.out, "aaa111")
        projection.apply(projection.plan(heads, self.out), self.out)
        path, meta, body = read_bucket(self.out, "aaa111")
        self.assertEqual(path, before_path)
        self.assertEqual(meta["activation_count"], before["activation_count"])
        self.assertEqual(meta["last_active"], before["last_active"])
        # Ombre's [[wikilink]] body is semantically unchanged, so it is kept as is.
        self.assertIn("[[多乐]]", body)
        self.assertEqual(meta["kernel_state_id"], "st:ombre:aaa111:v1")

    def test_revision_rewrites_body_but_not_lifecycle(self):
        self.revise("st:ombre:aaa111:v1", "哈娅养了两只猫，还有一只鹦鹉福禄。", "st:ombre:aaa111:v2")
        heads, _ = projection.select_heads(self.kernel)
        planned = projection.plan(heads, self.out)
        update = next(a for a in planned["actions"] if a["bucket_id"] == "aaa111")
        self.assertEqual(update["op"], "update")
        self.assertIn("body", update["changed"])
        projection.apply(planned, self.out)
        _, meta, body = read_bucket(self.out, "aaa111")
        self.assertEqual(body.strip(), "哈娅养了两只猫，还有一只鹦鹉福禄。")
        self.assertEqual(meta["activation_count"], 7013)
        self.assertEqual(meta["kernel_state_id"], "st:ombre:aaa111:v2")

    def test_unmanaged_and_deleted_buckets(self):
        (self.out / "dynamic" / "日常").mkdir(parents=True, exist_ok=True)
        (self.out / "dynamic" / "日常" / "fresh.md").write_text(
            "---\nid: fff666\ntype: dynamic\n---\n\nCC 直接写进 Ombre 的新记忆。\n", encoding="utf-8")
        path, meta, body = read_bucket(self.out, "bbb222")
        meta["deleted_at"] = NOW
        path.write_text(projection._render(meta, body), encoding="utf-8")
        heads, _ = projection.select_heads(self.kernel)
        planned = projection.plan(heads, self.out)
        self.assertEqual(planned["unmanaged"], ["fff666"])
        conflict = next(a for a in planned["actions"] if a["bucket_id"] == "bbb222")
        self.assertEqual(conflict["op"], "conflict")
        projection.apply(planned, self.out)
        self.assertIn("deleted_at", yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1]))


if __name__ == "__main__":
    unittest.main()
