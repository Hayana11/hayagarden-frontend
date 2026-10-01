"""R3C relationship-context adapter contract tests."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat.relationship_context import (
    SOURCE_ERROR,
    SOURCE_MISSING,
    SOURCE_OK,
    build_relationship_context,
    rel_context_status,
    should_send_relationship,
)


class RelationshipContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prose_path = str(Path(self.tmp.name) / "no_prose_anchor.md")
        self.prose_patcher = mock.patch(
            "chat.relationship_context.PROSE_ANCHOR_PATH", self.prose_path
        )
        self.prose_patcher.start()
        self.addCleanup(self.prose_patcher.stop)
        self.records = [
            {
                "id": "relationship-a",
                "name": "a",
                "type": "permanent",
                "domain": ["恋爱"],
                "tags": [],
                "valence": 0.7,
                "arousal": 0.4,
                "importance": 8.0,
                "created": "2026-07-18T10:00:00",
                "last_active": "2026-07-18T10:00:00",
                "content": "我们是长期伴侣，彼此信任。",
            },
        ]
        self.adapter = mock.patch(
            "chat.relationship_context.ombre_adapter.list_memory_records",
            side_effect=lambda **kwargs: list(self.records),
        )
        self.adapter_mock = self.adapter.start()
        self.addCleanup(self.adapter.stop)

        self.db_path = str(Path(self.tmp.name) / "memories.db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT,
                resolved INTEGER DEFAULT 0, importance INTEGER DEFAULT 0
            );
            INSERT INTO posts
                (id, type, content, tags, layer, created_at, resolved, importance)
            VALUES
                (1, 'DAILY_SUMMARY', '昨天确认先完成 provider parity。', '', '',
                 '2026-07-18 10:00:00', 0, 8);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def build(self):
        return build_relationship_context(self.get_db, prose_path=self.prose_path)

    def test_adapter_selection_preserves_filters_and_fair_share(self):
        self.records = [
            {
                **self.records[0],
                "id": "long",
                "content": "首文件独白。" + ("很长的前情提要内容" * 40),
            },
            {
                **self.records[0],
                "id": "later",
                "content": "第二桶锚点：彼此信任的约定。",
            },
        ]
        result = self.build()
        self.assertIn("首文件独白", result.text)
        self.assertIn("第二桶锚点", result.text)
        call = self.adapter_mock.call_args
        self.assertEqual(call.kwargs["bucket_type"], "permanent")
        self.assertEqual(call.kwargs["domain"], "恋爱")
        self.assertTrue(call.kwargs["include_content"])

    def test_wikilink_brackets_are_cleaned(self):
        self.records[0]["content"] = "[[哈娅]]和[[费奥多尔]]彼此信任。"
        result = self.build()
        self.assertIn("哈娅和费奥多尔彼此信任", result.text)
        self.assertNotIn("[[", result.text)
        self.assertNotIn("]]", result.text)

    def test_content_hash_fingerprint_detects_same_shape_edit(self):
        first = self.build()
        self.records[0]["content"] = "我们是短期旅伴，彼此客气。"
        second = self.build()
        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertIn("短期旅伴", second.text)

    def test_prose_anchor_remains_authoritative(self):
        Path(self.prose_path).write_text(
            "我们的故事是连贯的一整段散文，写在这里。",
            encoding="utf-8",
        )
        result = self.build()
        self.assertIn("连贯的一整段散文", result.text)
        self.assertNotIn("我们是长期伴侣", result.text)
        self.assertEqual(result.sources["anchor"], SOURCE_OK)
        self.adapter_mock.assert_not_called()

    def test_prose_missing_falls_back_to_adapter(self):
        result = self.build()
        self.assertIn("我们是长期伴侣", result.text)
        self.assertEqual(result.sources["anchor"], SOURCE_OK)

    def test_meta_instruction_sentences_are_filtered(self):
        self.records[0]["content"] = "我们在一起很多天了。回复要更有感情一些。她爱巧克力。"
        result = self.build()
        self.assertIn("她爱巧克力", result.text)
        self.assertNotIn("更有感情", result.text)

    def test_daily_failure_degrades_without_raising(self):
        result = build_relationship_context(lambda: (_ for _ in ()).throw(RuntimeError("db down")), prose_path=self.prose_path)
        self.assertIn("我们是长期伴侣", result.text)
        self.assertEqual(result.sources["daily"], SOURCE_ERROR)
        self.assertEqual(result.sources["anchor"], SOURCE_OK)
        self.assertEqual(rel_context_status(result.text, sent=True, sources=result.sources), "degraded")

    def test_all_sources_failed_marks_empty(self):
        self.adapter_mock.side_effect = RuntimeError("adapter down")
        boom = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        with self.assertLogs("relationship_context", level="WARNING") as logs:
            result = build_relationship_context(boom, prose_path=self.prose_path)
        self.assertEqual(result.text, "")
        self.assertEqual(result.sources, {"anchor": SOURCE_ERROR, "daily": SOURCE_ERROR})
        self.assertEqual(rel_context_status(result.text, sent=False, sources=result.sources), "EMPTY")
        self.assertTrue(any("EMPTY" in line for line in logs.output))

    def test_mood_is_not_in_build_output(self):
        result = self.build()
        self.assertNotIn("当前基调", result.text)
        self.assertIsNone(result.mood_key)
        self.assertNotIn("mood", result.sources)

    def test_state_machine_send_skip_skip_skip_send(self):
        decisions = []
        turns_since = 0
        for i in range(5):
            send = should_send_relationship(
                is_cold=i == 0,
                rel_fp="rel-v2:same",
                last_fp=None if i == 0 else "rel-v2:same",
                turns_since_rel_sent=turns_since,
                user_turn=True,
            )
            decisions.append(send)
            turns_since = 0 if send else turns_since + 1
        self.assertEqual(decisions, [True, False, False, False, True])

    def test_user_turn_false_does_not_force_refresh(self):
        self.assertFalse(should_send_relationship(
            is_cold=False, rel_fp="rel-v2:abc", last_fp="rel-v2:abc",
            turns_since_rel_sent=3, user_turn=False,
        ))
        self.assertTrue(should_send_relationship(
            is_cold=False, rel_fp="rel-v2:abc", last_fp="rel-v2:abc",
            turns_since_rel_sent=3, user_turn=True,
        ))


if __name__ == "__main__":
    unittest.main()
