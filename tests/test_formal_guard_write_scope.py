"""Candidate and progress writes must happen outside _formal_config_read_guard.

The real guard forces the production memories.db read-only while it is active. These tests
run it for real (against a temporary "production" root) and only replace the provider
chain it would hand out, so a write inside the guard fails exactly as it did in production.
"""
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dayblock_shadow
from chat.background_generation import BackgroundGenerationResult
from tools import daily_memory_dehydrator as daily
from tools import dayblock_scheduled_runner as runner
from tools import daily_profile_updater as profile
from tools import mail_memory_dehydrator as mail
from tools import memory_review as review
from tools.memory_candidates import CandidateSourceTrace

AUTHORITY = SimpleNamespace(provider="p", model_identity="m")


class GuardScopeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = str(root / "memories.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()
        (root / ".env").write_text("")
        self.depth, self.events = 0, []
        real_guard = runner._formal_config_read_guard

        @contextmanager
        def guard(source_db):
            with real_guard(source_db):  # the real read-only enforcement
                router = types.ModuleType("chat.provider_router")
                router.capture_generation_authority = self.capture
                sys.modules["chat.provider_router"] = router  # the real guard restores it on exit
                self.depth += 1
                try:
                    yield None, self.generate_background, None, lambda: "token"
                finally:
                    self.depth -= 1

        for module, name, value in ((runner, "PRODUCTION_ROOT", root), (dayblock_shadow, "ENV_PATH", str(root / ".env")),
                                    (runner, "_formal_config_read_guard", guard)):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reply = {"candidates": []}

    def capture(self):
        self.events.append(("authority", self.depth))
        return AUTHORITY

    def generate_background(self, request, frozen, *, cc_token_getter):
        self.events.append(("model", self.depth))
        return BackgroundGenerationResult(text=json.dumps(self.reply, ensure_ascii=False), provider=frozen.provider,
                                          model_identity=frozen.model_identity, actual_executor="test")

    def track(self, module, name):
        real = getattr(module, name)

        def wrapper(*args, **kwargs):
            self.events.append((name, self.depth))
            return real(*args, **kwargs)

        patcher = mock.patch.object(module, name, wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def candidates(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0]
        finally:
            conn.close()


class DailyGuardScopeTest(GuardScopeTestCase):
    def test_candidate_writes_happen_outside_guard_and_each_model_call_inside(self):
        trace = CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102"), derived_refs=("dayblock:2026-11-07:g",),
            unresolved_refs=(), evidence_text="x",
            raw_messages=({"id": 101, "author": "hayana", "content": "哈娅：" + "猫" * 120},
                          {"id": 102, "author": "assistant", "content": "费奥多尔：" + "星" * 120}))
        block = SimpleNamespace(source_day="2026-11-07", created_at="2026-11-08T00:00:00Z",
                                generation_id="g", chunk_id="c")
        self.reply = {"candidates": [{"content": "哈娅喜欢猫。", "evidence_message_ids": [101]}]}
        self.track(daily, "create_candidate")
        with mock.patch.object(daily, "load_ready_dayblocks", return_value=((block,), ())), \
             mock.patch.object(daily, "trace_ready_dayblock", return_value=trace):
            result = daily.update_day("2026-11-07", chat_db_path=self.db, candidate_db_path=self.db,
                                      dayblock_db_path="unused", chunk_chars=80)
        self.assertEqual(2, result["model_calls"])
        self.assertGreaterEqual(self.candidates(), 1)
        self.assertEqual([("authority", 1), ("model", 1), ("model", 1)],
                         [e for e in self.events if e[0] != "create_candidate"])
        self.assertTrue([e for e in self.events if e[0] == "create_candidate"])
        self.assertTrue(all(depth == 0 for name, depth in self.events if name == "create_candidate"))

    def test_default_vision_annotator_runs_inside_the_guard_per_image(self):
        seen = []

        def annotate(image, mime):
            seen.append(self.depth)
            return {"caption": "雪"}

        with mock.patch("chat.gallery_visual.annotate_image_bytes", annotate):
            annotator = daily._guarded_annotator(self.db)
            self.assertEqual({"caption": "雪"}, annotator(b"x", "image/png"))
            annotator(b"y", "image/png")
        self.assertEqual([1, 1], seen)
        self.assertEqual(0, self.depth)


class MailGuardScopeTest(GuardScopeTestCase):
    def test_candidate_and_progress_writes_happen_outside_guard(self):
        records = [
            {"id": "m1", "threadId": "t1", "direction": "outgoing", "subject": "s", "date": "d1",
             "internalDate": "1000", "text": "我们约好每月通信。", "truncated": False, "attachments": []},
            {"id": "m2", "threadId": "t1", "direction": "incoming", "subject": "s", "date": "d2",
             "internalDate": "2000", "text": "好的，我下个月搬家。", "truncated": False, "attachments": []},
        ]

        def get(path, **params):
            if path == "/readonly/archive/status":
                return {"ready": True, "initialComplete": True}
            if path == "/readonly/conversations":
                return {"conversations": [{"id": "c_x", "name": "阿蒂尔"}]}
            return {"messages": records, "nextPageToken": ""}

        self.reply = {"candidates": [{"content": "阿蒂尔下个月搬家。", "attribution": "penpal",
                                      "evidence_message_ids": ["m2"]}]}
        self.track(mail, "create_candidate")
        self.track(mail, "mark_processed")
        result = mail.run_formal(chat_db_path=self.db, get=get, candidate_db_path=self.db)
        self.assertEqual(("completed", 1), (result["status"], result["candidate_count"]))
        self.assertEqual(1, self.candidates())
        self.assertEqual(["m1", "m2"], sorted(mail.processed_message_ids(self.db)))
        self.assertEqual([("authority", 1), ("model", 1)],
                         [e for e in self.events if e[0] in ("authority", "model")])
        writes = [e for e in self.events if e[0] in ("create_candidate", "mark_processed")]
        self.assertEqual({"create_candidate", "mark_processed"}, {name for name, _ in writes})
        self.assertTrue(all(depth == 0 for _, depth in writes))


class ReviewGuardScopeTest(GuardScopeTestCase):
    def test_review_and_kernel_writes_happen_outside_guard_and_support_check_inside(self):
        from tools.memory_candidates import create_candidate
        from tools.memory_kernel import MemoryKernel

        kernel_db = str(Path(self.temp.name) / "kernel.db")
        MemoryKernel(kernel_db).initialize()

        class Ombre:
            def analyze(self_, text):
                return {"domain": ["生活"], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_name": "n",
                        "importance": 5, "why_remembered": "w"}

            def search(self_, query, limit):
                return []

            def judge_same_event(self_, old, new):
                return {"same_event": False, "confidence": 0.0}

        create_candidate(self.db, candidate_kind="memory", payload={"content": "阿蒂尔下个月搬家。"},
                         source_type="chat_messages", source_refs=["chat_messages:1"], derived_refs=["d:1"],
                         producer="daily_dehydrator", candidate_id="memcand:1")
        resolver = lambda ref: review.Resolved(ref, "chat_messages", "原文", "2026-11-01T09:00:00+08:00", ("Haya",))
        self.reply = {"supported": True, "reason": "ok"}
        for module, name in ((review, "_record"), (review, "commit_candidate")):
            self.track(module, name)
        authority, generate = runner.formal_background_generation(self.db)
        result = review.run_review(self.db, kernel_db, ombre=Ombre(), authority=authority, generate_fn=generate,
                                   resolvers={"chat_messages": resolver})
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        self.assertEqual([("authority", 1), ("model", 1)],
                         [e for e in self.events if e[0] in ("authority", "model")])
        writes = [e for e in self.events if e[0] in ("_record", "commit_candidate")]
        self.assertEqual({"_record", "commit_candidate"}, {name for name, _ in writes})
        self.assertTrue(all(depth == 0 for _, depth in writes))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual("committed", conn.execute("SELECT status FROM memory_candidates").fetchone()[0])
        finally:
            conn.close()


class ProfileGuardScopeTest(GuardScopeTestCase):
    def test_profile_document_writes_happen_outside_guard_and_each_model_call_inside(self):
        trace = CandidateSourceTrace(
            source_refs=("chat_messages:101", "chat_messages:102"), derived_refs=("dayblock:2026-11-07:g",),
            unresolved_refs=(), evidence_text="x",
            raw_messages=({"id": 101, "author": "hayana", "content": "哈娅：" + "猫" * 120},
                          {"id": 102, "author": "assistant", "content": "费奥多尔：" + "星" * 120}))
        block = SimpleNamespace(source_day="2026-11-07", created_at="2026-11-08T00:00:00Z",
                                generation_id="g", chunk_id="c")
        self.reply = {"SELF": {"changed": True, "updated_document": "费奥多尔爱星星。", "evidence_message_ids": [102]},
                      "HAYA": {"changed": True, "updated_document": "哈娅喜欢猫。", "evidence_message_ids": [101]}}
        self.track(profile, "_save")
        with mock.patch.object(profile, "load_ready_dayblocks", return_value=((block,), ())), \
             mock.patch.object(profile, "trace_ready_dayblock", return_value=trace):
            result = profile.update_day("2026-11-07", chat_db_path=self.db, profile_db_path=self.db,
                                        dayblock_db_path="unused", chunk_chars=80)
        self.assertEqual(2, result["chunk_count"])
        self.assertEqual(["SELF", "HAYA"], result["updated_targets"])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0])
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM profile_document_revisions").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual([("authority", 1), ("model", 1), ("model", 1)],
                         [e for e in self.events if e[0] != "_save"])
        saves = [e for e in self.events if e[0] == "_save"]
        self.assertEqual(2, len(saves))
        self.assertTrue(all(depth == 0 for _, depth in saves))


if __name__ == "__main__":
    unittest.main()
