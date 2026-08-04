"""
wake/executor.py
执行 wake action：写 wake_log、发消息、写日记、discharge drive/desire。
"""
import json
import logging

logger = logging.getLogger(__name__)


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def execute(action: str, thoughts: str, content: str,
            mode: str, get_db_fn,
            desire_driven: bool = False,
            surfaced_desire_ids=None,
            desire_ledger_enabled: bool = False,
            cache_info=None,
            wake_run_id: str = '',
            window_identity=None):
    """
    action: 'none' | 'message' | 'diary' | 'explore'
    get_db_fn: callable，返回 sqlite3 connection（来自 gateway.get_db）

    When soft window is on, ``window_identity`` must be the identity frozen at
    wake start. Re-validation and wake_log/chat writes share one IMMEDIATE txn.
    """
    from chat.window_identity import (
        REASON_OK,
        REASON_STALE,
        REASON_UNAVAILABLE,
        ensure_wake_window_identity_columns,
        gate_captured_against_conn,
        log_stale_async_result,
        normalize_window_identity,
        soft_window_enabled,
    )

    conn = get_db_fn()
    if isinstance(cache_info, str):
        cache_info_json = cache_info
    elif cache_info:
        cache_info_json = json.dumps(cache_info, ensure_ascii=False)
    else:
        cache_info_json = ''

    surfaced_json = None
    if surfaced_desire_ids:
        surfaced_json = json.dumps([str(x) for x in surfaced_desire_ids], ensure_ascii=False)

    flag_on = soft_window_enabled()
    captured_norm = normalize_window_identity(window_identity)
    deliver_chat = True
    gate_reason = REASON_OK
    current_norm = None

    try:
        ensure_wake_window_identity_columns(conn)
        wake_columns = _table_columns(conn, 'wake_log')
        msg_cols = _table_columns(conn, 'chat_messages')

        conn.execute('BEGIN IMMEDIATE')

        if flag_on:
            gate_reason, captured_norm, current_norm = gate_captured_against_conn(
                conn, window_identity,
            )
            if gate_reason != REASON_OK:
                deliver_chat = False
                log_stale_async_result(
                    source='wake',
                    reason=gate_reason,
                    wake_run_id=wake_run_id,
                    captured_identity=captured_norm or window_identity,
                    current_identity=current_norm,
                )

        # 记录 wake_log（stale/unavailable 时写审计行：consumed=1, notified=1）
        columns = ['thoughts', 'action', 'content', 'consumed', 'woke_at']
        consumed_val = 1 if (flag_on and gate_reason != REASON_OK) else 0
        values = [thoughts, action, content, consumed_val]
        placeholders = ['?', '?', '?', '?', "datetime('now','+8 hours')"]
        if 'surfaced_desire_ids' in wake_columns:
            columns.append('surfaced_desire_ids')
            values.append(surfaced_json)
            placeholders.append('?')
        if 'cache_info' in wake_columns:
            columns.append('cache_info')
            values.append(cache_info_json)
            placeholders.append('?')
        rid = str(wake_run_id or '').strip()
        if rid and 'wake_run_id' in wake_columns:
            columns.append('wake_run_id')
            values.append(rid)
            placeholders.append('?')
        if 'notified' in wake_columns and flag_on and gate_reason != REASON_OK:
            columns.append('notified')
            values.append(1)
            placeholders.append('?')

        # Persist original four-tuple even on stale audit rows.
        identity_for_row = captured_norm if captured_norm is not None else normalize_window_identity(window_identity)
        if identity_for_row is not None:
            if 'chat_id' in wake_columns:
                columns.append('chat_id')
                values.append(identity_for_row['chat_id'])
                placeholders.append('?')
            if 'context_id' in wake_columns:
                columns.append('context_id')
                values.append(int(identity_for_row['context_id']))
                placeholders.append('?')
            if 'context_epoch' in wake_columns:
                columns.append('context_epoch')
                values.append(int(identity_for_row['context_epoch']))
                placeholders.append('?')
            if 'resident_generation' in wake_columns:
                columns.append('resident_generation')
                values.append(int(identity_for_row['resident_generation']))
                placeholders.append('?')
        elif flag_on and gate_reason == REASON_UNAVAILABLE:
            # Do not backfill current; leave identity columns NULL on audit row.
            pass

        conn.execute(
            "INSERT INTO wake_log (%s) VALUES (%s)" % (
                ','.join(columns), ','.join(placeholders)
            ),
            tuple(values),
        )

        # 执行 action：仅在归属仍然有效时写聊天表面
        if (
            deliver_chat
            and action == 'message'
            and content
            and mode not in ('summarize', 'dream')
        ):
            if 'cache_info' in msg_cols and 'source_kind' in msg_cols:
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, cache_info, source_kind) "
                    "VALUES ('fyodor',?,?,?,'wake')",
                    (content, thoughts, cache_info_json),
                )
            elif 'source_kind' in msg_cols:
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, source_kind) "
                    "VALUES ('fyodor',?,?,'wake')",
                    (content, thoughts),
                )
            elif 'cache_info' in msg_cols:
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking, cache_info) "
                    "VALUES ('fyodor',?,?,?)",
                    (content, thoughts, cache_info_json),
                )
            else:
                conn.execute(
                    "INSERT INTO chat_messages (author, content, thinking) VALUES ('fyodor',?,?)",
                    (content, thoughts),
                )
        elif (
            deliver_chat
            and action == 'diary'
            and content
            and mode != 'summarize'
        ):
            conn.execute(
                "INSERT INTO posts (type, content, layer, author, processed) "
                "VALUES ('DIARY',?,'recent','fyodor',0)",
                (content,)
            )

        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not deliver_chat:
        return

    if desire_ledger_enabled and mode in ('normal', 'nightwatch') and surfaced_desire_ids:
        try:
            import desire_ledger as _dl
            _dl.mark_surfaced(surfaced_desire_ids)
        except Exception:
            pass

    # Stage D: legacy drive/desire writers are retired no-ops.
    # Authoritative Wake drive settlement is gateway → drive_authority
    # → V3 wake_outcome. Keep call sites for compatibility only.
    if mode not in ('dream', 'summarize'):
        try:
            import drive_engine as _de
            _de.discharge_by_action(action, thoughts)
        except Exception:
            pass

        if desire_driven:
            try:
                import desire as _des
                _des.satisfy(action)
            except Exception:
                pass
