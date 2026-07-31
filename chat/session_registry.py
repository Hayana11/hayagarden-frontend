"""Session Registry — authoritative Claude session binding for a window identity.

Flag-off data layer (P-CONTEXT-WINDOW v0.2). No Runtime wiring, no mtime /
directory scanning, no --continue. transcript_path is derived from an explicit
session_id (+ cwd) and frozen at registration time.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from chat import daily_context as dc
from tools.cc_jsonl_usage import session_jsonl_path

SCAN_STATUS_READY = 'READY'
SCAN_STATUS_BLOCKED = 'BLOCKED'

_REGISTRY_IDENTITY_FIELDS = (
    'context_epoch',
    'chat_id',
    'claude_session_id',
    'transcript_path',
    'source',
)


class SessionRegistryError(Exception):
    """Base error for Session Registry."""

    def __init__(self, message: str, *, error_code: str = 'registry_error'):
        super().__init__(message)
        self.error_code = error_code


class SessionRegistryConflict(SessionRegistryError):
    """Idempotency / identity conflict (fail closed)."""

    def __init__(self, message: str, *, error_code: str = 'registry_conflict'):
        super().__init__(message, error_code=error_code)


class SessionRegistryNotFound(SessionRegistryError):
    def __init__(self, message: str = 'session registry row missing'):
        super().__init__(message, error_code='registry_missing')


def derive_transcript_path(
    *,
    cwd: str,
    claude_session_id: str,
    claude_home: Optional[str] = None,
) -> str:
    """Derive JSONL path from explicit session_id. Never scans directories."""
    sid = str(claude_session_id or '').strip()
    if not sid:
        raise SessionRegistryError('claude_session_id required', error_code='session_id_required')
    work = str(cwd or '').strip()
    if not work:
        raise SessionRegistryError('cwd required for transcript_path', error_code='cwd_required')
    path = session_jsonl_path(work, sid, claude_home=claude_home)
    if path is None:
        raise SessionRegistryError('transcript_path derivation failed', error_code='path_derive_failed')
    return str(path)


def _row_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def get_context_claude_session(
    context_id: int,
    resident_generation: int,
    *,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    own = conn is None
    c = conn or dc._connect(db_path)
    try:
        row = c.execute(
            'SELECT * FROM context_claude_sessions '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        return _row_dict(row)
    finally:
        if own:
            c.close()


def get_context_claude_session_by_sid(
    claude_session_id: str,
    *,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[dict[str, Any]]:
    sid = str(claude_session_id or '').strip()
    if not sid:
        return None
    own = conn is None
    c = conn or dc._connect(db_path)
    try:
        row = c.execute(
            'SELECT * FROM context_claude_sessions WHERE claude_session_id=?',
            (sid,),
        ).fetchone()
        return _row_dict(row)
    finally:
        if own:
            c.close()


def _identity_mismatch(existing: dict[str, Any], expected: dict[str, Any]) -> Optional[str]:
    for key in _REGISTRY_IDENTITY_FIELDS:
        left = existing.get(key)
        right = expected.get(key)
        if key in ('context_epoch',):
            if int(left) != int(right):
                return key
        elif key == 'chat_id':
            if str(left) != str(right):
                return key
        else:
            if str(left) != str(right):
                return key
    # process_generation: NULL-tolerant; conflict only when both set and differ
    left_pg = existing.get('process_generation')
    right_pg = expected.get('process_generation')
    if left_pg is not None and right_pg is not None and int(left_pg) != int(right_pg):
        return 'process_generation'
    return None


def register_context_claude_session(
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    chat_id: str,
    claude_session_id: str,
    cwd: str,
    source: str,
    scan_offset: int,
    process_generation: Optional[int] = None,
    claude_home: Optional[str] = None,
    transcript_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Register or idempotently confirm a Session Registry row.

    ``scan_offset`` must be explicit (caller-supplied). ``transcript_path`` is
    derived from ``cwd`` + ``claude_session_id``; if provided it must match.
    """
    dc.ensure_schema(db_path)
    cid = int(context_id)
    epoch = int(context_epoch)
    gen = int(resident_generation)
    if cid <= 0 or epoch <= 0 or gen <= 0:
        raise SessionRegistryError('invalid window identity', error_code='invalid_identity')
    chat = str(chat_id or '').strip()
    if not chat:
        raise SessionRegistryError('chat_id required', error_code='chat_id_required')
    sid = str(claude_session_id or '').strip()
    src = str(source or '').strip()
    if not src:
        raise SessionRegistryError('source required', error_code='source_required')
    try:
        offset = int(scan_offset)
    except (TypeError, ValueError) as exc:
        raise SessionRegistryError(
            'scan_offset must be explicit int', error_code='scan_offset_required',
        ) from exc
    if offset < 0:
        raise SessionRegistryError('scan_offset must be >= 0', error_code='scan_offset_invalid')

    derived = derive_transcript_path(
        cwd=cwd, claude_session_id=sid, claude_home=claude_home,
    )
    if transcript_path is not None and str(transcript_path) != derived:
        raise SessionRegistryConflict(
            'transcript_path does not match session_id derivation',
            error_code='transcript_path_mismatch',
        )
    path = derived
    pg = None if process_generation is None else int(process_generation)
    now_s = dc._now_local_str()
    expected = {
        'context_epoch': epoch,
        'chat_id': chat,
        'claude_session_id': sid,
        'transcript_path': path,
        'source': src,
        'process_generation': pg,
    }

    conn = dc._connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        by_key = get_context_claude_session(cid, gen, conn=conn)
        by_sid = get_context_claude_session_by_sid(sid, conn=conn)

        if by_sid is not None and (
            int(by_sid['context_id']) != cid
            or int(by_sid['resident_generation']) != gen
        ):
            conn.rollback()
            raise SessionRegistryConflict(
                'claude_session_id already bound to another window identity',
                error_code='session_bound_elsewhere',
            )

        if by_key is not None:
            mismatch = _identity_mismatch(by_key, expected)
            if mismatch is not None:
                conn.rollback()
                raise SessionRegistryConflict(
                    f'registry identity conflict on {mismatch}',
                    error_code='registry_identity_conflict',
                )
            # Idempotent success — do not rewrite scan cursor / status.
            conn.commit()
            return dict(by_key)

        conn.execute(
            '''INSERT INTO context_claude_sessions (
                context_id, context_epoch, resident_generation, chat_id,
                claude_session_id, transcript_path, source, process_generation,
                scan_offset, scan_status, scan_error_code, last_mapped_message_id,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)''',
            (
                cid, epoch, gen, chat, sid, path, src, pg,
                offset, SCAN_STATUS_READY, now_s, now_s,
            ),
        )
        conn.commit()
        row = get_context_claude_session(cid, gen, conn=conn)
        assert row is not None
        return row
    except SessionRegistryError:
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise SessionRegistryConflict(
            f'registry integrity: {exc}', error_code='registry_integrity',
        ) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_scan_blocked(
    *,
    context_id: int,
    resident_generation: int,
    error_code: str,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """Persist BLOCKED status without advancing scan_offset."""
    own = conn is None
    c = conn or dc._connect(db_path)
    now_s = dc._now_local_str()
    try:
        if own:
            c.execute('BEGIN IMMEDIATE')
        cur = c.execute(
            '''UPDATE context_claude_sessions
               SET scan_status=?, scan_error_code=?, updated_at=?
               WHERE context_id=? AND resident_generation=?''',
            (
                SCAN_STATUS_BLOCKED, str(error_code), now_s,
                int(context_id), int(resident_generation),
            ),
        )
        if int(cur.rowcount or 0) != 1:
            if own:
                c.rollback()
            raise SessionRegistryNotFound()
        if own:
            c.commit()
        row = get_context_claude_session(
            int(context_id), int(resident_generation), conn=c,
        )
        assert row is not None
        return row
    except Exception:
        if own:
            c.rollback()
        raise
    finally:
        if own:
            c.close()


def cas_advance_scan_offset(
    *,
    context_id: int,
    resident_generation: int,
    expected_offset: int,
    new_offset: int,
    last_mapped_message_id: Optional[int] = None,
    scan_status: str = SCAN_STATUS_READY,
    scan_error_code: Optional[str] = None,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """CAS-advance scan_offset. Raises SessionRegistryConflict on mismatch."""
    own = conn is None
    c = conn or dc._connect(db_path)
    now_s = dc._now_local_str()
    try:
        exp = int(expected_offset)
        new = int(new_offset)
        if new < exp:
            raise SessionRegistryError(
                'new_offset < expected_offset', error_code='scan_offset_invalid',
            )
        if own:
            c.execute('BEGIN IMMEDIATE')
        row = get_context_claude_session(
            int(context_id), int(resident_generation), conn=c,
        )
        if row is None:
            if own:
                c.rollback()
            raise SessionRegistryNotFound()
        if int(row['scan_offset']) != exp:
            if own:
                c.rollback()
            raise SessionRegistryConflict(
                'scan_offset CAS mismatch',
                error_code='scan_offset_cas_conflict',
            )
        c.execute(
            '''UPDATE context_claude_sessions SET
                scan_offset=?,
                scan_status=?,
                scan_error_code=?,
                last_mapped_message_id=COALESCE(?, last_mapped_message_id),
                updated_at=?
               WHERE context_id=? AND resident_generation=? AND scan_offset=?''',
            (
                new, str(scan_status), scan_error_code,
                None if last_mapped_message_id is None else int(last_mapped_message_id),
                now_s,
                int(context_id), int(resident_generation), exp,
            ),
        )
        # Verify CAS wrote exactly one row
        check = get_context_claude_session(
            int(context_id), int(resident_generation), conn=c,
        )
        if check is None or int(check['scan_offset']) != new:
            if own:
                c.rollback()
            raise SessionRegistryConflict(
                'scan_offset CAS write failed',
                error_code='scan_offset_cas_conflict',
            )
        if own:
            c.commit()
        return dict(check)
    except SessionRegistryError:
        raise
    except Exception:
        if own:
            c.rollback()
        raise
    finally:
        if own:
            c.close()
