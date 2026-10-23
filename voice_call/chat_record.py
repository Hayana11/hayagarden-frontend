"""Leave one line in the chat for each call, like WeChat's call records.

The line is written straight into chat_messages as Fyodor's, so it shows on
his side of the chat and is part of the next turn's history (which is how the
model learns the call is over), but no reply is generated for it.

Idempotent per call id: the chat row and a voice_call_chat_records row keyed by
the call id are written in one transaction, so retrying after a failure, or
after a crash that lost the caller's "recorded" mark, never adds a second line.
"""
import sqlite3


def _ensure_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS voice_call_chat_records ("
        " call_id TEXT PRIMARY KEY,"
        " chat_message_id INTEGER NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )


def record_call_in_chat(db_path, summary):
    """Return the chat message id holding this call's record (existing or new)."""
    summary = summary or {}
    record = summary.get('record') or ''
    call_id = ((summary.get('call') or {}).get('id') or '').strip()
    if not record:
        return None
    if not call_id:
        raise ValueError('call record needs a call id')
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    try:
        _ensure_table(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = conn.execute(
                "SELECT chat_message_id FROM voice_call_chat_records WHERE call_id=?", (call_id,)
            ).fetchone()
            if existing:
                conn.execute('COMMIT')
                return existing[0]
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
            conn.execute(
                "INSERT INTO voice_call_chat_records (call_id, chat_message_id) VALUES (?, ?)",
                (call_id, cur.lastrowid),
            )
            conn.execute('COMMIT')
            return cur.lastrowid
        except BaseException:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()
