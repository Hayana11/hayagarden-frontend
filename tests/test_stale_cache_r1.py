"""Focused no-provider tests for the 55-minute stale resident gate."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest import mock

import cc_resident


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


if __name__ == "__main__":
    unittest.main()
