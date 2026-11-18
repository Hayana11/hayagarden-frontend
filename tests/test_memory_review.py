import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from chat.background_generation import BackgroundGenerationResult
from tools import memory_kernel_projection as projection
from tools import memory_review as review
from tools import ombre_projection_worker as worker
from tools.memory_candidates import create_candidate
from tools.memory_kernel import MemoryKernel
from tools.memory_kernel_store import remember

ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = {"domain": ["生活"], "valence": 0.6, "arousal": 0.4, "tags": ["猫", "札幌"],
            "suggested_name": "搬去札幌", "importance": 6, "why_remembered": "重要的搬家"}


class FakeOmbre:
    def __init__(self):
        self.hits, self.judge, self.calls = [], lambda old, new: {"same_event": False, "confidence": 0.1}, []

    def analyze(self, text):
        self.calls.append(("analyze", text))
        return dict(ANALYSIS)

    def search(self, query, limit):
        self.calls.append(("search", query))
        return list(self.hits)

    def judge_same_event(self, old, new):
        self.calls.append(("judge", old, new))
        return self.judge(old, new)


class FakeGenerator:
    def __init__(self):
        self.calls, self.supported, self.provider, self.fail = [], True, "p", None

    def __call__(self, request, authority):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        return BackgroundGenerationResult(
            text=json.dumps({"supported": self.supported, "reason": "原文明确说了"}),
            provider=self.provider, model_identity="m", actual_executor="test")


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.db, self.kernel_db = str(base / "memories.db"), str(base / "kernel.db")
        MemoryKernel(self.kernel_db).initialize()
        self.authority = SimpleNamespace(provider="p", model_identity="m")
        self.ombre, self.gen = FakeOmbre(), FakeGenerator()
        self.evidence = {}
        self.counter = 0

    def resolve(self, ref):
        return self.evidence[ref]

    def add(self, content, *, refs=("chat_messages:1",), producer="daily_dehydrator", priority=0,
            attribution=None, provenance="resolved", **payload):
        self.counter += 1
        body = {"content": content, **payload}
        if attribution:
            body["attribution"] = attribution
        for ref in refs:
            kind = ref.split(":")[0]
            self.evidence.setdefault(ref, review.Resolved(
                ref, kind, f"原文 {ref}", "2026-11-01T09:00:00+08:00", ("Haya",)))
        return create_candidate(
            self.db, candidate_kind="memory", payload=body, source_type="chat_messages", source_refs=refs,
            derived_refs=["dayblock:2026-11-01:g1"], producer=producer, priority=priority,
            explicit_intent=producer == "memory.write", provenance_status=provenance,
            candidate_id=f"memcand:{self.counter}")

    def run_review(self, **kwargs):
        resolvers = kwargs.pop("resolvers", None) or {
            kind: self.resolve for kind in ("chat_messages", "media_asset", "gmail_message")}
        return review.run_review(
            self.db, self.kernel_db, ombre=self.ombre, authority=self.authority, generate_fn=self.gen,
            resolvers=resolvers, **kwargs)

    def row(self, candidate_id):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            review.ensure_review_schema(conn)
            found = conn.execute("SELECT * FROM memory_candidate_reviews WHERE candidate_id=?",
                                 (candidate_id,)).fetchone()
            return dict(found) if found else None
        finally:
            conn.close()

    def status(self, candidate_id):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT status FROM memory_candidates WHERE candidate_id=?",
                                (candidate_id,)).fetchone()[0]
        finally:
            conn.close()

    def kernel_count(self, table):
        conn = sqlite3.connect(self.kernel_db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM memory_kernel_{table}").fetchone()[0]
        finally:
            conn.close()

    def seed_head(self, content="我养了一只猫叫布丁。"):
        return remember(content, source="memory_write", kernel_db=self.kernel_db, runtime=None)


class PipelineTest(ReviewTestCase):
    def test_dry_run_writes_no_kernel_rows_and_leaves_candidate_pending(self):
        cid = self.add("费奥多尔下个月搬去札幌。")
        result = self.run_review(dry_run=True)
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        row = self.row(cid)
        self.assertEqual((1, "committed_new", None), (row["dry_run"], row["outcome"], row["committed_state_id"]))
        self.assertEqual("pending", self.status(cid))
        self.assertEqual([0, 0, 0], [self.kernel_count(t) for t in ("evidence", "state", "delta")])

    def test_auto_create_commits_multi_evidence_state_with_deterministic_ids(self):
        refs = ("chat_messages:1", "gmail_message:abc")
        cid = self.add("费奥多尔下个月搬去札幌。", refs=refs, attribution="fedya", counterparts=[{"name": "阿蒂尔"}])
        self.run_review()
        ids = review.candidate_ids(cid)
        kernel = MemoryKernel(self.kernel_db)
        state = kernel.get_state(ids["state"])
        self.assertEqual((ids["lineage"], "asserted_by_companion", "费奥多尔下个月搬去札幌。"),
                         (state.lineage_id, state.epistemic_status, state.content))
        evidence = kernel.evidence_for_state(ids["state"])
        self.assertEqual(set(refs), {e.source_ref for e in evidence})
        self.assertEqual({"chat_messages", "gmail_message"}, {e.source_type for e in evidence})
        self.assertTrue(all(e.evidence_id.startswith(ids["evidence_prefix"]) and e.origin_kind == "source"
                            for e in evidence))
        self.assertIn("阿蒂尔", evidence[0].participants)
        delta = kernel.get_delta(ids["delta"])
        self.assertEqual(("accepted_by_review", None, state.evidence_refs),
                         (delta.review_status, delta.before_ref, delta.trigger_evidence_refs))
        value = state.structured_value
        self.assertEqual(("搬去札幌", ["生活"], ["猫", "札幌"], 6, 0.6, 0.4),
                         (value["title"], value["domain"], value["tags"], value["importance"],
                          value["valence"], value["arousal"]))
        row = self.row(cid)
        self.assertEqual(("accepted", "policy", ids["state"], 0), (
            row["decision"], row["decision_by"], row["committed_state_id"], row["dry_run"]))
        self.assertEqual("committed", self.status(cid))
        self.assertEqual(json.loads(row["analysis_json"])["why_remembered"], "重要的搬家")

    def test_kernel_commit_is_one_transaction(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:1", "chat_messages:2"))
        real_insert = MemoryKernel._insert

        def failing(conn, record):
            if type(record).__name__ == "Delta":
                raise RuntimeError("disk full")
            return real_insert(conn, record)

        with mock.patch.object(MemoryKernel, "_insert", staticmethod(failing)):
            result = self.run_review()
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual([0, 0, 0], [self.kernel_count(t) for t in ("evidence", "state", "delta")])
        self.assertEqual(("error", "pending"), (self.row(cid)["outcome"], self.status(cid)))

    def test_crash_after_kernel_commit_is_recovered_without_duplicate_or_model_call(self):
        cid = self.add("费奥多尔下个月搬去札幌。")
        candidate = {"candidate_id": cid, "producer": "daily_dehydrator", "derived_refs": [],
                     "payload": {"content": "费奥多尔下个月搬去札幌。"}}
        review.commit_candidate(MemoryKernel(self.kernel_db), candidate, (self.evidence["chat_messages:1"],),
                                analysis=ANALYSIS, support={"reason": "x"}, now="2026-11-02T00:00:00+00:00")
        self.assertIsNone(self.row(cid))
        result = self.run_review()
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        self.assertEqual([], self.gen.calls)
        self.assertEqual((1, "recovered_existing_state"),
                         (self.kernel_count("state"), self.row(cid)["decision_reason"]))
        self.assertEqual("committed", self.status(cid))
        # the same commit again is a no-op
        review.commit_candidate(MemoryKernel(self.kernel_db), candidate, (self.evidence["chat_messages:1"],),
                                analysis=ANALYSIS, support={}, now="2026-11-03T00:00:00+00:00")
        self.assertEqual(1, self.kernel_count("state"))

    def test_memory_write_already_in_kernel_is_linked_not_recommitted(self):
        head = self.seed_head()
        cid = self.add("我养了一只猫叫布丁。", producer="memory.write", priority=100)
        self.run_review()
        row = self.row(cid)
        self.assertEqual(("already_committed", head["lineage_id"], "policy"),
                         (row["outcome"], row["matched_lineage_id"], row["decision_by"]))
        self.assertEqual(("duplicate", []), (self.status(cid), self.gen.calls))
        self.assertEqual(1, self.kernel_count("state"))

    def test_other_producers_exact_duplicate(self):
        self.seed_head()
        cid = self.add("我养了一只猫，叫布丁")
        self.run_review()
        self.assertEqual("exact_duplicate", self.row(cid)["outcome"])

    def test_unsupported_claim_is_rejected_by_policy(self):
        self.gen.supported = False
        cid = self.add("费奥多尔下个月搬去札幌。")
        self.run_review()
        row = self.row(cid)
        self.assertEqual(("rejected_unsupported", "rejected", "policy"),
                         (row["outcome"], row["decision"], row["decision_by"]))
        self.assertEqual(("rejected", 0), (self.status(cid), self.kernel_count("state")))
        self.assertEqual([], [c for c in self.ombre.calls if c[0] == "analyze"])

    def test_unresolvable_evidence_needs_review_without_model_call(self):
        missing = self.add("甲", refs=("chat_messages:404",))
        gone = self.add("乙", refs=("gmail_message:x",))
        unresolved = self.add("丙", refs=(), provenance="unresolved")
        resolver = lambda ref: None  # noqa: E731

        self.run_review(resolvers={"chat_messages": resolver, "gmail_message": resolver})
        reasons = {c: self.row(c)["decision_reason"] for c in (missing, gone, unresolved)}
        self.assertIn("not_found", reasons[missing])
        self.assertTrue(reasons[gone].startswith("evidence_unresolvable:gmail_message:x=not_found"))
        self.assertEqual("provenance_unresolved", reasons[unresolved])
        self.assertTrue(all(self.row(c)["outcome"] == "needs_review" and self.row(c)["decision_by"] is None
                            for c in reasons))
        self.assertEqual(([], 0), (self.gen.calls, self.kernel_count("state")))

    def test_ombre_failures_are_errors_that_stay_pending_and_retry_next_run(self):
        head = self.seed_head()

        def boom(*_):
            raise RuntimeError("down")

        cases = {
            "analyze_raises": lambda o: setattr(o, "analyze", boom),
            "analyze_shape": lambda o: setattr(o, "analyze", lambda text: ["x"]),
            "judge_raises": lambda o: setattr(o, "judge", boom),
            "judge_shape": lambda o: setattr(o, "judge", lambda a, b: {"confidence": "high"}),
            "search_raises": lambda o: setattr(o, "search", boom),
        }
        before = self.kernel_count("state")
        for name, break_it in cases.items():
            self.ombre = FakeOmbre()
            self.ombre.hits = [{"bucket_id": head["bucket_id"]}]
            break_it(self.ombre)
            cid = self.add(f"今天去了海边散步 {name}。")
            result = self.run_review()
            row = self.row(cid)
            self.assertEqual(("error", "pending"), (row["outcome"], self.status(cid)), name)
            self.assertIn("ombre_failed:", row["error"], name)
            self.assertEqual(("failed", 1), (result["status"], result["ombre_failure_count"]), name)
            self.assertEqual(before, self.kernel_count("state"))
            self.ombre = FakeOmbre()  # Ombre recovers: the retry succeeds
            self.ombre.hits = [{"bucket_id": head["bucket_id"]}]
            self.run_review(dry_run=True)
            self.assertEqual("committed_new", self.row(cid)["outcome"], name)
            self.run_review(dry_run=True)
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE memory_candidates SET status='rejected' WHERE candidate_id=?", (cid,))
            conn.commit()
            conn.close()

    def test_partial_ombre_failure_is_partial_not_failed_and_counted(self):
        good, bad = self.add("甲去了海边。"), self.add("乙去了山里。")
        self.ombre.analyze = lambda text: ANALYSIS if "甲" in text else (_ for _ in ()).throw(RuntimeError("x"))
        result = self.run_review()
        self.assertEqual(("partial_failure", 1), (result["status"], result["ombre_failure_count"]))
        self.assertEqual(("committed", "pending"), (self.status(good), self.status(bad)))
        self.assertEqual(1, review.review_health(self.db)["last_run"]["failure_count"])

    def test_preflight_abort_leaves_candidates_pending_and_writes_no_review_rows(self):
        cid = self.add("今天去了海边散步。")
        runtime = OmbreAdapterTest.Runtime(["analyze", "search", "judge_same_event"],
                                           dehydrator={"ok": False, "methods": [], "error": "ImportError: x"})
        ombre = review.RuntimeReviewOmbre(runtime)
        for dry in (True, False):
            with self.assertRaisesRegex(review.ReviewAbort, "ombre_dehydrator_unavailable"):
                self.run_review(preflight=ombre.preflight, dry_run=dry)
            self.assertIsNone(self.row(cid))
            self.assertEqual("pending", self.status(cid))
            self.assertEqual("aborted", review.review_health(self.db)["last_run"]["status"])
        self.assertEqual([], self.ombre.calls)

    def test_main_aborts_non_zero_when_dehydrator_is_not_ok(self):
        cid = self.add("今天去了海边散步。")
        runtime = OmbreAdapterTest.Runtime(["analyze", "search", "judge_same_event"],
                                           dehydrator={"ok": False, "methods": [], "error": "x"})
        with mock.patch.object(projection.OmbreRuntime, "from_env", return_value=runtime), \
             mock.patch("tools.dayblock_scheduled_runner.formal_background_generation",
                        return_value=(self.authority, self.gen)):
            code = review.main(["--dry-run", "--db", self.db, "--kernel-db", self.kernel_db])
        self.assertEqual((1, "pending", None), (code, self.status(cid), self.row(cid)))

    def test_same_event_bands(self):
        head = self.seed_head()
        self.ombre.hits = [{"bucket_id": head["bucket_id"]}, {"bucket_id": "foreign-bucket"}]
        cases = [
            ({"same_event": True, "confidence": 0.9}, "needs_review", "revise"),
            ({"same_event": True, "confidence": 0.9, "duplicate": True}, "needs_review", "revise"),
            ({"same_event": True, "confidence": 0.85}, "needs_review", "revise"),
            ({"same_event": True, "confidence": 0.7}, "needs_review", "uncertain"),
            ({"same_event": True, "confidence": 0.6}, "needs_review", "uncertain"),
            ({"same_event": True, "confidence": 0.59}, "committed_new", "create"),
            ({"same_event": False, "confidence": 0.95}, "committed_new", "create"),
        ]
        for index, (judged, outcome, action) in enumerate(cases):
            self.ombre.judge = lambda old, new, judged=judged: judged
            cid = self.add(f"布丁最近学会了开门 {index}。")
            self.run_review()
            row = self.row(cid)
            self.assertEqual((outcome, action), (row["outcome"], row["proposed_action"]), judged)
            if outcome == "needs_review":
                self.assertEqual(head["lineage_id"], row["matched_lineage_id"])
        self.assertEqual(3, self.kernel_count("state"))  # the seed plus the two auto-created
        self.assertIn(("judge", "我养了一只猫叫布丁。", "布丁最近学会了开门 0。"), self.ombre.calls)

    def test_human_rejected_candidates_suppress_exact_and_same_event(self):
        old = self.add("费奥多尔不喜欢茶。")
        conn = sqlite3.connect(self.db)
        review.ensure_review_schema(conn)
        conn.execute("INSERT INTO memory_candidate_reviews (candidate_id, outcome, decision, decision_by, "
                     "dry_run, created_at, decided_at) VALUES (?, 'needs_review', 'rejected', 'haya', 0, 'x', "
                     "'2026-11-01T00:00:00+00:00')", (old,))
        conn.execute("UPDATE memory_candidates SET status='rejected' WHERE candidate_id=?", (old,))
        conn.commit()
        conn.close()
        exact = self.add("费奥多尔 不喜欢茶")
        self.run_review()
        self.assertEqual(("suppressed_rejected", "policy"), (self.row(exact)["outcome"], self.row(exact)["decision_by"]))
        self.assertEqual([], self.gen.calls)

        self.ombre.judge = lambda old_text, new_text: {"same_event": True, "confidence": 0.9}
        fuzzy = self.add("费奥多尔说他其实不爱喝茶。")
        self.run_review()
        self.assertEqual("suppressed_rejected", self.row(fuzzy)["outcome"])
        self.assertIn(("judge", "费奥多尔不喜欢茶。", "费奥多尔说他其实不爱喝茶。"), self.ombre.calls)
        self.assertEqual(0, self.kernel_count("state"))

    def test_policy_rejections_are_not_comparison_targets(self):
        self.gen.supported = False
        first = self.add("甲乙丙。")
        self.run_review()
        self.assertEqual("rejected_unsupported", self.row(first)["outcome"])
        self.gen.supported = True
        self.ombre.judge = lambda a, b: {"same_event": True, "confidence": 0.99}
        second = self.add("丁戊己。")
        self.run_review()
        self.assertEqual("committed_new", self.row(second)["outcome"])

    def test_penpal_claims_about_fedya_or_haya_never_auto_commit(self):
        about = self.add("阿蒂尔认为费奥多尔很孤独。", attribution="penpal")
        also = self.add("阿蒂尔说哈娅是个温柔的人。", attribution="penpal")
        other = self.add("阿蒂尔说他最近在整理旧书。", attribution="penpal")
        mutual = self.add("费奥多尔和阿蒂尔约好每月通信。", attribution="mutual")
        self.run_review()
        self.assertEqual(["needs_review", "needs_review", "committed_new", "committed_new"],
                         [self.row(c)["outcome"] for c in (about, also, other, mutual)])
        self.assertEqual("penpal_claim_about_fedya_or_haya", self.row(about)["decision_reason"])
        kernel = MemoryKernel(self.kernel_db)
        self.assertEqual("asserted_by_correspondent", kernel.get_state(review.candidate_ids(other)["state"]).epistemic_status)
        self.assertEqual("mutually_confirmed", kernel.get_state(review.candidate_ids(mutual)["state"]).epistemic_status)

    def test_model_authority_mismatch_aborts_the_run(self):
        first = self.add("甲。")
        self.gen.provider = "other"
        with self.assertRaisesRegex(review.ReviewAbort, "authority_mismatch"):
            self.run_review()
        self.assertEqual(("pending", None), (self.status(first), self.row(first)))
        conn = sqlite3.connect(self.db)
        self.assertEqual("aborted", conn.execute("SELECT status FROM memory_review_runs").fetchone()[0])
        conn.close()

    def test_highest_priority_then_oldest_first_and_limit(self):
        old = self.add("旧的。")
        newer = self.add("新的。")
        urgent = self.add("急的。", priority=100)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memory_candidates SET created_at='2026-11-01T00:00:00' WHERE candidate_id=?", (old,))
        conn.execute("UPDATE memory_candidates SET created_at='2026-11-02T00:00:00' WHERE candidate_id=?", (newer,))
        conn.commit()
        conn.close()
        result = self.run_review(limit=2)
        self.assertEqual(({urgent, old}, 1), ({c for c in (old, newer, urgent) if self.row(c)}, result["remaining"]))
        self.assertEqual("pending", self.status(newer))

    def test_failed_candidate_is_retried_and_dry_rows_are_replaced(self):
        cid = self.add("费奥多尔下个月搬去札幌。")
        self.gen.fail = ConnectionError("relay down")
        result = self.run_review()
        self.assertEqual(("partial_failure", 1), (result["status"], result["failure_count"]))
        self.assertEqual(("error", "pending"), (self.row(cid)["outcome"], self.status(cid)))
        self.gen.fail = None
        self.run_review(dry_run=True)
        self.assertEqual((1, "committed_new"), (self.row(cid)["dry_run"], self.row(cid)["outcome"]))
        self.run_review()
        self.assertEqual((0, "committed"), (self.row(cid)["dry_run"], self.status(cid)))
        self.run_review(dry_run=True)  # a decided row is never rewritten by a later dry run
        self.assertEqual(0, self.row(cid)["dry_run"])

    def test_row_decided_before_status_update_is_finalised_without_rereview(self):
        cid = self.add("费奥多尔下个月搬去札幌。")
        self.run_review()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memory_candidates SET status='pending'")
        conn.commit()
        conn.close()
        calls = len(self.gen.calls)
        self.run_review()
        self.assertEqual((calls, "committed"), (len(self.gen.calls), self.status(cid)))

    def test_media_link_hook_receives_media_refs_after_commit(self):
        self.add("费奥多尔分享了一张札幌的雪景。", refs=("chat_messages:1", "media_asset:sha256-ab"))
        links = []
        self.run_review(media_link=lambda state_id, refs: links.append((state_id, refs)))
        self.assertEqual([(review.candidate_ids("memcand:1")["state"], ("media_asset:sha256-ab",))], links)

    def test_health_records_runs_and_flags_unfinished_ones(self):
        self.add("费奥多尔下个月搬去札幌。")
        self.run_review(dry_run=True)
        self.run_review()
        health = review.review_health(self.db)
        self.assertEqual(("completed", 0), (health["last_run"]["status"], health["last_run"]["dry_run"]))
        self.assertEqual(health["last_run"]["run_id"], health["last_non_dry_success"]["run_id"])
        self.assertEqual(json.loads(health["last_run"]["counts_json"])["committed_new"], 1)
        self.assertEqual([], health["unfinished_runs"])
        stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat(timespec="seconds")
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO memory_review_runs (started_at, dry_run, limit_n, status) VALUES (?,0,50,'running')",
                     (stale,))
        conn.commit()
        conn.close()
        self.assertEqual(1, len(review.review_health(self.db)["unfinished_runs"]))


class ResolverTest(ReviewTestCase):
    def test_chat_message_resolver(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT)")
        conn.execute("INSERT INTO chat_messages VALUES (7, 'hayana', '我下个月搬家', '2026-11-01 09:00:00')")
        conn.commit()
        conn.close()
        item = review.resolve_chat_message("chat_messages:7", db_path=self.db)
        self.assertEqual(("[hayana] 我下个月搬家", "2026-11-01T09:00:00+08:00"), (item.text, item.occurred_at))
        self.assertIsNone(review.resolve_chat_message("chat_messages:8", db_path=self.db))

    def test_media_asset_resolver_requires_row_and_file(self):
        from tools import media_assets
        root = Path(self.temp.name) / "media"
        media_db = str(root / "gallery.db")
        root.mkdir()
        asset = media_assets.save_media_asset(b"\x89PNG", "image/png", {"caption": "雪景", "ocr_text": "札幌"},
                                              db_path=media_db, data_root=root)
        item = review.resolve_media_asset("media_asset:" + asset, media_db=media_db, media_root=str(root))
        self.assertEqual(("雪景\n札幌", "media_asset"), (item.text, item.source_type))
        self.assertTrue(item.content_ref.startswith("gallery/evidence/"))
        self.assertIsNone(review.resolve_media_asset("media_asset:nope", media_db=media_db, media_root=str(root)))
        (root / item.content_ref).unlink()
        self.assertIsNone(review.resolve_media_asset("media_asset:" + asset, media_db=media_db, media_root=str(root)))

    def test_gmail_resolver_maps_readonly_answers(self):
        message = {"subject": "札幌", "text": "我下个月搬去札幌。", "date": "Sun, 08 Nov 2026 10:00:00 +0800"}
        item = review.resolve_gmail_message("gmail_message:abc", reader=lambda path: message)
        self.assertEqual(("subject: 札幌\n我下个月搬去札幌。", "2026-11-08T10:00:00+08:00"), (item.text, item.occurred_at))

        def http_error(code):
            def reader(path):
                raise urllib.error.HTTPError(path, code, "x", None, None)
            return reader

        self.assertIsNone(review.resolve_gmail_message("gmail_message:abc", reader=http_error(404)))
        with self.assertRaises(review.EvidenceUnavailable):
            review.resolve_gmail_message("gmail_message:abc", reader=http_error(502))


class SpeakerAndHayaTest(ReviewTestCase):
    def test_support_prompt_labels_chat_and_mail_speakers(self):
        self.evidence["chat_messages:1"] = review.Resolved(
            "chat_messages:1", "chat_messages", "[hayana] 我想去札幌", None, ("hayana",), speaker=review.chat_speaker("hayana"))
        self.evidence["chat_messages:2"] = review.Resolved(
            "chat_messages:2", "chat_messages", "[assistant] 好", None, ("assistant",), speaker=review.chat_speaker("assistant"))
        self.evidence["chat_messages:3"] = review.Resolved(
            "chat_messages:3", "chat_messages", "[bot9] x", None, ("bot9",), speaker=review.chat_speaker("bot9"))
        self.evidence["gmail_message:o"] = review.Resolved("gmail_message:o", "gmail_message", "subject: a", None, speaker="费奥多尔")
        self.evidence["gmail_message:i"] = review.Resolved("gmail_message:i", "gmail_message", "subject: b", None, speaker=review.INCOMING_UNNAMED)
        self.evidence["media_asset:m"] = review.Resolved("media_asset:m", "media_asset", "猫", None)
        refs = ("chat_messages:1", "chat_messages:2", "chat_messages:3", "gmail_message:o", "gmail_message:i", "media_asset:m")
        self.add("哈娅想去札幌。", refs=refs, counterparts=[{"name": "哈娅"}])
        self.run_review(dry_run=True)
        request = self.gen.calls[0]
        self.assertIn("assistant 的发言是费奥多尔说的", request.system_text)
        self.assertIn("outgoing 是费奥多尔写的", request.system_text)
        prompt = request.prompt_text
        for expected in ("[chat_messages:1][发言人：哈娅]", "[chat_messages:2][发言人：费奥多尔]",
                         "[chat_messages:3][发言人：bot9]", "[gmail_message:o][发言人：费奥多尔]",
                         "[gmail_message:i][发言人：哈娅]",
                         "[media_asset:m][机器标注，不是任何人说的话]"):
            self.assertIn(expected, prompt)

    def test_chat_resolver_sets_speaker(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT)")
        conn.executemany("INSERT INTO chat_messages VALUES (?,?,?,?)",
                         [(1, "hayana", "a", "2026-11-01 09:00:00"), (2, "assistant", "b", "2026-11-01 09:01:00")])
        conn.commit()
        conn.close()
        self.assertEqual(["哈娅", "费奥多尔"], [review.resolve_chat_message(f"chat_messages:{i}", db_path=self.db).speaker for i in (1, 2)])

    def test_gmail_resolver_sets_speaker_from_direction(self):
        out = review.resolve_gmail_message("gmail_message:a", reader=lambda path: {"direction": "outgoing", "text": "x"})
        inc = review.resolve_gmail_message("gmail_message:a", reader=lambda path: {"direction": "incoming", "text": "x"})
        self.assertEqual(("费奥多尔", review.INCOMING_UNNAMED), (out.speaker, inc.speaker))

    def test_gmail_resolver_reads_production_from_list_shape(self):
        # Production /readonly/messages/<id>: no direction, from = [{"name": ...}].
        fedya = review.resolve_gmail_message("gmail_message:o", reader=lambda path: {
            "from": [{"name": "费奥多尔"}], "to": [{"name": "阿蒂尔"}], "text": "x"})
        haya = review.resolve_gmail_message("gmail_message:h", reader=lambda path: {
            "from": [{"name": "Hayana（未备注 · AB12）"}], "to": [{"name": "费奥多尔"}], "text": "y"})
        self.assertEqual("费奥多尔", fedya.speaker)
        self.assertEqual(review.INCOMING_UNNAMED, haya.speaker)  # raw display name is not trusted
        self.assertEqual("发言人：费奥多尔", review._speaker_label(fedya, ("阿蒂尔",)))
        self.assertEqual("发言人：哈娅", review._speaker_label(haya, ("哈娅",)))
        self.assertEqual("发言人：阿蒂尔", review._speaker_label(haya, ("阿蒂尔",)))
        self.assertEqual(f"发言人：{review.INCOMING_UNNAMED}（本线程对方：阿蒂尔、皮若尼耶）",
                         review._speaker_label(haya, ("阿蒂尔", "皮若尼耶")))

    def test_haya_mail_in_production_shape_reaches_support_prompt_as_haya(self):
        self.evidence["gmail_message:h"] = review.resolve_gmail_message(
            "gmail_message:h", reader=lambda path: {"from": [{"name": "Hayana（未备注 · AB12）"}],
                                                     "to": [{"name": "费奥多尔"}], "text": "我想你了"})
        self.add("哈娅在信里说她想费奥多尔。", refs=("gmail_message:h",), attribution="haya",
                 counterparts=[{"contact_id": "c_x", "name": "哈娅"}])
        self.run_review(dry_run=True)
        prompt = self.gen.calls[0].prompt_text
        self.assertIn("[gmail_message:h][发言人：哈娅]", prompt)
        self.assertNotIn("未备注", prompt.split("[gmail_message:h]")[1].split("]")[0])

    def test_haya_attribution_skips_penpal_guard_and_maps_epistemic_status(self):
        haya = self.add("哈娅说费奥多尔是她最信任的人。", attribution="haya")
        penpal = self.add("阿蒂尔说哈娅是个温柔的人。", attribution="penpal")
        self.run_review()
        self.assertEqual(("committed_new", "needs_review"), (self.row(haya)["outcome"], self.row(penpal)["outcome"]))
        state = MemoryKernel(self.kernel_db).get_state(review.candidate_ids(haya)["state"])
        self.assertEqual("derived_from_evidence", state.epistemic_status)
        self.assertEqual("haya", state.structured_value["review"]["attribution"])
        self.assertIn("haya 表示", self.gen.calls[0].system_text)


class TransientEvidenceTest(ReviewTestCase):
    def flaky(self, error):
        state = {"down": True}

        def resolver(ref):
            if state["down"]:
                raise error
            return self.evidence[ref]
        return resolver, state

    def assert_transient(self, error, expect):
        cid = self.add("甲。", refs=("gmail_message:g1",))
        resolver, state = self.flaky(error)
        result = self.run_review(resolvers={"gmail_message": resolver})
        self.assertEqual(1, result["failure_count"])
        self.assertEqual("partial_failure", result["status"])
        self.assertEqual({}, result["outcomes"])
        row = self.row(cid)
        self.assertEqual(("error", 0), (row["outcome"], row["error_count"]))
        self.assertIn("evidence_transient:gmail_message:g1=" + expect, row["error"])
        self.assertEqual("pending", self.status(cid))
        self.assertEqual([], self.gen.calls)
        state["down"] = False
        again = self.run_review(resolvers={"gmail_message": resolver})
        self.assertEqual({"committed_new": 1}, again["outcomes"])
        self.assertEqual("committed", self.status(cid))

    def test_gmail_502_is_error_and_retried(self):
        self.assert_transient(review.EvidenceTransient("gmail_http_502"), "gmail_http_502")

    def test_gmail_429_is_error_and_retried(self):
        self.assert_transient(review.EvidenceTransient("gmail_http_429"), "gmail_http_429")

    def test_oserror_from_resolver_is_error_and_retried(self):
        self.assert_transient(OSError("timed out"), "resolver_error:OSError")

    def test_real_gmail_resolver_classifies_failures(self):
        def reader(exc):
            def get(path):
                raise exc
            return get
        for exc, code in ((urllib.error.HTTPError("p", 502, "x", None, None), "gmail_http_502"),
                          (urllib.error.HTTPError("p", 429, "x", None, None), "gmail_http_429"),
                          (urllib.error.URLError("down"), "gmail_reader_unavailable"),
                          (TimeoutError("t"), "gmail_reader_unavailable")):
            with self.assertRaises(review.EvidenceTransient) as caught:
                review.resolve_gmail_message("gmail_message:abc", reader=reader(exc))
            self.assertEqual(code, str(caught.exception))

    def test_not_found_stays_needs_review(self):
        cid = self.add("甲。", refs=("gmail_message:g1",))
        result = self.run_review(resolvers={"gmail_message": lambda ref: None})
        self.assertEqual({"needs_review": 1}, result["outcomes"])
        row = self.row(cid)
        self.assertEqual(("needs_review", None), (row["outcome"], row["decision_by"]))
        self.assertEqual("evidence_unresolvable:gmail_message:g1=not_found", row["decision_reason"])
        self.assertEqual("needs_review", self.status(cid))

    def test_mixed_refs_one_transient_is_error(self):
        cid = self.add("甲。", refs=("chat_messages:1", "gmail_message:g1"))

        def down(ref):
            raise review.EvidenceTransient("gmail_http_502")

        result = self.run_review(resolvers={"chat_messages": self.resolve, "gmail_message": down})
        self.assertEqual(1, result["failure_count"])
        self.assertEqual(("error", 0, "pending"), (self.row(cid)["outcome"], self.row(cid)["error_count"], self.status(cid)))

    def test_transient_beats_not_found_in_same_candidate(self):
        cid = self.add("甲。", refs=("chat_messages:404", "gmail_message:g1"))

        def down(ref):
            raise review.EvidenceTransient("gmail_http_503")

        self.run_review(resolvers={"chat_messages": lambda ref: None, "gmail_message": down})
        self.assertEqual(("error", "pending"), (self.row(cid)["outcome"], self.status(cid)))


class RequeueTransientEvidenceTest(ReviewTestCase):
    G1, G2 = "1a1c" + "0" * 12, "2b2d" + "0" * 12

    def needs_review(self, content, reason, *, by=None, status="needs_review", dry_run=0, outcome="needs_review"):
        cid = self.add(content, refs=("gmail_message:" + self.G1,))
        conn = sqlite3.connect(self.db)
        review.ensure_review_schema(conn)
        conn.execute("INSERT INTO memory_candidate_reviews (candidate_id, outcome, decision_by, decision_reason, "
                     "dry_run, created_at) VALUES (?,?,?,?,?, 'x')", (cid, outcome, by, reason, dry_run))
        conn.execute("UPDATE memory_candidates SET status=? WHERE candidate_id=?", (status, cid))
        conn.commit()
        conn.close()
        return cid

    def test_requeues_only_transient_only_rows(self):
        g1, g2 = "gmail_message:" + self.G1, "gmail_message:" + self.G2
        hit1 = self.needs_review("甲。", f"evidence_unresolvable:{g1}=gmail_http_502; {g2}=gmail_http_502")
        hit2 = self.needs_review("乙。", f"evidence_unresolvable:{g1}=gmail_http_429")
        hit3 = self.needs_review("丙。", f"evidence_unresolvable:{g1}=gmail_reader_unavailable; {g2}=gmail_http_503")
        not_found = self.needs_review("丁。", f"evidence_unresolvable:{g1}=gmail_http_502; {g2}=not_found")
        http4 = self.needs_review("戊。", f"evidence_unresolvable:{g1}=gmail_http_403")
        penpal = self.needs_review("己。", "penpal_claim_about_fedya_or_haya")
        human = self.needs_review("庚。", f"evidence_unresolvable:{g1}=gmail_http_502", by="haya")
        committed = self.needs_review("辛。", "committed_new", status="committed", outcome="committed_new")
        dry = self.needs_review("壬。", f"evidence_unresolvable:{g1}=gmail_http_502", status="pending", dry_run=1)
        self.assertEqual(3, review.requeue_transient_evidence(self.db))
        for cid in (hit1, hit2, hit3):
            self.assertEqual(("pending", "requeued", None), (
                self.status(cid), self.row(cid)["outcome"], self.row(cid)["decision_by"]))
            self.assertTrue(self.row(cid)["decision_reason"].startswith("requeued_from_transient_evidence: evidence_unresolvable:"))
        for cid, status in ((not_found, "needs_review"), (http4, "needs_review"), (penpal, "needs_review"),
                            (human, "needs_review"), (committed, "committed"), (dry, "pending")):
            self.assertEqual(status, self.status(cid))
            self.assertNotEqual("requeued", self.row(cid)["outcome"])
        self.assertEqual("haya", self.row(human)["decision_by"])
        self.assertEqual("committed_new", self.row(committed)["outcome"])
        self.assertEqual(0, review.requeue_transient_evidence(self.db))

    def test_requeued_candidate_is_reviewed_and_committed_next_run(self):
        cid = self.needs_review("甲。", f"evidence_unresolvable:gmail_message:{self.G1}=gmail_http_502")
        self.assertEqual(1, review.requeue_transient_evidence(self.db))
        result = self.run_review()
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        self.assertEqual("committed", self.status(cid))

    def test_cli_flag_exits_before_ombre(self):
        self.needs_review("甲。", f"evidence_unresolvable:gmail_message:{self.G1}=gmail_http_502")
        with mock.patch("builtins.print") as shown, \
                mock.patch.object(projection.OmbreRuntime, "from_env", side_effect=AssertionError("ombre")):
            self.assertEqual(0, review.main(["--requeue-transient-evidence", "--db", self.db]))
        self.assertIn('"requeued": 1', shown.call_args[0][0])


class RequeueTest(ReviewTestCase):
    def test_policy_rejected_goes_back_to_pending_and_is_reviewed_again(self):
        policy = self.add("甲。")
        self.gen.supported = False
        self.run_review()
        self.assertEqual("rejected", self.status(policy))
        human = self.add("乙。")
        committed = self.add("丙。")
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO memory_candidate_reviews (candidate_id, outcome, decision, decision_by, dry_run, created_at) "
                     "VALUES (?, 'suppressed_rejected', 'rejected', 'fedya', 0, 'x')", (human,))
        conn.execute("UPDATE memory_candidates SET status='rejected' WHERE candidate_id=?", (human,))
        conn.execute("INSERT INTO memory_candidate_reviews (candidate_id, outcome, decision, decision_by, dry_run, created_at) "
                     "VALUES (?, 'committed_new', 'accepted', 'policy', 0, 'x')", (committed,))
        conn.execute("UPDATE memory_candidates SET status='committed' WHERE candidate_id=?", (committed,))
        conn.commit()
        conn.close()
        kernel_before = [self.kernel_count(t) for t in ("evidence", "state", "delta")]
        self.assertEqual(1, review.requeue_policy_rejected(self.db))
        self.assertEqual(("pending", "rejected", "committed"), tuple(self.status(c) for c in (policy, human, committed)))
        self.assertEqual("fedya", self.row(human)["decision_by"])
        self.assertEqual(kernel_before, [self.kernel_count(t) for t in ("evidence", "state", "delta")])
        self.assertEqual(0, review.requeue_policy_rejected(self.db))
        self.gen.supported = True
        result = self.run_review()
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        self.assertEqual("committed", self.status(policy))

    def test_dry_run_only_reviews_change_nothing(self):
        cid = self.add("甲。")
        self.gen.supported = False
        self.run_review(dry_run=True)
        self.assertEqual("pending", self.status(cid))
        self.assertEqual(0, review.requeue_policy_rejected(self.db))

    def test_cli_flag_prints_count_without_ombre(self):
        self.add("甲。")
        with mock.patch("builtins.print") as shown:
            self.assertEqual(0, review.main(["--requeue-policy-rejected", "--db", self.db]))
        self.assertIn('"requeued": 0', shown.call_args[0][0])


class OmbreAdapterTest(unittest.TestCase):
    class Runtime:
        def __init__(self, ops, results=None, dehydrator=None):
            self.ops, self.results, self.sent = ops, results or {}, []
            self.dehydrator = dehydrator or {"ok": True, "methods": ["analyze", "judge_same_event"], "error": ""}

        def capabilities(self):
            return {"version": "3.6.14", "ops": self.ops, "dehydrator": self.dehydrator}

        def batch(self, requests):
            self.sent.extend(requests)
            return [self.results.get(r["op"], projection.RequestFailed("ValueError:unknown op")) for r in requests]

    def test_preflight_names_missing_ops_and_never_guesses(self):
        with self.assertRaisesRegex(review.ReviewAbort, "ombre_review_ops_missing:analyze,judge_same_event"):
            review.RuntimeReviewOmbre(self.Runtime(["get", "search"])).preflight()
        review.RuntimeReviewOmbre(self.Runtime(["analyze", "search", "judge_same_event"])).preflight()
        ops = ["analyze", "search", "judge_same_event"]
        with self.assertRaisesRegex(review.ReviewAbort, "ombre_dehydrator_methods_missing:judge_same_event"):
            review.RuntimeReviewOmbre(self.Runtime(
                ops, dehydrator={"ok": True, "methods": ["analyze"], "error": ""})).preflight()
        with self.assertRaisesRegex(review.ReviewAbort, "ombre_dehydrator_unavailable:not_reported"):
            runtime = self.Runtime(ops)
            runtime.capabilities = lambda: {"ops": ops}
            review.RuntimeReviewOmbre(runtime).preflight()

    def test_ops_map_to_worker_requests_and_failures_raise(self):
        runtime = self.Runtime([], {"search": [{"id": "b1", "content": "x"}, {"content": "no id"}],
                                    "analyze": {"domain": []}})
        ombre = review.RuntimeReviewOmbre(runtime)
        self.assertEqual(["b1"], [hit["bucket_id"] for hit in ombre.search("q", 3)])
        self.assertEqual({"domain": []}, ombre.analyze("t"))
        self.assertEqual([{"op": "search", "query": "q", "limit": 3}, {"op": "analyze", "text": "t"}], runtime.sent)
        with self.assertRaisesRegex(RuntimeError, "judge_same_event_failed"):
            ombre.judge_same_event("a", "b")

    def test_worker_search_op_is_read_only_and_capabilities_list_ops(self):
        class Manager:
            async def search(self, query, limit):
                self.args = (query, limit)
                return [{"id": "b1", "content": "猫", "metadata": {"name": "n"}}]

        manager = Manager()
        found = asyncio.run(worker.handle(manager, {"op": "search", "query": "猫", "limit": 3}))
        self.assertEqual((("猫", 3), "b1"), (manager.args, found[0]["id"]))
        self.assertIn("search", worker.OPS)
        self.assertTrue({"analyze", "judge_same_event"} <= set(worker.OPS))
        self.assertNotIn("digest", worker.OPS)
        with self.assertRaises(worker.Incompatible):
            asyncio.run(worker.handle(object(), {"op": "search", "query": "x"}))


class CliAndDeployTest(ReviewTestCase):
    def test_main_without_ombre_runtime_aborts_visibly(self):
        env = {k: "" for k in ("OMBRE_RUNTIME_PYTHON", "OMBRE_RUNTIME_SRC", "OMBRE_RUNTIME_ENV_FILE")}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(review, "RUNTIME_ENV_FILE", str(Path(self.db).with_name("missing.env"))):
            self.assertEqual(1, review.main(["--dry-run", "--db", self.db, "--kernel-db", self.kernel_db]))
        health = review.review_health(self.db)
        self.assertEqual(("failed", "ombre_runtime_unconfigured"),
                         (health["last_run"]["status"], health["last_run"]["error"]))

    def test_runtime_env_is_read_from_frontend_env_file_for_cron(self):
        env_file = Path(self.db).with_name("frontend.env")
        env_file.write_text(
            "OTHER_SECRET=nope\nexport OMBRE_RUNTIME_PYTHON=/ombre/py\n"
            "OMBRE_RUNTIME_SRC='/ombre/src'\nOMBRE_RUNTIME_ENV_FILE=\"/ombre/.env\"\n",
            encoding="utf-8")
        keys = ("OMBRE_RUNTIME_PYTHON", "OMBRE_RUNTIME_SRC", "OMBRE_RUNTIME_ENV_FILE", "OTHER_SECRET")
        with mock.patch.dict(os.environ, {k: "" for k in keys}):
            os.environ["OMBRE_RUNTIME_SRC"] = "/already/set"
            loaded = review.load_runtime_env(str(env_file))
            self.assertEqual(["OMBRE_RUNTIME_PYTHON", "OMBRE_RUNTIME_ENV_FILE"], loaded)
            self.assertEqual("/ombre/py", os.environ["OMBRE_RUNTIME_PYTHON"])
            self.assertEqual("/already/set", os.environ["OMBRE_RUNTIME_SRC"])
            self.assertEqual("/ombre/.env", os.environ["OMBRE_RUNTIME_ENV_FILE"])
            self.assertEqual("", os.environ["OTHER_SECRET"])
            runtime = review.projection.OmbreRuntime.from_env()
            self.assertEqual(("/ombre/py", "/already/set", "/ombre/.env"),
                             (runtime.python, runtime.src_dir, runtime.env_file))

    def test_main_loads_runtime_env_before_anything_else(self):
        with mock.patch.object(review, "load_runtime_env", side_effect=RuntimeError("loaded first")):
            with self.assertRaisesRegex(RuntimeError, "loaded first"):
                review.main(["--status", "--db", self.db])

    def test_status_flag_prints_health(self):
        self.assertEqual(0, review.main(["--status", "--db", self.db]))

    def test_cron_commits_after_both_dehydrators_and_deploy_installs_with_rollback(self):
        cron = (ROOT / "deploy/cron.d/hayagarden-memory-review").read_text()
        self.assertIn("CRON_TZ=UTC", cron)
        self.assertIn("15 20 * * * root cd /opt/frontend &&", cron)
        self.assertIn("/usr/bin/flock -n /run/lock/hayagarden-memory-review.lock", cron)
        self.assertIn("python3.11 -m tools.memory_review >> /var/log/hayagarden-memory-review.log", cron)
        self.assertNotIn("--dry-run", cron)
        deploy = (ROOT / "scripts/deploy-frontend.sh").read_text()
        for line in (
            'REVIEW_CRON_PATH="/etc/cron.d/hayagarden-memory-review"',
            'cp -a "$REVIEW_CRON_PATH" "$REVIEW_CRON_BACKUP"; REVIEW_CRON_EXISTED=1',
            'install -o root -g root -m 0644 "$staging/deploy/cron.d/hayagarden-memory-review" "$REVIEW_CRON_PATH"',
            'cp -a "$REVIEW_CRON_BACKUP" "$REVIEW_CRON_PATH"; else rm -f "$REVIEW_CRON_PATH"',
        ):
            self.assertIn(line, deploy)
        self.assertEqual(2, deploy.count('"$MAIL_CRON_BACKUP" "$REVIEW_CRON_BACKUP" "$ROOT_CRONTAB_BACKUP"'))


class ScriptedGenerator:
    """Pops one scripted support response per call (dict -> JSON, str -> raw text)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def __call__(self, request, authority):
        self.calls.append(request)
        item = self.script.pop(0)
        return BackgroundGenerationResult(
            text=item if isinstance(item, str) else json.dumps(item),
            provider="p", model_identity="m", actual_executor="test")


class ContextAndTrimTest(ReviewTestCase):
    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT)")
        for i in range(1, 31):
            conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?)",
                         (i, "assistant" if i % 2 == 0 else "hayana", f"消息{i}", "2026-11-01 09:00:00"))
        conn.commit()
        conn.close()

    def script(self, *responses):
        self.gen = ScriptedGenerator(responses)

    def test_context_neighbors_rendered_capped_and_cited_not_duplicated(self):
        got = review.load_chat_context(self.db, [10, 12])
        ids = [r.ref for r in got]
        self.assertNotIn("chat_messages:10", ids)
        self.assertNotIn("chat_messages:12", ids)
        self.assertEqual(sorted(ids, key=lambda r: int(r.split(":")[1])), ids)
        self.assertIn("chat_messages:4", ids)
        self.assertIn("chat_messages:18", ids)
        self.assertNotIn("chat_messages:3", ids)
        self.assertEqual("哈娅", next(r for r in got if r.ref == "chat_messages:11").speaker)
        capped = review.load_chat_context(self.db, [10], max_chars=60)
        self.assertTrue(0 < len(capped) < 12)
        near = {r.ref for r in capped}
        self.assertIn("chat_messages:11", near)
        self.assertEqual([], review.load_chat_context(self.db, []))

    def test_prompt_has_marked_context_section_with_speaker_labels(self):
        self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script({"supported": True, "reason": "ok"})
        self.run_review()
        prompt = self.gen.calls[0].prompt_text
        self.assertIn("上下文（未被引用，仅供参考）", prompt)
        head, _, tail = prompt.partition("上下文（未被引用，仅供参考）")
        self.assertNotIn("[chat_messages:11]", head)
        self.assertIn("[chat_messages:11][发言人：哈娅]", tail)
        self.assertNotIn("[chat_messages:10]", tail)

    def test_no_context_section_without_chat_refs(self):
        self.add("费奥多尔下个月搬去札幌。", refs=("gmail_message:abc",))
        self.script({"supported": True, "reason": "ok"})
        self.run_review()
        self.assertNotIn("上下文", self.gen.calls[0].prompt_text)

    def test_extra_ids_validated_and_become_kernel_evidence_with_stable_ids(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script({"supported": True, "reason": "ok", "extra_evidence_ids": [11, "chat_messages:12", 999, "x"]})
        self.run_review()
        ids = review.candidate_ids(cid)
        evidence = MemoryKernel(self.kernel_db).evidence_for_state(ids["state"])
        refs = {e.source_ref for e in evidence}
        self.assertEqual({"chat_messages:10", "chat_messages:11", "chat_messages:12"}, refs)
        self.assertTrue(all(e.evidence_id.startswith(ids["evidence_prefix"]) for e in evidence))
        self.assertIn("[assistant] 消息12", next(e for e in evidence if e.source_ref == "chat_messages:12").content)
        support = json.loads(self.row(cid)["support_check_json"])
        self.assertEqual([11, 12], support["extra_evidence_ids"])
        self.assertEqual(2, len(support["ignored_extra_evidence_ids"]))
        conn = sqlite3.connect(self.db)
        try:
            stored = json.loads(conn.execute("SELECT source_refs_json FROM memory_candidates WHERE candidate_id=?",
                                             (cid,)).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(["chat_messages:10"], stored)

    def test_unknown_extra_ids_are_ignored(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script({"supported": True, "reason": "ok", "extra_evidence_ids": [10, 500]})
        self.run_review()
        evidence = MemoryKernel(self.kernel_db).evidence_for_state(review.candidate_ids(cid)["state"])
        self.assertEqual({"chat_messages:10"}, {e.source_ref for e in evidence})
        self.assertNotIn("extra_evidence_ids", json.loads(self.row(cid)["support_check_json"]))

    def test_trimmed_rewrite_commits_only_after_second_supported_check(self):
        cid = self.add("费奥多尔下个月搬去札幌，双方商定了日期。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "日期不是双方商定", "extra_evidence_ids": [11],
                     "supported_rewrite": "费奥多尔下个月搬去札幌。"},
                    {"supported": True, "rewrite_faithful": True, "reason": "核心被支持", "supported_rewrite": "忽略我"})
        result = self.run_review()
        self.assertEqual({"committed_new": 1}, result["outcomes"])
        self.assertEqual(2, len(self.gen.calls))
        second_head = self.gen.calls[1].prompt_text.split("=== 证据原文")[0]
        self.assertIn("原始候选：\n费奥多尔下个月搬去札幌，双方商定了日期。", second_head)
        self.assertIn("待核对的删减版", second_head)
        self.assertIn("费奥多尔下个月搬去札幌。", second_head)
        self.assertIn("rewrite_faithful", self.gen.calls[1].system_text)
        self.assertNotIn("原始候选", self.gen.calls[0].prompt_text)
        self.assertIn("[chat_messages:11]", self.gen.calls[1].prompt_text.split("上下文（未被引用")[0])
        ids = review.candidate_ids(cid)
        kernel = MemoryKernel(self.kernel_db)
        state = kernel.get_state(ids["state"])
        self.assertEqual("费奥多尔下个月搬去札幌。", state.content)
        self.assertTrue(state.structured_value["review"]["trimmed_from_candidate"])
        self.assertEqual("费奥多尔下个月搬去札幌，双方商定了日期。", state.structured_value["review"]["original_content"])
        self.assertEqual({"chat_messages:10", "chat_messages:11"},
                         {e.source_ref for e in kernel.evidence_for_state(ids["state"])})
        row = self.row(cid)
        self.assertEqual(("committed_new", "trimmed_rewrite"), (row["outcome"], row["decision_reason"]))
        support = json.loads(row["support_check_json"])
        self.assertEqual("费奥多尔下个月搬去札幌。", support["trimmed_rewrite"]["committed_content"])
        self.assertFalse(support["trimmed_rewrite"]["original_check"]["supported"])
        self.assertTrue(support["trimmed_rewrite"]["rewrite_check"]["supported"])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual("费奥多尔下个月搬去札幌，双方商定了日期。", json.loads(conn.execute(
                "SELECT payload_json FROM memory_candidates WHERE candidate_id=?", (cid,)).fetchone()[0])["content"])
        finally:
            conn.close()

    def test_trimmed_rewrite_failing_second_check_is_rejected(self):
        cid = self.add("费奥多尔下个月搬去札幌，双方商定了日期。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "x", "supported_rewrite": "费奥多尔下个月搬去札幌。"},
                    {"supported": False, "rewrite_faithful": True, "reason": "仍不支持", "supported_rewrite": "再来一次"})
        result = self.run_review()
        self.assertEqual({"rejected_unsupported": 1}, result["outcomes"])
        self.assertEqual(2, len(self.gen.calls))
        self.assertEqual(0, self.kernel_count("state"))
        self.assertEqual("rejected", self.status(cid))
        self.assertFalse(json.loads(self.row(cid)["support_check_json"])["rewrite_check"]["supported"])

    def test_no_rewrite_is_rejected_with_a_single_call(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "原文没有"})
        self.assertEqual({"rejected_unsupported": 1}, self.run_review()["outcomes"])
        self.assertEqual(1, len(self.gen.calls))
        self.assertEqual("rejected", self.status(cid))

    def test_trimmed_rewrite_that_is_exact_duplicate_stops_there(self):
        head = self.seed_head("费奥多尔下个月搬去札幌。")
        cid = self.add("费奥多尔下个月搬去札幌，双方商定了日期。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "x", "supported_rewrite": "费奥多尔下个月搬去札幌。"},
                    {"supported": True, "rewrite_faithful": True, "reason": "ok"})
        self.assertEqual({"exact_duplicate": 1}, self.run_review()["outcomes"])
        self.assertEqual(head["lineage_id"], self.row(cid)["matched_lineage_id"])

    def test_empty_model_response_three_runs_goes_to_needs_review(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script("", "", "")
        for n in (1, 2):
            self.assertEqual("partial_failure", self.run_review()["status"])
            row = self.row(cid)
            self.assertEqual(("error", n, "pending"), (row["outcome"], row["error_count"], self.status(cid)))
            self.assertIn("JSONDecodeError", row["error"])
        result = self.run_review()
        self.assertEqual({"needs_review": 1}, result["outcomes"])
        row = self.row(cid)
        self.assertEqual("needs_review", row["outcome"])
        self.assertTrue(row["decision_reason"].startswith("repeated_errors:SupportOutputError:JSONDecodeError"))
        self.assertEqual("needs_review", self.status(cid))
        self.assertEqual(0, self.kernel_count("state"))

    def test_dry_run_never_increments_error_count_but_shows_what_would_happen(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script("", "", "", "", "")
        self.run_review()
        self.run_review()
        self.assertEqual(2, self.row(cid)["error_count"])
        for _ in range(2):
            self.run_review(dry_run=True)
            row = self.row(cid)
            self.assertEqual(("needs_review", 1, 2, "pending"),
                             (row["outcome"], row["dry_run"], row["error_count"], self.status(cid)))
        fresh = self.add("另一条。", refs=("chat_messages:20",))
        self.script("")
        self.run_review(dry_run=True, limit=1)
        self.assertEqual(0, self.row(fresh)["error_count"] if self.row(fresh) else 0)

    def test_ombre_failures_count_too(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script({"supported": True, "reason": "ok"}, {"supported": True, "reason": "ok"},
                    {"supported": True, "reason": "ok"})
        self.ombre.analyze = mock.Mock(side_effect=RuntimeError("down"))
        self.run_review()
        self.run_review()
        self.assertEqual({"needs_review": 1}, self.run_review()["outcomes"])
        self.assertIn("repeated_errors:OmbreFailure", self.row(cid)["decision_reason"])
        self.assertEqual("needs_review", self.status(cid))

    def test_error_count_resets_after_success(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script("", "", {"supported": True, "reason": "ok"})
        self.run_review()
        self.run_review()
        self.assertEqual(2, self.row(cid)["error_count"])
        self.assertEqual({"committed_new": 1}, self.run_review()["outcomes"])
        self.assertEqual(0, self.row(cid)["error_count"])

    def test_rewrite_that_swaps_in_another_context_fact_is_rejected_even_if_that_fact_is_supported(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE chat_messages SET content='哈娅今天买了一只蓝色杯子。' WHERE id=11")
        conn.commit()
        conn.close()
        cid = self.add("费奥多尔下个月搬去札幌，并且双方已经商定日期。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "没有搬家的依据", "extra_evidence_ids": [11],
                     "supported_rewrite": "哈娅今天买了一只蓝色杯子。"},
                    {"supported": True, "rewrite_faithful": False, "reason": "这是另一件事，不是原候选的删减"})
        result = self.run_review()
        self.assertEqual({"rejected_unsupported": 1}, result["outcomes"])
        self.assertEqual(2, len(self.gen.calls))  # no third model call
        self.assertEqual(0, self.kernel_count("state"))
        self.assertEqual([], [c for c in self.ombre.calls if c[0] == "analyze"])
        row = self.row(cid)
        self.assertEqual(("rejected_unsupported", "policy", "rejected"),
                         (row["outcome"], row["decision_by"], self.status(cid)))
        self.assertTrue(row["decision_reason"].startswith("rewrite_not_faithful:"))
        check = json.loads(row["support_check_json"])["rewrite_check"]
        self.assertEqual((True, False), (check["supported"], check["rewrite_faithful"]))
        self.assertIn("原始候选：\n费奥多尔下个月搬去札幌", self.gen.calls[1].prompt_text)

    def test_second_check_must_answer_rewrite_faithful(self):
        cid = self.add("费奥多尔下个月搬去札幌，双方商定了日期。", refs=("chat_messages:10",))
        self.script({"supported": False, "reason": "x", "supported_rewrite": "费奥多尔下个月搬去札幌。"},
                    {"supported": True, "reason": "没有回答忠实度"})
        result = self.run_review()
        self.assertEqual("partial_failure", result["status"])
        row = self.row(cid)
        self.assertEqual(("error", 1, "pending"), (row["outcome"], row["error_count"], self.status(cid)))
        self.assertIn("missing_rewrite_faithful", row["error"])
        self.assertEqual(0, self.kernel_count("state"))

    def test_kernel_faults_never_escalate_to_human_review(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        real_insert = MemoryKernel._insert

        def failing(conn, record):
            if type(record).__name__ == "Delta":
                raise RuntimeError("disk full")
            return real_insert(conn, record)

        with mock.patch.object(MemoryKernel, "_insert", staticmethod(failing)):
            for _ in range(4):
                self.assertEqual("partial_failure", self.run_review()["status"])
                row = self.row(cid)
                self.assertEqual(("error", 0, "pending"), (row["outcome"], row["error_count"], self.status(cid)))
                self.assertIn("disk full", row["error"])
        self.assertEqual(0, self.kernel_count("state"))
        self.assertEqual({"committed_new": 1}, self.run_review()["outcomes"])  # recovers once the fault is gone
        self.assertEqual(("committed", 0), (self.status(cid), self.row(cid)["error_count"]))

    def test_infrastructure_errors_do_not_reset_or_advance_the_content_error_count(self):
        cid = self.add("费奥多尔下个月搬去札幌。", refs=("chat_messages:10",))
        self.script("", "")
        self.run_review()
        self.run_review()
        self.assertEqual(2, self.row(cid)["error_count"])
        self.gen = FakeGenerator()
        self.gen.fail = ConnectionError("relay down")  # transport fault, not an unusable reply
        self.run_review()
        row = self.row(cid)
        self.assertEqual(("error", 2, "pending"), (row["outcome"], row["error_count"], self.status(cid)))
        self.script("")
        self.assertEqual({"needs_review": 1}, self.run_review()["outcomes"])
        self.assertTrue(self.row(cid)["decision_reason"].startswith("repeated_errors:SupportOutputError"))

    def test_schema_upgrade_adds_error_count_idempotently(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE memory_candidate_reviews (candidate_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, "
                     "created_at TEXT NOT NULL, error TEXT, decision TEXT, decision_by TEXT)")
        review.ensure_review_schema(conn)
        review.ensure_review_schema(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_candidate_reviews)")]
        conn.close()
        self.assertEqual(1, cols.count("error_count"))


if __name__ == "__main__":
    unittest.main()
