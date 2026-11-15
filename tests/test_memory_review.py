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
        flaky = self.add("乙", refs=("gmail_message:x",))
        unresolved = self.add("丙", refs=(), provenance="unresolved")

        def resolver(ref):
            if ref == "gmail_message:x":
                raise review.EvidenceUnavailable("gmail_reader_unavailable")
            return None

        self.run_review(resolvers={"chat_messages": resolver, "gmail_message": resolver})
        reasons = {c: self.row(c)["decision_reason"] for c in (missing, flaky, unresolved)}
        self.assertIn("not_found", reasons[missing])
        self.assertIn("gmail_reader_unavailable", reasons[flaky])
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
        with mock.patch.dict(os.environ, env):
            self.assertEqual(1, review.main(["--dry-run", "--db", self.db, "--kernel-db", self.kernel_db]))
        health = review.review_health(self.db)
        self.assertEqual(("failed", "ombre_runtime_unconfigured"),
                         (health["last_run"]["status"], health["last_run"]["error"]))

    def test_status_flag_prints_health(self):
        self.assertEqual(0, review.main(["--status", "--db", self.db]))

    def test_cron_runs_dry_after_both_dehydrators_and_deploy_installs_with_rollback(self):
        cron = (ROOT / "deploy/cron.d/hayagarden-memory-review").read_text()
        self.assertIn("CRON_TZ=UTC", cron)
        self.assertIn("15 20 * * * root cd /opt/frontend &&", cron)
        self.assertIn("/usr/bin/flock -n /run/lock/hayagarden-memory-review.lock", cron)
        self.assertIn("python3.11 -m tools.memory_review --dry-run", cron)
        deploy = (ROOT / "scripts/deploy-frontend.sh").read_text()
        for line in (
            'REVIEW_CRON_PATH="/etc/cron.d/hayagarden-memory-review"',
            'cp -a "$REVIEW_CRON_PATH" "$REVIEW_CRON_BACKUP"; REVIEW_CRON_EXISTED=1',
            'install -o root -g root -m 0644 "$staging/deploy/cron.d/hayagarden-memory-review" "$REVIEW_CRON_PATH"',
            'cp -a "$REVIEW_CRON_BACKUP" "$REVIEW_CRON_PATH"; else rm -f "$REVIEW_CRON_PATH"',
        ):
            self.assertIn(line, deploy)
        self.assertEqual(2, deploy.count('"$MAIL_CRON_BACKUP" "$REVIEW_CRON_BACKUP" "$ROOT_CRONTAB_BACKUP"'))


if __name__ == "__main__":
    unittest.main()
