import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chat.background_generation import BackgroundGenerationResult
from tools import mail_memory_dehydrator as mail
from tools.memory_candidates import load_candidate

PENPAL = "mochi@example.com"


def record(mid, thread, direction, text, ms, address=PENPAL, **extra):
    data = {
        "id": mid, "threadId": thread, "direction": direction,
        "contacts": [{"address": address, "name": "mochi"}],
        "subject": "关于冬天", "date": f"day-{ms}", "internalDate": str(ms),
        "text": text, "truncated": False, "attachments": [],
    }
    data.update(extra)
    return data


class MailMemoryDehydratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.archive_path = str(root / "mail-archive.json")
        self.contacts_path = str(root / "mail-contacts.json")
        self.db = str(root / "candidates.db")
        self.authority = SimpleNamespace(provider="api_relay", model_identity="claude-opus-5-5")
        Path(self.contacts_path).write_text(json.dumps({
            "version": 1, "contacts": {PENPAL: {"id": "c_abcdefghijklmnop", "name": "AI-Mochi", "style": ""}},
        }), encoding="utf-8")
        self.messages = {
            "m1": record("m1", "t1", "outgoing", "我们约好每个月第一个周日写一封信。", 1000),
            "m2": record("m2", "t1", "incoming",
                         "好呀，就这么定。我下个月要搬去札幌。\n\nOn Sun, Fedya wrote:\n> 我们约好每个月第一个周日写一封信。",
                         2000),
        }
        self.write_archive()
        self.calls = []
        self.replies = []

    def tearDown(self):
        self.temp.cleanup()

    def write_archive(self, initial=True):
        Path(self.archive_path).write_text(json.dumps({
            "version": 1, "since": "2026-01-01", "initialComplete": initial,
            "messages": self.messages,
        }, ensure_ascii=False), encoding="utf-8")

    def generate(self, request, authority):
        self.calls.append(request)
        payload = self.replies.pop(0) if self.replies else {"candidates": []}
        if isinstance(payload, Exception):
            raise payload
        return BackgroundGenerationResult(
            text=json.dumps(payload, ensure_ascii=False), provider=authority.provider,
            model_identity=authority.model_identity, actual_executor="test",
        )

    def run_once(self, **kwargs):
        return mail.run(
            archive_path=self.archive_path, contacts_path=self.contacts_path,
            candidate_db_path=self.db, authority=self.authority, generate_fn=self.generate, **kwargs,
        )

    def test_archive_not_ready_fails_closed_without_model_call(self):
        self.write_archive(initial=False)
        with self.assertRaises(mail.ArchiveNotReady):
            self.run_once()
        self.assertEqual([], self.calls)
        self.assertFalse(Path(self.db).exists())

    def test_quoted_reply_is_stripped_and_addresses_redacted(self):
        self.messages["m2"]["text"] = "回信给 mochi@example.com 也行。\n> 旧的引用行\n\n在 2026年11月1日，Fedya 写道：\n旧信全文"
        self.write_archive()
        self.run_once()
        prompt = self.calls[0].prompt_text
        self.assertNotIn("mochi@example.com", prompt)
        self.assertIn("回信给 AI-Mochi 也行。", prompt)
        self.assertNotIn("旧的引用行", prompt)
        self.assertNotIn("旧信全文", prompt)
        self.assertIn("[speaker=费奥多尔]", prompt)
        self.assertIn("[speaker=AI-Mochi]", prompt)

    def test_candidates_carry_exact_refs_attribution_and_thread(self):
        self.replies.append({"candidates": [
            {"content": "费奥多尔和 AI-Mochi 约定每月第一个周日写信。", "attribution": "mutual",
             "evidence_message_ids": ["m2", "m1"]},
            {"content": "AI-Mochi 说下个月要搬去札幌。", "attribution": "penpal",
             "evidence_message_ids": ["m2"]},
        ]})
        result = self.run_once()
        self.assertEqual(2, result["candidate_count"])
        saved = load_candidate(self.db, result["threads"][0]["candidates"][0]["candidate_id"])
        self.assertEqual("gmail_message", saved["source_type"])
        self.assertEqual(["gmail_message:m1", "gmail_message:m2"], saved["source_refs"])
        self.assertEqual(["gmail_thread:t1"], saved["derived_refs"])
        self.assertEqual("mutual", saved["payload"]["attribution"])
        self.assertEqual([{"contact_id": "c_abcdefghijklmnop", "name": "AI-Mochi"}],
                         saved["payload"]["counterparts"])
        self.assertEqual("mail_dehydrator", saved["producer"])

    def test_attribution_must_match_cited_directions(self):
        self.replies.append({"candidates": [
            {"content": "费奥多尔喜欢冬天。", "attribution": "fedya", "evidence_message_ids": ["m2"]},
            {"content": "双方约定", "attribution": "mutual", "evidence_message_ids": ["m1"]},
            {"content": "越界", "attribution": "penpal", "evidence_message_ids": ["m9"]},
            {"content": "没有方向", "evidence_message_ids": ["m2"]},
        ]})
        result = self.run_once()
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(4, result["threads"][0]["model_candidate_count"])

    def test_rerun_without_new_mail_makes_no_model_call(self):
        self.run_once()
        self.assertEqual(1, len(self.calls))
        result = self.run_once()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(0, result["pending_thread_count"])

    def test_new_reply_only_reads_new_letter_with_old_as_context(self):
        self.run_once()
        self.messages["m3"] = record("m3", "t1", "outgoing", "札幌很冷，记得带围巾。", 3000)
        self.write_archive()
        self.replies.append({"candidates": [
            {"content": "只凭旧信", "attribution": "penpal", "evidence_message_ids": ["m2"]},
            {"content": "费奥多尔提醒 AI-Mochi 去札幌带围巾。", "attribution": "fedya",
             "evidence_message_ids": ["m3"]},
        ]})
        result = self.run_once()
        prompt = self.calls[1].prompt_text
        self.assertIn("[mail:m2][role=context]", prompt)
        self.assertIn("[mail:m3][role=new]", prompt)
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(["gmail_message:m3"], result["threads"][0]["candidates"][0]["source_refs"])

    def test_model_failure_keeps_thread_pending_for_next_run(self):
        self.replies.append(RuntimeError("relay down"))
        result = self.run_once()
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual(set(), mail.processed_message_ids(self.db))
        self.run_once()
        self.assertEqual({"m1", "m2"}, mail.processed_message_ids(self.db))

    def test_authority_mismatch_aborts_run(self):
        def wrong(request, authority):
            return BackgroundGenerationResult(text='{"candidates":[]}', provider="other",
                                              model_identity="x", actual_executor="test")
        with self.assertRaisesRegex(ValueError, "authority_mismatch"):
            mail.run(archive_path=self.archive_path, contacts_path=self.contacts_path,
                     candidate_db_path=self.db, authority=self.authority, generate_fn=wrong)

    def test_automated_sender_is_skipped_and_marked(self):
        self.messages = {"a1": record("a1", "ta", "incoming", "安全提醒", 500,
                                      address="no-reply@accounts.google.com")}
        self.write_archive()
        result = self.run_once()
        self.assertEqual([], self.calls)
        self.assertEqual("skipped_automated", result["threads"][0]["status"])
        self.assertEqual({"a1"}, mail.processed_message_ids(self.db))

    def test_rerun_is_idempotent_for_same_candidate(self):
        reply = {"candidates": [{"content": "AI-Mochi 要搬去札幌。", "attribution": "penpal",
                                 "evidence_message_ids": ["m2"]}]}
        self.replies.append(reply)
        self.run_once()
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM mail_dehydrator_progress")
        conn.commit(); conn.close()
        self.replies.append(reply)
        self.run_once()
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_max_threads_processes_oldest_first(self):
        self.messages["n1"] = record("n1", "t2", "incoming", "另一条线", 50)
        self.write_archive()
        result = self.run_once(max_threads=1)
        self.assertEqual("t2", result["threads"][0]["thread_id"])
        self.assertEqual(1, result["remaining_thread_count"])


if __name__ == "__main__":
    unittest.main()
