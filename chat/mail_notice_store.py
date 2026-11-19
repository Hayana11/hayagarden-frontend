"""Small durable store for Gmail message IDs awaiting the next formal model turn."""

def ensure_schema(get_db_fn):
    conn = get_db_fn()
    try:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS pending_mail_notices ('
            'message_id TEXT PRIMARY KEY, '
            "created_at DATETIME NOT NULL DEFAULT (datetime('now','+8 hours')), "
            'consumed INTEGER NOT NULL DEFAULT 0)'
        )
        conn.commit()
    finally:
        conn.close()


def record_pending_mail_notice(get_db_fn, message_id):
    conn = get_db_fn()
    try:
        cur = conn.execute(
            'INSERT OR IGNORE INTO pending_mail_notices (message_id) VALUES (?)',
            (message_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def peek_pending_mail_notice_ids(get_db_fn):
    conn = get_db_fn()
    try:
        return [
            row[0]
            for row in conn.execute(
                'SELECT message_id FROM pending_mail_notices '
                'WHERE consumed=0 ORDER BY created_at, message_id'
            )
        ]
    finally:
        conn.close()


def consume_pending_mail_notices(get_db_fn, message_ids):
    message_ids = list(message_ids or ())
    if not message_ids:
        return 0
    conn = get_db_fn()
    try:
        consumed = 0
        for message_id in message_ids:
            cur = conn.execute(
                'UPDATE pending_mail_notices SET consumed=1 '
                'WHERE message_id=? AND consumed=0',
                (message_id,),
            )
            consumed += cur.rowcount
        conn.commit()
        return consumed
    finally:
        conn.close()
