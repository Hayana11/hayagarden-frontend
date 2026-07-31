"""Context Window v0.2 — First-turn commit freeze.

Single DB handoff point on first non-empty text delta; message belongs to target
from the start. Does not wire Gateway or production callers.
"""
from __future__ import annotations

import logging
import sqlite3
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from chat.context_window import (
    CLOSE_REASON_MANUAL,
    INTENT_COMMITTING,
    INTENT_COMMITTED,
    INTENT_HANDOFF_PENDING,
    INTENT_READY,
    WINDOW_MODE_MANUAL,
    _collect_context_formal_messages,
    _intent_row,
    _last_formal_message_id,
    _serialize_context_switch,
    _shanghai_now,
    _switch_result_from_target_conn,
    _update_intent_conn,
    defer_old_resident_close,
    flush_old_resident_close,
    mark_intent_committed,
    resolve_canonical_context_row_conn,
)
from chat.daily_context import (
    DEFAULT_CHAT_ID,
    STATUS_PROVISIONAL,
    WINDOW_MODE_MANUAL_STAGED,
    ConflictError,
    _connect,
    _row_to_dict,
    advance_resident_history_cursor,
    ensure_schema,
    get_resident_history_cursor,
    release_resident_turn_lease,
)
from chat.daily_runtime import (
    bind_target_resident_after_switch,
    forged_history_watermark,
    handoff_lock,
    install_target_resident_after_swap,
)
from chat.session_registry import SCAN_STATUS_READY, get_context_claude_session
from tools.claude_forge_core import session_jsonl_path_for_cwd

logger = logging.getLogger(__name__)

# Narrow test hooks (default None).
_after_db_commit_hook: Optional[Callable[[], None]] = None
_before_handoff_hook: Optional[Callable[[], None]] = None


class FirstTurnError(Exception):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True)
class FirstTurnHooks:
    """Process-side first-turn hooks (staged resume + formal holder swap)."""

    prepare_staged: Callable[[dict[str, Any], Path], Any]
    discard_staged: Callable[[Any], None]
    formal_holder: Any
    forge_cwd: str
    claude_home: Path


@dataclass
class FirstTurnSession:
    switch_request_id: str
    first_turn_request_id: str
    user_message_id: int
    target_context_id: int
    target_context_epoch: int
    target_resident_generation: int
    staged: Any
    jsonl_path: Path
    start_offset: int
    stdin_sent: bool = False
    _buffered_first_text: Optional[str] = None
    _db_committed: bool = False
    _handoff_complete: bool = False
    _first_delta_released: bool = False


@dataclass(frozen=True)
class FirstTurnDeltaResult:
    released_text: tuple[str, ...]
    db_committed: bool
    handoff_complete: bool


@dataclass(frozen=True)
class FirstTurnRecoverResult:
    switch_result: dict[str, Any]
    released_text: tuple[str, ...]


@dataclass(frozen=True)
class FirstTurnCompleteResult:
    assistant_message_id: int
    cursor_advanced: bool


def _now_s(now: Optional[Any] = None) -> str:
    return _shanghai_now(now).strftime('%Y-%m-%d %H:%M:%S')


def _require_hooks(hooks: Optional[FirstTurnHooks]) -> FirstTurnHooks:
    if hooks is None:
        raise FirstTurnError('first turn hooks required', error_code='FIRST_TURN_HOOKS_REQUIRED')
    return hooks


def _jsonl_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return -1


def _load_target_and_intent(
    conn: sqlite3.Connection,
    switch_request_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    intent = _intent_row(conn, switch_request_id)
    if intent is None:
        raise FirstTurnError('intent missing', error_code='FIRST_TURN_INTENT_MISSING')
    target_id = intent.get('target_context_id')
    if target_id is None:
        raise FirstTurnError('target not prepared', error_code='FIRST_TURN_NOT_READY')
    target = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (int(target_id),),
    ).fetchone())
    if target is None:
        raise FirstTurnError('target missing', error_code='FIRST_TURN_TARGET_MISSING')
    return intent, target


def _verify_message_claim(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    target_id: int,
    target_epoch: int,
    target_gen: int,
) -> None:
    row = conn.execute(
        'SELECT context_id, context_epoch, resident_generation FROM daily_message_contexts '
        'WHERE message_id=?',
        (int(message_id),),
    ).fetchone()
    if row is None:
        return
    ex = dict(row)
    if (
        int(ex['context_id']) == int(target_id)
        and int(ex['context_epoch']) == int(target_epoch)
        and int(ex['resident_generation']) == int(target_gen)
    ):
        return
    raise FirstTurnError(
        'message mapped to another context',
        error_code='FIRST_TURN_MESSAGE_CONFLICT',
    )


def _insert_user_message_conn(
    conn: sqlite3.Connection,
    *,
    content: str,
    now_s: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO chat_messages (author, content, created_at, source_kind) "
        "VALUES ('hayana', ?, ?, 'chat')",
        (str(content), now_s),
    )
    return int(cur.lastrowid)


def _claim_staged_turn_lease_conn(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    resident_generation: int,
    lease_owner: str,
    request_message_id: int,
    now_s: str,
    exp_s: str,
) -> None:
    """Lease-only claim on manual_staged target (no owner/cursor before handoff)."""
    for lr in conn.execute(
        'SELECT lease_owner, request_message_id, expires_at FROM daily_resident_turn_leases '
        'WHERE context_id=? AND resident_generation=?',
        (int(context_id), int(resident_generation)),
    ).fetchall():
        lr_d = dict(lr)
        if (
            str(lr_d['lease_owner']) == lease_owner
            and int(lr_d['request_message_id']) == int(request_message_id)
        ):
            conn.execute(
                '''UPDATE daily_resident_turn_leases SET acquired_at=?, expires_at=?, updated_at=?
                   WHERE context_id=? AND resident_generation=?''',
                (now_s, exp_s, now_s, int(context_id), int(resident_generation)),
            )
            return
        conn.rollback()
        raise FirstTurnError('lease held by another', error_code='FIRST_TURN_LEASE_CONFLICT')

    conn.execute(
        '''INSERT INTO daily_resident_turn_leases (
            context_id, resident_generation, lease_owner, request_message_id,
            acquired_at, expires_at, updated_at
        ) VALUES (?,?,?,?,?,?,?)''',
        (
            int(context_id), int(resident_generation), lease_owner,
            int(request_message_id), now_s, exp_s, now_s,
        ),
    )


def _map_user_message_conn(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    now_s: str,
) -> None:
    conn.execute(
        'INSERT INTO daily_message_contexts '
        '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (
            int(message_id), int(context_id), int(context_epoch),
            int(resident_generation), 'user', now_s,
        ),
    )


def _commit_first_turn_db_conn(
    conn: sqlite3.Connection,
    *,
    intent: dict[str, Any],
    target: dict[str, Any],
    first_turn_request_id: str,
    user_message_id: int,
    forge_cwd: str,
    claude_home: str,
    now_s: str,
    now_dt: Any,
) -> dict[str, Any]:
    req_id = str(intent['request_id'])
    live = _intent_row(conn, req_id)
    if live is None:
        raise FirstTurnError('intent missing', error_code='FIRST_TURN_INTENT_MISSING')
    if str(live.get('status') or '') != INTENT_COMMITTING:
        raise FirstTurnError('intent not committing', error_code='FIRST_TURN_INTENT_STATUS')
    for key, expected in (
        ('request_id', req_id),
        ('first_turn_request_id', first_turn_request_id),
        ('first_user_message_id', int(user_message_id)),
        ('target_context_id', int(target['id'])),
        ('target_session_id', str(intent.get('target_session_id') or '')),
    ):
        if str(live.get(key) or '') != str(expected):
            raise FirstTurnError('intent CAS mismatch', error_code='FIRST_TURN_INTENT_CAS')

    source_id = int(intent['source_context_id'])
    source = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
    ).fetchone())
    if source is None or source.get('closed_at'):
        raise FirstTurnError('source not open', error_code='FIRST_TURN_SOURCE_STALE')
    for s_key, i_key in (
        ('id', 'source_context_id'),
        ('context_epoch', 'source_context_epoch'),
        ('version', 'source_version'),
        ('resident_generation', 'source_resident_generation'),
    ):
        if int(source[s_key]) != int(intent[i_key]):
            raise FirstTurnError('source identity mismatch', error_code='FIRST_TURN_SOURCE_STALE')
    messages = _collect_context_formal_messages(
        conn, context_id=source_id, context_epoch=int(source['context_epoch']),
    )
    live_boundary = _last_formal_message_id(messages)
    if live_boundary != int(intent['source_boundary_message_id']):
        raise FirstTurnError('source boundary stale', error_code='FIRST_TURN_SOURCE_STALE')

    chat_id = str(intent['chat_id'] or DEFAULT_CHAT_ID)
    current = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
    if int(current['id']) != source_id:
        raise FirstTurnError('source not canonical', error_code='FIRST_TURN_SOURCE_STALE')

    target_live = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (int(target['id']),),
    ).fetchone())
    assert target_live is not None
    if str(target_live.get('window_mode') or '') != WINDOW_MODE_MANUAL_STAGED:
        raise FirstTurnError('target not staged', error_code='FIRST_TURN_TARGET_STALE')
    if str(target_live.get('status') or '') != STATUS_PROVISIONAL:
        raise FirstTurnError('target status mismatch', error_code='FIRST_TURN_TARGET_STALE')
    if target_live.get('closed_at'):
        raise FirstTurnError('target closed', error_code='FIRST_TURN_TARGET_STALE')
    if int(target_live.get('resident_generation') or 0) != 1:
        raise FirstTurnError('target generation mismatch', error_code='FIRST_TURN_TARGET_STALE')
    if str(target_live.get('switch_request_id') or '') != req_id:
        raise FirstTurnError('target switch_request mismatch', error_code='FIRST_TURN_TARGET_STALE')
    if str(target_live.get('claude_session_id') or '') != str(intent.get('target_session_id') or ''):
        raise FirstTurnError('target session mismatch', error_code='FIRST_TURN_TARGET_STALE')

    reg = get_context_claude_session(
        int(target['id']), int(target['resident_generation']), conn=conn,
    )
    if reg is None:
        raise FirstTurnError('registry missing', error_code='FIRST_TURN_REGISTRY_MISSING')
    if (
        int(reg['context_id']) != int(target['id'])
        or int(reg['context_epoch']) != int(target['context_epoch'])
        or int(reg['resident_generation']) != int(target['resident_generation'])
    ):
        raise FirstTurnError('registry identity mismatch', error_code='FIRST_TURN_REGISTRY_MISMATCH')
    if str(reg['claude_session_id']) != str(intent.get('target_session_id') or ''):
        raise FirstTurnError('registry session mismatch', error_code='FIRST_TURN_REGISTRY_MISMATCH')
    if str(reg.get('scan_status') or '') != SCAN_STATUS_READY:
        raise FirstTurnError('registry not ready', error_code='FIRST_TURN_REGISTRY_MISMATCH')
    start_offset = int(live.get('first_turn_start_offset') or intent.get('target_jsonl_size') or 0)
    if int(reg['scan_offset']) != start_offset:
        raise FirstTurnError('registry offset mismatch', error_code='FIRST_TURN_REGISTRY_MISMATCH')
    frozen_path = session_jsonl_path_for_cwd(
        forge_cwd,
        str(intent.get('target_session_id') or ''),
        claude_home=Path(claude_home),
    )
    if frozen_path is not None:
        if Path(str(reg.get('transcript_path') or '')).resolve() != Path(frozen_path).resolve():
            raise FirstTurnError(
                'registry path mismatch', error_code='FIRST_TURN_REGISTRY_MISMATCH',
            )

    msg_row = conn.execute(
        'SELECT context_id, context_epoch, resident_generation FROM daily_message_contexts '
        'WHERE message_id=?',
        (int(user_message_id),),
    ).fetchone()
    if msg_row is None:
        raise FirstTurnError('user message not mapped', error_code='FIRST_TURN_MESSAGE_MISSING')
    msg = dict(msg_row)
    if (
        int(msg['context_id']) != int(target['id'])
        or int(msg['context_epoch']) != int(target['context_epoch'])
        or int(msg['resident_generation']) != int(target['resident_generation'])
    ):
        raise FirstTurnError('user message wrong context', error_code='FIRST_TURN_MESSAGE_CONFLICT')

    lease = conn.execute(
        'SELECT lease_owner, request_message_id, expires_at FROM daily_resident_turn_leases '
        'WHERE context_id=? AND resident_generation=?',
        (int(target['id']), int(target['resident_generation'])),
    ).fetchone()
    if lease is None:
        raise FirstTurnError('lease missing', error_code='FIRST_TURN_LEASE_MISSING')
    lease_d = dict(lease)
    if str(lease_d['lease_owner']) != first_turn_request_id:
        raise FirstTurnError('lease owner mismatch', error_code='FIRST_TURN_LEASE_MISMATCH')
    if int(lease_d['request_message_id']) != int(user_message_id):
        raise FirstTurnError('lease message mismatch', error_code='FIRST_TURN_LEASE_MISMATCH')

    close_cur = conn.execute(
        '''UPDATE daily_contexts SET closed_at=?, close_reason=?, version=version+1,
           updated_at=? WHERE id=? AND context_epoch=? AND version=? AND closed_at IS NULL''',
        (
            now_s, CLOSE_REASON_MANUAL, now_s,
            source_id, int(source['context_epoch']), int(source['version']),
        ),
    )
    if close_cur.rowcount != 1:
        raise FirstTurnError('source close CAS failed', error_code='FIRST_TURN_DB_CAS_FAILED')

    promote_cur = conn.execute(
        '''UPDATE daily_contexts SET window_mode=?, updated_at=?
           WHERE id=? AND window_mode=? AND closed_at IS NULL''',
        (WINDOW_MODE_MANUAL, now_s, int(target['id']), WINDOW_MODE_MANUAL_STAGED),
    )
    if promote_cur.rowcount != 1:
        raise FirstTurnError('target promote CAS failed', error_code='FIRST_TURN_DB_CAS_FAILED')

    _update_intent_conn(
        conn,
        req_id,
        status=INTENT_HANDOFF_PENDING,
        fields={
            'first_delta_committed_at': now_s,
            'first_turn_error_code': None,
        },
        now_s=now_s,
    )

    source_after = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
    ).fetchone())
    target_after = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (int(target['id']),),
    ).fetchone())
    assert source_after is not None and target_after is not None
    return _switch_result_from_target_conn(
        conn, source=source_after, target=target_after,
    )


def _release_buffered_first_delta(session: FirstTurnSession) -> tuple[str, ...]:
    """Release held first text exactly once after formal handoff completes."""
    if session._first_delta_released:
        return ()
    held = session._buffered_first_text
    session._first_delta_released = True
    if held is None:
        return ()
    return (held,)


def _rollback_claim_to_ready(
    *,
    switch_request_id: str,
    first_turn_request_id: str,
    target_context_id: int,
    target_resident_generation: int,
    db_path: str,
    now: Optional[Any] = None,
) -> None:
    """Clean reclaim after post-claim prepare failure: READY + release lease."""
    release_resident_turn_lease(
        int(target_context_id),
        int(target_resident_generation),
        lease_owner=str(first_turn_request_id),
        db_path=db_path,
    )
    now_s = _now_s(now)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        live = _intent_row(conn, switch_request_id)
        if live is not None and str(live.get('status') or '') == INTENT_COMMITTING:
            if str(live.get('first_turn_request_id') or '') == str(first_turn_request_id):
                _update_intent_conn(
                    conn,
                    switch_request_id,
                    status=INTENT_READY,
                    fields={'first_turn_error_code': None},
                    now_s=now_s,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _persist_first_assistant_idempotent(
    conn: sqlite3.Connection,
    *,
    intent: dict[str, Any],
    session: FirstTurnSession,
    assistant_content: str,
    chat_id: str,
    now_dt: Any,
    now_s: str,
) -> int:
    """Insert assistant + write first_assistant_message_id in one txn (crash-safe)."""
    existing_aid = intent.get('first_assistant_message_id')
    if existing_aid is not None:
        return int(existing_aid)

    user_mid = int(session.user_message_id)
    orphans = conn.execute(
        '''SELECT d.message_id FROM daily_message_contexts d
           WHERE d.context_id=? AND d.context_epoch=? AND d.resident_generation=?
             AND d.role='assistant' AND d.message_id>?
           ORDER BY d.message_id ASC''',
        (
            int(session.target_context_id),
            int(session.target_context_epoch),
            int(session.target_resident_generation),
            user_mid,
        ),
    ).fetchall()
    if len(orphans) > 1:
        raise FirstTurnError(
            'multiple first-turn assistants',
            error_code='FIRST_TURN_ASSISTANT_AMBIGUOUS',
        )
    if len(orphans) == 1:
        assistant_id = int(orphans[0]['message_id'])
        _update_intent_conn(
            conn,
            session.switch_request_id,
            fields={'first_assistant_message_id': assistant_id},
            now_s=now_s,
        )
        return assistant_id

    row = conn.execute(
        'SELECT id, context_epoch, resident_generation, is_backfill FROM daily_contexts WHERE id=?',
        (int(session.target_context_id),),
    ).fetchone()
    if row is None or int(dict(row).get('is_backfill') or 0):
        raise FirstTurnError('target not active', error_code='FIRST_TURN_TARGET_STALE')
    current = dict(row)
    if (
        int(current['context_epoch']) != int(session.target_context_epoch)
        or int(current['resident_generation']) != int(session.target_resident_generation)
    ):
        raise FirstTurnError('epoch/generation stale', error_code='FIRST_TURN_TARGET_STALE')
    active = conn.execute(
        'SELECT context_epoch FROM daily_contexts '
        "WHERE chat_id=? AND is_backfill=0 AND window_mode != 'manual_staged' "
        'ORDER BY context_epoch DESC LIMIT 1',
        (str(chat_id),),
    ).fetchone()
    if active is None or int(active['context_epoch']) != int(session.target_context_epoch):
        raise FirstTurnError('target not formal current', error_code='FIRST_TURN_TARGET_STALE')
    lease = conn.execute(
        'SELECT lease_owner, expires_at FROM daily_resident_turn_leases '
        'WHERE context_id=? AND resident_generation=?',
        (int(session.target_context_id), int(session.target_resident_generation)),
    ).fetchone()
    if lease is None or str(dict(lease)['lease_owner']) != session.first_turn_request_id:
        raise FirstTurnError('lease mismatch', error_code='FIRST_TURN_LEASE_MISMATCH')
    from chat.daily_context import _parse_local_dt
    try:
        exp_dt = _parse_local_dt(str(dict(lease)['expires_at']))
    except ValueError:
        exp_dt = now_dt
    if exp_dt <= now_dt:
        raise FirstTurnError('lease expired', error_code='FIRST_TURN_LEASE_EXPIRED')

    cur = conn.execute(
        "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info, choices) "
        "VALUES ('assistant', ?, '', '', '', '')",
        (str(assistant_content),),
    )
    assistant_id = int(cur.lastrowid)
    conn.execute(
        'INSERT INTO daily_message_contexts '
        '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (
            assistant_id, int(session.target_context_id),
            int(session.target_context_epoch),
            int(session.target_resident_generation),
            'assistant', now_s,
        ),
    )
    _update_intent_conn(
        conn,
        session.switch_request_id,
        fields={'first_assistant_message_id': assistant_id},
        now_s=now_s,
    )
    return assistant_id


def _complete_handoff_barrier(
  *,
  session: FirstTurnSession,
  switch_result: dict[str, Any],
  hooks: FirstTurnHooks,
  db_path: str,
  now: Optional[Any] = None,
) -> None:
    hook = _before_handoff_hook
    if hook is not None:
        hook()
    with handoff_lock():
        if hooks.formal_holder is not None:
            old = install_target_resident_after_swap(
                holder=hooks.formal_holder,
                staged_resident=session.staged,
                result=switch_result,
                db_path=db_path,
            )
        else:
            bind_target_resident_after_switch(
                staged_resident=session.staged,
                result=switch_result,
                db_path=db_path,
            )
            old = None
        mark_intent_committed(session.switch_request_id, db_path=db_path, now=now)
        if old is not None:
            defer_old_resident_close(old)
        flush_old_resident_close()
    session._handoff_complete = True


@_serialize_context_switch
def claim_and_start_first_turn(
    *,
    switch_request_id: str,
    first_turn_request_id: str,
    user_content: str,
    hooks: Optional[FirstTurnHooks] = None,
    db_path: str,
    now: Optional[Any] = None,
) -> FirstTurnSession:
    """Claim user message to target, lease, READY→COMMITTING, resume candidate."""
    hooks = _require_hooks(hooks)
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    ft_req = str(first_turn_request_id).strip()
    if not ft_req:
        raise FirstTurnError('first_turn_request_id required', error_code='FIRST_TURN_REQUEST_ID')
    req_id = str(switch_request_id)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        intent, target = _load_target_and_intent(conn, req_id)
        status = str(intent.get('status') or '')
        if status not in (INTENT_READY, INTENT_COMMITTING):
            conn.rollback()
            raise FirstTurnError('intent not ready', error_code='FIRST_TURN_INTENT_STATUS')

        existing_ft = intent.get('first_turn_request_id')
        existing_uid = intent.get('first_user_message_id')
        if status == INTENT_COMMITTING:
            if str(existing_ft or '') != ft_req:
                conn.rollback()
                raise FirstTurnError(
                    'committing under another first turn',
                    error_code='FIRST_TURN_IN_PROGRESS',
                )
        elif existing_ft and str(existing_ft) != ft_req:
            conn.rollback()
            raise FirstTurnError(
                'first turn request mismatch',
                error_code='FIRST_TURN_REQUEST_CONFLICT',
            )

        target_id = int(target['id'])
        target_epoch = int(target['context_epoch'])
        target_gen = int(target['resident_generation'])

        if existing_uid:
            user_message_id = int(existing_uid)
            _verify_message_claim(
                conn,
                message_id=user_message_id,
                target_id=target_id,
                target_epoch=target_epoch,
                target_gen=target_gen,
            )
        else:
            user_message_id = _insert_user_message_conn(
                conn, content=user_content, now_s=now_s,
            )
            _map_user_message_conn(
                conn,
                message_id=user_message_id,
                context_id=target_id,
                context_epoch=target_epoch,
                resident_generation=target_gen,
                now_s=now_s,
            )

        reg = get_context_claude_session(target_id, target_gen, conn=conn)
        if reg is None:
            conn.rollback()
            raise FirstTurnError('registry missing', error_code='FIRST_TURN_REGISTRY_MISSING')
        start_offset = int(reg['scan_offset'])

        exp_s = (now_dt + datetime.timedelta(seconds=480)).strftime(
            '%Y-%m-%d %H:%M:%S',
        )
        _claim_staged_turn_lease_conn(
            conn,
            context_id=target_id,
            resident_generation=target_gen,
            lease_owner=ft_req,
            request_message_id=int(user_message_id),
            now_s=now_s,
            exp_s=exp_s,
        )

        if status == INTENT_READY:
            _update_intent_conn(
                conn,
                req_id,
                status=INTENT_COMMITTING,
                fields={
                    'first_turn_request_id': ft_req,
                    'first_user_message_id': int(user_message_id),
                    'first_turn_started_at': now_s,
                    'first_turn_start_offset': start_offset,
                    'first_turn_error_code': None,
                },
                now_s=now_s,
            )

        conn.commit()
    except FirstTurnError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    staged: Any = None
    try:
        forge_path = session_jsonl_path_for_cwd(
            hooks.forge_cwd,
            str(intent['target_session_id']),
            claude_home=hooks.claude_home,
        )
        if forge_path is None:
            raise FirstTurnError('jsonl path missing', error_code='FIRST_TURN_JSONL_MISSING')
        jsonl_path = Path(forge_path)
        staged = hooks.prepare_staged(dict(intent), jsonl_path)
    except Exception:
        try:
            if staged is not None:
                hooks.discard_staged(staged)
        except Exception:
            logger.exception('discard_staged after prepare failure')
        _rollback_claim_to_ready(
            switch_request_id=req_id,
            first_turn_request_id=ft_req,
            target_context_id=target_id,
            target_resident_generation=target_gen,
            db_path=db_path,
            now=now,
        )
        raise

    return FirstTurnSession(
        switch_request_id=req_id,
        first_turn_request_id=ft_req,
        user_message_id=int(user_message_id),
        target_context_id=target_id,
        target_context_epoch=target_epoch,
        target_resident_generation=target_gen,
        staged=staged,
        jsonl_path=jsonl_path,
        start_offset=start_offset,
    )


def mark_first_turn_stdin_sent(session: FirstTurnSession) -> None:
    session.stdin_sent = True


def ingest_first_turn_text_delta(
    session: FirstTurnSession,
    *,
    text: str,
    hooks: Optional[FirstTurnHooks] = None,
    db_path: str,
    now: Optional[Any] = None,
) -> FirstTurnDeltaResult:
    """Buffer first non-empty text; commit DB + handoff before release."""
    hooks = _require_hooks(hooks)
    chunk = str(text or '')
    if not chunk.strip():
        return FirstTurnDeltaResult(released_text=(), db_committed=False, handoff_complete=False)

    if session._handoff_complete:
        return FirstTurnDeltaResult(
            released_text=(chunk,), db_committed=True, handoff_complete=True,
        )

    if session._db_committed:
        return FirstTurnDeltaResult(released_text=(), db_committed=True, handoff_complete=False)

    if session._buffered_first_text is None:
        session._buffered_first_text = chunk

    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        intent, target = _load_target_and_intent(conn, session.switch_request_id)
        switch_result = _commit_first_turn_db_conn(
            conn,
            intent=intent,
            target=target,
            first_turn_request_id=session.first_turn_request_id,
            user_message_id=session.user_message_id,
            forge_cwd=hooks.forge_cwd,
            claude_home=str(hooks.claude_home),
            now_s=now_s,
            now_dt=now_dt,
        )
        conn.commit()
        session._db_committed = True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    hook = _after_db_commit_hook
    if hook is not None:
        hook()

    _complete_handoff_barrier(
        session=session,
        switch_result=switch_result,
        hooks=hooks,
        db_path=db_path,
        now=now,
    )
    released = _release_buffered_first_delta(session)
    return FirstTurnDeltaResult(
        released_text=released, db_committed=True, handoff_complete=True,
    )


@_serialize_context_switch
def abort_first_turn_clean(
    session: FirstTurnSession,
    *,
    hooks: Optional[FirstTurnHooks] = None,
    db_path: str,
    now: Optional[Any] = None,
) -> None:
    """Pre-commit clean failure: source unchanged, target staged, retry allowed."""
    hooks = _require_hooks(hooks)
    if session.stdin_sent:
        raise FirstTurnError('stdin already sent', error_code='FIRST_TURN_NOT_CLEAN')
    if _jsonl_size(session.jsonl_path) > int(session.start_offset):
        raise FirstTurnError('jsonl grew', error_code='FIRST_TURN_NOT_CLEAN')
    if session._db_committed:
        raise FirstTurnError('already committed', error_code='FIRST_TURN_ALREADY_COMMITTED')

    hooks.discard_staged(session.staged)
    release_resident_turn_lease(
        session.target_context_id,
        session.target_resident_generation,
        lease_owner=session.first_turn_request_id,
        db_path=db_path,
    )

    now_s = _now_s(now)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _update_intent_conn(
            conn,
            session.switch_request_id,
            status=INTENT_READY,
            fields={'first_turn_error_code': None},
            now_s=now_s,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_first_turn_precommit_dirty(
    session: FirstTurnSession,
    *,
    error_code: str = 'FIRST_TURN_PRECOMMIT_DIRTY',
    db_path: str,
    now: Optional[Any] = None,
) -> None:
    """Dirty failure after stdin + JSONL growth before DB commit."""
    if session._db_committed:
        raise FirstTurnError('already committed', error_code='FIRST_TURN_ALREADY_COMMITTED')
    now_s = _now_s(now)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _update_intent_conn(
            conn,
            session.switch_request_id,
            fields={
                'first_turn_error_code': str(error_code),
                'orphan_jsonl_state': 'precommit_dirty',
            },
            now_s=now_s,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@_serialize_context_switch
def recover_first_turn_handoff_pending(
    session: FirstTurnSession,
    *,
    hooks: Optional[FirstTurnHooks] = None,
    db_path: str,
    now: Optional[Any] = None,
) -> FirstTurnRecoverResult:
    """Resume HANDOFF_PENDING after post-DB swap failure; never resend user.

    On success, releases the held first text delta exactly once.
    """
    hooks = _require_hooks(hooks)
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        intent = _intent_row(conn, session.switch_request_id)
        if intent is None:
            raise FirstTurnError('intent missing', error_code='FIRST_TURN_INTENT_MISSING')
        status = str(intent.get('status') or '')
        if status == INTENT_COMMITTED and session._handoff_complete:
            released = _release_buffered_first_delta(session)
            source = _row_to_dict(conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?',
                (int(intent['source_context_id']),),
            ).fetchone())
            target = _row_to_dict(conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?',
                (int(intent['target_context_id']),),
            ).fetchone())
            assert source is not None and target is not None
            switch_result = _switch_result_from_target_conn(
                conn, source=source, target=target,
            )
            return FirstTurnRecoverResult(
                switch_result=switch_result, released_text=released,
            )
        if status != INTENT_HANDOFF_PENDING:
            raise FirstTurnError('not handoff_pending', error_code='FIRST_TURN_INTENT_STATUS')
        source = _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?',
            (int(intent['source_context_id']),),
        ).fetchone())
        target = _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?',
            (int(intent['target_context_id']),),
        ).fetchone())
        assert source is not None and target is not None
        switch_result = _switch_result_from_target_conn(conn, source=source, target=target)
    finally:
        conn.close()

    if not session._db_committed:
        session._db_committed = True
    _complete_handoff_barrier(
        session=session,
        switch_result=switch_result,
        hooks=hooks,
        db_path=db_path,
        now=now,
    )
    released = _release_buffered_first_delta(session)
    return FirstTurnRecoverResult(
        switch_result=switch_result, released_text=released,
    )


@_serialize_context_switch
def complete_first_turn_round(
    session: FirstTurnSession,
    *,
    assistant_content: str,
    end_offset: int,
    db_path: str,
    now: Optional[Any] = None,
) -> FirstTurnCompleteResult:
    """Assistant persist + cursor CAS + lease release + completed_at."""
    if not session._handoff_complete:
        raise FirstTurnError('handoff incomplete', error_code='FIRST_TURN_HANDOFF_INCOMPLETE')
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    chat_id = DEFAULT_CHAT_ID

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        intent = _intent_row(conn, session.switch_request_id)
        if intent is None:
            conn.rollback()
            raise FirstTurnError('intent missing', error_code='FIRST_TURN_INTENT_MISSING')
        if str(intent.get('status') or '') != INTENT_COMMITTED:
            conn.rollback()
            raise FirstTurnError('not committed', error_code='FIRST_TURN_INTENT_STATUS')
        if intent.get('first_turn_completed_at') and intent.get('first_assistant_message_id'):
            assistant_id = int(intent['first_assistant_message_id'])
            conn.commit()
            return FirstTurnCompleteResult(
                assistant_message_id=assistant_id,
                cursor_advanced=False,
            )
        assistant_id = _persist_first_assistant_idempotent(
            conn,
            intent=intent,
            session=session,
            assistant_content=str(assistant_content),
            chat_id=chat_id,
            now_dt=now_dt,
            now_s=now_s,
        )
        conn.commit()
    except FirstTurnError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    conn = _connect(db_path)
    try:
        intent = _intent_row(conn, session.switch_request_id)
        assert intent is not None
        source = _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?',
            (int(intent['source_context_id']),),
        ).fetchone())
        target = _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?',
            (int(intent['target_context_id']),),
        ).fetchone())
        assert source is not None and target is not None
        switch_result = _switch_result_from_target_conn(conn, source=source, target=target)
    finally:
        conn.close()

    current_cursor = get_resident_history_cursor(
        session.target_context_id,
        session.target_resident_generation,
        db_path=db_path,
    )
    if current_cursor is not None and int(current_cursor) == int(assistant_id):
        cursor_result = {
            'history_cursor_message_id': int(assistant_id),
            'advanced': False,
        }
    else:
        cursor_before = forged_history_watermark(switch_result)
        cursor_result = advance_resident_history_cursor(
            session.target_context_id,
            session.target_resident_generation,
            assistant_id,
            expected_cursor=cursor_before,
            db_path=db_path,
        )

    release_resident_turn_lease(
        session.target_context_id,
        session.target_resident_generation,
        lease_owner=session.first_turn_request_id,
        db_path=db_path,
    )

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _update_intent_conn(
            conn,
            session.switch_request_id,
            fields={
                'first_turn_end_offset': int(end_offset),
                'first_turn_completed_at': now_s,
                'first_turn_error_code': None,
            },
            now_s=now_s,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return FirstTurnCompleteResult(
        assistant_message_id=int(assistant_id),
        cursor_advanced=bool(cursor_result.get('advanced', True)),
    )


def offline_first_turn_hooks(work_root: str | Path) -> FirstTurnHooks:
    """Test-only hooks: fake staged resident, optional formal holder."""
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    cwd = str(root / 'cwd')
    Path(cwd).mkdir(parents=True, exist_ok=True)
    claude_home = root / 'claude_home'
    claude_home.mkdir(parents=True, exist_ok=True)

    class _OfflineStaged:
        def __init__(self, session_id: str, jsonl_path: Path):
            self.session_id = session_id
            self.jsonl_path = jsonl_path
            self.generation = 1
            self.tool_profile = 'text_only'
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def kill(self) -> None:
            self._alive = False

    class _FakeHolder:
        def __init__(self):
            self._current: Any = None

        def get(self) -> Any:
            return self._current

        def swap(self, new: Any) -> Any:
            old = self._current
            self._current = new
            return old

    def prepare_staged(intent: dict[str, Any], forge_path: Path) -> Any:
        before = forge_path.read_bytes()
        staged = _OfflineStaged(str(intent['target_session_id']), forge_path)
        if forge_path.read_bytes() != before:
            raise FirstTurnError('jsonl mutated', error_code='FIRST_TURN_JSONL_MUTATED')
        return staged

    def discard_staged(staged: Any) -> None:
        if staged is not None and hasattr(staged, 'kill'):
            staged.kill()

    return FirstTurnHooks(
        prepare_staged=prepare_staged,
        discard_staged=discard_staged,
        formal_holder=_FakeHolder(),
        forge_cwd=cwd,
        claude_home=claude_home,
    )
