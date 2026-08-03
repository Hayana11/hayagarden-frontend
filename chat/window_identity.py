"""Cross-window ownership helpers for async job / Wake delivery (step 7).

Capture canonical window identity, normalize, and re-validate before side effects.
Does not own switch/Forge semantics — only reads the existing canonical resolver.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

IDENTITY_KEYS = ('chat_id', 'context_id', 'context_epoch', 'resident_generation')

REASON_OK = 'ok'
REASON_STALE = 'stale_window_result'
REASON_UNAVAILABLE = 'window_identity_unavailable'


class WindowIdentityError(Exception):
    """Base error for window identity capture / validation."""


class WindowIdentityUnavailable(WindowIdentityError):
    """Flag-on path could not obtain a complete canonical window identity."""


def soft_window_enabled() -> bool:
    from chat.daily_context import enabled
    return bool(enabled())


def normalize_window_identity(raw: Any) -> Optional[dict[str, Any]]:
    """Return a strict identity dict, or None when missing/malformed.

    Never invents or backfills from current window.
    """
    if not isinstance(raw, dict):
        return None
    chat_id = raw.get('chat_id')
    if not isinstance(chat_id, str) or not chat_id.strip():
        return None
    try:
        context_id = raw.get('context_id')
        context_epoch = raw.get('context_epoch')
        resident_generation = raw.get('resident_generation')
        if isinstance(context_id, bool) or isinstance(context_epoch, bool) or isinstance(
            resident_generation, bool,
        ):
            return None
        cid = int(context_id)
        epoch = int(context_epoch)
        gen = int(resident_generation)
    except (TypeError, ValueError):
        return None
    if cid <= 0 or epoch <= 0 or gen <= 0:
        return None
    return {
        'chat_id': chat_id.strip(),
        'context_id': cid,
        'context_epoch': epoch,
        'resident_generation': gen,
    }


def identity_from_context_row(row: dict[str, Any], *, chat_id: str = 'default') -> dict[str, Any]:
    """Build identity from a daily_contexts / summary row."""
    raw_chat = row.get('chat_id')
    resolved_chat = str(raw_chat).strip() if raw_chat not in (None, '') else str(chat_id)
    # get_current_context_window summary uses context_id; raw row uses id.
    if 'context_id' in row and row.get('context_id') is not None:
        cid = int(row['context_id'])
    else:
        cid = int(row['id'])
    return {
        'chat_id': resolved_chat or 'default',
        'context_id': cid,
        'context_epoch': int(row['context_epoch']),
        'resident_generation': int(row.get('resident_generation') or 1),
    }


def identities_match(a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> bool:
    na = normalize_window_identity(a)
    nb = normalize_window_identity(b)
    if na is None or nb is None:
        return False
    return all(na[k] == nb[k] for k in IDENTITY_KEYS)


def capture_current_window_identity(
    *,
    chat_id: str = 'default',
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Freeze the current canonical window identity (flag-on start path)."""
    from chat.context_window import NoOpenContextWindowError, get_current_context_window

    try:
        row = get_current_context_window(chat_id=chat_id, db_path=db_path)
    except NoOpenContextWindowError as exc:
        raise WindowIdentityUnavailable('no_open_context_window') from exc
    except Exception as exc:
        raise WindowIdentityUnavailable(str(exc) or 'capture_failed') from exc
    identity = identity_from_context_row(row, chat_id=chat_id)
    normalized = normalize_window_identity(identity)
    if normalized is None:
        raise WindowIdentityUnavailable('malformed_canonical_identity')
    return normalized


def read_current_window_identity_conn(
    conn: Any,
    *,
    chat_id: str = 'default',
) -> dict[str, Any]:
    """Resolve identity on an already-open connection (for atomic check+write)."""
    import sqlite3

    from chat.context_window import NoOpenContextWindowError, resolve_canonical_context_row_conn

    try:
        row = resolve_canonical_context_row_conn(conn, chat_id=chat_id)
    except NoOpenContextWindowError as exc:
        raise WindowIdentityUnavailable('no_open_context_window') from exc
    except sqlite3.OperationalError as exc:
        # Missing soft-window tables on a bare/fixture DB must stay fail-closed
        # for wake claim, not crash the continuity path.
        raise WindowIdentityUnavailable('window_schema_unavailable') from exc
    identity = identity_from_context_row(dict(row), chat_id=chat_id)
    normalized = normalize_window_identity(identity)
    if normalized is None:
        raise WindowIdentityUnavailable('malformed_canonical_identity')
    return normalized


def gate_captured_against_conn(
    conn: Any,
    captured: Any,
    *,
    chat_id: str = 'default',
) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Validate captured identity against canonical current inside caller's txn.

    Returns (reason, captured_norm, current_norm).
    Flag-off callers should not use this for delivery blocking.
    """
    captured_norm = normalize_window_identity(captured)
    if captured_norm is None:
        return REASON_UNAVAILABLE, None, None
    try:
        current = read_current_window_identity_conn(conn, chat_id=chat_id)
    except WindowIdentityUnavailable:
        return REASON_STALE, captured_norm, None
    if not identities_match(captured_norm, current):
        return REASON_STALE, captured_norm, current
    return REASON_OK, captured_norm, current


def evaluate_async_delivery(
    captured: Any,
    *,
    chat_id: str = 'default',
    db_path: Optional[str] = None,
) -> tuple[str, Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Gate for workspace-job persistence / pending / SSE.

    Flag off → always ok (legacy identity optional).
    Flag on → require complete identity matching current canonical window.
    """
    if not soft_window_enabled():
        return REASON_OK, normalize_window_identity(captured), None

    captured_norm = normalize_window_identity(captured)
    if captured_norm is None:
        return REASON_UNAVAILABLE, None, None

    from chat.context_window import get_current_context_window, NoOpenContextWindowError
    from chat.daily_context import ensure_schema

    ensure_schema(db_path)
    try:
        row = get_current_context_window(chat_id=chat_id, db_path=db_path)
        current = identity_from_context_row(row, chat_id=chat_id)
    except (NoOpenContextWindowError, WindowIdentityUnavailable, Exception):
        return REASON_STALE, captured_norm, None
    current_norm = normalize_window_identity(current)
    if current_norm is None or not identities_match(captured_norm, current_norm):
        return REASON_STALE, captured_norm, current_norm
    return REASON_OK, captured_norm, current_norm


def persist_workspace_job_chat_if_current(
    event: dict[str, Any],
    *,
    db_path: str,
    chat_id: str = 'default',
    after_gate_ok: Any = None,
    after_insert: Any = None,
    after_commit: Any = None,
) -> dict[str, Any]:
    """Atomically gate + INSERT workspace-job chat row (flag-on path).

    Contract::

        open connection → BEGIN IMMEDIATE → same-conn gate → INSERT → COMMIT

    Returns ``{reason, captured_identity, current_identity, persisted}``.
    Does not queue SSE/pending events — caller queues only after persisted=True.
    """
    import json as _json
    import sqlite3

    from chat.daily_context import ensure_schema

    ensure_schema(db_path)
    result: dict[str, Any] = {
        'reason': REASON_UNAVAILABLE,
        'captured_identity': None,
        'current_identity': None,
        'persisted': False,
    }
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        reason, captured, current = gate_captured_against_conn(
            conn, event.get('window_identity'), chat_id=chat_id,
        )
        result['reason'] = reason
        result['captured_identity'] = captured
        result['current_identity'] = current
        if reason != REASON_OK:
            conn.rollback()
            return result

        if after_gate_ok is not None:
            after_gate_ok(conn)

        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info(chat_messages)')}
        if 'source_kind' not in cols:
            conn.rollback()
            result['reason'] = REASON_UNAVAILABLE
            logger.warning(
                'reason=window_identity_unavailable source=workspace_job '
                'detail=chat_messages.source_kind missing'
            )
            return result

        meta = event.get('meta') or {}
        tc = [{
            'name': 'ws_job',
            'args': {'action': 'status', 'id': meta.get('job_id')},
            'result': _json.dumps({
                'ok': True,
                'job': meta,
                'log_tail': event.get('log_tail', ''),
            }, ensure_ascii=False),
            'success': meta.get('status') == 'succeeded',
            'job': meta,
        }]
        conn.execute(
            "INSERT INTO chat_messages (author, content, tool_calls, source_kind) "
            "VALUES ('assistant', ?, ?, 'workspace_job')",
            (event.get('content', ''), _json.dumps(tc, ensure_ascii=False)),
        )
        if after_insert is not None:
            after_insert(conn)
        conn.commit()
        result['persisted'] = True
        if after_commit is not None:
            after_commit()
        return result
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception(
            'reason=window_identity_unavailable source=workspace_job '
            'detail=atomic persist failed'
        )
        result['reason'] = REASON_UNAVAILABLE
        result['persisted'] = False
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def log_stale_async_result(
    *,
    source: str,
    reason: str,
    captured_identity: Any = None,
    current_identity: Any = None,
    job_id: Any = None,
    wake_run_id: Any = None,
) -> None:
    parts = [
        f'reason={reason}',
        f'source={source}',
    ]
    if job_id is not None:
        parts.append(f'job_id={job_id}')
    if wake_run_id is not None:
        parts.append(f'wake_run_id={wake_run_id}')
    parts.append(f'captured_identity={captured_identity!r}')
    parts.append(f'current_identity={current_identity!r}')
    logger.warning(' '.join(parts))


def ensure_wake_window_identity_columns(conn: Any) -> None:
    """Best-effort ADD COLUMN for wake_log window identity fields."""
    cols = {str(r[1]) for r in conn.execute('PRAGMA table_info(wake_log)')}
    alters = [
        ('chat_id', "ALTER TABLE wake_log ADD COLUMN chat_id TEXT"),
        ('context_id', "ALTER TABLE wake_log ADD COLUMN context_id INTEGER"),
        ('context_epoch', "ALTER TABLE wake_log ADD COLUMN context_epoch INTEGER"),
        ('resident_generation', "ALTER TABLE wake_log ADD COLUMN resident_generation INTEGER"),
    ]
    for name, sql in alters:
        if name in cols:
            continue
        try:
            conn.execute(sql)
        except Exception:
            pass


def wake_rows_match_identity(row: Any, identity: dict[str, Any]) -> bool:
    """True when a wake_log row carries the given complete identity."""
    if not isinstance(identity, dict):
        return False
    try:
        if hasattr(row, 'keys'):
            chat_id = row['chat_id'] if 'chat_id' in row.keys() else None
            context_id = row['context_id'] if 'context_id' in row.keys() else None
            context_epoch = row['context_epoch'] if 'context_epoch' in row.keys() else None
            resident_generation = (
                row['resident_generation'] if 'resident_generation' in row.keys() else None
            )
        else:
            return False
        row_identity = normalize_window_identity({
            'chat_id': chat_id,
            'context_id': context_id,
            'context_epoch': context_epoch,
            'resident_generation': resident_generation,
        })
    except Exception:
        return False
    return identities_match(row_identity, identity)


def fetch_claimable_wake_ids(get_db_fn) -> list[int]:
    """IDs eligible for turn claim / consume. Flag-off = legacy all consumed=0."""
    conn = get_db_fn()
    try:
        ensure_wake_window_identity_columns(conn)
        if not soft_window_enabled():
            rows = conn.execute(
                'SELECT id FROM wake_log WHERE consumed=0 ORDER BY id ASC',
            ).fetchall()
            return [int(row['id'] if hasattr(row, 'keys') else row[0]) for row in rows]

        try:
            current = read_current_window_identity_conn(conn)
        except WindowIdentityUnavailable:
            return []
        rows = conn.execute(
            'SELECT id, chat_id, context_id, context_epoch, resident_generation '
            'FROM wake_log WHERE consumed=0 ORDER BY id ASC',
        ).fetchall()
        out: list[int] = []
        for row in rows:
            if wake_rows_match_identity(row, current):
                out.append(int(row['id'] if hasattr(row, 'keys') else row[0]))
        return out
    finally:
        conn.close()


def fetch_unconsumed_wakes_for_injection(get_db_fn, columns: str) -> list[Any]:
    """Wake rows for system_builder injection. Flag-on filters to current identity."""
    conn = get_db_fn()
    try:
        ensure_wake_window_identity_columns(conn)
        if not soft_window_enabled():
            return list(conn.execute(
                f'SELECT {columns} FROM wake_log WHERE consumed=0 ORDER BY id ASC',
            ).fetchall())

        try:
            current = read_current_window_identity_conn(conn)
        except WindowIdentityUnavailable:
            return []
        # Always select identity columns for filtering, even if caller omitted them.
        select_cols = columns
        needed = ('chat_id', 'context_id', 'context_epoch', 'resident_generation')
        lower = columns.lower()
        extras = [c for c in needed if c not in lower]
        if extras:
            select_cols = columns + ', ' + ', '.join(extras)
        rows = conn.execute(
            f'SELECT {select_cols} FROM wake_log WHERE consumed=0 ORDER BY id ASC',
        ).fetchall()
        return [row for row in rows if wake_rows_match_identity(row, current)]
    finally:
        conn.close()


def fetch_pending_wake_notification_row(conn: Any) -> Any:
    """Latest un-notified message wake on an open connection; gated when flag on.

    Caller owns commit for any subsequent notified=1 updates on the same conn.
    """
    ensure_wake_window_identity_columns(conn)
    if not soft_window_enabled():
        return conn.execute(
            "SELECT id, content, woke_at FROM wake_log "
            "WHERE action='message' AND (notified IS NULL OR notified=0) "
            "ORDER BY id DESC LIMIT 1",
        ).fetchone()

    try:
        current = read_current_window_identity_conn(conn)
    except WindowIdentityUnavailable:
        return None
    rows = conn.execute(
        "SELECT id, content, woke_at, chat_id, context_id, context_epoch, "
        "resident_generation FROM wake_log "
        "WHERE action='message' AND (notified IS NULL OR notified=0) "
        "ORDER BY id DESC",
    ).fetchall()
    for row in rows:
        if wake_rows_match_identity(row, current):
            return row
    return None
