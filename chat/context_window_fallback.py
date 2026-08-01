"""Context Window v0.2 — Owner cold fallback from last-good (R0).

Owner-explicit abandon of a failed first-turn; creates a brand-new recovery
context from the last-good target checkpoint. Does not resend the failed user,
forge a missing assistant, reopen the old source, or call the model.
"""
from __future__ import annotations

import getpass
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from chat.context_window import (
    ACTIVE_INTENT_STATUSES,
    CLOSE_REASON_COLD_FALLBACK,
    INTENT_COMMITTING,
    INTENT_HANDOFF_PENDING,
    INTENT_RELEASED,
    WINDOW_MODE_MANUAL,
    _carryover_ids_conn,
    _collect_context_formal_messages,
    _intent_row,
    _is_resident_turn_active_conn,
    _now_s,
    _shanghai_now,
    _update_intent_conn,
    get_active_switch_intent,
    get_latest_last_good_checkpoint,
    resolve_canonical_context_row_conn,
    shanghai_calendar_day,
)
from chat.daily_context import (
    CHAT_DAY_START_HOUR,
    DEFAULT_CHAT_ID,
    DEFAULT_TIMEZONE,
    STATUS_PROVISIONAL,
    _active_epoch_high_water,
    _connect,
    _row_to_dict,
    ensure_schema,
    get_resident_history_cursor,
    group_carryover_rounds,
)
from chat.session_registry import SCAN_STATUS_READY, get_context_claude_session

logger = logging.getLogger(__name__)

ERROR_ABANDONED = 'FIRST_TURN_ABANDONED_BY_OWNER'
FALLBACK_PRECONDITION_FAILED = 'FALLBACK_PRECONDITION_FAILED'
FALLBACK_CHECKPOINT_UNPROVEN = 'FALLBACK_CHECKPOINT_UNPROVEN'
FALLBACK_HISTORY_INVALID = 'FALLBACK_HISTORY_INVALID'

_ALLOWED_FAILED_STATUSES = frozenset({INTENT_COMMITTING, INTENT_HANDOFF_PENDING})


class FallbackError(Exception):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = str(error_code)


@dataclass(frozen=True)
class FallbackRecoverResult:
    failed_request_id: str
    fallback_request_id: str
    fallback_context_id: int
    fallback_context_epoch: int
    safe_cursor: int
    floor_cursor: int
    carryover_message_ids: tuple[int, ...]
    last_good_switch_request_id: str
    closed_context_id: int


def _os_actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = 'unknown'
    return 'local_cli:%s' % user


def build_owner_canary_fixture(
    *,
    db_path: str,
    claude_home: Any,
    cwd: Any,
    chat_id: str,
    seed_last_good: bool = True,
    after_recovery: bool = False,
) -> dict[str, Any]:
    """Synthesize last-good + failed first-turn intent for Owner Canary / tests.

    Uses tempfile DB only. Never touches production paths.
    """
    import datetime
    import sqlite3
    from pathlib import Path

    from chat import daily_context as dc
    from chat.context_window import (
        INTENT_COMMITTING,
        INTENT_COMMITTED,
        WINDOW_MODE_MANUAL,
        _now_s,
    )
    from chat.daily_context import WINDOW_MODE_MANUAL_STAGED
    from chat.session_registry import register_context_claude_session

    now = datetime.datetime(2026, 8, 1, 12, 0, 0)
    now_s = _now_s(now)
    _ = after_recovery  # reserved for future multi-fail sequences
    home = Path(claude_home)
    work = Path(cwd)
    home.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                cache_info TEXT DEFAULT '',
                choices TEXT DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            )'''
        )
        conn.commit()
    finally:
        conn.close()
    dc.ensure_schema(db_path)

    def _ins(author: str, content: str) -> int:
        c = sqlite3.connect(db_path)
        cur = c.execute(
            'INSERT INTO chat_messages (author, content) VALUES (?,?)',
            (author, content),
        )
        c.commit()
        mid = int(cur.lastrowid)
        c.close()
        return mid

    def _bind(mid: int, ctx_id: int, epoch: int, gen: int, role: str) -> None:
        dc.record_daily_message_context(
            mid, context_id=ctx_id, context_epoch=epoch,
            resident_generation=gen, role=role, db_path=db_path,
        )

    if seed_last_good:
        # Bootstrap a source, then a completed last-good target with an extra round.
        source = dc.get_or_create_daily_context(
            chat_id=chat_id, local_day='2026-08-01', db_path=db_path, now=now,
        )
        source_id = int(source['id'])
        source_epoch = int(source['context_epoch'])
        source_gen = int(source['resident_generation'])
        u0 = _ins('hayana', 'seed-user-0')
        a0 = _ins('fyodor', 'seed-asst-0')
        _bind(u0, source_id, source_epoch, source_gen, 'user')
        _bind(a0, source_id, source_epoch, source_gen, 'assistant')

        # Close source and create last-good target manually.
        c = _connect(db_path)
        try:
            c.execute('BEGIN IMMEDIATE')
            c.execute(
                '''UPDATE daily_contexts SET closed_at=?, close_reason=?, version=version+1,
                   updated_at=? WHERE id=? AND closed_at IS NULL''',
                (now_s, 'manual', now_s, source_id),
            )
            next_epoch = int(source_epoch) + 1
            lg_req = str(uuid.uuid4())
            cur = c.execute(
                '''INSERT INTO daily_contexts (
                    chat_id, local_day, timezone, boundary_hour, context_epoch,
                    boundary_message_id, status, carryover_count, carryover_requested_count,
                    selection_finalized_at, is_backfill, resident_generation, version,
                    created_at, updated_at, window_mode, opened_at, closed_at,
                    close_reason, source_context_id, switch_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 0, 1, 1, ?, ?, ?, ?, NULL, NULL, ?, ?)''',
                (
                    chat_id, '2026-08-01', DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                    next_epoch, a0, STATUS_PROVISIONAL, now_s, now_s, now_s,
                    WINDOW_MODE_MANUAL, now_s, source_id, lg_req,
                ),
            )
            lg_id = int(cur.lastrowid)
            c.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                'VALUES (?,?,?)',
                (lg_id, 0, u0),
            )
            c.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                'VALUES (?,?,?)',
                (lg_id, 1, a0),
            )
            # Committed intent with last-good checkpoint.
            c.execute(
                '''INSERT INTO context_switch_intents (
                    request_id, chat_id, payload_hash, status,
                    source_context_id, source_context_epoch, source_version,
                    source_resident_generation, source_boundary_message_id,
                    carryover_count, selected_message_ids_json,
                    target_context_id, created_at, updated_at,
                    first_turn_completed_at, first_assistant_message_id,
                    last_good_context_id, last_good_context_epoch,
                    last_good_resident_generation, last_good_history_cursor_message_id,
                    last_good_recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (
                    lg_req, chat_id, 'canary', INTENT_COMMITTED,
                    source_id, source_epoch, 1, source_gen, a0,
                    1, json.dumps([u0, a0]),
                    lg_id, now_s, now_s, now_s, 0,  # placeholder asst id updated below
                    lg_id, next_epoch, 1, 0, now_s,
                ),
            )
            c.commit()
        finally:
            c.close()

        # First-turn user/assistant on last-good target + extra success round.
        ft_user = _ins('hayana', 'last-good-first-user')
        ft_asst = _ins('fyodor', 'last-good-first-asst')
        _bind(ft_user, lg_id, next_epoch, 1, 'user')
        _bind(ft_asst, lg_id, next_epoch, 1, 'assistant')
        extra_u = _ins('hayana', 'extra-success-user')
        extra_a = _ins('fyodor', 'extra-success-asst')
        _bind(extra_u, lg_id, next_epoch, 1, 'user')
        _bind(extra_a, lg_id, next_epoch, 1, 'assistant')
        dc.advance_resident_history_cursor(
            lg_id, 1, extra_a, db_path=db_path,
        )
        # Register READY registry covering extra_a.
        sid = str(uuid.uuid4())
        register_context_claude_session(
            context_id=lg_id,
            context_epoch=next_epoch,
            resident_generation=1,
            chat_id=chat_id,
            claude_session_id=sid,
            cwd=str(work),
            source='owner_canary',
            scan_offset=4096,
            claude_home=str(home),
            db_path=db_path,
        )
        c = _connect(db_path)
        try:
            c.execute(
                '''UPDATE context_claude_sessions
                   SET last_mapped_message_id=?, scan_status=?
                   WHERE context_id=? AND resident_generation=?''',
                (extra_a, SCAN_STATUS_READY, lg_id, 1),
            )
            c.execute(
                '''INSERT INTO chat_message_claude_events (
                    event_uuid, message_id, role, claude_session_id,
                    context_id, context_epoch, resident_generation, jsonl_byte_offset
                ) VALUES (?,?,?,?,?,?,?,?)''',
                (
                    str(uuid.uuid4()), extra_a, 'assistant', sid,
                    lg_id, next_epoch, 1, 2048,
                ),
            )
            c.execute(
                '''UPDATE context_switch_intents SET
                   first_assistant_message_id=?,
                   last_good_history_cursor_message_id=?
                   WHERE request_id=?''',
                (ft_asst, ft_asst, lg_req),
            )
            c.commit()
        finally:
            c.close()
        last_good_context_id = lg_id
        last_good_epoch = next_epoch
    else:
        # Reuse existing open recovery / last-good from prior recover.
        c = _connect(db_path)
        try:
            row = c.execute(
                '''SELECT id, context_epoch FROM daily_contexts
                   WHERE chat_id=? AND window_mode=? AND closed_at IS NULL
                   ORDER BY context_epoch DESC LIMIT 1''',
                (chat_id, WINDOW_MODE_MANUAL),
            ).fetchone()
            assert row is not None
            last_good_context_id = int(row['id'])
            last_good_epoch = int(row['context_epoch'])
        finally:
            c.close()

    # Failed switch from last-good identity: committing + staged target + failed user.
    failed_req = str(uuid.uuid4())
    ft_req = str(uuid.uuid4())
    c = _connect(db_path)
    try:
        src = _row_to_dict(c.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (last_good_context_id,),
        ).fetchone())
        assert src is not None
        # If after_recovery, current open is recovery; treat it as source for a new fail.
        source_id = int(src['id'])
        source_epoch = int(src['context_epoch'])
        source_gen = int(src.get('resident_generation') or 1)
        source_version = int(src.get('version') or 1)
        # At most one uncleared manual_staged per chat — clear any leftover.
        c.execute(
            '''UPDATE daily_contexts
               SET window_mode=?, closed_at=COALESCE(closed_at, ?),
                   close_reason=COALESCE(close_reason, 'canary_clear_staged'),
                   version=version+1, updated_at=?
               WHERE chat_id=? AND window_mode=?''',
            (
                WINDOW_MODE_MANUAL, now_s, now_s, chat_id,
                WINDOW_MODE_MANUAL_STAGED,
            ),
        )
        next_epoch = _active_epoch_high_water(c, chat_id) + 1
        cur = c.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, is_backfill,
                resident_generation, version, created_at, updated_at,
                window_mode, opened_at, source_context_id, switch_request_id
            ) VALUES (?, ?, ?, ?, ?, 0, ?, 0, 0, 1, 1, ?, ?, ?, ?, ?, ?)''',
            (
                chat_id, '2026-08-01', DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                next_epoch, STATUS_PROVISIONAL, now_s, now_s,
                WINDOW_MODE_MANUAL_STAGED, now_s, source_id, failed_req,
            ),
        )
        failed_target_id = int(cur.lastrowid)
        c.commit()
    finally:
        c.close()

    failed_user = _ins('hayana', 'FAILED_USER_DO_NOT_RESEND')
    # Map failed user to failed target (as first-turn would).
    _bind(failed_user, failed_target_id, next_epoch, 1, 'user')
    c = _connect(db_path)
    try:
        c.execute(
            '''INSERT INTO context_switch_intents (
                request_id, chat_id, payload_hash, status,
                source_context_id, source_context_epoch, source_version,
                source_resident_generation, source_boundary_message_id,
                carryover_count, selected_message_ids_json,
                target_context_id, orphan_jsonl_state,
                first_turn_request_id, first_user_message_id,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                failed_req, chat_id, 'fail', INTENT_COMMITTING,
                source_id, source_epoch, source_version, source_gen, 0,
                0, '[]', failed_target_id, 'precommit_dirty',
                ft_req, failed_user, now_s, now_s,
            ),
        )
        c.commit()
    finally:
        c.close()

    return {
        'last_good_context_id': last_good_context_id,
        'last_good_epoch': last_good_epoch,
        'failed_request_id': failed_req,
        'expected_status': INTENT_COMMITTING,
        'expected_first_turn_request_id': ft_req,
        'failed_user_message_id': failed_user,
        'failed_target_id': failed_target_id,
        'chat_id': chat_id,
    }


def inspect_failed_first_turn(
    *,
    request_id: str,
    db_path: str,
    chat_id: str = DEFAULT_CHAT_ID,
) -> dict[str, Any]:
    """Read-only snapshot for owner inspection (no mutation)."""
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        intent = _intent_row(conn, str(request_id))
        if intent is None:
            return {'ok': False, 'error_code': 'INTENT_MISSING', 'request_id': request_id}
        active = get_active_switch_intent(chat_id=chat_id, db_path=db_path)
        checkpoint = get_latest_last_good_checkpoint(chat_id=chat_id, db_path=db_path)
        try:
            canonical = resolve_canonical_context_row_conn(
                conn, chat_id=chat_id,
            )
            canonical_id = int(canonical['id'])
        except Exception as exc:
            canonical_id = None
            canonical_err = str(exc)
        else:
            canonical_err = None
        target_id = intent.get('target_context_id')
        target_gen = None
        lease_active = None
        if target_id is not None:
            trow = _row_to_dict(conn.execute(
                'SELECT resident_generation FROM daily_contexts WHERE id=?',
                (int(target_id),),
            ).fetchone())
            if trow is not None:
                target_gen = int(trow['resident_generation'])
                lease_active = _is_resident_turn_active_conn(
                    conn, int(target_id), target_gen, _shanghai_now(),
                )
        return {
            'ok': True,
            'request_id': str(intent['request_id']),
            'chat_id': str(intent['chat_id']),
            'status': str(intent.get('status') or ''),
            'orphan_jsonl_state': intent.get('orphan_jsonl_state'),
            'first_turn_request_id': intent.get('first_turn_request_id'),
            'first_user_message_id': intent.get('first_user_message_id'),
            'first_assistant_message_id': intent.get('first_assistant_message_id'),
            'first_turn_completed_at': intent.get('first_turn_completed_at'),
            'source_context_id': intent.get('source_context_id'),
            'target_context_id': intent.get('target_context_id'),
            'is_latest_active': (
                active is not None
                and str(active.get('request_id')) == str(request_id)
            ),
            'canonical_context_id': canonical_id,
            'canonical_error': canonical_err,
            'target_lease_active': lease_active,
            'last_good': checkpoint,
        }
    finally:
        conn.close()


def _assistant_mapped_in_context_conn(
    conn,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    message_id: int,
) -> bool:
    row = conn.execute(
        '''SELECT role FROM daily_message_contexts
           WHERE message_id=? AND context_id=? AND context_epoch=?
             AND resident_generation=?''',
        (
            int(message_id), int(context_id), int(context_epoch),
            int(resident_generation),
        ),
    ).fetchone()
    return row is not None and str(row['role']) == 'assistant'


def _prove_safe_cursor_conn(
    conn,
    *,
    checkpoint: dict[str, Any],
    db_path: str,
) -> int:
    """Return proven safe_cursor (>= floor) or raise FallbackError."""
    floor = int(checkpoint['history_cursor_message_id'])
    ctx_id = int(checkpoint['context_id'])
    epoch = int(checkpoint['context_epoch'])
    gen = int(checkpoint['resident_generation'])

    if not _assistant_mapped_in_context_conn(
        conn,
        context_id=ctx_id,
        context_epoch=epoch,
        resident_generation=gen,
        message_id=floor,
    ):
        raise FallbackError(
            'floor cursor not a proven assistant in last-good context',
            error_code=FALLBACK_CHECKPOINT_UNPROVEN,
        )

    durable = get_resident_history_cursor(ctx_id, gen, db_path=db_path)
    if durable is None:
        return floor
    safe = int(durable)
    if safe < floor:
        raise FallbackError(
            'durable cursor behind floor',
            error_code=FALLBACK_CHECKPOINT_UNPROVEN,
        )
    if safe == floor:
        return floor

    if not _assistant_mapped_in_context_conn(
        conn,
        context_id=ctx_id,
        context_epoch=epoch,
        resident_generation=gen,
        message_id=safe,
    ):
        return floor

    reg = get_context_claude_session(ctx_id, gen, conn=conn)
    if reg is None:
        return floor
    if str(reg.get('scan_status') or '') != SCAN_STATUS_READY:
        return floor
    mapped = reg.get('last_mapped_message_id')
    if mapped is None or int(mapped) != safe:
        return floor

    # scan_offset must cover the assistant's terminating event mapping.
    evt = conn.execute(
        '''SELECT MAX(jsonl_byte_offset) AS mx FROM chat_message_claude_events
           WHERE message_id=? AND context_id=? AND role='assistant' ''',
        (safe, ctx_id),
    ).fetchone()
    if evt is None or evt['mx'] is None:
        return floor
    if int(reg.get('scan_offset') or 0) < int(evt['mx']):
        return floor

    # Every round from floor..safe must be complete user/assistant.
    messages = _collect_context_formal_messages(
        conn, context_id=ctx_id, context_epoch=epoch,
    )
    rounds = group_carryover_rounds(messages)
    relevant = [
        rnd for rnd in rounds
        if max(int(x) for x in rnd['message_ids']) <= safe
        and max(int(x) for x in rnd['message_ids']) >= floor
    ]
    # Also include rounds entirely between that touch the range.
    for rnd in rounds:
        mids = [int(x) for x in rnd['message_ids']]
        if any(floor <= m <= safe for m in mids) and rnd not in relevant:
            relevant.append(rnd)
    for rnd in relevant:
        roles = [m['role'] for m in rnd['messages']]
        if not roles or roles[0] != 'user' or roles[-1] != 'assistant':
            return floor
        if max(int(x) for x in rnd['message_ids']) > safe:
            return floor
    return safe


def _build_recovery_carryover_ids_conn(
    conn,
    *,
    checkpoint: dict[str, Any],
    safe_cursor: int,
) -> list[int]:
    """Original last-good carryover + complete formal rounds ≤ safe_cursor."""
    ctx_id = int(checkpoint['context_id'])
    epoch = int(checkpoint['context_epoch'])
    inherited = _carryover_ids_conn(conn, ctx_id)
    messages = _collect_context_formal_messages(
        conn, context_id=ctx_id, context_epoch=epoch,
    )
    # Restrict to messages at or before safe_cursor.
    bounded = [m for m in messages if int(m['id']) <= int(safe_cursor)]
    rounds = group_carryover_rounds(bounded)
    round_ids: list[int] = []
    for rnd in rounds:
        roles = [m['role'] for m in rnd['messages']]
        if not roles or roles[0] != 'user' or roles[-1] != 'assistant':
            # Incomplete trailing user-only / broken round — fail closed.
            raise FallbackError(
                'incomplete formal round in recovery history',
                error_code=FALLBACK_HISTORY_INVALID,
            )
        for mid in rnd['message_ids']:
            round_ids.append(int(mid))

    seen: set[int] = set()
    out: list[int] = []
    for mid in list(inherited) + round_ids:
        mid_i = int(mid)
        if mid_i in seen:
            continue
        seen.add(mid_i)
        out.append(mid_i)
    if not out and int(safe_cursor) > 0:
        # Last-good with only the first assistant still needs that material.
        # Empty after a proven floor is invalid.
        raise FallbackError(
            'empty recovery history materials',
            error_code=FALLBACK_HISTORY_INVALID,
        )
    # Every id must still exist.
    if out:
        placeholders = ','.join('?' * len(out))
        found = {
            int(r['id'])
            for r in conn.execute(
                'SELECT id FROM chat_messages WHERE id IN (%s)' % placeholders,
                tuple(out),
            ).fetchall()
        }
        if found != set(out):
            raise FallbackError(
                'recovery history message missing',
                error_code=FALLBACK_HISTORY_INVALID,
            )
    return out


def recover_from_last_good(
    *,
    request_id: str,
    expected_status: str,
    expected_first_turn_request_id: str,
    reason: str,
    confirm_abandon_failed_turn: bool,
    db_path: str,
    chat_id: str = DEFAULT_CHAT_ID,
    now: Optional[Any] = None,
    actor: Optional[str] = None,
) -> FallbackRecoverResult:
    """Owner-explicit abandon + create cold fallback recovery context.

    Single BEGIN IMMEDIATE transaction. Never calls the model or starts a resident.
    """
    if not confirm_abandon_failed_turn:
        raise FallbackError(
            'confirm_abandon_failed_turn required',
            error_code=FALLBACK_PRECONDITION_FAILED,
        )
    reason_s = str(reason or '').strip()
    if not reason_s:
        raise FallbackError(
            'reason required',
            error_code=FALLBACK_PRECONDITION_FAILED,
        )
    exp_status = str(expected_status or '').strip()
    if exp_status not in _ALLOWED_FAILED_STATUSES:
        raise FallbackError(
            'expected_status not abandonable',
            error_code=FALLBACK_PRECONDITION_FAILED,
        )
    exp_ft = str(expected_first_turn_request_id or '').strip()
    if not exp_ft:
        raise FallbackError(
            'expected_first_turn_request_id required',
            error_code=FALLBACK_PRECONDITION_FAILED,
        )

    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    local_day = shanghai_calendar_day(now_dt)
    actor_s = str(actor or _os_actor())
    fallback_request_id = str(uuid.uuid4())
    req_id = str(request_id)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')

        intent = _intent_row(conn, req_id)
        if intent is None:
            conn.rollback()
            raise FallbackError('intent missing', error_code=FALLBACK_PRECONDITION_FAILED)
        if str(intent.get('chat_id') or '') != str(chat_id):
            conn.rollback()
            raise FallbackError('chat_id mismatch', error_code=FALLBACK_PRECONDITION_FAILED)

        # Latest active intent for this chat.
        active = conn.execute(
            'SELECT request_id FROM context_switch_intents '
            'WHERE chat_id=? AND status IN (%s) '
            'ORDER BY created_at DESC LIMIT 1'
            % ','.join('?' for _ in ACTIVE_INTENT_STATUSES),
            (chat_id, *tuple(ACTIVE_INTENT_STATUSES)),
        ).fetchone()
        if active is None or str(active['request_id']) != req_id:
            conn.rollback()
            raise FallbackError(
                'not latest active intent',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )

        live_status = str(intent.get('status') or '')
        if live_status != exp_status:
            conn.rollback()
            raise FallbackError(
                'status CAS mismatch',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )
        if live_status not in _ALLOWED_FAILED_STATUSES:
            conn.rollback()
            raise FallbackError(
                'status not abandonable',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )
        if str(intent.get('first_turn_request_id') or '') != exp_ft:
            conn.rollback()
            raise FallbackError(
                'first_turn_request_id CAS mismatch',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )
        if intent.get('first_turn_completed_at') is not None:
            conn.rollback()
            raise FallbackError(
                'first turn already completed',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )
        # No proven complete first assistant on this failed turn.
        if intent.get('first_assistant_message_id') is not None:
            conn.rollback()
            raise FallbackError(
                'first assistant already present',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )

        target_id = intent.get('target_context_id')
        if target_id is not None:
            trow = _row_to_dict(conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?',
                (int(target_id),),
            ).fetchone())
            if trow is not None:
                tgen = int(trow.get('resident_generation') or 1)
                if _is_resident_turn_active_conn(conn, int(target_id), tgen, now_dt):
                    conn.rollback()
                    raise FallbackError(
                        'target lease still active',
                        error_code=FALLBACK_PRECONDITION_FAILED,
                    )

        try:
            canonical = resolve_canonical_context_row_conn(
                conn, chat_id=chat_id, now=now_dt,
            )
        except Exception:
            conn.rollback()
            raise FallbackError(
                'canonical resolve failed',
                error_code=FALLBACK_PRECONDITION_FAILED,
            ) from None

        source_id = int(intent['source_context_id'])
        if live_status == INTENT_COMMITTING:
            if int(canonical['id']) != source_id:
                conn.rollback()
                raise FallbackError(
                    'canonical must be source while committing',
                    error_code=FALLBACK_PRECONDITION_FAILED,
                )
            close_id = source_id
            close_epoch = int(intent['source_context_epoch'])
            close_version = int(intent['source_version'])
        else:
            # handoff_pending: canonical must be failed target.
            if target_id is None or int(canonical['id']) != int(target_id):
                conn.rollback()
                raise FallbackError(
                    'canonical must be target while handoff_pending',
                    error_code=FALLBACK_PRECONDITION_FAILED,
                )
            close_id = int(target_id)
            close_epoch = int(canonical['context_epoch'])
            close_version = int(canonical.get('version') or 1)

        # Last-good checkpoint (global reader; must be complete).
        # Re-read inside txn from committed intents.
        lg_row = conn.execute(
            '''SELECT request_id,
                      last_good_context_id,
                      last_good_context_epoch,
                      last_good_resident_generation,
                      last_good_history_cursor_message_id,
                      last_good_recorded_at
               FROM context_switch_intents
               WHERE chat_id=?
                 AND status='committed'
                 AND first_turn_completed_at IS NOT NULL
                 AND last_good_context_id IS NOT NULL
                 AND last_good_context_epoch IS NOT NULL
                 AND last_good_resident_generation IS NOT NULL
                 AND last_good_history_cursor_message_id IS NOT NULL
                 AND last_good_recorded_at IS NOT NULL
               ORDER BY last_good_recorded_at DESC, updated_at DESC
               LIMIT 1''',
            (str(chat_id),),
        ).fetchone()
        if lg_row is None:
            conn.rollback()
            raise FallbackError(
                'no complete last-good checkpoint',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )
        checkpoint = {
            'context_id': int(lg_row['last_good_context_id']),
            'context_epoch': int(lg_row['last_good_context_epoch']),
            'resident_generation': int(lg_row['last_good_resident_generation']),
            'history_cursor_message_id': int(lg_row['last_good_history_cursor_message_id']),
            'recorded_at': str(lg_row['last_good_recorded_at']),
            'switch_request_id': str(lg_row['request_id']),
        }

        try:
            safe_cursor = _prove_safe_cursor_conn(
                conn, checkpoint=checkpoint, db_path=db_path,
            )
            carryover_ids = _build_recovery_carryover_ids_conn(
                conn, checkpoint=checkpoint, safe_cursor=safe_cursor,
            )
        except FallbackError:
            conn.rollback()
            raise

        floor_cursor = int(checkpoint['history_cursor_message_id'])

        # Close the failure-scene open canonical.
        close_cur = conn.execute(
            '''UPDATE daily_contexts SET closed_at=?, close_reason=?, version=version+1,
               updated_at=? WHERE id=? AND context_epoch=? AND version=? AND closed_at IS NULL''',
            (
                now_s, CLOSE_REASON_COLD_FALLBACK, now_s,
                int(close_id), int(close_epoch), int(close_version),
            ),
        )
        if close_cur.rowcount != 1:
            conn.rollback()
            raise FallbackError(
                'close failure scene CAS failed',
                error_code=FALLBACK_PRECONDITION_FAILED,
            )

        next_epoch = _active_epoch_high_water(conn, chat_id) + 1
        round_count = 0
        # Approximate carryover_count as complete rounds in materials.
        # Count user messages among carryover_ids that start rounds.
        if carryover_ids:
            placeholders = ','.join('?' * len(carryover_ids))
            authors = {
                int(r['id']): str(r['author']).lower()
                for r in conn.execute(
                    'SELECT id, author FROM chat_messages WHERE id IN (%s)'
                    % placeholders,
                    tuple(carryover_ids),
                ).fetchall()
            }
            from chat.daily_context import _USER_AUTHORS
            round_count = sum(
                1 for mid in carryover_ids
                if authors.get(mid, '') in _USER_AUTHORS
            )

        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, carryover_requested_count,
                selection_finalized_at, is_backfill, resident_generation, version,
                created_at, updated_at, window_mode, opened_at, closed_at,
                close_reason, source_context_id, switch_request_id, claude_session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                next_epoch, int(safe_cursor), STATUS_PROVISIONAL,
                int(round_count), int(round_count), now_s,
                now_s, now_s, WINDOW_MODE_MANUAL, now_s,
                int(checkpoint['context_id']), fallback_request_id,
            ),
        )
        recovery_id = int(cur.lastrowid)
        ordinal = 0
        for mid in carryover_ids:
            conn.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                'VALUES (?,?,?)',
                (recovery_id, ordinal, int(mid)),
            )
            ordinal += 1

        # Drop expired / matching failed target lease only.
        if target_id is not None:
            trow2 = _row_to_dict(conn.execute(
                'SELECT resident_generation FROM daily_contexts WHERE id=?',
                (int(target_id),),
            ).fetchone())
            if trow2 is not None:
                tgen = int(trow2['resident_generation'])
                lease = conn.execute(
                    'SELECT lease_owner, expires_at FROM daily_resident_turn_leases '
                    'WHERE context_id=? AND resident_generation=?',
                    (int(target_id), tgen),
                ).fetchone()
                if lease is not None:
                    from chat.daily_context import _parse_local_dt
                    try:
                        exp_dt = _parse_local_dt(str(lease['expires_at']))
                    except ValueError:
                        exp_dt = now_dt
                    if exp_dt <= now_dt:
                        conn.execute(
                            'DELETE FROM daily_resident_turn_leases '
                            'WHERE context_id=? AND resident_generation=?',
                            (int(target_id), tgen),
                        )

        _update_intent_conn(
            conn,
            req_id,
            status=INTENT_RELEASED,
            fields={
                'error_code': ERROR_ABANDONED,
                'first_turn_error_code': ERROR_ABANDONED,
                'owner_abandoned_at': now_s,
                'owner_abandon_reason': reason_s,
                'owner_abandon_actor': actor_s,
                'fallback_request_id': fallback_request_id,
                'fallback_checkpoint_switch_request_id': checkpoint['switch_request_id'],
                'fallback_history_cursor_message_id': int(safe_cursor),
                'fallback_context_id': recovery_id,
                'fallback_context_epoch': next_epoch,
                'fallback_committed_at': now_s,
            },
            now_s=now_s,
        )

        conn.commit()
        return FallbackRecoverResult(
            failed_request_id=req_id,
            fallback_request_id=fallback_request_id,
            fallback_context_id=recovery_id,
            fallback_context_epoch=next_epoch,
            safe_cursor=int(safe_cursor),
            floor_cursor=floor_cursor,
            carryover_message_ids=tuple(carryover_ids),
            last_good_switch_request_id=checkpoint['switch_request_id'],
            closed_context_id=int(close_id),
        )
    except FallbackError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
