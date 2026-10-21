"""The projection worker drives a (fake) Ombre source tree through BucketManager only."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools import memory_kernel_projection as projection

FAKE_BUCKET_MANAGER = '''
import json
import os
from pathlib import Path


class BucketManager:
    def __init__(self, config):
        self.root = Path(os.environ["FAKE_OMBRE_STORE"])
        print("ombre is chatty on stdout")  # must not corrupt the worker protocol

    def _path(self, bucket_id):
        return self.root / f"{bucket_id}.json"

    def _load(self, bucket_id):
        path = self._path(bucket_id)
        return json.loads(path.read_text()) if path.exists() else None

    async def create(self, content, tags=None, importance=5, domain=None, valence=0.5, arousal=0.3,
                     bucket_type="dynamic", name=None, title=None, pinned=False, source_tool=None,
                     bucket_id_override=None, imported=False, defer_derived_index=False{extra}):
        meta = dict(id=bucket_id_override, tags=tags, importance=10 if pinned else importance,
                    domain=domain or ["未分类"], valence=valence, arousal=arousal, type=bucket_type,
                    name=name, pinned=pinned, source_tool=source_tool, imported=imported,
                    defer_derived_index=defer_derived_index, activation_count=0)
        self._path(bucket_id_override).write_text(json.dumps({{"id": bucket_id_override, "content": content,
                                                              "metadata": meta}}))
        return bucket_id_override

    async def update(self, bucket_id, **kwargs):
        bucket = self._load(bucket_id)
        for key, value in kwargs.items():
            if key == "content":
                bucket["content"] = value
            else:
                bucket["metadata"][key] = value
        if bucket["metadata"].get("pinned"):
            bucket["metadata"]["importance"] = 10
        self._path(bucket_id).write_text(json.dumps(bucket))
        return True

    async def get(self, bucket_id):
        bucket = self._load(bucket_id)
        return None if bucket is None or bucket["metadata"].get("deleted_at") else bucket

    async def get_including_archive(self, bucket_id):
        return self._load(bucket_id)

    async def list_all(self, include_archive=False):
        buckets = [json.loads(p.read_text()) for p in sorted(self.root.glob("*.json"))]
        return [b for b in buckets if include_archive or not b["metadata"].get("deleted_at")]
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src, self.store = root / "ombre-src", root / "store"
        self.src.mkdir()
        self.store.mkdir()
        self.env = root / "env"
        self.env.write_text(f"# ombre env\nFAKE_OMBRE_STORE='{self.store}'\n", encoding="utf-8")
        self.write_src("3.6.14")
        self.runtime = projection.OmbreRuntime(sys.executable, str(self.src), str(self.env), timeout=30)

    def write_src(self, version, *, drop_param=None, extra=""):
        source = FAKE_BUCKET_MANAGER.replace("{extra}", extra).replace("{{", "{").replace("}}", "}")
        if drop_param:
            source = re.sub(rf",\s*{drop_param}=[^,)]+", "", source)
        (self.src / "VERSION").write_text(version + "\n", encoding="utf-8")
        (self.src / "utils.py").write_text("def load_config():\n    return {}\n", encoding="utf-8")
        (self.src / "bucket_manager.py").write_text(textwrap.dedent(source), encoding="utf-8")

    def test_create_get_update_round_trip_through_bucket_manager(self):
        self.assertEqual(self.runtime.capabilities()["version"], "3.6.14")
        self.assertIsNone(self.runtime.get("abc123def456"))
        answer = self.runtime.create_if_absent("abc123def456", {"content": "她今天买了鹦鹉。", "name": "鹦鹉", "domain": [],
                                             "tags": ["pet"], "importance": 6, "valence": 0.7, "arousal": 0.4,
                                             "pinned": True, "bucket_type": "permanent"})
        self.assertTrue(answer["created"])
        bucket = answer["bucket"]
        self.assertEqual(bucket, self.runtime.get("abc123def456"))
        meta = bucket["metadata"]
        self.assertEqual(bucket["content"], "她今天买了鹦鹉。")
        self.assertEqual((meta["importance"], meta["domain"], meta["type"]), (10, ["未分类"], "permanent"))
        self.assertEqual((meta["source_tool"], meta["imported"], meta["defer_derived_index"]),
                         ("kernel_projection", True, True))
        # Lifecycle fields never pass through the projection's update.
        bucket = self.runtime.update("abc123def456", {"content": "她今天买了一只鹦鹉。", "resolved": True,
                                                      "activation_count": 99})
        self.assertEqual(bucket["content"], "她今天买了一只鹦鹉。")
        self.assertNotIn("resolved", bucket["metadata"])
        self.assertEqual(bucket["metadata"]["activation_count"], 0)
        self.assertEqual([b["id"] for b in self.runtime.list()], ["abc123def456"])
        # A retry never creates twice: the existing bucket comes back untouched.
        again = self.runtime.create_if_absent("abc123def456", {"content": "别的内容"})
        self.assertFalse(again["created"])
        self.assertEqual(again["bucket"]["content"], "她今天买了一只鹦鹉。")

    def test_a_failing_request_is_reported_alone(self):
        results = self.runtime.batch([
            {"op": "update", "bucket_id": "missing", "fields": {"content": "x"}},
            {"op": "create_if_absent", "bucket_id": "ok1", "fields": {"content": "她在。"}},
        ])
        self.assertIsInstance(results[0], projection.RequestFailed)
        self.assertTrue(results[1]["created"])

    def test_soft_deleted_bucket_is_seen_as_deleted(self):
        (self.store / "gone.json").write_text(json.dumps(
            {"id": "gone", "content": "x", "metadata": {"deleted_at": "2026-10-21T00:00:00"}}))
        self.assertTrue(self.runtime.get("gone")["deleted"])

    def test_incompatible_version_fails_explicitly(self):
        self.write_src("4.0.0")
        with self.assertRaisesRegex(projection.RuntimeUnavailable, "incompatible:unsupported Ombre version"):
            self.runtime.capabilities()
        self.assertEqual(list(self.store.iterdir()), [])

    def test_missing_bucket_manager_capability_fails_explicitly(self):
        self.write_src("3.6.14", drop_param="bucket_id_override")
        with self.assertRaisesRegex(projection.RuntimeUnavailable, "lacks: bucket_id_override"):
            self.runtime.create_if_absent("abc", {"content": "x"})
        self.assertEqual(list(self.store.iterdir()), [])

    def test_unreachable_interpreter_is_runtime_unavailable(self):
        runtime = projection.OmbreRuntime(str(Path(self.tmp.name) / "no-python"), str(self.src), str(self.env))
        with self.assertRaisesRegex(projection.RuntimeUnavailable, "worker_unreachable"):
            runtime.get("abc")

    def test_worker_is_self_contained_and_never_holds(self):
        source = projection.WORKER.read_text(encoding="utf-8")
        for forbidden in ("from tools", "import tools", ".hold(", "merge_or_create", "write_text", "open(version_path, \"w"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
