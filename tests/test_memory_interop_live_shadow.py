"""R2A Live Shadow wiring: default-off, fail-open, observation-only."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.memory_internal_adapter import search_memories
from tools.memory_interop_legacy import legacy_rows_to_context_bundle
from tools.memory_interop_shadow import (
    ADAPTER_ID,
    PROTOCOL_VERSION,
    SHADOW_DB_PATH_ENV,
    SHADOW_ENABLED_ENV,
    build_search_event,
    build_write_event,
    dispatch_shadow_event,
    is_forbidden_shadow_path,
    is_shadow_enabled,
    observe_memory_write,
    resolve_shadow_db_path,
    sha256_text,
)
from tools.memory_interop_shadow_worker import RECEIPT_TABLE, process_event
from tools.memory_write_adapter import write_memory

ROOT = Path(__file__).resolve().parents[1]
UTC = "2026-09-29T08:00:00+00:00"


def _make_posts_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT,
            layer TEXT,
            tags TEXT DEFAULT '',
            importance INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            created_at TEXT,
            resolved INTEGER DEFAULT 0,
            recall_count INTEGER DEFAULT 0,
            last_recalled_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO posts (id, type, content, created_at) VALUES (?, ?, ?, ?)",
        [
            (1, "MEMORY", "alpha needle", "2026-08-01"),
            (2, "MEMORY", "beta needle", "2026-08-02"),
            (3, "DIARY", "gamma other", "2026-08-03"),
        ],
    )
    conn.commit()
    conn.close()


def _format_memory_search(result: dict) -> str:
    items = []
    for post in result.get("posts") or []:
        date = str(post.get("created_at") or "")[:10]
        pinned = " 📌" if post.get("pinned") else ""
        content = str(post.get("content") or "")[:220]
        items.append(f"[#{post.get('id', '')} {post.get('type', '')} {date}{pinned}] {content}")
    return "\n---\n".join(items) or "没有找到相关记忆"


def _receipts(path: str) -> list[dict]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM {RECEIPT_TABLE} ORDER BY finished_at, receipt_id"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _wait_receipts(path: str, count: int = 1, timeout: float = 8.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        if Path(path).is_file():
            last = _receipts(path)
            if len(last) >= count:
                return last
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {count} receipts; have {len(last)}")


def _enabled_env(shadow_db: str) -> dict[str, str]:
    env = os.environ.copy()
    env[SHADOW_ENABLED_ENV] = "1"
    env[SHADOW_DB_PATH_ENV] = shadow_db
    return env


class GateTests(unittest.TestCase):
    def test_gate_defaults_off(self):
        self.assertFalse(is_shadow_enabled({}))
        self.assertFalse(is_shadow_enabled({SHADOW_ENABLED_ENV: ""}))
        self.assertFalse(is_shadow_enabled({SHADOW_ENABLED_ENV: "0"}))
        self.assertFalse(is_shadow_enabled({SHADOW_ENABLED_ENV: "false"}))
        self.assertFalse(is_shadow_enabled({SHADOW_ENABLED_ENV: "true"}))
        self.assertFalse(is_shadow_enabled({SHADOW_ENABLED_ENV: "yes"}))
        self.assertTrue(is_shadow_enabled({SHADOW_ENABLED_ENV: "1"}))

    def test_off_causes_zero_dispatch_and_creates_no_shadow_db(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = str(Path(folder) / "posts.db")
            shadow = str(Path(folder) / "shadow-receipts.db")
            _make_posts_db(posts)
            recorded: list[dict] = []
            environ = {SHADOW_ENABLED_ENV: "0", SHADOW_DB_PATH_ENV: shadow}
            with mock.patch(
                "tools.memory_interop_shadow.dispatch_shadow_event",
                side_effect=lambda event, **kwargs: recorded.append(event),
            ), mock.patch.dict(os.environ, environ, clear=False):
                wrote = write_memory(posts, content="new fact")
                searched = search_memories(posts, keyword="needle")
            self.assertEqual("CREATED", wrote["status"])
            self.assertEqual(2, len(searched["posts"]))
            self.assertEqual([], recorded)
            self.assertFalse(Path(shadow).exists())
            names = {path.name for path in Path(folder).iterdir()}
            self.assertNotIn("shadow-receipts.db", names)

    def test_valid_one_enables_dispatch(self):
        recorded: list[dict] = []
        environ = {SHADOW_ENABLED_ENV: "1", SHADOW_DB_PATH_ENV: "/tmp/shadow-receipts.db"}
        observe_memory_write(
            posts_db_path="/tmp/posts.db",
            result={"status": "CREATED", "id": 9},
            content="abc",
            source_surface="claude_code",
            environ=environ,
            dispatch=lambda event, **kwargs: recorded.append(event),
        )
        self.assertEqual(1, len(recorded))
        self.assertEqual("write", recorded[0]["operation"])
        self.assertEqual("9", recorded[0]["legacy_row_id"])

    def test_invalid_or_missing_shadow_db_fails_open(self):
        recorded: list[dict] = []
        with mock.patch(
            "tools.memory_interop_shadow.subprocess.Popen",
            side_effect=lambda *args, **kwargs: recorded.append((args, kwargs)),
        ):
            dispatch_shadow_event(
                {"posts_db_path": "/tmp/posts.db", "operation": "write"},
                environ={SHADOW_ENABLED_ENV: "1"},
            )
            dispatch_shadow_event(
                {"posts_db_path": "/tmp/posts.db", "operation": "write"},
                environ={
                    SHADOW_ENABLED_ENV: "1",
                    SHADOW_DB_PATH_ENV: "/opt/frontend/memories.db",
                },
            )
            dispatch_shadow_event(
                {"posts_db_path": "/tmp/posts.db", "operation": "write"},
                environ={
                    SHADOW_ENABLED_ENV: "1",
                    SHADOW_DB_PATH_ENV: "/tmp/posts.db",
                },
            )
        self.assertEqual([], recorded)
        self.assertIsNone(resolve_shadow_db_path({SHADOW_ENABLED_ENV: "1"}))
        self.assertTrue(is_forbidden_shadow_path("/opt/frontend/memories.db"))
        self.assertTrue(is_forbidden_shadow_path("/var/lib/ombre/store.db"))
        self.assertTrue(is_forbidden_shadow_path("/tmp/memory_kernel.db"))
        self.assertTrue(is_forbidden_shadow_path("/tmp/continuity.db"))


class AuthoritativeParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.posts = str(Path(self.temp.name) / "posts.db")
        self.shadow = str(Path(self.temp.name) / "shadow-receipts.db")
        _make_posts_db(self.posts)

    def tearDown(self):
        self.temp.cleanup()

    def test_search_result_and_formatted_text_unchanged_off_on_and_failures(self):
        off = search_memories(self.posts, keyword="needle")
        off_text = _format_memory_search(off)
        env = _enabled_env(self.shadow)
        with mock.patch.dict(os.environ, env, clear=False):
            on = search_memories(
                self.posts,
                keyword="needle",
                shadow_surface="claude_code",
            )
        self.assertEqual(off, on)
        self.assertEqual(off_text, _format_memory_search(on))
        self.assertEqual([2, 1], [row["id"] for row in on["posts"]])
        _wait_receipts(self.shadow, 1)
        with mock.patch(
            "tools.memory_interop_shadow.dispatch_shadow_event",
            side_effect=RuntimeError("dispatcher boom"),
        ), mock.patch.dict(os.environ, env, clear=False):
            dispatcher_failed = search_memories(self.posts, keyword="needle")
        self.assertEqual(off, dispatcher_failed)
        self.assertEqual(off_text, _format_memory_search(dispatcher_failed))
        with mock.patch(
            "tools.memory_interop_shadow.WORKER_MODULE",
            "tools.missing_shadow_worker",
        ), mock.patch.dict(os.environ, env, clear=False):
            worker_failed = search_memories(self.posts, keyword="needle")
        self.assertEqual(off, worker_failed)
        self.assertEqual(off_text, _format_memory_search(worker_failed))

    def test_write_result_unchanged_and_exactly_one_row(self):
        env = _enabled_env(self.shadow)
        with mock.patch.dict(os.environ, {SHADOW_ENABLED_ENV: "0"}, clear=False):
            off = write_memory(self.posts, content="  keep this  ")
        self.assertEqual("CREATED", off["status"])
        with mock.patch.dict(os.environ, env, clear=False):
            on = write_memory(
                self.posts,
                content="second fact",
                shadow_surface="internal_mcp",
            )
        self.assertEqual("CREATED", on["status"])
        self.assertEqual(off["id"] + 1, on["id"])
        with mock.patch(
            "tools.memory_interop_shadow.dispatch_shadow_event",
            side_effect=OSError("spawn failed"),
        ), mock.patch.dict(os.environ, env, clear=False):
            dispatcher_failed = write_memory(self.posts, content="third fact")
        self.assertEqual("CREATED", dispatcher_failed["status"])
        with mock.patch(
            "tools.memory_interop_shadow.WORKER_MODULE",
            "tools.missing_shadow_worker",
        ), mock.patch.dict(os.environ, env, clear=False):
            worker_failed = write_memory(self.posts, content="fourth fact")
        self.assertEqual("CREATED", worker_failed["status"])
        conn = sqlite3.connect(self.posts)
        rows = conn.execute("SELECT id, content FROM posts ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(
            [
                (1, "alpha needle"),
                (2, "beta needle"),
                (3, "gamma other"),
                (off["id"], "keep this"),
                (on["id"], "second fact"),
                (dispatcher_failed["id"], "third fact"),
                (worker_failed["id"], "fourth fact"),
            ],
            rows,
        )
        _wait_receipts(self.shadow, 1)

    def test_save_memory_called_exactly_once_per_write(self):
        with mock.patch(
            "tools.memory_tool.save_memory",
            return_value=41,
        ) as save:
            result = write_memory(
                self.posts,
                content="once",
                shadow_surface="api_relay",
            )
        self.assertEqual({"status": "CREATED", "id": 41}, result)
        save.assert_called_once()

    def test_invalid_write_does_not_dispatch(self):
        recorded: list[dict] = []
        env = _enabled_env(self.shadow)
        with mock.patch(
            "tools.memory_interop_shadow.observe_memory_write",
            side_effect=lambda **kwargs: recorded.append(kwargs),
        ), mock.patch.dict(os.environ, env, clear=False):
            invalid = write_memory(self.posts, content="   ")
        self.assertEqual({"status": "INVALID_CONTENT"}, invalid)
        self.assertEqual([], recorded)


class IsolationAndReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.posts = str(Path(self.temp.name) / "posts.db")
        self.shadow = str(Path(self.temp.name) / "shadow-receipts.db")
        _make_posts_db(self.posts)

    def tearDown(self):
        self.temp.cleanup()

    def _write_event(self, **changes):
        event = build_write_event(
            posts_db_path=self.posts,
            result={"status": "CREATED", "id": 2},
            content="beta needle",
            source_surface="claude_code",
            turn_id="turn-9",
            request_id="req-9",
            observed_at=UTC,
        )
        event.update(changes)
        event["shadow_db_path"] = self.shadow
        return event

    def _search_event(self, **changes):
        posts = search_memories(self.posts, keyword="needle")["posts"]
        event = build_search_event(
            posts_db_path=self.posts,
            posts=posts,
            keyword="needle",
            source_surface="internal_mcp",
            turn_id="turn-search",
            request_id="req-search",
            observed_at=UTC,
        )
        event.update(changes)
        event["shadow_db_path"] = self.shadow
        return event

    def test_no_kernel_or_ombre_call_from_worker(self):
        event = self._write_event()
        loaded_before = set(sys.modules)
        with mock.patch("tools.memory_kernel.MemoryKernel") as kernel:
            receipt = process_event(event)
        self.assertEqual("ok", receipt["status"])
        kernel.assert_not_called()
        self.assertNotIn("tools.ombre_adapter", set(sys.modules) - loaded_before)
        source = (ROOT / "tools" / "memory_interop_shadow_worker.py").read_text(encoding="utf-8")
        for forbidden in (
            "save_memory(",
            "MemoryKernel(",
            "create_evidence(",
            "tools.ombre_adapter",
            "AcceptedReview",
            "KernelCommitter",
        ):
            self.assertNotIn(forbidden, source)

    def test_legacy_db_never_receives_shadow_tables_and_receipt_db_is_separate(self):
        receipt = process_event(self._write_event())
        self.assertEqual("ok", receipt["status"])
        posts_conn = sqlite3.connect(self.posts)
        post_tables = {
            row[0]
            for row in posts_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        posts_conn.close()
        self.assertIn("posts", post_tables)
        self.assertNotIn(RECEIPT_TABLE, post_tables)
        self.assertTrue(Path(self.shadow).is_file())
        self.assertNotEqual(Path(self.posts).resolve(), Path(self.shadow).resolve())
        shadow_conn = sqlite3.connect(self.shadow)
        shadow_tables = {
            row[0]
            for row in shadow_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        shadow_conn.close()
        self.assertIn(RECEIPT_TABLE, shadow_tables)
        self.assertNotIn("posts", shadow_tables)

    def test_write_receipt_binds_row_id_and_verifies_content_hash(self):
        receipt = process_event(self._write_event())
        self.assertEqual("ok", receipt["status"])
        rows = _receipts(self.shadow)
        self.assertEqual(1, len(rows))
        self.assertEqual("2", json.loads(rows[0]["metadata_json"])["legacy_row_id"])
        self.assertIn("legacy://posts/2", rows[0]["ordered_source_refs_json"])
        self.assertEqual(sha256_text("beta needle"), json.loads(rows[0]["metadata_json"])["expected_content_sha256"])
        mismatch = process_event(
            self._write_event(
                expected_content_sha256="0" * 64,
                payload_fingerprint="a" * 64,
                event_key="write|claude_code|2|" + ("0" * 64),
            )
        )
        self.assertEqual("hash_mismatch", mismatch["status"])
        self.assertEqual("hash_mismatch", mismatch["error_code"])

    def test_search_preserves_ordered_ids_and_hashes(self):
        event = self._search_event()
        receipt = process_event(event)
        self.assertEqual("ok", receipt["status"])
        self.assertEqual(["2", "1"], event["ordered_row_ids"])
        self.assertEqual(
            [sha256_text("beta needle"), sha256_text("alpha needle")],
            event["ordered_content_hashes"],
        )
        self.assertEqual(
            ["legacy://posts/2", "legacy://posts/1"],
            json.loads(receipt["ordered_source_refs_json"]),
        )
        self.assertEqual(2, receipt["result_count"])

    def test_changed_or_missing_source_row_is_shadow_failure_only(self):
        missing = process_event(
            self._write_event(
                legacy_row_id="99",
                expected_content_sha256=sha256_text("x"),
                event_key="write|claude_code|99|" + sha256_text("x"),
                payload_fingerprint="b" * 64,
            )
        )
        self.assertEqual("missing_row", missing["status"])
        event = self._search_event()
        conn = sqlite3.connect(self.posts)
        conn.execute("UPDATE posts SET content = ? WHERE id = 2", ("changed later",))
        conn.commit()
        conn.close()
        changed = process_event(event)
        self.assertEqual("hash_mismatch", changed["status"])
        conn = sqlite3.connect(self.posts)
        stored = conn.execute("SELECT content FROM posts WHERE id = 2").fetchone()[0]
        conn.close()
        self.assertEqual("changed later", stored)
        searched = search_memories(self.posts, keyword="needle")
        self.assertEqual(["alpha needle"], [row["content"] for row in searched["posts"]])

    def test_durable_receipt_omits_raw_query_and_content(self):
        process_event(self._search_event())
        process_event(self._write_event())
        raw = Path(self.shadow).read_bytes()
        self.assertNotIn(b"needle", raw)
        self.assertNotIn(b"beta needle", raw)
        self.assertNotIn(b"password", raw)
        self.assertNotIn(b"lease", raw)
        rows = _receipts(self.shadow)
        for row in rows:
            blob = json.dumps(row, ensure_ascii=False)
            self.assertNotIn("beta needle", blob)
            self.assertIn("sha256", json.dumps(json.loads(row["metadata_json"])))

    def test_idempotent_duplicate_and_conflicting_payload(self):
        first = process_event(self._write_event())
        second = process_event(self._write_event())
        self.assertEqual("ok", first["status"])
        self.assertIn(second["status"], {"ok", "duplicate"})
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(1, len(_receipts(self.shadow)))
        conflict = process_event(
            self._write_event(
                payload_fingerprint="f" * 64,
                capability_id="memory.write",
            )
        )
        self.assertEqual("conflict", conflict["status"])
        self.assertEqual("event_conflict", conflict["error_code"])
        self.assertEqual(2, len(_receipts(self.shadow)))
        statuses = {row["status"] for row in _receipts(self.shadow)}
        self.assertEqual({"ok", "conflict"}, statuses)

    def test_adapter_id_guard_remains_enforced(self):
        receipt = process_event(self._write_event(adapter_id="adapter.example"))
        self.assertEqual("translation_failed", receipt["status"])
        self.assertEqual("adapter_id_mismatch", receipt["error_code"])

    def test_explicit_row_helper_preserves_authoritative_order(self):
        conn = sqlite3.connect(self.posts)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM posts ORDER BY id ASC")]
        conn.close()
        from tools.memory_interop import InteropRequestContext, MEMORY_INTEROP_PROTOCOL_VERSION

        bundle = legacy_rows_to_context_bundle(
            list(reversed(rows)),
            request_context=InteropRequestContext(
                protocol_version=MEMORY_INTEROP_PROTOCOL_VERSION,
                request_id="req-order",
                trigger_kind="live_shadow",
                adapter_id=ADAPTER_ID,
                requested_at=UTC,
            ),
            bundle_id="bundle-order",
            generated_at=UTC,
            order="authoritative",
        )
        self.assertEqual(
            ["legacy.posts.3", "legacy.posts.2", "legacy.posts.1"],
            [item.item_id for item in bundle.items],
        )


class SurfaceAndAuthorityTests(unittest.TestCase):
    def test_observer_has_no_hot_path_db_or_kernel(self):
        source = (ROOT / "tools" / "memory_interop_shadow.py").read_text(encoding="utf-8")
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("memory_kernel", source)
        self.assertNotIn("memory_interop_legacy", source)
        self.assertNotIn("ombre_adapter", source)
        self.assertIn("shell=False", source)
        self.assertIn("DEVNULL", source)

    def test_home_and_legacy_gateway_search_remain_unwired(self):
        home = (ROOT / "mcp-http-server.js").read_text(encoding="utf-8")
        self.assertIn("/api/posts?search=", home)
        self.assertIn("limit=8", home)
        self.assertNotIn("memory_interop_shadow", home)
        gateway = (ROOT / "gateway.py").read_text(encoding="utf-8")
        self.assertIn("res = memory_tool.search_memories(args.get('keyword', ''))", gateway)
        self.assertIn("shadow_surface='api_relay'", gateway)
        start = gateway.index("if name == 'search_memories':")
        snippet = gateway[start:start + 250]
        self.assertNotIn("observe_memory_search", snippet)
        self.assertNotIn("shadow_surface", snippet)

    def test_capability_proxy_and_internal_mcp_pass_observation_surface_only(self):
        proxy = (ROOT / "capability-proxy-mcp-server.js").read_text(encoding="utf-8")
        self.assertIn("payload.shadow_surface = 'claude_code'", proxy)
        self.assertIn("tools.memory_internal_adapter", proxy)
        self.assertIn("tools.memory_write_adapter", proxy)
        internal = (ROOT / "internal-mcp-server.js").read_text(encoding="utf-8")
        self.assertIn("payload.shadow_surface = payload.shadow_surface || 'internal_mcp'", internal)
        adapter = (ROOT / "tools" / "memory_internal_adapter.py").read_text(encoding="utf-8")
        self.assertNotRegex(adapter, r"TODO_INTERNAL_DB_PATH|os\.environ|/opt/frontend/memories\.db")
        self.assertNotRegex(adapter, r"memory_tool|memory_library|ombre_adapter")
        self.assertNotRegex(adapter, r"SELECT|LIKE|ORDER BY|INSERT|UPDATE|DELETE|commit\s*\(")

    def test_uh_a0_lease_behavior_unchanged(self):
        from tools.capability_state import RUNTIME_STATE_INHERIT, RUNTIME_STATE_OFF

        chat = issue_turn_lease(turn_id="chat-shadow", turn_mode="chat", issued_from="default_policy")
        wake = issue_turn_lease(turn_id="wake-shadow", turn_mode="wake", issued_from="default_policy")
        with mock.patch(
            "tools.execution_fence.read_capability_state",
            return_value=RUNTIME_STATE_INHERIT,
        ):
            for tool_name in ("mcp__internal__write_memory", "memory_write"):
                self.assertEqual(
                    evaluate_tool_call(tool_name, {"content": "x"}, chat)["lease_decision"],
                    "ALLOW",
                )
                self.assertEqual(
                    evaluate_tool_call(tool_name, {"content": "x"}, wake)["lease_decision"],
                    "ALLOW",
                )
            denied = issue_turn_lease(
                turn_id="task-shadow",
                turn_mode="task",
                issued_from="default_policy",
            )
            self.assertEqual(
                evaluate_tool_call("memory_write", {"content": "x"}, denied)["lease_decision"],
                "DENIED_CAPABILITY",
            )
        with mock.patch(
            "tools.execution_fence.read_capability_state",
            return_value=RUNTIME_STATE_OFF,
        ):
            self.assertEqual(
                evaluate_tool_call("memory_write", {"content": "x"}, chat)["lease_decision"],
                "DENIED_CAPABILITY",
            )

    def test_stdin_adapters_keep_authoritative_json_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = str(Path(folder) / "posts.db")
            shadow = str(Path(folder) / "shadow.db")
            _make_posts_db(posts)
            env = _enabled_env(shadow)
            write_payload = json.dumps(
                {
                    "operation": "write_memory",
                    "db_path": posts,
                    "content": "via stdin",
                    "shadow_surface": "claude_code",
                    "shadow_turn_id": "turn-stdin",
                },
                ensure_ascii=False,
            )
            search_payload = json.dumps(
                {
                    "operation": "search_memories",
                    "db_path": posts,
                    "keyword": "needle",
                    "shadow_surface": "internal_mcp",
                },
                ensure_ascii=False,
            )
            wrote = subprocess.run(
                [sys.executable, "-m", "tools.memory_write_adapter"],
                cwd=ROOT,
                env=env,
                input=write_payload,
                text=True,
                capture_output=True,
                check=False,
            )
            searched = subprocess.run(
                [sys.executable, "-m", "tools.memory_internal_adapter"],
                cwd=ROOT,
                env=env,
                input=search_payload,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, wrote.returncode, wrote.stderr)
            self.assertEqual(0, searched.returncode, searched.stderr)
            write_result = json.loads(wrote.stdout)
            search_result = json.loads(searched.stdout)
            self.assertEqual("CREATED", write_result["status"])
            self.assertEqual({"status", "id"}, set(write_result))
            self.assertEqual(["posts"], list(search_result))
            self.assertEqual([2, 1], [row["id"] for row in search_result["posts"]])
            receipts = _wait_receipts(shadow, 2)
            surfaces = {row["source_surface"] for row in receipts}
            self.assertEqual({"claude_code", "internal_mcp"}, surfaces)


class AsyncDispatchTests(unittest.TestCase):
    def test_dispatch_does_not_wait_for_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            posts = str(Path(folder) / "posts.db")
            shadow = str(Path(folder) / "shadow.db")
            _make_posts_db(posts)
            event = build_write_event(
                posts_db_path=posts,
                result={"status": "CREATED", "id": 1},
                content="alpha needle",
                source_surface="api_relay",
                observed_at=UTC,
            )
            started = time.monotonic()
            with mock.patch.dict(os.environ, _enabled_env(shadow), clear=False):
                dispatch_shadow_event(event)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5)
            receipts = _wait_receipts(shadow, 1)
            self.assertEqual("ok", receipts[0]["status"])
            self.assertEqual("write", receipts[0]["operation"])
            self.assertEqual(PROTOCOL_VERSION, receipts[0]["protocol_version"])


if __name__ == "__main__":
    unittest.main()
