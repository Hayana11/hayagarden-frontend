"""CONTINUITY-R3 — grounded shadow generation contracts."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock, patch

from continuity.chunk_generation import (
    ACCEPTED_PROMPT,
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PersonaContractError,
    PROMPT_POLICY_VERSION,
    build_chunk_prompt,
    generate_continuity_chunk,
    select_persona_sections,
    validate_output,
)
from continuity.contracts import SourceMember
from continuity.coverage import source_hash
from continuity.materialization import (
    SourceMaterializationError,
    UnsupportedSourceError,
    _candidate_source_revision,
    materialize_candidate,
)
from scripts.generate_continuity_chunk_shadow import (
    _read_only_connection,
    _source_rows,
)
from continuity.sealing import CandidateBlock, SealingPolicy, seal_snapshot
from continuity.sources import build_source_snapshot, derive_autonomous_events, derive_completed_turns
from continuity.store import (
    ContinuityStoreConflict,
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_chunk,
    load_generation_job,
    load_candidates,
    materialize_job,
    save_source_snapshot,
)


def _rows(*, include_nearby: bool = False, branch_idx: int = 0, incomplete: bool = False):
    tool_calls = json.dumps([{
        'name': 'read_file',
        'args': {'path': 'README.md'},
        'result': 'unique-tool-result',
        'success': True,
    }], ensure_ascii=False)
    rows = [
        {
            'id': 1, 'author': 'hayana', 'content': 'user fact', 'thinking': 'secret thinking',
            'source_kind': 'chat', 'created_at': '2026-09-08 23:59:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
        },
        {
            'id': 2, 'author': 'assistant', 'content': 'assistant answer', 'thinking': 'hidden thought',
            'source_kind': 'chat', 'created_at': '2026-09-09 00:01:00',
            'attachments': '[]',
            'cache_info': json.dumps({'stream_interrupted': True}) if incomplete else '',
            'tool_calls': tool_calls, 'branches': json.dumps([{'content': 'branch'}]),
            'branch_idx': branch_idx,
        },
    ]
    if include_nearby:
        rows.extend([
            {
                'id': 3, 'author': 'hayana', 'content': 'nearby user', 'thinking': '',
                'source_kind': 'chat', 'created_at': '2026-09-09 00:02:00',
                'attachments': '[]', 'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
            },
            {
                'id': 4, 'author': 'assistant', 'content': 'nearby answer', 'thinking': '',
                'source_kind': 'chat', 'created_at': '2026-09-09 00:03:00',
                'attachments': '[]', 'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
            },
        ])
    return rows


def _wake_rows():
    rows = _rows()
    rows.append({
        'id': 3, 'author': 'assistant', 'content': 'wake event', 'thinking': 'wake thought',
        'source_kind': 'wake', 'created_at': '2026-09-09 03:59:00', 'attachments': '[]',
        'cache_info': json.dumps({
            'wake_mode': 'normal', 'canonical_chat_history': True,
            'unified_chat_resident': True, 'b3_authority': True,
            'source': 'wake', 'provider': 'claude_code',
        }), 'tool_calls': '', 'branches': '', 'branch_idx': 0,
    })
    return rows


def _single_candidate(snapshot, member: SourceMember) -> CandidateBlock:
    return CandidateBlock(
        candidate_id='candidate:single', snapshot_id=snapshot.snapshot_id,
        policy_version='continuity_sealing_v1_12k_20turns', block_seq=0,
        local_day=member.created_at[:10], branch_id=member.branch_id,
        source_start_seq=member.seq, source_end_seq=member.seq + 1,
        source_seqs=(member.seq,), source_refs=(member.source_ref,),
        source_revisions=(member.source_revision,), logical_size=member.logical_size,
        completed_turn_count=int(member.source_kind == 'completed_turn'), oversize=False,
        close_reason='test', source_revision=_candidate_source_revision((member,)),
    )


class MaterializationTests(unittest.TestCase):
    def setUp(self):
        rows = _rows()
        turns = derive_completed_turns(rows)
        events = derive_autonomous_events(rows)
        self.snapshot = build_source_snapshot(
            turns=turns, events=events, local_day='2026-09-08',
            source_watermark=2, created_at='2026-09-09T00:00:00Z',
        )
        self.candidate = _single_candidate(self.snapshot, self.snapshot.members[0])

    def test_exact_completed_turn(self):
        materialized = materialize_candidate(self.snapshot, self.candidate, _rows())
        self.assertIn('user fact', materialized.body)
        self.assertIn('assistant answer', materialized.body)

    def test_wake_event(self):
        rows = _wake_rows()
        snapshot = build_source_snapshot(
            turns=derive_completed_turns(rows), events=derive_autonomous_events(rows),
            local_day='2026-09-08', source_watermark=3, created_at='2026-09-09T00:00:00Z',
        )
        wake = next(item for item in snapshot.members if item.source_kind == 'autonomous_event')
        body = materialize_candidate(snapshot, _single_candidate(snapshot, wake), rows).body
        self.assertIn('[WAKE]', body)
        self.assertIn('wake event', body)

    def test_tool_outcome_is_rendered_once(self):
        body = materialize_candidate(self.snapshot, self.candidate, _rows()).body
        self.assertEqual(body.count('unique-tool-result'), 1)

    def test_tool_outcome_preserves_all_canonical_facts(self):
        rows = _rows()
        rows[1] = {
            **rows[1],
            'tool_calls': json.dumps([{
                'name': 'apply_patch',
                'args': {'path': 'x.py', 'line': 3},
                'result': 'unique-full-result',
                'success': False,
                'artifact': 'artifact-id-7',
                'diff': '-old\n+new',
            }], ensure_ascii=False),
        }
        turns = derive_completed_turns(rows)
        snapshot = build_source_snapshot(
            turns=turns, events=(), local_day='2026-09-08', source_watermark=2,
            created_at='2026-09-09T00:00:00Z',
        )
        body = materialize_candidate(
            snapshot, _single_candidate(snapshot, snapshot.members[0]), rows,
        ).body
        self.assertIn('NAME: apply_patch', body)
        self.assertIn('ARGS: {"line":3,"path":"x.py"}', body)
        self.assertIn('RESULT: unique-full-result', body)
        self.assertIn('SUCCESS: false', body)
        self.assertIn('ARTIFACT: artifact-id-7', body)
        self.assertIn('DIFF: -old\n+new', body)
        self.assertEqual(body.count('unique-full-result'), 1)

    def test_thinking_is_excluded(self):
        body = materialize_candidate(self.snapshot, self.candidate, _rows()).body
        self.assertNotIn('secret thinking', body)
        self.assertNotIn('hidden thought', body)

    def test_nearby_non_member_is_excluded(self):
        body = materialize_candidate(self.snapshot, self.candidate, _rows(include_nearby=True)).body
        self.assertNotIn('nearby user', body)
        self.assertNotIn('nearby answer', body)

    def test_attachment_span_fails_closed(self):
        member = SourceMember(
            seq=0, source_kind='attachment_span', source_ref='attachment:file-1',
            source_revision='rev', role='attachment', content_hash='hash', span_start=0, span_end=4,
            logical_size=4, created_at='2026-09-08 23:59:00', branch_id='active-transcript',
        )
        snapshot = self.snapshot.__class__(
            snapshot_id='source:attachment', identity_id=self.snapshot.identity_id,
            chat_id=self.snapshot.chat_id, branch_id=self.snapshot.branch_id,
            local_day=self.snapshot.local_day, source_watermark=0,
            policy_version=self.snapshot.policy_version, source_hash=source_hash((member,)),
            status='ready', created_at=self.snapshot.created_at, members=(member,),
        )
        with self.assertRaises(UnsupportedSourceError):
            materialize_candidate(snapshot, _single_candidate(snapshot, member), ())

    def test_wrong_revision_fails(self):
        candidate = self.candidate.__class__(
            **{**self.candidate.__dict__, 'source_revisions': ('wrong',)}
        )
        with self.assertRaises(SourceMaterializationError):
            materialize_candidate(self.snapshot, candidate, _rows())

    def test_branch_drift_fails(self):
        with self.assertRaises(SourceMaterializationError):
            materialize_candidate(self.snapshot, self.candidate, _rows(branch_idx=1))

    def test_incomplete_source_fails(self):
        with self.assertRaises(SourceMaterializationError):
            materialize_candidate(self.snapshot, self.candidate, _rows(incomplete=True))


class PromptContractTests(unittest.TestCase):
    PERSONA = (
        'preface excluded\n'
        '## A\nA body\n### A.1\nnested A body\n'
        '## B\nB body\n'
        '## C\nC body\n'
        '## D\nD body\n'
        '## E\nE body\n'
        '## F\nF must be excluded\n'
    )

    def setUp(self):
        self.source = SimpleNamespace(body='frozen evidence', source_token_estimate=10)

    def test_first_five_persona_sections_exclude_preface_and_section_six(self):
        selected = select_persona_sections(self.PERSONA)
        self.assertNotIn('preface excluded', selected)
        for fragment in ('## A', '### A.1', 'nested A body', '## B', '## C', '## D', '## E'):
            self.assertIn(fragment, selected)
        self.assertNotIn('## F', selected)
        self.assertNotIn('F must be excluded', selected)

    def test_request_composition_uses_persona_five_and_exact_prompt(self):
        system_text, prompt_text = build_chunk_prompt(
            self.source,
            persona_text=self.PERSONA,
        )
        self.assertIn('## A', system_text)
        self.assertIn('### A.1', system_text)
        self.assertNotIn('## F', system_text)
        self.assertIn(ACCEPTED_PROMPT, system_text)
        self.assertIn('FROZEN EVIDENCE BEGIN\nfrozen evidence\nFROZEN EVIDENCE END', prompt_text)
        self.assertNotIn('## A', prompt_text)

    def test_default_persona_reader_is_runtime_chat_persona_store(self):
        persona_store = ModuleType('chat.persona_store')
        persona_store.read_persona = Mock(return_value=self.PERSONA)
        with patch.dict(sys.modules, {'chat.persona_store': persona_store}):
            build_chunk_prompt(self.source)
        persona_store.read_persona.assert_called_once_with()

    def test_fewer_than_five_sections_fail_closed(self):
        with self.assertRaises(PersonaContractError):
            select_persona_sections('## A\n## B\n## C\n## D\n')


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        ensure_schema(self.conn)
        self.rows = _rows()
        snapshot = build_source_snapshot(
            turns=derive_completed_turns(self.rows), events=(), local_day='2026-09-08',
            source_watermark=2, created_at='2026-09-09T00:00:00Z',
        )
        save_source_snapshot(self.conn, snapshot)
        r2_job = enqueue_job(self.conn, snapshot, SealingPolicy())
        materialize_job(self.conn, r2_job.job_id, SealingPolicy())
        self.snapshot = snapshot
        self.candidate = load_candidates(self.conn, r2_job.job_id)[0]
        self.job = enqueue_generation_job(
            self.conn, self.candidate, snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        self.authority = SimpleNamespace(provider='claude_code', model_identity='model-A')
        self.persona_text = PromptContractTests.PERSONA
        self.requests = []
        self.calls = 0

    def tearDown(self):
        self.conn.close()

    def capture(self):
        self.calls += 1
        return self.authority

    def request_factory(self, **kwargs):
        request = SimpleNamespace(**kwargs)
        self.requests.append(request)
        return request

    def result(self, text='valid chunk', *, provider='claude_code', model='model-A'):
        return SimpleNamespace(
            text=text, provider=provider, model_identity=model,
            actual_executor='fake_background', usage={},
        )

    def generate(self, request, authority):
        self.assertEqual(authority.provider, 'claude_code')
        self.assertEqual(authority.model_identity, 'model-A')
        return self.result()

    def run_generation(self, *, rows_provider=None, generate_fn=None, capture=None, persona_reader=None):
        return generate_continuity_chunk(
            self.conn, self.job.generation_job_id,
            rows_provider=rows_provider or (lambda: self.rows),
            capture_authority=capture or self.capture,
            generate_fn=generate_fn or self.generate,
            request_factory=self.request_factory,
            persona_reader=persona_reader or (lambda: self.persona_text),
        )

    def test_primary_authority_capture_once_and_retry_is_frozen(self):
        attempts = {'count': 0}

        def fail_once(request, authority):
            attempts['count'] += 1
            if attempts['count'] == 1:
                raise RuntimeError('provider down')
            return self.result()

        self.assertRaises(RuntimeError, self.run_generation, generate_fn=fail_once)
        self.authority = SimpleNamespace(provider='api_relay', model_identity='model-B')
        self.run_generation(generate_fn=fail_once)
        self.assertEqual(self.calls, 1)
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual((job.frozen_provider, job.frozen_model_identity), ('claude_code', 'model-A'))

    def test_provider_failure_marks_failed_without_fallback(self):
        calls = []

        def fail(request, authority):
            calls.append(authority.provider)
            raise RuntimeError('down')

        self.assertRaises(RuntimeError, self.run_generation, generate_fn=fail)
        self.assertEqual(calls, ['claude_code'])
        self.assertEqual(load_generation_job(self.conn, self.job.generation_job_id).status, 'failed')

    def test_empty_output_fails(self):
        self.assertRaises(ValueError, self.run_generation, generate_fn=lambda request, authority: self.result(''))
        self.assertEqual(load_generation_job(self.conn, self.job.generation_job_id).error_code, 'empty_generation_output')

    def test_provider_and_model_mismatch_fail(self):
        for provider, model, code in (
            ('api_relay', 'model-A', 'provider_provenance_mismatch'),
            ('claude_code', 'model-B', 'model_provenance_mismatch'),
        ):
            with self.subTest(provider=provider, model=model):
                self.assertRaises(
                    ValueError,
                    self.run_generation,
                    generate_fn=lambda request, authority, p=provider, m=model: self.result(provider=p, model=m),
                )
                self.assertEqual(load_generation_job(self.conn, self.job.generation_job_id).error_code, code)
                # A failed job is intentionally reusable with its frozen authority.
                self.conn.execute("UPDATE continuity_generation_jobs SET status='pending' WHERE generation_job_id=?", (self.job.generation_job_id,))
                self.conn.commit()

    def test_source_mutation_before_generate_is_stale_and_no_call(self):
        changed = _rows()
        changed[0] = {**changed[0], 'content': 'changed before'}
        calls = []
        with self.assertRaises(SourceMaterializationError):
            self.run_generation(rows_provider=lambda: changed, generate_fn=lambda request, authority: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(load_generation_job(self.conn, self.job.generation_job_id).status, 'stale')

    def test_source_mutation_during_generate_is_stale_without_publish(self):
        count = {'value': 0}
        changed = _rows()
        changed[0] = {**changed[0], 'content': 'changed during'}

        def provider():
            count['value'] += 1
            return self.rows if count['value'] == 1 else changed

        with self.assertRaises(SourceMaterializationError):
            self.run_generation(rows_provider=provider)
        self.assertEqual(load_generation_job(self.conn, self.job.generation_job_id).status, 'stale')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)

    def test_valid_result_is_ready(self):
        chunk = self.run_generation()
        self.assertEqual(chunk.status, 'ready')
        self.assertGreater(chunk.source_token_estimate, 0)
        self.assertGreater(chunk.output_token_estimate, 0)

    def test_duplicate_ready_does_not_call_model_again(self):
        self.run_generation()
        calls = {'count': 0}

        def second(request, authority):
            calls['count'] += 1
            return self.result('second')

        again = self.run_generation(generate_fn=second)
        self.assertEqual(calls['count'], 0)
        self.assertEqual(again.body, 'valid chunk')

    def test_persistence_failure_leaves_no_chunk_and_job_not_ready(self):
        with patch('continuity.chunk_generation.publish_chunk_atomic', side_effect=RuntimeError('disk')):
            self.assertRaises(RuntimeError, self.run_generation)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)
        failed = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(failed.status, 'failed')
        self.assertEqual((failed.frozen_provider, failed.frozen_model_identity), ('claude_code', 'model-A'))

        def capture_must_not_run():
            raise AssertionError('authority must remain frozen on retry')

        chunk = self.run_generation(capture=capture_must_not_run)
        self.assertEqual(chunk.status, 'ready')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 1)

    def test_ready_body_is_immutable(self):
        chunk = self.run_generation()
        for column, value in (
            ('body', 'mutated'),
            ('candidate_id', 'candidate:other'),
            ('source_token_estimate', 1),
            ('provider', 'api_relay'),
        ):
            with self.subTest(column=column), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    f'UPDATE continuity_chunks SET {column}=? WHERE chunk_id=?',
                    (value, chunk.chunk_id),
                )
                self.conn.commit()
            self.conn.rollback()
        self.conn.execute(
            "UPDATE continuity_chunks SET status='stale' WHERE chunk_id=?",
            (chunk.chunk_id,),
        )
        self.conn.commit()
        self.assertEqual(load_chunk(self.conn, chunk.chunk_id).status, 'stale')
        self.assertEqual(load_chunk(self.conn, chunk.chunk_id).body, 'valid chunk')

    def test_policy_identity_change_creates_new_generation_job(self):
        second = enqueue_generation_job(
            self.conn, self.candidate, self.snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version='continuity_chunk_prompt_v3',
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        self.assertNotEqual(second.generation_job_id, self.job.generation_job_id)

    def test_prompt_policy_version_is_v2(self):
        self.assertEqual(PROMPT_POLICY_VERSION, 'continuity_chunk_prompt_v2')
        self.assertEqual(self.job.prompt_policy_version, PROMPT_POLICY_VERSION)

    def test_old_v1_job_fails_closed_before_model_or_publish(self):
        self.conn.execute(
            "UPDATE continuity_generation_jobs SET prompt_policy_version='continuity_chunk_prompt_v1' WHERE generation_job_id=?",
            (self.job.generation_job_id,),
        )
        self.conn.commit()
        calls = []
        with self.assertRaises(ContinuityStoreConflict):
            self.run_generation(generate_fn=lambda request, authority: calls.append(1))
        self.assertEqual(calls, [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(job.status, 'pending')
        self.assertEqual(job.prompt_policy_version, 'continuity_chunk_prompt_v1')

    def test_persona_failure_marks_job_failed_without_model_or_publish(self):
        calls = []
        with self.assertRaises(PersonaContractError):
            self.run_generation(
                persona_reader=lambda: '## A\n## B\n## C\n## D\n',
                generate_fn=lambda request, authority: calls.append(1),
            )
        self.assertEqual(calls, [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'persona_contract_error')

    def test_persona_reader_error_marks_job_failed_without_model_or_publish(self):
        calls = []

        def broken_reader():
            raise OSError('runtime persona unavailable')

        with self.assertRaises(PersonaContractError):
            self.run_generation(
                persona_reader=broken_reader,
                generate_fn=lambda request, authority: calls.append(1),
            )
        self.assertEqual(calls, [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'persona_contract_error')

    def test_output_token_estimate_is_deterministic(self):
        result = self.result('中文 deterministic')
        first = validate_output(result, authority=self.authority, source=SimpleNamespace(source_token_estimate=20))
        second = validate_output(result, authority=self.authority, source=SimpleNamespace(source_token_estimate=20))
        self.assertEqual(first, second)

    def test_generator_policy_identity_change_creates_new_job(self):
        second = enqueue_generation_job(
            self.conn, self.candidate, self.snapshot,
            generator_policy_version='continuity_chunk_generator_v2',
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        self.assertNotEqual(second.generation_job_id, self.job.generation_job_id)

    def test_static_error_marker_is_rejected_deterministically(self):
        with self.assertRaises(ValueError):
            self.run_generation(generate_fn=lambda request, authority: self.result('[static fallback]'))
        self.assertEqual(
            load_generation_job(self.conn, self.job.generation_job_id).error_code,
            'static_or_error_output',
        )

    def test_legitimate_failure_facts_are_allowed_in_body(self):
        result = self.result(
            'The previous generation failed with a provider error; retry remains unresolved.'
        )
        body, tokens = validate_output(
            result,
            authority=self.authority,
            source=SimpleNamespace(source_token_estimate=20),
        )
        self.assertIn('provider error', body)
        self.assertGreater(tokens, 0)

    def test_no_cross_provider_fallback(self):
        calls = []

        def fail(request, authority):
            calls.append(authority.provider)
            raise RuntimeError('failure')

        self.assertRaises(RuntimeError, self.run_generation, generate_fn=fail)
        self.assertEqual(calls, ['claude_code'])


class RunnerTests(unittest.TestCase):
    def test_source_loader_uses_cursor_metadata_and_read_only_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f'{directory}/source.db'
            conn = sqlite3.connect(path)
            conn.execute(
                'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, '
                'thinking TEXT, created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
                'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT)'
            )
            conn.execute(
                'INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (1, 'hayana', 'source', 'private thinking', '2026-09-08 23:59:00', '', '', 0, '', 'chat', '[]', '', '', ''),
            )
            conn.commit()
            conn.close()

            read_only = _read_only_connection(path)
            try:
                rows = _source_rows(read_only)
                self.assertEqual(rows[0]['id'], 1)
                self.assertEqual(rows[0]['thinking'], 'private thinking')
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute('DELETE FROM chat_messages')
            finally:
                read_only.close()

    def test_default_cc_adapter_passes_existing_token_getter(self):
        from continuity.chunk_generation import _default_generate

        request = SimpleNamespace()
        authority = SimpleNamespace(provider='claude_code', model_identity='model-A')
        with patch('chat.background_generation.generate_background', return_value='result') as generate, \
             patch('chat.cc_auth.read_cc_oauth_token') as token_getter:
            self.assertEqual(_default_generate(request, authority), 'result')
        generate.assert_called_once_with(request, authority, cc_token_getter=token_getter)


if __name__ == '__main__':
    unittest.main()

