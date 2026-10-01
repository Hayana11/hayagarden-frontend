"""R3C emotion consumer tests for normalized adapter ownership."""

from __future__ import annotations

import unittest
from unittest import mock

import emotion_memories


def record(bucket_id="abc123def456", *, valence=0.2, arousal=0.4, content="一段记忆。"):
    return {
        "id": bucket_id,
        "name": "memory",
        "type": "dynamic",
        "domain": ["恋爱"],
        "tags": ["情绪"],
        "valence": valence,
        "arousal": arousal,
        "importance": 5.0,
        "created": "2026-07-10T10:00:00",
        "last_active": "2026-07-11T10:00:00",
        "content": content,
    }


class EmotionMemoriesTests(unittest.TestCase):
    def test_list_uses_active_dynamic_adapter_records_and_normalizes(self):
        with mock.patch.object(
            emotion_memories.ombre_adapter,
            "list_memory_records",
            return_value=[record()],
        ) as listed:
            items = emotion_memories.list_memory_points(limit=15)
        listed.assert_called_once_with(
            bucket_type="dynamic",
            include_content=True,
            limit=15,
            sort="last_active_desc",
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bucket_id"], "abc123def456")
        self.assertAlmostEqual(items[0]["valence"], -0.6, places=2)
        self.assertEqual(items[0]["scale"], "bipolar")
        self.assertNotIn("path", items[0])

    def test_update_delegates_by_id_then_reads_normalized_record(self):
        updated = record(valence=0.2, arousal=0.55)
        with mock.patch.object(
            emotion_memories.ombre_adapter,
            "update_memory_emotion",
            return_value="trace ok",
        ) as update, mock.patch.object(
            emotion_memories.ombre_adapter,
            "get_memory_record",
            return_value=updated,
        ) as get:
            item = emotion_memories.update_memory_point("abc123def456", -0.6, 0.55)
        update.assert_called_once_with("abc123def456", -0.6, 0.55)
        get.assert_called_once_with("abc123def456")
        self.assertEqual(item["bucket_id"], "abc123def456")
        self.assertAlmostEqual(item["valence"], -0.6, places=2)
        self.assertNotIn("path", item)

    def test_update_rejects_path_shaped_id_without_adapter_call(self):
        with mock.patch.object(emotion_memories.ombre_adapter, "update_memory_emotion") as update:
            with self.assertRaises(ValueError):
                emotion_memories.update_memory_point("/somewhere/memory.md", 0.1, 0.2)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
