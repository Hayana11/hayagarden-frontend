"""Leave one line in the chat for each call, like WeChat's call records.

The line is written straight into chat_messages as Fyodor's, so it shows on
his side of the chat and is part of the next turn's history (which is how the
model learns the call is over), but no reply is generated for it.
"""
import sqlite3


def record_call_in_chat(db_path, summary):
    record = (summary or {}).get('record') or ''
    if not record:
        return None
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        cols = {row[1] for row in conn.execute('PRAGMA table_info(chat_messages)')}
        if 'source_kind' in cols:
            cur = conn.execute(
                "INSERT INTO chat_messages (author, content, source_kind) VALUES ('fyodor', ?, 'call')",
                (record,),
            )
        else:
            cur = conn.execute(
                "INSERT INTO chat_messages (author, content) VALUES ('fyodor', ?)",
                (record,),
            )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()
