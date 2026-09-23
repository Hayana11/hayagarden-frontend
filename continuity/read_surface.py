"""Read-only Continuity surface for the context-compression page.

This module answers history / detail / current-block GET queries from durable
storage.  It never opens the producer, never writes settings, never enqueues
generation, and never mutates resident or window identity.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from continuity.contracts import SourceMember, SourceSnapshot
from continuity.coverage import source_hash
from continuity.materialization import (
    SourceMaterializationError,
    materialize_candidate,
    materialize_source_members,
)
from continuity.sealing import (
    DEFAULT_SEALING_POLICY,
    CandidateBlock,
    SealingPolicy,
    seal_snapshot,
)
from continuity.sources import POLICY_VERSION as SOURCE_POLICY_VERSION
from continuity.sources import build_source_members, derive_autonomous_events, derive_completed_turns
from continuity.store import (
    load_candidate,
    load_claimed_source_revisions,
    load_generation_job,
    load_ready_chunk_for_job,
    load_snapshot,
    open_continuity_read_only,
)

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})
_EMPTY_CURRENT = {
    'ok': True,
    'available': False,
    'source_count': 0,
    'completed_turn_count': 0,
    'logical_size': 0,
    'original_char_count': 0,
    'start_at': None,
    'end_at': None,
    'body': None,
    'messages': [],
    'size_target': int(DEFAULT_SEALING_POLICY.target_logical_size),
    'turn_target': int(DEFAULT_SEALING_POLICY.max_completed_turns),
    'size_progress': 0,
    'turn_progress': 0,
    'source_refs': [],
    'settings_revision_id': None,
    'chat_id': None,
    'context_id': None,
    'context_epoch': None,
    'threshold_reached': False,
    'processing_state': 'waiting',
    'remaining_logical_size': int(DEFAULT_SEALING_POLICY.target_logical_size),
    'remaining_completed_turns': int(DEFAULT_SEALING_POLICY.max_completed_turns),
    'materialization_error': None,
}


def open_read_only(path: str | Path) -> sqlite3.Connection:
    """Open the Continuity/chat store without creating or writing it."""
    conn = open_continuity_read_only(path)
    conn.execute('PRAGMA query_only=ON')
    return conn


def _stamp(now: str | None) -> str:
    if now is not None:
        return str(now)
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def _local_calendar_day(now: str | None) -> str:
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


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


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
        raise ValueError('cannot create an empty read-surface snapshot')
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
    # In-memory only. Copied from the producer so this surface never imports it.
    members = tuple(replace(member, seq=index) for index, member in enumerate(members))
    provisional = _snapshot_for_members(members, identity=identity, created_at=created_at)
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
    snapshot = _snapshot_for_members(sealed_members, identity=identity, created_at=created_at)
    persisted_candidates = seal_snapshot(
        snapshot,
        policy,
        include_end_of_snapshot=False,
        close_partial_before_day=current_local_day,
    )
    return snapshot, persisted_candidates


def _row_value(row: Any, key: str, default: Any = '') -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _message_role(row: Any, fallback: str) -> str:
    author = str(_row_value(row, 'author', '') or '').strip().lower()
    if author in _USER_AUTHORS or fallback == 'user':
        return 'user'
    return 'assistant'


def _message_from_row(row: Any, *, source_ref: str, role: str) -> dict[str, Any]:
    message_id = _row_value(row, 'id', source_ref)
    return {
        'id': str(message_id),
        'role': _message_role(row, role),
        'content': str(_row_value(row, 'content', '') or ''),
        'created_at': str(_row_value(row, 'created_at', '') or '') or None,
        'source_ref': source_ref,
    }


def _messages_for_members(
    members: Iterable[SourceMember],
    rows: Iterable[Any],
) -> list[dict[str, Any]]:
    by_id: dict[int, Any] = {}
    for row in rows:
        try:
            by_id[int(_row_value(row, 'id', 0) or 0)] = row
        except (TypeError, ValueError):
            continue
    messages: list[dict[str, Any]] = []
    for member in members:
        source_ref = str(member.source_ref)
        parts = source_ref.split(':')
        if member.source_kind == 'completed_turn' and len(parts) == 3:
            for raw_id, role in ((parts[1], 'user'), (parts[2], 'assistant')):
                try:
                    row = by_id.get(int(raw_id))
                except (TypeError, ValueError):
                    row = None
                if row is not None:
                    messages.append(_message_from_row(row, source_ref=source_ref, role=role))
        elif member.source_kind == 'autonomous_event' and len(parts) == 2:
            try:
                row = by_id.get(int(parts[1]))
            except (TypeError, ValueError):
                row = None
            if row is not None:
                messages.append(_message_from_row(row, source_ref=source_ref, role='assistant'))
    return messages


def _span_from_timestamps(values: Iterable[str | None]) -> tuple[str | None, str | None]:
    stamps = [str(value) for value in values if str(value or '').strip()]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def _span_from_members(members: Iterable[SourceMember]) -> tuple[str | None, str | None]:
    return _span_from_timestamps(member.created_at for member in members)


def _span_from_messages(messages: Iterable[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    return _span_from_timestamps(item.get('created_at') for item in messages)


def _map_status(job_status: str | None, chunk_status: str | None) -> str:
    if chunk_status == 'ready':
        return 'complete'
    if job_status in ('pending', 'generating'):
        return 'compressing'
    if job_status == 'failed':
        return 'failed'
    return 'unavailable'


def _latest_generation_job(conn: sqlite3.Connection, candidate_id: str):
    if not _has_table(conn, 'continuity_generation_jobs'):
        return None
    row = conn.execute(
        'SELECT generation_job_id FROM continuity_generation_jobs '
        'WHERE candidate_id=? ORDER BY created_at DESC, generation_job_id DESC LIMIT 1',
        (candidate_id,),
    ).fetchone()
    if row is None:
        return None
    return load_generation_job(conn, str(row[0]))


def _scope_rows(
    conn: sqlite3.Connection,
    *,
    context_id: int | None,
    context_epoch: int | None,
) -> tuple[dict[str, object], ...]:
    if context_id is None or context_epoch is None:
        return ()
    from chat.daily_continuity_shadow import read_canonical_scope_rows

    return read_canonical_scope_rows(
        conn,
        context_id=int(context_id),
        context_epoch=int(context_epoch),
    )


def _percent(value: int, target: int) -> float:
    if target <= 0:
        return 0.0
    return min(100.0, max(0.0, 100.0 * float(value) / float(target)))


def _unavailable_current(
    *,
    identity: Mapping[str, Any] | None = None,
    error: str | None = None,
    policy: SealingPolicy = DEFAULT_SEALING_POLICY,
) -> dict[str, Any]:
    payload = dict(_EMPTY_CURRENT)
    payload['size_target'] = int(policy.target_logical_size)
    payload['turn_target'] = int(policy.max_completed_turns)
    payload['remaining_logical_size'] = int(policy.target_logical_size)
    payload['remaining_completed_turns'] = int(policy.max_completed_turns)
    payload['materialization_error'] = error
    if identity is not None:
        payload['chat_id'] = identity.get('chat_id')
        payload['context_id'] = identity.get('context_id')
        payload['context_epoch'] = identity.get('context_epoch')
    return payload


def _make_block(
    conn: sqlite3.Connection,
    candidate: CandidateBlock,
    *,
    rows_cache: dict[tuple[int, int], tuple[dict[str, object], ...]],
    include_messages: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, list[dict[str, Any]]]:
    snapshot = load_snapshot(conn, candidate.snapshot_id)
    job = _latest_generation_job(conn, candidate.candidate_id)
    chunk = load_ready_chunk_for_job(conn, job.generation_job_id) if job is not None else None
    job_status = job.status if job is not None else None
    chunk_status = chunk.status if chunk is not None else None
    provider = None
    model = None
    if chunk is not None:
        provider = chunk.provider or None
        model = chunk.model_identity or None
    elif job is not None:
        provider = job.frozen_provider
        model = job.frozen_model_identity

    start_at, end_at = _span_from_members(
        tuple(
            member for member in (snapshot.members if snapshot is not None else ())
            if member.source_ref in set(candidate.source_refs)
        )
    )
    messages: list[dict[str, Any]] = []
    original_char_count = None
    materialization_available = False
    materialization_error = None
    source_payload = None
    if snapshot is not None:
        source_payload = {
            'snapshot_id': snapshot.snapshot_id,
            'source_hash': snapshot.source_hash,
            'context_id': snapshot.context_id,
            'context_epoch': snapshot.context_epoch,
            'members': [
                {
                    'seq': member.seq,
                    'source_kind': member.source_kind,
                    'source_ref': member.source_ref,
                    'source_revision': member.source_revision,
                    'logical_size': member.logical_size,
                    'created_at': member.created_at,
                }
                for member in snapshot.members
                if member.source_ref in set(candidate.source_refs)
            ],
        }
        cache_key = None
        if snapshot.context_id is not None and snapshot.context_epoch is not None:
            cache_key = (int(snapshot.context_id), int(snapshot.context_epoch))
        rows: tuple[dict[str, object], ...] = ()
        if cache_key is not None:
            if cache_key not in rows_cache:
                try:
                    rows_cache[cache_key] = _scope_rows(
                        conn,
                        context_id=cache_key[0],
                        context_epoch=cache_key[1],
                    )
                except (OSError, sqlite3.Error, TypeError, ValueError, LookupError) as exc:
                    rows_cache[cache_key] = ()
                    materialization_error = str(exc) or 'source_rows_unavailable'
            rows = rows_cache[cache_key]
        if rows:
            try:
                materialized = materialize_candidate(snapshot, candidate, rows)
                selected = tuple(
                    member for member in snapshot.members
                    if member.source_ref in set(candidate.source_refs)
                )
                messages = _messages_for_members(selected, rows)
                original_char_count = sum(len(item['content']) for item in messages)
                if original_char_count == 0:
                    original_char_count = len(materialized.body)
                materialization_available = True
                message_span = _span_from_messages(messages)
                if message_span[0]:
                    start_at, end_at = message_span
            except SourceMaterializationError as exc:
                materialization_error = str(exc)
                messages = []
        elif materialization_error is None and (
            snapshot.context_id is None or snapshot.context_epoch is None
        ):
            materialization_error = 'generation_scope_identity_unavailable'

    block = {
        'candidate_id': candidate.candidate_id,
        'local_day': candidate.local_day,
        'start_at': start_at,
        'end_at': end_at,
        'completed_turn_count': int(candidate.completed_turn_count),
        'logical_size': int(candidate.logical_size),
        'original_char_count': original_char_count,
        'compressed_size': int(chunk.output_token_estimate) if chunk is not None else None,
        'compressed_char_count': len(chunk.body) if chunk is not None else None,
        'status': _map_status(job_status, chunk_status),
        'included': None,
        'provider': provider,
        'model': model,
        'generation_job_status': job_status,
        'chunk_status': chunk_status,
        'materialization_available': materialization_available,
        'materialization_error': materialization_error,
    }
    chunk_payload = None
    if chunk is not None:
        chunk_payload = {
            'body': chunk.body,
            'chunk_id': chunk.chunk_id,
            'status': chunk.status,
            'provider': chunk.provider,
            'model_identity': chunk.model_identity,
            'output_token_estimate': chunk.output_token_estimate,
            'source_token_estimate': chunk.source_token_estimate,
            'created_at': chunk.created_at,
            'generation_job_id': chunk.generation_job_id,
            'artifact_revision': chunk.artifact_revision,
        }
    if not include_messages:
        messages = []
    return block, source_payload, chunk_payload, messages


def _default_window_identity_reader(
    conn: sqlite3.Connection,
    chat_id: str,
) -> Mapping[str, Any]:
    from chat.window_identity import read_current_window_identity_conn

    return read_current_window_identity_conn(conn, chat_id=chat_id)


def _validate_window_identity(raw: Mapping[str, Any], requested_chat_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    chat_id = str(raw.get('chat_id') or requested_chat_id).strip()
    try:
        context_id = raw.get('context_id')
        context_epoch = raw.get('context_epoch')
        if isinstance(context_id, bool) or isinstance(context_epoch, bool):
            raise ValueError
        context_id = int(context_id)
        context_epoch = int(context_epoch)
    except (TypeError, ValueError):
        return None
    if not chat_id or context_id <= 0 or context_epoch <= 0:
        return None
    result: dict[str, Any] = {
        'chat_id': chat_id,
        'context_id': context_id,
        'context_epoch': context_epoch,
    }
    if raw.get('resident_generation') is not None:
        try:
            result['resident_generation'] = int(raw['resident_generation'])
        except (TypeError, ValueError):
            return None
    return result


def list_blocks(*, db_path: str | Path) -> dict[str, Any]:
    """Return persisted candidate history for the page list."""
    try:
        conn = open_read_only(db_path)
    except (OSError, sqlite3.Error):
        return {'ok': True, 'count': 0, 'included_authority': 'unknown', 'blocks': []}
    try:
        if not _has_table(conn, 'continuity_candidate_blocks'):
            return {'ok': True, 'count': 0, 'included_authority': 'unknown', 'blocks': []}
        rows = conn.execute(
            'SELECT candidate_id FROM continuity_candidate_blocks '
            'ORDER BY local_day DESC, created_at DESC, block_seq DESC, candidate_id DESC'
        ).fetchall()
        cache: dict[tuple[int, int], tuple[dict[str, object], ...]] = {}
        blocks: list[dict[str, Any]] = []
        for row in rows:
            candidate = load_candidate(conn, str(row[0]))
            if candidate is None:
                continue
            block, _source, _chunk, _messages = _make_block(
                conn, candidate, rows_cache=cache, include_messages=False,
            )
            blocks.append(block)
        return {
            'ok': True,
            'count': len(blocks),
            'included_authority': 'unknown',
            'blocks': blocks,
        }
    finally:
        conn.close()


def get_block_detail(*, db_path: str | Path, candidate_id: str) -> dict[str, Any] | None:
    """Return one persisted candidate with source, chunk, and original messages."""
    try:
        conn = open_read_only(db_path)
    except (OSError, sqlite3.Error):
        return None
    try:
        if not _has_table(conn, 'continuity_candidate_blocks'):
            return None
        candidate = load_candidate(conn, str(candidate_id))
        if candidate is None:
            return None
        cache: dict[tuple[int, int], tuple[dict[str, object], ...]] = {}
        block, source, chunk, messages = _make_block(
            conn, candidate, rows_cache=cache, include_messages=True,
        )
        return {
            'ok': True,
            'block': block,
            'source': source,
            'chunk': chunk,
            'messages': messages,
        }
    finally:
        conn.close()


def get_current(
    *,
    db_path: str | Path,
    chat_id: str = 'default',
    window_identity_reader: Callable[[sqlite3.Connection, str], Mapping[str, Any]] | None = None,
    policy: SealingPolicy = DEFAULT_SEALING_POLICY,
    now: str | None = None,
) -> dict[str, Any]:
    """Return the live unclaimed tail and its sealing progress.

    History already claimed by immutable snapshots is subtracted.  In-memory
    sealing decides ``threshold_reached`` only; this function never persists
    a snapshot, candidate, or generation job.
    """
    try:
        conn = open_read_only(db_path)
    except (OSError, sqlite3.Error):
        return _unavailable_current(error='source_rows_unavailable', policy=policy)
    try:
        from chat.window_identity import WindowIdentityUnavailable

        reader = window_identity_reader or _default_window_identity_reader
        try:
            identity = _validate_window_identity(reader(conn, chat_id), chat_id)
        except WindowIdentityUnavailable as exc:
            return _unavailable_current(error=str(exc) or 'window_identity_unavailable', policy=policy)
        except sqlite3.Error as exc:
            return _unavailable_current(error=str(exc) or 'window_identity_unavailable', policy=policy)
        if identity is None:
            return _unavailable_current(error='window_identity_unavailable', policy=policy)
        try:
            rows = _scope_rows(
                conn,
                context_id=int(identity['context_id']),
                context_epoch=int(identity['context_epoch']),
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, LookupError) as exc:
            return _unavailable_current(
                identity=identity,
                error=str(exc) or 'source_rows_unavailable',
                policy=policy,
            )
        members = build_source_members(
            derive_completed_turns(rows, chat_id=str(identity['chat_id'])),
            derive_autonomous_events(rows, chat_id=str(identity['chat_id'])),
        )
        if _has_table(conn, 'continuity_source_members'):
            claimed = load_claimed_source_revisions(
                conn,
                source_refs=(member.source_ref for member in members),
            )
        else:
            claimed = frozenset()
        unclaimed = tuple(
            member for member in members
            if (member.source_ref, member.source_revision) not in claimed
        )
        if not unclaimed:
            return _unavailable_current(identity=identity, policy=policy)

        stamp = _stamp(now)
        threshold_reached = False
        try:
            _snapshot, sealed_candidates = _sealed_prefix(
                unclaimed,
                identity=identity,
                policy=policy,
                created_at=stamp,
                current_local_day=_local_calendar_day(now),
            )
            threshold_reached = bool(sealed_candidates)
        except ValueError:
            threshold_reached = False

        completed_turn_count = sum(
            1 for member in unclaimed if member.source_kind == 'completed_turn'
        )
        logical_size = sum(int(member.logical_size) for member in unclaimed)
        size_target = int(policy.target_logical_size)
        turn_target = int(policy.max_completed_turns)
        if logical_size >= size_target or completed_turn_count >= turn_target:
            threshold_reached = True

        messages = _messages_for_members(unclaimed, rows)
        original_char_count = sum(len(item['content']) for item in messages)
        start_at, end_at = _span_from_messages(messages)
        if start_at is None:
            start_at, end_at = _span_from_members(unclaimed)
        body = None
        materialization_error = None
        try:
            body = materialize_source_members(unclaimed, rows).body
            if original_char_count == 0:
                original_char_count = len(body)
        except SourceMaterializationError as exc:
            materialization_error = str(exc)
            body = None

        processing_state = 'threshold_reached' if threshold_reached else 'waiting'
        return {
            'ok': True,
            'available': True,
            'source_count': len(unclaimed),
            'completed_turn_count': completed_turn_count,
            'logical_size': logical_size,
            'original_char_count': original_char_count,
            'start_at': start_at,
            'end_at': end_at,
            'body': body,
            'messages': messages,
            'size_target': size_target,
            'turn_target': turn_target,
            'size_progress': _percent(logical_size, size_target),
            'turn_progress': _percent(completed_turn_count, turn_target),
            'source_refs': [member.source_ref for member in unclaimed],
            'settings_revision_id': None,
            'chat_id': identity['chat_id'],
            'context_id': identity['context_id'],
            'context_epoch': identity['context_epoch'],
            'threshold_reached': threshold_reached,
            'processing_state': processing_state,
            'remaining_logical_size': max(0, size_target - logical_size),
            'remaining_completed_turns': max(0, turn_target - completed_turn_count),
            'materialization_error': materialization_error,
        }
    finally:
        conn.close()
