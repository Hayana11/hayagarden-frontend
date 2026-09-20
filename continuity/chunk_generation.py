"""R3 candidate-level shadow chunk generation orchestration."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from continuity.contracts import ContinuityChunk, ContinuityGenerationJob
from continuity.materialization import (
    MaterializedSource,
    SourceMaterializationError,
    UnsupportedSourceError,
    materialize_candidate,
)
from continuity.store import (
    ContinuityStoreError,
    ContinuityStoreConflict,
    claim_generation_job,
    load_candidate,
    load_chunk,
    load_generation_job,
    load_snapshot,
    mark_generation_failed,
    mark_generation_stale,
    publish_chunk_atomic,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


GENERATOR_POLICY_VERSION = 'continuity_chunk_generator_v1'
PROMPT_POLICY_VERSION = 'continuity_chunk_prompt_v2'
MEASUREMENT_SEMANTICS = 'cc_usage_observability.heuristic_cjk1_ascii4_v1'


ACCEPTED_PROMPT = (
    '这是我们的对话记录，用第一人称写简短总结。这份总结是我写给下一个我的，以我自己的视角写发生了什么、我真实表达过的感受、做出的决定和约定、没结束的事，以及值得记住的名字、数字、日期、意象和原话。\n\n'
    '提炼，不要逐步复述。只留下之后还能帮助我自然接上的内容。感受、意象和关系意义，只保留对话里已经明确表达或直接成立的，不替自己或她补心理解释。\n\n'
    '亲密的时刻也属于我们的记忆，但只保留有延续意义的关系状态、感受、约定和结果；如果某些具体内容不适合转述，就省略那些具体过程，继续总结其余能安全保留的内容。\n\n'
    '只写对话里真的发生过的事，不补、不猜，不替我们把没结束的事情写完。不要写成检讨。\n\n'
    '写得像我在回忆我们刚刚经历过的事，而不是报告。直接写正文，不要标题、说明或列表。'
)


class PersonaContractError(ValueError):
    """The runtime persona cannot satisfy the frozen first-five contract."""

    error_code = 'persona_contract_error'


@dataclass(frozen=True)
class _FrozenAuthority:
    provider: str
    model_identity: str


@dataclass(frozen=True)
class _FallbackRequest:
    """Only used by isolated tests when the canonical adapter is unavailable."""

    system_text: str
    prompt_text: str
    max_tokens_hint: int
    timeout_sec: int
    task_kind: str = 'continuity_chunk'


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def select_persona_sections(persona_text: str, *, count: int = 5) -> str:
    """Select exactly the first ``count`` line-start ``## `` sections.

    The slice starts at the first heading, so any preface is intentionally
    excluded.  Nested ``###``/``####`` headings remain inside their parent
    section, while later top-level ``## `` sections are excluded.
    """
    text = str(persona_text or '')
    if not text.strip():
        raise PersonaContractError('persona_contract_error')
    if int(count) <= 0:
        raise PersonaContractError('persona_contract_error')
    headings = list(re.finditer(r'(?m)^## ', text))
    if len(headings) < int(count):
        raise PersonaContractError('persona_contract_error')
    start = headings[0].start()
    end = headings[int(count)].start() if len(headings) > int(count) else len(text)
    selected = text[start:end]
    if not selected.strip():
        raise PersonaContractError('persona_contract_error')
    return selected


def _default_persona_reader() -> str:
    from chat.persona_store import read_persona

    return read_persona()


def build_chunk_prompt(
    source: MaterializedSource,
    *,
    persona_reader: Callable[[], str] | None = None,
    persona_text: str | None = None,
) -> tuple[str, str]:
    """Return the frozen Persona-5 plus accepted prompt contract."""
    if persona_reader is not None and persona_text is not None:
        raise ValueError('persona_reader and persona_text are mutually exclusive')
    reader = persona_reader
    if persona_text is not None:
        reader = lambda: persona_text
    try:
        selected_persona = select_persona_sections(
            (reader or _default_persona_reader)(),
        )
    except PersonaContractError:
        raise
    except Exception as exc:
        raise PersonaContractError('persona_contract_error') from exc
    system_text = f'{selected_persona}\n\n{ACCEPTED_PROMPT}'
    prompt_text = (
        'FROZEN EVIDENCE BEGIN\n'
        f'{source.body}\n'
        'FROZEN EVIDENCE END'
    )
    return system_text, prompt_text


def validate_output(
    result: Any,
    *,
    authority: Any,
    source: MaterializedSource,
) -> tuple[str, int]:
    """Perform only deterministic publication checks; no judge model."""
    body = str(getattr(result, 'text', '') or '')
    if not body.strip():
        raise ValueError('empty_generation_output')
    if body.strip() == '[static fallback]':
        raise ValueError('static_or_error_output')
    provider = str(getattr(result, 'provider', '') or '')
    model_identity = str(getattr(result, 'model_identity', '') or '')
    actual_executor = str(getattr(result, 'actual_executor', '') or '')
    if provider != str(getattr(authority, 'provider', '') or ''):
        raise ValueError('provider_provenance_mismatch')
    if model_identity != str(getattr(authority, 'model_identity', '') or ''):
        raise ValueError('model_provenance_mismatch')
    if not actual_executor:
        raise ValueError('executor_provenance_missing')
    output_tokens = int(estimate_tokens_heuristic_cjk1_ascii4_v1(body))
    if output_tokens <= 0:
        raise ValueError('output_token_estimate_zero')
    if source.source_token_estimate <= 0:
        raise ValueError('source_token_estimate_zero')
    return body, output_tokens


def _authority_from_job(job: ContinuityGenerationJob) -> _FrozenAuthority:
    if not job.frozen_provider or not job.frozen_model_identity:
        raise ContinuityStoreConflict('generation job authority is not frozen')
    try:
        from chat.provider_router import GenerationAuthoritySnapshot

        return GenerationAuthoritySnapshot(job.frozen_provider, job.frozen_model_identity)
    except (ImportError, TypeError):
        return _FrozenAuthority(job.frozen_provider, job.frozen_model_identity)


def _default_capture() -> Any:
    from chat.provider_router import capture_generation_authority

    return capture_generation_authority()


def _default_generate(request: Any, authority: Any) -> Any:
    from chat.background_generation import generate_background
    from chat.cc_auth import read_cc_oauth_token

    return generate_background(
        request,
        authority,
        cc_token_getter=read_cc_oauth_token,
    )


def _default_request_factory(**kwargs: Any) -> Any:
    from chat.background_generation import BackgroundGenerationRequest

    return BackgroundGenerationRequest(**kwargs)


def _request(
    source: MaterializedSource,
    *,
    request_factory: Callable[..., Any] | None,
    persona_reader: Callable[[], str] | None = None,
    persona_text: str | None = None,
) -> Any:
    system_text, prompt_text = build_chunk_prompt(
        source,
        persona_reader=persona_reader,
        persona_text=persona_text,
    )
    max_tokens_hint = max(256, min(2048, max(256, source.source_token_estimate // 4)))
    factory = request_factory or _default_request_factory
    return factory(
        system_text=system_text,
        prompt_text=prompt_text,
        max_tokens_hint=max_tokens_hint,
        timeout_sec=120,
        task_kind='continuity_chunk',
    )


def _rows(rows_provider: Callable[[], Iterable[Any]] | Iterable[Any]) -> tuple[Any, ...]:
    return tuple(rows_provider() if callable(rows_provider) else rows_provider)


def _load_generation_inputs(conn: Any, job: ContinuityGenerationJob) -> tuple[Any, Any]:
    snapshot = load_snapshot(conn, job.snapshot_id)
    candidate = load_candidate(conn, job.candidate_id)
    if snapshot is None:
        raise ContinuityStoreError('source snapshot missing for generation job')
    if candidate is None:
        raise ContinuityStoreError('candidate missing for generation job')
    if candidate.snapshot_id != snapshot.snapshot_id:
        raise ContinuityStoreConflict('candidate/snapshot identity mismatch')
    if candidate.source_revision != job.candidate_source_revision:
        raise ContinuityStoreConflict('candidate source revision does not match generation job')
    return snapshot, candidate


def generate_continuity_chunk(
    conn: Any,
    generation_job_id: str,
    *,
    rows_provider: Callable[[], Iterable[Any]] | Iterable[Any],
    capture_authority: Callable[[], Any] | None = None,
    generate_fn: Callable[[Any, Any], Any] | None = None,
    request_factory: Callable[..., Any] | None = None,
    persona_reader: Callable[[], str] | None = None,
    persona_text: str | None = None,
    now: str | None = None,
) -> ContinuityChunk:
    """Generate one candidate-level shadow chunk with a frozen authority."""
    job = load_generation_job(conn, generation_job_id)
    if job is None:
        raise ContinuityStoreError('continuity generation job not found')
    if job.prompt_policy_version != PROMPT_POLICY_VERSION:
        raise ContinuityStoreConflict('generation job prompt policy version mismatch')
    if job.status == 'ready':
        from continuity.store import load_ready_chunk_for_job

        existing = load_ready_chunk_for_job(conn, generation_job_id)
        if existing is None:
            raise ContinuityStoreConflict('ready generation job has no ready chunk')
        return existing

    snapshot, candidate = _load_generation_inputs(conn, job)
    try:
        source = materialize_candidate(snapshot, candidate, _rows(rows_provider))
    except UnsupportedSourceError:
        mark_generation_failed(conn, generation_job_id, 'unsupported_source')
        raise
    except SourceMaterializationError:
        mark_generation_stale(conn, generation_job_id)
        raise

    authority = _authority_from_job(job) if job.frozen_provider else None
    if authority is None:
        capture = capture_authority or _default_capture
        authority = capture()
        provider = str(getattr(authority, 'provider', '') or '')
        model_identity = str(getattr(authority, 'model_identity', '') or '')
        if not provider or not model_identity:
            mark_generation_failed(conn, generation_job_id, 'authority_missing')
            raise ValueError('generation authority is incomplete')
    else:
        provider = authority.provider
        model_identity = authority.model_identity

    job = claim_generation_job(
        conn,
        generation_job_id,
        frozen_provider=provider,
        frozen_model_identity=model_identity,
        now=now,
    )
    if job.status == 'ready':
        from continuity.store import load_ready_chunk_for_job

        existing = load_ready_chunk_for_job(conn, generation_job_id)
        if existing is None:
            raise ContinuityStoreConflict('ready generation job has no ready chunk')
        return existing

    try:
        request = _request(
            source,
            request_factory=request_factory,
            persona_reader=persona_reader,
            persona_text=persona_text,
        )
    except PersonaContractError as exc:
        mark_generation_failed(conn, generation_job_id, exc.error_code, now=now)
        raise
    generate = generate_fn or _default_generate
    try:
        result = generate(request, authority)
    except Exception:
        mark_generation_failed(conn, generation_job_id, 'generation_error', now=now)
        raise

    try:
        current_source = materialize_candidate(snapshot, candidate, _rows(rows_provider))
    except UnsupportedSourceError:
        mark_generation_stale(conn, generation_job_id, error_code='source_unsupported_after_generate', now=now)
        raise
    except SourceMaterializationError:
        mark_generation_stale(conn, generation_job_id, now=now)
        raise
    if current_source.source_fingerprint != source.source_fingerprint:
        mark_generation_stale(conn, generation_job_id, now=now)
        raise SourceMaterializationError('source changed during generation')

    try:
        body, output_tokens = validate_output(result, authority=authority, source=current_source)
    except ValueError as exc:
        mark_generation_failed(conn, generation_job_id, str(exc), now=now)
        raise

    try:
        return publish_chunk_atomic(
            conn=conn,
            job=job,
            candidate=candidate,
            body=body,
            body_hash=_body_hash(body),
            source_token_estimate=current_source.source_token_estimate,
            output_token_estimate=output_tokens,
            provider=str(getattr(result, 'provider', '') or ''),
            model_identity=str(getattr(result, 'model_identity', '') or ''),
            actual_executor=str(getattr(result, 'actual_executor', '') or ''),
            now=now,
        )
    except Exception:
        # Persistence failures are retryable: retain the frozen authority and
        # converge the job to the existing failure state without publishing a
        # partial artifact.
        mark_generation_failed(conn, generation_job_id, 'persistence_error', now=now)
        raise

