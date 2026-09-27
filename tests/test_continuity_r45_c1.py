"""R4.5-C1 explicit, gate-off Continuity producer tests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from continuity.producer import run_continuity_producer
from continuity.sealing import SealingPolicy


PERSONA = '\n'.join(
    f'## {letter}\nsection {letter}'
    for letter in ('A', 'B', 'C', 'D', 'E', 'F')
)


def _row(
    message_id: int,
    author: str,
    content: str,
    created_at: str,
    *,
    source_kind: str = 'chat',
    cache_info: str = '',
) -> tuple:
    return (
        message_id,
        author,
        content,
        'private thinking must never be evidence',
        created_at,
        '',
        '',
        0,
        cache_info,
        source_kind,
        '[]',
        '',
        '',
        '',
    )


def _wake_cache(run_id: str) -> str:
    return json.dumps({
        'wake_run_id': run_id,
        'wake_mode': 'normal',
        'canonical_chat_history': True,
        'unified_chat_resident': True,
        'b3_authority': True,
        'source': 'wake',
        'provider': 'claude_code',
    })


class ProducerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.source_path = Path(self.tempdir.name) / 'source.db'
        self.store_path = Path(self.tempdir.name) / 'continuity.db'
        self.identity = {
            'chat_id': 'default',
            'context_id': 7,
            'context_epoch': 3,
            'resident_generation': 99,
        }
        self._write_source([])
        self.authority_calls = 0
        self.model_calls = 0

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_source(
        self,
        rows: list[tuple],
        *,
        mappings: list[tuple[int, int, int, int, str]] | None = None,
        wakes: list[tuple[str, int, int, int]] | None = None,
    ) -> None:
        if self.source_path.exists():
            self.source_path.unlink()
        conn = sqlite3.connect(str(self.source_path))
        conn.execute(
            'CREATE TABLE chat_messages ('
            'id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, '
            'created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
            'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, '
            'file_url TEXT, file_name TEXT)'
        )
        conn.execute(
            'CREATE TABLE daily_message_contexts ('
            'message_id INTEGER PRIMARY KEY, context_id INTEGER NOT NULL, '
            'context_epoch INTEGER NOT NULL, resident_generation INTEGER NOT NULL, '
            'role TEXT NOT NULL)'
        )
        conn.execute(
            'CREATE TABLE wake_log ('
            'id INTEGER PRIMARY KEY, wake_run_id TEXT, chat_id TEXT, '
            'context_id INTEGER, context_epoch INTEGER, resident_generation INTEGER)'
        )
        conn.executemany(
            'INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows
        )
        conn.executemany(
            'INSERT INTO daily_message_contexts '
            '(message_id, context_id, context_epoch, resident_generation, role) '
            'VALUES (?,?,?,?,?)',
            mappings if mappings is not None else [
                (row[0], self.identity['context_id'], self.identity['context_epoch'], 1,
                 'user' if row[1] == 'hayana' else 'assistant')
                for row in rows if row[9] != 'wake'
            ],
        )
        conn.executemany(
            'INSERT INTO wake_log '
            '(wake_run_id, chat_id, context_id, context_epoch, resident_generation) '
            'VALUES (?,?,?,?,?)',
            [
                (run_id, 'default', context_id, context_epoch, generation)
                for run_id, context_id, context_epoch, generation in (wakes or [])
            ],
        )
        conn.commit()
        conn.close()

    def _run(
        self,
        *,
        policy: SealingPolicy,
        generate_fn=None,
        capture_authority=None,
    ):
        return run_continuity_producer(
            source_db_path=self.source_path,
            continuity_store_path=self.store_path,
            policy=policy,
            window_identity_reader=lambda _conn, _chat_id: self.identity,
            capture_authority=capture_authority or self._capture,
            generate_fn=generate_fn or self._generate,
            request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            persona_text=PERSONA,
            now='2026-09-14T00:00:00+00:00',
        )

    def _capture(self):
        self.authority_calls += 1
        return SimpleNamespace(provider='claude_code', model_identity='model-A')

    def _generate(self, _request, _authority):
        self.model_calls += 1
        return SimpleNamespace(
            text='grounded generated chunk',
            provider='claude_code',
            model_identity='model-A',
            actual_executor='test-provider',
        )

    def _turns(
        self,
        count: int,
        *,
        day: str = '2026-09-14',
        start_id: int = 1,
    ) -> list[tuple]:
        rows = []
        for index in range(count):
            user_id = start_id + index * 2
            assistant_id = user_id + 1
            rows.extend((
                _row(user_id, 'hayana', f'request {index}', f'{day} 01:{index:02d}:00'),
                _row(assistant_id, 'assistant', f'answer {index}', f'{day} 01:{index:02d}:30'),
            ))
        return rows

    def test_current_partial_tail_is_never_snapshotted(self):
        self._write_source(self._turns(3))
        policy = SealingPolicy(target_logical_size=100_000, max_completed_turns=20)
        first = self._run(policy=policy)
        second = self._run(policy=policy)
        self.assertEqual(first.status, 'idle')
        self.assertEqual(second.status, 'idle')
        self.assertEqual(first.sealed_candidate_count, 0)
        self.assertEqual(self.model_calls, 0)
        if self.store_path.exists():
            conn = sqlite3.connect(str(self.store_path))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_source_snapshots').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0], 0)
            conn.close()

    def test_target_seals_once_and_repeat_is_idempotent(self):
        self._write_source(self._turns(3))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)
        first = self._run(policy=policy)
        second = self._run(policy=policy)
        third = self._run(policy=policy)
        fourth = self._run(policy=policy)
        self.assertEqual(first.status, 'ready')
        self.assertEqual(second.status, 'ready')
        self.assertEqual(third.status, 'ready')
        self.assertEqual(fourth.status, 'idle')
        self.assertEqual(
            [first.sealed_candidate_count, second.sealed_candidate_count, third.sealed_candidate_count],
            [1, 1, 1],
        )
        self.assertEqual(
            [first.queued_generation_job_count, second.queued_generation_job_count, third.queued_generation_job_count],
            [1, 1, 1],
        )
        self.assertEqual(self.model_calls, 3)
        conn = sqlite3.connect(str(self.store_path))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_source_snapshots').fetchone()[0], 3)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 3)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM continuity_generation_jobs WHERE status='ready'").fetchone()[0], 3)
        conn.close()

    def test_backlog_of_28_seals_one_candidate_and_job_per_run(self):
        self._write_source(self._turns(28))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)

        first = self._run(policy=policy)
        self.assertEqual(first.status, 'ready')
        self.assertEqual(first.sealed_candidate_count, 1)
        self.assertEqual(first.queued_generation_job_count, 1)
        self.assertEqual(self.model_calls, 1)

        second = self._run(policy=policy)
        self.assertEqual(second.status, 'ready')
        self.assertEqual(second.sealed_candidate_count, 1)
        self.assertEqual(second.queued_generation_job_count, 1)
        self.assertEqual(self.model_calls, 2)

        conn = sqlite3.connect(str(self.store_path))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 2)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM continuity_chunks WHERE status='ready'").fetchone()[0], 2)
        refs = {
            row[0]
            for row in conn.execute(
                'SELECT source_ref FROM continuity_candidate_members'
            ).fetchall()
        }
        conn.close()
        self.assertEqual(refs, {'turn:1:2', 'turn:3:4'})

    def test_existing_pending_job_is_processed_without_sealing_another_candidate(self):
        self._write_source(self._turns(1))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)
        failed = self._run(
            policy=policy,
            generate_fn=lambda _request, _authority: (_ for _ in ()).throw(RuntimeError('temporary failure')),
        )
        self.assertEqual(failed.status, 'failed')

        conn = sqlite3.connect(str(self.store_path))
        conn.execute(
            "UPDATE continuity_generation_jobs SET status='pending', error_code=NULL"
        )
        conn.commit()
        conn.close()

        self._write_source(self._turns(3))
        resumed = self._run(policy=policy)
        self.assertEqual(resumed.status, 'ready')
        self.assertEqual(resumed.sealed_candidate_count, 0)
        self.assertEqual(resumed.queued_generation_job_count, 0)
        self.assertEqual(self.model_calls, 1)

        conn = sqlite3.connect(str(self.store_path))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_candidate_blocks').fetchone()[0], 1)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_generation_jobs').fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM continuity_generation_jobs WHERE status='ready'").fetchone()[0], 1)
        conn.close()

    def test_twenty_completed_turn_boundary_seals(self):
        self._write_source(self._turns(21))
        policy = SealingPolicy(target_logical_size=100_000, max_completed_turns=20)
        result = self._run(policy=policy)
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.sealed_candidate_count, 1)
        self.assertEqual(self.model_calls, 1)
        conn = sqlite3.connect(str(self.store_path))
        self.assertEqual(
            conn.execute('SELECT completed_turn_count FROM continuity_candidate_blocks').fetchone()[0],
            20,
        )
        conn.close()

    def test_natural_day_closes_yesterday_but_not_today(self):
        rows = self._turns(1, day='2026-09-13', start_id=1) + self._turns(
            1, day='2026-09-14', start_id=3,
        )
        self._write_source(rows)
        policy = SealingPolicy(target_logical_size=100_000, max_completed_turns=20)
        first = self._run(policy=policy)
        self.assertEqual(first.status, 'ready')
        conn = sqlite3.connect(str(self.store_path))
        refs = conn.execute(
            'SELECT source_ref FROM continuity_candidate_members ORDER BY ordinal'
        ).fetchall()
        conn.close()
        self.assertEqual([row[0] for row in refs], ['turn:1:2'])

    def test_context_scope_ignores_generation_and_foreign_context(self):
        rows = self._turns(2)
        rows += [
            _row(10, 'hayana', 'foreign request', '2026-09-14 02:00:00'),
            _row(11, 'assistant', 'foreign answer', '2026-09-14 02:00:30'),
        ]
        mappings = [
            (1, 7, 3, 1, 'user'), (2, 7, 3, 1, 'assistant'),
            (3, 7, 3, 2, 'user'), (4, 7, 3, 2, 'assistant'),
            (10, 8, 3, 1, 'user'), (11, 8, 3, 1, 'assistant'),
        ]
        self._write_source(rows, mappings=mappings)
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)
        first = self._run(policy=policy)
        second = self._run(policy=policy)
        self.assertEqual(first.source_member_count, 2)
        self.assertEqual(first.status, 'ready')
        self.assertEqual(second.status, 'ready')
        conn = sqlite3.connect(str(self.store_path))
        refs = sorted(
            row[0] for row in conn.execute(
                'SELECT source_ref FROM continuity_candidate_members'
            ).fetchall()
        )
        snapshot_identities = conn.execute(
            'SELECT context_id, context_epoch FROM continuity_source_snapshots'
        ).fetchall()
        conn.close()
        self.assertEqual(refs, ['turn:1:2', 'turn:3:4'])
        self.assertEqual(snapshot_identities, [(7, 3), (7, 3)])

    def test_canonical_wake_is_included_and_foreign_wake_is_excluded(self):
        rows = [
            _row(1, 'assistant', 'canonical wake', '2026-09-14 03:00:00',
                 source_kind='wake', cache_info=_wake_cache('wake-1')),
            _row(2, 'assistant', 'foreign wake', '2026-09-14 03:01:00',
                 source_kind='wake', cache_info=_wake_cache('wake-2')),
        ]
        self._write_source(
            rows,
            mappings=[],
            wakes=[('wake-1', 7, 3, 1), ('wake-2', 8, 3, 1)],
        )
        result = self._run(
            policy=SealingPolicy(target_logical_size=1, max_completed_turns=20)
        )
        self.assertEqual(result.status, 'ready')
        conn = sqlite3.connect(str(self.store_path))
        refs = conn.execute(
            'SELECT source_ref FROM continuity_candidate_members'
        ).fetchall()
        conn.close()
        self.assertEqual([row[0] for row in refs], ['wake:1'])

    def test_failed_generation_retries_same_job_and_frozen_authority(self):
        self._write_source(self._turns(1))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)
        failed = self._run(
            policy=policy,
            generate_fn=lambda _request, _authority: (_ for _ in ()).throw(RuntimeError('down')),
        )
        conn = sqlite3.connect(str(self.store_path))
        job_id = conn.execute(
            'SELECT generation_job_id FROM continuity_generation_jobs'
        ).fetchone()[0]
        conn.close()
        retried = self._run(policy=policy)
        self.assertEqual(failed.status, 'failed')
        self.assertEqual(retried.status, 'ready')
        self.assertEqual(self.authority_calls, 1)
        self.assertEqual(self.model_calls, 1)
        conn = sqlite3.connect(str(self.store_path))
        self.assertEqual(conn.execute(
            'SELECT COUNT(*) FROM continuity_generation_jobs WHERE generation_job_id=?',
            (job_id,),
        ).fetchone()[0], 1)
        conn.close()

    def test_source_change_during_model_call_marks_stale_without_ready_chunk(self):
        self._write_source(self._turns(1))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)

        def mutate_source(_request, _authority):
            conn = sqlite3.connect(str(self.source_path))
            conn.execute("UPDATE chat_messages SET content='changed' WHERE id=1")
            conn.commit()
            conn.close()
            return self._generate(_request, _authority)

        result = self._run(policy=policy, generate_fn=mutate_source)
        self.assertEqual(result.status, 'failed')
        conn = sqlite3.connect(str(self.store_path))
        job = conn.execute(
            'SELECT status, error_code FROM continuity_generation_jobs'
        ).fetchone()
        self.assertEqual(job[0], 'stale')
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM continuity_chunks').fetchone()[0], 0)
        conn.close()

    def test_orphan_generating_job_is_explicitly_blocked(self):
        self._write_source(self._turns(1))
        policy = SealingPolicy(target_logical_size=1, max_completed_turns=20)
        self._run(policy=policy, generate_fn=lambda _request, _authority: (_ for _ in ()).throw(RuntimeError('down')))
        conn = sqlite3.connect(str(self.store_path))
        conn.execute("UPDATE continuity_generation_jobs SET status='generating'")
        conn.commit()
        conn.close()
        result = self._run(policy=policy)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'orphan_generating_job')
        self.assertEqual(self.model_calls, 0)

    def test_default_reader_unavailable_fails_closed_without_side_effects(self):
        # The fixture has no canonical context-window schema, so the real default
        # reader must raise WindowIdentityUnavailable before opening the store.
        self._write_source([])
        result = run_continuity_producer(
            source_db_path=self.source_path,
            continuity_store_path=self.store_path,
            capture_authority=self._capture,
            generate_fn=self._generate,
            request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            persona_text=PERSONA,
            now='2026-09-14T00:00:00+00:00',
        )
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'window_identity_unavailable')
        self.assertEqual(self.authority_calls, 0)
        self.assertEqual(self.model_calls, 0)

        counts = [0, 0, 0, 0]
        if self.store_path.exists():
            conn = sqlite3.connect(str(self.store_path))
            for index, table in enumerate((
                'continuity_source_snapshots',
                'continuity_candidate_blocks',
                'continuity_generation_jobs',
                'continuity_chunks',
            )):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if exists:
                    counts[index] = conn.execute(
                        f'SELECT COUNT(*) FROM {table}'
                    ).fetchone()[0]
            conn.close()
        self.assertEqual(tuple(counts), (0, 0, 0, 0))

    def test_producer_has_no_chat_consumer_or_receipt_side_effect(self):
        self._write_source(self._turns(1))
        before = sqlite3.connect(str(self.source_path))
        before_counts = tuple(
            before.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('chat_messages', 'daily_message_contexts', 'wake_log')
        )
        before.close()
        result = self._run(
            policy=SealingPolicy(target_logical_size=1, max_completed_turns=20)
        )
        self.assertEqual(result.status, 'ready')
        after = sqlite3.connect(str(self.source_path))
        after_counts = tuple(
            after.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            for table in ('chat_messages', 'daily_message_contexts', 'wake_log')
        )
        after.close()
        self.assertEqual(before_counts, after_counts)


    def test_settings_bound_candidate_policy_and_preflight_use_distinct_versions(self):
        from unittest.mock import Mock, patch

        import continuity.chunk_generation as generation
        import continuity.store as store
        from continuity.contracts import SourceMember, SourceSnapshot
        from continuity.coverage import source_hash
        from continuity.settings import (
            binding_for_revision,
            ensure_authority,
            sealing_policy_for_revision,
        )

        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            store.ensure_schema(conn)
            authority = ensure_authority(
                conn,
                provider='claude_code',
                model_identity='explicit:claude-opus-5-5',
                persona_text=PERSONA,
                now='2026-09-27T00:00:00+00:00',
            )
            active = authority['active_revision']
            binding = binding_for_revision(conn, active['revision_id'])
            policy = sealing_policy_for_revision(active)

            member = SourceMember(
                0,
                'completed_turn',
                'turn:binding:1',
                'rev:binding:1',
                'user',
                'content-hash-binding-1',
                logical_size=12000,
                created_at='2026-09-27 00:00:00',
                branch_id='active-transcript',
            )
            snapshot = SourceSnapshot(
                'source:binding:1',
                'identity',
                'default',
                'active-transcript',
                '2026-09-27',
                1,
                'continuity_source_v1',
                source_hash((member,)),
                'ready',
                '2026-09-27T00:00:00+00:00',
                (member,),
                7,
                3,
            )
            store.save_source_snapshot(conn, snapshot)
            sealing_job = store.enqueue_job(
                conn, snapshot, policy, now='2026-09-27T00:00:00+00:00'
            )
            candidates = store.materialize_job(
                conn,
                sealing_job.job_id,
                policy,
                include_end_of_snapshot=True,
                settings_binding=binding,
                now='2026-09-27T00:00:00+00:00',
            )
            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]

            # policy_version identifies the sealing algorithm/policy object;
            # sealing_policy_version identifies the frozen settings authority value.
            self.assertEqual(candidate.policy_version, policy.version)
            self.assertEqual(
                candidate.sealing_policy_version,
                binding['sealing_policy_version'],
            )
            reloaded_candidate = store.load_candidate(conn, candidate.candidate_id)
            self.assertIsNotNone(reloaded_candidate)
            self.assertEqual(reloaded_candidate.policy_version, policy.version)
            self.assertEqual(
                reloaded_candidate.sealing_policy_version,
                binding['sealing_policy_version'],
            )

            job = store.enqueue_generation_job(
                conn,
                candidate,
                snapshot,
                generator_policy_version=generation.GENERATOR_POLICY_VERSION,
                prompt_policy_version=generation.PROMPT_POLICY_VERSION,
                measurement_semantics=str(binding['measurement_semantics']),
                settings_binding=binding,
                now='2026-09-27T00:00:00+00:00',
            )
            reloaded_job = store.load_generation_job(conn, job.generation_job_id)
            self.assertIsNotNone(reloaded_job)
            self.assertEqual(
                reloaded_job.settings_revision_id,
                binding['settings_revision_id'],
            )

            # This is the exact pre-model validation path; the fake provider
            # below proves that a successful preflight reaches one model call.
            self.assertIsNotNone(
                generation._frozen_binding(conn, reloaded_job, reloaded_candidate)
            )
            fake_generate = Mock(return_value=SimpleNamespace(
                text='grounded generated chunk',
                provider='claude_code',
                model_identity='explicit:claude-opus-5-5',
                actual_executor='fake-provider-once',
                usage=None,
            ))
            fake_request = Mock(
                side_effect=lambda **kwargs: SimpleNamespace(**kwargs)
            )
            materialized = SimpleNamespace(
                body='source evidence',
                source_token_estimate=100,
                source_fingerprint='binding-source-fingerprint',
            )
            with patch.object(
                generation, 'materialize_candidate', return_value=materialized
            ):
                chunk = generation.generate_continuity_chunk(
                    conn,
                    reloaded_job.generation_job_id,
                    rows_provider=(),
                    generate_fn=fake_generate,
                    request_factory=fake_request,
                    now='2026-09-27T00:01:00+00:00',
                )
            self.assertEqual(fake_generate.call_count, 1)
            self.assertEqual(chunk.status, 'ready')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
