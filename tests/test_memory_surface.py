"""tools.memory_surface: 0-or-1 passive recall with an evidence gate, cooldown and route log.

Fake pool and fake Ombre only: never reads the production Kernel or calls Ombre.
"""
from __future__ import annotations

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
        with mock.patch("tools.memory_kernel_read.touch_buckets") as touch:
            self.assertIsNone(ms.commit(user_message_id=100, db_path=self.db, now=NOW))
        touch.assert_not_called()
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
        with mock.patch("tools.memory_kernel_read.touch_buckets") as touch:
            self.assertEqual(ms.commit(user_message_id=100, db_path=self.db, now=NOW), "kernel:greek")
            self.assertIsNone(ms.commit(user_message_id=100, db_path=self.db, now=NOW))  # idempotent
        touch.assert_called_once_with(["greek"])
        self.assertEqual(self.cooldown_refs(), ["kernel:greek"])
        self.assertEqual(self.log_rows()[0][-1], 1)

    def test_unprojected_memory_commits_without_touch(self):
        pool = [item("kernel:greek", POOL[0]["content"], projected=False)] + POOL[1:]
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100, pool=pool)
        with mock.patch("tools.memory_kernel_read.touch_buckets") as touch:
            self.assertEqual(ms.commit(user_message_id=100, db_path=self.db, now=NOW), "kernel:greek")
        touch.assert_not_called()

    def test_cooldown_counts_her_turns_after_commit(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        with mock.patch("tools.memory_kernel_read.touch_buckets"):
            ms.commit(user_message_id=100, db_path=self.db, now=NOW)
        self.add_user_turns(10, start=200)
        cooled = self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=300)
        self.assertEqual(cooled.decision, "none")
        self.assertEqual(cooled.detail["trail"][0][1], "cooldown")
        self.add_user_turns(60, start=400)
        self.assertEqual(self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=600).ref, "kernel:greek")


class RetentionTests(Base):
    def test_route_log_is_pruned_after_retention(self):
        cfg = ms.SurfaceConfig(mode="on", log_retention_days=30)
        self.decide("嗯", msg_id=1, config=cfg, now=NOW - timedelta(days=40))
        self.decide("嗯", msg_id=2, config=cfg, now=NOW - timedelta(days=10))
        self.decide("嗯", msg_id=3, config=cfg, now=NOW)
        self.assertEqual([r[0] for r in self.log_rows()], [2, 3])


if __name__ == "__main__":
    unittest.main()
