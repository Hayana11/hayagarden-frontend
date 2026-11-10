from __future__ import annotations

import hashlib
import sqlite3

import pytest

from continuity import context_plan, store
from continuity.chunk_generation import (
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
)
from continuity.context_plan import ContextBudgetPolicy
from continuity.sealing import SealingPolicy
from continuity.sources import (
    build_source_snapshot,
    derive_autonomous_events,
    derive_completed_turns,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


@pytest.fixture(autouse=True)
def clear_process_cache(monkeypatch):
    monkeypatch.delenv('CONTINUITY_STRICT_REVALIDATE', raising=False)
    store.clear_ready_surface_cache()
    yield
    store.clear_ready_surface_cache()


@pytest.fixture
def ready_store(tmp_path):
    path = tmp_path / 'continuity.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    rows = [
        {
            'id': 1, 'author': 'hayana', 'content': 'first user', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-08 10:00:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '',
            'branches': '', 'branch_idx': 0,
        },
        {
            'id': 2, 'author': 'assistant', 'content': 'first reply', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-08 10:01:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '',
            'branches': '', 'branch_idx': 0,
        },
        {
            'id': 3, 'author': 'hayana', 'content': 'second user', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-08 10:02:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '',
            'branches': '', 'branch_idx': 0,
        },
        {
            'id': 4, 'author': 'assistant', 'content': 'second reply', 'thinking': '',
            'source_kind': 'chat', 'created_at': '2026-09-08 10:03:00',
            'attachments': '[]', 'cache_info': '', 'tool_calls': '',
            'branches': '', 'branch_idx': 0,
        },
    ]
    snapshot = build_source_snapshot(
        turns=derive_completed_turns(rows),
        events=derive_autonomous_events(rows),
        local_day='2026-09-08',
        source_watermark=4,
        created_at='2026-09-08T11:00:00Z',
    )
    store.save_source_snapshot(conn, snapshot)
    policy = SealingPolicy(
        version='test_split_policy',
        target_logical_size=1,
        max_completed_turns=1,
    )
    sealing_job = store.enqueue_job(conn, snapshot, policy)
    store.materialize_job(conn, sealing_job.job_id, policy)
    candidates = store.load_candidates(conn, sealing_job.job_id)
    assert len(candidates) >= 2
    generation_jobs = tuple(
        store.enqueue_generation_job(
            conn,
            candidate,
            snapshot,
            generator_policy_version=GENERATOR_POLICY_VERSION,
            prompt_policy_version=PROMPT_POLICY_VERSION,
            measurement_semantics=MEASUREMENT_SEMANTICS,
        )
        for candidate in candidates
    )
    conn.commit()

    def publish(index: int):
        candidate = candidates[index]
        generation_job = generation_jobs[index]
        claimed = store.claim_generation_job(
            conn,
            generation_job.generation_job_id,
            frozen_provider='claude_code',
            frozen_model_identity='model-A',
        )
        body = f'valid chunk {index}'
        chunk = store.publish_chunk_atomic(
            conn,
            job=claimed,
            candidate=candidate,
            body=body,
            body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(),
            source_token_estimate=10,
            output_token_estimate=estimate_tokens_heuristic_cjk1_ascii4_v1(body),
            provider='claude_code',
            model_identity='model-A',
            actual_executor='test',
        )
        conn.commit()
        return chunk

    yield {'conn': conn, 'path': path, 'snapshot': snapshot, 'publish': publish}
    conn.close()


def test_ready_artifact_hit_skips_full_chunk_validation(ready_store, monkeypatch):
    chunk = ready_store['publish'](0)
    calls = []
    original = store._validate_ready_chunk

    def counted(*args, **kwargs):
        calls.append(args[1].chunk_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, '_validate_ready_chunk', counted)
    first = store.read_ready_surface(ready_store['path'])
    second = store.read_ready_surface(ready_store['path'])

    assert first.status == second.status == 'ready'
    assert first.artifacts[0].chunk_id == chunk.chunk_id
    assert calls == [chunk.chunk_id]


def test_schema_contract_status_is_cached_by_path_and_schema_version(
    ready_store, monkeypatch,
):
    ready_store['publish'](0)
    calls = []
    original = store._schema_contract_status_uncached

    def counted(conn):
        calls.append(1)
        return original(conn)

    monkeypatch.setattr(store, '_schema_contract_status_uncached', counted)
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    assert len(calls) == 1

    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    assert len(calls) == 2


def test_ready_artifact_cache_miss_is_scoped_to_resolved_store_path(
    ready_store, tmp_path, monkeypatch,
):
    ready_store['publish'](0)
    copy_path = tmp_path / 'copy.db'
    copy_conn = sqlite3.connect(copy_path)
    ready_store['conn'].backup(copy_conn)
    copy_conn.close()
    calls = []
    original = store._validate_ready_chunk

    def counted(*args, **kwargs):
        calls.append(args[1].chunk_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, '_validate_ready_chunk', counted)
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    assert store.read_ready_surface(copy_path).status == 'ready'
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_strict_revalidation_detects_body_tampering_after_cache_hit(
    ready_store, monkeypatch,
):
    chunk = ready_store['publish'](0)
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    ready_store['conn'].execute('DROP TRIGGER continuity_chunks_immutable_body')
    ready_store['conn'].execute(
        "UPDATE continuity_chunks SET body='tampered' WHERE chunk_id=?",
        (chunk.chunk_id,),
    )
    store.ensure_schema(ready_store['conn'])
    ready_store['conn'].commit()
    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')

    surface = store.read_ready_surface(ready_store['path'])

    assert surface.status == 'corrupt'
    assert surface.error_code == 'chunk_body_hash_mismatch'
    monkeypatch.delenv('CONTINUITY_STRICT_REVALIDATE')
    assert store.read_ready_surface(ready_store['path']).error_code == 'chunk_body_hash_mismatch'


def test_only_new_ready_chunk_is_validated_after_warm_surface(
    ready_store, monkeypatch,
):
    first_chunk = ready_store['publish'](0)
    calls = []
    original = store._validate_ready_chunk

    def counted(*args, **kwargs):
        calls.append(args[1].chunk_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, '_validate_ready_chunk', counted)
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    second_chunk = ready_store['publish'](1)
    assert store.read_ready_surface(ready_store['path']).status == 'ready'
    assert store.read_ready_surface(ready_store['path']).status == 'ready'

    assert calls == [first_chunk.chunk_id, second_chunk.chunk_id]


def test_bindings_reuse_cached_snapshot_and_context_plan_skips_duplicate_proof(
    ready_store, monkeypatch,
):
    ready_store['publish'](0)
    surface = store.read_ready_surface(ready_store['path'])
    artifact = surface.artifacts[0]
    validated = ({
        'chunk_id': artifact.chunk_id,
        'artifact_revision': artifact.artifact_revision,
        'body_hash': artifact.body_hash,
    },)
    conn = store.open_continuity_read_only(ready_store['path'])
    try:
        bindings = __import__(
            'chat.daily_continuity_shadow', fromlist=['_bindings']
        )._bindings(conn, validated_ready_artifacts=validated)
    finally:
        conn.close()

    assert len(bindings) == 1
    assert bindings[0].ready_surface_validated is True
    expected = ready_store['snapshot'].members
    calls = []
    original = context_plan.source_hash

    def counted(members):
        calls.append(len(tuple(members)))
        return original(members)

    monkeypatch.setattr(context_plan, 'source_hash', counted)
    context_plan.build_context_plan(
        expected,
        raw_members=expected,
        chunks=bindings,
        budget_policy=ContextBudgetPolicy(token_budget=100_000),
    )
    assert calls.count(len(expected)) == 1


def test_strict_mode_keeps_context_plan_snapshot_hash_validation(
    ready_store, monkeypatch,
):
    ready_store['publish'](0)
    surface = store.read_ready_surface(ready_store['path'])
    artifact = surface.artifacts[0]
    validated = ({
        'chunk_id': artifact.chunk_id,
        'artifact_revision': artifact.artifact_revision,
        'body_hash': artifact.body_hash,
    },)
    monkeypatch.setenv('CONTINUITY_STRICT_REVALIDATE', '1')
    conn = store.open_continuity_read_only(ready_store['path'])
    try:
        bindings = __import__(
            'chat.daily_continuity_shadow', fromlist=['_bindings']
        )._bindings(conn, validated_ready_artifacts=validated)
    finally:
        conn.close()

    assert bindings[0].ready_surface_validated is False
    calls = []
    original = context_plan.source_hash

    def counted(members):
        calls.append(len(tuple(members)))
        return original(members)

    monkeypatch.setattr(context_plan, 'source_hash', counted)
    expected = ready_store['snapshot'].members
    context_plan.build_context_plan(
        expected,
        raw_members=expected,
        chunks=bindings,
        budget_policy=ContextBudgetPolicy(token_budget=100_000),
    )

    assert calls.count(len(ready_store['snapshot'].members)) == 2
