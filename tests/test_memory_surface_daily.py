"""Daily path wiring of the passive recall (tools.memory_surface).

The surfaced memory rides as a per-turn prefix ahead of her words (like task
feedback), is recorded in the manifest, survives the cold rebuild, never blocks
a turn, and is shown/persisted by the gateway once provider output starts.
"""
from __future__ import annotations

import contextlib
import json
import os
import unittest
from unittest import mock

import config_store
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    _FakeResident,
    _gateway_canonical_builder,
    _init_chat_messages,
    _insert,
    _prepare_turn,
    _sse_events_from_chunks,
    _tmp_db,
)
from tools import memory_surface as ms

BLOCK = "<recalled-memory>\n这是一条自动浮现的旧记忆（2026-06-27）。\n\n凌晨聊赫泰拉。\n</recalled-memory>"
ITEMS = [{"type": "MEMORY", "date": "2026-06-27", "preview": "凌晨聊赫泰拉。"}]


def surfaced(replayed=False):
    return ms.SurfaceResult("surfaced", "gate_passed", ref="kernel:greek", block=BLOCK, items=list(ITEMS),
                            bucket_id="greek", projected=True, detail={"replayed": True} if replayed else {})


class DailyRecallPrefixTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        self.addCleanup(os.unlink, self.db)
        _init_chat_messages(self.db)

    def prepare(self, text, decide):
        uid = _insert(self.db, "hayana", text, "2026-07-27 10:00:00")
        with mock.patch.object(ms, "decide", side_effect=decide) as decide_mock, \
             mock.patch.object(ms, "touch") as touch_mock:
            plan = _prepare_turn(self.db, uid)
        return plan, decide_mock, touch_mock

    def send(self, plan):
        resident = _FakeResident()
        list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system="STATIC"))
        dr._release_lease(plan)
        return resident.sent[0]

    def test_surfaced_memory_is_sent_ahead_of_her_words(self):
        plan, decide_mock, touch_mock = self.prepare("想吃希腊菜了", lambda **_k: surfaced())
        self.assertEqual(decide_mock.call_args.kwargs["user_text"], "想吃希腊菜了")
        self.assertTrue(plan.manifest["auto_recall_injected"])
        self.assertEqual(plan.manifest["memory_recall_ref"], "kernel:greek")
        self.assertEqual(plan.memory_recall["items"], ITEMS)
        touch_mock.assert_called_once()
        sent = self.send(plan)
        self.assertIn(BLOCK, sent)
        self.assertLess(sent.index(BLOCK), sent.index("想吃希腊菜了"))
        self.assertTrue(sent.rstrip().endswith("想吃希腊菜了"))

    def test_nothing_surfaced_leaves_the_payload_alone(self):
        plan, _decide, touch_mock = self.prepare(
            "今天好累", lambda **_k: ms.SurfaceResult("none", "gate_closed"))
        self.assertFalse(plan.manifest["auto_recall_injected"])
        self.assertEqual(plan.manifest["memory_recall_reason"], "gate_closed")
        self.assertNotIn("memory_recall", plan.assembly)
        touch_mock.assert_not_called()
        self.assertNotIn("<recalled-memory>", self.send(plan))

    def test_recall_failure_never_blocks_the_turn(self):
        def boom(**_k):
            raise RuntimeError("surface exploded")
        with self.assertLogs(dr.logger, level="ERROR"):
            plan, _decide, _touch = self.prepare("今天好累", boom)
        self.assertEqual(plan.manifest["memory_recall_decision"], "failed")
        self.assertFalse(plan.manifest["auto_recall_injected"])
        self.assertTrue(self.send(plan).rstrip().endswith("今天好累"))

    def test_replayed_decision_is_not_touched_twice(self):
        _plan, _decide, touch_mock = self.prepare("想吃希腊菜了", lambda **_k: surfaced(replayed=True))
        touch_mock.assert_not_called()

    def test_cold_rebuild_keeps_the_same_memory(self):
        plan, _decide, _touch = self.prepare("想吃希腊菜了", lambda **_k: surfaced())
        with mock.patch.object(config_store, "get_bool", return_value=True), \
             mock.patch("chat.daily_history._build_state_text", return_value=("", "none", {})):
            rebuilt = dr._rebuild_daily_assembly_with_history_budget(
                plan, static_system="S", history_token_budget=50, resident=None, is_cold=True, is_respawn=False)
        self.assertEqual(rebuilt["memory_recall"], BLOCK)
        content = dr.format_resident_turn_content(
            assembly=rebuilt, user_content=plan.user_content, is_cold=True, is_respawn=False)
        self.assertLess(content.index(BLOCK), content.rindex("想吃希腊菜了"))
        dr._release_lease(plan)

    def test_prefix_order_task_feedback_then_memory_then_conversation(self):
        content = dr.format_resident_turn_content(
            assembly={"task_feedback": "TASK-FEEDBACK", "memory_recall": BLOCK,
                      "current_day_history": [{"role": "user", "content": "早上好"}]},
            user_content="想吃希腊菜了", is_cold=False, is_respawn=False)
        self.assertLess(content.index("TASK-FEEDBACK"), content.index(BLOCK))
        self.assertLess(content.index(BLOCK), content.index("早上好"))
        self.assertTrue(content.endswith("想吃希腊菜了"))


class GatewayDailyRecallTests(unittest.TestCase):
    def run_stream(self, memory_recall):
        import gateway

        db = _tmp_db()
        self.addCleanup(os.unlink, db)
        _init_chat_messages(db)
        uid = _insert(db, "hayana", "想吃希腊菜了", "2026-07-27 10:00:00")
        plan = mock.MagicMock()
        plan.resident_key = "daily:default:1:1"
        plan.manifest = {}
        plan.memory_recall = memory_recall
        persisted = {}

        def _fake_stream(plan_arg, *, resident, env, static_system):
            yield ("heartbeat", None)
            yield ("think", "嗯")
            yield ("text", "daily reply")
            yield ("done", ("daily reply", "", {"input_tokens": 1, "output_tokens": 1}, {}))

        def _persist(*_args, **kwargs):
            persisted["cache_info"] = kwargs["cache_info"]
            return 4242

        cas = dr.CursorCASConflictAfterPersist("stop after persist", assistant_message_id=4242, manifest={})
        patches = [
            mock.patch.object(config_store, "get_bool", return_value=True),
            mock.patch("chat.daily_context.enabled", return_value=True),
            mock.patch.object(gateway, "DB_PATH", db),
            mock.patch("chat.system_builder.build_cc_daily_static_parts",
                       return_value={"persona": "P", "full_system": "STATIC"}),
            mock.patch.object(dr, "prepare_daily_turn", return_value=plan),
            mock.patch.object(dr, "build_canonical_turn_for_plan", side_effect=_gateway_canonical_builder()),
            mock.patch.object(dr, "stream_daily_resident_turn", side_effect=_fake_stream),
            mock.patch.object(dr, "persist_daily_assistant_for_plan", side_effect=_persist),
            mock.patch.object(dr, "handle_provider_success", side_effect=cas),
            mock.patch.object(gateway, "_CC_RESIDENT", mock.MagicMock()),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            chunks = list(gateway._stream_cc_daily_soft_window({"user_message_id": uid}, {"content": "想吃希腊菜了"}))
        return _sse_events_from_chunks(chunks), json.loads(persisted["cache_info"])

    def test_recall_event_precedes_thinking_and_is_persisted(self):
        events, cache_info = self.run_stream({"decision": "surfaced", "block": BLOCK, "items": ITEMS})
        kinds = [e.get("t") for e in events if e.get("t") != "ping"]
        self.assertEqual(kinds[:2], ["memory_recall", "think"])
        recall = next(e for e in events if e.get("t") == "memory_recall")
        self.assertEqual(recall["d"], {"count": 1, "items": ITEMS})
        self.assertEqual(sum(1 for e in events if e.get("t") == "memory_recall"), 1)
        self.assertEqual(cache_info["memory_recall"], {"count": 1, "items": ITEMS})

    def test_no_recall_no_event(self):
        events, cache_info = self.run_stream({"decision": "none", "reason": "gate_closed"})
        self.assertFalse(any(e.get("t") == "memory_recall" for e in events))
        self.assertNotIn("memory_recall", cache_info)


if __name__ == "__main__":
    unittest.main()
