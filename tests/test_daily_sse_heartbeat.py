"""Focused normal Daily synthetic heartbeat regressions."""
from __future__ import annotations

import json
import os
import sqlite3
import unittest

import cc_resident
import gateway
from chat import daily_runtime as dr
from chat.display_thinking import filter_display_thinking_events
from tests.test_daily_runtime import (
    _FIXED_NOW,
    _FakeResident,
    _init_chat_messages,
    _insert,
    _prepare_turn,
    _tmp_db,
)


class _HeartbeatResident(_FakeResident):
    def __init__(self):
        super().__init__()
        self.idle_heartbeat_sec = None

    def send_turn(
        self,
        content,
        commit_meta=None,
        turn_lease=None,
        idle_heartbeat_sec=None,
    ):
        self.sent.append(str(content))
        self.turn_leases.append(turn_lease)
        self.idle_heartbeat_sec = idle_heartbeat_sec
        yield ('heartbeat', None)
        yield ('heartbeat', None)
        yield ('text', 'daily reply')
        yield ('done', ('daily reply', '', {
            'input_tokens': 3,
            'output_tokens': 5,
        }, {}))


class NormalDailyHeartbeatTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_daily_runtime_requests_ten_second_idle_heartbeat(self):
        db = _tmp_db()
        plan = None
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'long turn', '2026-07-27 10:00:00')
            plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW)
            resident = _HeartbeatResident()

            events = list(dr.stream_daily_resident_turn(
                plan,
                resident=resident,
                env={},
                static_system='STATIC',
            ))

            self.assertEqual(resident.idle_heartbeat_sec, 10.0)
            self.assertEqual(
                [event for event, _payload in events],
                ['heartbeat', 'heartbeat', 'text', 'done'],
            )
            self.assertEqual(
                [payload for event, payload in events if event == 'text'],
                ['daily reply'],
            )
            self.assertEqual(
                [payload for event, payload in events if event in ('think', 'tool_use', 'tool_result')],
                [],
            )

            assistant_id = dr.persist_daily_assistant_for_plan(
                plan,
                content='daily reply',
                thinking='',
                tool_calls='',
                cache_info='',
                choices='',
            )
            row = sqlite3.connect(db).execute(
                'SELECT content, thinking, tool_calls FROM chat_messages WHERE id=?',
                (assistant_id,),
            ).fetchone()
            self.assertEqual(row, ('daily reply', '', ''))
        finally:
            if plan is not None:
                dr._release_lease(plan)
            os.unlink(db)

    def test_display_filter_passes_heartbeat_for_every_mode(self):
        source = [
            ('heartbeat', None),
            ('heartbeat', None),
            ('text', 'reply'),
            ('done', ('reply', '', {}, {})),
        ]
        for mode in ('off', 'native', 'authored', 'auto'):
            with self.subTest(mode=mode):
                events = list(filter_display_thinking_events(source, mode))
                self.assertEqual(events[0:2], source[0:2])
                self.assertEqual(
                    [event for event, _payload in events if event == 'heartbeat'],
                    ['heartbeat', 'heartbeat'],
                )
                self.assertNotIn('heartbeat', [
                    event for event, _payload in events if event in ('text', 'think', 'tool_use', 'tool_result')
                ])


class GatewayDailyHeartbeatTests(unittest.TestCase):
    def test_heartbeat_maps_to_transport_ping_only(self):
        source = [
            ('heartbeat', None),
            ('heartbeat', None),
            ('text', 'reply'),
            ('done', ('reply', '', {}, {})),
        ]
        packets = [
            gateway._daily_heartbeat_sse(event)
            for event, payload in source
            if gateway._daily_heartbeat_sse(event) is not None
        ]
        self.assertEqual(len(packets), 2)
        self.assertEqual(
            [json.loads(packet.split('data: ', 1)[1].strip()) for packet in packets],
            [{'t': 'ping'}, {'t': 'ping'}],
        )
        self.assertIsNone(gateway._daily_heartbeat_sse('text'))
        self.assertIsNone(gateway._daily_heartbeat_sse('done'))


if __name__ == '__main__':
    unittest.main()
