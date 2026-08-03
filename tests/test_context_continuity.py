"""Regression tests for wake and tool-result context continuity."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "context_continuity_under_test",
    os.path.join(ROOT, "chat", "context_continuity.py"),
)
_CONTEXT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CONTEXT)

build_system_with_wake_claim = _CONTEXT.build_system_with_wake_claim
consume_wake_ids = _CONTEXT.consume_wake_ids
format_tool_history = _CONTEXT.format_tool_history
is_pending_user_turn = _CONTEXT.is_pending_user_turn


class ContextContinuityTests(unittest.TestCase):
    def setUp(self):
        # Deploy preflight runs on the VPS where config_store may have
        # DAILY_SOFT_WINDOW_ENABLED=1. These fixtures only create wake_log /
        # chat_messages — force the legacy wake path so production flags cannot
        # pull resolve_canonical_context_row_conn onto a missing daily_contexts.
        self._soft_window_patch = mock.patch(
            'chat.window_identity.soft_window_enabled', return_value=False,
        )
        self._soft_window_patch.start()
        self.addCleanup(self._soft_window_patch.stop)

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        conn = self.db()
        conn.executescript(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY,
                content TEXT,
                consumed INTEGER DEFAULT 0
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY,
                author TEXT,
                content TEXT
            );
            INSERT INTO wake_log(id, content, consumed) VALUES
                (1, 'first wake', 0), (2, 'old wake', 1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def db(self):
        conn = sqlite3.connect(self.tmp.name)
        conn.row_factory = sqlite3.Row
        return conn

    def test_wake_visible_during_build_and_consumed_after_success(self):
        def builder(**_kwargs):
            conn = self.db()
            rows = conn.execute(
                "SELECT content FROM wake_log WHERE consumed=0 ORDER BY id"
            ).fetchall()
            conn.close()
            return "|".join(row["content"] for row in rows)

        system, claim = build_system_with_wake_claim(
            builder, self.db, user_turn=True
        )
        self.assertEqual(system, "first wake")
        self.assertEqual(claim, [1])

        # A failed generation leaves the snapshot pending for a retry.
        conn = self.db()
        self.assertEqual(
            conn.execute("SELECT consumed FROM wake_log WHERE id=1").fetchone()[0],
            0,
        )
        conn.close()

        self.assertEqual(consume_wake_ids(self.db, claim), 1)
        conn = self.db()
        self.assertEqual(
            conn.execute("SELECT consumed FROM wake_log WHERE id=1").fetchone()[0],
            1,
        )
        conn.close()

    def test_claim_only_consumes_snapshotted_wakes(self):
        _, claim = build_system_with_wake_claim(
            lambda: "system", self.db, user_turn=True
        )
        conn = self.db()
        conn.execute(
            "INSERT INTO wake_log(id, content, consumed) VALUES (3, 'later', 0)"
        )
        conn.commit()
        conn.close()

        consume_wake_ids(self.db, claim)
        conn = self.db()
        states = dict(conn.execute("SELECT id, consumed FROM wake_log"))
        conn.close()
        self.assertEqual(states, {1: 1, 2: 1, 3: 0})

    def test_frontend_turn_id_cannot_be_replayed_after_reply(self):
        conn = self.db()
        conn.execute(
            "INSERT INTO chat_messages(id, author, content) VALUES (10, 'hayana', 'hi')"
        )
        conn.commit()
        conn.close()
        self.assertTrue(is_pending_user_turn(self.db, 10))

        conn = self.db()
        conn.execute(
            "INSERT INTO chat_messages(id, author, content) "
            "VALUES (11, 'assistant', 'hello')"
        )
        conn.commit()
        conn.close()
        self.assertFalse(is_pending_user_turn(self.db, 10))

    def test_tool_results_are_rendered_with_provider_caps(self):
        payload = json.dumps([
            {
                "name": "web_search",
                "args": {"q": "wake"},
                "result": "x" * 6000,
                "success": True,
            },
            {
                "name": "get_light_status",
                "args": {},
                "result": "y" * 50,
                "success": False,
            },
        ])
        text = format_tool_history(payload, cap_small=20, cap_large=8000)
        self.assertIn("x" * 6000, text)
        self.assertIn("get_light_status（失败）", text)
        self.assertIn("y" * 20 + "…(已截断)", text)

    def test_invalid_tool_history_is_ignored(self):
        self.assertEqual(format_tool_history("not-json"), "")

    def test_gateway_and_frontend_keep_continuity_wiring(self):
        gateway = open(
            os.path.join(ROOT, "gateway.py"), encoding="utf-8"
        ).read()
        frontend = open(
            os.path.join(ROOT, "static", "chat.html"), encoding="utf-8"
        ).read()
        app_source = open(
            os.path.join(ROOT, "app.py"), encoding="utf-8"
        ).read()
        deploy_source = open(
            os.path.join(ROOT, "scripts", "deploy-frontend.sh"),
            encoding="utf-8",
        ).read()
        gitignore_source = open(
            os.path.join(ROOT, ".gitignore"), encoding="utf-8"
        ).read().splitlines()

        self.assertNotIn(
            "UPDATE wake_log SET consumed=1 WHERE consumed=0", gateway
        )
        # API 路径仍用 build_system_with_wake_claim；CC resident 用 capture_pending_wake_ids
        self.assertGreaterEqual(
            gateway.count("build_system_with_wake_claim(")
            + gateway.count("capture_pending_wake_ids("),
            3,
        )
        self.assertGreaterEqual(
            gateway.count("is_pending_user_turn("), 3
        )
        self.assertIn(
            "format_tool_history as _format_tool_history", gateway
        )
        self.assertNotIn("str(rc or '')[:2000]", gateway)
        self.assertIn("user_message_id", frontend)
        self.assertIn('"message_id": message_id', app_source)
        self.assertIn("for attempt in 1 2 3 4 5", deploy_source)
        self.assertIn("retrying in 3s", deploy_source)
        self.assertIn(
            'git merge-base --is-ancestor "$current_sha" "$target_sha"',
            deploy_source,
        )
        self.assertIn("Production-only commits:", deploy_source)
        self.assertIn("deploy/recovered-production-shas.txt", deploy_source)
        self.assertIn("snapshot_runtime", deploy_source)
        self.assertIn("restore_runtime", deploy_source)
        self.assertIn("':(exclude)attachments.db'", deploy_source)
        self.assertIn("':(exclude)memories.db-shm'", deploy_source)
        self.assertIn("':(exclude)memories.db-wal'", deploy_source)
        self.assertIn("memories.db-shm", gitignore_source)
        self.assertIn("memories.db-wal", gitignore_source)
        self.assertIn("from PIL import Image", deploy_source)
        self.assertIn("import frontmatter", deploy_source)
        self.assertIn("requirements.txt", deploy_source)
        moments_auth = open(
            os.path.join(ROOT, "moments_auth.py"), encoding="utf-8"
        ).read()
        config_store = open(
            os.path.join(ROOT, "config_store.py"), encoding="utf-8"
        ).read()
        mcp_source = open(
            os.path.join(ROOT, "mcp-http-server.js"), encoding="utf-8"
        ).read()
        self.assertIn("MOMENTS_OWNER_TOKEN", moments_auth)
        self.assertIn("MOMENTS_OWNER_TOKEN", config_store)
        self.assertIn("MOMENTS_OWNER_TOKEN", mcp_source)
        self.assertIn("Authorization: `Bearer ${ownerToken}`", mcp_source)


if __name__ == "__main__":
    unittest.main()
