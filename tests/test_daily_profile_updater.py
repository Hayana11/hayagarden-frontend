import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.daily_profile_updater import _chunks, _message_text, update_from_trace
from tools.memory_candidates import CandidateSourceTrace, load_candidate


class DailyProfileUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/var/tmp/hayagarden-agent/scratch")
        self.db = str(Path(self.temp.name) / "candidates.db")
        self.trace = CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102", "chat_messages:103"),
            derived_refs=("dayblock:2026-11-07:generation-abc",),
            unresolved_refs=(),
            raw_messages=(
                {"id": 101, "author": "hayana", "content": "明确的稳定偏好"},
                {"id": 102, "author": "assistant", "content": "明确的长期关系态度"},
                {"id": 103, "author": "assistant", "content": "明确的稳定习惯"},
            ),
            evidence_text="派生摘要不能代替原始聊天",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_chunking_keeps_each_complete_message(self):
        long_message = {"id": 7, "author": "hayana", "content": "x" * 180}
        second = {"id": 8, "author": "assistant", "content": "short"}
        formatted = [_message_text(long_message), _message_text(second)]
        chunks = _chunks(formatted, budget=100)
        self.assertEqual(formatted, [line for chunk in chunks for line in chunk.split("\n")])
        self.assertIn("x" * 180, chunks[0])
        self.assertIn("[msg:8]", chunks[-1])

    def test_one_candidate_per_target_and_repeat_is_idempotent(self):
        replies = [
            {"changes": [{"target": "HAYA", "section": "preferences", "proposed_text": "偏好增补", "evidence_message_ids": [101]}]},
            {"changes": [
                {"target": "HAYA", "section": "ongoing_projects", "proposed_text": "项目增补", "evidence_message_ids": [101, 102]},
                {"target": "SELF", "section": "relationships", "proposed_text": "关系态度增补", "evidence_message_ids": [102]},
            ]},
            {"changes": [{"target": "SELF", "section": "habits", "proposed_text": "习惯增补", "evidence_message_ids": [103]}]},
        ]
        calls = []

        def ask(prompt, **kwargs):
            calls.append(prompt)
            return replies[len(calls) - 1]

        first = update_from_trace(self.db, source_day="2026-11-07", trace=self.trace, llm_ask=ask, chunk_chars=36)
        self.assertEqual(3, len(calls))
        self.assertEqual({"SELF": 2, "HAYA": 2}, first["change_counts"])
        self.assertEqual(2, len(first["candidate_ids"]))
        self.assertTrue(all("[msg:" in prompt for prompt in calls))

        second = update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            llm_ask=lambda prompt, **kwargs: replies[0] if "[msg:101]" in prompt else replies[1] if "[msg:102]" in prompt else replies[2],
            chunk_chars=36,
        )
        self.assertEqual(set(first["candidate_ids"]), set(second["candidate_ids"]))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0])
            rows = conn.execute("SELECT candidate_id FROM memory_candidates").fetchall()
        candidates = [load_candidate(self.db, row[0]) for row in rows]
        self.assertEqual({"pending"}, {item["status"] for item in candidates})
        haya = next(item for item in candidates if item["payload"]["target"] == "HAYA")
        self.assertEqual({"chat_messages:101", "chat_messages:102"}, set(haya["source_refs"]))
        self.assertEqual("dayblock:2026-11-07:generation-abc", haya["derived_refs"][0])
        self.assertEqual(2, len(haya["payload"]["changes"]))


    def test_evidence_requires_a_direct_message_from_the_target_speaker(self):
        cases = (
            ("HAYA", [102], False),
            ("SELF", [101], False),
            ("HAYA", [101, 102], True),
            ("SELF", [102, 101], True),
        )
        for target, evidence_ids, retained in cases:
            with self.subTest(target=target, evidence_ids=evidence_ids):
                result = update_from_trace(
                    self.db, source_day="2026-11-07", trace=self.trace,
                    llm_ask=lambda prompt, **kwargs: {"changes": [{
                        "target": target, "section": "preferences", "proposed_text": "待审增补",
                        "evidence_message_ids": evidence_ids,
                    }]},
                    chunk_chars=100_000,
                )
                self.assertEqual(1 if retained else 0, result["change_counts"][target])
                self.assertEqual(1 if retained else 0, len(result["candidate_ids"]))
                if retained:
                    candidate = load_candidate(self.db, result["candidate_ids"][0])
                    self.assertEqual(set(evidence_ids), {int(ref.rsplit(":", 1)[1]) for ref in candidate["source_refs"]})

    def test_pending_profile_candidate_refreshes_on_same_batch_rerun(self):
        first = update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            llm_ask=lambda prompt, **kwargs: {"changes": [{
                "target": "HAYA", "section": "preferences", "proposed_text": "第一项",
                "evidence_message_ids": [101],
            }]},
            chunk_chars=100_000,
        )
        second = update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            llm_ask=lambda prompt, **kwargs: {"changes": [
                {"target": "HAYA", "section": "preferences", "proposed_text": "第一项", "evidence_message_ids": [101]},
                {"target": "HAYA", "section": "ongoing_projects", "proposed_text": "恢复的第二项", "evidence_message_ids": [101, 102]},
            ]},
            chunk_chars=100_000,
        )
        self.assertEqual(first["candidate_ids"], second["candidate_ids"])
        self.assertEqual(1, len(second["candidate_ids"]))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE candidate_kind='profile_patch' AND producer='daily_profile'"
            ).fetchone()[0])
        candidate_id = second["candidate_ids"][0]
        candidate = load_candidate(self.db, candidate_id)
        self.assertEqual("pending", candidate["status"])
        self.assertEqual(2, len(candidate["payload"]["changes"]))
        self.assertEqual({"chat_messages:101", "chat_messages:102"}, set(candidate["source_refs"]))

        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE memory_candidates SET status='reviewing' WHERE candidate_id=?", (candidate_id,))
        update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            llm_ask=lambda prompt, **kwargs: {"changes": [
                {"target": "HAYA", "section": "preferences", "proposed_text": "第一项", "evidence_message_ids": [101]},
                {"target": "HAYA", "section": "ongoing_projects", "proposed_text": "恢复的第二项", "evidence_message_ids": [101, 102]},
                {"target": "HAYA", "section": "habits", "proposed_text": "不覆盖已离开 pending 的版本", "evidence_message_ids": [101]},
            ]},
            chunk_chars=100_000,
        )
        candidate = load_candidate(self.db, candidate_id)
        self.assertEqual("reviewing", candidate["status"])
        self.assertEqual(2, len(candidate["payload"]["changes"]))

    def test_out_of_dayblock_evidence_is_discarded(self):
        result = update_from_trace(
            self.db, source_day="2026-11-07", trace=self.trace,
            llm_ask=lambda prompt, **kwargs: {"changes": [{
                "target": "HAYA", "section": "preferences", "proposed_text": "不可采纳",
                "evidence_message_ids": [999],
            }]},
        )
        self.assertEqual([], result["candidate_ids"])
        with sqlite3.connect(self.db) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_candidates'"
            ).fetchone()
        self.assertIsNone(table)


if __name__ == "__main__":
    unittest.main()
