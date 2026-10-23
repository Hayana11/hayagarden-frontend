"""Pending-notification long poll for the Elpis app shell.

The phone's foreground service calls ``/api/wake_log/pending_notification?wait=N``
in a loop. With ``wait`` the request is held open (re-checking once a second,
with the DB connection closed between checks) until a message or command is
pending or N seconds pass, so a new wake message reaches the phone within about
a second without the phone re-asking every few seconds. Without ``wait`` the
endpoint answers immediately, as the 15-minute WorkManager poll expects.
"""
import time

MAX_WAIT_S = 25
POLL_INTERVAL_S = 1.0


def parse_wait(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_WAIT_S, value))


def take_pending_payload(conn, fetch_row, soft_window):
    """Claim the latest pending wake message and screenshot command, if any.

    Marks what it returns as delivered and commits. Returns None when nothing
    is pending.
    """
    row = fetch_row(conn)
    if row:
        if soft_window:
            # Only mark the matched current-window row; never wipe foreign wakes.
            conn.execute(
                "UPDATE wake_log SET notified=1 WHERE id=?",
                (int(row['id']),),
            )
        else:
            conn.execute(
                "UPDATE wake_log SET notified=1 WHERE action='message' AND (notified IS NULL OR notified=0)"
            )

    # 手机截屏指令：优先取 pending；若 dispatched 超过 2 分钟还没回传，重试下发一次
    sreq = conn.execute(
        "SELECT id FROM screenshot_requests "
        "WHERE status='pending' "
        "   OR (status='dispatched' AND dispatched_at < datetime('now','+8 hours','-2 minutes')) "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if sreq:
        conn.execute(
            "UPDATE screenshot_requests SET status='dispatched', dispatched_at=datetime('now','+8 hours') "
            "WHERE id=?",
            (int(sreq['id']),)
        )

    conn.commit()

    if not row and not sreq:
        return None

    payload = {'has_message': bool(row)}
    if row:
        payload.update({
            'id': 'wake-' + str(row['id']),
            'title': '费奥多尔',
            'content': row['content'],
            'woke_at': row['woke_at'],
        })
    if sreq:
        payload.update({
            'command': 'screenshot',
            'command_id': 'cap-' + str(int(sreq['id'])),
        })
    return payload


def poll(open_conn, fetch_row, soft_window_enabled, wait_s,
         sleep=time.sleep, monotonic=time.monotonic):
    deadline = monotonic() + wait_s
    while True:
        conn = open_conn()
        try:
            payload = take_pending_payload(conn, fetch_row, soft_window_enabled())
        finally:
            conn.close()
        if payload is not None:
            return payload
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {'has_message': False}
        sleep(min(POLL_INTERVAL_S, remaining))
