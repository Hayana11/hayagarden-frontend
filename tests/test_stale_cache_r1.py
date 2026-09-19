"""Focused no-provider tests for the 55-minute stale resident gate."""

from __future__ import annotations

import contextlib
import threading
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
from chat import daily_runtime as dr
from chat import reality_context


ROOT = Path(__file__).resolve().parents[1]


class _AliveProcess:
    def poll(self):
        return None


def _minimal_resident():
    session = object.__new__(cc_resident.ResidentSession)
    session._lock = threading.RLock()
    session._last_round_context = 0
    session._last_cache_refresh_monotonic = None
    session._last_cache_refresh_at = None
    session._last_used = 123.0
    session._proc = _AliveProcess()
    session._next_spawn_reason = None
    return session


class StaleCacheGateTests(unittest.TestCase):
    def test_exact_context_threshold_is_not_stale(self):
        session = _minimal_resident()
        session._last_round_context = 70_000
        session._last_cache_refresh_monotonic = 1_000.0
        with mock.patch.object(cc_resident.time, "monotonic", return_value=4_300.0):
            self.assertFalse(session._stale_cache_guard_due())

    def test_one_token_over_threshold_at_exact_age_is_stale(self):
        session = _minimal_resident()
        session._last_round_context = 70_001
        session._last_cache_refresh_monotonic = 1_000.0
        with mock.patch.object(cc_resident.time, "monotonic", return_value=4_300.0):
            self.assertTrue(session._stale_cache_guard_due())

    def test_age_just_under_55_minutes_is_not_stale(self):
        session = _minimal_resident()
        session._last_round_context = 75_000
        session._last_cache_refresh_monotonic = 1_000.0
        with mock.patch.object(cc_resident.time, "monotonic", return_value=4_299.0):
            self.assertFalse(session._stale_cache_guard_due())

    def test_unknown_refresh_clock_is_a_noop(self):
        session = _minimal_resident()
        session._last_round_context = 90_000
        with mock.patch.object(cc_resident.time, "monotonic", return_value=10_000.0):
            self.assertFalse(session._stale_cache_guard_due())

    def test_future_or_invalid_refresh_clock_is_a_noop(self):
        session = _minimal_resident()
        session._last_round_context = 90_000
        session._last_cache_refresh_monotonic = 10_001.0
        with mock.patch.object(cc_resident.time, "monotonic", return_value=10_000.0):
            self.assertFalse(session._stale_cache_guard_due())
        session._last_cache_refresh_monotonic = "not-a-clock"
        self.assertFalse(session._stale_cache_guard_due())

    def test_fresh_successful_clock_is_not_stale(self):
        session = _minimal_resident()
        session._last_round_context = 90_000
        self.assertTrue(session.commit_cache_freshness(
            wall_at=100.0,
            monotonic_at=9_000.0,
        ))
        with mock.patch.object(cc_resident.time, "monotonic", return_value=9_005.0):
            self.assertFalse(session._stale_cache_guard_due())

    def test_guard_does_not_touch_last_used(self):
        session = _minimal_resident()
        session._last_round_context = 90_001
        session._last_cache_refresh_monotonic = 1_000.0
        with mock.patch.object(cc_resident.time, "monotonic", return_value=4_300.0):
            self.assertTrue(session._stale_cache_guard_due())
        self.assertEqual(session._last_used, 123.0)

    def test_peek_can_opt_in_without_changing_default_call(self):
        session = _minimal_resident()
        session._decide_respawn_reason = mock.Mock(return_value="stale_cache_guard")
        self.assertEqual(
            session.peek_respawn_reason(
                "system",
                allow_stale_cache_guard=True,
            ),
            "stale_cache_guard",
        )
        session._decide_respawn_reason.assert_called_once_with(
            "system",
            tool_profile=cc_resident.TOOL_PROFILE_LEGACY,
            allow_stale_cache_guard=True,
        )

    def test_guard_spawns_once_with_named_reason(self):
        session = _minimal_resident()
        session._decide_respawn_reason = mock.Mock(
            return_value="stale_cache_guard",
        )
        session._spawn = mock.Mock()
        self.assertTrue(session.ensure_stale_cache_guard("system", {"ENV": "1"}))
        session._spawn.assert_called_once_with(
            "system",
            {"ENV": "1"},
            reason="stale_cache_guard",
            tool_profile=cc_resident.TOOL_PROFILE_LEGACY,
        )

    def test_non_stale_reason_does_not_spawn(self):
        session = _minimal_resident()
        session._decide_respawn_reason = mock.Mock(return_value="hard_context")
        session._spawn = mock.Mock()
        self.assertFalse(session.ensure_stale_cache_guard("system", {}))
        session._spawn.assert_not_called()

    def test_replacement_failure_propagates_and_does_not_retry(self):
        session = _minimal_resident()
        session._decide_respawn_reason = mock.Mock(
            return_value="stale_cache_guard",
        )
        session._spawn = mock.Mock(side_effect=RuntimeError("spawn failed"))
        with self.assertRaisesRegex(RuntimeError, "spawn failed"):
            session.ensure_stale_cache_guard("system", {})
        session._spawn.assert_called_once()

    def test_reset_clears_freshness_clock_for_new_generation(self):
        session = _minimal_resident()
        session._last_round_context = 90_000
        session._last_cache_refresh_at = 100.0
        session._last_cache_refresh_monotonic = 200.0
        session._reset_session_meta(respawn_reason="stale_cache_guard")
        self.assertIsNone(session._last_cache_refresh_at)
        self.assertIsNone(session._last_cache_refresh_monotonic)

    def test_stale_check_is_after_stronger_respawn_checks(self):
        source = (ROOT / "cc_resident.py").read_text(encoding="utf-8")
        decision_start = source.index("def _decide_respawn_reason(")
        decision_end = source.index("\n    def ", decision_start + 1)
        decision = source[decision_start:decision_end]
        self.assertLess(
            decision.index("if allow_stale_cache_guard"),
            decision.index("return None"),
        )
        for stronger_reason in (
            "history_rewrite",
            "process_dead",
            "model_changed",
            "effort_changed",
            "idle",
            "system_changed",
            "hard_context",
            "turn_limit",
            "soft_context",
            "tool_profile_changed",
            "tool_surface_changed",
        ):
            self.assertLess(
                decision.index(stronger_reason),
                decision.index("if allow_stale_cache_guard"),
            )

    def test_generic_ensure_alive_does_not_opt_in_to_stale_gate(self):
        source = (ROOT / "cc_resident.py").read_text(encoding="utf-8")
        ensure_start = source.index("def ensure_alive(")
        ensure_end = source.index("\n    def ", ensure_start + 1)
        ensure = source[ensure_start:ensure_end]
        self.assertNotIn("allow_stale_cache_guard=True", ensure)

    def test_gateway_checks_stale_gate_before_send(self):
        source = (ROOT / "gateway.py").read_text(encoding="utf-8")
        start = source.index("def _cc_resident_stream_gen(")
        end = source.index("\ndef ", start + 1)
        block = source[start:end]
        self.assertLess(
            block.index("_cc_stale_guard_before_resident_reuse"),
            block.index("_CC_RESIDENT.ensure_alive"),
        )
        self.assertLess(
            block.index("_cc_stale_guard_before_resident_reuse"),
            block.index("_CC_RESIDENT.send_turn"),
        )

    def test_gateway_stale_probe_replaces_before_reuse(self):
        source = (ROOT / "gateway.py").read_text(encoding="utf-8")
        helper_start = source.index(
            "def _cc_stale_guard_before_resident_reuse(",
        )
        helper_end = source.index(
            "\ndef _cc_resident_stream_gen(",
            helper_start,
        )
        helper = source[helper_start:helper_end]
        self.assertLess(
            helper.index("peek_respawn_reason"),
            helper.index("ensure_stale_cache_guard"),
        )
        self.assertNotIn("_CC_RESIDENT.send_turn", helper)

    def test_daily_chat_uses_registered_reprepare_path(self):
        source = (ROOT / "chat" / "daily_runtime.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("allow_stale_cache_guard=True", source)
        self.assertIn("reprepare_after_registered_session_change", source)

    def test_unified_wake_does_not_opt_in(self):
        source = (ROOT / "chat" / "behavior_authority_b3.py").read_text(
            encoding="utf-8",
        )
        self.assertNotIn("ensure_stale_cache_guard", source)
        self.assertNotIn("allow_stale_cache_guard=True", source)

    def test_packing_authority_is_not_rewired_to_stale_guard(self):
        source = (ROOT / "chat" / "cold_bootstrap_budget.py").read_text(
            encoding="utf-8",
        )
        target_start = source.index("def cold_prompt_target(")
        target_end = source.index("def estimate_text_tokens(", target_start)
        target = source[target_start:target_end]
        self.assertNotIn("STALE_CACHE", target)
        self.assertNotIn("cold_rebuild_guard", target)

    def test_packing_targets_and_limits_remain_authoritative(self):
        from chat.cold_bootstrap_budget import (
            capacity_swap_prompt_target,
            cold_hard_limit,
            cold_rebuild_guard,
            cold_soft_limit,
            resident_rebuild_prompt_target,
        )

        self.assertEqual(resident_rebuild_prompt_target(), 90_000)
        self.assertEqual(capacity_swap_prompt_target(), 90_000)
        self.assertEqual(cold_rebuild_guard(), 70_000)
        self.assertEqual(cold_soft_limit(), 150_000)
        self.assertEqual(cold_hard_limit(), 180_000)
        self.assertEqual(cc_resident.IDLE_REAP_SECONDS, 3 * 60 * 60)


class _NoopDailyHeartbeat:
    failed = False

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return False


class _RuntimeDailyResident:
    tool_profile = dr.DAILY_TOOL_PROFILE

    def __init__(
        self,
        *,
        context_tokens=70_001,
        last_refresh_monotonic=1_000.0,
        clock_now=4_300.0,
        existing_reason=None,
        fail_on_ensure=False,
    ):
        self._alive = True
        self._cold = False
        self._next_spawn_reason = None
        self._pending_respawn_reason = None
        self._last_round_context = context_tokens
        self._last_cache_refresh_monotonic = last_refresh_monotonic
        self._clock_now = clock_now
        self._existing_reason = existing_reason
        self._fail_on_ensure = fail_on_ensure
        self.generation = 1
        self.session_id = "old-session"
        self.process_state = "old"
        self.old_resident_send_count = 0
        self.new_resident_send_count = 0
        self.send_count = 0
        self.ensure_alive_calls = 0
        self.spawn_attempt_reasons = []
        self.peek_observations = []
        self.stale_specific_path_calls = 0
        self.commit_cache_freshness_calls = 0
        self.reprepare_completed = False
        self.freshness_at_send = []

    def _kill(self, quiet=False):
        self._alive = False
        self.process_state = "closed"

    def peek_respawn_reason(
        self,
        system_text,
        *,
        tool_profile=cc_resident.TOOL_PROFILE_LEGACY,
        allow_stale_cache_guard=False,
    ):
        self.peek_observations.append({
            "allow_stale_cache_guard": bool(allow_stale_cache_guard),
            "existing_reason": self._existing_reason,
        })
        if self._existing_reason is not None:
            return self._existing_reason
        if (
            allow_stale_cache_guard
            and self._last_round_context > 70_000
            and self._last_cache_refresh_monotonic is not None
            and self._clock_now - self._last_cache_refresh_monotonic >= 3_300
        ):
            return "stale_cache_guard"
        return None

    def ensure_stale_cache_guard(self, *args, **kwargs):
        self.stale_specific_path_calls += 1
        raise AssertionError("Daily registered path must not call gateway stale helper")

    def ensure_alive(self, system_text, env, tool_profile=dr.DAILY_TOOL_PROFILE):
        self.ensure_alive_calls += 1
        self.spawn_attempt_reasons.append(
            self._next_spawn_reason or "process_dead"
        )
        if self._fail_on_ensure:
            raise RuntimeError("replacement spawn failed")
        self.generation += 1
        self.process_state = "new"
        self._alive = True
        self._cold = True
        self._pending_respawn_reason = (
            self._next_spawn_reason or "process_dead"
        )
        self._next_spawn_reason = None
        self._last_cache_refresh_monotonic = None
        return True

    def commit_cache_freshness(self, *, wall_at, monotonic_at):
        self.commit_cache_freshness_calls += 1
        self._last_cache_refresh_monotonic = monotonic_at
        return True

    def send_turn(
        self,
        content,
        commit_meta=None,
        on_stdin_flushed=None,
        **kwargs,
    ):
        self.send_count += 1
        if self.process_state == "old":
            self.old_resident_send_count += 1
        elif self.process_state == "new":
            self.new_resident_send_count += 1
        else:
            raise AssertionError("send reached a closed resident")
        self.freshness_at_send.append(self._last_cache_refresh_monotonic)
        if not self.reprepare_completed:
            raise AssertionError("CURRENT USER sent before registered reprepare")
        if on_stdin_flushed is not None:
            on_stdin_flushed()
        usage = {
            "input_tokens": 1,
            "output_tokens": 1,
            "respawn_reason": self._pending_respawn_reason,
        }
        self._pending_respawn_reason = None
        yield ("done", ("daily reply", "", usage, {}))


def _runtime_plan(*, generation=1, is_cold=False, is_respawn=False):
    key = "daily:default:1:%d" % generation
    return dr.DailyTurnPlan(
        request_id="runtime-test",
        chat_id="default",
        local_day="2026-09-19",
        context_id=1,
        context_epoch=1,
        resident_generation=generation,
        resident_key=key,
        user_message_id=2,
        epoch_token={
            "context_id": 1,
            "context_epoch": 1,
            "resident_generation": generation,
            "chat_id": "default",
        },
        lease_owner="runtime-test-owner",
        is_cold=is_cold,
        is_respawn=is_respawn,
        cursor_before=0,
        assembly={"state_snapshot": {}, "manifest": {}},
        manifest={
            "provider": "claude_code",
            "model": "test-model",
            "static_system_sha256": "static-sha",
            "persona_sha256": "persona-sha",
        },
        user_content="CURRENT USER",
        db_path="/tmp/stale-cache-r1-runtime-test.db",
        worker_id="runtime-test-worker",
        tool_profile=dr.DAILY_TOOL_PROFILE,
        turn_lease={"lease_id": "runtime-test-lease"},
    )


def _runtime_replacement_plan():
    return _runtime_plan(generation=2, is_cold=True, is_respawn=True)


def _daily_runtime_stack(resident, replacement_plan):
    stack = contextlib.ExitStack()
    replacement_calls = []

    def _replacement(**kwargs):
        replacement_calls.append(dict(kwargs))
        resident.reprepare_completed = True
        return replacement_plan

    def _registry(context_id, resident_generation, *, db_path=None):
        if int(resident_generation) == 1:
            return {"claude_session_id": "old-session"}
        return None

    stack.enter_context(mock.patch.object(dr, "LeaseHeartbeat", _NoopDailyHeartbeat))
    stack.enter_context(mock.patch.object(dr, "verify_epoch_token"))
    stack.enter_context(mock.patch.object(dr, "is_epoch_token_current", return_value=True))
    stack.enter_context(mock.patch.object(dr, "_release_lease"))
    stack.enter_context(mock.patch.object(dr, "get_context_claude_session", side_effect=_registry))
    stack.enter_context(mock.patch.object(
        dr, "effective_static_system_for_registry", return_value="STATIC",
    ))
    stack.enter_context(mock.patch.object(
        dr, "prepare_daily_turn", side_effect=_replacement,
    ))
    stack.enter_context(mock.patch.object(dr.dc, "respawn_daily_resident"))
    stack.enter_context(mock.patch.object(
        dr.dc, "get_resident_history_cursor", return_value=0,
    ))
    stack.enter_context(mock.patch.object(dr.dc, "upsert_resident_owner"))
    stack.enter_context(mock.patch.object(
        dr, "format_resident_turn_content", return_value="CURRENT USER",
    ))
    stack.enter_context(mock.patch.object(
        dr, "_apply_daily_cold_prompt_fence",
        side_effect=lambda plan, **kwargs: kwargs["content"],
    ))
    stack.enter_context(mock.patch.object(dr, "_capture_transcript_start"))
    stack.enter_context(mock.patch.object(dr, "_capture_transcript_end"))
    stack.enter_context(mock.patch.object(dr, "_bind_context_install_render_receipt"))
    stack.enter_context(mock.patch.object(dr, "_observe_continuity_shadow"))
    stack.enter_context(mock.patch.object(dr, "_validate_hot_no_op_payload"))
    stack.enter_context(mock.patch.object(
        reality_context,
        "build_reality_context",
        return_value={"time_anchor": "", "weather_anchor": ""},
    ))
    stack.enter_context(mock.patch.object(
        reality_context,
        "prepend_reality_to_provider_content",
        side_effect=lambda content, _reality: content,
    ))
    reprepare_spy = stack.enter_context(mock.patch.object(
        dr,
        "reprepare_after_registered_session_change",
        wraps=dr.reprepare_after_registered_session_change,
    ))
    return stack, replacement_calls, reprepare_spy


class StaleCacheDailyRuntimeTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def tearDown(self):
        dr.reset_bindings_for_tests()

    def _bind_initial_plan(self, plan):
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=plan.resident_key,
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            bound_cursor_message_id=plan.cursor_before,
            process_generation=1,
            tool_profile=plan.tool_profile,
            claude_session_id="old-session",
        ))

    def test_stale_success_is_exactly_once_on_reprepared_resident(self):
        resident = _RuntimeDailyResident()
        plan = _runtime_plan()
        replacement = _runtime_replacement_plan()
        self._bind_initial_plan(plan)
        stack, replacement_calls, reprepare_spy = _daily_runtime_stack(
            resident, replacement,
        )
        with stack:
            events = list(dr.ensure_resident_and_stream(
                plan,
                resident=resident,
                env={},
                static_system="STATIC",
            ))

        done = [payload for kind, payload in events if kind == "done"][0]
        usage = done[2]
        self.assertEqual(resident.old_resident_send_count, 0)
        self.assertEqual(resident.new_resident_send_count, 1)
        self.assertEqual(resident.send_count, 1)
        self.assertEqual(len(replacement_calls), 1)
        self.assertEqual(resident.generation, 2)
        self.assertEqual(resident.freshness_at_send, [None])
        self.assertEqual(resident.commit_cache_freshness_calls, 0)
        self.assertEqual(usage["respawn_reason"], "stale_cache_guard")
        self.assertEqual(
            reprepare_spy.call_args.kwargs["respawn_reason"],
            "stale_cache_guard",
        )

    def test_stale_replacement_failure_is_fail_closed_before_stdin(self):
        resident = _RuntimeDailyResident(fail_on_ensure=True)
        plan = _runtime_plan()
        replacement = _runtime_replacement_plan()
        self._bind_initial_plan(plan)
        stack, replacement_calls, reprepare_spy = _daily_runtime_stack(
            resident, replacement,
        )
        with stack:
            with self.assertRaisesRegex(RuntimeError, "replacement spawn failed"):
                list(dr.ensure_resident_and_stream(
                    plan,
                    resident=resident,
                    env={},
                    static_system="STATIC",
                ))

        self.assertEqual(resident.old_resident_send_count, 0)
        self.assertEqual(resident.new_resident_send_count, 0)
        self.assertEqual(resident.send_count, 0)
        self.assertEqual(resident.spawn_attempt_reasons, ["stale_cache_guard"])
        self.assertEqual(resident.ensure_alive_calls, 1)
        self.assertEqual(len(replacement_calls), 1)
        self.assertEqual(reprepare_spy.call_count, 1)
        self.assertTrue(resident.reprepare_completed)
        self.assertEqual(resident.stale_specific_path_calls, 0)

    def test_stronger_reason_is_runtime_authority_over_stale(self):
        resident = _RuntimeDailyResident(existing_reason="turn_limit")
        session = _minimal_resident()
        session._decide_respawn_reason = mock.Mock(return_value="turn_limit")
        session._spawn = mock.Mock()

        self.assertEqual(
            resident.peek_respawn_reason(
                "STATIC",
                allow_stale_cache_guard=True,
            ),
            "turn_limit",
        )
        self.assertFalse(session.ensure_stale_cache_guard("STATIC", {}))
        session._spawn.assert_not_called()
        self.assertEqual(resident.stale_specific_path_calls, 0)
        self.assertEqual(resident.send_count, 0)




if __name__ == "__main__":
    unittest.main()
