"""Explicit gate-off orchestration for Continuity R4.5-C1.

The producer discovers only the canonical current context/epoch, claims only
unclaimed source revisions, persists only a sealable prefix, and delegates
generation to the existing R3 engine.  Importing this module does not install
any scheduler or Chat consumer hook.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from continuity.chunk_generation import (
    GENERATOR_POLICY_VERSION,
    MEASUREMENT_SEMANTICS,
    PROMPT_POLICY_VERSION,
    generate_continuity_chunk,
)
from continuity.contracts import SourceMember, SourceSnapshot
from continuity.coverage import source_hash
from continuity.sealing import DEFAULT_SEALING_POLICY, CandidateBlock, SealingPolicy, seal_snapshot
from continuity.store import (
    ContinuityStoreError,
    ContinuityStoreConflict,
    enqueue_generation_job,
    enqueue_job,
    ensure_schema,
    load_claimed_source_revisions,
    load_generation_jobs,
    load_snapshot,
    materialize_job,
)
from continuity.sources import (
    POLICY_VERSION as SOURCE_POLICY_VERSION,
    build_source_members,
    derive_autonomous_events,
    derive_completed_turns,
)


ProducerStatus = Literal['idle', 'ready', 'failed', 'blocked']
MAX_GENERATION_JOBS_PER_RUN = 1


@dataclass(frozen=True)
class ContinuityProducerResult:
    """Evidence from one explicit producer run."""

    status: ProducerStatus
    error_code: str | None
    window_identity: dict[str, Any] | None
    source_member_count: int = 0
    unclaimed_source_count: int = 0
    sealed_candidate_count: int = 0
    queued_generation_job_count: int = 0
    generated_generation_job_id: str | None = None
    generated_chunk_id: str | None = None
    model_call_count: int = 0


class _ProducerBlocked(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _stamp(now: str | None) -> str:
    if now is not None:
        return str(now)
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def _local_calendar_day(now: str | None) -> str:
    """Return the Shanghai natural day used by continuity sealing."""
    if now is None:
        timestamp = dt.datetime.now(dt.timezone.utc)
    else:
        raw = str(now).strip().replace('Z', '+00:00')
        try:
            timestamp = dt.datetime.fromisoformat(raw)
        except ValueError:
            timestamp = dt.datetime.now(dt.timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d')


def _read_only_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    conn = sqlite3.connect(f'file:{resolved.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_write_connection(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(path).expanduser().resolve()))
    conn.row_factory = sqlite3.Row
    return conn


def _default_window_identity_reader(
    conn: sqlite3.Connection,
    chat_id: str,
) -> Mapping[str, Any]:
    """Use the existing canonical resolver; never select a new active window."""
    from chat.window_identity import read_current_window_identity_conn

    return read_current_window_identity_conn(conn, chat_id=chat_id)


def _validate_window_identity(raw: Mapping[str, Any], requested_chat_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _ProducerBlocked('window_identity_unavailable')
    chat_id = str(raw.get('chat_id') or requested_chat_id).strip()
    if not chat_id:
        raise _ProducerBlocked('window_identity_unavailable')
    try:
        context_id = raw.get('context_id')
        context_epoch = raw.get('context_epoch')
        if isinstance(context_id, bool) or isinstance(context_epoch, bool):
            raise ValueError
        context_id = int(context_id)
        context_epoch = int(context_epoch)
    except (TypeError, ValueError) as exc:
        raise _ProducerBlocked('window_identity_unavailable') from exc
    if context_id <= 0 or context_epoch <= 0:
        raise _ProducerBlocked('window_identity_unavailable')
    result: dict[str, Any] = {
        'chat_id': chat_id,
        'context_id': context_id,
        'context_epoch': context_epoch,
    }
    if raw.get('resident_generation') is not None:
        try:
            result['resident_generation'] = int(raw['resident_generation'])
        except (TypeError, ValueError) as exc:
            raise _ProducerBlocked('window_identity_unavailable') from exc
    return result


def _scope_rows(
    source_db_path: str | Path,
    identity: Mapping[str, Any],
) -> tuple[dict[str, object], ...]:
    """Re-read the same canonical scope from durable source storage."""
    from chat.daily_continuity_shadow import read_canonical_scope_rows

    conn = _read_only_connection(source_db_path)
    try:
        return read_canonical_scope_rows(
            conn,
            context_id=int(identity['context_id']),
            context_epoch=int(identity['context_epoch']),
        )
    finally:
        conn.close()


def _identity_and_rows(
    source_db_path: str | Path,
    *,
    chat_id: str,
    window_identity_reader: Callable[[sqlite3.Connection, str], Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[dict[str, object], ...]]:
    from chat.window_identity import REASON_UNAVAILABLE, WindowIdentityUnavailable

    try:
        conn = _read_only_connection(source_db_path)
    except (OSError, sqlite3.Error) as exc:
        raise _ProducerBlocked('source_rows_unavailable') from exc
    try:
        try:
            try:
                raw_identity = window_identity_reader(conn, chat_id)
            except WindowIdentityUnavailable as exc:
                raise _ProducerBlocked(REASON_UNAVAILABLE) from exc
            identity = _validate_window_identity(raw_identity, chat_id)
            from chat.daily_continuity_shadow import read_canonical_scope_rows

            rows = read_canonical_scope_rows(
                conn,
                context_id=identity['context_id'],
                context_epoch=identity['context_epoch'],
            )
        except _ProducerBlocked:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError, LookupError) as exc:
            raise _ProducerBlocked('source_rows_unavailable') from exc
        return identity, rows
    finally:
        conn.close()


def _source_watermark(members: Iterable[SourceMember]) -> int:
    high_water = 0
    for member in members:
        parts = str(member.source_ref).split(':')
        for part in parts[1:]:
            try:
                high_water = max(high_water, int(part))
            except (TypeError, ValueError):
                continue
    return high_water


def _snapshot_for_members(
    members: tuple[SourceMember, ...],
    *,
    identity: Mapping[str, Any],
    created_at: str,
) -> SourceSnapshot:
    if not members:
        raise ValueError('cannot create an empty producer snapshot')
    digest = source_hash(members)
    watermark = _source_watermark(members)
    local_day = str(members[0].created_at)[:10]
    snapshot_id = (
        f"source:{identity['context_id']}:{identity['context_epoch']}"
        f':{watermark}:{digest[:16]}'
    )
    return SourceSnapshot(
        snapshot_id=snapshot_id,
        identity_id='fyodor',
        chat_id=str(identity['chat_id']),
        branch_id='active-transcript',
        local_day=local_day,
        source_watermark=watermark,
        policy_version=SOURCE_POLICY_VERSION,
        source_hash=digest,
        status='ready',
        created_at=created_at,
        members=members,
        context_id=int(identity['context_id']),
        context_epoch=int(identity['context_epoch']),
    )


def _sealed_prefix(
    members: tuple[SourceMember, ...],
    *,
    identity: Mapping[str, Any],
    policy: SealingPolicy,
    created_at: str,
    current_local_day: str,
) -> tuple[SourceSnapshot, tuple[CandidateBlock, ...]]:
    # Claimed members are removed before this pass; compact seq values so the
    # immutable snapshot still satisfies the existing exact-coverage contract.
    members = tuple(
        replace(member, seq=index)
        for index, member in enumerate(members)
    )
    provisional = _snapshot_for_members(
        members,
        identity=identity,
        created_at=created_at,
    )
    candidates = seal_snapshot(
        provisional,
        policy,
        include_end_of_snapshot=False,
        close_partial_before_day=current_local_day,
    )
    if not candidates:
        return provisional, ()
    sealed_seqs = {
        int(seq)
        for candidate in candidates
        for seq in candidate.source_seqs
    }
    sealed_members = tuple(member for member in members if int(member.seq) in sealed_seqs)
    snapshot = _snapshot_for_members(
        sealed_members,
        identity=identity,
        created_at=created_at,
    )
    # Re-sealing the persisted prefix proves candidate identities are based on
    # exactly the members claimed by the stored snapshot.
    persisted_candidates = seal_snapshot(
        snapshot,
        policy,
        include_end_of_snapshot=False,
        close_partial_before_day=current_local_day,
    )
    return snapshot, persisted_candidates


def _source_rows_provider(
    source_db_path: str | Path,
    snapshot: SourceSnapshot,
) -> Callable[[], tuple[dict[str, object], ...]]:
    if snapshot.context_id is None or snapshot.context_epoch is None:
        raise _ProducerBlocked('generation_scope_identity_unavailable')
    identity = {
        'context_id': int(snapshot.context_id),
        'context_epoch': int(snapshot.context_epoch),
    }
    return lambda: _scope_rows(source_db_path, identity)


def run_continuity_producer(
    *,
    source_db_path: str | Path,
    continuity_store_path: str | Path,
    chat_id: str = 'default',
    policy: SealingPolicy = DEFAULT_SEALING_POLICY,
    window_identity_reader: Callable[[sqlite3.Connection, str], Mapping[str, Any]] | None = None,
    capture_authority: Callable[[], Any] | None = None,
    generate_fn: Callable[[Any, Any], Any] | None = None,
    request_factory: Callable[..., Any] | None = None,
    persona_reader: Callable[[], str] | None = None,
    persona_text: str | None = None,
    now: str | None = None,
) -> ContinuityProducerResult:
    """Run one explicit producer pass; never consumes a plan or writes receipt."""
    identity: dict[str, Any] | None = None
    source_member_count = 0
    unclaimed_count = 0
    sealed_count = 0
    queued_count = 0
    try:
        reader = window_identity_reader or _default_window_identity_reader
        identity, rows = _identity_and_rows(
            source_db_path,
            chat_id=chat_id,
            window_identity_reader=reader,
        )
        turns = derive_completed_turns(rows, chat_id=str(identity['chat_id']))
        events = derive_autonomous_events(rows, chat_id=str(identity['chat_id']))
        members = build_source_members(turns, events)
        source_member_count = len(members)

        store_conn = _read_write_connection(continuity_store_path)
        try:
            ensure_schema(store_conn)
            claimed = load_claimed_source_revisions(
                store_conn,
                source_refs=(member.source_ref for member in members),
            )
            unclaimed = tuple(
                member for member in members
                if (member.source_ref, member.source_revision) not in claimed
            )
            unclaimed_count = len(unclaimed)
            stamp = _stamp(now)
            if unclaimed:
                snapshot, candidates = _sealed_prefix(
                    unclaimed,
                    identity=identity,
                    policy=policy,
                    created_at=stamp,
                    current_local_day=_local_calendar_day(now),
                )
                if candidates:
                    sealed_count = len(candidates)
                    sealing_job = enqueue_job(
                        store_conn,
                        snapshot,
                        policy,
                        now=stamp,
                    )
                    stored_candidates = materialize_job(
                        store_conn,
                        sealing_job.job_id,
                        policy,
                        now=stamp,
                        include_end_of_snapshot=False,
                        close_partial_before_day=_local_calendar_day(now),
                    )
                    for candidate in stored_candidates:
                        enqueue_generation_job(
                            store_conn,
                            candidate,
                            snapshot,
                            generator_policy_version=GENERATOR_POLICY_VERSION,
                            prompt_policy_version=PROMPT_POLICY_VERSION,
                            measurement_semantics=MEASUREMENT_SEMANTICS,
                            now=stamp,
                        )
                    queued_count = len(stored_candidates)

            orphaned = load_generation_jobs(store_conn, statuses=('generating',))
            if orphaned:
                return ContinuityProducerResult(
                    status='blocked',
                    error_code='orphan_generating_job',
                    window_identity=identity,
                    source_member_count=source_member_count,
                    unclaimed_source_count=unclaimed_count,
                    sealed_candidate_count=sealed_count,
                    queued_generation_job_count=queued_count,
                )

            runnable = load_generation_jobs(
                store_conn,
                statuses=('pending', 'failed'),
            )
            if not runnable:
                return ContinuityProducerResult(
                    status='idle',
                    error_code=None,
                    window_identity=identity,
                    source_member_count=source_member_count,
                    unclaimed_source_count=unclaimed_count,
                    sealed_candidate_count=sealed_count,
                    queued_generation_job_count=queued_count,
                )

            job = runnable[0]
            snapshot = load_snapshot(store_conn, job.snapshot_id)
            if snapshot is None:
                raise ContinuityStoreError('source snapshot missing for generation job')
            rows_provider = _source_rows_provider(source_db_path, snapshot)
            call_count = 0

            def counted_generate(request: Any, authority: Any) -> Any:
                nonlocal call_count
                call_count += 1
                if generate_fn is None:
                    from continuity.chunk_generation import _default_generate

                    return _default_generate(request, authority)
                return generate_fn(request, authority)

            try:
                chunk = generate_continuity_chunk(
                    store_conn,
                    job.generation_job_id,
                    rows_provider=rows_provider,
                    capture_authority=capture_authority,
                    generate_fn=counted_generate,
                    request_factory=request_factory,
                    persona_reader=persona_reader,
                    persona_text=persona_text,
                    now=stamp,
                )
            except Exception as exc:
                failed = load_generation_jobs(
                    store_conn,
                    statuses=('failed', 'stale', 'pending', 'generating'),
                )
                current = next(
                    (item for item in failed if item.generation_job_id == job.generation_job_id),
                    None,
                )
                return ContinuityProducerResult(
                    status='failed',
                    error_code=(current.error_code if current is not None else str(exc)),
                    window_identity=identity,
                    source_member_count=source_member_count,
                    unclaimed_source_count=unclaimed_count,
                    sealed_candidate_count=sealed_count,
                    queued_generation_job_count=queued_count,
                    generated_generation_job_id=job.generation_job_id,
                    model_call_count=call_count,
                )
            return ContinuityProducerResult(
                status='ready',
                error_code=None,
                window_identity=identity,
                source_member_count=source_member_count,
                unclaimed_source_count=unclaimed_count,
                sealed_candidate_count=sealed_count,
                queued_generation_job_count=queued_count,
                generated_generation_job_id=job.generation_job_id,
                generated_chunk_id=chunk.chunk_id,
                model_call_count=call_count,
            )
        finally:
            store_conn.close()
    except _ProducerBlocked as exc:
        return ContinuityProducerResult(
            status='blocked',
            error_code=exc.error_code,
            window_identity=identity,
            source_member_count=source_member_count,
            unclaimed_source_count=unclaimed_count,
            sealed_candidate_count=sealed_count,
            queued_generation_job_count=queued_count,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError, ContinuityStoreConflict, ContinuityStoreError) as exc:
        return ContinuityProducerResult(
            status='failed',
            error_code=str(exc) or 'producer_error',
            window_identity=identity,
            source_member_count=source_member_count,
            unclaimed_source_count=unclaimed_count,
            sealed_candidate_count=sealed_count,
            queued_generation_job_count=queued_count,
        )


__all__ = [
    'ContinuityProducerResult',
    'MAX_GENERATION_JOBS_PER_RUN',
    'run_continuity_producer',
]
