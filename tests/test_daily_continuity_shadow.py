"""R4-R4B tests for the read-only daily continuity shadow adapter."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chat.daily_continuity_shadow import (
    build_daily_continuity_shadow_plan,
)
from continuity.context_plan import ContextBudgetPolicy, ContextSection
from continuity.sources import (
    build_source_members,
    derive_autonomous_events,
    derive_completed_turns,
)
from continuity.store import (
    claim_generation_job,
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_candidates,
    materialize_job,
    publish_chunk_atomic,
    save_source_snapshot,
)
from continuity.sealing import SealingPolicy
from continuity.sources import build_source_snapshot


def _row(
    message_id: int,
    author: str,
    content: str,
    created_at: str,
    *,
    source_kind: str = 'chat',
    cache_info: str = '',
    attachments: str = '[]',
) -> dict[str, object]:
    return {
        'id': message_id,
        'author': author,
        'content': content,
        'thinking': 'must not become evidence',
        'created_at': created_at,
        'tool_calls': '',
        'branches': '',
        'branch_idx': 0,
        'cache_info': cache_info,
        'source_kind': source_kind,
        'attachments': attachments,
        'image_url': '',
        'file_url': '',
        'file_name': '',
    }


def _wake_cache() -> str:
    return json.dumps({
        'wake_mode': 'normal',
        'canonical_chat_history': True,
        'unified_chat_resident': True,
        'b3_authority': True,
        'source': 'wake',
        'provider': 'claude_code',
    })


def _history_rows() -> list[dict[str, object]]:
    return [
        _row(1, 'hayana', 'first request', '2026-09-08 23:59:00'),
        _row(2, 'assistant', 'first answer', '2026-09-09 00:01:00'),
        _row(3, 'assistant', 'canonical wake', '2026-09-09 03:59:00',
             source_kind='wake', cache_info=_wake_cache()),
        _row(4, 'assistant', 'noncanonical wake', '2026-09-09 04:00:00',
             source_kind='wake', cache_info='{"wake_mode":"morning"}'),
    ]


def _write_source(path: Path, rows: list[dict[str, object]]) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute(
        'CREATE TABLE chat_messages ('
        'id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, '
        'created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, '
        'cache_info TEXT, source_kind TEXT, attachments TEXT, image_url TEXT, '
        'file_url TEXT, file_name TEXT)'
    )
    columns = (
        'id, author, content, thinking, created_at, tool_calls, branches, branch_idx, '
        'cache_info, source_kind, attachments, image_url, file_url, file_name'
    )
    placeholders = ','.join('?' for _ in columns.split(', '))
    conn.executemany(
        f'INSERT INTO chat_messages ({columns}) VALUES ({placeholders})',
        [tuple(row[name] for name in columns.split(', ')) for row in rows],
    )
    conn.commit()
    conn.close()


def _section(kind: str, tokens: int = 1) -> ContextSection:
    return ContextSection(
        kind=kind,
        source_ref=f'{kind}:accepted',
        content_hash=f'hash:{kind}',
        estimated_tokens=tokens,
    )


class DailyContinuityShadowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.source_path = root / 'source.sqlite'
        self.store_path = root / 'shadow.sqlite'
        self.current = _row(5, 'hayana', 'current request', '2026-09-09 05:00:00')
        _write_source(self.source_path, _history_rows() + [self.current])
        conn = sqlite3.connect(str(self.store_path))
        ensure_schema(conn)
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, **kwargs):
        fixed = kwargs.pop('accepted_fixed_sections', ())
        return build_daily_continuity_shadow_plan(
            source_db_path=self.source_path,
            shadow_store_path=self.store_path,
            current_user_message_id=5,
            budget_policy=ContextBudgetPolicy(token_budget=1000, recent_raw_target=1),
            accepted_fixed_sections=fixed,
            **kwargs,
        )

    def test_source_reads_are_read_only_and_use_ro_uri(self):
        before = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        with patch('chat.daily_continuity_shadow.sqlite3.connect', wraps=sqlite3.connect) as connect:
            result = self._run()
        after = hashlib.sha256(self.source_path.read_bytes()).hexdigest()
        self.assertEqual(result.status, 'ready')
        self.assertEqual(before, after)
        ro_calls = [call for call in connect.call_args_list if call.args]
        self.assertGreaterEqual(len(ro_calls), 2)
        self.assertTrue(all('mode=ro' in str(call.args[0]) for call in ro_calls))
        self.assertTrue(all(call.kwargs.get('uri') is True for call in ro_calls))
        with self.assertRaises(sqlite3.OperationalError):
            sqlite3.connect(
                f'file:{self.source_path.as_posix()}?mode=ro', uri=True,
            ).execute('CREATE TABLE should_not_exist (id INTEGER)')

    def test_only_rows_before_current_are_history_and_current_request_once(self):
        result = self._run()
        self.assertEqual(result.source_member_count, 2)
        self.assertEqual(
            [section.kind for section in result.plan.ordered_sections],
            ['older_continuity', 'recent_raw', 'current_request'],
        )
        self.assertEqual(
            sum(section.kind == 'current_request' for section in result.plan.ordered_sections),
            1,
        )
        self.assertEqual(result.plan.ordered_sections[-1].source_ref, 'message:5')

    def test_current_incomplete_user_is_not_a_completed_turn(self):
        rows = _history_rows() + [
            _row(5, 'hayana', 'unfinished current', '2026-09-09 05:00:00'),
        ]
        _write_source(self.source_path, rows)
        result = self._run()
        self.assertEqual(result.source_member_count, 2)
        self.assertNotIn('turn:5:', [m.source_ref for m in result.plan.source_members])

    def test_assistant_current_request_is_rejected(self):
        rows = _history_rows() + [
            _row(5, 'assistant', 'assistant current', '2026-09-09 05:00:00'),
        ]
        _write_source(self.source_path, rows)
        result = self._run()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'invalid_current_request')
        self.assertIsNone(result.plan)

    def test_nonformal_user_current_request_is_rejected(self):
        rows = _history_rows() + [
            _row(5, 'hayana', 'system current', '2026-09-09 05:00:00',
                 source_kind='system'),
        ]
        _write_source(self.source_path, rows)
        result = self._run()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'invalid_current_request')
        self.assertIsNone(result.plan)

    def test_members_match_r1_derivation_and_canonical_wake_filter(self):
        rows = _history_rows()
        expected = build_source_members(
            derive_completed_turns(rows),
            derive_autonomous_events(rows),
        )
        result = self._run()
        self.assertEqual(result.plan.source_members, expected)
        self.assertEqual(
            [member.source_ref for member in result.plan.source_members],
            ['turn:1:2', 'wake:3'],
        )
        self.assertNotIn('wake:4', [member.source_ref for member in result.plan.source_members])

    def test_attachment_identity_enters_current_request_hash(self):
        first = self._run().plan.plan_hash
        rows = _history_rows() + [
            _row(5, 'hayana', 'current request', '2026-09-09 05:00:00',
                 attachments='[{"type":"image","url":"/static/uploads/a.png","name":"a.png"}]'),
        ]
        _write_source(self.source_path, rows)
        second_result = self._run()
        self.assertEqual(second_result.status, 'ready')
        self.assertIsNotNone(second_result.plan)
        self.assertNotEqual(first, second_result.plan.plan_hash)
        request = second_result.plan.ordered_sections[-1]
        self.assertEqual(request.source_ref, 'message:5')

    def test_fixed_sections_are_caller_input_and_current_request_is_adapter_owned(self):
        result = self._run(accepted_fixed_sections=(_section('accepted_state'),))
        self.assertEqual(
            [section.kind for section in result.plan.ordered_sections],
            ['accepted_state', 'older_continuity', 'recent_raw', 'current_request'],
        )
        rejected = self._run(accepted_fixed_sections=(_section('current_request'),))
        self.assertEqual(rejected.status, 'blocked')
        self.assertEqual(rejected.error_code, 'invalid_current_request')

    def test_missing_or_invalid_store_is_unavailable_not_empty(self):
        self.store_path.unlink()
        missing = self._run()
        self.assertEqual(missing.status, 'blocked')
        self.assertEqual(missing.error_code, 'chunk_surface_unavailable')
        invalid_path = Path(self.tempdir.name) / 'invalid.sqlite'
        invalid_path.write_text('not sqlite')
        result = build_daily_continuity_shadow_plan(
            source_db_path=self.source_path,
            shadow_store_path=invalid_path,
            current_user_message_id=5,
            budget_policy=ContextBudgetPolicy(token_budget=1000),
        )
        self.assertEqual(result.error_code, 'chunk_surface_unavailable')

    def test_valid_empty_store_is_empty_surface(self):
        result = self._run()
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.chunk_surface, 'empty')
        self.assertEqual(result.chunk_binding_count, 0)

    def _materialize_ready_chunk(self) -> None:
        rows = _history_rows()
        members = build_source_members(
            derive_completed_turns(rows), derive_autonomous_events(rows),
        )
        snapshot = build_source_snapshot(
            turns=derive_completed_turns(rows), events=derive_autonomous_events(rows),
            local_day='2026-09-09', source_watermark=3, created_at='2026-09-09T05:00:00Z',
        )
        policy = SealingPolicy(version='test-policy', target_logical_size=1, max_completed_turns=20)
        conn = sqlite3.connect(str(self.store_path))
        save_source_snapshot(conn, snapshot)
        job = enqueue_job(conn, snapshot, policy)
        materialize_job(conn, job.job_id, policy)
        candidate = load_candidates(conn, job.job_id)[0]
        generation = enqueue_generation_job(
            conn, candidate, snapshot,
            generator_policy_version='generator-v1',
            prompt_policy_version='prompt-v1',
            measurement_semantics='heuristic_cjk1_ascii4_v1',
        )
        generation = claim_generation_job(
            conn, generation.generation_job_id,
            frozen_provider='claude_code', frozen_model_identity='claude-test',
        )
        body = 'compressed shadow body'
        publish_chunk_atomic(
            conn, job=generation, candidate=candidate, body=body,
            body_hash=hashlib.sha256(body.encode()).hexdigest(),
            source_token_estimate=sum(member.logical_size for member in members),
            output_token_estimate=4, provider='claude_code',
            model_identity='claude-test', actual_executor='test',
        )
        conn.close()

    def test_ready_chunks_are_bound_and_read_in_deterministic_order(self):
        self._materialize_ready_chunk()
        result = self._run()
        self.assertEqual(result.status, 'ready')
        self.assertEqual(result.chunk_surface, 'ready')
        self.assertEqual(result.chunk_binding_count, 1)
        self.assertTrue(any(item.kind == 'chunk' for item in result.plan.representations))

    def test_ready_id_without_chunk_is_unavailable(self):
        self._materialize_ready_chunk()
        with patch('continuity.store.load_chunk', return_value=None):
            result = self._run()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'chunk_surface_unavailable')
        self.assertEqual(result.chunk_surface, 'unavailable')
        self.assertIsNone(result.plan)

    def test_explicit_budget_policy_is_required(self):
        result = build_daily_continuity_shadow_plan(
            source_db_path=self.source_path,
            shadow_store_path=self.store_path,
            current_user_message_id=5,
            budget_policy=None,
        )
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.error_code, 'budget_policy_unmapped')

    def test_same_input_has_same_plan_hash_and_no_runtime_import(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)
        self.assertNotIn('chat.daily_runtime', sys.modules)


if __name__ == '__main__':
    unittest.main()

