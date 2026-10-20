"""Daily pre-send keepalive: cold prepare must not leave the stream silent."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock

import config_store
import gateway
from chat import daily_context as dc
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    _FIXED_NOW,
    _REAL_PREPARE_DAILY_TURN,
    _BlockingAfterTextResident,
    _init_chat_messages,
    _insert,
    _tmp_db,
)


def _sse_types(chunks):
    out = []
    for chunk in chunks:
        line = chunk.strip()
        if line.startswith('data: '):
            out.append(json.loads(line[6:]).get('t'))
    return out


def _drive(gen):
    """Collect yielded chunks and the sub-generator return value."""
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        return chunks, stop.value


class CallWithSseKeepaliveTests(unittest.TestCase):
    def test_fast_call_emits_nothing(self):
        chunks, value = _drive(gateway._call_with_sse_keepalive(lambda: 42, interval=1.0))
        self.assertEqual(value, 42)
        self.assertEqual(chunks, [])

    def test_slow_call_pings_until_done(self):
        release = threading.Event()

        def slow():
            release.wait(5)
            return 'plan'

        gen = gateway._call_with_sse_keepalive(slow, interval=0.02)
        chunks = [next(gen), next(gen), next(gen)]
        release.set()
        more, value = _drive(gen)
        self.assertEqual(value, 'plan')
        self.assertEqual(set(_sse_types(chunks + more)), {'ping'})
        self.assertGreaterEqual(len(chunks + more), 3)

    def test_worker_error_is_reraised_on_generator_thread(self):
        class Boom(Exception):
            pass

        def bad():
            raise Boom('prepare failed')

        with self.assertRaises(Boom):
            _drive(gateway._call_with_sse_keepalive(bad, interval=0.01))

    def test_client_close_waits_for_worker_before_cleanup(self):
        prepared = {}
        started = threading.Event()

        def prepare():
            started.set()
            time.sleep(0.15)
            prepared['plan'] = 'lease-holding-plan'

        seen_at_cleanup = []

        def stream():
            try:
                yield from gateway._call_with_sse_keepalive(prepare, interval=0.01)
            finally:
                seen_at_cleanup.append(prepared.get('plan'))

        gen = stream()
        next(gen)
        started.wait(1)
        gen.close()  # client disconnect mid-prepare
        self.assertEqual(seen_at_cleanup, ['lease-holding-plan'])


class DailyStreamSlowPrepareTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_slow_prepare_pings_and_disconnect_releases_lease(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'slow-cold', '2026-07-27 10:00:00')
            resident = _BlockingAfterTextResident()

            def _slow_prepare(**_kwargs):
                time.sleep(0.2)
                with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    return _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid,
                        db_path=db,
                        resident=resident,
                        static_system='S',
                        wall_now=_FIXED_NOW,
                    )

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch.object(gateway, 'DB_PATH', db), \
                 mock.patch.object(gateway, 'DAILY_PRESEND_KEEPALIVE_SEC', 0.02), \
                 mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                     'persona': 'P', 'full_system': 'STATIC',
                 }), \
                 mock.patch.object(dr, 'prepare_daily_turn', side_effect=_slow_prepare), \
                 mock.patch.object(gateway, '_CC_RESIDENT', resident):
                gen = gateway._stream_cc_daily_soft_window({'user_message_id': uid}, 'slow-cold')
                first = next(gen)
                self.assertEqual(_sse_types([first]), ['ping'])
                gen.close()  # proxy/SPA drops the stream while cold prepare runs

            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            self.assertIsNotNone(ctx)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), int(ctx['resident_generation']), db_path=db, now=_FIXED_NOW,
            ))
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
