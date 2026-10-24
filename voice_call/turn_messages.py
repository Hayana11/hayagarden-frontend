"""Which chat messages belong to a call, so the chat page can fold them away.

Every call turn still goes through the normal chat pipeline and stays in the
chat history the model reads (that is how he remembers the call). Only the chat
page hides them: it shows the call's record line ("ᯅ 通话时长 …") instead, and
that opens the call's own transcript.

A call turn is her message (mapped here by id when it is sent) plus the replies
that follow it up to her next message. Replies are found by position rather
than by id because a barge-in detaches the stream before the reply id arrives;
wake messages and call records are never folded into a turn.
"""
import sqlite3

TABLE = 'voice_call_turn_messages'
TURN = 'turn'
RECORD = 'record'
_REPLY_AUTHORS = ('fyodor', 'assistant', 'claude')  # same set as the chat page's isFyAuthor
_NOT_REPLIES = ('wake', 'call')  # source_kind of rows that are never part of a turn


def ensure_table(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS %s ('
        ' chat_message_id INTEGER PRIMARY KEY,'
        ' call_id TEXT NOT NULL,'
        " created_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ')' % TABLE
    )


def mark_turn(db_path, call_id, chat_message_id):
    """Record that ``chat_message_id`` (her message) was said in call ``call_id``."""
    if not call_id or chat_message_id is None:
        return
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        ensure_table(conn)
        conn.execute(
            'INSERT OR IGNORE INTO %s (chat_message_id, call_id) VALUES (?, ?)' % TABLE,
            (int(chat_message_id), str(call_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _is_reply(row):
    author = str(row.get('author') or '').lower()
    return author in _REPLY_AUTHORS and str(row.get('source_kind') or 'chat') not in _NOT_REPLIES


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def fold_call_turns(conn, rows):
    """Chat page view of ``rows`` (dicts in id order): call turns removed, call
    records tagged with ``call_id``. ``conn`` must be the chat database."""
    if not rows:
        return rows
    ids = [row['id'] for row in rows]
    lo, hi = min(ids), max(ids)
    turns = {}
    current = None  # call id of the turn whose replies we are inside
    if _table_exists(conn, TABLE):
        turns = dict(conn.execute(
            'SELECT chat_message_id, call_id FROM %s WHERE chat_message_id BETWEEN ? AND ?' % TABLE,
            (lo, hi),
        ).fetchall())
        # The page may open with replies whose turn started on the previous page:
        # look at the last row before the page that is not a reply.
        cols = {r[1] for r in conn.execute('PRAGMA table_info(chat_messages)')}
        kind = "coalesce(source_kind, 'chat')" if 'source_kind' in cols else "'chat'"
        before = conn.execute(
            'SELECT id FROM chat_messages WHERE id < ? AND NOT (lower(author) IN (%s) AND %s NOT IN (%s)) '
            'ORDER BY id DESC LIMIT 1'
            % (','.join('?' * len(_REPLY_AUTHORS)), kind, ','.join('?' * len(_NOT_REPLIES))),
            (lo, *_REPLY_AUTHORS, *_NOT_REPLIES),
        ).fetchone()
        if before is not None:
            prior = conn.execute(
                'SELECT call_id FROM %s WHERE chat_message_id=?' % TABLE, (before[0],)
            ).fetchone()
            current = prior[0] if prior else None
    records = {}
    if _table_exists(conn, 'voice_call_chat_records'):
        records = {
            mid: cid for cid, mid in conn.execute(
                'SELECT call_id, chat_message_id FROM voice_call_chat_records '
                'WHERE chat_message_id BETWEEN ? AND ?', (lo, hi),
            ).fetchall()
        }
    if not turns and not records and current is None:
        return rows
    callers = {}
    if records and _table_exists(conn, 'voice_calls'):
        call_cols = {r[1] for r in conn.execute('PRAGMA table_info(voice_calls)')}
        if 'caller' in call_cols:
            wanted = sorted(set(records.values()))
            callers = dict(conn.execute(
                'SELECT id, caller FROM voice_calls WHERE id IN (%s)' % ','.join('?' * len(wanted)),
                wanted,
            ).fetchall())
    out = []
    for row in rows:
        rid = row['id']
        if rid in records:
            current = None
            tagged = dict(row)
            tagged['call_id'] = records[rid]
            tagged['call_part'] = RECORD
            tagged['call_caller'] = callers.get(records[rid]) or 'her'  # who placed it: the card's side
            out.append(tagged)
            continue
        if rid in turns:
            current = turns[rid]
            continue
        if current is not None and _is_reply(row):
            continue
        if not _is_reply(row):
            current = None  # her own (non-call) message ends the turn
        out.append(row)
    return out
