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
import unittest.mock  # noqa: E402


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


class NexusR1RepairTests(unittest.TestCase):
    """Minimal verification for PR #158 hard-blocker repairs."""

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
        self.app = Flask("nexus-r1")
        self.app.register_blueprint(
            create_nexus_blueprint(self.runtime, owner_guard=lambda _req: None)
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_sse_turn_json_404(self):
        resp = self.client.get("/api/nexus/turn/does-not-exist/events")
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(resp.is_json)
        body = resp.get_json()
        self.assertEqual(body["code"], "turn_not_found")
        self.assertFalse(str(resp.content_type).startswith("text/event-stream"))

    def test_subscriber_overflow_json_429(self):
        from nexus_runtime import SUBSCRIBER_LIMIT

        r = self.client.post("/api/nexus/turn", json={"agent": "claude", "instruction": "sub"})
        turn_id = r.get_json()["turn_id"]
        # Reserve up to the limit via runtime API, then HTTP must 429 before SSE.
        holders = []
        for _ in range(SUBSCRIBER_LIMIT):
            holders.append(self.runtime.reserve_event_subscription(turn_id))
        try:
            resp = self.client.get(f"/api/nexus/turn/{turn_id}/events")
            self.assertEqual(resp.status_code, 429)
            self.assertTrue(resp.is_json)
            self.assertEqual(resp.get_json()["code"], "too_many_subscribers")
        finally:
            for _ in holders:
                self.runtime.release_event_subscription(turn_id)
            with self.client.get(f"/api/nexus/turn/{turn_id}/events") as stream:
                list(stream.response)

    def test_slow_subscriber_does_not_block_producer(self):
        r = self.runtime.start_turn("claude", "slow-sub")
        turn_id = r["turn_id"]
        turn = self.runtime.reserve_event_subscription(turn_id)
        produced = {"n": 0}
        done = threading.Event()

        def producer_wait():
            # Wait until turn finishes emitting; must not stall on slow reader.
            deadline = time.time() + 3
            while time.time() < deadline:
                if turn.terminal and turn.sequence >= 3:
                    produced["n"] = turn.sequence
                    done.set()
                    return
                time.sleep(0.01)

        threading.Thread(target=producer_wait, daemon=True).start()
        # Slow subscriber holds condition briefly but yields outside lock.
        events = []
        for event in self.runtime.iter_events_for_turn(turn, after_sequence=0):
            events.append(event)
            time.sleep(0.05)  # slow consumer
            if event["event"] in {"done", "err"}:
                break
        self.runtime.release_event_subscription(turn_id)
        self.assertTrue(done.wait(2), "producer stalled behind slow subscriber")
        self.assertGreaterEqual(produced["n"], 3)
        self.assertEqual(events[-1]["event"], "done")

    def test_interrupt_unique_interrupted_err_drops_content(self):
        self.claude.hang = True
        self.claude.emit_after_interrupt = [
            ("text", {"text": "should-drop"}),
            ("think", {"text": "should-drop"}),
            ("tool_use", {"name": "x"}),
            ("done", {"ok": True}),
        ]
        r = self.runtime.start_turn("claude", "interrupt-race")
        time.sleep(0.05)
        ir = self.runtime.interrupt(r["turn_id"])
        self.assertTrue(ir["interrupted"])
        events = _drain_events(self.runtime, r["turn_id"], timeout=3)
        terminals = [e for e in events if e["event"] in {"done", "err"}]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["event"], "err")
        # Competitive done while interrupting is not a confirmed provider stop.
        self.assertEqual(terminals[0]["data"]["code"], "interrupt_unconfirmed")
        turn = self.runtime._turns[r["turn_id"]]
        self.assertEqual(turn.state, "error")
        self.assertEqual(turn.error_code, "interrupt_unconfirmed")
        # No post-interrupt content events.
        for e in events:
            if e["event"] in {"text", "think", "tool_use", "tool_result", "git", "done"}:
                # meta/accepted and pre-interrupt think may exist; only fail if
                # payload matches the injected competitive content.
                if e["event"] == "think" and e["data"].get("text") == "planning edit":
                    continue
                if e["event"] in {"text", "think", "tool_use"} and (
                    e["data"].get("text") == "should-drop" or e["data"].get("name") == "x"
                ):
                    self.fail(f"content/done leaked after interrupt: {e}")
                if e["event"] in {"git", "done"}:
                    self.fail(f"content/done leaked after interrupt: {e}")
        self.assertGreaterEqual(self.claude.ensure_stopped_calls, 1)

    def test_runtime_interrupt_not_blocked_by_adapter(self):
        self.codex.hang = True
        self.codex.block_interrupt.set()
        r = self.runtime.start_turn("codex", "block-int")
        time.sleep(0.05)
        finished = {}

        def do_interrupt():
            t0 = time.time()
            finished["result"] = self.runtime.interrupt(r["turn_id"])
            finished["elapsed"] = time.time() - t0

        th = threading.Thread(target=do_interrupt, daemon=True)
        th.start()
        self.assertTrue(self.codex.interrupt_entered.wait(1))
        # While adapter.request_interrupt is blocked, runtime lock must be free
        # enough for status / busy checks.
        status = self.runtime.status()
        self.assertIn("capabilities", status)
        with self.assertRaises(NexusBusyError):
            self.runtime.start_turn("claude", "should-busy")
        self.codex.block_interrupt.clear()
        th.join(timeout=2)
        self.assertTrue(finished.get("result", {}).get("interrupted"))
        self.assertLess(finished.get("elapsed", 99), 1.5)
        events = _drain_events(self.runtime, r["turn_id"], timeout=3)
        self.assertEqual(events[-1]["data"].get("code"), "interrupted")
        # Next turn can start after old turn ends.
        nxt = self.runtime.start_turn("claude", "after")
        ev2 = _drain_events(self.runtime, nxt["turn_id"])
        self.assertEqual(ev2[-1]["event"], "done")

    def test_claude_partial_final_multiblock_no_dup(self):
        from nexus_adapters import ClaudeStreamNormalizer

        n = ClaudeStreamNormalizer()
        events = []
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "plan-"},
                        },
                    }
                )
            )
        )
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "thinking_delta", "thinking": "ning"},
                        },
                    }
                )
            )
        )
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 1,
                            "delta": {"type": "text_delta", "text": "hello "},
                        },
                    }
                )
            )
        )
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "stream_event",
                        "event": {
                            "type": "content_block_delta",
                            "index": 1,
                            "delta": {"type": "text_delta", "text": "world"},
                        },
                    }
                )
            )
        )
        # Final assistant message with full blocks — must not re-send streamed text/think,
        # but must emit tool_use and any non-streamed blocks.
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "thinking", "thinking": "planning"},
                                {"type": "text", "text": "hello world"},
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "Write",
                                    "input": {"path": "a.txt"},
                                },
                            ]
                        },
                    }
                )
            )
        )
        events.extend(
            n.feed(
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": "ok",
                                    "is_error": False,
                                }
                            ]
                        },
                    }
                )
            )
        )
        names = [e[0] for e in events]
        self.assertEqual(names.count("think"), 2)  # only deltas
        self.assertEqual(names.count("text"), 2)  # only deltas
        self.assertEqual(names.count("tool_use"), 1)
        self.assertEqual(names.count("tool_result"), 1)
        # Order: think deltas, text deltas, then tool_use, then tool_result
        self.assertEqual(names, ["think", "think", "text", "text", "tool_use", "tool_result"])
        joined_text = "".join(e[1]["text"] for e in events if e[0] == "text")
        self.assertEqual(joined_text, "hello world")

    def test_staged_git_and_non_repo(self):
        tracked = self.root / "staged.txt"
        tracked.write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged.txt"], cwd=str(self.root), check=True, capture_output=True)
        # staged only (not committed)
        summary = git_summary(self.root)
        self.assertIn("staged.txt", summary["changed_files"])
        self.assertGreaterEqual(summary["additions"], 1)
        self.assertFalse(summary["clean"])

        # unstaged modification + untracked
        tracked.write_text("one\ntwo\n", encoding="utf-8")
        (self.root / "untracked.txt").write_text("a\nb\n", encoding="utf-8")
        summary2 = git_summary(self.root)
        self.assertIn("staged.txt", summary2["changed_files"])
        self.assertIn("untracked.txt", summary2["changed_files"])
        self.assertGreaterEqual(summary2["additions"], 2)
        self.assertFalse(summary2["clean"])

        with tempfile.TemporaryDirectory() as bare:
            bare_path = Path(bare)
            with self.assertRaises(NexusPathError) as ctx:
                git_summary(bare_path)
            self.assertEqual(ctx.exception.code, "not_a_git_worktree")

    def test_status_degraded_when_workspace_missing(self):
        missing = Path(self.tmp.name) / "no-such-nexus-root"
        runtime = NexusRuntime(workspace_override=str(missing), context_usage_getter=lambda: None)
        app = Flask("nexus-missing")
        app.register_blueprint(create_nexus_blueprint(runtime, owner_guard=lambda _req: None))
        client = app.test_client()
        resp = client.get("/api/nexus/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["workspace"]["ok"])
        self.assertEqual(body["workspace"]["error"], "workspace_root_missing")
        self.assertIn("capabilities", body)
        self.assertFalse(body["capabilities"]["agent_availability"]["claude"]["available"])
        self.assertEqual(
            body["capabilities"]["agent_availability"]["claude"]["reason"],
            "ENVIRONMENT_BLOCKED",
        )
        turn = client.post("/api/nexus/turn", json={"agent": "codex", "instruction": "x"})
        self.assertEqual(turn.status_code, 503)

    def test_live_claude_adapter_unavailable(self):
        from nexus_adapters import CLAUDE_HARD_CONFINEMENT_AVAILABLE, ClaudeNexusAdapter

        self.assertFalse(CLAUDE_HARD_CONFINEMENT_AVAILABLE)
        adapter = ClaudeNexusAdapter(self.root)
        self.assertFalse(adapter.hard_workspace_confinement)
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": adapter, "codex": self.codex},
        )
        with self.assertRaises(Exception) as ctx:
            runtime.start_turn("claude", "nope")
        self.assertEqual(ctx.exception.code, "claude_unavailable")


class CodexLockShapeStubTests(unittest.TestCase):
    """In-memory stub proving stream_bound_turn does not hold lock across wait/yield."""

    def test_interrupt_during_stream_bound_turn_no_deadlock(self):
        import codex_app_server as cas

        server = cas.CodexAppServer(cwd="/tmp", db_path=os.devnull, sandbox="workspace-write")
        # Avoid real process: stub setup pieces.
        started = {"turn": False}
        released_for_wait = threading.Event()
        interrupt_done = threading.Event()

        def fake_start_locked():
            return None

        def fake_ensure(_thread_id, _instructions):
            return "thread-1"

        def fake_request(method, params, *, timeout=30, ensure_started=True, early_notifications=None):
            if method == "turn/start":
                started["turn"] = True
                return {"turn": {"id": "turn-1"}}
            return {}

        def fake_next(timeout):
            # Signal that stream is waiting outside the lock.
            released_for_wait.set()
            deadline = time.time() + float(timeout)
            while time.time() < deadline:
                if server._cancel_requested.is_set():
                    return {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "turn": {"status": "interrupted"},
                        },
                    }
                time.sleep(0.01)
            raise cas.CodexAppServerError("timeout")

        server._start_locked = fake_start_locked  # type: ignore[method-assign]
        server._ensure_bound_thread_locked = fake_ensure  # type: ignore[method-assign]
        server._request_locked = fake_request  # type: ignore[method-assign]
        server._next_message = fake_next  # type: ignore[method-assign]
        server._stop_locked = lambda: None  # type: ignore[method-assign]
        server._send_locked = lambda payload: None  # type: ignore[method-assign]

        results = {"interrupt_elapsed": None, "events": [], "params": None}

        def run_stream():
            try:
                for item in server.stream_bound_turn(None, "dev", "prompt", timeout=2):
                    results["events"].append(item)
            except Exception as exc:
                results["events"].append(("exc", str(exc)))

        def run_interrupt():
            self.assertTrue(released_for_wait.wait(1))
            acquired = server._lock.acquire(timeout=0.2)
            self.assertTrue(acquired, "stream held server lock across wait")
            server._lock.release()
            t0 = time.time()
            server.interrupt_turn("turn-1", "thread-1")
            results["interrupt_elapsed"] = time.time() - t0
            results["params"] = dict(server._last_interrupt_params or {})
            interrupt_done.set()

        th_s = threading.Thread(target=run_stream, daemon=True)
        th_i = threading.Thread(target=run_interrupt, daemon=True)
        th_s.start()
        th_i.start()
        th_i.join(timeout=2)
        th_s.join(timeout=3)
        self.assertTrue(interrupt_done.is_set())
        self.assertIsNotNone(results["interrupt_elapsed"])
        self.assertLess(results["interrupt_elapsed"], 0.5)
        self.assertEqual(results["params"].get("turnId"), "turn-1")
        self.assertEqual(results["params"].get("threadId"), "thread-1")
        kinds = [e[0] for e in results["events"]]
        self.assertIn("err", kinds)
        err = [e for e in results["events"] if e[0] == "err"][0]
        self.assertEqual(err[1].get("code"), "interrupted")


class NexusR2CodexGateTests(unittest.TestCase):
    """R2: Codex read-isolation gate, env allowlist, ephemeral, interrupt protocol, git -z."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_git_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_write_is_not_hard_confinement(self):
        from nexus_adapters import (
            CODEX_HARD_CONFINEMENT_AVAILABLE,
            CodexNexusAdapter,
        )

        self.assertFalse(CODEX_HARD_CONFINEMENT_AVAILABLE)
        adapter = CodexNexusAdapter(self.root)
        self.assertFalse(adapter.hard_workspace_confinement)
        # sandbox may still be workspace-write on the server factory path, but
        # that must not flip hard confinement.
        self.assertEqual(adapter.hard_workspace_confinement, False)

    def test_blocked_codex_status_and_post_503(self):
        from nexus_adapters import CodexNexusAdapter, FakeClaudeAdapter

        runtime = NexusRuntime(
            workspace=self.root,
            adapters={
                "claude": FakeClaudeAdapter(self.root),
                "codex": CodexNexusAdapter(self.root),
            },
        )
        status = runtime.status()
        self.assertFalse(status["capabilities"]["agent_availability"]["codex"]["available"])
        self.assertEqual(
            status["capabilities"]["agent_availability"]["codex"]["reason"],
            "ENVIRONMENT_BLOCKED",
        )
        self.assertFalse(
            status["capabilities"]["agent_availability"]["codex"]["sandbox_is_read_isolation"]
        )
        app = Flask("nexus-r2")
        app.register_blueprint(create_nexus_blueprint(runtime, owner_guard=lambda _req: None))
        client = app.test_client()
        resp = client.post("/api/nexus/turn", json={"agent": "codex", "instruction": "x"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["code"], "codex_unavailable")

    def test_nexus_env_allowlist_excludes_secrets(self):
        import codex_app_server as cas

        os.environ["APP_SECRET"] = "should-not-leak"
        os.environ["BOARD_TOKEN"] = "board-secret"
        os.environ["DATABASE_PASSWORD"] = "db-secret"
        home = self.root / "nexus-codex-home"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            sandbox="workspace-write",
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        env = server._environment()
        self.assertNotIn("APP_SECRET", env)
        self.assertNotIn("BOARD_TOKEN", env)
        self.assertNotIn("DATABASE_PASSWORD", env)
        self.assertEqual(env.get("CODEX_HOME"), str(home))
        self.assertNotEqual(env.get("CODEX_HOME"), "/root/.codex")
        # inherit mode still carries secrets for non-Nexus callers
        legacy = cas.CodexAppServer(cwd=str(self.root), db_path=os.devnull)
        legacy_env = legacy._environment()
        self.assertIn("APP_SECRET", legacy_env)

    def test_ephemeral_thread_start_param(self):
        import codex_app_server as cas

        home = self.root / "nx-home"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
            service_name="hayagarden_nexus",
        )
        captured = {}

        def fake_request(method, params, **kwargs):
            captured["method"] = method
            captured["params"] = dict(params)
            return {"thread": {"id": "thr-ephemeral", "path": None}}

        with unittest.mock.patch.object(server, "_request_locked", side_effect=fake_request):
            tid = server._ensure_bound_thread_locked(None, "dev")
        self.assertEqual(tid, "thr-ephemeral")
        self.assertEqual(captured["method"], "thread/start")
        self.assertTrue(captured["params"].get("ephemeral") is True)
        self.assertEqual(captured["params"].get("serviceName"), "hayagarden_nexus")

    def test_runtime_memory_only_after_restart(self):
        claude = FakeClaudeAdapter(self.root)
        codex = FakeCodexAdapter(self.root)
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": claude, "codex": codex},
        )
        r = runtime.start_turn("claude", "one")
        _drain_events(runtime, r["turn_id"])
        self.assertTrue(runtime.list_turns())
        # New runtime instance: no recoverable Nexus turn state.
        runtime2 = NexusRuntime(
            workspace=self.root,
            adapters={"claude": FakeClaudeAdapter(self.root), "codex": FakeCodexAdapter(self.root)},
        )
        self.assertEqual(runtime2.list_turns(), [])
        self.assertIsNone(runtime2.status()["active_turn_id"])

    def test_interrupt_requires_provider_completed_interrupted(self):
        import codex_app_server as cas

        home = self.root / "nx-home2"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        server._start_locked = lambda: None  # type: ignore
        server._ensure_bound_thread_locked = lambda *_a, **_k: "thread-9"  # type: ignore
        server._request_locked = lambda *_a, **_k: {"turn": {"id": "turn-9"}}  # type: ignore
        server._stop_locked = lambda: None  # type: ignore
        server._process = unittest.mock.Mock()
        server._process.poll.return_value = None
        server._process.stdin = unittest.mock.Mock()
        sent = []

        def fake_send(payload):
            sent.append(payload)

        server._send_locked = fake_send  # type: ignore
        msgs = [
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-9",
                    "turnId": "turn-9",
                    "turn": {"status": "interrupted"},
                },
            }
        ]

        def fake_next(_timeout):
            if msgs:
                return msgs.pop(0)
            raise cas.CodexAppServerError("empty")

        server._next_message = fake_next  # type: ignore
        server._cancel_requested.set()
        events = list(server.stream_bound_turn(None, "d", "p", timeout=2))
        self.assertTrue(any(e[0] == "err" and e[1].get("code") == "interrupted" for e in events))
        interrupt_rpc = [p for p in sent if p.get("method") == "turn/interrupt"]
        self.assertTrue(interrupt_rpc)
        params = interrupt_rpc[0]["params"]
        self.assertEqual(params.get("threadId"), "thread-9")
        self.assertEqual(params.get("turnId"), "turn-9")

    def test_interrupt_rejected_does_not_pretend_success(self):
        import codex_app_server as cas

        home = self.root / "nx-home3"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        server._start_locked = lambda: None  # type: ignore
        server._ensure_bound_thread_locked = lambda *_a, **_k: "thread-8"  # type: ignore
        server._request_locked = lambda *_a, **_k: {"turn": {"id": "turn-8"}}  # type: ignore
        server._stop_locked = lambda: None  # type: ignore
        server._process = unittest.mock.Mock()
        server._process.poll.return_value = None
        server._process.stdin = unittest.mock.Mock()
        server._send_locked = lambda payload: None  # type: ignore
        server._cancel_requested.set()
        # Provider completes as completed instead of interrupted.
        server._next_message = lambda _t: {  # type: ignore
            "method": "turn/completed",
            "params": {
                "threadId": "thread-8",
                "turnId": "turn-8",
                "turn": {"status": "completed"},
            },
        }
        events = list(server.stream_bound_turn(None, "d", "p", timeout=2))
        err = [e for e in events if e[0] == "err"][0]
        self.assertEqual(err[1]["code"], "interrupt_unconfirmed")

    def test_immediate_interrupt_preserves_cancel(self):
        claude = FakeClaudeAdapter(self.root)
        claude.hang = True
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": claude, "codex": FakeCodexAdapter(self.root)},
        )
        accepted = runtime.start_turn("claude", "hang")
        # Interrupt immediately — must not be cleared by stream_turn start.
        ir = runtime.interrupt(accepted["turn_id"])
        self.assertTrue(ir["interrupted"])
        events = _drain_events(runtime, accepted["turn_id"], timeout=3)
        self.assertEqual(events[-1]["data"].get("code"), "interrupted")

    def test_interrupt_after_done_is_not_active(self):
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": FakeClaudeAdapter(self.root), "codex": FakeCodexAdapter(self.root)},
        )
        r = runtime.start_turn("claude", "done")
        events = _drain_events(runtime, r["turn_id"])
        self.assertEqual(events[-1]["event"], "done")
        ir = runtime.interrupt(r["turn_id"])
        self.assertFalse(ir["interrupted"])
        self.assertEqual(ir["detail"], "not_active")

    def test_git_special_names_and_untracked_dir(self):
        special = self.root / 'file with spaces "quotes".txt'
        special.write_text("hello\n", encoding="utf-8")
        uni = self.root / "文件-unicode.txt"
        uni.write_text("你好\n", encoding="utf-8")
        nested = self.root / "untracked_dir"
        nested.mkdir()
        (nested / "inner.txt").write_text("a\nb\n", encoding="utf-8")
        summary = git_summary(self.root)
        self.assertIn('file with spaces "quotes".txt', summary["changed_files"])
        self.assertIn("文件-unicode.txt", summary["changed_files"])
        self.assertIn("untracked_dir/inner.txt", summary["changed_files"])
        # Directory itself is not a fake single-line addition entry.
        self.assertNotIn("untracked_dir", summary["changed_files"])
        self.assertFalse(summary["clean"])
        self.assertGreaterEqual(summary["additions"], 1 + 1 + 2)


class NexusR3InterruptFidelityTests(unittest.TestCase):
    """R3: runtime e2e interrupt terminal fidelity (not CodexAppServer-only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_git_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _runtime(self, claude: FakeClaudeAdapter) -> NexusRuntime:
        return NexusRuntime(
            workspace=self.root,
            adapters={"claude": claude, "codex": FakeCodexAdapter(self.root)},
        )

    def _assert_terminal_code(self, runtime, turn_id, code, *, state):
        events = _drain_events(runtime, turn_id, timeout=3)
        terminals = [e for e in events if e["event"] in {"done", "err"}]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["event"], "err")
        self.assertEqual(terminals[0]["data"]["code"], code)
        turn = runtime._turns[turn_id]
        self.assertEqual(turn.state, state)
        self.assertEqual(turn.error_code, code)
        return events

    def test_runtime_preserves_interrupt_failed(self):
        claude = FakeClaudeAdapter(self.root, hang=True)
        claude.interrupt_err_code = "interrupt_failed"
        runtime = self._runtime(claude)
        r = runtime.start_turn("claude", "fail")
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        self._assert_terminal_code(runtime, r["turn_id"], "interrupt_failed", state="error")
        self.assertGreaterEqual(claude.ensure_stopped_calls, 1)

    def test_runtime_preserves_interrupt_rejected(self):
        claude = FakeClaudeAdapter(self.root, hang=True)
        claude.interrupt_err_code = "interrupt_rejected"
        runtime = self._runtime(claude)
        r = runtime.start_turn("claude", "reject")
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        self._assert_terminal_code(runtime, r["turn_id"], "interrupt_rejected", state="error")
        self.assertGreaterEqual(claude.ensure_stopped_calls, 1)

    def test_runtime_preserves_interrupt_unconfirmed(self):
        claude = FakeClaudeAdapter(self.root, hang=True)
        claude.interrupt_err_code = "interrupt_unconfirmed"
        runtime = self._runtime(claude)
        r = runtime.start_turn("claude", "unconfirmed")
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        self._assert_terminal_code(runtime, r["turn_id"], "interrupt_unconfirmed", state="error")
        self.assertGreaterEqual(claude.ensure_stopped_calls, 1)

    def test_runtime_confirmed_interrupted_only(self):
        claude = FakeClaudeAdapter(self.root, hang=True)
        claude.interrupt_err_code = "interrupted"
        runtime = self._runtime(claude)
        r = runtime.start_turn("claude", "ok-interrupt")
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        self._assert_terminal_code(runtime, r["turn_id"], "interrupted", state="interrupted")
        # Confirmed interrupt does not require force-stop before releasing the gate.
        self.assertEqual(claude.ensure_stopped_calls, 0)

    def test_serial_gate_released_after_failed_interrupt_stop(self):
        claude = FakeClaudeAdapter(self.root, hang=True)
        claude.interrupt_err_code = "interrupt_failed"
        runtime = self._runtime(claude)
        r = runtime.start_turn("claude", "gate")
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        _drain_events(runtime, r["turn_id"], timeout=3)
        nxt = runtime.start_turn("claude", "after-failed-interrupt")
        ev = _drain_events(runtime, nxt["turn_id"])
        self.assertEqual(ev[-1]["event"], "done")


class NexusR3CodexSessionTests(unittest.TestCase):
    """R3: ephemeral session continuity, interrupt id lifecycle, auth/home, rename."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _init_git_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ephemeral_session_continues_in_same_process(self):
        from nexus_adapters import CodexNexusAdapter

        class LiveCodex(CodexNexusAdapter):
            hard_workspace_confinement = True

        class FakeServer:
            def __init__(self, **kwargs):
                self.thread_ids_seen = []
                self._active_thread_id = None
                self._active_turn_id = None
                self.interrupt_calls = []
                self.stopped = False
                self._n = 0

            def stream_bound_turn(self, thread_id, instructions, prompt, **kwargs):
                self.thread_ids_seen.append(thread_id)
                self._n += 1
                tid = thread_id or "ephemeral-thread-A"
                turn = f"turn-{self._n}"
                self._active_thread_id = tid
                self._active_turn_id = turn
                yield "meta", {"phase": "turn_started", "thread_id": tid, "turn_id": turn}
                yield "text", f"hello-{self._n}"
                yield "done", {"thread_id": tid, "turn_id": turn, "status": "completed"}

            def interrupt_active_turn(self):
                self.interrupt_calls.append(
                    (self._active_thread_id, self._active_turn_id)
                )

            def stop(self):
                self.stopped = True

            def close(self):
                self.stop()

        servers = []

        def factory(**kwargs):
            s = FakeServer(**kwargs)
            servers.append(s)
            return s

        adapter = LiveCodex(self.root, server_factory=factory)
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": FakeClaudeAdapter(self.root), "codex": adapter},
        )
        r1 = runtime.start_turn("codex", "first")
        ev1 = _drain_events(runtime, r1["turn_id"])
        self.assertEqual(ev1[-1]["event"], "done")
        sid = adapter.session_id
        self.assertEqual(sid, "ephemeral-thread-A")
        self.assertEqual(runtime.status()["sessions"]["codex"]["session_id"], sid)
        self.assertEqual(servers[0].thread_ids_seen, [None])

        r2 = runtime.start_turn("codex", "second")
        ev2 = _drain_events(runtime, r2["turn_id"])
        self.assertEqual(ev2[-1]["event"], "done")
        self.assertEqual(adapter.session_id, sid)
        self.assertEqual(runtime.status()["sessions"]["codex"]["session_id"], sid)
        # Second turn resumes the first ephemeral thread id — never forced None.
        self.assertEqual(servers[0].thread_ids_seen, [None, "ephemeral-thread-A"])
        self.assertEqual(len(servers), 1)

    def test_interrupt_uses_server_active_turn_not_stale_cache(self):
        from nexus_adapters import CodexNexusAdapter

        class LiveCodex(CodexNexusAdapter):
            hard_workspace_confinement = True

        class FakeServer:
            def __init__(self, **kwargs):
                self._active_thread_id = None
                self._active_turn_id = None
                self.interrupt_targets = []
                self._n = 0
                self.hang = threading.Event()
                self.started = threading.Event()

            def stream_bound_turn(
                self,
                thread_id,
                instructions,
                prompt,
                *,
                timeout=360,
                cancel_event=None,
            ):
                self._n += 1
                tid = thread_id or "thr-live"
                turn = f"turn-live-{self._n}"
                self._active_thread_id = tid
                self._active_turn_id = turn
                yield "meta", {"phase": "turn_started", "thread_id": tid, "turn_id": turn}
                self.started.set()
                for _ in range(200):
                    if cancel_event is not None and cancel_event.is_set():
                        yield "err", {
                            "code": "interrupted",
                            "message": "turn interrupted",
                            "provider_status": "interrupted",
                        }
                        return
                    time.sleep(0.01)
                yield "done", {"thread_id": tid, "turn_id": turn, "status": "completed"}

            def interrupt_active_turn(self):
                self.interrupt_targets.append(
                    (self._active_thread_id, self._active_turn_id)
                )

            def stop(self):
                pass

        server = FakeServer()
        adapter = LiveCodex(self.root, server_factory=lambda **kw: server)
        # Poison stale cache from a previous turn — must not be preferred.
        adapter._active_codex_thread_id = "stale-thread"
        adapter._active_codex_turn_id = "stale-turn"
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": FakeClaudeAdapter(self.root), "codex": adapter},
        )
        r = runtime.start_turn("codex", "hang")
        self.assertTrue(server.started.wait(2))
        # Ensure the stream loop is waiting on cancel before we interrupt.
        time.sleep(0.05)
        runtime.interrupt(r["turn_id"])
        events = _drain_events(runtime, r["turn_id"], timeout=3)
        self.assertEqual(events[-1]["data"]["code"], "interrupted")
        self.assertEqual(server.interrupt_targets, [("thr-live", "turn-live-1")])
        self.assertNotIn(("stale-thread", "stale-turn"), server.interrupt_targets)

    def test_interrupt_rpc_sent_once_when_stream_also_cancels(self):
        import codex_app_server as cas

        home = self.root / "nx-once"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        server._start_locked = lambda: None  # type: ignore
        server._ensure_bound_thread_locked = lambda *_a, **_k: "thread-once"  # type: ignore
        server._request_locked = lambda *_a, **_k: {"turn": {"id": "turn-once"}}  # type: ignore
        server._stop_locked = lambda: None  # type: ignore
        server._process = unittest.mock.Mock()
        server._process.poll.return_value = None
        server._process.stdin = unittest.mock.Mock()
        sent = []

        def fake_send(payload):
            sent.append(dict(payload))

        server._send_locked = fake_send  # type: ignore
        msgs = [
            {"id": 99, "result": {"ok": True}},  # unrelated response — must not ack interrupt
            {
                "id": None,  # placeholder filled after interrupt request id known
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-once",
                    "turnId": "turn-once",
                    "turn": {"status": "interrupted"},
                },
            },
        ]
        phase = {"n": 0}

        def fake_next(_timeout):
            phase["n"] += 1
            if phase["n"] == 1:
                # External interrupt already sent one RPC before stream sees cancel.
                server.interrupt_turn("turn-once", "thread-once")
                return msgs[0]
            if phase["n"] == 2:
                # Matching interrupt ack by request id.
                return {"id": server._interrupt_request_id, "result": {}}
            if phase["n"] == 3:
                return msgs[2]
            raise cas.CodexAppServerError("empty")

        server._next_message = fake_next  # type: ignore
        server._cancel_requested.set()
        events = list(server.stream_bound_turn(None, "d", "p", timeout=2))
        interrupt_rpc = [p for p in sent if p.get("method") == "turn/interrupt"]
        self.assertEqual(len(interrupt_rpc), 1)
        self.assertTrue(any(e[0] == "err" and e[1].get("code") == "interrupted" for e in events))

    def test_jsonrpc_error_response_is_interrupt_rejected(self):
        import codex_app_server as cas

        home = self.root / "nx-rej"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        server._start_locked = lambda: None  # type: ignore
        server._ensure_bound_thread_locked = lambda *_a, **_k: "thread-rej"  # type: ignore
        server._request_locked = lambda *_a, **_k: {"turn": {"id": "turn-rej"}}  # type: ignore
        server._stop_locked = lambda: None  # type: ignore
        server._process = unittest.mock.Mock()
        server._process.poll.return_value = None
        server._process.stdin = unittest.mock.Mock()
        sent = []
        server._send_locked = lambda payload: sent.append(payload)  # type: ignore
        phase = {"n": 0}

        def fake_next(_timeout):
            phase["n"] += 1
            if phase["n"] == 1:
                return {
                    "id": server._interrupt_request_id,
                    "error": {"message": "turn not interruptible"},
                }
            raise cas.CodexAppServerError("empty")

        server._next_message = fake_next  # type: ignore
        server._cancel_requested.set()
        events = list(server.stream_bound_turn(None, "d", "p", timeout=2))
        err = [e for e in events if e[0] == "err"][0]
        self.assertEqual(err[1]["code"], "interrupt_rejected")

    def test_nexus_auth_probe_uses_allowlist_codex_home(self):
        import codex_app_server as cas

        home = self.root / ".nexus-codex-home"
        home.mkdir()
        server = cas.CodexAppServer(
            cwd=str(self.root),
            db_path=os.devnull,
            env_mode="nexus_allowlist",
            codex_home=str(home),
            ephemeral_threads=True,
        )
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})
            captured["cmd"] = list(cmd)
            return unittest.mock.Mock(returncode=1, stdout="not logged in")

        with unittest.mock.patch.object(cas, "find_codex", return_value="/tmp/fake-codex"), unittest.mock.patch.object(
            cas.subprocess, "run", side_effect=fake_run
        ):
            status = server._nexus_auth_status()
        self.assertFalse(status["authenticated"])
        self.assertFalse(status["ready"])
        self.assertEqual(captured["env"].get("CODEX_HOME"), str(home))
        self.assertNotEqual(captured["env"].get("CODEX_HOME"), "/root/.codex")
        self.assertNotIn("BOARD_TOKEN", captured["env"])

    def test_restart_has_no_recoverable_nexus_thread_on_disk(self):
        home = self.root / ".nexus-codex-home"
        home.mkdir()
        # Simulate leftover provider files that must not be treated as recoverable
        # Nexus session state across Python process restart.
        (home / "sessions").mkdir()
        (home / "sessions" / "rollout-fake.jsonl").write_text("{}", encoding="utf-8")
        claude = FakeClaudeAdapter(self.root)
        codex = FakeCodexAdapter(self.root)
        runtime = NexusRuntime(
            workspace=self.root,
            adapters={"claude": claude, "codex": codex},
        )
        r = runtime.start_turn("claude", "one")
        _drain_events(runtime, r["turn_id"])
        runtime2 = NexusRuntime(
            workspace=self.root,
            adapters={"claude": FakeClaudeAdapter(self.root), "codex": FakeCodexAdapter(self.root)},
        )
        self.assertEqual(runtime2.list_turns(), [])
        self.assertIsNone(runtime2.status()["active_turn_id"])
        self.assertIsNone(runtime2.status()["sessions"]["codex"]["session_id"])
        self.assertFalse(runtime2.status()["sessions"]["codex"]["exists"])
        # Disk leftovers under .nexus-codex-home are not a recoverable Nexus thread.
        self.assertTrue((home / "sessions" / "rollout-fake.jsonl").exists())
        summary = git_summary(self.root)
        self.assertTrue(
            all(".nexus-codex-home" not in p for p in summary["changed_files"]),
            summary["changed_files"],
        )

    def test_git_staged_and_unstaged_rename_target_only(self):
        from nexus_git import _parse_porcelain_z

        # Synthetic porcelain -z: destination first, source second.
        staged = _parse_porcelain_z(b"R  dest_staged.txt\0src_staged.txt\0")
        self.assertEqual(staged, [("R ", "dest_staged.txt")])
        unstaged = _parse_porcelain_z(b" R dest_work.txt\0src_work.txt\0")
        self.assertEqual(unstaged, [(" R", "dest_work.txt")])

        src = self.root / "rename_src.txt"
        src.write_text("body\n", encoding="utf-8")
        subprocess.run(["git", "add", "rename_src.txt"], cwd=str(self.root), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add src"], cwd=str(self.root), check=True, capture_output=True)
        subprocess.run(
            ["git", "mv", "rename_src.txt", "rename_dst.txt"],
            cwd=str(self.root),
            check=True,
            capture_output=True,
        )
        summary = git_summary(self.root)
        self.assertIn("rename_dst.txt", summary["changed_files"])
        self.assertNotIn("rename_src.txt", summary["changed_files"])
        # Frozen six field names only.
        self.assertEqual(
            set(summary.keys()),
            {"branch", "changed_files", "diff_stat", "additions", "deletions", "clean"},
        )


if __name__ == "__main__":
    unittest.main()