"""
wake/executor.py
执行 wake action：写 wake_log、发消息、写日记、discharge drive/desire。
"""


def execute(action: str, thoughts: str, content: str,
            mode: str, get_db_fn,
            desire_driven: bool = False):
    """
    action: 'none' | 'message' | 'diary' | 'explore'
    get_db_fn: callable，返回 sqlite3 connection（来自 gateway.get_db）
    """
    conn = get_db_fn()

    # 记录 wake_log
    conn.execute(
        "INSERT INTO wake_log (thoughts, action, content, consumed, woke_at) "
        "VALUES (?,?,?,0,datetime('now','+8 hours'))",
        (thoughts, action, content)
    )
    conn.commit()

    # 执行 action
    if action == 'message' and content and mode not in ('summarize', 'dream'):
        conn.execute(
            "INSERT INTO chat_messages (author, content, thinking) VALUES ('fyodor',?,?)",
            (content, thoughts)
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
