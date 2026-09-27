"""Production Continuity settings/generation integration contracts; no real provider calls."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

# Keep any optional config_store import made by the runtime gate isolated from
# the live memories.db during this test module.
os.environ.setdefault("HAYAGARDEN_CONFIG_DB_PATH", os.path.join(tempfile.gettempdir(), "continuity-r1-config-tests.db"))

import continuity.chunk_generation as generation
import continuity.store as store
from continuity.contracts import SourceMember, SourceSnapshot
from continuity.coverage import source_hash
from continuity.materialization import SourceMaterializationError
from continuity.sealing import DEFAULT_SEALING_POLICY
from continuity.settings import (
    SettingsAuthorityError,
    binding_for_revision,
    ensure_authority,
    load_authority,
    promote_pending_after_seal,
    save_revision,
    sealing_policy_for_revision,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

PERSONA = "\n".join(f"## {c}\nsection {c}" for c in "ABCDEF")
AUTHORITY = ("claude_code", "explicit:claude-opus-5-5")
SUMMARY = "今天我们确认了一个后续约定，并把未完成事项留给下次继续。"


class ContinuityProductionR1Tests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        store.ensure_schema(self.conn)
        authority = ensure_authority(
            self.conn, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
            persona_text=PERSONA, now="2026-09-26T00:00:00+00:00",
        )
        self.revision = authority["active_revision"]
        self.binding = binding_for_revision(self.conn, self.revision["revision_id"])
        self.policy = sealing_policy_for_revision(self.revision)
        self.snapshot, self.candidate, self.job = self._create_bound(
            "turn:1", "rev:1", "2026-09-25 12:00:00", 12000,
            self.binding, self.policy,
        )
        self.source = SimpleNamespace(
            body="source evidence",
            source_token_estimate=3000,
            source_fingerprint="source-fingerprint-1",
        )

    def tearDown(self):
        self.conn.close()

    def _create_bound(self, ref, revision, created_at, size, binding, policy):
        member = SourceMember(
            0, "completed_turn", ref, revision, "user", "content-hash-" + revision,
            logical_size=size, created_at=created_at, branch_id="active-transcript",
        )
        snapshot = SourceSnapshot(
            "source:" + ref, "identity", "default", "active-transcript",
            created_at[:10], 1, "continuity_source_v1", source_hash((member,)),
            "ready", created_at + "+00:00", (member,), 30, 28,
        )
        store.save_source_snapshot(self.conn, snapshot)
        sealing_job = store.enqueue_job(self.conn, snapshot, policy, now="2026-09-26T00:00:00+00:00")
        candidates = store.materialize_job(
            self.conn, sealing_job.job_id, policy, include_end_of_snapshot=True,
            settings_binding=binding, now="2026-09-26T00:00:00+00:00",
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        job = store.enqueue_generation_job(
            self.conn, candidate, snapshot,
            generator_policy_version=generation.GENERATOR_POLICY_VERSION,
            prompt_policy_version=generation.PROMPT_POLICY_VERSION,
            measurement_semantics=str(binding["measurement_semantics"]),
            settings_binding=binding, now="2026-09-26T00:00:00+00:00",
        )
        return snapshot, candidate, job

    def _run(self, *, job=None, result=None, generate_fn=None, materialized=None):
        job = job or self.job
        result = result or SimpleNamespace(
            text=SUMMARY, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
            actual_executor="fake_provider_once",
            usage={
                "input_tokens": 600,
                "output_tokens": 25,
                "cache_creation_input_tokens": 120,
                "cache_read_input_tokens": 300,
            },
        )
        fake_generate = generate_fn or Mock(return_value=result)
        fake_request = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        source = materialized or self.source
        with patch.object(generation, "materialize_candidate", return_value=source):
            chunk = generation.generate_continuity_chunk(
                self.conn, job.generation_job_id, rows_provider=(),
                generate_fn=fake_generate, request_factory=fake_request,
                now="2026-09-26T00:01:00+00:00",
            )
        return chunk, fake_generate, fake_request

    def _counts_and_fingerprint(self, table):
        columns = [r[1] for r in self.conn.execute(f'PRAGMA table_info("{table}")')]
        rows = self.conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
        encoded = json.dumps([tuple(r) for r in rows], default=str, separators=(",", ":"))
        return len(rows), hashlib.sha256(encoded.encode()).hexdigest()

    def test_schema_additive_migration_preserves_legacy_generation_job(self):
        legacy = sqlite3.connect(":memory:")
        legacy.row_factory = sqlite3.Row
        legacy.execute("""
          CREATE TABLE continuity_generation_jobs(
            generation_job_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,
            candidate_id TEXT NOT NULL,snapshot_id TEXT NOT NULL,candidate_source_revision TEXT NOT NULL,
            generator_policy_version TEXT NOT NULL,prompt_policy_version TEXT NOT NULL,
            measurement_semantics TEXT NOT NULL,frozen_provider TEXT,frozen_model_identity TEXT,
            status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,error_code TEXT,generation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL
          )
        """)
        original = ("legacy-job", "legacy-idem", "legacy-candidate", "legacy-source", "legacy-rev",
                    "legacy-generator", "legacy-prompt", "legacy-measurement", "claude_code",
                    "explicit:claude-opus-4-6", "ready", 1, None, "legacy-generation", "created", "updated")
        legacy.execute("INSERT INTO continuity_generation_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", original)
        store.ensure_schema(legacy)
        loaded = store.load_generation_job(legacy, "legacy-job")
        self.assertEqual(tuple(legacy.execute(
            "SELECT generation_job_id,idempotency_key,candidate_id,snapshot_id,candidate_source_revision,"
            "generator_policy_version,prompt_policy_version,measurement_semantics,frozen_provider,"
            "frozen_model_identity,status,attempt,error_code,generation_id,created_at,updated_at "
            "FROM continuity_generation_jobs WHERE generation_job_id='legacy-job'"
        ).fetchone()), original)
        self.assertIsNone(loaded.settings_revision_id)
        self.assertIsNone(loaded.frozen_prompt_body)
        legacy.close()

    def test_settings_authority_has_single_baseline_active_revision(self):
        authority = load_authority(self.conn)
        self.assertEqual(authority["active_revision"]["revision_id"], "continuity-settings-baseline-v1")
        self.assertIsNone(authority["pending_revision"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_settings_authority").fetchone()[0], 1)
        self.assertEqual(authority["active_revision"]["target_logical_size"], 12000)
        self.assertEqual(authority["active_revision"]["max_completed_turns"], 20)

    def test_open_block_save_stays_pending_and_active_does_not_move(self):
        before = load_authority(self.conn)["active_revision"]["revision_id"]
        authority = save_revision(
            self.conn,
            {"length": 15000, "turns": 24, "provider": AUTHORITY[0],
             "model_identity": "explicit:claude-opus-4-6", "prompt_body": "new frozen prompt"},
            current_block={"available": True, "unclaimed_source_count": 1,
                           "source_refs": [self.candidate.source_refs[0]],
                           "source_revisions": [self.candidate.source_revisions[0]],
                           "context_id": 30, "context_epoch": 28},
            persona_text=PERSONA, now="2026-09-26T00:02:00+00:00",
        )
        self.assertEqual(authority["active_revision"]["revision_id"], before)
        self.assertEqual(authority["pending_revision"]["target_logical_size"], 15000)
        self.assertEqual(authority["pending_anchor"]["active_revision_id"], before)

    def test_empty_open_block_save_becomes_active_immediately(self):
        authority = save_revision(
            self.conn,
            {"length": 14000, "turns": 22, "provider": AUTHORITY[0],
             "model_identity": AUTHORITY[1], "prompt_body": "new prompt"},
            current_block={"available": True, "unclaimed_source_count": 0,
                           "source_refs": [], "source_revisions": []},
            persona_text=PERSONA,
        )
        self.assertEqual(authority["active_revision"]["target_logical_size"], 14000)
        self.assertIsNone(authority["pending_revision"])

    def test_seal_promotes_pending_once_and_receipt_is_idempotent(self):
        pending = save_revision(
            self.conn,
            {"length": 15000, "turns": 24, "provider": AUTHORITY[0],
             "model_identity": AUTHORITY[1], "prompt_body": "next block prompt"},
            current_block={"available": True, "unclaimed_source_count": 1,
                           "source_refs": list(self.candidate.source_refs),
                           "source_revisions": list(self.candidate.source_revisions)},
            persona_text=PERSONA,
        )["pending_revision"]
        first = promote_pending_after_seal(
            self.conn, candidate_id=self.candidate.candidate_id,
            generation_job_id=self.job.generation_job_id, now="2026-09-26T00:03:00+00:00",
        )
        second = promote_pending_after_seal(
            self.conn, candidate_id=self.candidate.candidate_id,
            generation_job_id=self.job.generation_job_id, now="2026-09-26T00:04:00+00:00",
        )
        self.assertEqual(first["active_revision_id"], pending["revision_id"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_settings_promotion_receipts").fetchone()[0], 1)

    def test_next_candidate_uses_promoted_revision(self):
        pending = save_revision(
            self.conn,
            {"length": 15000, "turns": 24, "provider": AUTHORITY[0],
             "model_identity": AUTHORITY[1], "prompt_body": "next block prompt"},
            current_block={"available": True, "unclaimed_source_count": 1,
                           "source_refs": list(self.candidate.source_refs),
                           "source_revisions": list(self.candidate.source_revisions)},
            persona_text=PERSONA,
        )["pending_revision"]
        promote_pending_after_seal(
            self.conn, candidate_id=self.candidate.candidate_id,
            generation_job_id=self.job.generation_job_id,
        )
        binding = binding_for_revision(self.conn, pending["revision_id"])
        policy = sealing_policy_for_revision(pending)
        _snapshot, candidate, job = self._create_bound(
            "turn:2", "rev:2", "2026-09-25 13:00:00", 15000, binding, policy,
        )
        self.assertEqual(candidate.settings_revision_id, pending["revision_id"])
        self.assertEqual(candidate.target_logical_size, 15000)
        self.assertEqual(job.settings_revision_id, pending["revision_id"])

    def test_candidate_membership_is_exact_and_ordered(self):
        rows = self.conn.execute(
            "SELECT source_ref,source_revision FROM continuity_candidate_members "
            "WHERE candidate_id=? ORDER BY ordinal", (self.candidate.candidate_id,),
        ).fetchall()
        self.assertEqual(tuple((r[0], r[1]) for r in rows),
                         tuple(zip(self.candidate.source_refs, self.candidate.source_revisions)))
        self.assertEqual(len(rows), len(self.snapshot.members))

    def test_generation_job_freezes_complete_revision_binding(self):
        self.assertEqual(self.job.settings_revision_id, self.binding["settings_revision_id"])
        self.assertEqual(self.job.frozen_provider, self.binding["provider"])
        self.assertEqual(self.job.frozen_model_identity, self.binding["model_identity"])
        self.assertEqual(self.job.frozen_prompt_body, self.binding["prompt_body"])
        self.assertEqual(self.job.frozen_prompt_hash, self.binding["prompt_hash"])
        self.assertEqual(self.job.frozen_prompt_revision, self.binding["prompt_revision"])
        self.assertEqual(self.job.frozen_persona_body, self.binding["persona_body"])
        self.assertEqual(self.job.frozen_persona_hash, self.binding["persona_hash"])
        self.assertEqual(self.job.frozen_persona_revision, self.binding["persona_revision"])

    def test_active_settings_change_does_not_change_old_job(self):
        old_prompt = self.job.frozen_prompt_body
        save_revision(
            self.conn,
            {"length": 16000, "turns": 30, "provider": AUTHORITY[0],
             "model_identity": "explicit:claude-opus-4-6", "prompt_body": "current r3 prompt"},
            current_block={"available": True, "unclaimed_source_count": 0,
                           "source_refs": [], "source_revisions": []},
            persona_text=PERSONA,
        )
        stored = store.load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(stored.settings_revision_id, self.binding["settings_revision_id"])
        self.assertEqual(stored.frozen_prompt_body, old_prompt)
        self.assertEqual(stored.frozen_model_identity, AUTHORITY[1])

    def test_generation_uses_only_frozen_provider_and_model(self):
        seen = []
        def generate(request, authority):
            seen.append((authority.provider, authority.model_identity))
            return SimpleNamespace(text=SUMMARY, provider=authority.provider,
                                   model_identity=authority.model_identity,
                                   actual_executor="fake", usage=None)
        self._run(generate_fn=generate)
        self.assertEqual(seen, [AUTHORITY])

    def test_provider_error_fails_once_without_fallback_or_chunk(self):
        calls = []
        def fail_once(request, authority):
            calls.append((authority.provider, authority.model_identity))
            raise RuntimeError("provider timeout")
        with patch.object(generation, "materialize_candidate", return_value=self.source):
            with self.assertRaises(RuntimeError):
                generation.generate_continuity_chunk(
                    self.conn, self.job.generation_job_id, rows_provider=(),
                    generate_fn=fail_once, request_factory=lambda **kw: SimpleNamespace(**kw),
                )
        job = store.load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], AUTHORITY)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_code, "generation_error")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_chunks").fetchone()[0], 0)

    def test_ready_replay_returns_existing_chunk_with_zero_calls(self):
        first, _, _ = self._run()
        second, fake_generate, _ = self._run()
        self.assertEqual(first.chunk_id, second.chunk_id)
        fake_generate.assert_not_called()
        self.assertEqual(store.load_generation_job(self.conn, self.job.generation_job_id).status, "ready")

    def test_prompt_hash_mismatch_fails_before_model_call(self):
        tampered = replace(self.job, frozen_prompt_hash="0" * 64)
        fake = Mock()
        with patch.object(generation, "load_generation_job", return_value=tampered):
            with self.assertRaises(Exception):
                generation.generate_continuity_chunk(
                    self.conn, self.job.generation_job_id, rows_provider=(),
                    generate_fn=fake, request_factory=lambda **kw: SimpleNamespace(**kw),
                )
        fake.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_chunks").fetchone()[0], 0)

    def test_frozen_settings_revision_mismatch_fails_before_model_call(self):
        fake = Mock()
        tampered = replace(self.job, settings_revision_id="missing-revision")
        with patch.object(generation, "load_generation_job", return_value=tampered):
            with self.assertRaises(Exception):
                generation.generate_continuity_chunk(
                    self.conn, self.job.generation_job_id, rows_provider=(),
                    generate_fn=fake, request_factory=lambda **kw: SimpleNamespace(**kw),
                )
        fake.assert_not_called()

    def test_stale_source_before_generation_has_zero_calls_and_no_chunk(self):
        fake = Mock()
        with patch.object(generation, "materialize_candidate",
                          side_effect=SourceMaterializationError("source stale")):
            with self.assertRaises(SourceMaterializationError):
                generation.generate_continuity_chunk(
                    self.conn, self.job.generation_job_id, rows_provider=(),
                    generate_fn=fake, request_factory=lambda **kw: SimpleNamespace(**kw),
                )
        fake.assert_not_called()
        self.assertEqual(store.load_generation_job(self.conn, self.job.generation_job_id).status, "stale")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_chunks").fetchone()[0], 0)

    def test_source_changes_during_call_prevent_ready_chunk(self):
        calls = Mock(return_value=SimpleNamespace(
            text=SUMMARY, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
            actual_executor="fake", usage=None,
        ))
        changed = SimpleNamespace(**{**vars(self.source), "source_fingerprint": "source-fingerprint-2"})
        with patch.object(generation, "materialize_candidate", side_effect=[self.source, changed]):
            with self.assertRaises(SourceMaterializationError):
                generation.generate_continuity_chunk(
                    self.conn, self.job.generation_job_id, rows_provider=(),
                    generate_fn=calls, request_factory=lambda **kw: SimpleNamespace(**kw),
                )
        calls.assert_called_once()
        self.assertEqual(store.load_generation_job(self.conn, self.job.generation_job_id).status, "stale")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_chunks").fetchone()[0], 0)

    def test_empty_and_transport_garbage_outputs_fail_without_chunk(self):
        for output in ("", "ERROR: upstream unavailable", "bad\x00transport"):
            with self.subTest(output=output):
                snapshot, candidate, job = self._create_bound(
                    "turn:" + str(len(output)) + str(self.conn.total_changes),
                    "rev:" + str(self.conn.total_changes),
                    "2026-09-25 14:00:00", 12000, self.binding, self.policy,
                )
                fake = Mock(return_value=SimpleNamespace(
                    text=output, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
                    actual_executor="fake", usage=None,
                ))
                with patch.object(generation, "materialize_candidate", return_value=self.source):
                    with self.assertRaises(ValueError):
                        generation.generate_continuity_chunk(
                            self.conn, job.generation_job_id, rows_provider=(),
                            generate_fn=fake, request_factory=lambda **kw: SimpleNamespace(**kw),
                        )
                fake.assert_called_once()
                self.assertEqual(store.load_generation_job(self.conn, job.generation_job_id).status, "failed")
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM continuity_chunks WHERE generation_job_id=?",
                    (job.generation_job_id,),
                ).fetchone()[0], 0)

    def test_success_publishes_exactly_one_immutable_chunk_and_ready_job(self):
        chunk, fake, _ = self._run()
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(chunk.status, "ready")
        self.assertEqual(store.load_generation_job(self.conn, self.job.generation_job_id).status, "ready")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM continuity_chunks WHERE generation_job_id=?",
            (self.job.generation_job_id,),
        ).fetchone()[0], 1)

    def test_chunk_has_full_candidate_job_settings_prompt_persona_provenance(self):
        chunk, _, _ = self._run()
        self.assertEqual(chunk.generation_job_id, self.job.generation_job_id)
        self.assertEqual(chunk.candidate_id, self.candidate.candidate_id)
        self.assertEqual(chunk.settings_revision_id, self.binding["settings_revision_id"])
        self.assertEqual(chunk.prompt_hash, self.binding["prompt_hash"])
        self.assertEqual(chunk.prompt_revision, self.binding["prompt_revision"])
        self.assertEqual(chunk.persona_revision, self.binding["persona_revision"])
        self.assertEqual(hashlib.sha256(chunk.body.encode()).hexdigest(), chunk.body_hash)

    def test_enqueued_generation_authority_cannot_be_rewritten(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE continuity_generation_jobs SET frozen_model_identity=? WHERE generation_job_id=?",
                ("explicit:claude-opus-4-6", self.job.generation_job_id),
            )
        self.conn.rollback()
        persisted = store.load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(persisted.frozen_provider, AUTHORITY[0])
        self.assertEqual(persisted.frozen_model_identity, AUTHORITY[1])

    def test_chunk_body_and_frozen_provenance_are_immutable(self):
        chunk, _, _ = self._run()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE continuity_chunks SET body='rewrite' WHERE chunk_id=?", (chunk.chunk_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE continuity_chunks SET settings_revision_id='other' WHERE chunk_id=?", (chunk.chunk_id,))

    def test_provider_usage_and_cache_fields_are_recorded_without_estimation(self):
        chunk, _, _ = self._run()
        self.assertEqual(chunk.input_tokens, 600)
        self.assertEqual(chunk.provider_output_tokens, 25)
        self.assertEqual(chunk.cache_creation_input_tokens, 120)
        self.assertEqual(chunk.cache_read_input_tokens, 300)
        self.assertTrue(chunk.cache_hit)
        self.assertEqual(chunk.cache_usage_status, "available")
        self.assertEqual(chunk.usage["output_tokens"], 25)

    def test_missing_cache_usage_is_unavailable_not_estimated(self):
        result = SimpleNamespace(
            text=SUMMARY, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
            actual_executor="fake", usage={"input_tokens": 10},
        )
        chunk, _, _ = self._run(result=result)
        self.assertIsNone(chunk.cache_creation_input_tokens)
        self.assertIsNone(chunk.cache_read_input_tokens)
        self.assertIsNone(chunk.cache_hit)
        self.assertEqual(chunk.cache_usage_status, "partial")

    def test_cache_prefix_identity_ignores_candidate_and_source(self):
        first = generation._usage_diagnostics(SimpleNamespace(usage=None), self.job, self.binding)
        other_job = replace(self.job, candidate_id="different", snapshot_id="different",
                            generation_job_id="different", generation_id="different")
        second = generation._usage_diagnostics(SimpleNamespace(usage=None), other_job, self.binding)
        self.assertEqual(first["cache_prefix_identity"], second["cache_prefix_identity"])

    def test_cache_prefix_identity_changes_with_prompt_persona_and_model(self):
        base = generation._usage_diagnostics(SimpleNamespace(usage=None), self.job, self.binding)["cache_prefix_identity"]
        changed_prompt = dict(self.binding, prompt_hash="prompt-changed", prompt_revision="prompt-new")
        changed_persona = dict(self.binding, persona_revision="persona:new", persona_hash="persona-hash-new")
        changed_model_job = replace(self.job, frozen_model_identity="explicit:claude-opus-4-6")
        self.assertNotEqual(base, generation._usage_diagnostics(SimpleNamespace(usage=None), self.job, changed_prompt)["cache_prefix_identity"])
        self.assertNotEqual(base, generation._usage_diagnostics(SimpleNamespace(usage=None), self.job, changed_persona)["cache_prefix_identity"])
        self.assertNotEqual(base, generation._usage_diagnostics(SimpleNamespace(usage=None), changed_model_job, self.binding)["cache_prefix_identity"])

    def test_generation_does_not_change_chat_resident_or_context_identity(self):
        self.conn.executescript("""
          CREATE TABLE chat_messages(id INTEGER PRIMARY KEY,content TEXT);
          CREATE TABLE daily_resident_cursors(context_id INTEGER,resident_generation INTEGER,history_cursor_message_id INTEGER);
          CREATE TABLE context_receipts(context_id INTEGER,context_epoch INTEGER,receipt_revision INTEGER);
          INSERT INTO chat_messages VALUES(1,'chat sentinel');
          INSERT INTO daily_resident_cursors VALUES(30,45,10087);
          INSERT INTO context_receipts VALUES(30,28,1);
        """)
        before = {t: self._counts_and_fingerprint(t) for t in ("chat_messages","daily_resident_cursors","context_receipts")}
        self._run()
        after = {t: self._counts_and_fingerprint(t) for t in before}
        self.assertEqual(before, after)

    def test_context_plan_consumer_gate_defaults_off_and_fails_closed(self):
        fake_config = ModuleType("config_store")
        fake_config.get = lambda key, default=None: default
        with patch.dict(sys.modules, {"config_store": fake_config}):
            runtime = importlib.import_module("chat.daily_runtime")
            self.assertFalse(runtime._context_plan_consumer_enabled())
        fake_config.get = Mock(side_effect=RuntimeError("unavailable"))
        with patch.dict(sys.modules, {"config_store": fake_config}):
            self.assertFalse(runtime._context_plan_consumer_enabled())

    def test_legacy_ready_chunk_remains_readable_without_retroactive_binding(self):
        member = SourceMember(
            0, "completed_turn", "turn:legacy", "legacy-rev", "user", "legacy-hash",
            logical_size=12000, created_at="2026-09-24 10:00:00", branch_id="active-transcript",
        )
        snapshot = SourceSnapshot(
            "source:legacy", "identity", "default", "active-transcript", "2026-09-24",
            2, "continuity_source_v1", source_hash((member,)), "ready",
            "2026-09-24T10:00:00+00:00", (member,), 30, 28,
        )
        store.save_source_snapshot(self.conn, snapshot)
        sealing = store.enqueue_job(self.conn, snapshot, DEFAULT_SEALING_POLICY)
        candidates = store.materialize_job(self.conn, sealing.job_id, DEFAULT_SEALING_POLICY, include_end_of_snapshot=True)
        candidate = candidates[0]
        job = store.enqueue_generation_job(
            self.conn, candidate, snapshot,
            generator_policy_version=generation.GENERATOR_POLICY_VERSION,
            prompt_policy_version=generation.PROMPT_POLICY_VERSION,
            measurement_semantics=generation.MEASUREMENT_SEMANTICS,
        )
        fake_result = SimpleNamespace(text=SUMMARY, provider=AUTHORITY[0], model_identity=AUTHORITY[1],
                                      actual_executor="legacy-fake", usage=None)
        with patch.object(generation, "materialize_candidate", return_value=self.source):
            chunk = generation.generate_continuity_chunk(
                self.conn, job.generation_job_id, rows_provider=(),
                capture_authority=lambda: SimpleNamespace(provider=AUTHORITY[0], model_identity=AUTHORITY[1]),
                generate_fn=lambda req, auth: fake_result,
                request_factory=lambda **kw: SimpleNamespace(**kw),
            )
        self.assertIsNone(job.settings_revision_id)
        self.assertIsNone(chunk.settings_revision_id)
        self.assertEqual(store._read_ready_surface_connection(self.conn).status, "ready")

    def test_unknown_provider_is_rejected_without_cross_provider_fallback(self):
        from chat.background_generation import BackgroundGenerationError, BackgroundGenerationRequest, generate_background
        relay = Mock()
        with self.assertRaises(BackgroundGenerationError):
            generate_background(
                BackgroundGenerationRequest("system", "tail", 100, 1),
                SimpleNamespace(provider="deepseek", model_identity="deepseek-chat"),
                relay_factory=relay,
            )
        relay.assert_not_called()

    def test_only_one_authority_and_no_result_cache_tables(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM continuity_settings_authority").fetchone()[0], 1)
        names = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("continuity_summary_cache", names)
        self.assertNotIn("continuity_result_cache", names)


if __name__ == "__main__":
    unittest.main()
