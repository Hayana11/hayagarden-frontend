import datetime as dt
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
TEST_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEST_ROOT / "tools"))
sys.path.insert(0, str(TEST_ROOT))

import dayblock_shadow as dbs
from continuity.contracts import SourceMember, candidate_source_revision


TZ = ZoneInfo("Asia/Shanghai")


def _chat_row(mid, author, body, created_at, *, source_kind="chat", cache_info="{}", attachments="[]"):
    return (mid, author, body, "", created_at, "[]", "[]", 0, cache_info,
            source_kind, attachments, "", "", "")


def make_source_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE chat_messages(
        id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, created_at TEXT,
        tool_calls TEXT, branches TEXT, branch_idx INTEGER, cache_info TEXT, source_kind TEXT,
        attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT
      );
      CREATE TABLE daily_message_contexts(message_id INTEGER, context_id INTEGER, context_epoch INTEGER);
      CREATE TABLE wake_log(wake_run_id TEXT, context_id INTEGER, context_epoch INTEGER, woke_at TEXT);
    """)
    cache = json.dumps({
        "wake_mode": "normal", "canonical_chat_history": True,
        "unified_chat_resident": True, "b3_authority": True,
        "source": "wake", "provider": "claude_code", "wake_run_id": "wake-run-1",
    })
    rows = [
        _chat_row(1, "hayana", "target user turn one", "2026-09-25 00:00:00"),
        _chat_row(2, "fyodor", "target assistant turn one", "2026-09-25 00:01:00"),
        _chat_row(3, "hayana", "target user turn two", "2026-09-25 23:58:00"),
        _chat_row(4, "fyodor", "target assistant turn two", "2026-09-25 23:59:00",
                  attachments='[{"type":"image","url":"/static/uploads/a.png"}]'),
        _chat_row(5, "hayana", "today must not enter", "2026-09-26 00:00:00"),
        _chat_row(6, "fyodor", "today reply must not enter", "2026-09-26 00:01:00"),
        _chat_row(7, "fyodor", "canonical wake event", "2026-09-25 18:00:00",
                  source_kind="wake", cache_info=cache),
    ]
    conn.executemany(
        "INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    conn.executemany(
        "INSERT INTO daily_message_contexts VALUES(?,?,?)",
        [(mid, 31, 9) for mid in range(1, 7)],
    )
    conn.execute(
        "INSERT INTO wake_log VALUES(?,?,?,?)",
        ("wake-run-1", 31, 9, "2026-09-25 18:00:00"),
    )
    conn.commit()
    conn.close()


def make_inputs(db_path):
    now = dt.datetime(2026, 9, 26, 12, 0, tzinfo=TZ)
    day = "2026-09-25"
    created = now.isoformat(timespec="seconds")
    rows = dbs.discover_natural_day_rows(db_path, day)
    snapshot, turns, events, members = dbs.derive_source_contract(rows, day, created)
    materialized = dbs.materialize_raw_evidence(members, rows)
    return rows, snapshot, turns, events, members, materialized


def authority(provider="claude_code", model="explicit:claude-opus-4-6", rev="authority-r1"):
    return dbs.AuthorityObservation(provider, model, rev, "authority-snapshot-" + rev)


class DayBlockShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dayblock-r0-")
        self.source_db = Path(self.temp.name) / "source.db"
        self.preview_db = Path(self.temp.name) / "preview.db"
        make_source_db(self.source_db)

    def tearDown(self):
        self.temp.cleanup()

    def test_schedule_and_natural_day_are_half_open(self):
        before = dt.datetime(2026, 9, 26, 2, 59, tzinfo=TZ)
        at = dt.datetime(2026, 9, 26, 3, 0, tzinfo=TZ)
        after = dt.datetime(2026, 9, 26, 23, 59, tzinfo=TZ)
        self.assertEqual(dbs.scheduled_source_day(before), "2026-09-25")
        self.assertEqual(dbs.scheduled_source_day(at), "2026-09-25")
        self.assertEqual(dbs.scheduled_source_day(after), "2026-09-25")
        self.assertEqual(dbs.natural_day_window("2026-09-25"),
                         ("2026-09-25T00:00:00+08:00", "2026-09-26T00:00:00+08:00"))
        self.assertTrue(dbs._is_in_day("2026-09-25 23:59:59", "2026-09-25"))
        self.assertFalse(dbs._is_in_day("2026-09-26 00:00:00", "2026-09-25"))

    def test_discovery_excludes_today_and_keeps_turn_and_wake_kinds_distinct(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        ids = {int(row["id"]) for row in rows}
        self.assertEqual(ids, {1, 2, 3, 4, 7})
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual([m.source_kind for m in members],
                         ["completed_turn", "autonomous_event", "completed_turn", "attachment_span"])
        self.assertEqual([m.source_ref for m in members],
                         ["turn:1:2", "wake:7", "turn:3:4", "attachment:4:0"])
        self.assertEqual(materialized.source_refs, tuple(m.source_ref for m in members))
        self.assertFalse(any(m.source_ref.startswith("dayblock_visible_turn:") for m in members))
        self.assertNotIn("today must not enter", materialized.body)
        self.assertNotIn("today reply must not enter", materialized.body)
        self.assertGreater(materialized.source_token_estimate, 0)
        self.assertTrue(snapshot.source_hash)

    def _insert_messages(self, rows, scopes=None):
        scopes = scopes or {int(row[0]): (31, 9) for row in rows}
        conn = sqlite3.connect(self.source_db)
        conn.executemany(
            "INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        conn.executemany(
            "INSERT INTO daily_message_contexts VALUES(?,?,?)",
            [(int(row[0]), *scopes[int(row[0])]) for row in rows],
        )
        conn.commit()
        conn.close()

    def _partial_cache(self, **overrides):
        value = {"partial_rescue": True, "turn_incomplete": True, "stream_interrupted": True}
        value.update(overrides)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def test_partial_rescue_pair_is_dayblock_visible_projection(self):
        self._insert_messages([
            _chat_row(8, "hayana", "爸爸再查查灯💡", "2026-09-25 12:00:00",
                      attachments='[{"type":"image","url":"/static/uploads/user.png"}]'),
            _chat_row(9, "fyodor", "partial assistant evidence", "2026-09-25 12:01:00",
                      cache_info=self._partial_cache(),
                      attachments='[{"type":"image","url":"/static/uploads/assistant.png"}]'),
        ])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        self.assertEqual(
            [(row["id"], row["_dayblock_context_id"], row["_dayblock_context_epoch"])
             for row in rows if int(row["id"]) in {8, 9}],
            [(8, 31, 9), (9, 31, 9)],
        )
        snapshot, turns, events, members = dbs.derive_source_contract(
            rows, "2026-09-25", "2026-09-26T12:00:00+08:00",
        )
        materialized = dbs.materialize_raw_evidence(members, rows)
        visible = [member for member in members if member.source_ref == "dayblock_visible_turn:8:9"]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].source_kind, "incomplete_user_turn")
        self.assertEqual(len(turns), 2)
        self.assertFalse(any(turn.turn_id == "turn:8:9" for turn in turns))
        self.assertEqual(dbs.uncovered_formal_source_rows(rows, members), ())
        self.assertIn("爸爸再查查灯💡", materialized.body)
        self.assertIn("partial assistant evidence", materialized.body)
        self.assertEqual(
            {item["message_id"]: item["parent_source_ref"] for item in materialized.attachments
             if item["message_id"] in {8, 9}},
            {8: "dayblock_visible_turn:8:9", 9: "dayblock_visible_turn:8:9"},
        )
        assistant = next(row for row in rows if int(row["id"]) == 9)
        self.assertEqual(
            json.loads(assistant["cache_info"]),
            {"partial_rescue": True, "turn_incomplete": True, "stream_interrupted": True},
        )
        self.assertEqual(snapshot.members[visible[0].seq].source_ref, "dayblock_visible_turn:8:9")

    def test_partial_rescue_revision_binds_both_rows_and_finality_provenance(self):
        self._insert_messages([
            _chat_row(8, "hayana", "original user", "2026-09-25 12:00:00"),
            _chat_row(9, "fyodor", "original assistant", "2026-09-25 12:01:00",
                      cache_info=self._partial_cache()),
        ])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        original = next(member for member in members if member.source_ref == "dayblock_visible_turn:8:9")
        for message_id, changes in ((8, {"content": "changed user"}),
                                    (9, {"content": "changed assistant"}),
                                    (9, {"cache_info": self._partial_cache(stream_interrupted=False)})):
            changed = tuple(
                dict(row, **changes) if int(row["id"]) == message_id else row
                for row in rows
            )
            _, _, _, changed_members = dbs.derive_source_contract(
                changed, "2026-09-25", "2026-09-26T12:00:00+08:00",
            )
            revised = next(member for member in changed_members if member.source_ref == "dayblock_visible_turn:8:9")
            self.assertNotEqual(original.source_revision, revised.source_revision)

    def test_user_only_remains_incomplete_user_projection(self):
        self._insert_messages([_chat_row(8, "hayana", "unfinished user", "2026-09-25 12:00:00")])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        self.assertEqual(
            [member.source_ref for member in members if member.source_ref.startswith("incomplete_user:")],
            ["incomplete_user:8"],
        )
        self.assertFalse(any(member.source_ref.startswith("dayblock_visible_turn:") for member in members))
        self.assertEqual(dbs.uncovered_formal_source_rows(rows, members), ())

    def test_empty_partial_rescue_assistant_is_not_visible_and_not_dropped(self):
        self._insert_messages([
            _chat_row(8, "hayana", "user before empty rescue", "2026-09-25 12:00:00"),
            _chat_row(9, "fyodor", "", "2026-09-25 12:01:00", cache_info=self._partial_cache()),
        ])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        self.assertFalse(any(member.source_ref.startswith("dayblock_visible_turn:") for member in members))
        self.assertIn("incomplete_user:8", [member.source_ref for member in members])
        self.assertEqual(dbs.uncovered_formal_source_rows(rows, members), ())

    def test_isolated_partial_rescue_assistant_remains_uncovered(self):
        self._insert_messages([
            _chat_row(8, "fyodor", "isolated partial", "2026-09-25 12:01:00",
                      cache_info=self._partial_cache()),
        ])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        self.assertFalse(any(member.source_ref.startswith("dayblock_visible_turn:") for member in members))
        self.assertIn(8, [int(row["id"]) for row in dbs.uncovered_formal_source_rows(rows, members)])

    def test_partial_rescue_pair_requires_same_context_and_epoch(self):
        self._insert_messages(
            [_chat_row(8, "hayana", "cross scope user", "2026-09-25 12:00:00"),
             _chat_row(9, "fyodor", "cross scope assistant", "2026-09-25 12:01:00",
                       cache_info=self._partial_cache())],
            scopes={8: (31, 9), 9: (31, 10)},
        )
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        self.assertFalse(any(member.source_ref.startswith("dayblock_visible_turn:") for member in members))
        self.assertEqual(
            [int(row["id"]) for row in dbs.uncovered_formal_source_rows(rows, members)], [8, 9],
        )

    def test_user_user_partial_assistant_does_not_cross_first_user(self):
        self._insert_messages([
            _chat_row(8, "hayana", "first user", "2026-09-25 12:00:00"),
            _chat_row(9, "hayana", "second user", "2026-09-25 12:01:00"),
            _chat_row(10, "fyodor", "partial after second", "2026-09-25 12:02:00",
                      cache_info=self._partial_cache()),
        ])
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        _, _, _, members = dbs.derive_source_contract(rows, "2026-09-25", "2026-09-26T12:00:00+08:00")
        self.assertIn("incomplete_user:8", [member.source_ref for member in members])
        self.assertIn("dayblock_visible_turn:9:10", [member.source_ref for member in members])
        self.assertNotIn("dayblock_visible_turn:8:10", [member.source_ref for member in members])
        self.assertEqual(dbs.uncovered_formal_source_rows(rows, members), ())

    def test_unpaired_formal_source_row_blocks_generation(self):
        conn = sqlite3.connect(self.source_db)
        conn.execute("INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     _chat_row(8, "hayana", "unfinished user turn", "2026-09-25 14:47:55"))
        conn.execute("INSERT INTO daily_message_contexts VALUES(?,?,?)", (8, 31, 9))
        conn.commit()
        conn.close()
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        uncovered = dbs.uncovered_formal_source_rows(rows, members)
        self.assertEqual(uncovered, ())
        self.assertEqual(
            [member.source_ref for member in members if member.source_kind == "incomplete_user_turn"],
            ["incomplete_user:8"],
        )
        self.assertEqual(len(turns), 2)
        self.assertIn("[INCOMPLETE USER TURN]", materialized.body)
        self.assertIn("unfinished user turn", materialized.body)
        conn = dbs.open_preview_store(self.preview_db)
        try:
            result = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T12:00:00+08:00",
                lambda: authority(), uncovered_formal_row_count=len(uncovered),
            )
            self.assertNotIn("dayblock_uncovered_formal_source_rows", result["blocking_reasons"])
            self.assertFalse(result["ready_to_generate"])
            self.assertEqual(conn.execute(
                "SELECT uncovered_formal_row_count FROM dayblock_shadow_jobs"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM dayblock_source_members WHERE source_kind='incomplete_user_turn'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_membership_has_exact_coverage_and_revision_changes_with_source(self):
        rows, snapshot, turns, events, members, _ = make_inputs(self.source_db)
        revision = candidate_source_revision(members)
        changed = list(rows)
        changed[0] = dict(changed[0], content="revised user evidence")
        snapshot2, _, _, members2 = dbs.derive_source_contract(
            tuple(changed), "2026-09-25", "2026-09-26T12:00:00+08:00",
        )
        self.assertNotEqual(snapshot.source_hash, snapshot2.source_hash)
        self.assertNotEqual(revision, candidate_source_revision(members2))
        self.assertEqual(len({m.source_ref for m in members}), len(members))

    def test_attachment_metadata_is_materialized_and_source_revision_bound(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        self.assertEqual(dbs.attachment_reference_count(rows), 1)
        attachment = next(member for member in members if member.source_kind == "attachment_span")
        self.assertIn(attachment.source_ref, materialized.source_refs)
        self.assertIn("metadata_only_unavailable", materialized.body)
        self.assertNotIn("dayblock_critical_attachment_content_unavailable", materialized.blockers)
        self.assertEqual(materialized.source_refs, tuple(member.source_ref for member in members))

    def test_candidate_identity_binds_source_revision_and_frozen_authority(self):
        original, _ = dbs._identity_ids(
            "2026-09-25", "snapshot-hash", "source-revision", authority(),
        )
        other_model, _ = dbs._identity_ids(
            "2026-09-25", "snapshot-hash", "source-revision",
            authority(model="explicit:claude-opus-5-6", rev="authority-r2"),
        )
        other_source, _ = dbs._identity_ids(
            "2026-09-25", "snapshot-hash", "source-revision-2", authority(),
        )
        self.assertNotEqual(original, other_model)
        self.assertNotEqual(original, other_source)

    def test_prompt_contract_is_versioned_and_hash_frozen(self):
        expected = hashlib.sha256(dbs.PROMPT_CONTRACT_JSON.encode("utf-8")).hexdigest()
        self.assertEqual(dbs.PROMPT_POLICY_VERSION, "dayblock_prompt_contract_r3")
        self.assertEqual(dbs.PROMPT_CONTRACT_HASH, expected)
        self.assertIn("Chunks may help navigation", dbs.PROMPT_CONTRACT_JSON)
        self.assertIn("open loops", dbs.PROMPT_CONTRACT_JSON)
        self.assertIn("incomplete turn", dbs.PROMPT_CONTRACT_JSON)
        self.assertIn("用第一人称写一篇简短日记，800字左右。", dbs.PROMPT_CONTRACT_JSON)
        self.assertIn("只有确实出现过的原话才加引号", dbs.PROMPT_CONTRACT_JSON)

    def test_raw_representation_requires_budget_and_uses_raw_evidence_when_large(self):
        self.assertEqual(dbs.plan_representation(100, None), "undetermined_raw_budget")
        self.assertEqual(dbs.plan_representation(100, 100), "raw_exact_membership")
        self.assertEqual(dbs.plan_representation(101, 100),
                         "continuity_index_plus_raw_evidence_members")

    def test_primary_authority_is_captured_once_and_retry_reuses_frozen_values(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        conn = dbs.open_preview_store(self.preview_db)
        calls = []
        try:
            first = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T12:00:00+08:00",
                lambda: calls.append("first") or authority(),
                persona_revision="persona-v1", persona_captured_at="2026-09-26T12:00:00+08:00",
            )
            second = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-27T12:00:00+08:00",
                lambda: calls.append("should-not-run") or authority("api_relay", "changed", "r2"),
                persona_revision="persona-v2", persona_captured_at="2026-09-27T12:00:00+08:00",
            )
            self.assertEqual(calls, ["first"])
            self.assertTrue(second["idempotent_replay"])
            self.assertEqual(first["candidate_id"], second["candidate_id"])
            self.assertEqual(first["frozen_authority"], second["frozen_authority"])
            self.assertEqual(second["persona_revision"], "persona-v1")
            self.assertIn("dayblock_persona_revision_changed_after_freeze", second["blocking_reasons"])
            retry = dbs.authority_for_retry(first["frozen_authority"], {
                "provider": "api_relay", "model_identity": "later-chat-model",
            })
            self.assertEqual(retry, first["frozen_authority"])
            self.assertEqual(retry["provider"], "claude_code")
        finally:
            conn.close()

    def test_new_source_revision_stales_old_plan_without_overwriting_it(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        conn = dbs.open_preview_store(self.preview_db)
        try:
            first = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T12:00:00+08:00",
                lambda: authority(),
            )
            revised_rows = list(rows)
            revised_rows[0] = dict(revised_rows[0], content="changed source")
            snapshot2, turns2, events2, members2 = dbs.derive_source_contract(
                tuple(revised_rows), "2026-09-25", "2026-09-26T12:01:00+08:00",
            )
            mat2 = dbs.materialize_raw_evidence(members2, tuple(revised_rows))
            second = dbs.persist_shadow_job(
                conn, snapshot2, turns2, events2, members2, mat2, tuple(revised_rows),
                candidate_source_revision(members2), "2026-09-26T12:01:00+08:00",
                lambda: authority(rev="authority-r2"),
            )
            statuses = dict(conn.execute("SELECT job_id,status FROM dayblock_shadow_jobs"))
            self.assertNotEqual(first["candidate_id"], second["candidate_id"])
            self.assertEqual(statuses[first["job_id"]], "stale")
            self.assertEqual(statuses[second["job_id"]], "blocked")
            self.assertEqual(conn.execute("SELECT count(*) FROM dayblock_source_snapshots").fetchone()[0], 2)
        finally:
            conn.close()

    def test_shadow_schema_keeps_source_and_frozen_job_contract_only(self):
        conn = dbs.open_preview_store(self.preview_db)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("dayblock_source_snapshots", tables)
            self.assertIn("dayblock_source_members", tables)
            self.assertIn("dayblock_shadow_jobs", tables)
            self.assertNotIn("dayblock_artifacts", tables)
            self.assertNotIn("dayblock_memory_projections", tables)
            self.assertNotIn("dayblock_current_heads", tables)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO dayblock_source_members VALUES('x',0,'continuity_chunk',"
                    "'chunk:1','r','h','assistant',1,'2026-09-25 00:00:00','active-transcript')"
                )
        finally:
            conn.close()

    def test_existing_mcp_diary_and_memory_write_contracts_remain(self):
        manifest = Path("/opt/frontend/tools/capability_manifest.py").read_text(encoding="utf-8")
        self.assertIn('"memory.write"', manifest)
        self.assertIn('"diary.write"', manifest)
        self.assertFalse(any(path.name in {"app.py", "auto_diary.py", "memory_tool.py"}
                             for path in Path("/opt/frontend-preview").glob("dayblock*")))

    def test_cli_has_no_independent_provider_or_model_selector_and_calls_no_model(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dayblock_cli", "/opt/frontend-preview/tools/dayblock_canary.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        options = {option for action in cli.build_parser()._actions for option in action.option_strings}
        self.assertNotIn("--provider", options)
        self.assertNotIn("--model", options)
        self.assertEqual(dbs.MODEL_CALL_COUNT, 0)

    def _member(self, seq, ref, *, kind="completed_turn", revision=None):
        revision = revision or ("rev-" + ref)
        return SourceMember(
            seq=seq, source_kind=kind, source_ref=ref, source_revision=revision,
            role="conversation", content_hash=revision, logical_size=100,
            created_at=f"2026-09-25 0{seq}:00:00", branch_id="active-transcript",
        )

    def _chunk(self, chunk_id, body, members, *, status="ready"):
        return {
            "chunk_id": chunk_id, "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "status": status,
            "source_members": [
                {"source_ref": m.source_ref, "source_revision": m.source_revision}
                for m in members
            ],
        }

    def test_generation_input_direct_raw_when_full_input_fits(self):
        members = (self._member(0, "turn:1:2"), self._member(1, "turn:3:4"))
        raw = {m.source_ref: "evidence " * 20 for m in members}
        plan = dbs.build_generation_input_plan(members, raw, "static", 1000)
        self.assertEqual(plan["mode"], "direct_raw")
        self.assertEqual(plan["raw_coverage"], 1.0)
        self.assertEqual(plan["chunk_coverage"], 0.0)
        self.assertTrue(plan["budget_fit"])

    def test_over_budget_uses_deterministic_mixed_exact_chunk_and_raw(self):
        members = (self._member(0, "turn:1:2"), self._member(1, "turn:3:4"))
        raw = {m.source_ref: "evidence " * 400 for m in members}
        chunk = self._chunk("c1", "short grounded chunk", (members[0],))
        a = dbs.build_generation_input_plan(members, raw, "", 1200, (chunk,))
        b = dbs.build_generation_input_plan(members, raw, "", 1200, (chunk,))
        self.assertEqual(a, b)
        self.assertEqual(a["mode"], "mixed")
        self.assertEqual(a["chunk_refs"], ["turn:1:2"])
        self.assertEqual(a["raw_refs"], ["turn:3:4"])
        self.assertEqual(a["source_coverage"], 1.0)
        self.assertEqual(a["uncovered_source_count"], 0)
        self.assertTrue(a["budget_fit"])

    def test_chunk_cannot_cover_nonmember_or_revision_mismatch(self):
        member = self._member(0, "turn:1:2")
        raw = {member.source_ref: "evidence " * 300}
        wrong = self._chunk("wrong", "short", (self._member(0, member.source_ref, revision="other"),))
        plan = dbs.build_generation_input_plan((member,), raw, "", 50, (wrong,))
        self.assertEqual(plan["chunk_refs"], [])
        self.assertEqual(plan["raw_refs"], [member.source_ref])
        self.assertEqual(plan["source_coverage"], 1.0)
        self.assertEqual(plan["uncovered_source_count"], 0)
        self.assertFalse(plan["budget_fit"])

    def test_failed_chunk_falls_back_to_raw_without_silent_drop(self):
        members = (self._member(0, "turn:1:2"), self._member(1, "turn:3:4"))
        raw = {m.source_ref: "evidence " * 300 for m in members}
        failed = self._chunk("failed", "short", (members[0],), status="stale")
        plan = dbs.build_generation_input_plan(members, raw, "", 40, (failed,))
        self.assertEqual(plan["chunk_refs"], [])
        self.assertEqual(plan["raw_refs"], [m.source_ref for m in members])
        self.assertEqual(plan["source_coverage"], 1.0)
        self.assertEqual(plan["uncovered_source_count"], 0)
        self.assertIn("dayblock_generation_input_exceeds_budget", plan["blocking_reasons"])

    def test_full_source_coverage_does_not_claim_ready_without_verified_budget(self):
        member = self._member(0, "turn:1:2")
        plan = dbs.build_generation_input_plan(
            (member,), {member.source_ref: "all raw evidence"}, "static", None,
        )
        self.assertEqual(plan["source_coverage"], 1.0)
        self.assertEqual(plan["uncovered_source_count"], 0)
        self.assertIsNone(plan["budget_fit"])
        self.assertIn("dayblock_model_context_budget_unverified", plan["blocking_reasons"])

    def test_persisted_generation_plan_rejects_incomplete_source_coverage(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        conn = dbs.open_preview_store(self.preview_db)
        try:
            plan = {
                "mode": "direct_raw", "source_coverage": 0.5,
                "uncovered_source_count": 1, "budget_fit": True,
                "blocking_reasons": [],
            }
            result = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T03:00:00+08:00",
                lambda: authority(), raw_budget_tokens=10000,
                persona_revision="persona-v1",
                persona_captured_at="2026-09-26T03:00:00+08:00",
                input_plan=plan,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["ready_to_generate"])
            self.assertIn("dayblock_source_coverage_incomplete", result["blocking_reasons"])
            self.assertIn("dayblock_source_coverage_not_exact", result["blocking_reasons"])
        finally:
            conn.close()

    def test_generation_plan_requires_every_raw_member(self):
        member = self._member(0, "turn:1:2")
        with self.assertRaisesRegex(RuntimeError, "generation_raw_materialization_incomplete"):
            dbs.build_generation_input_plan((member,), {}, "", 100)

    def test_canonical_text_attachment_is_included_in_materialized_evidence(self):
        text_source = Path(self.temp.name) / "text-source.db"
        conn = sqlite3.connect(text_source)
        conn.executescript("""
          CREATE TABLE chat_messages(
            id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, created_at TEXT,
            tool_calls TEXT, branches TEXT, branch_idx INTEGER, cache_info TEXT, source_kind TEXT,
            attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT
          );
          CREATE TABLE daily_message_contexts(message_id INTEGER, context_id INTEGER, context_epoch INTEGER);
          CREATE TABLE wake_log(wake_run_id TEXT, context_id INTEGER, context_epoch INTEGER, woke_at TEXT);
        """)
        rows = [
            _chat_row(1, "hayana", "Please read the attached note.", "2026-09-25 10:00:00",
                      attachments='[{"type":"file","url":"/static/uploads/files/abcdef12_note.txt","name":"note.txt"}]'),
            _chat_row(2, "fyodor", "I read the note.", "2026-09-25 10:01:00"),
        ]
        conn.executemany("INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO daily_message_contexts VALUES(?,?,?)", [(1,31,9),(2,31,9)])
        conn.commit()
        conn.close()
        static = Path(self.temp.name) / "static"
        upload_dir = static / "uploads" / "files"
        upload_dir.mkdir(parents=True)
        (upload_dir / "abcdef12_note.txt").write_text("canonical attachment text", encoding="utf-8")
        source_rows = dbs.discover_natural_day_rows(text_source, "2026-09-25")
        snapshot, turns, events, members = dbs.derive_source_contract(
            source_rows, "2026-09-25", "2026-09-26T03:00:00+08:00", static_dir=static,
        )
        materialized = dbs.materialize_raw_evidence(members, source_rows, static_dir=static)
        self.assertIn("canonical_text_extracted", materialized.body)
        self.assertIn("canonical attachment text", materialized.body)
        self.assertFalse(materialized.blockers)
        self.assertEqual(len(turns), 1)

    def test_unreadable_critical_attachment_blocks(self):
        conn = sqlite3.connect(self.source_db)
        conn.execute("INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     _chat_row(8, "hayana", "What color is in this image?",
                               "2026-09-25 14:47:55",
                               attachments='[{"type":"image","url":"/static/uploads/missing.png","name":"missing.png"}]'))
        conn.execute("INSERT INTO daily_message_contexts VALUES(?,?,?)", (8,31,9))
        conn.commit()
        conn.close()
        rows = dbs.discover_natural_day_rows(self.source_db, "2026-09-25")
        snapshot, turns, events, members = dbs.derive_source_contract(
            rows, "2026-09-25", "2026-09-26T03:00:00+08:00",
        )
        materialized = dbs.materialize_raw_evidence(members, rows)
        self.assertTrue(materialized.blockers)
        self.assertIn("dayblock_critical_attachment_content_unavailable", materialized.blockers)

    def test_persona_snapshot_hash_and_retry_remain_frozen(self):
        persona_file = Path(self.temp.name) / "persona.md"
        persona_file.write_text("persona v1", encoding="utf-8")
        first = dbs.capture_persona_snapshot(persona_file)
        self.assertEqual(first["revision"], "runtime_persona_sha256:" + hashlib.sha256(b"persona v1").hexdigest())
        persona_file.write_text("persona v2", encoding="utf-8")
        second = dbs.capture_persona_snapshot(persona_file)
        self.assertNotEqual(first["revision"], second["revision"])

    def test_frozen_model_budget_is_exact_identity_only(self):
        self.assertEqual(dbs.frozen_model_context_budget("claude_code", "explicit:claude-opus-5"), 872_000)
        self.assertEqual(dbs.frozen_model_context_budget("claude_code", "explicit:claude-opus-5-5"), 872_000)
        self.assertIsNone(dbs.frozen_model_context_budget("claude_code", "explicit:claude-opus-5-6"))
        self.assertIsNone(dbs.frozen_model_context_budget("api_relay", "explicit:claude-opus-5-5"))

    def test_authority_capture_must_match_scheduled_0300(self):
        self.assertTrue(dbs.scheduled_authority_capture_valid("2026-09-25", "2026-09-26T03:00:45+08:00"))
        self.assertFalse(dbs.scheduled_authority_capture_valid("2026-09-25", "2026-09-26T12:00:26+08:00"))

    def test_late_plan_reuses_frozen_authority_and_never_calls_current_capture(self):
        rows, snapshot, turns, events, members, materialized = make_inputs(self.source_db)
        conn = dbs.open_preview_store(self.preview_db)
        try:
            first = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T03:00:10+08:00",
                lambda: authority(),
            )
            frozen = dbs.AuthorityObservation(
                first["frozen_authority"]["provider"],
                first["frozen_authority"]["model_identity"],
                first["frozen_authority"]["authority_revision"],
                first["frozen_authority"]["authority_snapshot_id"],
            )
            replay = dbs.persist_shadow_job(
                conn, snapshot, turns, events, members, materialized, rows,
                candidate_source_revision(members), "2026-09-26T12:00:00+08:00",
                lambda: (_ for _ in ()).throw(AssertionError("current authority reread")),
                frozen_authority=frozen,
                frozen_authority_captured_at=first["frozen_authority"]["captured_at"],
            )
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["frozen_authority"], first["frozen_authority"])
        finally:
            conn.close()

    def test_model_call_counter_stays_zero_and_source_is_read_only(self):
        self.assertEqual(dbs.MODEL_CALL_COUNT, 0)
        conn = dbs.open_source_read_only(self.source_db)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE chat_messages SET content='mutated' WHERE id=1")
        finally:
            conn.close()

    def test_source_reader_is_query_only(self):
        conn = dbs.open_source_read_only(self.source_db)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_not_exist(id INTEGER)")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
