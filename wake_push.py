"""Wake notification delivery to the Elpis app shell.

``GET /api/wake_log/pending_notification`` speaks one protocol. The legacy
consume-on-read protocol (no ``protocol`` param: latest message returned and
marked delivered in the same request, so a lost response lost the message) was
retired after the new Canary was verified on the owner's phone (#625); such
requests get 410 and change nothing.

ack (``protocol=ack``) — requires the Health Bridge device credential
(``X-Health-Device-ID`` + ``Authorization: Bearer``); the route rejects anything
else with 401 before touching wake state. Then:
  * the latest pending message for the current window is leased to that device
    (``push_lease_until`` / ``push_lease_device``) and returned with a stable id
    ``wake-<row id>``; handing it out never consumes it;
  * the lease is taken with a conditional UPDATE checked by rowcount, so two
    devices never both hold it; the same device may re-take its own lease (a
    restarted process need not wait), and its on-device ledger shows it once —
    except for a row already overtaken by an acked newer message, which is
    never handed out again (see below);
  * ``POST /api/wake_log/ack {"ids": [...]}`` is the only thing that marks a
    message delivered. Every id must have been handed to the acking device,
    otherwise the whole request is refused (403) and nothing changes. Acks are
    idempotent;
  * an unacknowledged message is handed out again once its lease expires
    (dropped response, killed process, no notification permission);
  * acks are explicit and per id: only ``pending`` rows become delivered
    (``notified=1``, ``push_disposition='acked'``); acking an already acked row
    is a no-op, and a late ack of a row that was meanwhile superseded leaves it
    superseded (the response says so). There is no cursor: a "highest id seen"
    cursor cannot prove that every lower id was shown when two pollers run
    concurrently, so ``since`` is accepted for old builds and ignored. An ack
    lost before a reboot is recovered instead by re-delivery after the lease:
    the device ledger recognises the id as shown and acks just that id;
  * in non-soft-window mode only the latest message is ever pushed (the legacy
    no-flood rule). An ack of wake-<n> therefore closes older pending rows as
    *superseded* (``notified=2``, ``push_disposition='superseded'``) — but not
    rows whose lease is still live, which are in flight and wait for their own
    ack. While overtaken and in flight, such a row is not handed out again, not
    even to the device holding the lease, and its lease is not renewed. If the
    lease then runs out unacknowledged, the next poll supersedes it instead of
    handing it out late. Superseded rows were never counted as delivered and are no
    longer pending;
  * phone screenshots are retired (only the old shell could take them; the
    Canary never could): ``commands=screenshot`` is ignored, no command is ever
    handed out, and a ``cap-<id>`` ack is refused like any unknown id.

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
# wake_log.notified: 0/NULL pending, 1 delivered (ack, legacy read, or executor
# audit row), 2 superseded by a newer delivered message (never delivered).
# "Newer delivered" means push_disposition='acked', never a bare notified=1:
# executor audit rows carry notified=1 without having been delivered.
NOTIFIED_DELIVERED = 1
NOTIFIED_SUPERSEDED = 2


class AckRefused(Exception):
    """An ack named something this device was never handed; nothing was changed."""


def parse_wait(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_WAIT_S, value))


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
    _add_column(conn, 'wake_log', 'push_disposition', 'TEXT')


# ── ack protocol (caller has authenticated device_id) ────────
def lease_pending_payload(conn, fetch_row, soft_window, now, device_id):
    """Lease (not consume) the latest message to device_id."""
    ensure_columns(conn)
    row = fetch_row(conn)
    for _ in range(100):
        if not row or soft_window or not _newer_acked_exists(conn, int(row['id'])):
            break
        # Older than a message already delivered: supersede instead of pushing it
        # late — unless its lease is live (in flight; it waits for its own ack).
        cur = conn.execute(
            "UPDATE wake_log SET notified=2, push_disposition='superseded' "
            "WHERE id=? AND (notified IS NULL OR notified=0) "
            "AND (push_lease_until IS NULL OR push_lease_until<=?)",
            (int(row['id']), now),
        )
        if cur.rowcount != 1:
            # In flight and already overtaken: never hand it out again, not even to
            # the device holding the lease (same-device re-take is only for a plain
            # lost response). It stays pending for its own ack or for the lease to
            # run out, after which it is superseded.
            row = None
            break
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
    conn.commit()
    if not leased:
        return None
    payload = _payload(leased)
    payload['protocol'] = ACK_PROTOCOL
    payload['lease_s'] = LEASE_S
    return payload


def ack(conn, ids, soft_window, device_id, now):
    """Acknowledge ids handed to device_id.

    All-or-nothing validation: raises AckRefused, changing nothing, if any id was
    not handed to this device. Returns {id: 'acked' | 'superseded'}.
    """
    ensure_columns(conn)
    wake_ids = []
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
        else:
            raise AckRefused(key)

    results = {}
    for wake_id in wake_ids:
        # Only pending -> delivered. Never turns superseded (or anything else) back.
        conn.execute(
            "UPDATE wake_log SET notified=1, push_disposition='acked' "
            "WHERE id=? AND (notified IS NULL OR notified=0)",
            (wake_id,),
        )
        state = conn.execute(
            "SELECT notified, push_disposition FROM wake_log WHERE id=?", (wake_id,)
        ).fetchone()
        superseded = state[0] == NOTIFIED_SUPERSEDED
        results['wake-%d' % wake_id] = 'superseded' if superseded else 'acked'
        if not soft_window and not superseded:
            conn.execute(
                "UPDATE wake_log SET notified=2, push_disposition='superseded' "
                "WHERE action='message' AND (notified IS NULL OR notified=0) AND id<? "
                "AND (push_lease_until IS NULL OR push_lease_until<=?)",
                (wake_id, now),
            )
    conn.commit()
    return results


def _newer_acked_exists(conn, wake_id):
    return conn.execute(
        "SELECT 1 FROM wake_log WHERE action='message' AND push_disposition='acked' AND id>? LIMIT 1",
        (wake_id,),
    ).fetchone() is not None


# ── shared ───────────────────────────────────────────────────
def _payload(row):
    return {
        'has_message': True,
        'id': 'wake-' + str(row['id']),
        'title': '费奥多尔',
        'content': row['content'],
        'woke_at': row['woke_at'],
    }


# ── incoming call delta (independent of the message lease/ack above) ──
CALL_TITLE = '费佳来电'
_UNCHANGED = object()


def parse_known_call(raw):
    """The call id the phone believes is still ringing (state reconciliation, never an ack)."""
    value = (raw or '').strip()
    return value if value and len(value) <= 64 and value.isalnum() else None


def call_delta(ringing_id, known_call):
    """What to tell the phone about his ringing call, or _UNCHANGED.

    A ringing call it does not know yet: show it ({id, title}). No ringing call but
    it still shows one: withdraw it (None). Otherwise leave its notification alone.
    """
    if ringing_id and ringing_id != known_call:
        return {'id': ringing_id, 'title': CALL_TITLE}
    if not ringing_id and known_call:
        return None
    return _UNCHANGED


def with_call_delta(payload, delta):
    """Attach ``call`` to a message payload (or to an empty one). ``call`` omitted means
    leave the phone's call notification as it is; ``"call": null`` means withdraw it."""
    if delta is _UNCHANGED:
        return payload
    out = dict(payload) if payload is not None else {'has_message': False}
    out['call'] = delta
    return out


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
