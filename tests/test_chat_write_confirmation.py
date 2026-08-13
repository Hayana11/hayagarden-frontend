"""Focused W1 Chat deferred confirmation bridge contract."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import gateway


def _decode_sse(events):
    decoded = []
    for raw in events:
        assert raw.startswith("data: ")
        decoded.append(json.loads(raw[len("data: "):].strip()))
    return decoded


class _FakeConfirmationResident:
    def __init__(self, pending):
        self._pending_deferred = dict(pending)
        self.resume_calls = []
        self.write_count = 0

    @property
    def pending_deferred(self):
        return dict(self._pending_deferred) if self._pending_deferred else None

    def _kill(self, quiet=True):
        return None

    def resume_pending_deferred_turn(self, content, env, turn_lease, **kwargs):
        self.resume_calls.append((content, dict(turn_lease)))
        self.write_count += 1
        yield (
            "tool_use",
            {
                "id": self._pending_deferred["tool_use_id"],
                "name": self._pending_deferred["tool_name"],
                "args": dict(self._pending_deferred["tool_input"]),
            },
        )
        yield (
            "tool_result",
            {
                "tool_use_id": self._pending_deferred["tool_use_id"],
                "result": "mock post result",
                "is_error": False,
            },
        )
        self._pending_deferred = None
        yield ("text", "已记好")
        yield ("done", ("已记好", "", {"provider": "claude_code"}, {}))


class DeferredConfirmationBridgeTests(unittest.TestCase):
    pending = {
        "session_id": "session-abc",
        "tool_use_id": "toolu-1",
        "tool_name": "mcp__home__add_todo",
        "tool_input": {"content": "明天寄快递"},
        "approval_id": (
            "action_sha256:"
            "8f4d7f7e7b2a4c7e4d0f4d1c0d35b5f8244c3c1b8ab3d4d48e4c3a7e3e0e9e01"
        ),
    }

    def run_bridge(self, resident, decision, approval_id=None):
        payload = {
            "confirmation_decision": decision,
            "approval_id": approval_id or self.pending["approval_id"],
        }
        with mock.patch.object(gateway, "_CC_RESIDENT", resident), \
             mock.patch.object(gateway, "_persist_turn_assistant"):
            return _decode_sse(
                gateway._stream_cc_deferred_confirmation(payload)
            )

    def test_approve_issues_exact_user_confirmation_lease_and_resumes_once(self):
        resident = _FakeConfirmationResident(self.pending)
        events = self.run_bridge(resident, "approve")

        self.assertEqual(len(resident.resume_calls), 1)
        content, lease = resident.resume_calls[0]
        self.assertIn("已确认", content)
        self.assertEqual(lease["issued_from"], "user_confirmation")
        self.assertEqual(lease["turn_mode"], "chat")
        self.assertIn("todo.write", lease["allowed_capabilities"])
        self.assertEqual(
            lease["approval_ids"],
            (self.pending["approval_id"],),
        )
        self.assertEqual([event["t"] for event in events], [
            "tool_use", "tool_result", "text", "usage", "done",
        ])
        self.assertEqual(events[0]["d"]["id"], "toolu-1")
        self.assertEqual(events[1]["d"]["result"], "mock post result")
        self.assertEqual(events[2]["d"], "已记好")
        self.assertTrue(events[3]["ok"])

        replay = self.run_bridge(resident, "approve")
        self.assertEqual(resident.write_count, 1)
        self.assertEqual(replay[0]["code"], "LEASE_MISMATCH")
        self.assertEqual(resident.write_count, 1)

    def test_mismatched_approval_fails_closed_without_write(self):
        resident = _FakeConfirmationResident(self.pending)
        events = self.run_bridge(resident, "approve", "action_sha256:not-the-pending-action")

        self.assertEqual(events[0]["t"], "err")
        self.assertEqual(events[0]["code"], "LEASE_MISMATCH")
        self.assertEqual(resident.write_count, 0)

    def test_reject_clears_pending_without_write_or_confirmation_lease(self):
        resident = _FakeConfirmationResident(self.pending)
        events = self.run_bridge(resident, "reject")

        self.assertEqual(resident.write_count, 0)
        self.assertIsNone(resident.pending_deferred)
        self.assertEqual([event["t"] for event in events], ["text", "done"])
        self.assertEqual(events[0]["d"], "已取消")
        self.assertTrue(events[1]["ok"])


if __name__ == "__main__":
    unittest.main()
