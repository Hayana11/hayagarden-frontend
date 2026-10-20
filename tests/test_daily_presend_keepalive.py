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

    def test_unsent_disconnect_keeps_hot_resident_and_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'long hot input', '2026-07-27 10:00:00')
            resident = _BlockingAfterTextResident()
            seen = {}

            def _slow_prepare(**_kwargs):
                with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    plan = _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid,
                        db_path=db,
                        resident=resident,
                        static_system='S',
                        wall_now=_FIXED_NOW,
                    )
                # A live hot resident is bound to exactly this turn's key.
                dr.set_local_binding(dr.LocalResidentBinding(
                    resident_key=plan.resident_key,
                    context_id=plan.context_id,
                    context_epoch=plan.context_epoch,
                    resident_generation=plan.resident_generation,
                    bound_cursor_message_id=None,
                    process_generation=1,
                    tool_profile=plan.tool_profile,
                ))
                seen['generation'] = plan.resident_generation
                time.sleep(0.2)
                return plan

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch.object(gateway, 'DB_PATH', db), \
                 mock.patch.object(gateway, 'DAILY_PRESEND_KEEPALIVE_SEC', 0.02), \
                 mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                     'persona': 'P', 'full_system': 'STATIC',
                 }), \
                 mock.patch.object(dr, 'prepare_daily_turn', side_effect=_slow_prepare), \
                 mock.patch.object(gateway, '_CC_RESIDENT', resident):
                gen = gateway._stream_cc_daily_soft_window({'user_message_id': uid}, 'x')
                self.assertEqual(_sse_types([next(gen)]), ['ping'])
                gen.close()

            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            self.assertEqual(resident.killed, 0)
            self.assertIsNotNone(dr.get_local_binding())
            self.assertEqual(int(ctx['resident_generation']), seen['generation'])
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), seen['generation'], db_path=db, now=_FIXED_NOW,
            ))
        finally:
            dr.set_local_binding(None)
            os.unlink(db)


class ReleaseUnsentDailyTurnTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def _plan(self, db):
        _init_chat_messages(db)
        uid = _insert(db, 'hayana', 'unsent', '2026-07-27 10:00:00')
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            return _REAL_PREPARE_DAILY_TURN(
                user_message_id=uid,
                db_path=db,
                resident=_BlockingAfterTextResident(),
                static_system='S',
                wall_now=_FIXED_NOW,
            )

    def test_fresh_plan_releases_lease_only(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            out = dr.release_unsent_daily_turn(plan, error_code='client_stream_cancelled_unsent')
            self.assertIsNotNone(out)
            self.assertTrue(out['unsent_release'])
            self.assertTrue(plan.lease_released)
            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            self.assertEqual(int(ctx['resident_generation']), plan.resident_generation)
        finally:
            os.unlink(db)

    def test_refuses_once_resident_path_may_have_run(self):
        markers = {
            '_resident_stream_entered': True,
            '_current_user_stdin_flushed': True,
            '_capacity_swap_install_state': {'old_proc': None},
            '_capacity_swap_deferred_old_proc': object(),
        }
        for attr, value in markers.items():
            with self.subTest(marker=attr):
                db = _tmp_db()
                try:
                    plan = self._plan(db)
                    setattr(plan, attr, value)
                    self.assertIsNone(dr.release_unsent_daily_turn(plan, error_code='x'))
                    self.assertFalse(plan.lease_released)
                    dr._release_lease(plan)
                finally:
                    os.unlink(db)

    def test_stream_entry_marks_plan(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            events = dr.ensure_resident_and_stream(
                plan, resident=_BlockingAfterTextResident(), env={}, static_system='S',
            )
            self.assertTrue(dr.is_unsent_daily_turn(plan))  # generator not started
            try:
                next(events)
            except Exception:
                pass
            finally:
                events.close()
            self.assertFalse(dr.is_unsent_daily_turn(plan))
            dr._release_lease(plan)
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
