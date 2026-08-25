"""Focused UH-A0 MCP startup readiness environment contract."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident


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


class UhA0McpStartupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mcp_path = str(Path(self.tmp.name) / "mcp.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_scoped_env_contract(self):
        base = {"CLAUDE_CODE_OAUTH_TOKEN": "token"}
        uh_env = cc_resident._prepare_spawn_env(
            base, cc_resident.TOOL_PROFILE_UH_A0
        )
        self.assertEqual(uh_env["MCP_CONNECTION_NONBLOCKING"], "0")
        self.assertEqual(uh_env["MCP_CONNECT_TIMEOUT_MS"], "15000")
        self.assertNotIn("MCP_CONNECTION_NONBLOCKING", base)
        self.assertNotIn("MCP_CONNECT_TIMEOUT_MS", base)

        for profile in (
            cc_resident.TOOL_PROFILE_LEGACY,
            cc_resident.TOOL_PROFILE_TEXT_ONLY,
        ):
            untouched = {"EXISTING": "value"}
            self.assertIs(
                cc_resident._prepare_spawn_env(untouched, profile),
                untouched,
            )
            self.assertEqual(
                untouched,
                {"EXISTING": "value"},
            )

    def test_all_uh_a0_spawn_paths_pass_readiness_env(self):
        calls = (
            ("_spawn", {"reason": "test"}),
            ("spawn_resumable", {
                "resume_session_id": "resume-session",
                "reason": "test",
            }),
            ("spawn_fresh_named", {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "reason": "test",
            }),
        )
        for method_name, kwargs in calls:
            with self.subTest(method=method_name):
                session = cc_resident.ResidentSession(
                    self.tmp.name, "", self.mcp_path
                )
                session._build_spawn_tool_flags = mock.Mock(
                    return_value={
                        "tools": "Read",
                        "extra": ["--allowedTools", "Read"],
                        "surface_allowed": "Read",
                        "mcp_path": self.mcp_path,
                        "surface_fingerprint": "surface",
                    }
                )
                session._capture_tool_surface = mock.Mock()
                popen = mock.Mock(return_value=_FakeProcess())
                with mock.patch.object(
                    cc_resident.subprocess, "Popen", popen
                ), mock.patch(
                    "chat.cc_model.cc_model_snapshot",
                    return_value=("model", "model-id", []),
                ), mock.patch(
                    "chat.cc_runtime.require_pinned_claude_version",
                ), mock.patch(
                    "chat.cc_runtime.claude_cmd",
                    side_effect=lambda *args: list(args),
                ):
                    if method_name == "_spawn":
                        session._spawn(
                            "SYS",
                            {"CLAUDE_CODE_OAUTH_TOKEN": "token"},
                            tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                            **kwargs,
                        )
                    else:
                        getattr(session, method_name)(
                            "SYS",
                            {"CLAUDE_CODE_OAUTH_TOKEN": "token"},
                            tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                            **kwargs,
                        )
                child_env = popen.call_args.kwargs["env"]
                self.assertEqual(
                    child_env["MCP_CONNECTION_NONBLOCKING"], "0"
                )
                self.assertEqual(
                    child_env["MCP_CONNECT_TIMEOUT_MS"], "15000"
                )

    def test_legacy_and_text_only_spawn_envs_are_unchanged(self):
        for profile in (
            cc_resident.TOOL_PROFILE_LEGACY,
            cc_resident.TOOL_PROFILE_TEXT_ONLY,
        ):
            env = {"EXISTING": "value"}
            with self.subTest(profile=profile):
                self.assertIs(cc_resident._prepare_spawn_env(env, profile), env)
                self.assertNotIn("MCP_CONNECTION_NONBLOCKING", env)
                self.assertNotIn("MCP_CONNECT_TIMEOUT_MS", env)


if __name__ == "__main__":
    unittest.main()
