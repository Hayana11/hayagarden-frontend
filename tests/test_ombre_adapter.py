"""Regression tests for the unified Ombre integration boundary.

All tests use in-memory fakes.  They never import /opt/ombre-brain, read the
production vault, open the production SQLite database or call the network.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import ombre_adapter
from tools.cleaner import should_pin_synced_memory


class FakeBucketManager:
    def __init__(self, buckets=None, search_results=None):
        self.buckets = list(buckets or [])
        self.search_results = list(search_results or [])
        self.touched = []

    async def list_all(self, include_archive=False):
        self.include_archive = include_archive
        return list(self.buckets)

    async def search(self, query, limit=2):
        self.last_query = query
        self.last_limit = limit
        return list(self.search_results[:limit])

    async def touch(self, bucket_id):
        self.touched.append(bucket_id)


def bucket(
    bucket_id,
    content,
    *,
    bucket_type="dynamic",
    domains=None,
    last_active="2026-07-25T10:00:00",
    importance=5,
    **metadata,
):
    meta = {
        "id": bucket_id,
        "name": f"name-{bucket_id}",
        "type": bucket_type,
        "domain": list(domains or []),
        "last_active": last_active,
        "created": last_active,
        "importance": importance,
        "valence": 0.5,
        "arousal": 0.3,
    }
    meta.update(metadata)
    return {"id": bucket_id, "content": content, "metadata": meta}


class SafeHandoffTests(unittest.TestCase):
    def test_safe_handoff_uses_only_dynamic_recent_and_never_touches(self):
        manager = FakeBucketManager([
            bucket("self", "SELF", domains=["self_anchor"]),
            bucket("user", "USER", domains=["user_portrait"]),
            bucket("rel", "REL", domains=["relationship"]),
            bucket(
                "permanent-new",
                "PERMANENT MUST NOT BE RECENT",
                bucket_type="permanent",
                last_active="2026-07-25T23:59:00",
                importance=10,
            ),
            bucket(
                "pinned-new",
                "PINNED MUST NOT BE RECENT",
                last_active="2026-07-25T23:58:00",
                pinned=True,
                importance=10,
            ),
            bucket(
                "dynamic-new",
                "NEW DYNAMIC",
                last_active="2026-07-25T22:00:00",
                importance=6,
            ),
            bucket(
                "dynamic-old",
                "OLD DYNAMIC",
                last_active="2026-07-24T22:00:00",
                importance=9,
            ),
            bucket(
                "resolved",
                "RESOLVED MUST NOT APPEAR",
                last_active="2026-07-25T23:57:00",
                resolved=True,
            ),
        ])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.dict(os.environ, {"OMBRE_HANDOFF_MODE": "safe"}, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            text = ombre_adapter.get_handoff(timeout=1.0, wall_timeout=2.0)

        self.assertIn("[自我] SELF", text)
        self.assertIn("[你] USER", text)
        self.assertIn("[我们] REL", text)
        self.assertIn("NEW DYNAMIC", text)
        self.assertIn("OLD DYNAMIC", text)
        self.assertNotIn("PERMANENT MUST NOT BE RECENT", text)
        self.assertNotIn("PINNED MUST NOT BE RECENT", text)
        self.assertNotIn("RESOLVED MUST NOT APPEAR", text)
        self.assertEqual(manager.touched, [])

    def test_legacy_mode_preserves_existing_handoff_when_available(self):
        async def legacy_handoff():
            return "LEGACY HANDOFF"

        server = SimpleNamespace(handoff=legacy_handoff)
        with mock.patch.dict(os.environ, {"OMBRE_HANDOFF_MODE": "legacy"}, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            text = ombre_adapter.get_handoff(timeout=1.0, wall_timeout=2.0)
        self.assertEqual(text, "LEGACY HANDOFF")

    def test_new_server_without_handoff_falls_back_safely(self):
        manager = FakeBucketManager([
            bucket("new", "NEW SERVER DYNAMIC", last_active="2026-07-25T22:00:00"),
        ])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.dict(os.environ, {"OMBRE_HANDOFF_MODE": "legacy"}, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            text = ombre_adapter.get_handoff(timeout=1.0, wall_timeout=2.0)
        self.assertIn("NEW SERVER DYNAMIC", text)
        self.assertEqual(manager.touched, [])


class SearchAndWriteTests(unittest.TestCase):
    def test_explicit_search_touches_returned_hits(self):
        manager = FakeBucketManager(search_results=[
            bucket("hit", "matched content", domains=["技术"]),
        ])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.search_memories(
                "curwe",
                limit=1,
                timeout=1.0,
                wall_timeout=2.0,
                touch=True,
            )
        self.assertEqual(result, [("name-hit", "matched content")])
        self.assertEqual(manager.touched, ["hit"])

    def test_read_only_search_can_disable_touch(self):
        manager = FakeBucketManager(search_results=[bucket("hit", "matched content")])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            ombre_adapter.search_memories(
                "curwe", timeout=1.0, wall_timeout=2.0, touch=False
            )
        self.assertEqual(manager.touched, [])

    def test_hold_clamps_importance_and_passes_explicit_pin_only(self):
        received = {}

        async def hold(**kwargs):
            received.update(kwargs)
            return "saved"

        server = SimpleNamespace(hold=hold)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.hold_memory(
                "memory",
                tags="技术",
                importance=99,
                pinned=False,
                timeout=1.0,
                wall_timeout=2.0,
            )
        self.assertEqual(result, "saved")
        self.assertEqual(received["importance"], 10)
        self.assertFalse(received["pinned"])


class EmotionSnapshotTests(unittest.TestCase):
    def test_safe_snapshot_excludes_permanent_pinned_and_test_data(self):
        dynamic_a = bucket(
            "a", "A", valence=0.8, arousal=0.6,
            last_active="2026-07-25T22:00:00",
        )
        dynamic_b = bucket(
            "b", "B", valence=0.4, arousal=0.2,
            last_active="2026-07-25T21:00:00",
        )
        permanent = bucket(
            "p", "P", bucket_type="permanent", valence=0.0, arousal=1.0,
            last_active="2026-07-25T23:00:00",
        )
        pinned = bucket(
            "pin", "PIN", pinned=True, valence=0.0, arousal=1.0,
            last_active="2026-07-25T23:00:00",
        )
        test_data = bucket(
            "test", "TEST", provenance={"kind": "test"}, valence=0.0, arousal=1.0,
            last_active="2026-07-25T23:00:00",
        )
        manager = FakeBucketManager([dynamic_a, dynamic_b, permanent, pinned, test_data])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.dict(os.environ, {"OMBRE_EMOTION_MODE": "safe"}, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.get_emotion_snapshot(timeout=1.0)
        self.assertEqual(result, {"valence": 0.6, "arousal": 0.4, "count": 2})
        self.assertEqual(manager.touched, [])


class LegacyParityContractTests(unittest.TestCase):
    """Golden contracts for default legacy_module production behaviour."""

    def test_default_backend_is_legacy_module(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ombre_adapter._backend(), "legacy_module")

    def test_empty_query_returns_empty_list_without_server_load(self):
        with mock.patch.object(ombre_adapter, "_load_server") as load:
            self.assertEqual(ombre_adapter.search_memories("   "), [])
            load.assert_not_called()

    def test_search_contract_matches_gateway_recall_shape(self):
        long_content = "x" * 400
        manager = FakeBucketManager(search_results=[
            bucket("a", long_content, name="alpha"),
            bucket("b", "second hit", name="beta"),
            bucket("c", "third hit", name="gamma"),
        ])
        server = SimpleNamespace(bucket_mgr=manager)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.search_memories(
                "curwe",
                limit=2,
                timeout=1.0,
                wall_timeout=2.0,
                touch=True,
            )
        self.assertEqual(manager.last_limit, 2)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], "alpha")
        self.assertEqual(len(result[0][1]), 300)
        self.assertEqual(result[0][1], long_content[:300])
        self.assertEqual(result[1], ("beta", "second hit"))
        self.assertEqual(manager.touched, ["a", "b"])

    def test_search_failure_degrades_to_empty_list(self):
        def boom():
            raise RuntimeError("ombre unavailable")

        with mock.patch.object(ombre_adapter, "_load_server", side_effect=boom):
            self.assertEqual(
                ombre_adapter.search_memories("curwe", timeout=0.2, wall_timeout=0.3),
                [],
            )

    def test_handoff_failure_degrades_to_none(self):
        def boom():
            raise RuntimeError("ombre unavailable")

        with mock.patch.object(ombre_adapter, "_load_server", side_effect=boom):
            self.assertIsNone(ombre_adapter.get_handoff(timeout=0.2, wall_timeout=0.3))

    def test_breath_contract_preserves_legacy_timeouts(self):
        async def breath():
            return "BREATH SURFACE"

        server = SimpleNamespace(breath=breath)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.surface_memories(timeout=6.0, wall_timeout=7.0)
        self.assertEqual(result, "BREATH SURFACE")

    def test_breath_failure_degrades_to_none(self):
        def boom():
            raise RuntimeError("ombre unavailable")

        with mock.patch.object(ombre_adapter, "_load_server", side_effect=boom):
            self.assertIsNone(ombre_adapter.surface_memories(timeout=0.2, wall_timeout=0.3))

    def test_emotion_http_endpoint_failure_returns_empty_snapshot(self):
        with mock.patch.dict(os.environ, {"OMBRE_EMOTION_MODE": "http"}, clear=False), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            result = ombre_adapter.get_emotion_snapshot(timeout=0.2)
        self.assertEqual(result, {"valence": None, "arousal": None, "count": 0})

    def test_warmup_async_returns_immediately_without_loading_server(self):
        import time

        started = time.monotonic()
        with mock.patch.object(ombre_adapter, "_WARMUP_STARTED", False), \
             mock.patch.object(ombre_adapter, "_warmup_jieba_only", side_effect=lambda: time.sleep(2)), \
             mock.patch.object(ombre_adapter, "_load_server") as load_server:
            ombre_adapter.warmup_async()
        self.assertLess(time.monotonic() - started, 0.2)
        load_server.assert_not_called()


class CleanerPolicyTests(unittest.TestCase):
    def test_cleaner_never_auto_pins_by_importance(self):
        for importance in range(1, 11):
            self.assertFalse(should_pin_synced_memory(importance))

    def test_explicit_hold_pinned_true_passes_through(self):
        received = {}

        async def hold(**kwargs):
            received.update(kwargs)
            return "saved"

        server = SimpleNamespace(hold=hold)
        with mock.patch.object(ombre_adapter, "_load_server", return_value=server):
            result = ombre_adapter.hold_memory(
                "pinned memory",
                importance=10,
                pinned=True,
                timeout=1.0,
                wall_timeout=2.0,
            )
        self.assertEqual(result, "saved")
        self.assertTrue(received["pinned"])
        self.assertEqual(received["importance"], 10)


class WiringTests(unittest.TestCase):
    def test_hot_paths_no_longer_import_ombre_server_directly(self):
        root = Path(ROOT)
        for relative in (
            "chat/system_builder.py",
            "gateway.py",
            "emotion_engine.py",
            "tools/cleaner.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("from server import", source, relative)
        self.assertIn("from tools import ombre_adapter", (root / "gateway.py").read_text(encoding="utf-8"))

    def test_workspace_chat_uses_workspace_breath_timeout_budget(self):
        source = (Path(ROOT) / "gateway.py").read_text(encoding="utf-8")
        self.assertIn("_ombre_breath_sync(timeout=4.0, wall_timeout=5.0)", source)
        self.assertNotRegex(
            source,
            r"def workspace_chat\(\):[\s\S]*?_ombre_breath_sync\(\)",
        )


class CleanerBatchTests(unittest.TestCase):
    def test_cleaner_batch_uses_adapter_and_preserves_explicit_unpinned(self):
        import asyncio
        from tools import cleaner

        received = []

        async def fake_hold(**kwargs):
            received.append(kwargs)
            return "ok"

        with mock.patch("tools.ombre_adapter.hold_memory_async", side_effect=fake_hold):
            result = asyncio.run(cleaner._batch_hold([{
                "id": 7,
                "content": "memory",
                "tags_str": "技术",
                "importance": 10,
                "pinned": False,
            }]))
        self.assertEqual(result, [(7, "ok", None)])
        self.assertEqual(received[0]["importance"], 10)
        self.assertFalse(received[0]["pinned"])


class HttpBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        fixture = Path(ROOT) / "tests/fixtures/ombre_http_buckets_v2810.json"
        cls.bucket_records = json.loads(fixture.read_text(encoding="utf-8"))

    def test_http_handoff_reads_realistic_v2810_bucket_list(self):
        records = self.bucket_records

        def fake_json(path, **kwargs):
            if path == "/api/buckets":
                return records
            if path == "/api/bucket/self-anchor-001":
                return {"content": "SELF HTTP"}
            if path == "/api/bucket/recent-dynamic-001":
                return {"content": "RECENT HTTP"}
            raise AssertionError(path)

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_json", side_effect=fake_json):
            text = ombre_adapter.get_handoff(timeout=1, wall_timeout=2)
        self.assertIn("[自我] SELF HTTP", text)
        self.assertIn("[近期·I7] RECENT HTTP", text)

    def test_http_handoff_excludes_pinned_dynamic_from_recent(self):
        records = self.bucket_records

        def fake_json(path, **kwargs):
            if path == "/api/buckets":
                return records
            if path == "/api/bucket/self-anchor-001":
                return {"content": "SELF"}
            if path == "/api/bucket/recent-dynamic-001":
                return {"content": "SAFE RECENT"}
            raise AssertionError(path)

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_json", side_effect=fake_json):
            text = ombre_adapter.get_handoff(timeout=1, wall_timeout=2)
        self.assertIn("[近期·I7] SAFE RECENT", text)
        self.assertNotIn("pinned-dynamic-001", text)

    def test_http_handoff_respects_total_wall_timeout(self):
        def slow_json(path, **kwargs):
            time.sleep(0.3)
            return []

        import time
        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_json", side_effect=slow_json):
            started = time.monotonic()
            text = ombre_adapter.get_handoff(timeout=1, wall_timeout=0.2)
            elapsed = time.monotonic() - started
        self.assertIsNone(text)
        self.assertLess(elapsed, 0.6)

    def test_http_search_does_not_touch_hits_known_gap(self):
        touched = []

        def fake_json(path, **kwargs):
            if path == "/api/search":
                return [{"id": "hit-1", "name": "alpha", "content_preview": "matched"}]
            if path == "/api/bucket/hit-1":
                return {"content": "matched"}
            raise AssertionError(path)

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_json", side_effect=fake_json), \
             mock.patch.object(ombre_adapter, "_mcp_call", side_effect=lambda *a, **k: touched.append(a)):
            result = ombre_adapter.search_memories(
                "curwe", limit=1, timeout=1.0, wall_timeout=2.0, touch=True,
            )
        self.assertEqual(result, [("alpha", "matched")])
        self.assertEqual(touched, [])

    def test_http_handoff_may_surface_archive_bucket_without_list_flag(self):
        records = list(self.bucket_records) + [{
            "id": "archive-dynamic-001",
            "type": "dynamic",
            "domain": ["技术"],
            "resolved": False,
            "digested": False,
            "dont_surface": False,
            "pinned": False,
            "importance": 8,
            "last_active_epoch_ms": 100,
        }]

        def fake_json(path, **kwargs):
            if path == "/api/buckets":
                return records
            if path == "/api/bucket/self-anchor-001":
                return {"content": "SELF"}
            if path == "/api/bucket/archive-dynamic-001":
                return {"content": "ARCHIVE LEAK"}
            if path == "/api/bucket/recent-dynamic-001":
                return {"content": "SAFE RECENT"}
            raise AssertionError(path)

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_json", side_effect=fake_json):
            text = ombre_adapter.get_handoff(timeout=1, wall_timeout=2)
        self.assertIn("ARCHIVE LEAK", text)

    def test_http_emotion_filters_core_and_test_records(self):
        records = [
            {"id": "a", "type": "dynamic", "valence": 0.8, "arousal": 0.6},
            {"id": "b", "type": "dynamic", "valence": 0.4, "arousal": 0.2},
            {"id": "p", "type": "permanent", "valence": 0.0, "arousal": 1.0},
            {"id": "pin", "type": "dynamic", "pinned": True, "valence": 0.0, "arousal": 1.0},
            {"id": "test", "type": "dynamic", "erasable_test_data": True, "valence": 0.0, "arousal": 1.0},
        ]
        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False),              mock.patch.object(ombre_adapter, "_http_json", return_value=records):
            result = ombre_adapter.get_emotion_snapshot(timeout=1)
        self.assertEqual(result, {"valence": 0.6, "arousal": 0.4, "count": 2})

    def test_http_hold_uses_mcp_and_keeps_pin_explicit(self):
        import asyncio

        async def fake_call(name, arguments, *, timeout):
            self.assertEqual(name, "hold")
            self.assertFalse(arguments["pinned"])
            self.assertEqual(arguments["importance"], 10)
            self.assertGreaterEqual(timeout, 30)
            return "saved-http"

        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False),              mock.patch.object(ombre_adapter, "_mcp_call", side_effect=fake_call):
            result = asyncio.run(ombre_adapter.hold_memory_async(
                "HTTP memory", importance=99, pinned=False
            ))
        self.assertEqual(result, "saved-http")


if __name__ == "__main__":
    unittest.main()
