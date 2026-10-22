"""Daily path wiring of the passive recall (tools.memory_surface), with the owner's acceptance lines:

1. Daily cold and hot turns have exactly one recall entry (memory_surface).
2. The legacy gateway recall never adds a second block, even when it has content.
3. Shadow only writes the route log: prompt, UI and cooldown are untouched.
4. Failures and retries consume nothing; only a persisted reply commits the memory.

Every turn runs the full /chat/stream Daily flow (real prepare + assembly, fake
resident capturing the exact stdin payload, real persistence).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import unittest
from unittest import mock

import config_store
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    GatewaySuccessHandoffTests,
    _FakeResident,
    _gateway_canonical_builder,
    _init_chat_messages,
    _insert,
    _prepare_turn,
    _sse_events_from_chunks,
    _tmp_db,
)
from tests.test_memory_surface import FakeRuntime, wait_touches
from tools import memory_surface as ms

BLOCK = "<recalled-memory>\n这是一条自动浮现的旧记忆（2026-06-27）。\n\n凌晨聊赫泰拉。\n</recalled-memory>"
ITEMS = [{"type": "MEMORY", "date": "2026-06-27", "preview": "凌晨聊赫泰拉。"}]
LEGACY = "<recalled-memory>\nLEGACY-RECALL-SENTINEL\n</recalled-memory>"
GREEK = {"ref": "kernel:greek", "id": "kernel:greek", "type": "MEMORY", "source": "kernel",
         "content": "六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。",
         "created_at": "2026-06-27 04:00:00", "bucket_id": "greek", "projected": True}
POOL = [GREEK] + [
    {"ref": f"kernel:m{i}", "id": f"kernel:m{i}", "type": "MEMORY", "source": "kernel",
     "content": f"第{i}条无关的日常记忆。", "created_at": "2026-06-01 10:00:00",
     "bucket_id": f"m{i}", "projected": True}
    for i in range(6)
]
QUERY = "想起赫泰拉和阿斯帕西娅了"


def injected():
    return ms.SurfaceResult("surfaced", "gate_passed", mode="on", ref="kernel:greek", block=BLOCK,
                            items=list(ITEMS), bucket_id="greek", projected=True)


class _ColdThenHotResident(_FakeResident):
    def __init__(self):
        super().__init__()
        self.cold_flags = []

    def ensure_alive(self, *args, **kwargs):
        cold = super().ensure_alive(*args, **kwargs)
        self.cold_flags.append(bool(cold))
        return cold


class _Flow(unittest.TestCase):
    """Drive one user turn through gateway /chat/stream on a temp DB."""

    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        self.addCleanup(os.unlink, self.db)
        _init_chat_messages(self.db)
        self.resident = _ColdThenHotResident()
        self.runtime = FakeRuntime()
        patcher = mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env",
                             side_effect=lambda *a, **k: self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def touched(self):
        """Touch batches sent to Ombre, once the commit's background touch has finished."""
        wait_touches()
        return self.runtime.calls

    def turn(self, text, *, extra=(), created_at="2026-07-27 10:00:00"):
        uid = _insert(self.db, "hayana", text, created_at)
        gateway, patches, *_rest = GatewaySuccessHandoffTests._success_stream_patches(None, self.db, uid, self.resident)
        self.legacy = mock.Mock(return_value=(LEGACY, [{"type": "LEGACY", "date": "", "preview": "x"}]))
        with contextlib.ExitStack() as stack:
            for patch in list(patches) + list(extra):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(gateway, "_recall_memories", self.legacy))
            resp = gateway.app.test_client().post("/chat/stream", json={"content": text})
            self.assertEqual(resp.status_code, 200)
            events = _sse_events_from_chunks(list(resp.response))
        return uid, events

    def query(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def cache_info(self, assistant_id=None):
        rows = self.query("SELECT cache_info FROM chat_messages WHERE author='assistant' ORDER BY id")
        return json.loads(rows[-1][0] or "{}") if rows else {}

    def real_surface(self, mode, *, pool=None, ombre=("greek",)):
        """The real decide() over a fake Kernel pool and a fake Ombre ranking."""
        return [
            mock.patch.object(ms, "load_config", return_value=ms.SurfaceConfig(mode=mode)),
            mock.patch("tools.memory_kernel_switch.cutover_enabled", return_value=True),
            mock.patch.object(ms, "_pool", return_value=list(POOL if pool is None else pool)),
            mock.patch("tools.ombre_adapter.search_bucket_ids", return_value=list(ombre)),
        ]


class SingleEntryTests(_Flow):
    def test_cold_and_hot_turns_carry_exactly_one_recall_from_one_entry(self):
        decide = mock.Mock(side_effect=lambda **_k: injected())
        first, events1 = self.turn("想吃希腊菜了", extra=[mock.patch.object(ms, "decide", decide)])
        second, events2 = self.turn("再说说赫泰拉", extra=[mock.patch.object(ms, "decide", decide)],
                                    created_at="2026-07-27 10:05:00")
        self.assertEqual(self.resident.cold_flags[:2], [True, False])  # one cold turn, one hot turn
        self.assertEqual([c.kwargs["user_message_id"] for c in decide.call_args_list], [first, second])
        for sent in self.resident.sent[:2]:
            self.assertEqual(sent.count("<recalled-memory>"), 1)
            self.assertIn(BLOCK, sent)
            self.assertNotIn("LEGACY-RECALL-SENTINEL", sent)
        self.legacy.assert_not_called()  # the gateway's legacy recall is not a Daily entry
        for events in (events1, events2):
            self.assertEqual(sum(1 for e in events if e.get("t") == "memory_recall"), 1)

    def test_legacy_recall_with_content_still_adds_nothing_when_nothing_surfaced(self):
        decide = mock.Mock(return_value=ms.SurfaceResult("none", "gate_closed", mode="on"))
        _uid, events = self.turn("今天好累", extra=[mock.patch.object(ms, "decide", decide)])
        self.assertNotIn("<recalled-memory>", self.resident.sent[0])
        self.legacy.assert_not_called()
        self.assertFalse(any(e.get("t") == "memory_recall" for e in events))


class SingleClueFlowTests(_Flow):
    def test_single_unseen_clue_with_ombre_top3_passes_although_her_message_is_stored(self):
        # The turn stores "渔粉到了" before deciding; it must not count as the clue's history.
        fish = dict(GREEK, ref="kernel:fish", id="kernel:fish", bucket_id="fish",
                    content="她第二回点了番茄玉米渔粉，不辣，加生菜。")
        uid, events = self.turn("渔粉到了", extra=self.real_surface("on", pool=POOL + [fish], ombre=("fish",)))
        self.assertEqual(self.query("SELECT content FROM chat_messages WHERE id=?", uid), [("渔粉到了",)])
        self.assertIn("番茄玉米渔粉", self.resident.sent[0])
        self.assertEqual(sum(1 for e in events if e.get("t") == "memory_recall"), 1)
        self.assertEqual(self.query("SELECT ref, committed FROM memory_surface_log"), [("kernel:fish", 1)])


class LogFailClosedFlowTests(_Flow):
    def test_unlogged_decision_reaches_neither_prompt_ui_nor_cooldown(self):
        broken_log = mock.patch.object(ms, "_log", side_effect=sqlite3.OperationalError("disk I/O error"))
        _uid, events = self.turn(QUERY, extra=self.real_surface("on") + [broken_log])
        self.assertNotIn("<recalled-memory>", self.resident.sent[0])
        self.assertFalse(any(e.get("t") == "memory_recall" for e in events))
        self.assertTrue(any(e.get("t") == "done" and e.get("ok") for e in events))  # the turn itself is fine
        self.assertNotIn("memory_recall", self.cache_info())
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.query("SELECT ref FROM memory_surface_cooldown"), [])


class ShadowTests(_Flow):
    def test_shadow_logs_only(self):
        uid, events = self.turn(QUERY, extra=self.real_surface("shadow"))
        sent = self.resident.sent[0]
        self.assertNotIn("<recalled-memory>", sent)
        self.assertNotIn("赫泰拉和阿斯帕西娅，她说想吃希腊菜", sent)
        self.assertFalse(any(e.get("t") == "memory_recall" for e in events))
        self.assertNotIn("memory_recall", self.cache_info())
        self.assertEqual(self.touched(), [])
        self.assertEqual(self.query("SELECT ref FROM memory_surface_cooldown"), [])
        self.assertEqual(
            self.query("SELECT user_message_id, mode, decision, ref, committed FROM memory_surface_log"),
            [(uid, "shadow", "surfaced", "kernel:greek", 0)],
        )

    def test_shadow_payload_is_byte_identical_to_off(self):
        # The live reality block (clock minute, weather fetch) is pinned so the two
        # payloads can differ only by what surfacing put in.
        reality = {"time_anchor": "【现实时间锚】FIXED", "weather_anchor": "", "time_anchor_reason": "fixed",
                   "weather_anchor_reason": None, "weather_status": "fixed"}
        pinned = mock.patch("chat.reality_context.build_reality_context", return_value=reality)
        self.turn(QUERY, extra=self.real_surface("off") + [pinned])
        off_payload = self.resident.sent[0]
        self.setUp()  # fresh DB and resident, same message
        self.turn(QUERY, extra=self.real_surface("shadow") + [pinned])
        self.assertIn("【现实时间锚】FIXED", off_payload)
        self.assertEqual(self.resident.sent[0], off_payload)


class CommitAfterPersistTests(_Flow):
    def test_failed_turn_consumes_nothing_and_its_retry_gets_the_same_memory(self):
        fail = mock.patch.object(dr, "persist_daily_assistant_for_plan", side_effect=RuntimeError("disk full"))
        uid, events = self.turn(QUERY, extra=self.real_surface("on") + [fail])
        self.assertIn(BLOCK.split("\n")[0], self.resident.sent[0])  # it was offered...
        self.assertTrue(any(e.get("t") == "err" for e in events))
        self.assertEqual(self.touched(), [])  # ...but never committed
        self.assertEqual(self.query("SELECT ref FROM memory_surface_cooldown"), [])
        self.assertEqual(self.query("SELECT committed FROM memory_surface_log WHERE user_message_id=?", uid), [(0,)])

        # Retry of the same user message: same decision replayed, then committed once persisted.
        gateway, patches, *_rest = GatewaySuccessHandoffTests._success_stream_patches(None, self.db, uid, self.resident)
        dr.reset_bindings_for_tests()
        with contextlib.ExitStack() as stack:
            for patch in list(patches) + self.real_surface("on"):
                stack.enter_context(patch)
            resp = gateway.app.test_client().post("/chat/stream", json={"content": QUERY})
            events = _sse_events_from_chunks(list(resp.response))
        self.assertIn("赫泰拉和阿斯帕西娅，她说想吃希腊菜", self.resident.sent[-1])
        self.assertTrue(any(e.get("t") == "done" and e.get("ok") for e in events))
        self.assertEqual(self.touched(), [[{"op": "touch", "bucket_ids": ["greek"]}]])
        self.assertEqual(self.query("SELECT touched FROM memory_surface_log WHERE user_message_id=?", uid), [(1,)])
        self.assertEqual(self.query("SELECT ref FROM memory_surface_cooldown"), [("kernel:greek",)])
        self.assertEqual(self.query("SELECT committed FROM memory_surface_log WHERE user_message_id=?", uid), [(1,)])
        self.assertEqual(self.cache_info()["memory_recall"], {"count": 1, "items": [
            {"type": "MEMORY", "date": "2026-06-27", "preview": GREEK["content"][:80]}]})


class DailyRecallPrefixTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        self.addCleanup(os.unlink, self.db)
        _init_chat_messages(self.db)

    def prepare(self, text, decide):
        uid = _insert(self.db, "hayana", text, "2026-07-27 10:00:00")
        runtime = FakeRuntime()
        with mock.patch.object(ms, "decide", side_effect=decide) as decide_mock, \
             mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env", return_value=runtime):
            plan = _prepare_turn(self.db, uid)
        wait_touches()
        self.assertEqual(runtime.calls, [])  # deciding never touches
        return plan, decide_mock

    def test_static_system_counts_as_context(self):
        _plan, decide = self.prepare("想吃希腊菜了", lambda **_k: injected())
        self.assertIn("S", decide.call_args.kwargs["context_text"])  # _prepare_turn's static system

    def test_recall_failure_never_blocks_the_turn(self):
        def boom(**_k):
            raise RuntimeError("surface exploded")
        with self.assertLogs(dr.logger, level="ERROR"):
            plan, _decide = self.prepare("今天好累", boom)
        self.assertEqual(plan.manifest["memory_recall_decision"], "failed")
        self.assertFalse(plan.manifest["auto_recall_injected"])
        resident = _FakeResident()
        list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system="STATIC"))
        dr._release_lease(plan)
        self.assertTrue(resident.sent[0].rstrip().endswith("今天好累"))

    def test_cold_rebuild_keeps_the_same_memory(self):
        plan, _decide = self.prepare("想吃希腊菜了", lambda **_k: injected())
        with mock.patch.object(config_store, "get_bool", return_value=True), \
             mock.patch("chat.daily_history._build_state_text", return_value=("", "none", {})):
            rebuilt = dr._rebuild_daily_assembly_with_history_budget(
                plan, static_system="S", history_token_budget=50, resident=None, is_cold=True, is_respawn=False)
        self.assertEqual(rebuilt["memory_recall"], BLOCK)
        dr._release_lease(plan)

    def test_prefix_order_task_feedback_then_memory_then_conversation(self):
        content = dr.format_resident_turn_content(
            assembly={"task_feedback": "TASK-FEEDBACK", "memory_recall": BLOCK,
                      "current_day_history": [{"role": "user", "content": "早上好"}]},
            user_content="想吃希腊菜了", is_cold=False, is_respawn=False)
        self.assertLess(content.index("TASK-FEEDBACK"), content.index(BLOCK))
        self.assertLess(content.index(BLOCK), content.index("早上好"))
        self.assertTrue(content.endswith("想吃希腊菜了"))


class GatewayRecallEventTests(unittest.TestCase):
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
        plan.user_message_id = uid
        plan.db_path = db
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
            commit = stack.enter_context(mock.patch.object(ms, "commit"))
            chunks = list(gateway._stream_cc_daily_soft_window({"user_message_id": uid}, {"content": "想吃希腊菜了"}))
        return _sse_events_from_chunks(chunks), json.loads(persisted["cache_info"]), commit, uid

    def test_recall_event_precedes_thinking_and_commits_after_persist(self):
        events, cache_info, commit, uid = self.run_stream(
            {"decision": "surfaced", "mode": "on", "block": BLOCK, "items": ITEMS})
        kinds = [e.get("t") for e in events if e.get("t") != "ping"]
        self.assertEqual(kinds[:2], ["memory_recall", "think"])
        recall = next(e for e in events if e.get("t") == "memory_recall")
        self.assertEqual(recall["d"], {"count": 1, "items": ITEMS})
        self.assertEqual(cache_info["memory_recall"], {"count": 1, "items": ITEMS})
        commit.assert_called_once()
        self.assertEqual(commit.call_args.kwargs["user_message_id"], uid)

    def test_no_recall_no_event_no_commit(self):
        events, cache_info, commit, _uid = self.run_stream({"decision": "surfaced", "mode": "shadow"})
        self.assertFalse(any(e.get("t") == "memory_recall" for e in events))
        self.assertNotIn("memory_recall", cache_info)
        commit.assert_not_called()


class ReviewApiTests(unittest.TestCase):
    """app.py routes for the review console, over a temp DB."""

    def setUp(self):
        import app
        self.app = app
        self.client = app.app.test_client()
        path = _tmp_db()
        self.addCleanup(os.unlink, path)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)")
        conn.execute("INSERT INTO chat_messages VALUES (5, 'hayana', '今天好累')")
        ms.ensure_schema(conn)
        conn.execute("INSERT INTO memory_surface_log (user_message_id, created_at, mode, decision, reason) "
                     "VALUES (5, '2026-10-22 08:00:00', 'shadow', 'none', 'no_clues')")
        conn.commit()
        conn.close()
        for patcher in (mock.patch.object(app, "DB_PATH", path),
                        mock.patch.object(ms, "_memory_previews", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_list_and_label(self):
        body = self.client.get("/api/memory-surface/review?filter=unlabeled&limit=10").get_json()
        self.assertEqual([r["message"] for r in body["rows"]], ["今天好累"])
        log_id = body["rows"][0]["id"]
        resp = self.client.post(f"/api/memory-surface/review/{log_id}/label", json={"label": "ok", "note": "对"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["label"]["label"], "ok")
        body = self.client.get("/api/memory-surface/review?filter=labeled").get_json()
        self.assertEqual(body["counts"]["ok"], 1)

    def test_bad_requests(self):
        self.assertEqual(self.client.get("/api/memory-surface/review?filter=x").status_code, 400)
        self.assertEqual(self.client.get("/api/memory-surface/review?before=abc").status_code, 400)
        self.assertEqual(self.client.post("/api/memory-surface/review/1/label", json={"label": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/memory-surface/review/999/label", json={"label": "ok"}).status_code,
                         404)


if __name__ == "__main__":
    unittest.main()
