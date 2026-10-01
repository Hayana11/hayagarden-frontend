"""Focused R4B adapter-to-read-shadow wiring tests."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import ombre_adapter
from tools import ombre_read_shadow
from tools import ombre_read_shadow_worker


def _record(bucket_id="r1", content="opaque memory", *, bucket_type="dynamic"):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": f"name-{bucket_id}",
            "type": bucket_type,
            "domain": ["技术"],
            "tags": ["tag"],
            "valence": 0.5,
            "arousal": 0.3,
            "importance": 5,
            "created": "2026-09-30T00:00:00",
            "last_active": "2026-09-30T01:00:00",
        },
    }


class _Manager:
    def __init__(self):
        self.updated = []

    async def list_all(self, include_archive=False):
        return [_record(), _record("r2", "second memory")]

    async def get(self, bucket_id):
        return _record(bucket_id)

    async def search(self, query, limit=2):
        return [_record("r1", "search result")][:limit]

    async def update(self, bucket_id, **kwargs):
        self.updated.append((bucket_id, kwargs))
        return "updated"


def _server():
    manager = _Manager()

    async def handoff():
        return "authoritative handoff"

    async def breath():
        return "authoritative surface"

    return SimpleNamespace(bucket_mgr=manager, handoff=handoff, breath=breath), manager


class R4BWiringTests(unittest.TestCase):
    def _fake_observer_module(self, calls, *, raising=False):
        def make(name):
            def observer(*args, **kwargs):
                calls[name].append((args, kwargs))
                if raising:
                    raise RuntimeError("observer failure")
            return observer

        return SimpleNamespace(
            observe_handoff=make("handoff"),
            observe_records=make("records"),
            observe_record=make("record"),
            observe_emotion=make("emotion"),
            observe_search=make("search"),
        )

    def _importer(self, module):
        real_import = ombre_adapter.importlib.import_module

        def importer(name, *args, **kwargs):
            if name == "tools.ombre_read_shadow":
                return module
            return real_import(name, *args, **kwargs)

        return importer

    def test_all_authoritative_reads_observe_once_after_result(self):
        calls = {name: [] for name in ("handoff", "records", "record", "emotion", "search")}
        observer = self._fake_observer_module(calls)
        server, _ = _server()
        env = {
            "OMBRE_ADAPTER_BACKEND": "legacy_module",
            "OMBRE_HANDOFF_MODE": "legacy",
            "OMBRE_EMOTION_MODE": "safe",
            "OMBRE_READ_SHADOW_ENABLED": "1",
            "OMBRE_READ_SHADOW_HANDOFF_ENABLED": "1",
            "OMBRE_READ_SHADOW_RECORDS_ENABLED": "1",
            "OMBRE_READ_SHADOW_EMOTION_ENABLED": "1",
            "OMBRE_READ_SHADOW_SEARCH_ENABLED": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_adapter.importlib, "import_module", side_effect=self._importer(observer)):
            handoff = ombre_adapter.get_handoff(timeout=1, wall_timeout=2)
            records = ombre_adapter.list_memory_records(include_content=True, timeout=1, wall_timeout=2)
            record = ombre_adapter.get_memory_record("r1", timeout=1, wall_timeout=2)
            emotion = ombre_adapter.get_emotion_snapshot(timeout=1)
            search = ombre_adapter.search_memories(
                "记忆系统", limit=1, timeout=1, wall_timeout=2, touch=False,
            )

        self.assertEqual(handoff, "authoritative handoff")
        self.assertEqual(len(records), 2)
        self.assertEqual(record["id"], "r1")
        self.assertEqual(emotion["count"], 2)
        self.assertEqual(search, [("name-r1", "search result")])
        self.assertEqual({name: len(items) for name, items in calls.items()},
                         {"handoff": 1, "records": 1, "record": 1, "emotion": 1, "search": 1})
        self.assertIs(calls["handoff"][0][0][0], handoff)
        self.assertIs(calls["records"][0][0][0], records)
        self.assertIs(calls["record"][0][0][0], record)
        self.assertIs(calls["emotion"][0][0][0], emotion)
        self.assertIs(calls["search"][0][0][0], search)

    def test_gate_off_returns_exact_authoritative_value_without_dispatch(self):
        server, _ = _server()
        with mock.patch.dict(os.environ, {
            "OMBRE_ADAPTER_BACKEND": "legacy_module",
            "OMBRE_READ_SHADOW_ENABLED": "",
            "OMBRE_READ_SHADOW_SEARCH_ENABLED": "",
        }, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_read_shadow, "try_acquire_shadow_slot") as acquire:
            result = ombre_adapter.search_memories(
                "记忆系统", limit=1, timeout=1, wall_timeout=2, touch=False,
            )
        self.assertEqual(result, [("name-r1", "search result")])
        acquire.assert_not_called()

    def test_import_and_observer_failures_fail_open(self):
        server, _ = _server()
        with mock.patch.dict(os.environ, {
            "OMBRE_ADAPTER_BACKEND": "legacy_module",
            "OMBRE_HANDOFF_MODE": "legacy",
        }, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_adapter.importlib, "import_module", side_effect=ImportError("missing")):
            self.assertEqual(ombre_adapter.get_handoff(timeout=1, wall_timeout=2),
                             "authoritative handoff")

        observer = self._fake_observer_module({name: [] for name in ("handoff", "records", "record", "emotion", "search")}, raising=True)
        with mock.patch.dict(os.environ, {
            "OMBRE_ADAPTER_BACKEND": "legacy_module",
            "OMBRE_HANDOFF_MODE": "legacy",
        }, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_adapter.importlib, "import_module", side_effect=self._importer(observer)):
            self.assertEqual(ombre_adapter.get_handoff(timeout=1, wall_timeout=2),
                             "authoritative handoff")

    def test_spawn_failure_is_fail_open(self):
        server, _ = _server()
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.dict(os.environ, {
                 "OMBRE_ADAPTER_BACKEND": "legacy_module",
                 "OMBRE_HANDOFF_MODE": "legacy",
                 "OMBRE_READ_SHADOW_ENABLED": "1",
                 "OMBRE_READ_SHADOW_HANDOFF_ENABLED": "1",
                 "OMBRE_READ_SHADOW_DB_PATH": str(Path(temp_dir) / "receipts.db"),
             }, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_read_shadow.subprocess, "Popen", side_effect=OSError("spawn denied")):
            self.assertEqual(ombre_adapter.get_handoff(timeout=1, wall_timeout=2),
                             "authoritative handoff")

    def test_http_backend_never_imports_or_recurses_into_observer(self):
        with mock.patch.dict(os.environ, {"OMBRE_ADAPTER_BACKEND": "http"}, clear=False), \
             mock.patch.object(ombre_adapter, "_http_handoff", return_value="http handoff"), \
             mock.patch.object(ombre_adapter.importlib, "import_module") as importer:
            self.assertEqual(ombre_adapter.get_handoff(timeout=1, wall_timeout=2), "http handoff")
        importer.assert_not_called()

    def test_write_and_surface_paths_do_not_observe(self):
        calls = {name: [] for name in ("handoff", "records", "record", "emotion", "search")}
        observer = self._fake_observer_module(calls)
        server, manager = _server()
        with mock.patch.dict(os.environ, {
            "OMBRE_ADAPTER_BACKEND": "legacy_module",
        }, clear=False), \
             mock.patch.object(ombre_adapter, "_load_server", return_value=server), \
             mock.patch.object(ombre_adapter.importlib, "import_module", side_effect=self._importer(observer)):
            self.assertEqual(
                ombre_adapter.update_memory_emotion(
                    "r1", 0.2, 0.4, timeout=1, wall_timeout=2,
                ),
                "updated",
            )
            self.assertEqual(
                ombre_adapter.surface_memories(timeout=1, wall_timeout=2),
                "authoritative surface",
            )
        self.assertEqual(manager.updated[0][0], "r1")
        self.assertTrue(all(not values for values in calls.values()))

    def test_observer_receipt_has_hashes_not_raw_memory_or_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = str(Path(temp_dir) / "receipts.db")
            query = "private query"
            event = {
                "operation": "search",
                "receipt_db_path": db,
                "query": query,
                "limit": 5,
                "authoritative": {
                    "name_hashes": [],
                    "content_hashes": [],
                    "result_hash": ombre_read_shadow._hash_json({"names": [], "contents": []}),
                },
            }
            with mock.patch.object(
                ombre_read_shadow_worker.ombre_adapter,
                "search_memories",
                return_value=[],
            ):
                ombre_read_shadow_worker.run_event(event)
            raw = Path(db).read_bytes()
        self.assertNotIn(query.encode("utf-8"), raw)
        self.assertNotIn(b"opaque memory", raw)
        self.assertIn(b"query_sha256", raw)



if __name__ == "__main__":
    unittest.main()
