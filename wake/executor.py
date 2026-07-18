"""
wake/executor.py
执行 wake action：写 wake_log、发消息、写日记、discharge drive/desire。
"""
import json


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
            cache_info=None):
    """
    action: 'none' | 'message' | 'diary' | 'explore'
    get_db_fn: callable，返回 sqlite3 connection（来自 gateway.get_db）
    """
    conn = get_db_fn()
    if isinstance(cache_info, str):
        cache_info_json = cache_info
    elif cache_info:
        cache_info_json = json.dumps(cache_info, ensure_ascii=False)
    else:
        cache_info_json = ''

    # 记录 wake_log
    surfaced_json = None
    if surfaced_desire_ids:
        surfaced_json = json.dumps([str(x) for x in surfaced_desire_ids], ensure_ascii=False)
    wake_columns = _table_columns(conn, 'wake_log')
    columns = ['thoughts', 'action', 'content', 'consumed', 'woke_at']
    values = [thoughts, action, content, 0]
    placeholders = ['?', '?', '?', '?', "datetime('now','+8 hours')"]
    if 'surfaced_desire_ids' in wake_columns:
        columns.append('surfaced_desire_ids')
        values.append(surfaced_json)
        placeholders.append('?')
    if 'cache_info' in wake_columns:
        columns.append('cache_info')
        values.append(cache_info_json)
        placeholders.append('?')
    conn.execute(
        "INSERT INTO wake_log (%s) VALUES (%s)" % (
            ','.join(columns), ','.join(placeholders)
        ),
        tuple(values),
    )
    conn.commit()

    # 执行 action
    if action == 'message' and content and mode not in ('summarize', 'dream'):
        if 'cache_info' in _table_columns(conn, 'chat_messages'):
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
        conn.commit()
    elif action == 'diary' and content and mode != 'summarize':
        conn.execute(
            "INSERT INTO posts (type, content, layer, author, processed) "
            "VALUES ('DIARY',?,'recent','fyodor',0)",
            (content,)
        )
        conn.commit()

    conn.close()

    if desire_ledger_enabled and mode in ('normal', 'nightwatch') and surfaced_desire_ids:
        try:
            import desire_ledger as _dl
            _dl.mark_surfaced(surfaced_desire_ids)
        except Exception:
            pass

    # Discharge drive / desire（dream/summarize 不 discharge）
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
