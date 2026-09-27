"""R1/R4 — Continuity Native-Fork eligibility resolver contracts.

Pure, read-only resolver tests. No model calls, no forking, no mutation of
sealing/Chunk/settings-authority state — this file only asserts what
``resolve_continuity_native_fork`` decides given durable DB rows.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from chat import daily_context as dc
from continuity import native_fork_eligibility as nfe
from continuity.contracts import ContinuityGenerationJob, SourceMember, SourceSnapshot
from continuity.sealing import CandidateBlock


def _job(**overrides) -> ContinuityGenerationJob:
    base = dict(
        generation_job_id='job:1',
        idempotency_key='idem:1',
        candidate_id='cand:1',
        snapshot_id='snap:1',
        candidate_source_revision='rev:1',
        generator_policy_version='continuity_chunk_generator_v1',
        prompt_policy_version='continuity_chunk_prompt_v2',
        measurement_semantics='cc_usage_observability.heuristic_cjk1_ascii4_v1',
        frozen_provider='claude_code',
        frozen_model_identity='claude-opus-5-5',
        status='generating',
        attempt=1,
        error_code=None,
        generation_id='gen:1',
        created_at='2026-09-08 04:00:00',
        updated_at='2026-09-08 04:00:00',
    )
    base.update(overrides)
    return ContinuityGenerationJob(**base)


def _candidate(*, source_refs, completed_turn_count=None, **overrides) -> CandidateBlock:
    base = dict(
        candidate_id='cand:1',
        snapshot_id='snap:1',
        policy_version='continuity_sealing_v1',
        block_seq=0,
        local_day='2026-09-08',
        branch_id='active-transcript',
        source_start_seq=0,
        source_end_seq=len(source_refs) - 1,
        source_seqs=tuple(range(len(source_refs))),
        source_refs=tuple(source_refs),
        source_revisions=tuple(f'rev-{i}' for i in range(len(source_refs))),
        logical_size=100,
        completed_turn_count=(completed_turn_count if completed_turn_count is not None else len(source_refs)),
        oversize=False,
        close_reason='target_reached',
        source_revision='candidate-rev',
    )
    base.update(overrides)
    return CandidateBlock(**base)


def _snapshot(*, context_id=1, context_epoch=1) -> SourceSnapshot:
    member = SourceMember(
        seq=0, source_kind='completed_turn', source_ref='turn:1:2',
        source_revision='rev-0', role='conversation', content_hash='rev-0',
        logical_size=10, created_at='2026-09-08 04:00:00', branch_id='active-transcript',
    )
    return SourceSnapshot(
        snapshot_id='snap:1', identity_id='fyodor', chat_id='default',
        branch_id='active-transcript', local_day='2026-09-08', source_watermark=2,
        policy_version='continuity_source_v1', source_hash='hash', status='ready',
        created_at='2026-09-08 04:00:00', members=(member,),
        context_id=context_id, context_epoch=context_epoch,
    )


class ContinuityNativeForkEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 't.db')
        self.cwd = os.path.join(self.tmp.name, 'proj')
        self.claude_home = os.path.join(self.tmp.name, 'claude')
        os.makedirs(self.cwd, exist_ok=True)
        os.makedirs(self.claude_home, exist_ok=True)
        dc.ensure_schema(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT, content TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )'''
        )
        self.conn.commit()

        self._flag_on = True
        self._flag_patch = mock.patch('config_store.get_bool', side_effect=self._get_bool)
        self._flag_patch.start()
        self.addCleanup(self._flag_patch.stop)

    def _get_bool(self, key, default=False):
        if key == nfe.FLAG_KEY:
            return self._flag_on
        return default

    def _registry(self, *, session, context_id=1, epoch=1, gen=1, scan_status='READY'):
        self.conn.execute(
            '''INSERT INTO context_claude_sessions (
                context_id, context_epoch, resident_generation, chat_id,
                claude_session_id, transcript_path, source, scan_status
            ) VALUES (?, ?, ?, 'default', ?, ?, 'test', ?)''',
            (context_id, epoch, gen, session, f'/tmp/{session}.jsonl', scan_status),
        )
        self.conn.commit()

    def _mapping(self, *, event_uuid, message_id, session, offset=0, context_id=1, epoch=1, gen=1, role='assistant'):
        self.conn.execute(
            '''INSERT INTO chat_message_claude_events (
                event_uuid, message_id, role, claude_session_id,
                context_id, context_epoch, resident_generation, jsonl_byte_offset
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (event_uuid, message_id, role, session, context_id, epoch, gen, offset),
        )
        self.conn.commit()

    def _capture(self, provider='claude_code', model='claude-opus-5-5'):
        snapshot = mock.Mock(provider=provider, model_identity=model)
        return lambda: snapshot

    def _resolve(self, *, job=None, candidate=None, snapshot=None, **kwargs):
        job = job or _job()
        candidate = candidate or _candidate(source_refs=['turn:1:2'])
        snapshot = snapshot or _snapshot()
        with mock.patch(
            'continuity.native_fork_eligibility.session_jsonl_path',
            side_effect=self._fake_jsonl_path,
        ):
            return nfe.resolve_continuity_native_fork(
                self.conn, job=job, candidate=candidate, snapshot=snapshot,
                cwd=self.cwd, claude_home=self.claude_home,
                capture_authority=self._capture(), **kwargs,
            )

    def _fake_jsonl_path(self, cwd, session_id, *, claude_home=None):
        from pathlib import Path
        path = Path(self.tmp.name) / f'{session_id}.jsonl'
        if not path.exists():
            path.write_bytes(b'{"type":"user"}\n')
        return path

    def test_flag_off_refuses(self):
        self._flag_on = False
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_FLAG_OFF)

    def test_happy_path_is_eligible(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        plan = self._resolve()
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.parent_session_id, 's1')
        self.assertEqual(plan.fork_event_uuid, 'ev1')
        self.assertEqual(plan.boundary_message_id, 2)
        self.assertEqual(plan.context_id, 1)
        self.assertEqual(plan.context_epoch, 1)

    def test_unfrozen_authority_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        job = _job(frozen_provider=None, frozen_model_identity=None)
        plan = self._resolve(job=job)
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_AUTHORITY_UNFROZEN)

    def test_api_relay_provider_is_never_eligible(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        job = _job(frozen_provider='api_relay', frozen_model_identity='some-model')
        plan = self._resolve(job=job)
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_UNSUPPORTED_PROVIDER)

    def test_live_model_mismatch_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        job = _job(frozen_provider='claude_code', frozen_model_identity='claude-opus-5-5')
        with mock.patch(
            'continuity.native_fork_eligibility.session_jsonl_path',
            side_effect=self._fake_jsonl_path,
        ):
            plan = nfe.resolve_continuity_native_fork(
                self.conn, job=job, candidate=_candidate(source_refs=['turn:1:2']),
                snapshot=_snapshot(), cwd=self.cwd, claude_home=self.claude_home,
                capture_authority=self._capture(model='claude-sonnet-5'),
            )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_MODEL_MISMATCH)

    def test_settings_change_after_freeze_still_falls_back_to_oneshot(self):
        """Model changes after this job's authority was frozen never retroactively
        make an old job eligible; frozen authority always wins for this job, and a
        live mismatch always means the existing one-shot, never a guess."""
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        job = _job(frozen_provider='claude_code', frozen_model_identity='claude-opus-5-5')
        with mock.patch(
            'continuity.native_fork_eligibility.session_jsonl_path',
            side_effect=self._fake_jsonl_path,
        ):
            plan = nfe.resolve_continuity_native_fork(
                self.conn, job=job, candidate=_candidate(source_refs=['turn:1:2']),
                snapshot=_snapshot(), cwd=self.cwd, claude_home=self.claude_home,
                capture_authority=self._capture(provider='claude_code', model='claude-sonnet-5'),
            )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_MODEL_MISMATCH)

    def test_missing_registry_row_refuses(self):
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SESSION_REGISTRY_MISSING)

    def test_blocked_scan_status_refuses(self):
        self._registry(session='s1', scan_status='BLOCKED')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SESSION_NOT_READY)

    def test_ambiguous_registry_multi_session_refuses(self):
        self._registry(session='s1', gen=1)
        self._registry(session='s2', gen=2)
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SESSION_REGISTRY_AMBIGUOUS)

    def test_missing_mapping_refuses(self):
        self._registry(session='s1')
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_MAPPING_MISSING)

    def test_mapping_session_mismatch_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s2')
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SESSION_MISMATCH)

    def test_mapping_epoch_mismatch_refuses(self):
        self._registry(session='s1', epoch=1)
        self._mapping(event_uuid='ev1', message_id=2, session='s1', epoch=2)
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SESSION_MISMATCH)

    def test_ambiguous_offset_tie_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1', offset=5)
        self._mapping(event_uuid='ev2', message_id=2, session='s1', offset=5)
        plan = self._resolve()
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_MAPPING_AMBIGUOUS)

    def test_autonomous_event_tail_is_unsupported_scope(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        candidate = _candidate(source_refs=['turn:1:2', 'wake:9'])
        plan = self._resolve(candidate=candidate)
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_SCOPE_UNSUPPORTED)

    def test_window_identity_missing_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        snapshot = SourceSnapshot(
            snapshot_id='snap:1', identity_id='fyodor', chat_id='default',
            branch_id='active-transcript', local_day='2026-09-08', source_watermark=2,
            policy_version='continuity_source_v1', source_hash='hash', status='ready',
            created_at='2026-09-08 04:00:00', members=(), context_id=None, context_epoch=None,
        )
        plan = self._resolve(snapshot=snapshot)
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_WINDOW_IDENTITY_MISSING)

    def test_missing_parent_transcript_refuses(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        job = _job()
        candidate = _candidate(source_refs=['turn:1:2'])
        snapshot = _snapshot()
        with mock.patch(
            'continuity.native_fork_eligibility.session_jsonl_path',
            return_value=None,
        ):
            plan = nfe.resolve_continuity_native_fork(
                self.conn, job=job, candidate=candidate, snapshot=snapshot,
                cwd=self.cwd, claude_home=self.claude_home,
                capture_authority=self._capture(),
            )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_PARENT_TRANSCRIPT_MISSING)

    def test_resolver_never_raises_on_internal_error(self):
        self._registry(session='s1')
        self._mapping(event_uuid='ev1', message_id=2, session='s1')
        with mock.patch(
            'continuity.native_fork_eligibility.session_jsonl_path',
            side_effect=RuntimeError('boom'),
        ):
            plan = nfe.resolve_continuity_native_fork(
                self.conn, job=_job(), candidate=_candidate(source_refs=['turn:1:2']),
                snapshot=_snapshot(), cwd=self.cwd, claude_home=self.claude_home,
                capture_authority=self._capture(),
            )
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, nfe.REASON_RESOLVER_ERROR)

    def test_observability_never_claims_native_mode_on_refusal(self):
        plan = self._resolve()
        obs = plan.observability()
        self.assertEqual(obs['continuity_generation_mode'], nfe.MODE_ONESHOT)
        self.assertEqual(obs['continuity_generation_fallback_reason'], nfe.REASON_SESSION_REGISTRY_MISSING)


if __name__ == '__main__':
    unittest.main()
