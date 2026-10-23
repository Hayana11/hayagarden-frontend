import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wake_push


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
    def lease(self, since=0, screenshot=False, now=None):
        conn = self.open_conn()
        try:
            return wake_push.lease_pending_payload(
                conn, fetch_row, False, self.now if now is None else now,
                since=since, want_screenshot=screenshot)
        finally:
            conn.close()

    def ack(self, *ids, soft=False):
        conn = self.open_conn()
        try:
            return wake_push.ack(conn, list(ids), soft)
        finally:
            conn.close()


class ParseTests(unittest.TestCase):
    def test_parse_wait_clamps_and_rejects_garbage(self):
        self.assertEqual(wake_push.parse_wait(None), 0)
        self.assertEqual(wake_push.parse_wait('abc'), 0)
        self.assertEqual(wake_push.parse_wait('-5'), 0)
        self.assertEqual(wake_push.parse_wait('10'), 10)
        self.assertEqual(wake_push.parse_wait('999'), wake_push.MAX_WAIT_S)

    def test_parse_since(self):
        self.assertEqual(wake_push.parse_since(None), 0)
        self.assertEqual(wake_push.parse_since('12'), 12)
        self.assertEqual(wake_push.parse_since('wake-12'), 12)
        self.assertEqual(wake_push.parse_since('x'), 0)
        self.assertEqual(wake_push.parse_since('-3'), 0)


class LegacyProtocolTests(Base):
    """Unchanged behaviour for clients that cannot ack (retired shell, installed Canary)."""

    def take(self, conn):
        return wake_push.take_pending_payload(conn, fetch_row, False)

    def test_latest_is_returned_and_all_pending_consumed(self):
        self.add_message('older')
        latest = self.add_message('latest')
        conn = self.open_conn()
        payload = self.take(conn)
        conn.close()
        self.assertEqual(payload['content'], 'latest')
        self.assertEqual(payload['id'], 'wake-%d' % latest)
        conn = self.open_conn()
        self.assertIsNone(self.take(conn))
        conn.close()

    def test_screenshot_command_is_dispatched(self):
        cap = self.add_screenshot()
        conn = self.open_conn()
        payload = self.take(conn)
        conn.close()
        self.assertEqual(payload, {'has_message': False, 'command': 'screenshot', 'command_id': 'cap-%d' % cap})
        self.assertEqual(self.screenshot_status(cap), 'dispatched')


class AckProtocolTests(Base):
    def test_handing_out_does_not_consume(self):
        wake = self.add_message('晚安')
        payload = self.lease()
        self.assertEqual(payload['id'], 'wake-%d' % wake)
        self.assertEqual(payload['protocol'], 'ack')
        self.assertEqual(payload['lease_s'], wake_push.LEASE_S)
        self.assertEqual(self.notified(wake), 0)

    def test_response_lost_before_reaching_phone_is_redelivered_after_lease(self):
        wake = self.add_message('晚安')
        self.assertIsNotNone(self.lease())  # response dropped on the way
        self.assertIsNone(self.lease(now=self.now + wake_push.LEASE_S - 1))
        again = self.lease(now=self.now + wake_push.LEASE_S)
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

    def test_ack_of_latest_retires_older_pending_like_legacy(self):
        older = self.add_message('older')
        latest = self.add_message('latest')
        self.lease()
        self.ack('wake-%d' % latest)
        self.assertEqual(self.notified(older), 1)

    def test_soft_window_ack_only_touches_that_row(self):
        older = self.add_message('other window')
        latest = self.add_message('mine')
        self.ack('wake-%d' % latest, soft=True)
        self.assertEqual(self.notified(older), 0)

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self.ack('wake-999', 'cap-999', 'nonsense', ''),
                         {'wake-999': 'unknown', 'cap-999': 'unknown', 'nonsense': 'unknown', '': 'unknown'})

    def test_phone_restart_cursor_recovers_a_lost_ack(self):
        wake = self.add_message('晚安')
        self.lease()
        # shown, ack lost, phone reboots; first poll after boot carries since=<shown id>
        self.assertIsNone(self.lease(since=wake, now=self.now + wake_push.LEASE_S + 1))
        self.assertEqual(self.notified(wake), 1)

    def test_cursor_never_acks_messages_that_were_never_handed_out(self):
        first = self.add_message('one')
        self.lease()
        second = self.add_message('two, created while the phone was offline')
        # A stale or buggy cursor above an un-handed-out row must not swallow it.
        payload = self.lease(since=second, now=self.now + 1)
        self.assertEqual(self.notified(first), 1)
        self.assertEqual(payload['id'], 'wake-%d' % second)
        self.assertEqual(self.notified(second), 0)

    def test_two_concurrent_requests_claim_one_message_once(self):
        wake = self.add_message('晚安')
        barrier = threading.Barrier(2)
        results = []

        def claim():
            conn = self.open_conn()
            try:
                barrier.wait()
                results.append(wake_push.lease_pending_payload(conn, fetch_row, False, self.now))
            finally:
                conn.close()

        threads = [threading.Thread(target=claim) for _ in range(2)]
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

        def worker():
            for _ in range(20):
                conn = self.open_conn()
                try:
                    payload = wake_push.lease_pending_payload(conn, fetch_row, False, self.now)
                finally:
                    conn.close()
                if payload:
                    with lock:
                        handed.append(payload['id'])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        # Latest-only semantics: within one lease window exactly one handout.
        self.assertEqual(handed, ['wake-%d' % ids[-1]])

    def test_screenshot_only_for_clients_that_support_it(self):
        cap = self.add_screenshot()
        self.assertIsNone(self.lease())
        self.assertEqual(self.screenshot_status(cap), 'pending')
        payload = self.lease(screenshot=True)
        self.assertEqual(payload['command_id'], 'cap-%d' % cap)
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
            return wake_push.lease_pending_payload(conn, fetch_row, False, self.now)
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
