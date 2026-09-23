"""M2-02B generation-bound UH-A0 surface contract tests."""
from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import cc_resident
from chat.capacity_swap import CAPACITY_SWAP_REASONS


class _FakeProcess:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.pid = 7001
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        self._returncode = 0
        return 0


class SurfaceGenerationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rs = cc_resident.ResidentSession(
            self._tmp.name,
            "",
            str(Path(self._tmp.name) / "mcp.json"),
        )
        self.rs._proc = _FakeProcess()
        self.rs._system_text = "SYS"
        self.rs._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
        self.rs._model_identity = None
        self.rs._history_rewrite_epoch = "epoch-1"
        self.rs._last_used = time.time()
        self.rs._bound_tool_surface_fingerprint = "surface-a"

    def tearDown(self):
        self._tmp.cleanup()

    def _reason_patches(self, current_surface):
        stack = ExitStack()
        stack.enter_context(
            mock.patch(
                "chat.cc_history_rewrite.current_history_rewrite_epoch",
                return_value="epoch-1",
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_history_rewrite.is_unreadable_epoch",
                return_value=False,
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_history_rewrite.sanitize_bound_epoch",
                side_effect=lambda value: value,
            )
        )
        stack.enter_context(
            mock.patch(
                "tools.cc_capability_adapter.physical_surface_fingerprint",
                return_value=current_surface,
            )
        )
        return stack

    @staticmethod
    def _tool_flags(fingerprint="surface-a"):
        return {
            "tools": "Read",
            "extra": ["--allowedTools", "Read"],
            "surface_allowed": "Read",
            "mcp_path": "",
            "surface_fingerprint": fingerprint,
        }

    def _spawn_patches(self, process):
        stack = ExitStack()
        stack.enter_context(
            mock.patch(
                "cc_resident.subprocess.Popen",
                return_value=process,
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_model.cc_model_snapshot",
                return_value=("model", "model-identity", []),
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_runtime.require_managed_claude_runtime",
                return_value="2.1.280",
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_runtime.claude_cmd_for_version",
                side_effect=lambda _version, *args, **kwargs: list(args),
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_model.cc_model_runtime_compatibility",
                return_value=(True, None),
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_history_rewrite.current_history_rewrite_epoch",
                return_value="epoch-1",
            )
        )
        stack.enter_context(
            mock.patch(
                "chat.cc_history_rewrite.sanitize_bound_epoch",
                side_effect=lambda value: value,
            )
        )
        return stack

    def test_same_surface_keeps_hot_reuse(self):
        generation = self.rs.generation
        process = self.rs._proc
        with self._reason_patches("surface-a"):
            self.assertIsNone(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )
            )
        self.assertEqual(self.rs.generation, generation)
        self.assertIs(self.rs._proc, process)

    def test_changed_surface_is_lazy_read_only(self):
        generation = self.rs.generation
        process = self.rs._proc
        stdin = self.rs._proc.stdin
        with self._reason_patches("surface-b"):
            reason = self.rs.peek_respawn_reason(
                "SYS",
                tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
            )
        self.assertEqual(reason, "tool_surface_changed")
        self.assertEqual(self.rs.generation, generation)
        self.assertIs(self.rs._proc, process)
        self.assertFalse(stdin.closed)

    def test_storage_failure_surface_is_not_hot_reused(self):
        with self._reason_patches(mock.DEFAULT) as stack:
            stack.enter_context(
                mock.patch(
                    "tools.cc_capability_adapter.physical_surface_fingerprint",
                    side_effect=OSError("runtime state unavailable"),
                )
            )
            self.assertEqual(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "tool_surface_changed",
            )

    def test_surface_can_change_off_then_on_without_history_churn(self):
        with self._reason_patches("surface-b"):
            self.assertEqual(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "tool_surface_changed",
            )
        self.rs._bound_tool_surface_fingerprint = "surface-b"
        with self._reason_patches("surface-b"):
            self.assertIsNone(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )
            )
        with self._reason_patches("surface-a"):
            self.assertEqual(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "tool_surface_changed",
            )

    def test_missing_bound_surface_is_legacy_compatible(self):
        self.rs._bound_tool_surface_fingerprint = None
        with self._reason_patches("surface-b"):
            self.assertIsNone(
                self.rs.peek_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )
            )

    def test_existing_higher_priority_reason_wins(self):
        self.rs._system_text = "OLD"
        with self._reason_patches("surface-b"):
            self.assertEqual(
                self.rs.peek_respawn_reason(
                    "NEW",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "system_changed",
            )

    def test_legacy_and_text_only_ignore_surface_identity(self):
        for profile in (
            cc_resident.TOOL_PROFILE_LEGACY,
            cc_resident.TOOL_PROFILE_TEXT_ONLY,
        ):
            self.rs._tool_profile = profile
            with self._reason_patches(
                mock.Mock(side_effect=AssertionError("must not compare"))
            ):
                self.assertIsNone(
                    self.rs.peek_respawn_reason(
                        "SYS",
                        tool_profile=profile,
                    )
                )

    def test_tool_surface_change_is_not_capacity_swap(self):
        self.assertNotIn("tool_surface_changed", CAPACITY_SWAP_REASONS)

    def test_uh_a0_spawn_plan_fingerprint_is_passed_through(self):
        self.rs._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
        plan = {
            "turn_lease_path": str(Path(self._tmp.name) / "lease.json"),
            "built_in_tools_csv": "Read",
            "spawn_extra_args": ["--allowedTools", "Read"],
            "surface_allowlist_csv": "Read",
            "mcp_config_path": "",
            "physical_surface_fingerprint": "surface-plan",
        }
        with mock.patch(
            "tools.cc_capability_adapter.build_uh_a0_spawn_plan",
            return_value=plan,
        ):
            flags = self.rs._build_spawn_tool_flags(env={})
        self.assertEqual(flags["surface_fingerprint"], "surface-plan")

    def test_all_successful_uh_a0_spawn_paths_bind_surface(self):
        for method_name, kwargs in (
            (
                "_spawn",
                {"reason": "process_dead"},
            ),
            (
                "spawn_resumable",
                {"resume_session_id": "resume-session"},
            ),
            (
                "spawn_fresh_named",
                {"session_id": "11111111-1111-4111-8111-111111111111"},
            ),
        ):
            with self.subTest(method=method_name):
                session = cc_resident.ResidentSession(
                    self._tmp.name,
                    "",
                    str(Path(self._tmp.name) / "mcp.json"),
                )
                session._build_spawn_tool_flags = mock.Mock(
                    return_value=self._tool_flags("surface-success")
                )
                session._capture_tool_surface = mock.Mock()
                process = _FakeProcess()
                with self._spawn_patches(process):
                    if method_name == "_spawn":
                        session._spawn(
                            "SYS",
                            {},
                            tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                            **kwargs,
                        )
                    else:
                        getattr(session, method_name)(
                            "SYS",
                            {},
                            tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                            **kwargs,
                        )
                self.assertEqual(
                    session.bound_tool_surface_fingerprint,
                    "surface-success",
                )
                self.assertEqual(session.generation, 1)

    def test_failed_spawn_does_not_overwrite_previous_binding(self):
        session = cc_resident.ResidentSession(
            self._tmp.name,
            "",
            str(Path(self._tmp.name) / "mcp.json"),
        )
        session._bound_tool_surface_fingerprint = "old-surface"
        session._build_spawn_tool_flags = mock.Mock(
            return_value=self._tool_flags("new-surface")
        )
        with self._spawn_patches(None) as stack:
            stack.enter_context(
                mock.patch(
                    "cc_resident.subprocess.Popen",
                    side_effect=OSError("spawn failed"),
                )
            )
            with self.assertRaises(OSError):
                session._spawn(
                    "SYS",
                    {},
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )
        self.assertEqual(
            session.bound_tool_surface_fingerprint,
            "old-surface",
        )


if __name__ == "__main__":
    unittest.main()
