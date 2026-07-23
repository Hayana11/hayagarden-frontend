"""Stable wake_run_id generation for all scheduler modes."""

from __future__ import annotations

import datetime
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wake import wake_run_id as wrid


class WakeRunIdFormatTests(unittest.TestCase):
    def test_morning_date(self):
        now = datetime.datetime(2026, 7, 23, 8, 50)
        self.assertEqual(wrid.make_morning_wake_run_id(now), 'morning-2026-07-23')

    def test_normal_slot_floors_to_30_minutes(self):
        now = datetime.datetime(2026, 7, 23, 10, 17)
        self.assertEqual(wrid.make_normal_wake_run_id(now), 'normal-2026-07-23-10:00')
        later = datetime.datetime(2026, 7, 23, 10, 47)
        self.assertEqual(wrid.make_normal_wake_run_id(later), 'normal-2026-07-23-10:30')

    def test_nightwatch_slot_floors_to_30_minutes(self):
        now = datetime.datetime(2026, 7, 23, 2, 5)
        self.assertEqual(wrid.make_nightwatch_wake_run_id(now), 'nightwatch-2026-07-23-02:00')

    def test_self_trigger_uses_trigger_id(self):
        self.assertEqual(wrid.make_self_trigger_wake_run_id(7), 'self-trigger-7')

    def test_ritual_type_and_date(self):
        now = datetime.datetime(2026, 12, 21, 3, 0)
        self.assertEqual(wrid.make_ritual_wake_run_id('solstice', now), 'ritual-solstice-2026-12-21')

    def test_same_slot_stable_across_retries(self):
        a = datetime.datetime(2026, 7, 23, 14, 5)
        b = datetime.datetime(2026, 7, 23, 14, 29)
        self.assertEqual(
            wrid.make_normal_wake_run_id(a),
            wrid.make_normal_wake_run_id(b),
        )


class DreamWakePayloadTests(unittest.TestCase):
    def test_normal_wake_passes_stable_run_id(self):
        import tools.dream_wake as dream_wake

        now = datetime.datetime(2026, 7, 23, 14, 22)
        with (
            mock.patch.object(dream_wake, '_now', return_value=now),
            mock.patch.object(dream_wake, 'run_self_triggers', return_value=False),
            mock.patch.object(dream_wake, '_in_active_hours', return_value=True),
            mock.patch.object(dream_wake, '_calc_t_hours', return_value=2.0),
            mock.patch.object(dream_wake._wcfg, 'get_float', side_effect=lambda k, d=None: 30 if k == 'WAKE_MIN_IDLE_MINUTES' else d),
            mock.patch.object(dream_wake, 'random') as rnd,
            mock.patch.object(dream_wake, '_call_wake', return_value={'action': 'none'}) as call_wake,
            mock.patch.object(dream_wake, '_log'),
        ):
            rnd.random.return_value = 0.0
            dream_wake.run()
        call_wake.assert_called_once_with({
            'mode': 'normal',
            'wake_run_id': 'normal-2026-07-23-14:00',
        })

    def test_nightwatch_passes_stable_run_id(self):
        import tools.dream_wake as dream_wake

        now = datetime.datetime(2026, 7, 23, 2, 10)
        with (
            mock.patch.object(dream_wake, '_get_recent_activity', return_value=(True, 'active')),
            mock.patch.object(dream_wake, 'random') as rnd,
            mock.patch.object(dream_wake, '_call_wake', return_value={'action': 'none'}) as call_wake,
            mock.patch.object(dream_wake, '_log'),
        ):
            rnd.random.return_value = 0.0
            dream_wake.run_nightwatch(now)
        call_wake.assert_called_once_with({
            'mode': 'nightwatch',
            'activity_desc': 'active',
            'wake_run_id': 'nightwatch-2026-07-23-02:00',
        })

    def test_daily_ritual_passes_stable_run_id(self):
        import tools.daily_rituals as daily_rituals

        now = datetime.datetime(2026, 12, 21, 3, 0)
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"action":"message"}'

        def fake_urlopen(req, timeout=120):
            captured['body'] = json.loads(req.data.decode())
            return FakeResp()

        with (
            mock.patch.object(daily_rituals, '_now', return_value=now),
            mock.patch('urllib.request.urlopen', side_effect=fake_urlopen),
            mock.patch.object(daily_rituals, '_log'),
        ):
            daily_rituals.run()
        self.assertEqual(captured['body']['wake_run_id'], 'ritual-solstice-2026-12-21')


if __name__ == '__main__':
    unittest.main()
