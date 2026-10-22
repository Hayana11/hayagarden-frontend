"""tools.memory_surface: 0-or-1 passive recall with an evidence gate, cooldown and route log.

Fake pool and fake Ombre only: never reads the production Kernel or calls Ombre.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from tools import memory_surface as ms

NOW = datetime(2026, 10, 22, 14, 0, 0)
OLD = "2026-07-01 10:00:00"
ON = ms.SurfaceConfig(mode="on")


def item(ref, content, *, type_="MEMORY", created_at=OLD, bucket=None, projected=True):
    return {"ref": ref, "id": ref, "content": content, "type": type_, "created_at": created_at,
            "bucket_id": bucket or ref.split(":")[-1], "projected": projected, "source": "kernel"}


POOL = [
    item("kernel:greek", "六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。"),
    item("kernel:fish", "她第二回点了番茄玉米渔粉，不辣，加生菜。"),
    item("kernel:flow", "她修了一整晚 Hidden Flow 的提示词，flow studio 终于能看见了。"),
    item("kernel:other1", "下雨天她把伞给了我。"),
    item("kernel:other2", "她在阳台晒被子。"),
    item("kernel:recent", "今天中午她说赫泰拉阿斯帕西娅很有意思。", created_at="2026-10-22 12:00:00"),
]
FACT = item("kernel:fact", "她过敏的东西：芒果和菠萝蜜，吃了会起疹子。", type_="FACT")


class FakeRuntime:
    """Stands in for the Ombre worker: one result per request, like OmbreRuntime.batch."""

    timeout = 60.0

    def __init__(self):
        self.calls = []
        self.fail = None  # an exception to raise for the whole run (runtime unavailable)
        self.bad = set()  # buckets whose own request fails
        self.during = None  # called inside the run, before results come back

    def batch(self, requests):
        self.calls.append(requests)
        if self.during:
            self.during()
        if self.fail is not None:
            raise self.fail
        from tools.memory_kernel_projection import RequestFailed
        return [RequestFailed("bucket_not_found") if r["bucket_ids"][0] in self.bad else {"touched": 1}
                for r in requests]

    def touched(self):
        return [r["bucket_ids"][0] for call in self.calls for r in call]


def wait_touches():
    """commit() hands the touch to a background thread; tests wait for it."""
    import threading
    for thread in threading.enumerate():
        if thread.name == "memory-surface-touch":
            thread.join(5)


class Base(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Path(path)
        self.addCleanup(self.db.unlink)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT)")
        # Chatter that makes everyday words common in their conversation.
        for i in range(1, 41):
            conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (?,?,?)",
                         (i, "hayana" if i % 2 else "fyodor", "这个地方一次变成了正常的事情"))
        conn.commit()
        conn.close()
        self.ombre_calls = []
        self.ombre_result = ["greek", "flow", "fish"]
        self.runtime = FakeRuntime()
        patcher = mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env",
                             side_effect=lambda *a, **k: self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ombre(self, query, limit):
        self.ombre_calls.append(query)
        return self.ombre_result

    def decide(self, text, msg_id=100, *, pool=None, context="", config=ON, ombre="default", now=NOW):
        return ms.decide(
            user_message_id=msg_id, user_text=text, context_text=context, db_path=self.db,
            config=config, pool_loader=lambda _p: list(POOL if pool is None else pool),
            ombre=self.ombre if ombre == "default" else ombre, now=now,
        )

    def query(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def log_rows(self):
        return self.query("SELECT user_message_id, mode, decision, reason, ref, committed "
                          "FROM memory_surface_log ORDER BY id")

    def cooldown_refs(self):
        try:
            return [r[0] for r in self.query("SELECT ref FROM memory_surface_cooldown")]
        except sqlite3.OperationalError:
            return []

    def add_user_turns(self, count, start=1000):
        conn = sqlite3.connect(self.db)
        for i in range(count):
            conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (?,?,?)", (start + i, "hayana", "嗯"))
        conn.commit()
        conn.close()


class EntryAndKeywordTests(Base):
    def test_entry_gate(self):
        self.assertEqual(ms.entry_gate(""), "no_text")
        self.assertEqual(ms.entry_gate("[image]"), "no_text")
        self.assertEqual(ms.entry_gate("/clear now"), "command")
        self.assertEqual(ms.entry_gate("双皮奶"), "too_short")
        self.assertIsNone(ms.entry_gate("想吃希腊菜了"))

    def test_keywords_drop_pet_names_fillers_and_short_english(self):
        words = ms.keywords("爸爸爸爸 费佳费佳 宝宝 you are the hidden flow 赫泰拉")
        self.assertNotIn("爸爸爸爸", words)
        self.assertNotIn("费佳费佳", words)
        self.assertNotIn("宝宝", words)
        for english in ("you", "are", "the"):
            self.assertNotIn(english, words)
        self.assertIn("hidden", words)
        self.assertIn("flow", words)

    def test_pasted_blocks_are_not_her_words(self):
        self.assertNotIn("赫泰拉", ms.query_text("好累【赫泰拉 阿斯帕西娅 private after】"))
        self.assertNotIn("赫泰拉", ms.query_text("看这个```赫泰拉```"))


class DecisionTests(Base):
    def test_two_rare_clues_surface_exactly_one_original_memory(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了")
        self.assertTrue(result.injected)
        self.assertEqual(result.ref, "kernel:greek")
        self.assertIn("<recalled-memory>", result.block)
        self.assertIn("六月二十七日凌晨我们聊赫泰拉", result.block)  # original text, not a summary
        self.assertIn("2026-07-01", result.block)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(self.log_rows(), [(100, "on", "surfaced", "gate_passed", "kernel:greek", 0)])

    def test_one_common_overlap_is_not_a_memory(self):
        result = self.decide("这个地方一次变成正常了")  # words frequent in their chat
        self.assertEqual((result.decision, result.reason), ("none", "no_clues"))
        self.assertFalse(result.injected)

    def test_single_clue_needs_two_lanes(self):
        # 渔粉 is one clue: not enough on its own...
        self.ombre_result = ["greek", "flow"]
        self.assertEqual(self.decide("渔粉到了", msg_id=101).decision, "none")
        # ...unless it never appears in their chat and Ombre ranks that memory top-3.
        self.ombre_result = ["fish"]
        self.assertEqual(self.decide("渔粉到了", msg_id=102).ref, "kernel:fish")
        self.ombre_result = ["other1", "other2", "greek", "fish"]
        self.assertEqual(self.decide("渔粉到了", msg_id=103).decision, "none")

    def test_recent_memories_are_not_candidates(self):
        pool = [p for p in POOL if p["ref"] in ("kernel:recent", "kernel:other1")]
        self.assertEqual(self.decide("想起赫泰拉和阿斯帕西娅了", pool=pool).decision, "none")

    def test_fact_surfaces_unless_this_turn_already_carries_it(self):
        pool = POOL + [FACT]
        self.assertEqual(self.decide("今天吃了芒果和菠萝蜜", msg_id=110, pool=pool).ref, "kernel:fact")
        carried = self.decide("今天吃了芒果和菠萝蜜", msg_id=111, pool=pool,
                              context="## 关于她\n- 她过敏的东西：芒果和菠萝蜜，吃了会起疹子。")
        self.assertEqual(carried.decision, "none")
        self.assertEqual(carried.detail["trail"][0][1], "in_context")

    def test_already_in_context_is_skipped(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了", context="早上她说：六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。")
        self.assertEqual((result.decision, result.reason), ("none", "gate_closed"))
        self.assertEqual(result.detail["trail"][0][1], "in_context")

    def test_same_message_gets_the_same_answer(self):
        first = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        again = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        self.assertEqual((again.ref, again.block), (first.ref, first.block))
        self.assertTrue(again.detail.get("replayed"))
        self.assertEqual(len(self.log_rows()), 1)  # replay is not a second decision

    def test_replay_never_resurrects_a_retired_memory(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        gone = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100,
                           pool=[p for p in POOL if p["ref"] != "kernel:greek"])
        self.assertEqual((gone.decision, gone.reason), ("none", "replayed_ref_gone"))

    def test_ombre_down_keeps_keyword_lane(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了", ombre=lambda q, n: None)
        self.assertEqual(result.ref, "kernel:greek")
        self.assertEqual(result.detail["ombre"], "unavailable")

    def test_skips_and_failures_are_logged_not_silent(self):
        self.assertEqual(self.decide("嗯", msg_id=1).reason, "too_short")

        def broken(_p):
            raise RuntimeError("kernel unreadable")
        with self.assertLogs("hayagarden.memory_surface", level="ERROR"):
            result = ms.decide(user_message_id=7, user_text="想起赫泰拉和阿斯帕西娅了", context_text="",
                               db_path=self.db, config=ON, pool_loader=broken, ombre=self.ombre, now=NOW)
        self.assertEqual((result.decision, result.reason), ("failed", "error"))
        self.assertFalse(result.injected)
        self.assertEqual(self.log_rows(), [(1, "on", "skipped", "too_short", None, 0),
                                           (7, "on", "failed", "error", None, 0)])

    def test_kernel_cutover_off_skips(self):
        with mock.patch("tools.memory_kernel_switch.cutover_enabled", return_value=False):
            result = ms.decide(user_message_id=8, user_text="想起赫泰拉和阿斯帕西娅了", context_text="",
                               db_path=self.db, config=ON, ombre=self.ombre, now=NOW)
        self.assertEqual((result.decision, result.reason), ("skipped", "kernel_cutover_off"))


class SingleClueTests(Base):
    def test_her_current_message_does_not_count_against_its_own_clue(self):
        # Daily stores the message before deciding; it must not make 渔粉 look "seen once".
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (100, 'hayana', '渔粉到了')")
        conn.commit()
        conn.close()
        self.ombre_result = ["fish"]
        result = self.decide("渔粉到了", msg_id=100)
        self.assertEqual(result.ref, "kernel:fish")
        self.assertEqual(result.detail["chat_df"], {"渔粉": 0})

    def test_an_earlier_message_still_counts(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (99, 'hayana', '渔粉好吃')")
        conn.execute("INSERT INTO chat_messages (id, author, content) VALUES (100, 'hayana', '渔粉到了')")
        conn.commit()
        conn.close()
        self.ombre_result = ["fish"]
        self.assertEqual(self.decide("渔粉到了", msg_id=100).decision, "none")


class NoSubstituteTests(Base):
    """Only the best candidate is judged; when it is blocked nothing surfaces."""

    RUNNER_UP = item("kernel:greek2", "她说赫泰拉和阿斯帕西娅的故事还没讲完。")

    def pool(self):
        fillers = [item(f"kernel:filler{i}", f"第{i}条无关的日常记忆。") for i in range(4)]
        return POOL + [self.RUNNER_UP] + fillers

    def test_top_cooling_down_is_not_replaced_by_the_runner_up(self):
        self.ombre_result = ["greek"]
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100, pool=self.pool())
        ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True)
        self.ombre_result = ["greek", "greek2"]
        result = self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=101, pool=self.pool())
        self.assertEqual((result.decision, result.reason), ("none", "gate_closed"))
        self.assertEqual(result.detail["trail"], [["kernel:greek", "cooldown", result.detail["trail"][0][2]]])
        self.assertEqual(result.detail["runner_up"], "kernel:greek2")  # would have passed, stays out

    def test_top_already_in_context_is_not_replaced_by_the_runner_up(self):
        self.ombre_result = ["greek", "greek2"]
        result = self.decide("想起赫泰拉和阿斯帕西娅了", pool=self.pool(),
                             context="六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。")
        self.assertEqual(result.decision, "none")
        self.assertEqual(result.detail["trail"][0][:2], ["kernel:greek", "in_context"])
        self.assertEqual(result.detail["runner_up"], "kernel:greek2")
        self.assertFalse(result.injected)

    def test_a_memory_without_any_clue_is_never_a_candidate(self):
        self.ombre_result = ["other1", "greek"]  # Ombre's top shares no word with her message
        self.assertEqual(self.decide("想起赫泰拉和阿斯帕西娅了").ref, "kernel:greek")


class LogFailClosedTests(Base):
    def test_unlogged_decision_never_surfaces(self):
        with mock.patch.object(ms, "_log", side_effect=sqlite3.OperationalError("disk I/O error")), \
                self.assertLogs("hayagarden.memory_surface", level="ERROR"):
            result = self.decide("想起赫泰拉和阿斯帕西娅了")
        self.assertEqual((result.decision, result.reason), ("failed", "log_write_failed"))
        self.assertFalse(result.injected)
        self.assertEqual((result.block, result.items), ("", []))
        self.assertEqual(result.detail["unlogged_ref"], "kernel:greek")
        self.assertIsNone(ms.commit(user_message_id=100, db_path=self.db, now=NOW))
        self.assertEqual(self.cooldown_refs(), [])


class ModeTests(Base):
    def test_off_does_nothing_at_all(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了", config=ms.SurfaceConfig(mode="off"))
        self.assertEqual((result.decision, result.reason, result.mode), ("skipped", "off", "off"))
        self.assertEqual(self.ombre_calls, [])
        with self.assertRaises(sqlite3.OperationalError):  # not even the tables
            self.log_rows()

    def test_shadow_logs_the_decision_but_hands_nothing_to_the_turn(self):
        shadow = ms.SurfaceConfig(mode="shadow")
        result = self.decide("想起赫泰拉和阿斯帕西娅了", config=shadow)
        self.assertEqual((result.decision, result.ref, result.mode), ("surfaced", "kernel:greek", "shadow"))
        self.assertFalse(result.injected)
        self.assertEqual((result.block, result.items), ("", []))
        self.assertEqual(self.log_rows(), [(100, "shadow", "surfaced", "gate_passed", "kernel:greek", 0)])
        # Nothing to commit in shadow: no cooldown, no touch.
        self.assertIsNone(ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True))
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.cooldown_refs(), [])
        # So the next message can still pick the same memory.
        self.assertEqual(self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=101, config=shadow).ref, "kernel:greek")

    def test_a_shadow_decision_is_not_replayed_once_mode_is_on(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", config=ms.SurfaceConfig(mode="shadow"))
        on = self.decide("想起赫泰拉和阿斯帕西娅了")
        self.assertTrue(on.injected)
        self.assertFalse(on.detail.get("replayed"))

    def test_unknown_mode_falls_back_to_shadow(self):
        with mock.patch("config_store.get", side_effect=lambda key, default=None: (
                "loud" if key == "MEMORY_SURFACE_MODE" else default)):
            with self.assertLogs("hayagarden.memory_surface", level="WARNING"):
                self.assertEqual(ms.load_config().mode, "shadow")


class CommitTests(Base):
    def test_deciding_consumes_nothing_until_commit(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        self.assertEqual(self.cooldown_refs(), [])
        # A retry of the same message (the first attempt failed before persisting) gets the same memory.
        self.assertEqual(self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100).ref, "kernel:greek")
        # A different message is not blocked either: nothing has surfaced yet.
        self.assertEqual(self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=101).ref, "kernel:greek")

    def test_commit_starts_cooldown_marks_committed_and_touches_once(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        self.assertEqual(ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True), "kernel:greek")
        self.assertIsNone(ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True))  # idempotent
        self.assertEqual(self.runtime.calls, [[{"op": "touch", "bucket_ids": ["greek"]}]])
        self.assertEqual(self.query("SELECT ref, bucket_id, status, attempts FROM memory_surface_touch_outbox"),
                         [("kernel:greek", "greek", "done", 1)])
        self.assertEqual(self.cooldown_refs(), ["kernel:greek"])
        self.assertEqual(self.log_rows()[0][-1], 1)

    def test_unprojected_memory_commits_without_touch(self):
        pool = [item("kernel:greek", POOL[0]["content"], projected=False)] + POOL[1:]
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100, pool=pool)
        self.assertEqual(ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True), "kernel:greek")
        self.assertEqual(self.runtime.calls, [])
        self.assertEqual(self.query("SELECT COUNT(*) FROM memory_surface_touch_outbox"), [(0,)])  # nothing to touch

    def test_cooldown_counts_her_turns_after_commit(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        ms.commit(user_message_id=100, db_path=self.db, now=NOW, wait=True)
        self.add_user_turns(10, start=200)
        cooled = self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=300)
        self.assertEqual(cooled.decision, "none")
        self.assertEqual(cooled.detail["trail"][0][1], "cooldown")
        self.add_user_turns(60, start=400)
        self.assertEqual(self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=600).ref, "kernel:greek")


class _Crash(BaseException):
    """The process dies mid-send (not an Exception: nothing inside catches it)."""


class _OutboxBase(Base):
    def surface(self, msg_id, text="想起赫泰拉和阿斯帕西娅了", ombre=("greek", "flow", "fish")):
        self.ombre_result = list(ombre)
        result = self.decide(text, msg_id=msg_id)
        self.assertEqual(result.decision, "surfaced", result.detail)
        return ms.commit(user_message_id=msg_id, db_path=self.db, now=NOW, wait=True)

    def outbox(self):
        return self.query("SELECT user_message_id, bucket_id, status, attempts, last_error "
                          "FROM memory_surface_touch_outbox ORDER BY id")


class TouchOutboxTests(_OutboxBase):
    def test_runtime_outage_spends_no_attempt_and_the_next_flush_delivers(self):
        self.runtime.fail = RuntimeError("worker_unreachable")
        self.surface(100)
        [(_msg, _bucket, status, attempts, error)] = self.outbox()
        self.assertEqual((status, attempts), ("pending", 0))
        self.assertIn("worker_unreachable", error)
        self.runtime.fail = None
        self.assertEqual(ms.flush_touches(self.db, now=NOW)["done"], 1)
        self.assertEqual([r[2:4] for r in self.outbox()], [("done", 1)])

    def test_no_runtime_configured_leaves_rows_waiting(self):
        with mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env", return_value=None):
            self.surface(100)
        self.assertEqual([r[2:4] for r in self.outbox()], [("pending", 0)])
        self.assertEqual(self.runtime.calls, [])

    def test_each_row_is_its_own_request_so_two_surfacings_mean_two_touches(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.add_user_turns(61, start=200)  # past the cooldown
        self.surface(300, text="又想起赫泰拉和阿斯帕西娅了")
        self.surface(400, text="flow studio 和 hidden 修好啦", ombre=("flow",))
        self.runtime.fail, self.runtime.calls = None, []
        self.assertEqual(ms.flush_touches(self.db, now=NOW)["done"], 3)
        self.assertEqual(self.runtime.calls, [[{"op": "touch", "bucket_ids": ["greek"]},
                                               {"op": "touch", "bucket_ids": ["greek"]},
                                               {"op": "touch", "bucket_ids": ["flow"]}]])

    def test_one_bad_bucket_fails_only_its_own_row(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.surface(400, text="flow studio 和 hidden 修好啦", ombre=("flow",))
        self.runtime.fail, self.runtime.bad = None, {"greek"}
        counts = ms.flush_touches(self.db, now=NOW)
        self.assertEqual((counts["done"], counts["retry"]), (1, 1))
        self.assertEqual([r[1:4] for r in self.outbox()], [("greek", "pending", 1), ("flow", "done", 1)])
        self.assertEqual(self.outbox()[0][4], "request:bucket_not_found")

    def test_failed_after_max_attempts_with_its_last_error(self):
        self.runtime.bad = {"greek"}
        self.surface(100)
        for _ in range(ms.MAX_TOUCH_ATTEMPTS + 2):
            ms.flush_touches(self.db, now=NOW)
        self.assertEqual(self.outbox(), [(100, "greek", "failed", ms.MAX_TOUCH_ATTEMPTS, "request:bucket_not_found")])
        self.assertEqual(len(self.runtime.touched()), ms.MAX_TOUCH_ATTEMPTS)
        self.assertEqual([r["status"] for r in ms.outbox_rows(self.db, status="failed")], ["failed"])

    def test_a_leased_row_is_not_sent_by_a_concurrent_flush(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.runtime.fail, self.runtime.calls = None, []
        inner = []
        # While the first flush is sending, another flusher (own connection, as another process would) runs.
        self.runtime.during = lambda: inner.append(ms.flush_touches(self.db, runtime=FakeRuntime(), now=NOW))
        self.assertEqual(ms.flush_touches(self.db, now=NOW)["done"], 1)
        self.assertEqual(inner[0]["claimed"], 0)
        self.assertEqual(self.runtime.touched(), ["greek"])

    def test_a_crash_mid_send_is_resent_after_the_lease_expires(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.runtime.fail, self.runtime.calls = _Crash(), []
        with self.assertRaises(_Crash):
            ms.flush_touches(self.db, now=NOW)
        self.assertEqual([r[2:4] for r in self.outbox()], [("sending", 1)])
        self.runtime.fail = None
        self.assertEqual(ms.flush_touches(self.db, now=NOW)["claimed"], 0)  # lease still held
        later = NOW + timedelta(seconds=FakeRuntime.timeout + 121)
        self.assertEqual(ms.flush_touches(self.db, now=later)["done"], 1)  # at-least-once: sent again
        self.assertEqual(self.runtime.touched(), ["greek", "greek"])

    def test_an_expired_lease_with_no_attempts_left_is_marked_failed(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memory_surface_touch_outbox SET status='sending', attempts=?, "
                     "lease_until='2026-10-22 13:00:00'", (ms.MAX_TOUCH_ATTEMPTS,))
        conn.commit()
        conn.close()
        ms.flush_touches(self.db, now=NOW)
        self.assertEqual(self.outbox()[0][2:], ("failed", ms.MAX_TOUCH_ATTEMPTS, "lease_expired"))

    def test_route_log_pruning_keeps_pending_touches(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        later = NOW + timedelta(days=40)
        self.decide("今天好累啊啊啊", msg_id=500, now=later)  # this write prunes the 40-day-old log row
        self.assertEqual(self.query("SELECT COUNT(*) FROM memory_surface_log WHERE user_message_id=100"), [(0,)])
        self.runtime.fail = None
        self.assertEqual(ms.flush_touches(self.db, now=later)["done"], 1)

    def test_done_rows_are_pruned_after_their_retention(self):
        self.surface(100)
        ms.flush_touches(self.db, now=NOW + timedelta(days=ms.OUTBOX_DONE_RETENTION_DAYS + 1))
        self.assertEqual(self.outbox(), [])

    def test_migrate_creates_every_table(self):
        tables = ms.migrate(self.db)
        self.assertIn("lease_until", tables["memory_surface_touch_outbox"])
        self.assertIn("snapshot_json", tables["memory_surface_labels"])
        self.assertNotIn("touched", tables["memory_surface_log"])  # the log table's shape is unchanged


class FlushExitCodeTests(_OutboxBase):
    """`python -m tools.memory_surface flush` is the timer's verdict: no false green."""

    def run_flush(self, db=None):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ms.main(["flush", "--db", str(db or self.db)])
        return code, json.loads(out.getvalue())

    def test_nothing_owed_exits_zero(self):
        ms.migrate(self.db)
        self.assertEqual(self.run_flush()[0], 0)

    def test_everything_delivered_exits_zero(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.runtime.fail = None
        code, report = self.run_flush()
        self.assertEqual((code, report["run"]["done"]), (0, 1))

    def test_ombre_unavailable_exits_one_without_spending_attempts(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        code, report = self.run_flush()
        self.assertEqual((code, report["run"]["runtime"]), (1, "unavailable"))
        self.assertEqual([r[2:4] for r in self.outbox()], [("pending", 0)])

    def test_a_retry_this_run_exits_one(self):
        self.runtime.fail = RuntimeError("down")
        self.surface(100)
        self.runtime.fail, self.runtime.bad = None, {"greek"}
        code, report = self.run_flush()
        self.assertEqual((code, report["run"]["retry"]), (1, 1))

    def test_a_failure_this_run_exits_one(self):
        self.runtime.bad = {"greek"}
        self.surface(100)
        for _ in range(ms.MAX_TOUCH_ATTEMPTS - 2):
            ms.flush_touches(self.db, now=NOW)
        code, report = self.run_flush()
        self.assertEqual((code, report["run"]["failed"]), (1, 1))

    def test_an_old_failed_row_keeps_exiting_one_with_nothing_claimed(self):
        self.runtime.bad = {"greek"}
        self.surface(100)
        for _ in range(ms.MAX_TOUCH_ATTEMPTS):
            ms.flush_touches(self.db, now=NOW)
        code, report = self.run_flush()
        self.assertEqual((code, report["run"]["claimed"], report["outbox"]["failed"]), (1, 0, 1))

    def test_pending_rows_with_no_runtime_configured_exit_one(self):
        with mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env", return_value=None):
            self.surface(100)
            code, report = self.run_flush()
        self.assertEqual((code, report["run"]["runtime"], report["outbox"]["pending"]), (1, "missing", 1))

    def test_no_runtime_and_nothing_owed_exits_zero(self):
        ms.migrate(self.db)
        with mock.patch("tools.memory_kernel_projection.OmbreRuntime.from_env", return_value=None):
            self.assertEqual(self.run_flush()[0], 0)

    def test_an_unreadable_database_exits_one(self):
        code, report = self.run_flush(db=Path(tempfile.gettempdir()) / "no-such-dir" / "memories.db")
        self.assertEqual((code, report["run"]["error"], report["outbox"]), (1, 1, None))


class ReviewTests(Base):
    """The review console's data: decisions frozen when made, labels that outlive the log."""

    def setUp(self):
        super().setUp()
        self.pool = list(POOL)
        self.loader = lambda _p: list(self.pool)
        conn = sqlite3.connect(self.db)
        conn.executemany("INSERT INTO chat_messages (id, author, content) VALUES (?,?,?)", [
            (100, "hayana", "想起赫泰拉和阿斯帕西娅了"), (101, "hayana", "今天好累啊啊啊"),
            (102, "hayana", "一个地方变成正常")])
        conn.commit()
        conn.close()
        for msg_id, text in ((100, "想起赫泰拉和阿斯帕西娅了"), (101, "今天好累啊啊啊"), (102, "一个地方变成正常")):
            self.decide(text, msg_id=msg_id, pool=self.pool)
        self.ids = [r[0] for r in self.query("SELECT id FROM memory_surface_log ORDER BY id")]

    def page(self, **kwargs):
        return ms.review_rows(self.db, pool_loader=self.loader, **kwargs)

    def label(self, log_id, label, **kwargs):
        return ms.set_label(self.db, log_id=log_id, label=label, now=NOW, pool_loader=self.loader, **kwargs)

    def test_rows_carry_her_words_and_the_memory_newest_first(self):
        page = self.page()
        self.assertEqual([r["user_message_id"] for r in page["rows"]], [102, 101, 100])
        surfaced = page["rows"][-1]
        self.assertEqual((surfaced["decision"], surfaced["reason"], surfaced["frozen"]),
                         ("surfaced", "gate_passed", True))
        self.assertEqual(surfaced["message"], "想起赫泰拉和阿斯帕西娅了")
        self.assertEqual(surfaced["top"]["ref"], "kernel:greek")
        self.assertIn("希腊菜", surfaced["top"]["text"])
        self.assertIn("西娅", surfaced["clues"])
        self.assertIsNone(surfaced["label"])
        self.assertEqual(page["counts"], {"total": 3, "labeled": 0, "ok": 0, "missed": 0, "wrong": 0})

    def test_paging_and_filters(self):
        first = self.page(limit=2)
        self.assertEqual(first["next_before"], self.ids[1])
        rest = self.page(limit=2, before_id=first["next_before"])
        self.assertEqual([r["user_message_id"] for r in rest["rows"]], [100])
        self.assertIsNone(rest["next_before"])
        self.assertEqual([r["user_message_id"] for r in self.page(only="surfaced")["rows"]], [100])
        self.label(self.ids[0], "ok")
        self.assertEqual([r["user_message_id"] for r in self.page(only="labeled")["rows"]], [100])
        self.assertEqual([r["user_message_id"] for r in self.page(only="unlabeled")["rows"]], [102, 101])
        with self.assertRaises(ValueError):
            self.page(only="everything")

    def test_snapshot_is_what_the_gate_saw_not_later_edits(self):
        # After the decision: her message and the memory are both revised.
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE chat_messages SET content='改过的话' WHERE id=100")
        conn.commit()
        conn.close()
        self.pool[0] = item("kernel:greek", "改写后的记忆：完全不同的内容。")
        row = self.label(self.ids[0], "wrong", note="不该想起")
        self.assertEqual(row["message"], "想起赫泰拉和阿斯帕西娅了")
        self.assertIn("希腊菜", row["top"]["text"])
        snap = json.loads(self.query("SELECT snapshot_json FROM memory_surface_labels")[0][0])
        self.assertEqual(snap["message"], "想起赫泰拉和阿斯帕西娅了")
        self.assertIn("希腊菜", snap["top"]["text"])
        self.assertTrue(snap["frozen"])

    def test_label_upserts_keeps_its_first_snapshot_and_clears(self):
        row = self.label(self.ids[1], "missed", note="该想起渔粉", expected_ref="kernel:fish")
        self.assertEqual((row["label"]["label"], row["label"]["expected_ref"]), ("missed", "kernel:fish"))
        self.assertIn("渔粉", row["expected"]["text"])
        row = self.label(self.ids[1], "wrong")
        self.assertEqual((row["label"]["label"], row["label"]["note"]), ("wrong", ""))
        self.assertNotIn("expected", row)
        self.assertEqual(self.query("SELECT COUNT(*) FROM memory_surface_labels"), [(1,)])
        self.assertEqual(self.page()["counts"]["wrong"], 1)
        self.assertIsNone(self.label(self.ids[1], "clear")["label"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM memory_surface_labels"), [(0,)])
        self.assertIsNone(self.label(9999, "ok"))
        with self.assertRaises(ValueError):
            self.label(self.ids[1], "maybe")

    def test_a_labeled_decision_outlives_the_pruned_log_row(self):
        self.label(self.ids[0], "wrong", note="不该想起")
        self.label(self.ids[1], "missed", expected_ref="kernel:fish")
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM memory_surface_log WHERE id IN (?, ?)", (self.ids[0], self.ids[1]))
        conn.commit()
        conn.close()
        labeled = self.page(only="labeled")
        self.assertEqual([r["user_message_id"] for r in labeled["rows"]], [101, 100])
        self.assertTrue(all(r["log_pruned"] for r in labeled["rows"]))
        self.assertEqual(labeled["rows"][1]["message"], "想起赫泰拉和阿斯帕西娅了")
        self.assertEqual(labeled["counts"], {"total": 3, "labeled": 2, "ok": 0, "missed": 1, "wrong": 1})
        self.assertEqual([r["user_message_id"] for r in self.page()["rows"]], [102, 101, 100])
        self.assertEqual([r["user_message_id"] for r in self.page(only="surfaced")["rows"]], [100])
        # Still editable and clearable without its log row.
        self.assertEqual(self.label(self.ids[0], "wrong", note="改备注")["label"]["note"], "改备注")
        self.assertEqual(self.label(self.ids[0], "clear"), {"id": self.ids[0], "removed": True})
        self.assertEqual(self.page()["counts"]["labeled"], 1)

    def test_rows_logged_before_snapshots_fall_back_to_live_text_and_say_so(self):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO memory_surface_log (user_message_id, created_at, mode, decision, reason, ref, "
                     "detail_json) VALUES (101, '2026-10-22 13:00:00', 'shadow', 'surfaced', 'gate_passed', "
                     "'kernel:fish', '{}')")
        conn.commit()
        conn.close()
        row = self.page(limit=1)["rows"][0]
        self.assertFalse(row["frozen"])
        self.assertEqual(row["message"], "今天好累啊啊啊")
        self.assertIn("渔粉", row["top"]["text"])

    def test_empty_database_reads_as_no_rows(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertEqual(ms.review_rows(path)["rows"], [])


class RetentionTests(Base):
    def test_route_log_is_pruned_after_retention(self):
        cfg = ms.SurfaceConfig(mode="on", log_retention_days=30)
        self.decide("嗯", msg_id=1, config=cfg, now=NOW - timedelta(days=40))
        self.decide("嗯", msg_id=2, config=cfg, now=NOW - timedelta(days=10))
        self.decide("嗯", msg_id=3, config=cfg, now=NOW)
        self.assertEqual([r[0] for r in self.log_rows()], [2, 3])


if __name__ == "__main__":
    unittest.main()
