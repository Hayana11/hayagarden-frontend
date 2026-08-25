"""Focused Claude Code provider contract for defer -> confirmation -> resume."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
from chat import cc_runtime
from tools.execution_fence import (
    UH_A0TurnRuntime,
    capability_for_tool,
    evaluate_tool_call,
    read_current_turn_lease,
)
from tools.lease_signer import issue_turn_lease


class FakeProcess:
    def __init__(self, events, pid):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        self.stderr = io.StringIO()
        self.pid = pid
        self._stopped = False

    def poll(self):
        return 0 if self._stopped else None

    def terminate(self):
        self._stopped = True

    def kill(self):
        self._stopped = True

    def wait(self, timeout=None):
        self._stopped = True
        return 0


class DeferredResumeContractTests(unittest.TestCase):
    def lease(self, *, source="default_policy", requested=(), approvals=(), turn_id="100"):
        return issue_turn_lease(
            turn_id=turn_id,
            turn_mode="chat",
            issued_from=source,
            requested_capabilities=requested,
            approval_ids=approvals,
            issued_at="2026-08-12T00:00:00Z",
        )

    def test_authoritative_defer_then_confirmation_resumes_same_pending_call(self):
        action = {"content": "明天寄快递"}
        assistant_tool_use = {
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "toolu-1",
                    "name": "mcp__capability__todo_write",
                    "input": action,
                }],
            },
        }
        deferred_result = {
            "type": "result",
            "stop_reason": "tool_deferred",
            "session_id": "session-abc",
            "deferred_tool_use": {
                "id": "toolu-1",
                "name": "mcp__capability__todo_write",
                "input": action,
            },
        }
        resumed_events = [
            {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": "toolu-1",
                        "name": "mcp__capability__todo_write",
                        "input": action,
                    }],
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "toolu-1",
                        "content": "mock post result",
                    }],
                },
            },
            {
                "type": "result",
                "stop_reason": "end_turn",
                "session_id": "session-abc",
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            lease_path = Path(tmp) / "lease.json"
            session = cc_resident.ResidentSession(
                cwd=tmp,
                allowed_tools="",
                mcp_config_path=str(Path(tmp) / "cc-tools.json"),
            )
            session._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            session._system_text = "UH-A0 test system"
            session._uh_a0_turn_lease_path = str(lease_path)
            session._proc = FakeProcess([assistant_tool_use, deferred_result], pid=100)
            runtime = UH_A0TurnRuntime(lease_path)

            with mock.patch.dict(
                os.environ,
                {
                    cc_runtime.ARGV_OVERRIDE_ENV: json.dumps(["/opt/pin/claude"]),
                    cc_runtime.SKIP_VERSION_PROBE_ENV: "1",
                },
            ), mock.patch.object(
                session, "_attach_jsonl_usage_with_retry",
                side_effect=lambda usage, cursor=None: usage,
            ), mock.patch(
                "cc_resident.subprocess.Popen",
                return_value=FakeProcess(resumed_events, pid=101),
            ) as popen:
                first_stream = session.send_turn(
                    "请判断是否记入待办",
                    turn_lease=self.lease(),
                    turn_runtime=runtime,
                )
                first_event, first_payload = next(first_stream)
                self.assertEqual(first_event, "tool_use")
                self.assertTrue(first_payload["deferred_tool_use"])
                self.assertEqual(first_payload["status"], "waiting_for_confirmation")
                self.assertEqual(first_payload["id"], "toolu-1")
                self.assertEqual(first_payload["name"], "mcp__capability__todo_write")
                self.assertEqual(first_payload["args"], action)
                # The first externally visible waiting event is authoritative:
                # pending state exists and the default lease is already gone.
                self.assertIsNone(read_current_turn_lease(lease_path)[0])
                self.assertEqual(
                    session.pending_deferred,
                    {
                        "session_id": "session-abc",
                        "tool_use_id": "toolu-1",
                        "tool_name": "mcp__capability__todo_write",
                        "tool_input": action,
                        "approval_id": first_payload["approval_id"],
                    },
                )
                first_events = [(first_event, first_payload)] + list(first_stream)
                waiting = [
                    payload for event, payload in first_events
                    if event == "tool_use"
                    and isinstance(payload, dict)
                    and payload.get("deferred_tool_use")
                    and payload.get("status") == "waiting_for_confirmation"
                ]
                self.assertEqual(len(waiting), 1)
                self.assertFalse(any(event == "tool_result" for event, _ in first_events))

                confirmation = self.lease(
                    source="user_confirmation",
                    requested=("todo.write",),
                    approvals=(first_payload["approval_id"],),
                    turn_id="101",
                )
                confirmation_leases = []

                def on_confirmation_stdin_flushed():
                    lease, _record = read_current_turn_lease(lease_path)
                    confirmation_leases.append(lease)

                resumed = list(session.resume_pending_deferred_turn(
                    "好，记上吧",
                    dict(os.environ),
                    confirmation,
                    on_stdin_flushed=on_confirmation_stdin_flushed,
                    turn_runtime=runtime,
                ))
                self.assertEqual(len(confirmation_leases), 1)
                self.assertEqual(confirmation_leases[0]["turn_id"], "101")
                self.assertEqual(
                    confirmation_leases[0]["issued_from"],
                    "user_confirmation",
                )

                args = popen.call_args[0][0]
                self.assertIn("-p", args)
                self.assertIn("--resume", args)
                self.assertEqual(args[args.index("--resume") + 1], "session-abc")
                self.assertEqual(args[args.index("--settings") + 1].endswith(
                    "cc-settings-uh-a0.json"
                ), True)
                self.assertEqual(args[args.index("--mcp-config") + 1].endswith(
                    "cc-tools-uh-a0.json"
                ), True)

                resumed_tool_uses = [
                    payload for event, payload in resumed
                    if event == "tool_use" and isinstance(payload, dict)
                ]
                self.assertEqual(len(resumed_tool_uses), 1)
                self.assertEqual(resumed_tool_uses[0]["id"], "toolu-1")
                self.assertEqual(resumed_tool_uses[0]["name"], "mcp__capability__todo_write")
                self.assertEqual(resumed_tool_uses[0]["args"], action)
                self.assertEqual(resumed_tool_uses[0]["lease_decision"], "ALLOW")
                self.assertIsNone(session.pending_deferred)
                self.assertIsNone(read_current_turn_lease(lease_path)[0])



    def test_stale_home_pending_is_rejected_after_provider_cutover(self):
        action = {"content": "明天寄快递"}
        old_tool = "mcp__home__add_todo"
        current_tool = "mcp__capability__todo_write"
        self.assertEqual(capability_for_tool(old_tool), "todo.write")

        with tempfile.TemporaryDirectory() as tmp:
            session = cc_resident.ResidentSession(
                cwd=tmp,
                allowed_tools="",
                mcp_config_path=str(Path(tmp) / "cc-tools.json"),
            )
            session._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            session._system_text = "UH-A0 test system"
            pending = session._capture_authoritative_deferred({
                "session_id": "old-session",
                "deferred_tool_use": {
                    "id": "old-toolu-1",
                    "name": old_tool,
                    "input": action,
                },
            })
            old_approval_id = pending["approval_id"]
            confirmation = self.lease(
                source="user_confirmation",
                requested=("todo.write",),
                approvals=(old_approval_id,),
                turn_id="cutover-confirmation",
            )
            home_tool_calls = []

            def forbidden_home_execution(*args, **kwargs):
                home_tool_calls.append((args, kwargs))
                raise AssertionError("stale Home tool execution")

            with mock.patch.object(
                session,
                "spawn_resumable",
                side_effect=AssertionError("stale pending spawned"),
            ) as spawn, mock.patch.object(
                session,
                "send_turn",
                side_effect=forbidden_home_execution,
            ):
                with self.assertRaisesRegex(
                    cc_resident.ResidentError,
                    r"deferred_resume:LEASE_MISMATCH",
                ):
                    list(session.resume_pending_deferred_turn(
                        "好，记上吧",
                        dict(os.environ),
                        confirmation,
                    ))

            self.assertIsNone(session.pending_deferred)
            spawn.assert_not_called()
            self.assertEqual(home_tool_calls, [])

            new_action = evaluate_tool_call(current_tool, action, self.lease())
            self.assertEqual(new_action["capability_id"], "todo.write")
            self.assertEqual(new_action["lease_decision"], "CAPABILITY_ASK_REQUIRED")
            self.assertNotEqual(new_action["approval_id"], old_approval_id)

if __name__ == "__main__":
    unittest.main()
