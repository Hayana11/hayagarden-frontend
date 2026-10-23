import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wake_push

DEVICE_A = '11111111-1111-4111-8111-111111111111'
DEVICE_B = '22222222-2222-4222-8222-222222222222'


def fetch_row(conn):
    return conn.execute(
        "SELECT id, content, woke_at FROM wake_log "
        "WHERE action='message' AND (notified IS NULL OR notified=0) "
        "ORDER BY id DESC LIMIT 1",
    ).fetchone()


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = self.open_conn()
        conn.executescript("""
            CREATE TABLE wake_log (id INTEGER PRIMARY KEY, action TEXT, content TEXT,
                                   woke_at TEXT, notified INTEGER DEFAULT 0);
            CREATE TABLE screenshot_requests (id INTEGER PRIMARY KEY, status TEXT,
                                              dispatched_at TEXT);
        """)
        conn.commit()
        conn.close()
        self.opened = 0
        self.now = 1000.0

    def tearDown(self):
        os.unlink(self.path)

    def open_conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def counting_open(self):
        self.opened += 1
        return self.open_conn()

    def add_message(self, content):
        conn = self.open_conn()
        cur = conn.execute(
            "INSERT INTO wake_log (action, content, woke_at) VALUES ('message', ?, '2026-10-23 01:00:00')",
            (content,))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def add_screenshot(self):
        conn = self.open_conn()
        cur = conn.execute("INSERT INTO screenshot_requests (status) VALUES ('pending')")
        conn.commit()
        conn.close()
        return cur.lastrowid

    def notified(self, wake_id):
        conn = self.open_conn()
        try:
            return conn.execute("SELECT notified FROM wake_log WHERE id=?", (wake_id,)).fetchone()[0]
        finally:
            conn.close()

    def screenshot_status(self, cap_id):
        conn = self.open_conn()
        try:
            return conn.execute("SELECT status FROM screenshot_requests WHERE id=?", (cap_id,)).fetchone()[0]
        finally:
            conn.close()

    # ack-protocol helpers, all on fresh connections like separate HTTP requests
    def lease(self, screenshot=False, now=None, device=DEVICE_A):
        conn = self.open_conn()
        try:
            return wake_push.lease_pending_payload(
                conn, fetch_row, False, self.now if now is None else now, device,
                want_screenshot=screenshot)
        finally:
            conn.close()

    def ack(self, *ids, soft=False, device=DEVICE_A, now=None):
        conn = self.open_conn()
        try:
            return wake_push.ack(conn, list(ids), soft, device, self.now if now is None else now)
        finally:
            conn.close()


class ParseTests(unittest.TestCase):
    def test_parse_wait_clamps_and_rejects_garbage(self):
        self.assertEqual(wake_push.parse_wait(None), 0)
        self.assertEqual(wake_push.parse_wait('abc'), 0)
        self.assertEqual(wake_push.parse_wait('-5'), 0)
        self.assertEqual(wake_push.parse_wait('10'), 10)
        self.assertEqual(wake_push.parse_wait('999'), wake_push.MAX_WAIT_S)



class AckProtocolTests(Base):
    def test_handing_out_does_not_consume(self):
        wake = self.add_message('晚安')
        payload = self.lease()
        self.assertEqual(payload['id'], 'wake-%d' % wake)
        self.assertEqual(payload['protocol'], 'ack')
        self.assertEqual(payload['lease_s'], wake_push.LEASE_S)
        self.assertEqual(self.notified(wake), 0)

    def test_response_lost_before_reaching_phone_is_redelivered(self):
        wake = self.add_message('晚安')
        self.assertIsNotNone(self.lease())  # response dropped on the way
        # The same device may re-take its own lease at once (restarted process)...
        self.assertEqual(self.lease(now=self.now + 1)['id'], 'wake-%d' % wake)
        # ...another device must wait for the lease to run out.
        # (A's re-take at now+1 renewed the lease to now+1+LEASE_S.)
        self.assertIsNone(self.lease(now=self.now + wake_push.LEASE_S, device=DEVICE_B))
        again = self.lease(now=self.now + 1 + wake_push.LEASE_S, device=DEVICE_B)
        self.assertEqual(again['id'], 'wake-%d' % wake)

    def test_killed_after_receiving_before_ack_is_redelivered(self):
        wake = self.add_message('晚安')
        self.lease()
        # process dies before showing/acking: nothing acked, lease runs out
        self.assertEqual(self.notified(wake), 0)
        self.assertEqual(self.lease(now=self.now + wake_push.LEASE_S + 1)['id'], 'wake-%d' % wake)

    def test_no_notification_permission_means_no_ack_and_no_loss(self):
        wake = self.add_message('晚安')
        for step in range(3):  # client cannot show it, so it never acks
            payload = self.lease(now=self.now + step * (wake_push.LEASE_S + 1))
            self.assertEqual(payload['id'], 'wake-%d' % wake)
        self.assertEqual(self.notified(wake), 0)
        self.assertEqual(self.ack('wake-%d' % wake), {'wake-%d' % wake: 'acked'})
        self.assertEqual(self.notified(wake), 1)

    def test_ack_is_idempotent_and_final(self):
        wake = self.add_message('晚安')
        self.lease()
        key = 'wake-%d' % wake
        self.assertEqual(self.ack(key), {key: 'acked'})
        self.assertEqual(self.ack(key), {key: 'acked'})
        self.assertEqual(self.notified(wake), 1)
        self.assertIsNone(self.lease(now=self.now + 10 * wake_push.LEASE_S))

    def disposition(self, wake_id):
        conn = self.open_conn()
        try:
            return conn.execute("SELECT notified, push_disposition FROM wake_log WHERE id=?",
                                (wake_id,)).fetchone()[:]
        finally:
            conn.close()

    def test_ack_marks_only_the_acked_row_delivered(self):
        older = self.add_message('older, never leased or shown')
        latest = self.add_message('latest')
        self.lease()
        self.ack('wake-%d' % latest)
        self.assertEqual(tuple(self.disposition(latest)), (1, 'acked'))
        # Older pending rows are closed as superseded, never as delivered...
        self.assertEqual(tuple(self.disposition(older)), (2, 'superseded'))
        # ...and are no longer pending, so they do not surface afterwards.
        self.assertIsNone(self.lease(now=self.now + 10 * wake_push.LEASE_S))

    def test_superseded_rows_are_not_resurrected_or_acked_later(self):
        older = self.add_message('older')
        latest = self.add_message('latest')
        self.lease()
        self.ack('wake-%d' % latest)
        with self.assertRaises(wake_push.AckRefused):
            self.ack('wake-%d' % older)  # never handed to this device
        self.assertEqual(tuple(self.disposition(older)), (2, 'superseded'))

    def test_newer_message_after_ack_is_still_delivered(self):
        first = self.add_message('first')
        self.lease()
        self.ack('wake-%d' % first)
        second = self.add_message('second')
        self.assertEqual(self.lease(now=self.now + 1)['id'], 'wake-%d' % second)
        self.assertEqual(tuple(self.disposition(second)), (0, None))

    def test_soft_window_ack_only_touches_that_row(self):
        older = self.add_message('other window')
        latest = self.add_message('mine')
        self.lease()
        self.ack('wake-%d' % latest, soft=True)
        self.assertEqual(self.notified(older), 0)

    def test_unknown_malformed_and_unleased_ids_are_refused_without_changes(self):
        wake = self.add_message('never handed out')
        for bad in ('wake-999', 'cap-999', 'nonsense', '', 'wake-%d' % wake):
            with self.assertRaises(wake_push.AckRefused):
                self.ack(bad)
        self.assertEqual(self.notified(wake), 0)

    def test_ack_by_another_device_is_refused_without_changes(self):
        wake = self.add_message('晚安')
        self.lease(device=DEVICE_A)
        with self.assertRaises(wake_push.AckRefused):
            self.ack('wake-%d' % wake, device=DEVICE_B)
        self.assertEqual(self.notified(wake), 0)

    def test_batch_with_one_foreign_id_changes_nothing(self):
        mine = self.add_message('mine')
        self.lease(device=DEVICE_A)
        with self.assertRaises(wake_push.AckRefused):
            self.ack('wake-%d' % mine, 'wake-424242')
        self.assertEqual(self.notified(mine), 0)

    def state(self, wake_id):
        conn = self.open_conn()
        try:
            return tuple(conn.execute("SELECT notified, push_disposition FROM wake_log WHERE id=?",
                                      (wake_id,)).fetchone())
        finally:
            conn.close()

    def test_lost_ack_is_recovered_by_redelivery_and_an_explicit_ack(self):
        wake = self.add_message('晚安')
        self.lease()
        # Shown, ack lost, phone reboots. Nothing is inferred; the lease runs out,
        # the message is handed out again, the ledger sees it was shown and acks it.
        self.assertEqual(self.state(wake), (0, None))
        again = self.lease(now=self.now + wake_push.LEASE_S + 1)
        self.assertEqual(again['id'], 'wake-%d' % wake)
        self.assertEqual(self.ack(again['id']), {again['id']: 'acked'})
        self.assertEqual(self.state(wake), (1, 'acked'))

    def test_in_flight_older_message_is_not_lost_when_a_newer_one_is_acked_first(self):
        # Review case: wake-10 leased but not yet shown; wake-11 shown and acked
        # first (worker and long poll racing); the next poll still carries since=11.
        w10 = self.add_message('ten')
        self.assertEqual(self.lease()['id'], 'wake-%d' % w10)
        w11 = self.add_message('eleven')
        self.assertEqual(self.lease(now=self.now + 1)['id'], 'wake-%d' % w11)
        self.ack('wake-%d' % w11, now=self.now + 2)
        conn = self.open_conn()
        try:
            wake_push.lease_pending_payload(conn, fetch_row, False, self.now + 3, DEVICE_A)
        finally:
            conn.close()
        # since is gone, and the live lease keeps wake-10 out of the supersede sweep.
        self.assertEqual(self.state(w10), (0, None))
        self.assertEqual(self.ack('wake-%d' % w10, now=self.now + 4), {'wake-%d' % w10: 'acked'})
        self.assertEqual(self.state(w10), (1, 'acked'))

    def lease_row(self, wake_id):
        conn = self.open_conn()
        try:
            return tuple(conn.execute("SELECT push_lease_until, push_lease_device FROM wake_log WHERE id=?",
                                      (wake_id,)).fetchone())
        finally:
            conn.close()

    def overtaken_in_flight(self):
        """device A leases old, then leases and acks newer, all within old's lease."""
        old = self.add_message('old')
        self.assertEqual(self.lease()['id'], 'wake-%d' % old)               # 1
        newer = self.add_message('newer')
        self.assertEqual(self.lease(now=self.now + 1)['id'], 'wake-%d' % newer)  # 2
        self.assertEqual(self.ack('wake-%d' % newer, now=self.now + 2),
                         {'wake-%d' % newer: 'acked'})                       # 3
        return old, newer

    def test_overtaken_in_flight_row_is_not_retaken_by_the_same_device(self):
        old, _newer = self.overtaken_in_flight()
        lease_before = self.lease_row(old)
        self.assertIsNone(self.lease(now=self.now + 10))                     # 4, 5: not returned
        self.assertEqual(self.lease_row(old), lease_before)                  # 5: lease not renewed
        self.assertEqual(self.state(old), (0, None))                         # 5: still pending
        self.assertEqual(self.ack('wake-%d' % old, now=self.now + 20),      # 6: late ack in lease
                         {'wake-%d' % old: 'acked'})
        self.assertEqual(self.state(old), (1, 'acked'))

    def test_overtaken_in_flight_row_without_ack_is_superseded_when_its_lease_ends(self):
        old, _newer = self.overtaken_in_flight()                             # 7
        self.assertIsNone(self.lease(now=self.now + 10))
        self.assertIsNone(self.lease(now=self.now + wake_push.LEASE_S + 1))
        self.assertEqual(self.state(old), (2, 'superseded'))

    def test_overtaken_in_flight_row_is_not_handed_to_another_device_either(self):
        old, _newer = self.overtaken_in_flight()
        self.assertIsNone(self.lease(now=self.now + 10, device=DEVICE_B))
        self.assertEqual(self.state(old), (0, None))

    def test_plain_lost_response_retake_still_works_without_a_newer_ack(self):
        only = self.add_message('only')
        self.lease()
        self.assertEqual(self.lease(now=self.now + 5)['id'], 'wake-%d' % only)

    def test_late_ack_of_a_message_superseded_meanwhile_keeps_it_superseded(self):
        w10 = self.add_message('ten')
        self.lease()                                   # handed to this device...
        later = self.now + wake_push.LEASE_S + 1       # ...lease ran out unacked
        w11 = self.add_message('eleven')
        self.assertEqual(self.lease(now=later)['id'], 'wake-%d' % w11)
        self.ack('wake-%d' % w11, now=later)
        self.assertEqual(self.state(w10), (2, 'superseded'))
        result = self.ack('wake-%d' % w10, now=later + 1)  # the late ack arrives
        self.assertEqual(result, {'wake-%d' % w10: 'superseded'})
        self.assertEqual(self.state(w10), (2, 'superseded'))
        self.assertEqual(self.ack('wake-%d' % w11, now=later + 2), {'wake-%d' % w11: 'acked'})
        self.assertEqual(self.state(w11), (1, 'acked'))

    def test_in_flight_message_whose_lease_expires_is_superseded_not_pushed_late(self):
        w10 = self.add_message('ten')
        self.lease()
        w11 = self.add_message('eleven')
        self.lease(now=self.now + 1)
        self.ack('wake-%d' % w11, now=self.now + 2)
        self.assertEqual(self.state(w10), (0, None))           # still in flight
        self.assertIsNone(self.lease(now=self.now + wake_push.LEASE_S + 5))
        self.assertEqual(self.state(w10), (2, 'superseded'))   # never pushed after wake-11

    def test_executor_audit_rows_do_not_supersede_pending_messages(self):
        pending = self.add_message('real message')
        conn = self.open_conn()
        conn.execute("INSERT INTO wake_log (action, content, woke_at, notified) "
                     "VALUES ('message', 'stale audit row', 'now', 1)")
        conn.commit()
        conn.close()
        self.assertEqual(self.lease()['id'], 'wake-%d' % pending)

    def test_two_devices_requesting_at_once_get_one_lease(self):
        wake = self.add_message('晚安')
        barrier = threading.Barrier(2)
        results = []

        def claim(device):
            conn = self.open_conn()
            try:
                barrier.wait()
                results.append(wake_push.lease_pending_payload(conn, fetch_row, False, self.now, device))
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(d,)) for d in (DEVICE_A, DEVICE_B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        delivered = [r for r in results if r is not None]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['id'], 'wake-%d' % wake)

    def test_concurrency_stress_never_double_leases(self):
        ids = [self.add_message('m%d' % i) for i in range(5)]
        handed = []
        lock = threading.Lock()

        def worker(device):
            for _ in range(20):
                conn = self.open_conn()
                try:
                    payload = wake_push.lease_pending_payload(conn, fetch_row, False, self.now, device)
                finally:
                    conn.close()
                if payload:
                    with lock:
                        handed.append((device, payload['id']))

        devices = ['%08d-0000-4000-8000-%012d' % (i, i) for i in range(6)]
        threads = [threading.Thread(target=worker, args=(d,)) for d in devices]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # Latest-only, one lease window: exactly one device ever holds it
        # (it may re-take its own lease; no other device gets it).
        self.assertEqual({wake for _, wake in handed}, {'wake-%d' % ids[-1]})
        self.assertEqual(len({device for device, _ in handed}), 1)

    def test_screenshot_only_for_clients_that_support_it(self):
        cap = self.add_screenshot()
        self.assertIsNone(self.lease())
        self.assertEqual(self.screenshot_status(cap), 'pending')
        payload = self.lease(screenshot=True)
        self.assertEqual(payload['command_id'], 'cap-%d' % cap)
        with self.assertRaises(wake_push.AckRefused):
            self.ack('cap-%d' % cap, device=DEVICE_B)
        self.assertEqual(self.screenshot_status(cap), 'dispatched')
        self.assertEqual(self.ack('cap-%d' % cap), {'cap-%d' % cap: 'acked'})
        self.assertEqual(self.screenshot_status(cap), 'received')
        self.assertEqual(self.ack('cap-%d' % cap), {'cap-%d' % cap: 'acked'})
        self.assertEqual(self.screenshot_status(cap), 'received')

    def test_unacked_screenshot_is_redispatched_after_two_minutes(self):
        cap = self.add_screenshot()
        self.lease(screenshot=True)
        conn = self.open_conn()
        conn.execute("UPDATE screenshot_requests SET dispatched_at=datetime('now','+8 hours','-3 minutes') WHERE id=?",
                     (cap,))
        conn.commit()
        conn.close()
        self.assertEqual(self.lease(screenshot=True)['command_id'], 'cap-%d' % cap)


class WaitTests(Base):
    def run_poll(self, wait_s, clock):
        def take(conn):
            return wake_push.lease_pending_payload(conn, fetch_row, False, self.now, DEVICE_A)
        return wake_push.poll(self.counting_open, take, wait_s, sleep=clock.sleep, monotonic=clock.monotonic)

    def test_without_wait_answers_immediately(self):
        clock = FakeClock()
        self.assertEqual(self.run_poll(0, clock), {'has_message': False})
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.opened, 1)

    def test_message_arriving_during_wait_is_delivered_at_once(self):
        clock = FakeClock()
        clock.on_sleep = lambda n: self.add_message('during wait') if n == 3 else None
        payload = self.run_poll(25, clock)
        self.assertEqual(payload['content'], 'during wait')
        self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])
        self.assertEqual(self.opened, 4)

    def test_wait_times_out_empty(self):
        clock = FakeClock()
        self.assertEqual(self.run_poll(3, clock), {'has_message': False})
        self.assertEqual(sum(clock.sleeps), 3.0)


if __name__ == '__main__':
    unittest.main()
