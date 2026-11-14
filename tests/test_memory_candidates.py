import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.memory_candidates import load_candidate
from tools.memory_write_adapter import write_memory
from tools.memory_kernel import MemoryKernel


class MemoryCandidateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp/hayagarden-agent/scratch")
        self.root = Path(self.temp.name)
        self.posts = self.root / "posts.db"
        self.kernel = self.root / "kernel.db"
        with sqlite3.connect(self.posts) as conn:
            conn.executescript("""
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
                );
                CREATE TABLE moments_active_turn (
                    conversation_id TEXT PRIMARY KEY, turn_key TEXT UNIQUE,
                    user_message_id INTEGER, started_at TEXT
                );
                CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO runtime_config VALUES ('memory.kernel_write', '1');
                INSERT INTO chat_messages VALUES (41, 'hayana', '完整的原始用户消息', '2026-11-14 12:00:00');
                INSERT INTO moments_active_turn VALUES ('hayana-chat', 'turn-live', 41, '2026-11-14 12:00:00');
            """)
        MemoryKernel(self.kernel).initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _memory_write_candidates(self):
        with sqlite3.connect(self.posts) as conn:
            ids = conn.execute(
                "SELECT candidate_id FROM memory_candidates WHERE producer='memory.write'"
            ).fetchall()
        return [load_candidate(str(self.posts), row[0]) for row in ids]

    def test_memory_write_keeps_kernel_write_and_adds_candidate(self):
        from tools import memory_kernel_store

        with patch("tools.memory_kernel_store.default_runtime", return_value=None), patch.dict(
            "os.environ", {"MEMORY_KERNEL_DB": str(self.kernel)}, clear=False,
        ):
            result = write_memory(
                str(self.posts), content="这条明确说要长期记住的内容",
                shadow_surface="api_relay", shadow_turn_id="turn-live",
            )
        candidate, = self._memory_write_candidates()
        self.assertNotIn("candidate_id", result)
        self.assertEqual("kernel", result["store"])
        self.assertEqual(["chat_messages:41"], candidate["source_refs"])
        self.assertEqual("resolved", candidate["provenance_status"])
        self.assertTrue(candidate["explicit_intent"])
        self.assertEqual(100, candidate["priority"])
        self.assertEqual("memory.write", candidate["producer"])
        self.assertIsNotNone(memory_kernel_store.find_head(
            "这条明确说要长期记住的内容", kernel_db=self.kernel,
        ))

    def test_memory_write_returns_before_slow_ombre_projection(self):
        from tools import memory_kernel_store, memory_tool

        calls = []
        written = []
        save_memory = memory_tool.save_memory

        def slow_projection(*args, **kwargs):
            calls.append(True)
            time.sleep(5.2)
            return "PENDING:simulated_timeout"

        def capture_write(*args, **kwargs):
            result = save_memory(*args, **kwargs)
            written.append(result)
            return result

        with patch("tools.memory_kernel_store._project_one", side_effect=slow_projection) as project, \
             patch("tools.memory_kernel_store.default_runtime", return_value=object()), \
             patch.object(memory_tool, "save_memory", side_effect=capture_write), \
             patch.dict("os.environ", {
                 "MEMORY_KERNEL_DB": str(self.kernel),
                 "MEMORY_KERNEL_PROJECTION_DB": str(self.root / "projection.db"),
             }, clear=False):
            started = time.monotonic()
            result = write_memory(
                str(self.posts), content="Kernel 落账后即可确认成功",
                shadow_surface="api_relay", shadow_turn_id="turn-live",
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0)
        self.assertEqual([], calls)
        project.assert_not_called()
        self.assertEqual("CREATED", result["status"])
        self.assertEqual("kernel", result["store"])
        self.assertEqual(1, len(written))
        self.assertEqual("PENDING", written[0].ombre_status)
        state = MemoryKernel(self.kernel).get_state(result["kernel_state_id"])
        self.assertEqual("Kernel 落账后即可确认成功", state.content)
        candidate, = self._memory_write_candidates()
        self.assertEqual("pending", candidate["status"])
        self.assertEqual(["chat_messages:41"], candidate["source_refs"])

    def test_memory_write_retry_keeps_one_candidate_and_kernel_deduplicates(self):
        with patch("tools.memory_kernel_store.default_runtime", return_value=None), patch.dict(
            "os.environ", {"MEMORY_KERNEL_DB": str(self.kernel)}, clear=False,
        ):
            first = write_memory(
                str(self.posts), content="重复提交的长期记忆",
                shadow_surface="api_relay", shadow_turn_id="turn-live",
            )
            second = write_memory(
                str(self.posts), content="重复提交的长期记忆",
                shadow_surface="api_relay", shadow_turn_id="turn-live",
            )
        self.assertEqual({"status", "store", "kernel_state_id", "ombre_bucket_id", "deduplicated"}, set(first))
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["kernel_state_id"], second["kernel_state_id"])
        self.assertEqual(1, len(self._memory_write_candidates()))

    def test_candidate_persistence_failure_does_not_block_kernel_write(self):
        from tools import memory_kernel_store

        with patch("tools.memory_candidates.capture_memory_write_candidate", side_effect=OSError("disk unavailable")), \
             patch("tools.memory_kernel_store.default_runtime", return_value=None), patch.dict(
            "os.environ", {"MEMORY_KERNEL_DB": str(self.kernel)}, clear=False,
        ):
            result = write_memory(
                str(self.posts), content="Kernel 正式写入不依赖候选旁路",
                shadow_surface="api_relay", shadow_turn_id="turn-live",
            )
        self.assertEqual({"status", "store", "kernel_state_id", "ombre_bucket_id", "deduplicated"}, set(result))
        self.assertIsNotNone(memory_kernel_store.find_head(
            "Kernel 正式写入不依赖候选旁路", kernel_db=self.kernel,
        ))

    def test_missing_turn_mapping_is_retained_as_unresolved(self):
        from tools.memory_candidates import capture_memory_write_candidate

        candidate_id = capture_memory_write_candidate(
            str(self.posts), content="保留正文", surface="api_relay", turn_id="unknown-turn",
        )
        candidate = load_candidate(str(self.posts), candidate_id)
        self.assertEqual("chat_turn", candidate["source_type"])
        self.assertEqual(["turn_id:unknown-turn"], candidate["source_refs"])
        self.assertEqual("unresolved", candidate["provenance_status"])


if __name__ == "__main__":
    unittest.main()
