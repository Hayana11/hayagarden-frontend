"""Nexus runtime + API lifecycle tests covering M5 items 1-16."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402

from nexus_adapters import FakeClaudeAdapter, FakeCodexAdapter  # noqa: E402
from nexus_events import FROZEN_EVENTS, PUBLIC_FIELDS, make_event, redact_value  # noqa: E402
from nexus_git import git_summary  # noqa: E402
from nexus_paths import NexusPathError, resolve_under_nexus  # noqa: E402
from nexus_routes import create_nexus_blueprint  # noqa: E402
from nexus_runtime import NexusBusyError, NexusRuntime  # noqa: E402


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "nexus@test"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "nexus"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("nexus\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


def _drain_events(runtime: NexusRuntime, turn_id: str, timeout: float = 5.0) -> list[dict]:
    out = []
    deadline = time.time() + timeout
    for event in runtime.iter_events(turn_id):
        out.append(event)
        if event["event"] in {"done", "err"}:
            break
        if time.time() > deadline:
            break
    return out


class NexusRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_git_repo(self.root)
        self.claude = FakeClaudeAdapter(self.root)
        self.codex = FakeCodexAdapter(self.root)
        self.runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": self.claude, "codex": self.codex},
            context_usage_getter=lambda: None,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_claude_fake_event_chain(self):
        result = self.runtime.start_turn("claude", "edit fixture")
        events = _drain_events(self.runtime, result["turn_id"])
        names = [e["event"] for e in events]
        self.assertIn("meta", names)
        self.assertIn("think", names)
        self.assertIn("tool_use", names)
        self.assertIn("tool_result", names)
        self.assertIn("text", names)
        self.assertIn("git", names)
        self.assertEqual(names[-1], "done")
        self.assertTrue((self.root / "nexus_fixture.txt").exists())
        for event in events:
            for key in PUBLIC_FIELDS:
                self.assertIn(key, event)
            self.assertIn(event["event"], FROZEN_EVENTS)

    def test_02_codex_fake_event_chain(self):
        result = self.runtime.start_turn("codex", "edit fixture")
        events = _drain_events(self.runtime, result["turn_id"])
        names = [e["event"] for e in events]
        self.assertIn("tool_use", names)
        self.assertEqual(names[-1], "done")
        self.assertTrue((self.root / "nexus_fixture_codex.txt").exists())

    def test_03_session_continuity(self):
        r1 = self.runtime.start_turn("claude", "one")
        _drain_events(self.runtime, r1["turn_id"])
        sid1 = self.claude.session_id
        r2 = self.runtime.start_turn("claude", "two")
        events = _drain_events(self.runtime, r2["turn_id"])
        self.assertEqual(self.claude.session_id, sid1)
        self.assertEqual(self.claude.turns, 2)
        meta = [e for e in events if e["event"] == "meta" and e["data"].get("turn") == 2]
        self.assertTrue(meta)

        c1 = self.runtime.start_turn("codex", "one")
        _drain_events(self.runtime, c1["turn_id"])
        csid = self.codex.session_id
        c2 = self.runtime.start_turn("codex", "two")
        _drain_events(self.runtime, c2["turn_id"])
        self.assertEqual(self.codex.session_id, csid)
        self.assertEqual(self.codex.turns, 2)

    def test_04_cannot_run_both(self):
        self.claude.hang = True
        first = self.runtime.start_turn("claude", "hang")
        time.sleep(0.05)
        with self.assertRaises(NexusBusyError):
            self.runtime.start_turn("codex", "should-busy")
        self.runtime.interrupt(first["turn_id"])
        _drain_events(self.runtime, first["turn_id"], timeout=3)

    def test_05_lock_released_after_success(self):
        r1 = self.runtime.start_turn("claude", "ok")
        _drain_events(self.runtime, r1["turn_id"])
        r2 = self.runtime.start_turn("codex", "ok2")
        events = _drain_events(self.runtime, r2["turn_id"])
        self.assertEqual(events[-1]["event"], "done")

    def test_06_lock_released_after_error(self):
        self.claude.fail = True
        r1 = self.runtime.start_turn("claude", "fail")
        events = _drain_events(self.runtime, r1["turn_id"])
        self.assertEqual(events[-1]["event"], "err")
        r2 = self.runtime.start_turn("codex", "after-fail")
        events2 = _drain_events(self.runtime, r2["turn_id"])
        self.assertEqual(events2[-1]["event"], "done")

    def test_07_lock_released_after_interrupt(self):
        self.codex.hang = True
        r1 = self.runtime.start_turn("codex", "hang")
        time.sleep(0.05)
        ir = self.runtime.interrupt(r1["turn_id"])
        self.assertTrue(ir["interrupted"])
        events = _drain_events(self.runtime, r1["turn_id"], timeout=3)
        self.assertEqual(events[-1]["event"], "err")
        self.assertEqual(events[-1]["data"].get("code"), "interrupted")
        r2 = self.runtime.start_turn("claude", "after-interrupt")
        events2 = _drain_events(self.runtime, r2["turn_id"])
        self.assertEqual(events2[-1]["event"], "done")

    def test_08_old_interrupt_does_not_kill_new_turn(self):
        old = self.runtime.start_turn("claude", "old")
        _drain_events(self.runtime, old["turn_id"])
        self.codex.hang = True
        new = self.runtime.start_turn("codex", "new-hang")
        time.sleep(0.05)
        stale = self.runtime.interrupt(old["turn_id"])
        self.assertFalse(stale["interrupted"])
        self.assertEqual(stale["detail"], "not_active")
        # New turn still running — interrupt it properly.
        live = self.runtime.interrupt(new["turn_id"])
        self.assertTrue(live["interrupted"])
        events = _drain_events(self.runtime, new["turn_id"], timeout=3)
        self.assertEqual(events[-1]["data"].get("code"), "interrupted")

    def test_13_sequence_monotonic_single_terminal(self):
        result = self.runtime.start_turn("claude", "seq")
        events = _drain_events(self.runtime, result["turn_id"])
        seqs = [e["sequence"] for e in events]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        terminals = [e for e in events if e["event"] in {"done", "err"}]
        self.assertEqual(len(terminals), 1)

    def test_14_unknown_and_sensitive_redaction(self):
        result = self.runtime.start_turn("codex", "redact")
        events = _drain_events(self.runtime, result["turn_id"])
        # Fake codex emits a status with token — must be redacted.
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("SECRET_TOKEN_VALUE", blob)
        status_events = [e for e in events if e["event"] == "status"]
        self.assertTrue(status_events)
        # Unknown provider kinds become status (not a 10th event name)
        for e in events:
            self.assertIn(e["event"], FROZEN_EVENTS)

    def test_git_summary_fixed_workspace(self):
        (self.root / "extra.txt").write_text("x\n", encoding="utf-8")
        summary = git_summary(self.root)
        for key in ("branch", "changed_files", "diff_stat", "additions", "deletions", "clean"):
            self.assertIn(key, summary)
        self.assertIn("extra.txt", summary["changed_files"])
        self.assertFalse(summary["clean"])


class NexusRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_git_repo(self.root)
        self.claude = FakeClaudeAdapter(self.root)
        self.codex = FakeCodexAdapter(self.root)
        self.runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": self.claude, "codex": self.codex},
            context_usage_getter=lambda: None,
        )
        self.app = Flask("nexus-test")
        self.app.register_blueprint(
            create_nexus_blueprint(self.runtime, owner_guard=lambda _req: None)
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_11_reject_control_fields(self):
        for field, value in [
            ("cwd", "/tmp"),
            ("shell", "bash"),
            ("env", {"A": "1"}),
            ("sandbox", "danger-full-access"),
            ("command", "rm -rf /"),
            ("allowedTools", ["Bash"]),
            ("mcp-config", "{}"),
            ("push", True),
        ]:
            resp = self.client.post(
                "/api/nexus/turn",
                json={"agent": "claude", "instruction": "x", field: value},
            )
            self.assertEqual(resp.status_code, 400, field)
            body = resp.get_json()
            self.assertEqual(body["code"], "unexpected_fields")
            self.assertIn(field, body["rejected_fields"])

    def test_busy_423(self):
        self.claude.hang = True
        first = self.client.post("/api/nexus/turn", json={"agent": "claude", "instruction": "hang"})
        self.assertEqual(first.status_code, 202)
        second = self.client.post("/api/nexus/turn", json={"agent": "codex", "instruction": "busy"})
        self.assertEqual(second.status_code, 423)
        self.assertEqual(second.get_json()["code"], "nexus_busy")
        turn_id = first.get_json()["turn_id"]
        self.client.post(f"/api/nexus/turn/{turn_id}/interrupt")
        # drain
        with self.client.get(f"/api/nexus/turn/{turn_id}/events") as resp:
            list(resp.response)

    def test_events_url_and_sse(self):
        resp = self.client.post("/api/nexus/turn", json={"agent": "claude", "instruction": "sse"})
        body = resp.get_json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["events_url"], f"/api/nexus/turn/{body['turn_id']}/events")
        with self.client.get(body["events_url"]) as stream:
            self.assertEqual(stream.status_code, 200)
            self.assertTrue(stream.content_type.startswith("text/event-stream"))
            payload = b"".join(stream.response).decode("utf-8", errors="replace")
        self.assertIn("event: done", payload)
        self.assertIn('"sequence":', payload)

    def test_git_endpoint_frozen_fields(self):
        resp = self.client.get("/api/nexus/git")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(
            set(data.keys()),
            {"branch", "changed_files", "diff_stat", "additions", "deletions", "clean"},
        )

    def test_status_and_turns(self):
        resp = self.client.get("/api/nexus/status")
        self.assertEqual(resp.status_code, 200)
        status = resp.get_json()
        self.assertIn("capabilities", status)
        self.assertFalse(status["capabilities"]["last_event_id"])
        self.assertEqual(status["capabilities"]["busy_http_status"], 423)
        self.assertIsNone(status["context_usage"])
        r = self.client.post("/api/nexus/turn", json={"agent": "codex", "instruction": "hist"})
        turn_id = r.get_json()["turn_id"]
        with self.client.get(f"/api/nexus/turn/{turn_id}/events") as stream:
            list(stream.response)
        turns = self.client.get("/api/nexus/turns").get_json()["turns"]
        self.assertTrue(any(t["turn_id"] == turn_id for t in turns))
        self.assertLessEqual(len(turns), 50)

    def test_interrupt_idempotent(self):
        self.claude.hang = True
        r = self.client.post("/api/nexus/turn", json={"agent": "claude", "instruction": "hang"})
        turn_id = r.get_json()["turn_id"]
        a = self.client.post(f"/api/nexus/turn/{turn_id}/interrupt").get_json()
        b = self.client.post(f"/api/nexus/turn/{turn_id}/interrupt").get_json()
        self.assertTrue(a["ok"])
        self.assertTrue(b["ok"])
        with self.client.get(f"/api/nexus/turn/{turn_id}/events") as stream:
            list(stream.response)

    def test_instruction_limit(self):
        resp = self.client.post(
            "/api/nexus/turn",
            json={"agent": "claude", "instruction": "x" * 8001},
        )
        self.assertEqual(resp.status_code, 400)


class NexusSafetyStaticTests(unittest.TestCase):
    def test_15_source_does_not_touch_forbidden_targets(self):
        nexus_files = [
            Path(ROOT) / "nexus_paths.py",
            Path(ROOT) / "nexus_events.py",
            Path(ROOT) / "nexus_git.py",
            Path(ROOT) / "nexus_adapters.py",
            Path(ROOT) / "nexus_runtime.py",
            Path(ROOT) / "nexus_routes.py",
        ]
        for path in nexus_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("memories.db", text)
            self.assertNotIn("shell=True", text.replace(" ", ""))
            self.assertNotIn('sandbox="danger-full-access"', text)
            self.assertNotIn("sandbox='danger-full-access'", text)
            self.assertNotIn("group_chat_store", text)
        # Codex adapter must not use the global client singleton.
        adapters = (Path(ROOT) / "nexus_adapters.py").read_text(encoding="utf-8")
        self.assertNotIn("codex_app_server.client", adapters)

    def test_redact_helper(self):
        data = redact_value({"token": "abc", "ok": True, "path": "/opt/frontend/x"})
        self.assertEqual(data["token"], "[redacted]")
        self.assertEqual(data["ok"], True)


if __name__ == "__main__":
    unittest.main()
