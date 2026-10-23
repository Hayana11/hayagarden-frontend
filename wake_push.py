"""Wake notification delivery to the Elpis app shell.

Two protocols share ``GET /api/wake_log/pending_notification``:

legacy (no ``protocol`` param) — what the retired shell and already-installed
Canary builds speak: the latest pending message (and a screenshot command) is
returned and marked delivered in the same request. A response lost on the way
loses the message; kept only because those clients cannot acknowledge.

ack (``protocol=ack``) — handing a message out never consumes it:
  * the latest pending message for the current window gets a short lease
    (``push_lease_until``) and is returned with a stable id ``wake-<row id>``;
  * a conditional UPDATE claims the lease, so two concurrent pollers never both
    receive the same message while the lease runs;
  * the client shows it, then ``POST /api/wake_log/ack {"ids": [...]}``; ack is
    idempotent and is the only thing that sets ``notified=1``;
  * an unacknowledged message is handed out again once its lease expires
    (network drop, response lost, process killed, no notification permission);
  * ``since=<n>`` is the client's cursor: "I have shown every message up to
    wake-<n>". Rows handed out before and at or below the cursor are acked, which
    recovers acks lost in transit. It never acks a row that was never handed out;
  * screenshot commands are only offered to clients that pass
    ``commands=screenshot``; ``cap-<id>`` acks move them to ``received``.

``wait=N`` (max 25s) holds either kind of request open, re-checking once a
second with the DB connection closed in between, until something is
deliverable or N seconds pass (then ``{"has_message": false}``).
"""
import sqlite3
import time

MAX_WAIT_S = 25
POLL_INTERVAL_S = 1.0
LEASE_S = 60
ACK_PROTOCOL = 'ack'
SCREENSHOT_LEASE_SQL = "datetime('now','+8 hours','-2 minutes')"


def parse_wait(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_WAIT_S, value))


def parse_since(raw):
    try:
        value = int(str(raw or '').replace('wake-', ''))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def ensure_columns(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(wake_log)')}
    if 'push_lease_until' not in cols:
        try:
            conn.execute('ALTER TABLE wake_log ADD COLUMN push_lease_until REAL')
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Two first requests after deploy can race to migrate; the loser is fine.
            if 'duplicate column' not in str(exc):
                raise


# ── legacy protocol ──────────────────────────────────────────
def take_pending_payload(conn, fetch_row, soft_window):
    """Legacy: claim and immediately consume the latest message and a screenshot command."""
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

    sreq = _claim_screenshot(conn)
    conn.commit()

    if not row and not sreq:
        return None
    return _payload(row, sreq)


# ── ack protocol ─────────────────────────────────────────────
def lease_pending_payload(conn, fetch_row, soft_window, now, since=0, want_screenshot=False):
    """Ack protocol: lease (not consume) the latest message; optionally a screenshot command."""
    ensure_columns(conn)
    if since:
        _ack_through(conn, since)
    row = fetch_row(conn)
    leased = None
    if row:
        cur = conn.execute(
            "UPDATE wake_log SET push_lease_until=? "
            "WHERE id=? AND (notified IS NULL OR notified=0) "
            "AND (push_lease_until IS NULL OR push_lease_until<=?)",
            (now + LEASE_S, int(row['id']), now),
        )
        if cur.rowcount == 1:
            leased = row
    sreq = _claim_screenshot(conn) if want_screenshot else None
    conn.commit()
    if not leased and not sreq:
        return None
    payload = _payload(leased, sreq)
    payload['protocol'] = ACK_PROTOCOL
    payload['lease_s'] = LEASE_S
    return payload


def ack(conn, ids, soft_window):
    """Idempotently acknowledge wake-<n> / cap-<n> ids. Returns {id: 'acked'|'unknown'}."""
    ensure_columns(conn)
    results = {}
    for raw in ids or []:
        key = str(raw or '').strip()
        if key.startswith('wake-') and key[5:].isdigit():
            wake_id = int(key[5:])
            exists = conn.execute(
                "SELECT 1 FROM wake_log WHERE id=? AND action='message'", (wake_id,)
            ).fetchone()
            if not exists:
                results[key] = 'unknown'
                continue
            conn.execute("UPDATE wake_log SET notified=1 WHERE id=?", (wake_id,))
            if not soft_window:
                # Same no-flood rule as legacy: seeing the latest retires older pending ones.
                conn.execute(
                    "UPDATE wake_log SET notified=1 WHERE action='message' "
                    "AND (notified IS NULL OR notified=0) AND id<?",
                    (wake_id,),
                )
            results[key] = 'acked'
        elif key.startswith('cap-') and key[4:].isdigit():
            cap_id = int(key[4:])
            exists = conn.execute(
                "SELECT 1 FROM screenshot_requests WHERE id=?", (cap_id,)
            ).fetchone()
            if not exists:
                results[key] = 'unknown'
                continue
            conn.execute(
                "UPDATE screenshot_requests SET status='received' "
                "WHERE id=? AND status IN ('pending','dispatched')",
                (cap_id,),
            )
            results[key] = 'acked'
        else:
            results[key] = 'unknown'
    conn.commit()
    return results


def _ack_through(conn, since):
    conn.execute(
        "UPDATE wake_log SET notified=1 WHERE action='message' "
        "AND (notified IS NULL OR notified=0) AND push_lease_until IS NOT NULL AND id<=?",
        (since,),
    )


# ── shared ───────────────────────────────────────────────────
def _claim_screenshot(conn):
    # 手机截屏指令：优先取 pending；若 dispatched 超过 2 分钟还没回传，重试下发一次
    sreq = conn.execute(
        "SELECT id FROM screenshot_requests "
        "WHERE status='pending' "
        "   OR (status='dispatched' AND dispatched_at < " + SCREENSHOT_LEASE_SQL + ") "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if sreq:
        conn.execute(
            "UPDATE screenshot_requests SET status='dispatched', dispatched_at=datetime('now','+8 hours') "
            "WHERE id=?",
            (int(sreq['id']),)
        )
    return sreq


def _payload(row, sreq):
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


def poll(open_conn, take, wait_s, sleep=time.sleep, monotonic=time.monotonic):
    """Run ``take(conn)`` until it yields a payload or ``wait_s`` passes."""
    deadline = monotonic() + wait_s
    while True:
        conn = open_conn()
        try:
            payload = take(conn)
        finally:
            conn.close()
        if payload is not None:
            return payload
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {'has_message': False}
        sleep(min(POLL_INTERVAL_S, remaining))
