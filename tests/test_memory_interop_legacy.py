from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.memory_interop import (
    MEMORY_INTEROP_PROTOCOL_VERSION,
    InteropRequestContext,
)
from tools.memory_interop_legacy import (
    LEGACY_POSTS_ADAPTER_ID,
    LEGACY_POSTS_DESCRIPTOR,
    LEGACY_POSTS_SOURCE_TYPE,
    legacy_post_to_evidence,
    legacy_post_to_submission_envelope,
    open_legacy_posts_readonly,
    retrieve_legacy_posts_context_bundle,
    shadow_compare_legacy_search,
)
from tools.memory_kernel import MemoryKernel
from tools.product_handlers import search_memory_posts


ROOT = Path(__file__).resolve().parents[1]
UTC_1 = "2026-09-29T08:00:00+00:00"
UTC_2 = "2026-09-29T08:01:00Z"
NAIVE_CREATED_AT = "2026-08-01 12:00:00"
AWARE_CREATED_AT = "2026-09-29T08:00:00+00:00"
OFFSET_CREATED_AT = "2026-09-29T16:00:00+08:00"

NEEDLE_ORDER = [60, 12, 11, 10, 9, 8, 7, 6]
EMPTY_ORDER = [70, 60, 50, 12, 11, 10, 9, 8]


def _request_context(**changes):
    values = {
        "protocol_version": MEMORY_INTEROP_PROTOCOL_VERSION,
        "request_id": "req-1",
        "turn_id": "turn-1",
        "trigger_kind": "offline_shadow",
        "adapter_id": LEGACY_POSTS_ADAPTER_ID,
        "capability_id": "memory.search",
        "lease_ref": "lease:1",
        "requested_at": UTC_1,
        "metadata": {"trace": "a"},
    }
    values.update(changes)
    return InteropRequestContext(**values)


def _make_legacy_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT DEFAULT 'fyodor',
            created_at TEXT,
            pinned INTEGER DEFAULT 0,
            tags TEXT DEFAULT '',
            layer TEXT DEFAULT 'recent',
            resolved INTEGER DEFAULT 0,
            importance INTEGER DEFAULT 0,
            recall_count INTEGER DEFAULT 0,
            last_recalled_at TEXT
        );
        """
    )
    rows = [
        (1, "MEMORY", "old needle", NAIVE_CREATED_AT, 0, "old", "recent", 0, 1, 0, None),
        (2, "MEMORY", "pinned needle", "2026-08-02", 1, "pin", "recent", 0, 2, 0, None),
        (3, "MEMORY", "needle three", "2026-08-03", 0, "three", "recent", 0, 0, 0, None),
        (4, "DIARY", "needle diary", "2026-08-04", 0, "diary", "recent", 0, 0, 0, None),
        (5, "MEMORY", "needle five", "2026-08-05", 0, "five", "core", 0, 0, 0, None),
        (6, "MEMORY", "needle six", "2026-08-06", 0, "six", "recent", 0, 0, 3, "2026-08-20 01:00:00"),
        (7, "MEMORY", "needle seven", "2026-08-07", 0, "seven", "recent", 0, 0, 0, None),
        (8, "MEMORY", "needle eight", "2026-08-08", 0, "eight", "recent", 0, 0, 0, None),
        (9, "MEMORY", "needle nine", "2026-08-09", 0, "nine", "recent", 0, 0, 0, None),
        (10, "MEMORY", "needle ten", "2026-08-10", 0, "ten", "recent", 0, 0, 0, None),
        (11, "MEMORY", "needle eleven", "2026-08-11", 0, "eleven", "recent", 0, 0, 0, None),
        (12, "MEMORY", "needle twelve", "2026-08-12", 0, "twelve", "recent", 0, 0, 0, None),
        (50, "MEMORY", "content does not match", "2026-08-13", 0, "needle", "recent", 0, 0, 0, None),
        (60, "MEMORY", "resolved needle", "2026-08-14", 0, "resolved", "recent", 1, 0, 1, "2026-08-21 02:00:00"),
        (70, "MEMORY", "中文关键词：海边", "2026-08-15", 0, "中文", "recent", 0, 0, 0, None),
    ]
    conn.executemany(
        """
        INSERT INTO posts (
            id, type, content, created_at, pinned, tags, layer, resolved,
            importance, recall_count, last_recalled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    conn.close()


def _snapshot(path: str):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            """
            SELECT id, type, content, author, created_at, pinned, tags, layer,
                   resolved, importance, recall_count, last_recalled_at
            FROM posts ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()


def _digest(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _row(path: str, post_id: int) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def _envelope(row, **changes):
    values = {
        "request_context": _request_context(),
        "submission_id": "submission-1",
        "idempotency_key": "idempotency-1",
        "submitted_at": UTC_2,
        "observed_at": UTC_1,
    }
    values.update(changes)
    return legacy_post_to_submission_envelope(row, **values)


class LegacyInteropDescriptorTests(unittest.TestCase):
    def test_descriptor_uses_protocol_and_stable_adapter_id(self):
        self.assertEqual("0.1", LEGACY_POSTS_DESCRIPTOR.protocol_version)
        self.assertEqual(MEMORY_INTEROP_PROTOCOL_VERSION, LEGACY_POSTS_DESCRIPTOR.protocol_version)
        self.assertEqual("legacy.posts.v1", LEGACY_POSTS_DESCRIPTOR.adapter_id)
        self.assertEqual(("ingest", "retrieve", "context_contribute"), LEGACY_POSTS_DESCRIPTOR.supported_operations)
        self.assertTrue(LEGACY_POSTS_DESCRIPTOR.readable)
        self.assertFalse(LEGACY_POSTS_DESCRIPTOR.writable)
        self.assertEqual(("memory.search", "memory.write"), LEGACY_POSTS_DESCRIPTOR.supported_capabilities)


class LegacyPostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")
        _make_legacy_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_row_to_evidence_preserves_exact_content_and_derived_origin(self):
        evidence = legacy_post_to_evidence(_row(self.db_path, 1), observed_at=UTC_1)
        self.assertEqual("old needle", evidence.content)
        self.assertEqual("derived", evidence.origin_kind)
        self.assertEqual(LEGACY_POSTS_SOURCE_TYPE, evidence.source_type)
        self.assertEqual("legacy://posts/1", evidence.source_ref)
        self.assertEqual("legacy.posts.1", evidence.evidence_id)
        self.assertEqual(UTC_1, evidence.observed_at)
        self.assertEqual("posts", evidence.provenance["legacy_table"])
        self.assertEqual(1, evidence.provenance["legacy_row_id"])
        self.assertEqual("MEMORY", evidence.provenance["type"])
        self.assertEqual("fyodor", evidence.provenance["author"])
        self.assertEqual("recent", evidence.provenance["layer"])
        self.assertEqual("old", evidence.provenance["tags"])
        self.assertEqual(0, evidence.provenance["pinned"])
        self.assertEqual(0, evidence.provenance["resolved"])
        self.assertEqual(1, evidence.provenance["importance"])
        self.assertEqual(NAIVE_CREATED_AT, evidence.provenance["created_at"])
        self.assertNotIn("confidence", evidence.provenance)
        self.assertIsNone(evidence.occurred_at)

    def test_naive_created_at_is_not_converted_into_occurred_at(self):
        evidence = legacy_post_to_evidence(_row(self.db_path, 1), observed_at=UTC_1)
        self.assertEqual(NAIVE_CREATED_AT, evidence.provenance["created_at"])
        self.assertIsNone(evidence.occurred_at)
        self.assertNotIn("+08", str(evidence.provenance["created_at"]))
        self.assertNotEqual(evidence.occurred_at, "2026-08-01T12:00:00+08:00")

    def test_timezone_aware_created_at_may_become_occurred_at(self):
        aware = legacy_post_to_evidence(
            {"id": 80, "content": "aware timestamp", "created_at": AWARE_CREATED_AT},
            observed_at=UTC_1,
        )
        self.assertEqual(AWARE_CREATED_AT, aware.occurred_at)
        self.assertEqual(AWARE_CREATED_AT, aware.provenance["created_at"])
        offset = legacy_post_to_evidence(
            {"id": 81, "content": "offset timestamp", "created_at": OFFSET_CREATED_AT},
            observed_at=UTC_1,
        )
        self.assertEqual(OFFSET_CREATED_AT, offset.occurred_at)
        self.assertEqual(OFFSET_CREATED_AT, offset.provenance["created_at"])
        self.assertNotEqual("2026-09-29T08:00:00+00:00", offset.occurred_at)

    def test_missing_optional_fields_are_not_invented(self):
        evidence = legacy_post_to_evidence(
            {"id": 99, "content": "only these keys"},
            observed_at=UTC_1,
        )
        self.assertEqual("only these keys", evidence.content)
        self.assertNotIn("type", evidence.provenance)
        self.assertNotIn("author", evidence.provenance)
        self.assertNotIn("tags", evidence.provenance)
        self.assertNotIn("created_at", evidence.provenance)
        self.assertIsNone(evidence.occurred_at)


class LegacyPostEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")
        _make_legacy_db(self.db_path)
        self.row = _row(self.db_path, 12)

    def tearDown(self):
        self.temp.cleanup()

    def test_envelope_contains_evidence_only(self):
        envelope = _envelope(self.row)
        self.assertEqual(MEMORY_INTEROP_PROTOCOL_VERSION, envelope.protocol_version)
        self.assertEqual(LEGACY_POSTS_ADAPTER_ID, envelope.adapter_id)
        self.assertEqual(1, len(envelope.evidence))
        self.assertEqual((), envelope.candidate_states)
        self.assertEqual((), envelope.candidate_deltas)
        self.assertEqual("derived", envelope.evidence[0].origin_kind)
        self.assertEqual("legacy_shadow_translation", envelope.provenance["kind"])
        self.assertFalse(envelope.provenance["writable"])

    def test_semantic_fingerprint_is_deterministic(self):
        first = _envelope(self.row)
        second = _envelope(self.row)
        self.assertEqual(first.semantic_fingerprint(), second.semantic_fingerprint())
        self.assertRegex(first.semantic_fingerprint(), r"^[0-9a-f]{64}$")

    def test_semantic_content_change_changes_fingerprint(self):
        first = _envelope(self.row)
        changed = dict(self.row)
        changed["content"] = "needle twelve changed"
        second = _envelope(changed)
        self.assertNotEqual(first.semantic_fingerprint(), second.semantic_fingerprint())

    def test_transport_metadata_does_not_change_fingerprint(self):
        first = _envelope(self.row)
        same_semantics = _envelope(
            self.row,
            submission_id="submission-2",
            idempotency_key="other-key",
            submitted_at="2026-09-30T09:00:00+01:00",
            request_context=_request_context(
                request_id="req-2",
                turn_id="turn-2",
                requested_at="2026-09-30T08:00:00Z",
                metadata={"trace": "request-b"},
            ),
        )
        self.assertEqual(first.semantic_fingerprint(), same_semantics.semantic_fingerprint())


class LegacySearchContextBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")
        _make_legacy_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _retrieve(self, keyword, limit=8):
        return retrieve_legacy_posts_context_bundle(
            self.db_path,
            keyword=keyword,
            request_context=_request_context(),
            bundle_id="bundle-1",
            generated_at=UTC_1,
            limit=limit,
        )

    def test_search_preserves_authoritative_order_and_inclusions(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            authoritative = search_memory_posts(conn, keyword="needle", limit=8)
        finally:
            conn.close()
        bundle = self._retrieve("needle")
        self.assertEqual(NEEDLE_ORDER, [row["id"] for row in authoritative])
        self.assertEqual(
            [f"legacy.posts.{post_id}" for post_id in NEEDLE_ORDER],
            [item.item_id for item in bundle.items],
        )
        self.assertEqual(1, dict(authoritative[0])["resolved"])
        self.assertIn("legacy.posts.60", [item.item_id for item in bundle.items])
        diary = self._retrieve("needle diary")
        self.assertEqual(("legacy.posts.4",), tuple(item.item_id for item in diary.items))
        self.assertEqual("DIARY", diary.items[0].provenance["type"])

    def test_empty_and_unicode_keywords_preserve_current_behavior(self):
        empty = self._retrieve("")
        self.assertEqual(
            [f"legacy.posts.{post_id}" for post_id in EMPTY_ORDER],
            [item.item_id for item in empty.items],
        )
        unicode_bundle = self._retrieve("海边")
        self.assertEqual(("legacy.posts.70",), tuple(item.item_id for item in unicode_bundle.items))
        self.assertEqual("中文关键词：海边", unicode_bundle.items[0].content)

    def test_context_items_preserve_content_source_refs_and_do_not_invent_epistemics(self):
        bundle = self._retrieve("needle")
        first = bundle.items[0]
        self.assertEqual("resolved needle", first.content)
        self.assertEqual(("legacy://posts/60",), first.source_refs)
        self.assertEqual((), first.kernel_refs)
        self.assertEqual(LEGACY_POSTS_ADAPTER_ID, first.source_adapter_id)
        self.assertIsNone(first.confidence)
        self.assertIsNone(first.epistemic_status)
        self.assertEqual({}, dict(first.permission_boundary))
        self.assertEqual("legacy memory.search shadow", first.retrieval_reason)
        self.assertEqual(0, first.selection_metadata["legacy_search"]["position"])
        self.assertEqual((LEGACY_POSTS_ADAPTER_ID,), bundle.contributor_adapter_ids)
        self.assertEqual("req-1", bundle.request_id)
        self.assertEqual("turn-1", bundle.turn_id)

    def test_shadow_compare_reports_structural_parity(self):
        result = shadow_compare_legacy_search(
            self.db_path,
            keyword="needle",
            request_context=_request_context(),
            bundle_id="bundle-1",
            generated_at=UTC_1,
        )
        self.assertEqual([str(post_id) for post_id in NEEDLE_ORDER], result["row_ids"])
        self.assertTrue(result["same_ids"])
        self.assertTrue(result["same_order"])
        self.assertTrue(result["same_content"])
        self.assertTrue(result["same_count"])
        self.assertTrue(result["same_source_refs"])


class LegacyReadOnlyGuaranteeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")
        _make_legacy_db(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_translation_and_retrieval_do_not_mutate_posts_or_recall(self):
        before = _snapshot(self.db_path)
        digest_before = _digest(self.db_path)
        names_before = set(Path(self.temp.name).iterdir())
        _envelope(_row(self.db_path, 1))
        retrieve_legacy_posts_context_bundle(
            self.db_path,
            keyword="needle",
            request_context=_request_context(),
            bundle_id="bundle-1",
            generated_at=UTC_1,
        )
        self.assertEqual(before, _snapshot(self.db_path))
        self.assertEqual(digest_before, _digest(self.db_path))
        self.assertEqual(names_before, set(Path(self.temp.name).iterdir()))
        row = _row(self.db_path, 6)
        self.assertEqual(3, row["recall_count"])
        self.assertEqual("2026-08-20 01:00:00", row["last_recalled_at"])

    def test_readonly_connection_rejects_mutation(self):
        conn = open_legacy_posts_readonly(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO posts (type, content) VALUES (?, ?)",
                    ("MEMORY", "should not persist"),
                )
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE posts SET recall_count = 99 WHERE id = 6")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM posts WHERE id = 6")
        finally:
            conn.close()
        self.assertEqual(3, _row(self.db_path, 6)["recall_count"])

    def test_kernel_database_is_never_opened_or_created(self):
        real_connect = sqlite3.connect

        def connect_guard(database, *args, **kwargs):
            name = os.fspath(database)
            if "memory_kernel" in name or name.endswith(".kernel.db"):
                raise AssertionError("kernel database opened")
            return real_connect(database, *args, **kwargs)

        with mock.patch.object(
            MemoryKernel, "__init__", side_effect=AssertionError("MemoryKernel constructed")
        ), mock.patch.object(
            MemoryKernel, "create_evidence", side_effect=AssertionError("create_evidence")
        ), mock.patch.object(
            MemoryKernel, "create_state", side_effect=AssertionError("create_state")
        ), mock.patch.object(
            MemoryKernel, "revise_state", side_effect=AssertionError("revise_state")
        ):
            _envelope(_row(self.db_path, 1))
            with mock.patch(
                "tools.memory_interop_legacy.sqlite3.connect",
                side_effect=connect_guard,
            ) as connect:
                retrieve_legacy_posts_context_bundle(
                    self.db_path,
                    keyword="needle",
                    request_context=_request_context(),
                    bundle_id="bundle-1",
                    generated_at=UTC_1,
                )
        self.assertTrue(connect.called)
        for call in connect.call_args_list:
            database = call.args[0] if call.args else call.kwargs.get("database")
            self.assertTrue(call.kwargs.get("uri"))
            self.assertIn("mode=ro", str(database))
            self.assertIn(Path(self.db_path).resolve().as_posix(), str(database))
            self.assertNotIn("memory_kernel", str(database))
        self.assertFalse(any(Path(self.temp.name).glob("memory_kernel*")))
        conn = sqlite3.connect(self.db_path)
        try:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        self.assertIn("posts", names)
        self.assertFalse(any(name.startswith("memory_kernel") for name in names))


class LegacyAdapterIsolationTests(unittest.TestCase):
    def test_import_does_not_load_production_runtime_or_ombre_modules(self):
        script = r'''
import json
import sys
import tools.memory_interop_legacy

blocked_roots = ("app", "gateway", "chat", "wake", "providers", "cc_resident")
loaded = sorted(
    name for name in sys.modules
    if name == "tools.ombre_adapter"
    or name.startswith("tools.ombre_adapter.")
    or name.split(".", 1)[0] in blocked_roots
)
print(json.dumps(loaded))
'''
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 and "No module named 'tools'" in completed.stderr:
            escaped_root = json.dumps(str(ROOT))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    f"import sys; sys.path.insert(0, {escaped_root});" + script,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], json.loads(completed.stdout))

    def test_adapter_source_does_not_mutate_or_commit_kernel(self):
        source = (ROOT / "tools" / "memory_interop_legacy.py").read_text(encoding="utf-8")
        for forbidden in (
            "save_memory",
            "create_evidence",
            "create_state",
            "revise_state",
            "INSERT INTO",
            "DELETE FROM",
            "UPDATE posts",
            "tools.ombre_adapter",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_production_modules_do_not_import_the_legacy_interop_adapter(self):
        paths = list(ROOT.glob("*.py"))
        for directory in ("tools", "chat", "wake"):
            paths.extend((ROOT / directory).rglob("*.py"))
        skip = {
            "tools/memory_interop_legacy.py",
            "tools/memory_interop_legacy_shadow.py",
        }
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            if relative in skip:
                continue
            with self.subTest(path=relative):
                self.assertNotIn("memory_interop_legacy", path.read_text(encoding="utf-8"))


class LegacyShadowCliTests(unittest.TestCase):
    def test_offline_cli_is_opt_in_and_reports_parity(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "legacy.db")
            _make_legacy_db(db_path)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "memory_interop_legacy_shadow.py"),
                    "--db-path",
                    db_path,
                    "--keyword",
                    "needle",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["same_order"])
        self.assertEqual([str(post_id) for post_id in NEEDLE_ORDER], payload["row_ids"])


if __name__ == "__main__":
    unittest.main()
