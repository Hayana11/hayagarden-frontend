"""Shared per-request chat turn lifecycle for moments chat-collection intents."""

from __future__ import annotations

import secrets
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import moments_intent

DEFAULT_CONVERSATION_ID = 'hayana-chat'
_SHANGHAI = timezone(timedelta(hours=8))


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _now_str() -> str:
    return datetime.now(_SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')


def ensure_turn_schema(memories_db_path: str) -> None:
    conn = sqlite3.connect(memories_db_path)
    try:
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS moments_active_turn (
                conversation_id TEXT PRIMARY KEY,
                turn_key TEXT NOT NULL UNIQUE,
                user_message_id INTEGER,
                started_at TEXT NOT NULL
            )'''
        )
        conn.execute(
            '''CREATE TABLE IF NOT EXISTS moments_pending_intent (
                turn_key TEXT PRIMARY KEY,
                previous_turns INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT ''
            )'''
        )
        conn.commit()
    finally:
        conn.close()


def get_active_turn(memories_db_path: str, conversation_id: str) -> dict[str, Any] | None:
    key = (conversation_id or DEFAULT_CONVERSATION_ID).strip() or DEFAULT_CONVERSATION_ID
    conn = _conn(memories_db_path)
    try:
        row = conn.execute(
            'SELECT conversation_id, turn_key, user_message_id, started_at '
            'FROM moments_active_turn WHERE conversation_id=?',
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        'conversation_id': row['conversation_id'],
        'turn_key': row['turn_key'],
        'user_message_id': row['user_message_id'],
        'started_at': row['started_at'],
    }


def prepare_turn(
    turn_data: dict[str, Any] | None = None,
    *,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
    memories_db_path: str | None = None,
) -> dict[str, Any]:
    del conversation_id, memories_db_path
    data = dict(turn_data or {})
    data['turn_key'] = secrets.token_hex(16)
    return data


def activate_turn(
    turn_data: dict[str, Any],
    *,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
    memories_db_path: str,
) -> dict[str, Any]:
    conv = (conversation_id or DEFAULT_CONVERSATION_ID).strip() or DEFAULT_CONVERSATION_ID
    data = dict(turn_data)
    turn_key = (data.get('turn_key') or '').strip()
    if not turn_key:
        raise ValueError('turn_key required')

    conn = _conn(memories_db_path)
    try:
        old = conn.execute(
            'SELECT turn_key FROM moments_active_turn WHERE conversation_id=?',
            (conv,),
        ).fetchone()
        if old:
            moments_intent.clear_pending(memories_db_path, old['turn_key'])
        conn.execute('DELETE FROM moments_active_turn WHERE conversation_id=?', (conv,))
        conn.execute(
            '''INSERT INTO moments_active_turn
               (conversation_id, turn_key, user_message_id, started_at)
               VALUES (?, ?, ?, ?)''',
            (conv, turn_key, data.get('user_message_id'), _now_str()),
        )
        conn.commit()
    finally:
        conn.close()
    return data


def begin_turn(
    turn_data: dict[str, Any] | None = None,
    *,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
    memories_db_path: str,
) -> dict[str, Any]:
    data = prepare_turn(
        turn_data,
        conversation_id=conversation_id,
        memories_db_path=memories_db_path,
    )
    return activate_turn(
        data,
        conversation_id=conversation_id,
        memories_db_path=memories_db_path,
    )


def sync_user_message_id(
    memories_db_path: str,
    *,
    conversation_id: str,
    turn_key: str,
    user_message_id: int,
) -> None:
    conv = (conversation_id or DEFAULT_CONVERSATION_ID).strip() or DEFAULT_CONVERSATION_ID
    conn = _conn(memories_db_path)
    try:
        conn.execute(
            'UPDATE moments_active_turn SET user_message_id=? '
            'WHERE conversation_id=? AND turn_key=?',
            (int(user_message_id), conv, turn_key),
        )
        conn.commit()
    finally:
        conn.close()


def insert_user_message(
    get_db_fn: Callable[[], Any],
    turn_data: dict[str, Any],
    content: str,
    *,
    memories_db_path: str,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
) -> dict[str, Any]:
    turn_data = dict(turn_data)
    text = (content or '').strip()
    if not text:
        # Redo/edit stream: reuse explicit identity, no new INSERT.
        from chat.scoring_identity import parse_scoring_message_id
        mid = parse_scoring_message_id(turn_data.get('user_message_id'))
        if mid is not None:
            turn_data['user_message_id'] = mid
        return turn_data
    conn = get_db_fn()
    previous_user_at = None
    created_at = None
    user_id = None
    _user_events_requested = all(
        str(os.environ.get(name, '0')).strip() == '1'
        for name in (
            'INTERNAL_STATE_V3_SHADOW_ENABLED',
            'INTERNAL_STATE_V3_SCORE_PROOF_ENABLED',
            'INTERNAL_STATE_V3_USER_EVENTS_ENABLED',
        )
    )
    try:
        # previous_user_at：必须在插入本条之前读取，避免 longing 被算成 0。
        # Stage C Bond authority needs clocks; tolerate fixtures missing created_at.
        from chat.interaction_state import USER_AUTHOR_SQL
        try:
            prev = conn.execute(
                f"SELECT created_at FROM chat_messages WHERE {USER_AUTHOR_SQL} "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if prev is not None:
                previous_user_at = (
                    prev['created_at'] if hasattr(prev, 'keys') else prev[0]
                )
                previous_user_at = str(previous_user_at) if previous_user_at else None
        except Exception:
            previous_user_at = None
        cur = conn.execute(
            "INSERT INTO chat_messages (author,content) VALUES ('hayana',?)",
            (text,),
        )
        user_id = int(cur.lastrowid)
        try:
            row = conn.execute(
                "SELECT created_at FROM chat_messages WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is not None:
                created_at = row['created_at'] if hasattr(row, 'keys') else row[0]
                created_at = str(created_at) if created_at else None
        except Exception:
            created_at = None
        capture_alert_failed = False
        # Optional Shadow outbox (flag-gated diagnostics). Stage C authority
        # applies user_rule after commit via affect_bond_authority; same
        # event_key is idempotent if both paths fire.
        if _user_events_requested and user_id is not None and created_at:
            try:
                import internal_state_shadow as _shadow
                if _shadow.is_user_events_enabled():
                    try:
                        _shadow.enqueue_user_rule_in_txn(
                            conn,
                            message_id=user_id,
                            text=text,
                            created_at=created_at,
                            previous_user_at=previous_user_at,
                        )
                    except Exception:
                        try:
                            _shadow.mark_proof_gap(
                                conn,
                                failed_message_id=user_id,
                                error_code='outbox_capture_gap',
                                db_path=memories_db_path,
                            )
                        except Exception:
                            try:
                                _gap_result = _shadow.mark_proof_gap_standalone(
                                    db_path=memories_db_path,
                                    failed_message_id=user_id,
                                    error_code='outbox_capture_gap',
                                )
                                if _gap_result.status == 'failed':
                                    capture_alert_failed = not _shadow.note_capture_evidence_failure(
                                        f'user_rule message_id={user_id}',
                                        db_path=memories_db_path,
                                    )
                            except Exception as _gap_exc:
                                capture_alert_failed = not _shadow.note_capture_evidence_failure(
                                    f'user_rule standalone exception={_gap_exc}',
                                    db_path=memories_db_path,
                                )
            except Exception as _cap_exc:
                try:
                    import internal_state_shadow as _shadow2
                    capture_alert_failed = not _shadow2.note_capture_evidence_failure(
                        f'user_rule import/enable: {_cap_exc}',
                        db_path=memories_db_path,
                    )
                except Exception:
                    try:
                        from internal_state_capture_alert import write_capture_alert
                        capture_alert_failed = not write_capture_alert(
                            db_path=memories_db_path,
                            detail=f'user_rule shadow import/enable: {_cap_exc}',
                        )
                    except Exception:
                        capture_alert_failed = True
        conn.commit()
        turn_data['user_message_id'] = user_id
    finally:
        conn.close()
    # One shared touch after successful persist — covers CC/Relay/old /chat.
    try:
        from chat.interaction_state import touch_user_interaction
        touch_user_interaction(get_db_fn)
    except Exception:
        pass
    # Stage C: authoritative Bond mutation (Canonical user_rule). Best-effort;
    # chat must not fail if V3 apply has a transient error.
    if user_id is not None and created_at:
        try:
            from chat.affect_bond_authority import apply_user_rule_best_effort
            apply_user_rule_best_effort(
                message_id=int(user_id),
                text=text,
                created_at=str(created_at),
                previous_user_at=previous_user_at,
                db_path=memories_db_path,
            )
        except Exception:
            pass
    # commit 后立即 drain（失败行仍保留，可重放）
    if user_id is not None and created_at:
        try:
            import internal_state_shadow as _shadow
            _shadow.drain_shadow_outbox_best_effort(db_path=memories_db_path)
        except Exception:
            pass
    turn_key = turn_data.get('turn_key')
    if turn_key:
        sync_user_message_id(
            memories_db_path,
            conversation_id=conversation_id,
            turn_key=str(turn_key),
            user_message_id=int(turn_data['user_message_id']),
        )
    return turn_data


def release_turn(
    *,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
    memories_db_path: str,
    turn_key: str | None = None,
    persisted: bool = False,
) -> None:
    conv = (conversation_id or DEFAULT_CONVERSATION_ID).strip() or DEFAULT_CONVERSATION_ID
    active = get_active_turn(memories_db_path, conv)
    key = (turn_key or (active or {}).get('turn_key') or '').strip()
    if not key:
        return
    if not persisted:
        moments_intent.clear_pending(memories_db_path, key)
    conn = _conn(memories_db_path)
    try:
        conn.execute(
            'DELETE FROM moments_active_turn WHERE conversation_id=? AND turn_key=?',
            (conv, key),
        )
        conn.commit()
    finally:
        conn.close()


def collect_chat_moment(
    memories_db_path: str,
    *,
    turn_key: str | None = None,
    conversation_id: str = DEFAULT_CONVERSATION_ID,
    previous_turns: int = 0,
    caption: str = '',
) -> None:
    conv = (conversation_id or DEFAULT_CONVERSATION_ID).strip() or DEFAULT_CONVERSATION_ID
    key = (turn_key or '').strip()
    if not key:
        active = get_active_turn(memories_db_path, conv)
        if not active:
            raise ValueError('no active turn')
        key = active['turn_key']
    active = get_active_turn(memories_db_path, conv)
    if not active or active['turn_key'] != key:
        raise ValueError('turn_key does not match active turn')
    moments_intent.set_pending(
        memories_db_path,
        key,
        previous_turns=previous_turns,
        caption=caption,
    )
