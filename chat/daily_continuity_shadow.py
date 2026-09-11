"""Read-only adapter from durable daily rows to the pure R4 ContextPlan.

This module is deliberately outside the daily runtime.  It acquires durable
rows and already-materialized shadow chunks, then makes exactly one call to
the canonical continuity planner.  It never creates a database, writes a
source row, or assembles provider-facing text.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

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
)
from continuity.store import (
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
_STORE_TABLES = frozenset({
    'continuity_source_snapshots',
    'continuity_source_members',
    'continuity_candidate_blocks',
    'continuity_candidate_members',
    'continuity_chunks',
})


class _ChunkSurfaceUnavailable(Exception):
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


def _source_rows(conn: sqlite3.Connection, current_user_message_id: int) -> tuple[dict[str, object], dict[str, object]]:
    """Read history before the current request and the request row separately."""
    history_cursor = conn.execute(
        f'SELECT {_SOURCE_COLUMNS} FROM {_SOURCE_TABLE} '
        'WHERE id < ? ORDER BY id ASC',
        (int(current_user_message_id),),
    )
    history_columns = tuple(item[0] for item in history_cursor.description)
    history = tuple(dict(zip(history_columns, row)) for row in history_cursor.fetchall())

    current_cursor = conn.execute(
        f'SELECT {_SOURCE_COLUMNS} FROM {_SOURCE_TABLE} WHERE id = ?',
        (int(current_user_message_id),),
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


def _bindings(conn: sqlite3.Connection) -> tuple[ContextChunkBinding, ...]:
    bindings: list[ContextChunkBinding] = []
    for chunk in load_ready_chunks(conn):
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
        try:
            current_request = _current_request_section(current_row)
        except (TypeError, ValueError):
            return _blocked('invalid_current_request')

        try:
            store_conn = _read_only_connection(shadow_store_path)
        except (FileNotFoundError, OSError, sqlite3.Error):
            return _blocked('chunk_surface_unavailable')
        try:
            _check_store_schema(store_conn)
            bindings = _bindings(store_conn)
        except (OSError, sqlite3.Error, TypeError, ValueError, _ChunkSurfaceUnavailable):
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


def build_daily_continuity_shadow(**kwargs: object) -> DailyContinuityShadowResult:
    """Compatibility spelling for the explicit shadow-plan adapter."""
    return build_daily_continuity_shadow_plan(**kwargs)  # type: ignore[arg-type]


__all__ = [
    'DailyContinuityShadowResult',
    'build_daily_continuity_shadow',
    'build_daily_continuity_shadow_plan',
]

