import os
import sqlite3
import sys
import tempfile
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


class WakePushTests(unittest.TestCase):
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

    def tearDown(self):
        os.unlink(self.path)

    def open_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def counting_open(self):
        self.opened += 1
        return self.open_conn()

    def add_message(self, content):
        conn = self.open_conn()
        conn.execute("INSERT INTO wake_log (action, content, woke_at) VALUES ('message', ?, '2026-10-23 01:00:00')",
                     (content,))
        conn.commit()
        conn.close()

    def poll(self, wait_s, clock):
        return wake_push.poll(self.counting_open, fetch_row, lambda: False, wait_s,
                              sleep=clock.sleep, monotonic=clock.monotonic)

    def test_parse_wait_clamps_and_rejects_garbage(self):
        self.assertEqual(wake_push.parse_wait(None), 0)
        self.assertEqual(wake_push.parse_wait('abc'), 0)
        self.assertEqual(wake_push.parse_wait('-5'), 0)
        self.assertEqual(wake_push.parse_wait('10'), 10)
        self.assertEqual(wake_push.parse_wait('999'), wake_push.MAX_WAIT_S)

    def test_without_wait_answers_immediately(self):
        clock = FakeClock()
        self.assertEqual(self.poll(0, clock), {'has_message': False})
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(self.opened, 1)

    def test_pending_message_is_returned_and_marked(self):
        self.add_message('older')
        self.add_message('latest')
        clock = FakeClock()
        payload = self.poll(25, clock)
        self.assertEqual(payload['content'], 'latest')
        self.assertTrue(payload['has_message'])
        self.assertEqual(payload['id'], 'wake-2')
        self.assertEqual(clock.sleeps, [])
        # Both pending rows are marked so the 15-minute worker cannot repeat them.
        self.assertEqual(self.poll(0, FakeClock()), {'has_message': False})

    def test_message_arriving_during_wait_is_delivered(self):
        clock = FakeClock()
        clock.on_sleep = lambda n: self.add_message('during wait') if n == 3 else None
        payload = self.poll(25, clock)
        self.assertEqual(payload['content'], 'during wait')
        self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])
        self.assertEqual(self.opened, 4)

    def test_wait_times_out_empty(self):
        clock = FakeClock()
        self.assertEqual(self.poll(3, clock), {'has_message': False})
        self.assertEqual(sum(clock.sleeps), 3.0)

    def test_screenshot_command_is_dispatched(self):
        conn = self.open_conn()
        conn.execute("INSERT INTO screenshot_requests (status) VALUES ('pending')")
        conn.commit()
        conn.close()
        payload = self.poll(25, FakeClock())
        self.assertEqual(payload, {'has_message': False, 'command': 'screenshot', 'command_id': 'cap-1'})
        conn = self.open_conn()
        status = conn.execute("SELECT status FROM screenshot_requests").fetchone()['status']
        conn.close()
        self.assertEqual(status, 'dispatched')


if __name__ == '__main__':
    unittest.main()
