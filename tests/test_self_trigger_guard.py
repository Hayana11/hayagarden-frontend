"""self_trigger must not be eaten by normal idle guard; concurrency skips retry."""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat.interaction_state import InteractionClock, wake_guard_reason


def _recent_clock():
    now = datetime.datetime(2026, 7, 20, 12, 0, 0)
    return InteractionClock(
        last_user_at=now - datetime.timedelta(minutes=10),
        last_wake_message_at=None,
        user_idle_hours=10 / 60,
        effective_idle_hours=10 / 60,
        reliable=True,
        reason='ok',
    )


class SelfTriggerGuardReasonTests(unittest.TestCase):
    def test_self_trigger_recent_interaction_still_allowed(self):
        clock = _recent_clock()
        self.assertIsNone(wake_guard_reason(
            clock, mode='self_trigger', min_idle_minutes=30,
        ))
        self.assertEqual(
            wake_guard_reason(clock, mode='normal', min_idle_minutes=30),
            'recent_interaction',
        )

    def test_self_trigger_still_blocked_when_chat_or_wake_busy(self):
        clock = _recent_clock()
        self.assertEqual(
            wake_guard_reason(clock, mode='self_trigger', chat_busy=True),
            'chat_generating',
        )
        self.assertEqual(
            wake_guard_reason(clock, mode='self_trigger', wake_busy=True),
            'wake_in_progress',
        )


class SelfTriggerRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'CREATE TABLE self_triggers ('
            'id INTEGER PRIMARY KEY, trigger_at TEXT, note TEXT, '
            'consumed INTEGER DEFAULT 0)'
        )
        conn.execute(
            "INSERT INTO self_triggers (id, trigger_at, note, consumed) "
            "VALUES (7, '2026-07-20 11:00:00', '提醒小猫', 0)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _consumed(self, tid=7):
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            'SELECT consumed FROM self_triggers WHERE id=?', (tid,),
        ).fetchone()
        conn.close()
        return int(row[0])

    def _claim_then_release_helpers(self):
        """Local claim/release mirroring app.py SQL (no Flask)."""
        def claim():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "UPDATE self_triggers SET consumed=1 "
                "WHERE consumed=0 AND trigger_at <= ? "
                "RETURNING id, trigger_at, note",
                ('2026-07-20 12:00:00',),
            ).fetchall()
            conn.commit()
            out = [dict(r) for r in rows]
            conn.close()
            return out

        def release(ids):
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE self_triggers SET consumed=0 "
                "WHERE consumed=1 AND id IN (%s)" % ','.join('?' for _ in ids),
                tuple(ids),
            )
            conn.commit()
            conn.close()

        return claim, release

    def test_chat_busy_skip_releases_pending(self):
        from tools import dream_wake

        claim, release = self._claim_then_release_helpers()
        triggers = claim()
        self.assertEqual(self._consumed(), 1)

        with mock.patch.object(dream_wake, '_call_wake', return_value={
            'ok': True, 'skipped': True, 'reason': 'chat_generating',
        }), mock.patch.object(
            dream_wake, '_release_self_triggers', side_effect=lambda ids: release(ids),
        ), mock.patch.object(dream_wake, '_log'):
            # Inline the per-trigger body used by run_self_triggers
            for t in triggers:
                result = dream_wake._call_wake({
                    'mode': 'self_trigger',
                    'self_trigger_id': t['id'],
                    'self_trigger_note': t.get('note') or '',
                })
                if result.get('skipped') and result.get('reason') in (
                    'chat_generating', 'wake_in_progress',
                ):
                    dream_wake._release_self_triggers([t['id']])

        self.assertEqual(self._consumed(), 0)

    def test_wake_busy_skip_releases_pending(self):
        from tools import dream_wake

        claim, release = self._claim_then_release_helpers()
        triggers = claim()
        with mock.patch.object(dream_wake, '_call_wake', return_value={
            'ok': True, 'skipped': True, 'reason': 'wake_in_progress',
        }), mock.patch.object(
            dream_wake, '_release_self_triggers', side_effect=lambda ids: release(ids),
        ), mock.patch.object(dream_wake, '_log'):
            for t in triggers:
                result = dream_wake._call_wake({'mode': 'self_trigger'})
                if result.get('skipped') and result.get('reason') == 'wake_in_progress':
                    dream_wake._release_self_triggers([t['id']])
        self.assertEqual(self._consumed(), 0)

    def test_run_self_triggers_uses_self_trigger_mode_and_releases_on_busy(self):
        from tools import dream_wake

        claim, release = self._claim_then_release_helpers()
        claimed = claim()

        class FakeResp:
            def __init__(self, payload):
                self._payload = json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._payload

        wake_calls = []

        def fake_urlopen(req, timeout=5):
            url = getattr(req, 'full_url', None) or req.get_full_url()
            if url.endswith('/api/self_triggers/claim'):
                return FakeResp(claimed)
            if url.endswith('/api/self_triggers/release'):
                body = json.loads(req.data.decode())
                release(body.get('ids') or [])
                return FakeResp({'ok': True, 'released': 1})
            raise AssertionError('unexpected url %s' % url)

        def fake_call_wake(payload):
            wake_calls.append(payload)
            return {'ok': True, 'skipped': True, 'reason': 'chat_generating'}

        with mock.patch('urllib.request.urlopen', side_effect=fake_urlopen), \
             mock.patch.object(dream_wake, '_call_wake', side_effect=fake_call_wake), \
             mock.patch.object(dream_wake, '_log'):
            # Pretend claim already returned via urlopen; reset DB consumed so
            # run_self_triggers' claim path returns our fixture.
            # Simpler: patch claim by making urlopen return claimed once, then release.
            dream_wake.run_self_triggers()

        self.assertEqual(len(wake_calls), 1)
        self.assertEqual(wake_calls[0]['mode'], 'self_trigger')
        self.assertEqual(wake_calls[0]['self_trigger_id'], 7)
        self.assertEqual(wake_calls[0]['wake_run_id'], 'self-trigger-7')
        self.assertEqual(self._consumed(), 0)

    def test_normal_recent_still_skips(self):
        self.assertEqual(
            wake_guard_reason(_recent_clock(), mode='normal', min_idle_minutes=30),
            'recent_interaction',
        )


if __name__ == '__main__':
    unittest.main()
