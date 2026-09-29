"""Native-fork routing inside the real generation and producer entries.

``GenerateContinuityChunkRoutingTest`` drives ``generate_continuity_chunk``
with the resolver/executor stubbed to pin the routing rules.
``ProductionProducerEntryTest`` enters through ``run_continuity_producer`` with
no ``generate_fn`` (the production default) and runs the real eligibility
resolver and the real native executor; only process boundaries are faked
(``fork_session``, the CLI subprocess, the OAuth token, the one-shot adapter).
No real model request is made anywhere in this file.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from continuity.chunk_generation import (
    ACCEPTED_PROMPT,
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
    generate_continuity_chunk,
)
from continuity.native_fork_eligibility import FLAG_KEY, ContinuityForkPlan
from continuity.native_fork_executor import NATIVE_FORK_EXECUTOR, NativeForkGenerationError
from continuity.producer import run_continuity_producer
from continuity.sealing import SealingPolicy
from continuity.sources import build_source_snapshot, derive_completed_turns
from continuity.store import (
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_candidates,
    load_generation_job,
    load_generation_jobs,
    load_ready_chunk_for_job,
    materialize_job,
    save_source_snapshot,
)
from tools.claude_forge_subprocess import SubprocessRunResult

PERSONA = '\n\n'.join(f'## Section {i}\ntext {i}' for i in range(1, 7))
OPUS = 'claude-opus-5-5'
SONNET = 'claude-sonnet-5'


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


def _oneshot_result(text='isolated one-shot summary', model_identity='model-A'):
    return SimpleNamespace(
        text=text, provider='claude_code', model_identity=model_identity,
        actual_executor='claude_code_background_oneshot', usage=None,
    )


class GenerateContinuityChunkRoutingTest(unittest.TestCase):
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
        candidate = load_candidates(self.conn, r2_job.job_id)[0]
        self.job = enqueue_generation_job(
            self.conn, candidate, snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        self.authority = SimpleNamespace(provider='claude_code', model_identity='model-A')
        self.provider_calls = 0

    def tearDown(self):
        self.conn.close()

    def _count(self):
        self.provider_calls += 1

    def _run(self, **kwargs):
        return generate_continuity_chunk(
            self.conn, self.job.generation_job_id,
            rows_provider=lambda: self.rows,
            capture_authority=lambda: self.authority,
            persona_reader=lambda: PERSONA,
            on_provider_call=self._count,
            **kwargs,
        )

    def _patched(self, *, plan, execute=None, oneshot=None):
        stack = ExitStack()
        stack.enter_context(mock.patch(
            'continuity.native_fork_eligibility.resolve_continuity_native_fork', return_value=plan,
        ))
        execute_mock = stack.enter_context(mock.patch(
            'continuity.native_fork_executor.execute_continuity_native_fork',
            **(execute or {'return_value': None}),
        ))
        oneshot_mock = stack.enter_context(mock.patch(
            'continuity.chunk_generation._default_generate',
            return_value=oneshot or _oneshot_result(),
        ))
        return stack, execute_mock, oneshot_mock

    def test_eligible_native_result_is_published_with_frozen_prompt_only(self):
        native = SimpleNamespace(
            text='native fork summary', provider='claude_code', model_identity='model-A',
            actual_executor=NATIVE_FORK_EXECUTOR, usage={'cache_read_input_tokens': 12345},
        )

        def fake_execute(plan, **kwargs):
            kwargs['on_provider_start']()
            return native

        stack, execute_mock, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(eligible=True, reason=''),
            execute={'side_effect': fake_execute},
        )
        with stack:
            chunk = self._run()
        self.assertEqual(chunk.actual_executor, NATIVE_FORK_EXECUTOR)
        self.assertEqual(execute_mock.call_args.kwargs['prompt_body'], ACCEPTED_PROMPT)
        oneshot_mock.assert_not_called()
        self.assertEqual(self.provider_calls, 1)

    def test_mixed_turn_wake_turn_plan_uses_native_once(self):
        native = SimpleNamespace(
            text='native mixed summary', provider='claude_code', model_identity='model-A',
            actual_executor=NATIVE_FORK_EXECUTOR, usage={'cache_read_input_tokens': 12345},
        )

        def fake_execute(plan, **kwargs):
            self.assertEqual(plan.scope_completed_turns, 2)
            self.assertEqual(plan.scope_wake_count, 1)
            kwargs['on_provider_start']()
            return native

        stack, execute_mock, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(
                eligible=True, reason='', scope_completed_turns=2, scope_wake_count=1,
            ),
            execute={'side_effect': fake_execute},
        )
        with stack:
            chunk = self._run()
        self.assertEqual(chunk.actual_executor, NATIVE_FORK_EXECUTOR)
        execute_mock.assert_called_once()
        oneshot_mock.assert_not_called()
        self.assertEqual(self.provider_calls, 1)

    def test_ineligible_plan_runs_oneshot_and_records_reason(self):
        stack, execute_mock, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(eligible=False, reason='parent_model_mismatch'),
        )
        with stack:
            chunk = self._run()
        execute_mock.assert_not_called()
        oneshot_mock.assert_called_once()
        self.assertEqual(chunk.actual_executor, 'claude_code_background_oneshot')
        self.assertEqual(chunk.usage['continuity_generation_mode'], 'isolated_oneshot')
        self.assertEqual(chunk.usage['continuity_generation_fallback_reason'], 'parent_model_mismatch')
        self.assertEqual(self.provider_calls, 1)

    def test_pre_provider_native_failure_falls_back_once_and_keeps_reason(self):
        stack, _, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(eligible=True, reason=''),
            execute={'side_effect': NativeForkGenerationError('token_unavailable')},
        )
        with stack:
            chunk = self._run()
        oneshot_mock.assert_called_once()
        self.assertEqual(
            chunk.usage['continuity_generation_fallback_reason'],
            'native_fork_pre_provider:token_unavailable',
        )
        self.assertEqual(self.provider_calls, 1)

    def test_post_provider_native_failure_never_issues_second_call(self):
        def started_then_failed(plan, **kwargs):
            kwargs['on_provider_start']()
            raise NativeForkGenerationError('timeout', provider_started=True)

        stack, _, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(eligible=True, reason=''),
            execute={'side_effect': started_then_failed},
        )
        with stack:
            with self.assertRaises(NativeForkGenerationError):
                self._run()
        oneshot_mock.assert_not_called()
        self.assertEqual(self.provider_calls, 1)
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'native_fork_post_provider:timeout')

    def test_unclassified_native_exception_fails_closed_without_oneshot(self):
        stack, _, oneshot_mock = self._patched(
            plan=ContinuityForkPlan(eligible=True, reason=''),
            execute={'side_effect': RuntimeError('unexpected')},
        )
        with stack:
            with self.assertRaises(RuntimeError):
                self._run()
        oneshot_mock.assert_not_called()
        job = load_generation_job(self.conn, self.job.generation_job_id)
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'generation_error')

    def test_explicit_generate_fn_bypasses_native_route(self):
        calls = []

        def explicit(request, authority):
            calls.append(1)
            return SimpleNamespace(
                text='explicit', provider='claude_code', model_identity='model-A',
                actual_executor='explicit_test_executor', usage=None,
            )

        with mock.patch('continuity.native_fork_eligibility.resolve_continuity_native_fork') as resolve_mock:
            chunk = self._run(generate_fn=explicit)
        resolve_mock.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(chunk.actual_executor, 'explicit_test_executor')
        self.assertIsNone(chunk.usage)
        self.assertEqual(self.provider_calls, 1)


_REGISTRY_DDL = '''CREATE TABLE context_claude_sessions (
    context_id INTEGER NOT NULL, context_epoch INTEGER NOT NULL,
    resident_generation INTEGER NOT NULL, chat_id TEXT NOT NULL,
    claude_session_id TEXT NOT NULL, transcript_path TEXT NOT NULL,
    source TEXT NOT NULL, process_generation INTEGER NULL,
    scan_offset INTEGER NOT NULL DEFAULT 0, scan_status TEXT NOT NULL DEFAULT 'READY',
    scan_error_code TEXT NULL, last_mapped_message_id INTEGER NULL,
    created_at DATETIME, updated_at DATETIME,
    PRIMARY KEY (context_id, resident_generation))'''

_MAPPING_DDL = '''CREATE TABLE chat_message_claude_events (
    event_uuid TEXT NOT NULL PRIMARY KEY, message_id INTEGER NOT NULL,
    role TEXT NOT NULL, claude_session_id TEXT NOT NULL,
    context_id INTEGER NOT NULL, context_epoch INTEGER NOT NULL,
    resident_generation INTEGER NOT NULL, jsonl_byte_offset INTEGER NULL,
    created_at DATETIME)'''


class ProductionProducerEntryTest(unittest.TestCase):
    """Default ``run_continuity_producer`` path: no generate_fn injected."""

    PARENT = 'parent-sid'
    CHILD = 'child-sid'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source_path = root / 'source.db'
        self.store_path = root / 'continuity.db'
        self.home = root / 'home'
        self.home.mkdir()
        self.identity = {'chat_id': 'default', 'context_id': 7, 'context_epoch': 3, 'resident_generation': 99}
        self.parent_model = OPUS
        self.frozen_identity = f'explicit:{OPUS}'
        self.subprocess_calls = 0
        self.oneshot_calls = 0
        self.token = 'fake-token'
        self.run_result = None

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {'HOME': str(self.home)}))
        stack.enter_context(mock.patch(
            'config_store.get_bool', side_effect=lambda key, default=False: True if key == FLAG_KEY else default,
        ))
        stack.enter_context(mock.patch(
            'chat.window_identity.read_current_window_identity_conn',
            side_effect=lambda _conn, chat_id='default': dict(self.identity),
        ))
        stack.enter_context(mock.patch(
            'continuity.native_fork_executor.import_fork_session',
            return_value=(self._fake_fork, None),
        ))
        stack.enter_context(mock.patch(
            'chat.cc_runtime.require_managed_claude_runtime', return_value='2.1.280',
        ))
        stack.enter_context(mock.patch(
            'chat.cc_runtime.claude_cmd_for_version',
            side_effect=lambda _version, *args, **kwargs: ['/managed/claude', *args],
        ))
        stack.enter_context(mock.patch(
            'tools.cc_capability_adapter.build_uh_a0_spawn_plan',
            side_effect=lambda **_kwargs: {
                'built_in_tools_csv': 'Read,Glob,Grep',
                'spawn_extra_args': [
                    '--settings', '/parent/cc-settings-uh-a0.json',
                    '--mcp-config', '/parent/cc-tools-uh-a0.json',
                    '--strict-mcp-config',
                    '--allowedTools', 'Read,Glob,Grep',
                    '--disallowedTools', 'Bash',
                ],
                'surface_allowlist_csv': 'Read,Glob,Grep',
                'mcp_config_path': '/parent/cc-tools-uh-a0.json',
                'physical_surface_fingerprint': 'surface-fp',
            },
        ))
        stack.enter_context(mock.patch(
            'chat.system_builder.build_cc_static_parts',
            return_value={'full_system': 'MAIN-CHAT-SYSTEM', 'persona': 'PERSONA'},
        ))
        stack.enter_context(mock.patch(
            'chat.cc_effort.cc_effort_snapshot',
            return_value=('', 'default', []),
        ))
        stack.enter_context(mock.patch('chat.cc_auth.read_cc_oauth_token', side_effect=lambda: self.token))
        stack.enter_context(mock.patch(
            'continuity.native_fork_executor.run_subprocess_with_timeout', side_effect=self._fake_subprocess,
        ))
        stack.enter_context(mock.patch(
            'continuity.chunk_generation._default_generate', side_effect=self._fake_oneshot,
        ))

        from chat.cc_runtime import repo_root
        from tools.cc_jsonl_usage import session_jsonl_path

        self.cwd = str(repo_root())
        self.parent_cwd = str(root / 'parent-project')
        Path(self.parent_cwd).mkdir()
        self.parent_path = session_jsonl_path(self.parent_cwd, self.PARENT)
        self.child_path = session_jsonl_path(self.parent_cwd, self.CHILD)
        self._write_source()

    def _write_source(self):
        conn = sqlite3.connect(str(self.source_path))
        conn.execute(
            'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, thinking TEXT, '
            'created_at TEXT, tool_calls TEXT, branches TEXT, branch_idx INTEGER, cache_info TEXT, '
            'source_kind TEXT, attachments TEXT, image_url TEXT, file_url TEXT, file_name TEXT)'
        )
        conn.execute(
            'CREATE TABLE daily_message_contexts (message_id INTEGER PRIMARY KEY, context_id INTEGER NOT NULL, '
            'context_epoch INTEGER NOT NULL, resident_generation INTEGER NOT NULL, role TEXT NOT NULL)'
        )
        conn.execute(
            'CREATE TABLE wake_log (id INTEGER PRIMARY KEY, wake_run_id TEXT, chat_id TEXT, '
            'context_id INTEGER, context_epoch INTEGER, resident_generation INTEGER)'
        )
        conn.execute(_REGISTRY_DDL)
        conn.execute(_MAPPING_DDL)
        for index in range(2):
            user_id, assistant_id = 1 + index * 2, 2 + index * 2
            for mid, author, text, minute in (
                (user_id, 'hayana', f'request {index}', f'01:{index:02d}:00'),
                (assistant_id, 'assistant', f'answer {index}', f'01:{index:02d}:30'),
            ):
                conn.execute(
                    'INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (mid, author, text, '', f'2026-09-13 {minute}', '', '', 0, '', 'chat', '[]', '', '', ''),
                )
                conn.execute(
                    'INSERT INTO daily_message_contexts VALUES (?,?,?,?,?)',
                    (mid, 7, 3, 99, 'user' if author == 'hayana' else 'assistant'),
                )
        conn.execute(
            'INSERT INTO context_claude_sessions (context_id, context_epoch, resident_generation, chat_id, '
            "claude_session_id, transcript_path, source, scan_status) VALUES (7, 3, 99, 'default', ?, ?, 'test', 'READY')",
            (self.PARENT, str(self.parent_path)),
        )
        conn.execute(
            'INSERT INTO chat_message_claude_events (event_uuid, message_id, role, claude_session_id, '
            "context_id, context_epoch, resident_generation, jsonl_byte_offset) VALUES ('ev-last', 4, 'assistant', ?, 7, 3, 99, 10)",
            (self.PARENT,),
        )
        conn.commit()
        conn.close()

    def _write_parent(self, *, seconds_ago=60.0):
        stamp = dt.datetime.fromtimestamp(time.time() - seconds_ago, tz=dt.timezone.utc)
        event = {
            'type': 'assistant', 'uuid': 'ev-last', 'requestId': 'req-1',
            'timestamp': stamp.isoformat().replace('+00:00', 'Z'),
            'cwd': self.parent_cwd,
            'message': {'model': self.parent_model, 'usage': {'input_tokens': 1, 'output_tokens': 1}},
        }
        self.parent_path.parent.mkdir(parents=True, exist_ok=True)
        self.parent_path.write_text(
            json.dumps({'type': 'user', 'uuid': 'u', 'cwd': self.parent_cwd})
            + '\n' + json.dumps(event) + '\n'
        )

    def _fake_fork(self, session_id, *, directory, up_to_message_id, title):
        assert session_id == self.PARENT and up_to_message_id == 'ev-last'
        self.fork_directory = directory
        self.child_path.write_text(self.parent_path.read_text())
        return SimpleNamespace(session_id=self.CHILD)

    def _fake_subprocess(self, **kwargs):
        self.subprocess_calls += 1
        self.subprocess_cwd = kwargs['cwd']
        self.subprocess_cmd = kwargs.get('cmd')
        self.subprocess_stdin = kwargs.get('stdin_payload')
        if self.run_result is not None:
            return self.run_result
        lines = [
            json.dumps({'type': 'system', 'subtype': 'init', 'model': self.parent_model}),
            json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                        'result': 'native summary', 'usage': {'input_tokens': 40, 'cache_read_input_tokens': 88000}}),
        ]
        return SubprocessRunResult(exit_code=0, stdout_lines=[l + '\n' for l in lines], process_started=True)

    def _fake_oneshot(self, request, authority):
        self.oneshot_calls += 1
        return _oneshot_result(text='oneshot summary', model_identity=authority.model_identity)

    def _produce(self, generate_fn=None):
        return run_continuity_producer(
            source_db_path=self.source_path,
            continuity_store_path=self.store_path,
            policy=SealingPolicy(),
            window_identity_reader=lambda _conn, _chat_id: self.identity,
            capture_authority=lambda: SimpleNamespace(provider='claude_code', model_identity=self.frozen_identity),
            generate_fn=generate_fn,
            request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            persona_text=PERSONA,
            now='2026-09-14T00:00:00+00:00',
        )

    def _chunk(self, result):
        conn = sqlite3.connect(str(self.store_path))
        conn.row_factory = sqlite3.Row
        try:
            return load_ready_chunk_for_job(conn, result.generated_generation_job_id)
        finally:
            conn.close()

    def _job(self, result):
        conn = sqlite3.connect(str(self.store_path))
        conn.row_factory = sqlite3.Row
        try:
            return load_generation_job(conn, result.generated_generation_job_id)
        finally:
            conn.close()

    def test_default_producer_reaches_native_route(self):
        self._write_parent()
        before = self.parent_path.read_bytes()
        result = self._produce()
        self.assertEqual(result.status, 'ready', result)
        self.assertEqual(self.subprocess_calls, 1)
        self.assertEqual(self.oneshot_calls, 0)
        self.assertEqual(result.model_call_count, 1)
        self.assertEqual(self._chunk(result).actual_executor, NATIVE_FORK_EXECUTOR)
        self.assertEqual(self.fork_directory, self.parent_cwd)
        self.assertEqual(self.subprocess_cwd, self.parent_cwd)
        self.assertEqual(self.parent_path.read_bytes(), before)
        cmd = self.subprocess_cmd
        self.assertNotEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertNotEqual(cmd[cmd.index('--allowedTools') + 1], '')
        self.assertEqual(cmd[cmd.index('--system-prompt') + 1], 'MAIN-CHAT-SYSTEM')
        self.assertNotIn('--safe-mode', cmd)
        self.assertIn('不要调用任何工具，只根据当前已继承的对话内容完成压缩。', self.subprocess_stdin)

    def test_default_producer_different_compression_model_uses_oneshot(self):
        self._write_parent()
        self.frozen_identity = f'explicit:{SONNET}'
        result = self._produce()
        self.assertEqual(result.status, 'ready', result)
        self.assertEqual(self.subprocess_calls, 0)
        self.assertEqual(self.oneshot_calls, 1)
        self.assertEqual(result.model_call_count, 1)
        chunk = self._chunk(result)
        self.assertEqual(chunk.model_identity, f'explicit:{SONNET}')
        self.assertEqual(chunk.usage['continuity_generation_fallback_reason'], 'parent_model_mismatch')

    def test_default_producer_cold_parent_uses_oneshot(self):
        from cc_resident import STALE_CACHE_MAX_AGE_SECONDS

        self._write_parent(seconds_ago=STALE_CACHE_MAX_AGE_SECONDS + 5)
        result = self._produce()
        self.assertEqual(result.status, 'ready', result)
        self.assertEqual((self.subprocess_calls, self.oneshot_calls, result.model_call_count), (0, 1, 1))
        self.assertEqual(self._chunk(result).usage['continuity_generation_fallback_reason'], 'parent_cold')

    def test_default_producer_pre_provider_failure_falls_back_once(self):
        self._write_parent()
        self.token = ''
        result = self._produce()
        self.assertEqual(result.status, 'ready', result)
        self.assertEqual((self.subprocess_calls, self.oneshot_calls, result.model_call_count), (0, 1, 1))
        self.assertEqual(
            self._chunk(result).usage['continuity_generation_fallback_reason'],
            'native_fork_pre_provider:token_unavailable',
        )

    def test_default_producer_post_provider_failure_never_calls_twice(self):
        self._write_parent()
        self.run_result = SubprocessRunResult(exit_code=-1, stdout_lines=[], process_started=True, timed_out=True)
        result = self._produce()
        self.assertEqual(result.status, 'failed', result)
        self.assertEqual((self.subprocess_calls, self.oneshot_calls, result.model_call_count), (1, 0, 1))
        self.assertEqual(result.error_code, 'native_fork_post_provider:timeout')
        self.assertEqual(self._job(result).status, 'failed')

    def test_explicit_generate_fn_through_producer_skips_native_route(self):
        self._write_parent()
        calls = []

        def explicit(request, authority):
            calls.append(1)
            return SimpleNamespace(
                text='explicit', provider='claude_code', model_identity=authority.model_identity,
                actual_executor='explicit_test_executor', usage=None,
            )

        result = self._produce(generate_fn=explicit)
        self.assertEqual(result.status, 'ready', result)
        self.assertEqual((len(calls), self.subprocess_calls, self.oneshot_calls, result.model_call_count), (1, 0, 0, 1))


if __name__ == '__main__':
    unittest.main()
