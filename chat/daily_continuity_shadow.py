"""Read-only adapter from durable daily rows to the pure R4 ContextPlan.

This module is deliberately outside the daily runtime.  It acquires durable
rows and already-materialized shadow chunks, then makes exactly one call to
the canonical continuity planner.  It never creates a database, writes a
source row, or assembles provider-facing text.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

from continuity.context_plan import (
    ContextBudgetPolicy,
    ContextChunkBinding,
    ContextPlan,
    ContextSection,
    build_context_plan,
)
from continuity.sources import (
    build_source_members,
    derive_autonomous_events,
    derive_completed_turns,
    evidence_ref,
    is_formal_user_source_row,
)
from continuity.store import (
    ContinuityStoreError,
    load_candidate,
    load_ready_chunks,
    load_snapshot,
)


ShadowStatus = Literal['ready', 'blocked', 'failed']
ChunkSurface = Literal['ready', 'empty', 'unavailable']

_SOURCE_COLUMNS = (
    'id, author, content, thinking, created_at, tool_calls, branches, branch_idx, '
    'cache_info, source_kind, attachments, image_url, file_url, file_name'
)

_SOURCE_TABLE = 'chat_messages'
_MESSAGE_CONTEXT_TABLE = 'daily_message_contexts'
_WAKE_TABLE = 'wake_log'
_STORE_TABLES = frozenset({
    'continuity_source_snapshots',
    'continuity_source_members',
    'continuity_candidate_blocks',
    'continuity_candidate_members',
    'continuity_chunks',
})


class _ChunkSurfaceUnavailable(Exception):
    pass


class _SourceScopeUnavailable(Exception):
    pass


@dataclass(frozen=True)
class DailyContinuityShadowResult:
    """Machine-readable result of one canonical shadow planning attempt."""

    status: ShadowStatus
    error_code: str | None
    plan: ContextPlan | None
    chunk_surface: ChunkSurface
    source_member_count: int = 0
    chunk_binding_count: int = 0


def _blocked(error_code: str, *, chunk_surface: ChunkSurface = 'unavailable') -> DailyContinuityShadowResult:
    return DailyContinuityShadowResult(
        status='blocked',
        error_code=error_code,
        plan=None,
        chunk_surface=chunk_surface,
    )


def _failed() -> DailyContinuityShadowResult:
    return DailyContinuityShadowResult(
        status='failed',
        error_code='unexpected_exception',
        plan=None,
        chunk_surface='unavailable',
    )


def _read_only_connection(path: str | Path | None) -> sqlite3.Connection:
    """Open an existing SQLite file without a writable SQLite URI."""
    if path is None:
        raise FileNotFoundError('database path is required')
    resolved = Path(path).expanduser().resolve()
    return sqlite3.connect(f'file:{resolved.as_posix()}?mode=ro', uri=True)


def read_canonical_scope_rows(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
    before_message_id: int | None = None,
) -> tuple[dict[str, object], ...]:
    """Read the frozen R4.5-A source universe for one canonical window.

    Formal ownership comes from the durable message-context table. Wake rows are
    admitted only through matching wake_log provenance; no date, message range,
    or resident generation selects the universe. before_message_id exists only
    for the current-request adapter's historical cutoff.
    """
    try:
        context_id = int(context_id)
        context_epoch = int(context_epoch)
    except (TypeError, ValueError) as exc:
        raise _SourceScopeUnavailable('source scope identity is malformed') from exc
    if context_id <= 0 or context_epoch <= 0:
        raise _SourceScopeUnavailable('source scope identity is invalid')

    row_columns = tuple(item.strip() for item in _SOURCE_COLUMNS.split(','))
    cutoff = ''
    params: list[object] = [context_id, context_epoch]
    if before_message_id is not None:
        cutoff = ' AND m.id < ?'
        params.append(int(before_message_id))
    formal_cursor = conn.execute(
        f'SELECT m.{", m.".join(row_columns)} FROM {_SOURCE_TABLE} m '
        f'INNER JOIN {_MESSAGE_CONTEXT_TABLE} dmc ON dmc.message_id=m.id '
        'WHERE dmc.context_id=? AND dmc.context_epoch=?' + cutoff +
        ' ORDER BY m.id ASC',
        tuple(params),
    )
    formal_rows = [dict(zip(row_columns, row)) for row in formal_cursor.fetchall()]

    wake_run_rows = conn.execute(
        f'SELECT wake_run_id FROM {_WAKE_TABLE} '
        'WHERE context_id=? AND context_epoch=? '
        'AND wake_run_id IS NOT NULL AND wake_run_id != ?',
        (context_id, context_epoch, ''),
    ).fetchall()
    scoped_wake_run_ids = {
        str(row[0]).strip() for row in wake_run_rows if str(row[0] or '').strip()
    }
    wake_rows: list[dict[str, object]] = []
    if scoped_wake_run_ids:
        wake_cutoff = ''
        wake_params: list[object] = []
        if before_message_id is not None:
            wake_cutoff = ' AND id < ?'
            wake_params.append(int(before_message_id))
        wake_cursor = conn.execute(
            f'SELECT {_SOURCE_COLUMNS} FROM {_SOURCE_TABLE} '
            "WHERE source_kind='wake'" + wake_cutoff + ' ORDER BY id ASC',
            tuple(wake_params),
        )
        for row in wake_cursor.fetchall():
            candidate = dict(zip(row_columns, row))
            try:
                cache_info = json.loads(str(candidate.get('cache_info') or ''))
            except (TypeError, ValueError):
                continue
            if (
                isinstance(cache_info, dict)
                and str(cache_info.get('wake_run_id') or '').strip()
                in scoped_wake_run_ids
            ):
                wake_rows.append(candidate)

    by_message_id = {
        int(row['id']): row for row in (*formal_rows, *wake_rows)
    }
    return tuple(by_message_id[mid] for mid in sorted(by_message_id))


def _source_rows(
    conn: sqlite3.Connection,
    current_user_message_id: int,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Read only the current request scope and its durable wake provenance."""
    current_id = int(current_user_message_id)
    mapping_rows = conn.execute(
        f'SELECT context_id, context_epoch FROM {_MESSAGE_CONTEXT_TABLE} '
        'WHERE message_id=?',
        (current_id,),
    ).fetchall()
    if len(mapping_rows) != 1:
        raise _SourceScopeUnavailable('current request scope mapping is unavailable')
    try:
        context_id = int(mapping_rows[0][0])
        context_epoch = int(mapping_rows[0][1])
    except (TypeError, ValueError) as exc:
        raise _SourceScopeUnavailable('current request scope mapping is malformed') from exc
    if context_id <= 0 or context_epoch <= 0:
        raise _SourceScopeUnavailable('current request scope identity is invalid')

    history = read_canonical_scope_rows(
        conn,
        context_id=context_id,
        context_epoch=context_epoch,
        before_message_id=current_id,
    )
    current_cursor = conn.execute(
        f'SELECT {_SOURCE_COLUMNS} FROM {_SOURCE_TABLE} WHERE id = ?',
        (current_id,),
    )
    current_columns = tuple(item[0] for item in current_cursor.description)
    current_rows = current_cursor.fetchall()
    if len(current_rows) != 1:
        raise LookupError('current request row is unavailable')
    return history, dict(zip(current_columns, current_rows[0]))


def _check_source_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (_SOURCE_TABLE,),
    ).fetchall()
    if not rows:
        raise sqlite3.OperationalError('chat_messages table is unavailable')
    columns = {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info({_SOURCE_TABLE})').fetchall()
    }
    required = frozenset(item.strip() for item in _SOURCE_COLUMNS.split(','))
    missing = required - columns
    if missing:
        raise sqlite3.OperationalError(
            'chat_messages columns unavailable: ' + ','.join(sorted(missing))
        )

    for table, required in (
        (
            _MESSAGE_CONTEXT_TABLE,
            frozenset({'message_id', 'context_id', 'context_epoch', 'resident_generation', 'role'}),
        ),
        (
            _WAKE_TABLE,
            frozenset({'wake_run_id', 'context_id', 'context_epoch', 'chat_id'}),
        ),
    ):
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchall()
        if not table_rows:
            raise _SourceScopeUnavailable(f'{table} table is unavailable')
        table_columns = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info({table})').fetchall()
        }
        missing = required - table_columns
        if missing:
            raise _SourceScopeUnavailable(
                f'{table} columns unavailable: ' + ','.join(sorted(missing))
            )


def _check_store_schema(conn: sqlite3.Connection) -> None:
    names = frozenset(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    )
    missing = _STORE_TABLES - names
    if missing:
        raise sqlite3.OperationalError(
            'continuity shadow tables unavailable: ' + ','.join(sorted(missing))
        )


def _current_request_section(row: dict[str, object]) -> ContextSection:
    evidence = evidence_ref(row, prefix='message')
    return ContextSection(
        kind='current_request',
        source_ref=evidence.source_ref,
        content_hash=evidence.content_hash,
        estimated_tokens=int(evidence.logical_size),
    )


def _bindings(
    conn: sqlite3.Connection,
    *,
    validated_ready_artifacts: Sequence[Mapping[str, str]] | None = None,
) -> tuple[ContextChunkBinding, ...]:
    chunks = load_ready_chunks(conn)
    if validated_ready_artifacts is not None:
        expected: dict[str, tuple[str, str]] = {}
        try:
            for item in validated_ready_artifacts:
                chunk_id = str(item.get('chunk_id') or '').strip()
                artifact_revision = str(item.get('artifact_revision') or '').strip()
                body_hash = str(item.get('body_hash') or '').strip()
                if not chunk_id or not artifact_revision or not body_hash:
                    raise ValueError('validated ready artifact identity is incomplete')
                if chunk_id in expected:
                    raise ValueError('validated ready artifact identity is duplicated')
                expected[chunk_id] = (artifact_revision, body_hash)
        except (AttributeError, TypeError, ValueError) as exc:
            raise _ChunkSurfaceUnavailable(str(exc)) from exc
        chunks_by_id = {str(chunk.chunk_id): chunk for chunk in chunks}
        selected = []
        for chunk_id in sorted(expected):
            chunk = chunks_by_id.get(chunk_id)
            if chunk is None:
                raise _ChunkSurfaceUnavailable(
                    'strict ready artifact disappeared during binding acquisition'
                )
            artifact_revision, body_hash = expected[chunk_id]
            if (
                str(chunk.artifact_revision) != artifact_revision
                or str(chunk.body_hash) != body_hash
            ):
                raise _ChunkSurfaceUnavailable(
                    'strict ready artifact identity changed during binding acquisition'
                )
            selected.append(chunk)
        chunks = tuple(selected)

    bindings: list[ContextChunkBinding] = []
    for chunk in chunks:
        candidate = load_candidate(conn, chunk.candidate_id)
        snapshot = load_snapshot(conn, chunk.snapshot_id)
        if candidate is None or snapshot is None:
            raise _ChunkSurfaceUnavailable('ready chunk provenance is unavailable')
        bindings.append(ContextChunkBinding(
            chunk=chunk,
            candidate=candidate,
            snapshot=snapshot,
        ))
    return tuple(bindings)


def build_daily_continuity_shadow_plan(
    *,
    source_db_path: str | Path | None,
    shadow_store_path: str | Path | None,
    current_user_message_id: int,
    budget_policy: ContextBudgetPolicy | None,
    accepted_fixed_sections: Sequence[ContextSection] = (),
    budget_policy_version: str = '',
    continuity_store_path: str | Path | None = None,
    validated_ready_artifacts: Sequence[Mapping[str, str]] | None = None,
) -> DailyContinuityShadowResult:
    """Build one canonical shadow plan from explicit read-only surfaces.

    The caller supplies accepted metadata-only fixed sections.  The adapter
    owns the durable current-request section and refuses a caller-supplied
    duplicate.  The pure R4 planner remains the only selection authority.
    """
    if budget_policy is None or not isinstance(budget_policy, ContextBudgetPolicy):
        return _blocked('budget_policy_unmapped')
    if isinstance(current_user_message_id, bool) or not isinstance(current_user_message_id, int):
        return _blocked('invalid_current_request')

    try:
        try:
            accepted = tuple(accepted_fixed_sections)
        except (TypeError, ValueError):
            return _blocked('invalid_fixed_sections')
        if any(not isinstance(section, ContextSection) for section in accepted):
            return _blocked('invalid_fixed_sections')
        if any(getattr(section, 'kind', None) == 'current_request' for section in accepted):
            return _blocked('invalid_current_request')

        try:
            source_conn = _read_only_connection(source_db_path)
        except (FileNotFoundError, OSError, sqlite3.Error):
            return _blocked('source_rows_unavailable')
        try:
            _check_source_schema(source_conn)
            history_rows, current_row = _source_rows(source_conn, current_user_message_id)
        except LookupError:
            return _blocked('invalid_current_request')
        except _SourceScopeUnavailable:
            return _blocked('source_scope_unavailable')
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return _blocked('source_rows_unavailable')
        finally:
            source_conn.close()

        try:
            turns = derive_completed_turns(history_rows)
            events = derive_autonomous_events(history_rows)
            expected_members = build_source_members(turns, events)
        except (TypeError, ValueError):
            return _blocked('source_rows_unavailable')
        if not is_formal_user_source_row(current_row):
            return _blocked('invalid_current_request')
        try:
            current_request = _current_request_section(current_row)
        except (TypeError, ValueError):
            return _blocked('invalid_current_request')

        try:
            store_path = (
                continuity_store_path
                if continuity_store_path is not None
                else shadow_store_path
            )
            store_conn = _read_only_connection(store_path)
        except (FileNotFoundError, OSError, sqlite3.Error):
            return _blocked('chunk_surface_unavailable')
        try:
            _check_store_schema(store_conn)
            bindings = _bindings(
                store_conn,
                validated_ready_artifacts=validated_ready_artifacts,
            )
        except (OSError, sqlite3.Error, TypeError, ValueError, ContinuityStoreError, _ChunkSurfaceUnavailable):
            return _blocked('chunk_surface_unavailable')
        finally:
            store_conn.close()

        try:
            plan = build_context_plan(
                expected_members,
                raw_members=expected_members,
                chunks=bindings,
                budget_policy=budget_policy,
                fixed_sections=accepted + (current_request,),
                budget_policy_version=str(budget_policy_version or '').strip(),
            )
        except ValueError:
            return _blocked('invalid_fixed_sections')
        return DailyContinuityShadowResult(
            status='ready',
            error_code=None,
            plan=plan,
            chunk_surface='ready' if bindings else 'empty',
            source_member_count=len(expected_members),
            chunk_binding_count=len(bindings),
        )
    except Exception:
        return _failed()


__all__ = [
    'DailyContinuityShadowResult',
    'build_daily_continuity_shadow_plan',
    'read_canonical_scope_rows',
]

