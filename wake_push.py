"""Wake notification delivery to the Elpis app shell.

Two protocols share ``GET /api/wake_log/pending_notification``:

legacy (no ``protocol`` param, unauthenticated) — what the retired shell and
already-installed Canary builds speak: the latest pending message (and a
screenshot command) is returned and marked delivered in the same request. A
response lost on the way loses the message. Kept only until the new Canary is
installed and verified; then it is switched off separately.

ack (``protocol=ack``) — requires the Health Bridge device credential
(``X-Health-Device-ID`` + ``Authorization: Bearer``); the route rejects anything
else with 401 before touching wake or screenshot state. Then:
  * the latest pending message for the current window is leased to that device
    (``push_lease_until`` / ``push_lease_device``) and returned with a stable id
    ``wake-<row id>``; handing it out never consumes it;
  * the lease is taken with a conditional UPDATE checked by rowcount, so two
    devices never both hold it; the same device may re-take its own lease (a
    restarted process need not wait), and its on-device ledger shows it once;
  * ``POST /api/wake_log/ack {"ids": [...]}`` is the only thing that marks a
    message delivered. Every id must have been handed to the acking device,
    otherwise the whole request is refused (403) and nothing changes. Acks are
    idempotent;
  * an unacknowledged message is handed out again once its lease expires
    (dropped response, killed process, no notification permission);
  * ``since=<n>``: "this device has shown everything up to wake-<n>". It acks
    rows at or below n that were handed to this device, recovering an ack lost
    before a reboot; never rows handed elsewhere or never handed out;
  * in non-soft-window mode an ack of the latest message also retires older
    still-pending messages: only the latest is ever delivered (the legacy
    no-flood rule), so they would otherwise surface one by one afterwards;
  * screenshot commands go only to a device that passes ``commands=screenshot``
    and are bound to it; ``cap-<id>`` acks from that device move them to
    ``received``.

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


class AckRefused(Exception):
    """An ack named something this device was never handed; nothing was changed."""


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


def _add_column(conn, table, column, decl):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(%s)' % table)}
    if column in cols:
        return
    try:
        conn.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, decl))
        conn.commit()
    except sqlite3.OperationalError as exc:
        # Two first requests after deploy can race to migrate; the loser is fine.
        if 'duplicate column' not in str(exc):
            raise


def ensure_columns(conn):
    _add_column(conn, 'wake_log', 'push_lease_until', 'REAL')
    _add_column(conn, 'wake_log', 'push_lease_device', 'TEXT')
    _add_column(conn, 'screenshot_requests', 'dispatched_device', 'TEXT')


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

    ensure_columns(conn)
    sreq = _claim_screenshot(conn)
    conn.commit()

    if not row and not sreq:
        return None
    return _payload(row, sreq)


# ── ack protocol (caller has authenticated device_id) ────────
def lease_pending_payload(conn, fetch_row, soft_window, now, device_id,
                          since=0, want_screenshot=False):
    """Lease (not consume) the latest message to device_id; optionally a screenshot command."""
    ensure_columns(conn)
    if since:
        conn.execute(
            "UPDATE wake_log SET notified=1 WHERE action='message' "
            "AND (notified IS NULL OR notified=0) AND push_lease_device=? AND id<=?",
            (device_id, since),
        )
    row = fetch_row(conn)
    leased = None
    if row:
        cur = conn.execute(
            "UPDATE wake_log SET push_lease_until=?, push_lease_device=? "
            "WHERE id=? AND (notified IS NULL OR notified=0) "
            "AND (push_lease_until IS NULL OR push_lease_until<=? OR push_lease_device=?)",
            (now + LEASE_S, device_id, int(row['id']), now, device_id),
        )
        if cur.rowcount == 1:
            leased = row
    sreq = _claim_screenshot(conn, device_id) if want_screenshot else None
    conn.commit()
    if not leased and not sreq:
        return None
    payload = _payload(leased, sreq)
    payload['protocol'] = ACK_PROTOCOL
    payload['lease_s'] = LEASE_S
    return payload


def ack(conn, ids, soft_window, device_id):
    """Acknowledge ids handed to device_id. All-or-nothing: raises AckRefused, changing nothing."""
    ensure_columns(conn)
    wake_ids, cap_ids = [], []
    for raw in ids or []:
        key = str(raw or '').strip()
        if key.startswith('wake-') and key[5:].isdigit():
            row = conn.execute(
                "SELECT push_lease_device FROM wake_log WHERE id=? AND action='message'",
                (int(key[5:]),),
            ).fetchone()
            if not row or row[0] != device_id:
                raise AckRefused(key)
            wake_ids.append(int(key[5:]))
        elif key.startswith('cap-') and key[4:].isdigit():
            row = conn.execute(
                "SELECT dispatched_device FROM screenshot_requests WHERE id=?",
                (int(key[4:]),),
            ).fetchone()
            if not row or row[0] != device_id:
                raise AckRefused(key)
            cap_ids.append(int(key[4:]))
        else:
            raise AckRefused(key)

    for wake_id in wake_ids:
        conn.execute("UPDATE wake_log SET notified=1 WHERE id=?", (wake_id,))
        if not soft_window:
            conn.execute(
                "UPDATE wake_log SET notified=1 WHERE action='message' "
                "AND (notified IS NULL OR notified=0) AND id<?",
                (wake_id,),
            )
    for cap_id in cap_ids:
        conn.execute(
            "UPDATE screenshot_requests SET status='received' "
            "WHERE id=? AND status IN ('pending','dispatched')",
            (cap_id,),
        )
    conn.commit()
    return {('wake-%d' % i): 'acked' for i in wake_ids} | {('cap-%d' % i): 'acked' for i in cap_ids}


# ── shared ───────────────────────────────────────────────────
def _claim_screenshot(conn, device_id=None):
    # 手机截屏指令：优先取 pending；若 dispatched 超过 2 分钟还没回传，重试下发一次
    sreq = conn.execute(
        "SELECT id FROM screenshot_requests "
        "WHERE status='pending' "
        "   OR (status='dispatched' AND dispatched_at < " + SCREENSHOT_LEASE_SQL + ") "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if sreq:
        if device_id is None:
            conn.execute(
                "UPDATE screenshot_requests SET status='dispatched', dispatched_at=datetime('now','+8 hours'), "
                "dispatched_device=NULL WHERE id=?",
                (int(sreq['id']),)
            )
        else:
            conn.execute(
                "UPDATE screenshot_requests SET status='dispatched', dispatched_at=datetime('now','+8 hours'), "
                "dispatched_device=? WHERE id=?",
                (device_id, int(sreq['id']))
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
