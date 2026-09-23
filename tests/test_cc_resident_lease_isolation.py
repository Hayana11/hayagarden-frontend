"""Focused regression tests for resident-owned UH-A0 turn leases."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
from tools import cc_capability_adapter, external_mcp_surface
from tools.execution_fence import UH_A0TurnRuntime, read_current_turn_lease
from tools.lease_signer import issue_turn_lease


class ResidentLeaseIsolationTests(unittest.TestCase):
    def lease(self, turn_id):
        return issue_turn_lease(
            turn_id=turn_id,
            turn_mode="chat",
            issued_from="default_policy",
            requested_capabilities=(),
            approval_ids=(),
            issued_at="2026-09-01T00:00:00Z",
        )

    def test_collision_reproduced_with_the_old_shared_path(self):
        """The old global path lets B erase A's logically active lease."""
        with tempfile.TemporaryDirectory() as tmp:
            shared_path = Path(tmp) / ".uh-a0-current-turn-lease.json"
            resident_a = cc_resident.ResidentSession(tmp, "", "")
            resident_b = cc_resident.ResidentSession(tmp, "", "")
            resident_a._uh_a0_turn_lease_path = str(shared_path)
            resident_b._uh_a0_turn_lease_path = str(shared_path)
            runtime_a = UH_A0TurnRuntime(shared_path, session_id="resident-a")
            runtime_b = UH_A0TurnRuntime(shared_path, session_id="resident-b")

            runtime_a.start_turn(self.lease("turn-a"), session_id="resident-a")
            runtime_b.start_turn(self.lease("turn-b"), session_id="resident-b")
            self.assertEqual(read_current_turn_lease(shared_path)[1]["turn_id"], "turn-b")

            runtime_b.end_turn(turn_id="turn-b")
            self.assertIsNone(read_current_turn_lease(shared_path)[0])
            self.assertEqual(runtime_a.active_turn_id, "turn-a")

    def test_two_resident_paths_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident_a = cc_resident.ResidentSession(tmp, "", "")
            resident_b = cc_resident.ResidentSession(tmp, "", "")
            resident_a._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            resident_b._tool_profile = cc_resident.TOOL_PROFILE_UH_A0

            self.assertNotEqual(
                resident_a._uh_a0_turn_lease_path,
                resident_b._uh_a0_turn_lease_path,
            )

    def test_same_resident_path_stable_across_hot_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident = cc_resident.ResidentSession(tmp, "", "")
            resident._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            path = resident._uh_a0_turn_lease_path

            first = resident._prepare_spawn_env({"UH_A0_TURN_LEASE_PATH": "/stale"})
            second = resident._prepare_spawn_env({"UH_A0_TURN_LEASE_PATH": "/other"})

            self.assertEqual(path, resident._uh_a0_turn_lease_path)
            self.assertEqual(first["UH_A0_TURN_LEASE_PATH"], path)
            self.assertEqual(second["UH_A0_TURN_LEASE_PATH"], path)

    def test_spawn_config_matches_resident(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident = cc_resident.ResidentSession(tmp, "", "")
            resident._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            seen = {}

            def capture_spawn_env(*, env, **_kwargs):
                seen["env"] = dict(env)
                return {
                    "built_in_tools_csv": "",
                    "spawn_extra_args": [],
                    "surface_allowlist_csv": "",
                    "mcp_config_path": "",
                    "physical_surface_fingerprint": "synthetic-surface",
                    "turn_lease_path": resident._uh_a0_turn_lease_path,
                }

            flags = {}
            with mock.patch(
                "tools.cc_capability_adapter.build_uh_a0_spawn_plan",
                side_effect=capture_spawn_env,
            ):
                flags = resident._build_spawn_tool_flags(
                    env={"UH_A0_TURN_LEASE_PATH": "/stale", "KEEP": "yes"},
                )

            self.assertEqual(seen["env"]["UH_A0_TURN_LEASE_PATH"], resident._uh_a0_turn_lease_path)
            config = cc_capability_adapter.build_uh_a0_mcp_config(env=seen["env"])
            self.assertEqual(
                config["mcpServers"]["external"]["env"]["UH_A0_TURN_LEASE_PATH"],
                resident._uh_a0_turn_lease_path,
            )
            self.assertEqual(flags["surface_fingerprint"], "synthetic-surface")

    def test_claude_spawn_environment_matches_resident(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident = cc_resident.ResidentSession(tmp, "", "")
            captured = {}
            def fake_spawn_flags(env):
                captured["env"] = dict(env)
                return {
                    "tools": "",
                    "extra": [],
                    "surface_allowed": "",
                    "mcp_path": None,
                    "surface_fingerprint": "synthetic-surface",
                }

            resident._build_spawn_tool_flags = mock.Mock(side_effect=fake_spawn_flags)
            with mock.patch(
                 "chat.cc_runtime.require_managed_claude_runtime",
                 return_value="2.1.280",
             ), mock.patch(
                 "chat.cc_runtime.claude_cmd_for_version",
                 side_effect=lambda _version, *a, **k: ["claude", *a],
             ), \
                 mock.patch("chat.cc_model.cc_model_snapshot", return_value=("", "default", [])), \
                 mock.patch("cc_resident.subprocess.Popen") as popen, \
                 mock.patch("tools.cc_tool_surface.capture_tool_surface_snapshot", return_value={}):
                resident._spawn(
                    "SYS",
                    {"UH_A0_TURN_LEASE_PATH": "/stale", "KEEP": "yes"},
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )

            self.assertEqual(
                popen.call_args.kwargs["env"]["UH_A0_TURN_LEASE_PATH"],
                resident._uh_a0_turn_lease_path,
            )
            self.assertEqual(
                captured["env"]["UH_A0_TURN_LEASE_PATH"],
                resident._uh_a0_turn_lease_path,
            )

    def test_resident_b_end_does_not_clear_resident_a(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident_a = cc_resident.ResidentSession(tmp, "", "")
            resident_b = cc_resident.ResidentSession(tmp, "", "")
            runtime_a = UH_A0TurnRuntime(resident_a._uh_a0_turn_lease_path, session_id="resident-a")
            runtime_b = UH_A0TurnRuntime(resident_b._uh_a0_turn_lease_path, session_id="resident-b")

            runtime_a.start_turn(self.lease("turn-a"), session_id="resident-a")
            runtime_b.start_turn(self.lease("turn-b"), session_id="resident-b")
            runtime_b.end_turn(turn_id="turn-b")

            lease_a, record_a = read_current_turn_lease(resident_a._uh_a0_turn_lease_path)
            self.assertEqual(lease_a["turn_id"], "turn-a")
            self.assertEqual(record_a["turn_id"], "turn-a")
            runtime_a.end_turn(turn_id="turn-a")

    def test_external_surface_turn_id_uses_resident_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            resident = cc_resident.ResidentSession(tmp, "", "")
            runtime = UH_A0TurnRuntime(resident._uh_a0_turn_lease_path, session_id="resident-a")
            runtime.start_turn(self.lease("turn-a"), session_id="resident-a")
            try:
                with mock.patch.dict(
                    os.environ,
                    {"UH_A0_TURN_LEASE_PATH": resident._uh_a0_turn_lease_path},
                    clear=False,
                ):
                    observed_turn_id = external_mcp_surface._turn_id_from_environment()
                    self.assertEqual(observed_turn_id, "turn-a")
                    with mock.patch.object(
                        external_mcp_surface,
                        "open_external_mcp_production",
                        return_value=mock.MagicMock(),
                    ), mock.patch.object(
                        external_mcp_surface,
                        "current_external_tools",
                        return_value=[],
                    ):
                        result = external_mcp_surface.invoke_external_surface(
                            "synthetic_missing_tool",
                            {},
                            turn_id=observed_turn_id,
                        )
                self.assertEqual(result["status"], "FAILED_PRE_CALL")
                self.assertEqual(result["error"]["code"], "SURFACE_TOOL_UNAVAILABLE")
            finally:
                runtime.end_turn(turn_id="turn-a")


if __name__ == "__main__":
    unittest.main()
