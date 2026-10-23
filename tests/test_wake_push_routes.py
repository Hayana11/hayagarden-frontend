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
    def test_screenshot_only_for_authenticated_declaring_device(self):
        cap = self.add_screenshot()
        self.assertEqual(self.get(query='protocol=ack&commands=screenshot').status_code, 401)
        self.assertEqual(self.get(self.headers(self.a)).get_json(), {'has_message': False})
        self.assertEqual(self.cap_state(cap), ('pending', None))
        body = self.get(self.headers(self.a), 'protocol=ack&commands=screenshot').get_json()
        self.assertEqual(body['command_id'], 'cap-%d' % cap)
        self.assertEqual(self.cap_state(cap), ('dispatched', self.a['deviceId']))
        self.assertEqual(self.post_ack(['cap-%d' % cap], self.headers(self.b)).status_code, 403)
        self.assertEqual(self.cap_state(cap), ('dispatched', self.a['deviceId']))
        self.assertEqual(self.post_ack(['cap-%d' % cap], self.headers(self.a)).status_code, 200)
        self.assertEqual(self.cap_state(cap)[0], 'received')

    def test_reenrolled_device_old_credential_stops_working(self):
        old = dict(self.a)
        self.a = self.enroll('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'phone A again')
        self.secrets.append(self.a['credential'])
        self.assertEqual(self.a['deviceId'], old['deviceId'])
        self.assertEqual(self.get(self.headers(old)).status_code, 401)
        self.assertEqual(self.get(self.headers(self.a)).status_code, 200)

    def test_legacy_protocol_still_serves_installed_clients(self):
        wake = self.add_message('legacy')
        body = self.get(query='').get_json()
        self.assertEqual(body['id'], 'wake-%d' % wake)
        self.assertEqual(self.wake_state(wake)[0], 1)


if __name__ == '__main__':
    unittest.main()
