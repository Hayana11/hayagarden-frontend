"""Continuity Native-Fork eligibility resolver contracts.

Read-only resolver tests over durable, cross-process evidence only: the
canonical current window, the session registry, the exact-boundary mapping,
and the parent transcript JSONL the provider wrote. No model calls, no fork.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chat import daily_context as dc
from continuity import native_fork_eligibility as nfe
from continuity.contracts import ContinuityGenerationJob, SourceMember, SourceSnapshot
from continuity.sealing import CandidateBlock
from tools.cc_jsonl_usage import session_jsonl_path

MODEL = 'claude-opus-5-5'
NOW = dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.timezone.utc).timestamp()


def _iso(seconds_ago: float) -> str:
    moment = dt.datetime.fromtimestamp(NOW - seconds_ago, tz=dt.timezone.utc)
    return moment.isoformat().replace('+00:00', 'Z')


def _job(**overrides) -> ContinuityGenerationJob:
    base = dict(
        generation_job_id='job:1', idempotency_key='idem:1', candidate_id='cand:1',
        snapshot_id='snap:1', candidate_source_revision='rev:1',
        generator_policy_version='continuity_chunk_generator_v1',
        prompt_policy_version='continuity_chunk_prompt_v2',
        measurement_semantics='cc_usage_observability.heuristic_cjk1_ascii4_v1',
        frozen_provider='claude_code', frozen_model_identity=f'explicit:{MODEL}',
        status='generating', attempt=1, error_code=None, generation_id='gen:1',
        created_at='2026-09-27 04:00:00', updated_at='2026-09-27 04:00:00',
    )
    base.update(overrides)
    return ContinuityGenerationJob(**base)


def _candidate(source_refs=('turn:1:2',), **overrides) -> CandidateBlock:
    refs = tuple(source_refs)
    base = dict(
        candidate_id='cand:1', snapshot_id='snap:1', policy_version='sealing_v1',
        block_seq=0, local_day='2026-09-27', branch_id='active-transcript',
        source_start_seq=0, source_end_seq=len(refs) - 1,
        source_seqs=tuple(range(len(refs))), source_refs=refs,
        source_revisions=tuple(f'rev-{i}' for i in range(len(refs))),
        logical_size=100, completed_turn_count=len(refs), oversize=False,
        close_reason='target_reached', source_revision='candidate-rev',
    )
    base.update(overrides)
    return CandidateBlock(**base)


def _snapshot(*, context_id=1, context_epoch=1) -> SourceSnapshot:
    member = SourceMember(
        seq=0, source_kind='completed_turn', source_ref='turn:1:2',
        source_revision='rev-0', role='conversation', content_hash='rev-0',
        logical_size=10, created_at='2026-09-27 04:00:00', branch_id='active-transcript',
    )
    return SourceSnapshot(
        snapshot_id='snap:1', identity_id='fyodor', chat_id='default',
        branch_id='active-transcript', local_day='2026-09-27', source_watermark=2,
        policy_version='continuity_source_v1', source_hash='hash', status='ready',
        created_at='2026-09-27 04:00:00', members=(member,),
        context_id=context_id, context_epoch=context_epoch,
    )


def assistant_event(uuid: str, *, model: str = MODEL, seconds_ago: float = 60.0, request_id: str | None = None) -> dict:
    return {
        'type': 'assistant',
        'uuid': uuid,
        'requestId': request_id or f'req-{uuid}',
        'timestamp': _iso(seconds_ago),
        'message': {
            'model': model,
            'usage': {'input_tokens': 3, 'output_tokens': 5, 'cache_read_input_tokens': 90000},
        },
    }


class ContinuityNativeForkEligibilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 't.db')
        self.cwd = os.path.join(self.tmp.name, 'proj')
        self.claude_home = os.path.join(self.tmp.name, 'claude')
        os.makedirs(self.cwd, exist_ok=True)
        dc.ensure_schema(self.db)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row

        self._flag_on = True
        flag = mock.patch('config_store.get_bool', side_effect=self._get_bool)
        flag.start()
        self.addCleanup(flag.stop)

        self.current = {'chat_id': 'default', 'context_id': 1, 'context_epoch': 1, 'resident_generation': 1}
        window = mock.patch(
            'chat.window_identity.read_current_window_identity_conn',
            side_effect=lambda _conn, chat_id='default': dict(self.current),
        )
        window.start()
        self.addCleanup(window.stop)

    def tearDown(self):
        self.conn.close()

    def _get_bool(self, key, default=False):
        if key == nfe.FLAG_KEY:
            return self._flag_on
        return default

    def _parent_path(self, session: str, *, cwd: str | None = None) -> Path:
        return session_jsonl_path(cwd or self.cwd, session, claude_home=self.claude_home)

    def _write_parent(self, session: str, events: list[dict], *, cwd: str | None = None) -> Path:
        parent_cwd = cwd or self.cwd
        path = self._parent_path(session, cwd=parent_cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for event in events:
            row = dict(event)
            row.setdefault('cwd', parent_cwd)
            rows.append(row)
        lines = [json.dumps({'type': 'user', 'uuid': 'u0'})] + [json.dumps(e) for e in rows]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def _registry(self, *, session='s1', context_id=1, epoch=1, gen=1, scan_status='READY', transcript_path=None):
        path = transcript_path if transcript_path is not None else str(self._parent_path(session))
        self.conn.execute(
            '''INSERT INTO context_claude_sessions (
                context_id, context_epoch, resident_generation, chat_id,
                claude_session_id, transcript_path, source, scan_status
            ) VALUES (?, ?, ?, 'default', ?, ?, 'test', ?)''',
            (context_id, epoch, gen, session, path, scan_status),
        )
        self.conn.commit()

    def _mapping(self, *, event_uuid='ev1', message_id=2, session='s1', offset=0, context_id=1, epoch=1, gen=1):
        self.conn.execute(
            '''INSERT INTO chat_message_claude_events (
                event_uuid, message_id, role, claude_session_id,
                context_id, context_epoch, resident_generation, jsonl_byte_offset
            ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?)''',
            (event_uuid, message_id, session, context_id, epoch, gen, offset),
        )
        self.conn.commit()

    def _hot_parent(self, **event_kwargs):
        self._write_parent('s1', [assistant_event('ev1', **event_kwargs)])
        self._registry()
        self._mapping()

    def _resolve(self, *, job=None, candidate=None, snapshot=None, source_conn='default', cwd=None):
        return nfe.resolve_continuity_native_fork(
            self.conn if source_conn == 'default' else source_conn,
            job=job or _job(), candidate=candidate or _candidate(), snapshot=snapshot or _snapshot(),
            cwd=cwd or self.cwd, claude_home=self.claude_home, now_wall=NOW,
        )

    def assertRefused(self, plan, reason):
        self.assertFalse(plan.eligible)
        self.assertEqual(plan.reason, reason)

    def test_hot_same_model_exact_parent_is_eligible(self):
        self._hot_parent()
        plan = self._resolve()
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.parent_session_id, 's1')
        self.assertEqual(plan.fork_event_uuid, 'ev1')
        self.assertEqual(plan.parent_model, MODEL)
        self.assertEqual(plan.resident_generation, 1)
        self.assertEqual(plan.parent_transcript_path, str(self._parent_path('s1')))
        self.assertEqual(plan.parent_cwd, self.cwd)

    def test_attested_parent_cwd_allows_producer_cwd_to_differ(self):
        parent_cwd = os.path.join(self.tmp.name, 'parent-project')
        parent_path = self._write_parent('s1', [assistant_event('ev1')], cwd=parent_cwd)
        self._registry(transcript_path=str(parent_path))
        self._mapping()
        plan = self._resolve(cwd=self.cwd)
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.parent_cwd, parent_cwd)
        self.assertEqual(plan.parent_transcript_path, str(parent_path))

    def test_flag_off_refuses(self):
        self._flag_on = False
        self._hot_parent()
        self.assertRefused(self._resolve(), nfe.REASON_FLAG_OFF)

    def test_unfrozen_authority_refuses(self):
        self._hot_parent()
        self.assertRefused(
            self._resolve(job=_job(frozen_provider=None, frozen_model_identity=None)),
            nfe.REASON_AUTHORITY_UNFROZEN,
        )

    def test_api_relay_job_is_never_eligible(self):
        self._hot_parent()
        self.assertRefused(
            self._resolve(job=_job(frozen_provider='api_relay', frozen_model_identity='x')),
            nfe.REASON_UNSUPPORTED_PROVIDER,
        )

    def test_default_model_identity_cannot_be_attested(self):
        self._hot_parent()
        self.assertRefused(
            self._resolve(job=_job(frozen_model_identity='default')),
            nfe.REASON_MODEL_UNATTESTABLE,
        )

    def test_different_compression_model_uses_oneshot(self):
        """Main chat on Opus 5.5, compression job frozen to Sonnet 5 → one-shot."""
        self._hot_parent(model='claude-opus-5-5')
        self.assertRefused(
            self._resolve(job=_job(frozen_model_identity='explicit:claude-sonnet-5')),
            nfe.REASON_PARENT_MODEL_MISMATCH,
        )

    def test_settings_page_model_is_not_live_proof(self):
        """Config says the job's model, but the provider-written parent says otherwise."""
        self._hot_parent(model='claude-sonnet-5')
        with mock.patch(
            'chat.provider_router.capture_generation_authority',
            return_value=mock.Mock(provider='claude_code', model_identity=f'explicit:{MODEL}'),
        ):
            self.assertRefused(self._resolve(), nfe.REASON_PARENT_MODEL_MISMATCH)

    def test_parent_switched_model_after_boundary_refuses(self):
        self._write_parent('s1', [
            assistant_event('ev1', seconds_ago=600),
            assistant_event('ev2', model='claude-sonnet-5', seconds_ago=60),
        ])
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_PARENT_MODEL_MISMATCH)

    def test_cold_parent_refuses(self):
        from cc_resident import STALE_CACHE_MAX_AGE_SECONDS

        self._hot_parent(seconds_ago=STALE_CACHE_MAX_AGE_SECONDS + 1)
        self.assertRefused(self._resolve(), nfe.REASON_PARENT_COLD)

    def test_future_timestamp_beyond_skew_refuses(self):
        self._hot_parent(seconds_ago=-600)
        self.assertRefused(self._resolve(), nfe.REASON_PARENT_COLD)

    def test_parent_without_usage_records_refuses(self):
        event = assistant_event('ev1')
        event.pop('requestId')
        self._write_parent('s1', [event])
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_PARENT_USAGE_MISSING)

    def test_boundary_event_absent_from_transcript_refuses(self):
        self._write_parent('s1', [assistant_event('other')])
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_BOUNDARY_NOT_IN_TRANSCRIPT)

    def test_source_connection_unavailable_refuses(self):
        self._hot_parent()
        self.assertRefused(self._resolve(source_conn=None), nfe.REASON_SOURCE_UNAVAILABLE)

    def test_snapshot_not_current_window_refuses(self):
        self._hot_parent()
        self.current = {**self.current, 'context_epoch': 2}
        self.assertRefused(self._resolve(), nfe.REASON_NOT_CURRENT_WINDOW)

    def test_no_open_window_refuses(self):
        from chat.window_identity import WindowIdentityUnavailable

        self._hot_parent()
        with mock.patch(
            'chat.window_identity.read_current_window_identity_conn',
            side_effect=WindowIdentityUnavailable('no_open_context_window'),
        ):
            self.assertRefused(self._resolve(), nfe.REASON_NOT_CURRENT_WINDOW)

    def test_registry_for_older_generation_only_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry(gen=1)
        self._mapping()
        self.current = {**self.current, 'resident_generation': 2}
        self.assertRefused(self._resolve(), nfe.REASON_SESSION_REGISTRY_MISSING)

    def test_blocked_scan_status_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry(scan_status='BLOCKED')
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_SESSION_NOT_READY)

    def test_registered_path_not_fork_target_refuses(self):
        parent = self._write_parent('s1', [assistant_event('ev1')])
        elsewhere = Path(self.tmp.name) / 'elsewhere.jsonl'
        elsewhere.write_bytes(parent.read_bytes())
        self._registry(transcript_path=str(elsewhere))
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_TRANSCRIPT_PATH_MISMATCH)

    def test_missing_provider_cwd_refuses(self):
        event = assistant_event('ev1')
        event['cwd'] = None
        self._write_parent('s1', [event])
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_TRANSCRIPT_PATH_MISMATCH)

    def test_conflicting_provider_cwd_refuses(self):
        event = assistant_event('ev1')
        other = assistant_event('ev2', seconds_ago=30)
        other['cwd'] = os.path.join(self.tmp.name, 'other-project')
        self._write_parent('s1', [event, other])
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_TRANSCRIPT_PATH_MISMATCH)

    def test_missing_parent_transcript_refuses(self):
        self._registry()
        self._mapping()
        self.assertRefused(self._resolve(), nfe.REASON_PARENT_TRANSCRIPT_MISSING)

    def test_missing_mapping_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry()
        self.assertRefused(self._resolve(), nfe.REASON_MAPPING_MISSING)

    def test_mapping_other_session_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry()
        self._mapping(session='s2')
        self.assertRefused(self._resolve(), nfe.REASON_SESSION_MISMATCH)

    def test_mapping_other_generation_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry()
        self._mapping(gen=2)
        self.assertRefused(self._resolve(), nfe.REASON_SESSION_MISMATCH)

    def test_ambiguous_offset_tie_refuses(self):
        self._write_parent('s1', [assistant_event('ev1')])
        self._registry()
        self._mapping(event_uuid='ev1', offset=5)
        self._mapping(event_uuid='ev2', offset=5)
        self.assertRefused(self._resolve(), nfe.REASON_MAPPING_AMBIGUOUS)

    def test_wake_tail_is_unsupported_scope(self):
        self._hot_parent()
        self.assertRefused(
            self._resolve(candidate=_candidate(source_refs=('turn:1:2', 'wake:9'))),
            nfe.REASON_SCOPE_UNSUPPORTED,
        )

    def test_snapshot_without_window_identity_refuses(self):
        self._hot_parent()
        self.assertRefused(
            self._resolve(snapshot=_snapshot(context_id=None, context_epoch=None)),
            nfe.REASON_WINDOW_IDENTITY_MISSING,
        )

    def test_resolver_never_raises(self):
        self._hot_parent()
        with mock.patch(
            'continuity.native_fork_eligibility._transcript_evidence',
            side_effect=RuntimeError('boom'),
        ):
            self.assertRefused(self._resolve(), nfe.REASON_RESOLVER_ERROR)

    def test_refusal_observability_names_oneshot_and_reason(self):
        obs = self._resolve().observability()
        self.assertEqual(obs['continuity_generation_mode'], nfe.MODE_ONESHOT)
        self.assertEqual(obs['continuity_generation_fallback_reason'], nfe.REASON_SESSION_REGISTRY_MISSING)


class CanonicalWakeScopeEligibilityTest(unittest.TestCase):
    """Mixed candidate scope keeps Wake provenance canonical and fail-closed."""

    def setUp(self):
        self._fixture = ContinuityNativeForkEligibilityTest('test_hot_same_model_exact_parent_is_eligible')
        self._fixture.setUp()
        for name in ('tmp', 'db', 'cwd', 'claude_home', 'conn', '_flag_on', 'current'):
            setattr(self, name, getattr(self._fixture, name))

    def tearDown(self):
        self._fixture.doCleanups()
        self._fixture.tearDown()

    def __getattr__(self, name):
        fixture = self.__dict__.get('_fixture')
        if fixture is not None:
            return getattr(fixture, name)
        raise AttributeError(name)

    def _ensure_messages(self):
        self.conn.execute(
            '''CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY, author TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                cache_info TEXT DEFAULT '', source_kind TEXT NOT NULL DEFAULT 'chat'
            )'''
        )

    def _wake(self, *, wake_id=3, canonical=True):
        self._ensure_messages()
        cache = {
            'wake_mode': 'normal',
            'canonical_chat_history': canonical,
            'unified_chat_resident': canonical,
            'b3_authority': canonical,
            'source': 'wake',
            'provider': 'claude_code',
        }
        self.conn.execute(
            'INSERT OR REPLACE INTO chat_messages '
            '(id, author, content, cache_info, source_kind) VALUES (?, ?, ?, ?, ?)',
            (wake_id, 'assistant', 'canonical autonomous wake', json.dumps(cache), 'wake'),
        )
        self.conn.commit()
        return self.conn.execute(
            'SELECT * FROM chat_messages WHERE id=?', (wake_id,)
        ).fetchone()

    def _mixed_snapshot_candidate(self, *, canonical=True, refs=None):
        row = self._wake(canonical=canonical)
        wake_revision = nfe.row_revision(row)
        refs = tuple(refs or ('turn:1:2', 'wake:3', 'turn:4:5'))
        kinds = ('completed_turn', 'autonomous_event', 'completed_turn')
        revisions = ('rev-turn-1', wake_revision, 'rev-turn-2')
        members = tuple(
            SourceMember(
                seq=index, source_kind=kind, source_ref=ref,
                source_revision=revision, role='assistant', content_hash=revision,
                logical_size=10, created_at='2026-09-27 04:00:00',
                branch_id='active-transcript',
            )
            for index, (kind, ref, revision) in enumerate(zip(kinds, refs, revisions))
        )
        snapshot = SourceSnapshot(
            snapshot_id='snap:1', identity_id='fyodor', chat_id='default',
            branch_id='active-transcript', local_day='2026-09-27',
            source_watermark=5, policy_version='continuity_source_v1',
            source_hash='hash', status='ready', created_at='2026-09-27 04:00:00',
            members=members, context_id=1, context_epoch=1,
        )
        candidate = _candidate(
            source_refs=refs,
            source_seqs=(0, 1, 2),
            source_revisions=revisions,
            completed_turn_count=2,
            source_end_seq=3,
        )
        return snapshot, candidate

    def _hot_boundary(self):
        self._write_parent('s1', [assistant_event('ev5')])
        self._registry()
        self._mapping(event_uuid='ev5', message_id=5)

    def test_turn_wake_turn_is_native_eligible_at_final_turn_boundary(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate()
        plan = self._resolve(snapshot=snapshot, candidate=candidate)
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.boundary_message_id, 5)
        self.assertEqual(plan.fork_event_uuid, 'ev5')
        self.assertEqual(plan.scope_completed_turns, 2)
        self.assertEqual(plan.scope_wake_count, 1)

    def test_noncanonical_wake_falls_back_closed(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate(canonical=False)
        self.assertRefused(self._resolve(snapshot=snapshot, candidate=candidate), nfe.REASON_SCOPE_UNSUPPORTED)

    def test_leading_wake_is_unsupported(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate(
            refs=('wake:3', 'turn:1:2', 'turn:4:5'),
        )
        self.assertRefused(self._resolve(snapshot=snapshot, candidate=candidate), nfe.REASON_SCOPE_UNSUPPORTED)

    def test_trailing_wake_is_unsupported(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate(
            refs=('turn:1:2', 'turn:4:5', 'wake:3'),
        )
        self.assertRefused(self._resolve(snapshot=snapshot, candidate=candidate), nfe.REASON_SCOPE_UNSUPPORTED)

    def test_unknown_autonomous_ref_is_unsupported(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate(
            refs=('turn:1:2', 'autonomous:3', 'turn:4:5'),
        )
        self.assertRefused(self._resolve(snapshot=snapshot, candidate=candidate), nfe.REASON_SCOPE_UNSUPPORTED)

    def test_pure_turn_scope_remains_eligible_without_wake(self):
        self._hot_boundary()
        members = (
            SourceMember(
                seq=0, source_kind='completed_turn', source_ref='turn:1:2',
                source_revision='rev-0', role='conversation', content_hash='rev-0',
                logical_size=10, created_at='2026-09-27 04:00:00', branch_id='active-transcript',
            ),
            SourceMember(
                seq=1, source_kind='completed_turn', source_ref='turn:4:5',
                source_revision='rev-1', role='conversation', content_hash='rev-1',
                logical_size=10, created_at='2026-09-27 04:00:01', branch_id='active-transcript',
            ),
        )
        snapshot = _snapshot()
        snapshot = SourceSnapshot(**{**snapshot.__dict__, 'members': members})
        candidate = _candidate(
            source_refs=('turn:1:2', 'turn:4:5'), source_seqs=(0, 1),
            source_revisions=('rev-0', 'rev-1'), completed_turn_count=2,
        )
        plan = self._resolve(snapshot=snapshot, candidate=candidate)
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.scope_wake_count, 0)

    def test_wake_revision_must_belong_to_snapshot(self):
        self._hot_boundary()
        snapshot, candidate = self._mixed_snapshot_candidate()
        bad = list(snapshot.members)
        bad[1] = SourceMember(**{**bad[1].__dict__, 'source_revision': 'foreign-revision', 'content_hash': 'foreign-revision'})
        snapshot = SourceSnapshot(**{**snapshot.__dict__, 'members': tuple(bad)})
        self.assertRefused(self._resolve(snapshot=snapshot, candidate=candidate), nfe.REASON_SCOPE_UNSUPPORTED)

    def test_earlier_generation_canonical_wake_allows_current_boundary(self):
        self._write_parent('s2', [assistant_event('ev5')])
        self._registry(session='s2', gen=2)
        self._mapping(event_uuid='ev5', message_id=5, session='s2', gen=2)
        self.current = {**self.current, 'resident_generation': 2}
        self._fixture.current = self.current
        snapshot, candidate = self._mixed_snapshot_candidate()
        plan = self._resolve(snapshot=snapshot, candidate=candidate)
        self.assertTrue(plan.eligible, plan.reason)
        self.assertEqual(plan.resident_generation, 2)
        self.assertEqual(plan.scope_wake_count, 1)


if __name__ == '__main__':
    unittest.main()
