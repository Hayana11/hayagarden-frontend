"""B-2 projection: Kernel heads -> Ombre, only through Ombre's runtime API."""
from __future__ import annotations

import copy
import dataclasses
import os
import sqlite3
import stat
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
    """Stands in for BucketManager: Ombre owns ids, normalisation and lifecycle."""

    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.created: list[str] = []
        self.runs = 0
        self.down = False

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
                results.append(getattr(self, op)(*args))
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
            return {"created": False, "bucket": existing}
        meta = {k: v for k, v in fields.items() if k not in ("content", "bucket_type")}
        meta.update(id=bucket_id, type=fields["bucket_type"], activation_count=0, source_tool="kernel_projection")
        self._normalise(meta)
        self.buckets[bucket_id] = {"id": bucket_id, "content": fields["content"], "metadata": meta}
        self.created.append(bucket_id)
        return {"created": True, "bucket": self._view(self.buckets[bucket_id])}

    def update(self, bucket_id, fields):
        self._check("update", bucket_id)
        bucket = self.buckets[bucket_id]
        for key, value in fields.items():
            if key == "content":
                bucket["content"] = value
            else:
                bucket["metadata"][key] = value
        self._normalise(bucket["metadata"])
        return self._view(bucket)

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
        return projection.project_heads(self.heads(), self.ombre, self.state)


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
        counts = projection.project_heads(heads, self.ombre, self.state, batch_size=2)
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
        self.assertEqual(projection.project_lineage(bbb, self.ombre, self.state), "projected")
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
                projection.project_lineage(head, self.ombre, self.state)
        self.assertIn(head.bucket_id, self.ombre.buckets)
        self.assertEqual(projection.project_lineage(head, self.ombre, self.state), "projected")
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
        self.assertEqual(scan["deleted"], ["lin:ombre:bbb222"])
        self.revise("st:ombre:bbb222:v1", "我们的小家很完美。", "st:ombre:bbb222:v2")
        self.assertEqual(self.project_all()["conflict"], 1)
        self.assertTrue(self.ombre.buckets["bbb222"]["metadata"]["deleted_at"])  # never resurrected
        self.assertNotIn("bbb222", self.ombre.creates())


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
        for forbidden in ("import yaml", "frontmatter", "flock", "write_text", "/var/lib/hayagarden/ombre",
                          ".hold(", '"hold"'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
