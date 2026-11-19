"""Daily stream: a heartbeat at least every DAILY_PRESEND_KEEPALIVE_SEC until the
first provider content event.

A cold turn is silent for a long time after the first SSE byte: the hot-context
check, resident.ensure_alive (starting Claude) and the stdin write (blocks until
the CLI boots and reads) all run before any provider event. Proxies on the phone
path drop an idle stream after ~40s, and the client then reconnects to a turn that
already ended.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
import unittest
from unittest import mock

import config_store
import gateway
from chat import daily_runtime as dr
from tests.test_daily_runtime import (
    _FIXED_NOW,
    _REAL_PREPARE_DAILY_TURN,
    _FakeResident,
    _init_chat_messages,
    _insert,
    _tmp_db,
)

INTERVAL = 0.02          # stands in for DAILY_PRESEND_KEEPALIVE_SEC (5s)
BOUND = INTERVAL * 25    # one blocked next() must never take longer than this


# ---- the pump on its own ------------------------------------------------------------

class Source:
    """A controllable event source whose steps can block."""

    def __init__(self, script, *, cleanup_delay=0.0):
        self.cleanup_delay = cleanup_delay
        self.script = list(script)       # each: ('event', evt, payload) | ('block', Event) | ('raise', exc)
        self.steps_run = []              # (index, thread ident)
        self.cleanup_thread = []
        self.cleanups = 0
        self.main = threading.get_ident()

    def __iter__(self):
        return self._gen()

    def _gen(self):
        try:
            for index, step in enumerate(self.script):
                self.steps_run.append((index, threading.get_ident()))
                if step[0] == 'block':
                    step[1].wait(10)
                elif step[0] == 'raise':
                    raise step[1]
                else:
                    yield step[1], step[2]
        finally:
            time.sleep(self.cleanup_delay)
            self.cleanups += 1
            self.cleanup_thread.append(threading.get_ident())


def pump(source, interval=INTERVAL):
    return dr._iter_with_presend_heartbeat(iter(source), interval=interval)


def take(gen, n):
    return [next(gen) for _ in range(n)]


class PumpTests(unittest.TestCase):
    def test_fast_source_is_passed_through_unchanged(self):
        script = [('event', 'text', 'a'), ('event', 'text', 'b'), ('event', 'done', ('ab', '', {}, {}))]
        source = Source(script)
        self.assertEqual(
            list(pump(source)),
            [('text', 'a'), ('text', 'b'), ('done', ('ab', '', {}, {}))],
        )
        self.assertEqual(source.cleanups, 1)

    def test_heartbeats_fill_the_silence_before_the_first_event(self):
        release = threading.Event()
        gen = pump(Source([('block', release), ('event', 'text', 'hello'), ('event', 'done', None)]))
        started = time.monotonic()
        beats = []
        while len(beats) < 8:
            t0 = time.monotonic()
            beats.append(next(gen))
            self.assertLess(time.monotonic() - t0, BOUND, 'a blocked step left the stream silent')
        self.assertEqual(set(beats), {('heartbeat', None)})
        self.assertGreater(time.monotonic() - started, INTERVAL * 5)
        release.set()
        rest = list(gen)
        self.assertEqual([e for e in rest if e[0] != 'heartbeat'], [('text', 'hello'), ('done', None)])

    def test_a_source_heartbeat_does_not_end_the_presend_phase(self):
        release = threading.Event()
        gen = pump(Source([('event', 'heartbeat', None), ('block', release), ('event', 'text', 'x')]))
        self.assertEqual(next(gen), ('heartbeat', None))   # the source's own
        beats = take(gen, 4)                               # still bridged while blocked
        self.assertEqual(set(beats), {('heartbeat', None)})
        release.set()
        self.assertEqual([e for e in gen if e[0] != 'heartbeat'], [('text', 'x')])

    def test_no_heartbeats_are_injected_after_the_first_content_event(self):
        release = threading.Event()
        gen = pump(Source([('event', 'text', 'first'), ('block', release), ('event', 'text', 'second')]))
        self.assertEqual(next(gen), ('text', 'first'))
        out = []
        consumer = threading.Thread(target=lambda: out.append(next(gen)), daemon=True)
        consumer.start()
        consumer.join(INTERVAL * 10)
        self.assertTrue(consumer.is_alive(), 'the stream must block as before once content started')
        self.assertEqual(out, [])
        release.set()
        consumer.join(5)
        self.assertEqual(out, [('text', 'second')])

    def test_every_step_runs_once_in_order_on_one_worker_thread(self):
        release = threading.Event()
        source = Source([('block', release), ('event', 'text', 'a'), ('event', 'text', 'b')])
        gen = pump(source)
        take(gen, 3)
        release.set()
        list(gen)
        indexes = [i for i, _ in source.steps_run]
        self.assertEqual(indexes, [0, 1, 2])
        threads = {t for _, t in source.steps_run}
        self.assertEqual(len(threads), 1)
        self.assertNotIn(source.main, threads)
        self.assertEqual(set(source.cleanup_thread), threads, 'cleanup must run on the same thread')

    def test_exceptions_from_the_source_surface_unchanged_after_heartbeats(self):
        class Boom(Exception):
            pass

        error = Boom('ensure_alive failed')
        release = threading.Event()
        gen = pump(Source([('block', release), ('raise', error)]))
        take(gen, 3)
        release.set()
        with self.assertRaises(Boom) as caught:
            list(gen)
        self.assertIs(caught.exception, error)

    def test_an_immediate_exception_surfaces_too(self):
        error = RuntimeError('provider exploded')
        with self.assertRaises(RuntimeError) as caught:
            list(pump(Source([('raise', error)])))
        self.assertIs(caught.exception, error)

    def test_close_while_a_step_is_in_flight_waits_for_it_then_cleans_up(self):
        release = threading.Event()
        source = Source([('block', release), ('event', 'text', 'never consumed')], cleanup_delay=0.15)
        gen = pump(source)
        take(gen, 2)
        closed = threading.Event()
        threading.Thread(target=lambda: (gen.close(), closed.set()), daemon=True).start()
        self.assertFalse(closed.wait(INTERVAL * 10), 'close() must wait for the in-flight step')
        self.assertEqual(source.cleanups, 0)
        release.set()
        self.assertTrue(closed.wait(5))
        # close() returns only once the source's own cleanup has finished
        self.assertEqual(source.cleanups, 1)
        self.assertEqual([i for i, _ in source.steps_run], [0, 1], 'the step ran to completion once')

    def _close_while_blocked_then_release(self, script):
        release = script[0][1]
        source = Source(script)
        gen = pump(source)
        take(gen, 2)
        closed = threading.Event()
        threading.Thread(target=lambda: (gen.close(), closed.set()), daemon=True).start()
        self.assertFalse(closed.wait(INTERVAL * 10))
        release.set()
        self.assertTrue(closed.wait(5), 'close() hung after the last step ended the source')
        return source

    def test_close_when_the_in_flight_step_ends_the_source_does_not_hang(self):
        source = self._close_while_blocked_then_release([('block', threading.Event())])
        self.assertEqual(source.cleanups, 1)

    def test_close_when_the_in_flight_step_raises_does_not_hang(self):
        source = self._close_while_blocked_then_release(
            [('block', threading.Event()), ('raise', RuntimeError('boom'))])
        self.assertEqual(source.cleanups, 1)

    def test_close_after_the_stream_finished_does_not_hang(self):
        source = Source([('event', 'text', 'a')])
        gen = pump(source)
        self.assertEqual(list(gen), [('text', 'a')])
        gen.close()
        self.assertEqual(source.cleanups, 1)

    def test_close_after_an_error_does_not_hang(self):
        gen = pump(Source([('raise', ValueError('x'))]))
        with self.assertRaises(ValueError):
            next(gen)
        gen.close()

    def test_close_at_a_yield_closes_the_source_exactly_once(self):
        source = Source([('event', 'text', 'a'), ('event', 'text', 'b')])
        gen = pump(source)
        self.assertEqual(next(gen), ('text', 'a'))
        gen.close()
        self.assertEqual(source.cleanups, 1)

    def test_interval_zero_or_less_is_handled_by_the_caller_not_the_pump(self):
        # stream_daily_resident_turn skips the pump when the interval is <= 0
        self.assertGreater(dr.DAILY_PRESEND_HEARTBEAT_SEC, 0)
        self.assertEqual(gateway.DAILY_PRESEND_KEEPALIVE_SEC, dr.DAILY_PRESEND_HEARTBEAT_SEC)


# ---- the real stream, with a resident that blocks like Claude does --------------------------

class BlockingResident(_FakeResident):
    """ensure_alive and the stdin write block until the test lets them go."""

    def __init__(self, *, spawn_gate=None, write_gate=None, alive_error=None):
        super().__init__()
        self.spawn_gate = spawn_gate
        self.write_gate = write_gate
        self.alive_error = alive_error
        self.stdin_writes = 0
        self.ensure_alive_calls = 0
        self.threads = {}
        self.log = []

    def ensure_alive(self, system_text, env, tool_profile=None):
        self.ensure_alive_calls += 1
        self.threads['ensure_alive'] = threading.get_ident()
        self.log.append('spawn_begin')
        if self.spawn_gate is not None:
            self.spawn_gate.wait(15)
        if self.alive_error is not None:
            raise self.alive_error
        self.log.append('spawn_end')
        return super().ensure_alive(system_text, env, tool_profile or self.tool_profile)

    def send_turn(self, content, commit_meta=None, turn_lease=None, **kwargs):
        self.sent.append(str(content))
        self.threads['send_turn'] = threading.get_ident()
        self.log.append('stdin_write_begin')
        if self.write_gate is not None:
            self.write_gate.wait(15)           # the pipe is full until the CLI reads
        self.stdin_writes += 1                 # exactly one write+flush per turn
        self.log.append('stdin_flushed')
        try:
            yield ('text', 'hello')
            yield ('done', ('hello', '', {'input_tokens': 1, 'output_tokens': 1}, {}))
        finally:
            self.threads['send_turn_cleanup'] = threading.get_ident()
            self.log.append('send_turn_closed')


class DailyStreamHeartbeatTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        uid = _insert(self.db, 'hayana', 'cold turn', '2026-07-27 10:00:00')
        self.resident = None
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            self.plan = _REAL_PREPARE_DAILY_TURN(
                user_message_id=uid, db_path=self.db, resident=_FakeResident(),
                static_system='S', wall_now=_FIXED_NOW,
            )
        self.stack = contextlib.ExitStack()
        for patch in (
            mock.patch.object(config_store, 'get_bool', return_value=True),
            mock.patch('chat.reality_context.build_reality_context',
                       return_value={'time_anchor': '', 'weather_anchor': ''}),
            mock.patch('chat.reality_context.prepend_reality_to_provider_content',
                       side_effect=lambda content, _reality: content),
            mock.patch.object(dr, 'format_resident_turn_content', return_value='body'),
            mock.patch.object(dr, '_apply_daily_cold_prompt_fence',
                              side_effect=lambda _plan, **kwargs: kwargs['content']),
            mock.patch.object(dr, '_bind_context_install_render_receipt'),
            mock.patch.object(dr, 'set_local_binding'),
            mock.patch.object(dr.dc, 'get_resident_history_cursor', return_value=0),
            mock.patch.object(dr.dc, 'upsert_resident_owner'),
            mock.patch.object(dr, '_capture_transcript_start'),
            mock.patch.object(dr, '_capture_transcript_end'),
            mock.patch.object(dr, '_validate_hot_no_op_payload'),
            mock.patch.object(dr, '_observe_continuity_shadow'),
        ):
            self.stack.enter_context(patch)

    def tearDown(self):
        self.stack.close()
        dr._release_lease(self.plan)
        os.unlink(self.db)

    def stream(self, resident, **kwargs):
        self.resident = resident
        return dr.stream_daily_resident_turn(
            self.plan, resident=resident, env={}, static_system='S',
            presend_heartbeat_sec=INTERVAL, **kwargs,
        )

    def drain_until_text(self, gen, *, release=None, beats_before_release=6):
        """Pull events; release the gate after N heartbeats; return all events.

        Every next() is timed: a blocked step must never leave the consumer
        waiting longer than BOUND (the stand-in for the proxy's idle cut).
        """
        events, worst, beats = [], 0.0, 0
        while True:
            t0 = time.monotonic()
            event = next(gen)
            worst = max(worst, time.monotonic() - t0)
            events.append(event)
            if event[0] == 'heartbeat':
                beats += 1
                if release is not None and beats >= beats_before_release and not release.is_set():
                    release.set()
            if event[0] == 'text':
                break
        self.assertLess(worst, BOUND, 'the stream was silent for too long')
        return events

    def assertSilenceFilled(self, events, minimum):
        beats = [e for e in events if e[0] == 'heartbeat']
        self.assertGreaterEqual(len(beats), minimum)
        text_at = next(i for i, e in enumerate(events) if e[0] == 'text')
        self.assertTrue(all(e[0] == 'heartbeat' for e in events[:text_at]),
                        'only heartbeats may precede the first provider content')

    def test_ensure_alive_blocking_30s_is_covered_by_heartbeats(self):
        gate = threading.Event()
        resident = BlockingResident(spawn_gate=gate)
        events = self.drain_until_text(self.stream(resident), release=gate)
        self.assertSilenceFilled(events, 6)
        self.assertEqual(events[-1], ('text', 'hello'))
        self.assertEqual(resident.ensure_alive_calls, 1)
        self.assertEqual(resident.stdin_writes, 1)

    def test_stdin_write_blocking_30s_is_covered_by_heartbeats(self):
        gate = threading.Event()
        resident = BlockingResident(write_gate=gate)
        events = self.drain_until_text(self.stream(resident), release=gate)
        self.assertSilenceFilled(events, 6)
        self.assertEqual(events[-1], ('text', 'hello'))
        self.assertEqual(resident.stdin_writes, 1, 'the turn must be written exactly once')
        self.assertEqual(resident.sent, ['body'])
        self.assertEqual(resident.ensure_alive_calls, 1)

    def test_hot_context_check_blocking_is_covered_by_heartbeats(self):
        gate = threading.Event()
        calls = []

        def slow_hot_plan(plan, *, resident, static_system):
            calls.append(1)
            gate.wait(15)
            return 'NO_OP'

        self.plan.manifest['context_plan_hot_pending'] = True
        resident = BlockingResident()
        with mock.patch.object(dr, '_prepare_hot_context_plan', side_effect=slow_hot_plan):
            events = self.drain_until_text(self.stream(resident), release=gate)
        self.assertSilenceFilled(events, 6)
        self.assertEqual(calls, [1])
        self.assertEqual(resident.stdin_writes, 1)

    def test_all_three_blocking_steps_in_one_turn_keep_the_stream_alive(self):
        hot, spawn, write = threading.Event(), threading.Event(), threading.Event()

        def slow_hot_plan(plan, *, resident, static_system):
            hot.wait(15)
            return 'NO_OP'

        self.plan.manifest['context_plan_hot_pending'] = True
        resident = BlockingResident(spawn_gate=spawn, write_gate=write)
        gen_events, worst, beats = [], 0.0, 0
        with mock.patch.object(dr, '_prepare_hot_context_plan', side_effect=slow_hot_plan):
            gen = self.stream(resident)
            gates = [hot, spawn, write]
            while True:
                t0 = time.monotonic()
                event = next(gen)
                worst = max(worst, time.monotonic() - t0)
                gen_events.append(event)
                if event[0] == 'heartbeat':
                    beats += 1
                    if beats % 6 == 0 and gates:
                        gates.pop(0).set()   # let the next stage through, ~6 beats apart
                if event[0] == 'text':
                    break
        self.assertLess(worst, BOUND)
        self.assertGreaterEqual(beats, 18)
        self.assertEqual(resident.log[:3], ['spawn_begin', 'spawn_end', 'stdin_write_begin'])
        self.assertEqual(resident.stdin_writes, 1)
        self.assertEqual(resident.ensure_alive_calls, 1)

    def test_a_fast_turn_adds_nothing_and_matches_the_unpumped_stream(self):
        resident = BlockingResident()
        pumped = list(self.stream(resident))
        self.assertEqual([e[0] for e in pumped], ['text', 'done'])
        self.assertEqual(resident.stdin_writes, 1)

    def test_disabled_pump_is_the_original_direct_iteration(self):
        resident = BlockingResident()
        events = list(dr.stream_daily_resident_turn(
            self.plan, resident=resident, env={}, static_system='S', presend_heartbeat_sec=0,
        ))
        self.assertEqual([e[0] for e in events], ['text', 'done'])
        self.assertEqual(resident.stdin_writes, 1)

    def test_text_and_done_follow_normally_and_nothing_is_dropped_or_reordered(self):
        gate = threading.Event()
        resident = BlockingResident(write_gate=gate)
        gen = self.stream(resident)
        self.drain_until_text(gen, release=gate)
        rest = list(gen)
        self.assertEqual([e[0] for e in rest], ['done'])
        self.assertEqual(rest[0][1][0], 'hello')

    def test_the_whole_turn_runs_on_one_thread_that_is_not_the_consumers(self):
        gate = threading.Event()
        resident = BlockingResident(spawn_gate=gate)
        gen = self.stream(resident)
        self.drain_until_text(gen, release=gate)
        list(gen)
        self.assertEqual(resident.threads['ensure_alive'], resident.threads['send_turn'])
        self.assertNotEqual(resident.threads['ensure_alive'], threading.get_ident())

    def test_client_leaving_during_the_stdin_write_waits_then_cleans_up_without_rewriting(self):
        gate = threading.Event()
        resident = BlockingResident(write_gate=gate)
        gen = self.stream(resident)
        beats = 0
        while beats < 4:
            self.assertEqual(next(gen)[0], 'heartbeat')
            beats += 1
        closed = threading.Event()
        threading.Thread(target=lambda: (gen.close(), closed.set()), daemon=True).start()
        self.assertFalse(closed.wait(INTERVAL * 10), 'the write cannot be abandoned mid-flight')
        self.assertEqual(resident.stdin_writes, 0)
        gate.set()                                   # the CLI finally reads
        self.assertTrue(closed.wait(5))
        self.assertEqual(resident.stdin_writes, 1, 'written exactly once, never repeated')
        self.assertEqual(resident.sent, ['body'])
        self.assertLess(resident.log.index('stdin_flushed'), resident.log.index('send_turn_closed'))
        self.assertEqual(resident.threads['send_turn'], resident.threads['send_turn_cleanup'])

    def test_client_leaving_during_spawn_still_sends_once_like_an_unwrapped_stream(self):
        gate = threading.Event()
        resident = BlockingResident(spawn_gate=gate)
        gen = self.stream(resident)
        take(gen, 3)
        closed = threading.Event()
        threading.Thread(target=lambda: (gen.close(), closed.set()), daemon=True).start()
        self.assertFalse(closed.wait(INTERVAL * 10))
        gate.set()
        self.assertTrue(closed.wait(5))
        # The in-flight step is not abandoned: the spawn finishes and the turn that
        # was already being sent is written once, exactly as when the client
        # disconnects at the first yield of an unwrapped stream.
        self.assertEqual(resident.ensure_alive_calls, 1)
        self.assertEqual(resident.stdin_writes, 1)

    def test_errors_from_ensure_alive_surface_after_the_heartbeats(self):
        class SpawnFailed(Exception):
            pass

        gate = threading.Event()
        error = SpawnFailed('claude did not start')
        resident = BlockingResident(spawn_gate=gate, alive_error=error)
        gen = self.stream(resident)
        take(gen, 3)
        gate.set()
        with self.assertRaises(SpawnFailed) as caught:
            list(gen)
        self.assertIs(caught.exception, error)
        self.assertEqual(resident.stdin_writes, 0)
        self.assertEqual(resident.ensure_alive_calls, 1)


def take(gen, n):
    return [next(gen) for _ in range(n)]


# ---- wiring ----------------------------------------------------------------------------------

class GatewayWiringTests(unittest.TestCase):
    def test_gateway_hands_its_keepalive_interval_to_the_runtime_through_the_plan(self):
        source = open(os.path.join(os.path.dirname(__file__), '..', 'gateway.py')).read()
        self.assertIn('_daily_plan._presend_heartbeat_sec = DAILY_PRESEND_KEEPALIVE_SEC', source)
        # the stream call keeps its original signature (test doubles replace it)
        call = source[source.index('_daily_events = _daily_rt.stream_daily_resident_turn('):]
        self.assertNotIn('presend_heartbeat_sec', call[:call.index(')\n') + 1])

    def test_interval_resolution_prefers_explicit_then_plan_then_default(self):
        plan = mock.MagicMock()
        self.assertEqual(dr._presend_heartbeat_interval(plan, 1.5), 1.5)
        self.assertEqual(dr._presend_heartbeat_interval(plan, None), dr.DAILY_PRESEND_HEARTBEAT_SEC)
        plan._presend_heartbeat_sec = 0.25
        self.assertEqual(dr._presend_heartbeat_interval(plan, None), 0.25)
        self.assertEqual(dr._presend_heartbeat_interval(plan, 2), 2.0)
        plan._presend_heartbeat_sec = True   # a bool is not a cadence
        self.assertEqual(dr._presend_heartbeat_interval(plan, None), dr.DAILY_PRESEND_HEARTBEAT_SEC)
        self.assertEqual(dr._presend_heartbeat_interval(object(), 0), 0.0)

    def test_the_cadence_survives_reprepare_because_it_is_not_a_plan_field(self):
        import dataclasses
        names = {f.name for f in dataclasses.fields(dr.DailyTurnPlan)}
        self.assertNotIn('_presend_heartbeat_sec', names)

    def test_heartbeat_events_become_transport_pings(self):
        self.assertEqual(gateway._daily_heartbeat_sse('heartbeat'), gateway._sse_json({'t': 'ping'}))
        self.assertIsNone(gateway._daily_heartbeat_sse('text'))

    def test_default_cadence_is_well_under_the_proxy_idle_cut(self):
        self.assertLessEqual(dr.DAILY_PRESEND_HEARTBEAT_SEC, 10)
        self.assertEqual(dr.DAILY_PRESEND_HEARTBEAT_SEC, gateway.DAILY_PRESEND_KEEPALIVE_SEC)


if __name__ == '__main__':
    unittest.main()


# ---- through the real gateway generator: SSE bytes on the wire --------------------------------

def _sse_types(chunks):
    import json
    out = []
    for chunk in chunks:
        line = chunk.strip()
        if line.startswith('data: '):
            out.append(json.loads(line[6:]).get('t'))
    return out


class GatewaySseBytesTests(unittest.TestCase):
    """The reported failure: nginx saw 108 bytes, then silence, then the phone dropped."""

    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        self.uid = _insert(self.db, 'hayana', 'cold start', '2026-07-27 10:00:00')

    def tearDown(self):
        dr.set_local_binding(None)
        os.unlink(self.db)

    def _run(self, resident, release_after_pings):
        resident_ref = resident

        def prepare(**_kwargs):
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                return _REAL_PREPARE_DAILY_TURN(
                    user_message_id=self.uid, db_path=self.db, resident=resident_ref,
                    static_system='S', wall_now=_FIXED_NOW,
                )

        with contextlib.ExitStack() as stack:
            for patch in (
                mock.patch.object(config_store, 'get_bool', return_value=True),
                mock.patch('chat.daily_context.enabled', return_value=True),
                mock.patch.object(gateway, 'DB_PATH', self.db),
                mock.patch.object(gateway, 'DAILY_PRESEND_KEEPALIVE_SEC', INTERVAL),
                mock.patch('chat.system_builder.build_cc_daily_static_parts',
                           return_value={'persona': 'P', 'full_system': 'STATIC'}),
                mock.patch.object(dr, 'prepare_daily_turn', side_effect=prepare),
                mock.patch.object(gateway, '_CC_RESIDENT', resident_ref),
                mock.patch('chat.reality_context.build_reality_context',
                           return_value={'time_anchor': '', 'weather_anchor': ''}),
                mock.patch('chat.reality_context.prepend_reality_to_provider_content',
                           side_effect=lambda content, _reality: content),
                mock.patch.object(dr, 'format_resident_turn_content', return_value='body'),
                mock.patch.object(dr, '_apply_daily_cold_prompt_fence',
                                  side_effect=lambda _plan, **kwargs: kwargs['content']),
                mock.patch.object(dr, '_bind_context_install_render_receipt'),
                mock.patch.object(dr.dc, 'get_resident_history_cursor', return_value=0),
                mock.patch.object(dr, '_capture_transcript_start'),
                mock.patch.object(dr, '_capture_transcript_end'),
                mock.patch.object(dr, '_validate_hot_no_op_payload'),
                mock.patch.object(dr, '_observe_continuity_shadow'),
            ):
                stack.enter_context(patch)
            gen = gateway._stream_cc_daily_soft_window({'user_message_id': self.uid}, 'cold start')
            chunks, pings = [], 0
            worst = 0.0
            try:
                while True:
                    t0 = time.monotonic()
                    chunk = next(gen)
                    worst = max(worst, time.monotonic() - t0)
                    chunks.append(chunk)
                    types = _sse_types([chunk])
                    if types == ['ping']:
                        pings += 1
                        if pings >= release_after_pings:
                            for gate in (resident_ref.spawn_gate, resident_ref.write_gate):
                                if gate is not None:
                                    gate.set()
                    if 'text' in types:
                        break
            finally:
                for gate in (resident_ref.spawn_gate, resident_ref.write_gate):
                    if gate is not None:
                        gate.set()
                gen.close()
            return chunks, worst

    def test_blocked_ensure_alive_keeps_pinging_on_the_wire_then_text_arrives(self):
        resident = BlockingResident(spawn_gate=threading.Event())
        chunks, worst = self._run(resident, release_after_pings=6)
        types = _sse_types(chunks)
        self.assertGreaterEqual(types.count('ping'), 6)
        self.assertEqual(types[-1], 'text')
        self.assertLess(worst, BOUND, 'the SSE stream went silent while Claude was starting')
        self.assertEqual(resident.stdin_writes, 1)

    def test_blocked_stdin_write_keeps_pinging_on_the_wire_then_text_arrives(self):
        resident = BlockingResident(write_gate=threading.Event())
        chunks, worst = self._run(resident, release_after_pings=6)
        types = _sse_types(chunks)
        self.assertGreaterEqual(types.count('ping'), 6)
        self.assertEqual(types[-1], 'text')
        self.assertLess(worst, BOUND, 'the SSE stream went silent during the stdin write')
        self.assertEqual(resident.stdin_writes, 1)
        self.assertEqual(resident.ensure_alive_calls, 1)
