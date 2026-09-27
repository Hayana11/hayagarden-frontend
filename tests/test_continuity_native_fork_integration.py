"""R2/R3 integration — native fork wiring inside generate_continuity_chunk.

Exercises the *default* execution path (no ``generate_fn`` injected) to prove
R3: eligible → native executor runs and its result is what gets published;
ineligible, or any native-path error → the existing isolated one-shot runs
unmodified, never a failed job. Every existing R3/R4/producer test keeps
injecting its own ``generate_fn`` and is therefore untouched by this module
(verified separately by the full existing suite staying green).
"""
from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest import mock

from continuity.chunk_generation import (
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
    ACCEPTED_PROMPT,
    generate_continuity_chunk,
)
from continuity.native_fork_eligibility import ContinuityForkPlan
from continuity.sealing import SealingPolicy
from continuity.sources import build_source_snapshot, derive_completed_turns
from continuity.store import (
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_candidates,
    materialize_job,
    save_source_snapshot,
)

PERSONA = '\n\n'.join(f'## Section {i}\ntext {i}' for i in range(1, 7))


def _rows():
    return (
        {
            'id': 1, 'author': 'hayana', 'content': 'user fact', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-08 23:59:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
        },
        {
            'id': 2, 'author': 'assistant', 'content': 'assistant answer', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-09 00:01:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '', 'branches': '', 'branch_idx': 0,
        },
    )


class NativeForkIntegrationTest(unittest.TestCase):
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

    def tearDown(self):
        self.conn.close()

    def _run(self):
        return generate_continuity_chunk(
            self.conn, self.job.generation_job_id,
            rows_provider=lambda: self.rows,
            capture_authority=lambda: self.authority,
            persona_reader=lambda: PERSONA,
        )

    def test_eligible_native_fork_result_is_published(self):
        eligible_plan = ContinuityForkPlan(eligible=True, reason='', generation_job_id=self.job.generation_job_id)
        native_result = SimpleNamespace(
            text='native fork summary', provider='claude_code', model_identity='model-A',
            actual_executor='claude_code_continuity_native_fork', usage={'cache_read_input_tokens': 12345},
        )
        with mock.patch(
            'continuity.native_fork_eligibility.resolve_continuity_native_fork',
            return_value=eligible_plan,
        ), mock.patch(
            'continuity.native_fork_executor.execute_continuity_native_fork',
            return_value=native_result,
        ) as execute_mock:
            chunk = self._run()
        self.assertEqual(chunk.body, 'native fork summary')
        self.assertEqual(chunk.actual_executor, 'claude_code_continuity_native_fork')
        execute_mock.assert_called_once()
        _, kwargs = execute_mock.call_args
        self.assertEqual(kwargs['prompt_body'], ACCEPTED_PROMPT)

    def test_ineligible_plan_falls_back_to_existing_one_shot(self):
        ineligible_plan = ContinuityForkPlan(eligible=False, reason='flag_off')
        fallback_result = SimpleNamespace(
            text='isolated one-shot summary', provider='claude_code', model_identity='model-A',
            actual_executor='claude_code_background_oneshot', usage=None,
        )
        with mock.patch(
            'continuity.native_fork_eligibility.resolve_continuity_native_fork',
            return_value=ineligible_plan,
        ), mock.patch(
            'continuity.chunk_generation._default_generate', return_value=fallback_result,
        ) as default_mock:
            chunk = self._run()
        self.assertEqual(chunk.body, 'isolated one-shot summary')
        self.assertEqual(chunk.actual_executor, 'claude_code_background_oneshot')
        default_mock.assert_called_once()

    def test_native_fork_execution_error_falls_back_without_failing_job(self):
        eligible_plan = ContinuityForkPlan(eligible=True, reason='', generation_job_id=self.job.generation_job_id)
        fallback_result = SimpleNamespace(
            text='isolated one-shot after native error', provider='claude_code', model_identity='model-A',
            actual_executor='claude_code_background_oneshot', usage=None,
        )
        with mock.patch(
            'continuity.native_fork_eligibility.resolve_continuity_native_fork',
            return_value=eligible_plan,
        ), mock.patch(
            'continuity.native_fork_executor.execute_continuity_native_fork',
            side_effect=RuntimeError('native boom'),
        ), mock.patch(
            'continuity.chunk_generation._default_generate', return_value=fallback_result,
        ) as default_mock:
            chunk = self._run()
        self.assertEqual(chunk.body, 'isolated one-shot after native error')
        default_mock.assert_called_once()

    def test_explicit_generate_fn_bypasses_native_fork_entirely(self):
        calls = {'count': 0}

        def explicit_generate(request, authority):
            calls['count'] += 1
            return SimpleNamespace(
                text='explicit', provider='claude_code', model_identity='model-A',
                actual_executor='explicit_test_executor', usage=None,
            )

        with mock.patch(
            'continuity.native_fork_eligibility.resolve_continuity_native_fork',
        ) as resolve_mock:
            chunk = generate_continuity_chunk(
                self.conn, self.job.generation_job_id,
                rows_provider=lambda: self.rows,
                capture_authority=lambda: self.authority,
                generate_fn=explicit_generate,
                persona_reader=lambda: PERSONA,
            )
        self.assertEqual(calls['count'], 1)
        self.assertEqual(chunk.actual_executor, 'explicit_test_executor')
        resolve_mock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
