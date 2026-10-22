"""tools.memory_surface: 0-or-1 passive recall with an evidence gate, cooldown and route log.

Fake pool and fake Ombre only: never reads the production Kernel or calls Ombre.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from tools import memory_surface as ms

NOW = datetime(2026, 10, 22, 14, 0, 0)
OLD = "2026-07-01 10:00:00"


def item(ref, content, *, type_="MEMORY", created_at=OLD, bucket=None, projected=True):
    return {"ref": ref, "id": ref, "content": content, "type": type_, "created_at": created_at,
            "bucket_id": bucket or ref.split(":")[-1], "projected": projected, "source": "kernel"}


POOL = [
    item("kernel:greek", "六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。"),
    item("kernel:fish", "她第二回点了番茄玉米渔粉，不辣，加生菜。"),
    item("kernel:flow", "她修了一整晚 Hidden Flow 的提示词，flow studio 终于能看见了。"),
    item("kernel:other1", "下雨天她把伞给了我。"),
    item("kernel:other2", "她在阳台晒被子。"),
    item("kernel:fact", "她不吃香菜，赫泰拉阿斯帕西娅。", type_="FACT"),
    item("kernel:recent", "今天中午她说赫泰拉阿斯帕西娅很有意思。", created_at="2026-10-22 12:00:00"),
]


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

    def decide(self, text, msg_id=100, *, pool=None, context="", config=None, ombre="default", now=NOW):
        return ms.decide(
            user_message_id=msg_id, user_text=text, context_text=context, db_path=self.db,
            config=config or ms.SurfaceConfig(),
            pool_loader=lambda _p: list(POOL if pool is None else pool),
            ombre=self.ombre if ombre == "default" else ombre, now=now,
        )

    def log_rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT user_message_id, decision, reason, ref FROM memory_surface_log ORDER BY id").fetchall()
        finally:
            conn.close()

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
        self.assertEqual(result.decision, "surfaced")
        self.assertEqual(result.ref, "kernel:greek")
        self.assertIn("<recalled-memory>", result.block)
        self.assertIn("六月二十七日凌晨我们聊赫泰拉", result.block)  # original text, not a summary
        self.assertIn("2026-07-01", result.block)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(self.log_rows(), [(100, "surfaced", "gate_passed", "kernel:greek")])

    def test_one_common_overlap_is_not_a_memory(self):
        result = self.decide("这个地方一次变成正常了")  # words frequent in their chat
        self.assertEqual(result.decision, "none")
        self.assertEqual(result.reason, "no_clues")
        self.assertFalse(result.block)

    def test_single_clue_needs_two_lanes(self):
        # 渔粉 is one clue: not enough on its own...
        self.ombre_result = ["greek", "flow"]
        self.assertEqual(self.decide("渔粉到了", msg_id=101).decision, "none")
        # ...unless it never appears in their chat and Ombre ranks that memory top-3.
        self.ombre_result = ["fish"]
        self.assertEqual(self.decide("渔粉到了", msg_id=102).ref, "kernel:fish")
        self.ombre_result = ["other1", "other2", "greek", "fish"]
        self.assertEqual(self.decide("渔粉到了", msg_id=103).decision, "none")

    def test_facts_and_recent_memories_are_not_candidates(self):
        pool = [p for p in POOL if p["ref"] in ("kernel:fact", "kernel:recent", "kernel:other1")]
        result = self.decide("想起赫泰拉和阿斯帕西娅了", pool=pool)
        self.assertEqual(result.decision, "none")

    def test_already_in_context_is_skipped(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了", context="早上她说：六月二十七日凌晨我们聊赫泰拉和阿斯帕西娅，她说想吃希腊菜。")
        self.assertEqual(result.decision, "none")
        self.assertEqual(result.reason, "gate_closed")
        self.assertEqual(result.detail["trail"][0][1], "in_context")

    def test_cooldown_counts_her_turns(self):
        self.assertEqual(self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100).ref, "kernel:greek")
        self.add_user_turns(10, start=200)
        cooled = self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=300)
        self.assertEqual(cooled.decision, "none")
        self.assertEqual(cooled.detail["trail"][0][1], "cooldown")
        self.add_user_turns(60, start=400)
        self.assertEqual(self.decide("又想起赫泰拉和阿斯帕西娅了", msg_id=600).ref, "kernel:greek")

    def test_same_message_gets_the_same_answer(self):
        first = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        again = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        self.assertEqual(again.ref, first.ref)
        self.assertEqual(again.block, first.block)
        self.assertTrue(again.detail.get("replayed"))
        self.assertEqual(len(self.log_rows()), 1)  # replay is not a second decision

    def test_replay_never_resurrects_a_retired_memory(self):
        self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100)
        gone = self.decide("想起赫泰拉和阿斯帕西娅了", msg_id=100,
                           pool=[p for p in POOL if p["ref"] != "kernel:greek"])
        self.assertEqual(gone.decision, "none")
        self.assertEqual(gone.reason, "replayed_ref_gone")

    def test_ombre_down_keeps_keyword_lane(self):
        result = self.decide("想起赫泰拉和阿斯帕西娅了", ombre=lambda q, n: None)
        self.assertEqual(result.ref, "kernel:greek")
        self.assertEqual(result.detail["ombre"], "unavailable")

    def test_skips_are_logged_not_silent(self):
        self.assertEqual(self.decide("嗯", msg_id=1).reason, "too_short")
        self.assertEqual(self.decide("想起赫泰拉了", msg_id=2, config=ms.SurfaceConfig(enabled=False)).reason,
                         "disabled")
        self.assertEqual([r[1:3] for r in self.log_rows()],
                         [("skipped", "too_short"), ("skipped", "disabled")])

    def test_failure_is_logged_and_returns_nothing(self):
        def broken(_p):
            raise RuntimeError("kernel unreadable")
        with self.assertLogs("hayagarden.memory_surface", level="ERROR"):
            result = ms.decide(user_message_id=7, user_text="想起赫泰拉和阿斯帕西娅了", context_text="",
                               db_path=self.db, config=ms.SurfaceConfig(), pool_loader=broken,
                               ombre=self.ombre, now=NOW)
        self.assertEqual((result.decision, result.reason), ("failed", "error"))
        self.assertFalse(result.surfaced)
        self.assertEqual(self.log_rows(), [(7, "failed", "error", None)])

    def test_kernel_cutover_off_skips(self):
        with mock.patch("tools.memory_kernel_switch.cutover_enabled", return_value=False):
            result = ms.decide(user_message_id=8, user_text="想起赫泰拉和阿斯帕西娅了", context_text="",
                               db_path=self.db, config=ms.SurfaceConfig(), ombre=self.ombre, now=NOW)
        self.assertEqual((result.decision, result.reason), ("skipped", "kernel_cutover_off"))


class TouchTests(unittest.TestCase):
    def test_touch_only_projected_surfaced_buckets(self):
        with mock.patch("tools.memory_kernel_read.touch_buckets") as touch:
            ms.touch(ms.SurfaceResult("surfaced", "gate_passed", ref="kernel:a", block="b", bucket_id="a",
                                      projected=True))
            ms.touch(ms.SurfaceResult("surfaced", "gate_passed", ref="kernel:b", block="b", bucket_id="b",
                                      projected=False))
            ms.touch(ms.SurfaceResult("none", "gate_closed"))
        touch.assert_called_once_with(["a"])


if __name__ == "__main__":
    unittest.main()
