import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.memory_candidates import create_profile_patch_candidate, load_candidate
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

    def test_candidate_survives_reopen_and_keeps_profile_contract(self):
        refs = ["chat_messages:41"]
        ids = []
        for target in ("SELF", "HAYA"):
            ids.append(create_profile_patch_candidate(
                str(self.posts), target=target, source_day="2026-11-11",
                changes=[{"section": "preferences", "proposed_text": "完整、不截断的候选修订",
                          "evidence_refs": refs}],
                source_refs=refs, derived_refs=["dayblock:2026-11-11:generation"],
                prompt_version="test-v1",
            ))
        reopened = [load_candidate(str(self.posts), candidate_id) for candidate_id in ids]
        self.assertEqual(["SELF", "HAYA"], [item["payload"]["target"] for item in reopened])
        self.assertEqual([refs, refs], [item["source_refs"] for item in reopened])
        self.assertEqual(["pending", "pending"], [item["status"] for item in reopened])

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
