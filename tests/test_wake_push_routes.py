"""Route-level auth for the ack push protocol, against the real Health Bridge credential store."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import health_store
from wake_push_routes import create_wake_push_blueprint

PKG = 'xyz.lovestyle.home.canary'


def fetch_row(conn):
    return conn.execute(
        "SELECT id, content, woke_at FROM wake_log "
        "WHERE action='message' AND (notified IS NULL OR notified=0) "
        "ORDER BY id DESC LIMIT 1",
    ).fetchone()


class WakePushRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'memories.db')
        self.health_db = os.path.join(self.tmp.name, 'health.db')
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE wake_log (id INTEGER PRIMARY KEY, action TEXT, content TEXT,
                                   woke_at TEXT, notified INTEGER DEFAULT 0);
            CREATE TABLE screenshot_requests (id INTEGER PRIMARY KEY, status TEXT,
                                              dispatched_at TEXT);
        """)
        conn.commit()
        conn.close()
        self.a = self.enroll('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'phone A')
        self.b = self.enroll('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', 'phone B')
        self.secrets = [self.a['credential'], self.b['credential']]

        app = Flask(__name__)
        app.register_blueprint(create_wake_push_blueprint(
            self.open_db, fetch_row, lambda: False, self.health_db))
        self.client = app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def enroll(self, install_id, label):
        return health_store.enroll_device({
            'schemaVersion': health_store.SCHEMA_VERSION,
            'installId': install_id,
            'packageName': PKG,
            'label': label,
        }, self.health_db)

    def open_db(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def headers(self, device, credential=None):
        return {
            'X-Health-Device-ID': device['deviceId'],
            'Authorization': 'Bearer ' + (device['credential'] if credential is None else credential),
        }

    def add_message(self, text='晚安'):
        conn = self.open_db()
        cur = conn.execute("INSERT INTO wake_log (action, content, woke_at) VALUES ('message', ?, 'now')", (text,))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def add_screenshot(self):
        conn = self.open_db()
        cur = conn.execute("INSERT INTO screenshot_requests (status) VALUES ('pending')")
        conn.commit()
        conn.close()
        return cur.lastrowid

    def wake_state(self, wake_id):
        conn = self.open_db()
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(wake_log)')}
            row = conn.execute('SELECT * FROM wake_log WHERE id=?', (wake_id,)).fetchone()
            return (row['notified'],
                    row['push_lease_until'] if 'push_lease_until' in cols else None,
                    row['push_lease_device'] if 'push_lease_device' in cols else None)
        finally:
            conn.close()

    def cap_state(self, cap_id):
        conn = self.open_db()
        try:
            cols = {r[1] for r in conn.execute('PRAGMA table_info(screenshot_requests)')}
            row = conn.execute('SELECT * FROM screenshot_requests WHERE id=?', (cap_id,)).fetchone()
            return (row['status'], row['dispatched_device'] if 'dispatched_device' in cols else None)
        finally:
            conn.close()

    def get(self, headers=None, query='protocol=ack'):
        return self.client.get('/api/wake_log/pending_notification?' + query, headers=headers or {})

    def post_ack(self, ids, headers=None):
        return self.client.post('/api/wake_log/ack', json={'ids': ids}, headers=headers or {})

    def assert_no_secret(self, response):
        blob = response.get_data(as_text=True) + repr(dict(response.headers))
        for secret in self.secrets:
            self.assertNotIn(secret, blob)

    # ── the required cases ────────────────────────────────────
    def test_no_authorization(self):
        wake = self.add_message()
        for response in (self.get(), self.get({'X-Health-Device-ID': self.a['deviceId']}),
                         self.post_ack(['wake-%d' % wake])):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers.get('WWW-Authenticate'), 'Bearer')
            self.assert_no_secret(response)
        self.assertEqual(self.wake_state(wake), (0, None, None))

    def test_wrong_credential(self):
        wake = self.add_message()
        bad = [
            self.headers(self.a, credential='not-the-credential'),
            self.headers(self.a, credential=self.b['credential']),  # B's secret with A's id
            {'X-Health-Device-ID': 'not-a-uuid', 'Authorization': 'Bearer ' + self.a['credential']},
            {'X-Health-Device-ID': self.a['deviceId'], 'Authorization': 'Basic ' + self.a['credential']},
        ]
        for headers in bad:
            for response in (self.get(headers), self.post_ack(['wake-%d' % wake], headers)):
                self.assertEqual(response.status_code, 401)
                self.assert_no_secret(response)
        self.assertEqual(self.wake_state(wake), (0, None, None))

    def test_correct_credential_lease_then_ack(self):
        wake = self.add_message()
        response = self.get(self.headers(self.a))
        self.assertEqual(response.status_code, 200)
        self.assert_no_secret(response)
        body = response.get_json()
        self.assertEqual((body['id'], body['protocol']), ('wake-%d' % wake, 'ack'))
        notified, lease_until, lease_device = self.wake_state(wake)
        self.assertEqual((notified, lease_device), (0, self.a['deviceId']))
        self.assertIsNotNone(lease_until)

        ack = self.post_ack([body['id']], self.headers(self.a))
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.get_json()['results'], {body['id']: 'acked'})
        self.assert_no_secret(ack)
        again = self.post_ack([body['id']], self.headers(self.a))
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.wake_state(wake)[0], 1)

    def test_device_a_claims_device_b_acks(self):
        wake = self.add_message()
        self.assertEqual(self.get(self.headers(self.a)).get_json()['id'], 'wake-%d' % wake)
        before = self.wake_state(wake)
        response = self.post_ack(['wake-%d' % wake], self.headers(self.b))
        self.assertEqual(response.status_code, 403)
        self.assert_no_secret(response)
        self.assertEqual(self.wake_state(wake), before)
        # B also cannot take A's live lease.
        self.assertEqual(self.get(self.headers(self.b)).get_json(), {'has_message': False})

    def test_forged_ack_of_unclaimed_wake_id(self):
        wake = self.add_message()
        for ids in (['wake-%d' % wake], ['wake-99999'], ['cap-1'], ['garbage']):
            response = self.post_ack(ids, self.headers(self.a))
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.wake_state(wake), (0, None, None))
        # and it is still deliverable afterwards
        self.assertEqual(self.get(self.headers(self.a)).get_json()['id'], 'wake-%d' % wake)

    def test_unauthenticated_flood_never_takes_the_lease(self):
        wake = self.add_message()
        started = time.monotonic()
        for _ in range(30):
            self.assertEqual(self.get(query='protocol=ack&wait=25').status_code, 401)
            self.assertEqual(self.get(self.headers(self.a, 'wrong'), 'protocol=ack&wait=25').status_code, 401)
        self.assertLess(time.monotonic() - started, 5, 'unauthenticated requests must not be held open')
        self.assertEqual(self.wake_state(wake), (0, None, None))
        self.assertEqual(self.get(self.headers(self.a)).get_json()['id'], 'wake-%d' % wake)

    def test_since_from_older_builds_never_acks_anything(self):
        w10 = self.add_message('ten')
        self.assertEqual(self.get(self.headers(self.a)).get_json()['id'], 'wake-%d' % w10)
        w11 = self.add_message('eleven')
        self.get(self.headers(self.a), 'protocol=ack&since=%d' % (w11 + 100))
        self.assertEqual(self.wake_state(w10)[0], 0)
        self.assertEqual(self.wake_state(w11)[0], 0)

    # ── commands, rotation, compatibility ─────────────────────
    def test_screenshot_commands_are_retired(self):
        cap = self.add_screenshot()
        self.assertEqual(self.get(query='protocol=ack&commands=screenshot').status_code, 401)
        self.assertEqual(self.get(self.headers(self.a), 'protocol=ack&commands=screenshot').get_json(),
                         {'has_message': False})
        self.assertEqual(self.cap_state(cap), ('pending', None))
        self.assertEqual(self.post_ack(['cap-%d' % cap], self.headers(self.a)).status_code, 403)
        self.assertEqual(self.cap_state(cap), ('pending', None))
        wake = self.add_message()
        body = self.get(self.headers(self.a), 'protocol=ack&commands=screenshot').get_json()
        self.assertEqual(body['id'], 'wake-%d' % wake)
        self.assertNotIn('command_id', body)

    def test_reenrolled_device_old_credential_stops_working(self):
        old = dict(self.a)
        self.a = self.enroll('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'phone A again')
        self.secrets.append(self.a['credential'])
        self.assertEqual(self.a['deviceId'], old['deviceId'])
        self.assertEqual(self.get(self.headers(old)).status_code, 401)
        self.assertEqual(self.get(self.headers(self.a)).status_code, 200)

    def test_legacy_protocol_is_retired_and_changes_nothing(self):
        wake = self.add_message('would have been consumed')
        cap = self.add_screenshot()
        for query in ('', 'wait=25', 'protocol=legacy'):
            started = time.monotonic()
            response = self.get(query=query)
            self.assertEqual(response.status_code, 410)
            self.assertLess(time.monotonic() - started, 2)
        # even with a valid credential, no protocol means no delivery
        self.assertEqual(self.get(self.headers(self.a), query='').status_code, 410)
        self.assertEqual(self.wake_state(wake), (0, None, None))
        self.assertEqual(self.cap_state(cap), ('pending', None))
        self.assertEqual(self.get(self.headers(self.a)).get_json()['id'], 'wake-%d' % wake)


if __name__ == '__main__':
    unittest.main()


class CallDeltaRouteTests(WakePushRouteTests):
    """The same suite with call support on (proves the message protocol is unchanged),
    plus the call delta: omitted = leave it, object = show it, null = withdraw it."""

    def setUp(self):
        super().setUp()
        self.ringing = None
        app = Flask(__name__)
        app.register_blueprint(create_wake_push_blueprint(
            self.open_db, fetch_row, lambda: False, self.health_db,
            incoming_call=lambda: self.ringing))
        self.client = app.test_client()

    def poll(self, known=None, wait=0):
        query = 'protocol=ack&wait=%d' % wait + ('&known_call=%s' % known if known else '')
        return self.get(self.headers(self.a), query).get_json()

    def test_a_new_ringing_call_is_announced_at_once(self):
        self.ringing = 'abc123'
        started = time.monotonic()
        body = self.poll(wait=25)
        self.assertLess(time.monotonic() - started, 5, 'does not wait out the long poll')
        self.assertEqual(body, {'has_message': False, 'call': {'id': 'abc123', 'title': '费佳来电'}})

    def test_a_call_the_phone_already_shows_is_left_alone(self):
        self.ringing = 'abc123'
        self.assertEqual(self.poll(known='abc123'), {'has_message': False})
        started = time.monotonic()
        self.assertEqual(self.poll(known='abc123', wait=1), {'has_message': False})
        self.assertGreaterEqual(time.monotonic() - started, 0.9, 'no hot loop on an unchanged call')

    def test_a_call_that_stopped_ringing_is_withdrawn(self):
        self.ringing = None
        self.assertEqual(self.poll(known='abc123'), {'has_message': False, 'call': None})
        self.assertEqual(self.poll(), {'has_message': False}, 'nothing known, nothing ringing')

    def test_a_different_call_replaces_the_known_one(self):
        self.ringing = 'def456'
        self.assertEqual(self.poll(known='abc123')['call']['id'], 'def456')

    def test_message_and_call_delta_travel_together_and_only_the_message_is_acked(self):
        wake = self.add_message('来电前的消息')
        self.ringing = 'abc123'
        body = self.poll()
        self.assertTrue(body['has_message'])
        self.assertEqual(body['id'], 'wake-%d' % wake)
        self.assertEqual(body['call'], {'id': 'abc123', 'title': '费佳来电'})
        self.assertEqual(self.post_ack(['wake-%d' % wake], self.headers(self.a)).status_code, 200)
        self.assertEqual(self.post_ack(['abc123'], self.headers(self.a)).status_code, 403,
                         'a call is never acked')

    def test_withdrawal_is_not_held_back_by_a_message(self):
        wake = self.add_message('挂断时的消息')
        self.ringing = None
        body = self.poll(known='abc123')
        self.assertEqual((body['id'], body['call']), ('wake-%d' % wake, None))

    def test_a_call_delta_never_touches_message_lease_state(self):
        wake = self.add_message()
        self.ringing = 'abc123'
        before = self.wake_state(wake)
        self.poll(known='abc123')  # message leased as before
        leased = self.wake_state(wake)
        self.assertEqual(leased[0], before[0])
        self.assertEqual(leased[2], self.a['deviceId'])

    def test_a_malformed_known_call_counts_as_none(self):
        self.ringing = 'abc123'
        self.assertEqual(self.poll(known='x%27%3B--')['call']['id'], 'abc123')


class CalendarFlagRouteTests(WakePushRouteTests):
    """The same suite with the calendar flag wired (message protocol unchanged), plus
    ``calendar: true`` for the device that has an unfetched calendar write."""

    def setUp(self):
        super().setUp()
        self.pending_for = set()
        app = Flask(__name__)
        app.register_blueprint(create_wake_push_blueprint(
            self.open_db, fetch_row, lambda: False, self.health_db,
            calendar_pending=lambda device_id: device_id in self.pending_for))
        self.client = app.test_client()

    def poll(self, device, wait=0):
        return self.get(self.headers(device), 'protocol=ack&wait=%d' % wait).get_json()

    def test_pending_calendar_write_returns_at_once_for_that_device_only(self):
        self.pending_for.add(self.a['deviceId'])
        started = time.monotonic()
        self.assertEqual(self.poll(self.a, wait=25), {'has_message': False, 'calendar': True})
        self.assertLess(time.monotonic() - started, 5, 'does not wait out the long poll')
        self.assertEqual(self.poll(self.b), {'has_message': False})

    def test_flag_rides_beside_a_message(self):
        wake = self.add_message('日历和消息')
        self.pending_for.add(self.a['deviceId'])
        body = self.poll(self.a)
        self.assertEqual((body['id'], body['calendar']), ('wake-%d' % wake, True))
