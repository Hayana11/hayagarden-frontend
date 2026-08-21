"""M3-02A isolated tests for the Internal MCP service activation guard."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "activate-internal-mcp.sh"
UNIT = ROOT / "deploy" / "systemd" / "internal-mcp.service"
SHA = "a" * 40


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class InternalMcpActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="m3-02a-activation-")
        self.root = Path(self.temp.name) / "frontend"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        (self.root / "internal-mcp-server.js").write_text("// test server\n", encoding="utf-8")
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        (self.state / "DEPLOYED_SHA").write_text(SHA + "\n", encoding="utf-8")
        self.systemd = Path(self.temp.name) / "systemd"
        self.systemd.mkdir()
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.temp.name) / "systemctl.log"
        self.fake_state = Path(self.temp.name) / "fake-state"
        self.fake_state.mkdir()

        write_executable(
            self.bin / "git",
            """#!/usr/bin/env bash
set -eu
case " $* " in
  *" status "*) [[ "${FAKE_DIRTY:-0}" == 1 ]] && printf '%s\n' ' M dirty.txt'; exit 0 ;;
  *" rev-parse "*) printf '%s\n' "${FAKE_HEAD}"; exit 0 ;;
esac
exit 2
""",
        )
        write_executable(
            self.bin / "systemctl",
            """#!/usr/bin/env bash
set -eu
state="${FAKE_STATE_DIR}"
log="${FAKE_SYSTEMCTL_LOG}"
mkdir -p "$state"
printf '%s\n' "$*" >> "$log"
action="${1:-}"
last="${!#}"
case "$action" in
  is-enabled)
    [[ -f "$state/enabled" ]] && { cat "$state/enabled"; exit 0; }
    printf '%s\n' disabled
    exit 1
    ;;
  is-active)
    [[ -f "$state/active" ]] && { cat "$state/active"; exit 0; }
    printf '%s\n' inactive
    exit 3
    ;;
  daemon-reload) exit 0 ;;
  enable) printf '%s\n' enabled > "$state/enabled" ;;
  disable) rm -f "$state/enabled" ;;
  start) printf '%s\n' active > "$state/active" ;;
  stop) rm -f "$state/active" ;;
  mask) printf '%s\n' masked > "$state/enabled" ;;
esac
""",
        )
        write_executable(
            self.bin / "node",
            """#!/usr/bin/env bash
set -eu
bash "$1"
""",
        )
        self.readiness = Path(self.temp.name) / "readiness.sh"
        write_executable(
            self.readiness,
            """#!/usr/bin/env bash
printf '%s\n' readiness >> "${FAKE_SYSTEMCTL_LOG}"
exit "${FAKE_READINESS_RC:-0}"
""",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def env(self, *, head=SHA, dirty="0", readiness="0") -> dict[str, str]:
        return {
            "FRONTEND_ROOT": str(self.root),
            "INTERNAL_MCP_STATE_DIR": str(self.state),
            "INTERNAL_MCP_SYSTEMD_DIR": str(self.systemd),
            "INTERNAL_MCP_UNIT_SOURCE": str(UNIT),
            "INTERNAL_MCP_READINESS_BIN": str(self.readiness),
            "SYSTEMCTL_BIN": str(self.bin / "systemctl"),
            "GIT_BIN": str(self.bin / "git"),
            "NODE_BIN": str(self.bin / "node"),
            "FAKE_STATE_DIR": str(self.fake_state),
            "FAKE_SYSTEMCTL_LOG": str(self.log),
            "FAKE_HEAD": head,
            "FAKE_DIRTY": dirty,
            "FAKE_READINESS_RC": readiness,
        }

    def run_activation(self, env: dict[str, str], expected=SHA) -> subprocess.CompletedProcess[str]:
        command = ["bash", str(SCRIPT), expected]
        if os.geteuid() != 0:
            if shutil.which("sudo") is None:
                self.skipTest("activation tests require sudo on non-root runners")
            command = [
                "sudo", "-n", "env",
                *[f"{key}={value}" for key, value in env.items()],
                *command,
            ]
            return subprocess.run(command, text=True, capture_output=True)
        return subprocess.run(command, env={**os.environ, **env}, text=True, capture_output=True)

    def test_unit_contract_is_fixed(self) -> None:
        text = UNIT.read_text(encoding="utf-8")
        for line in (
            "WorkingDirectory=/opt/frontend",
            "ExecStart=/usr/bin/node /opt/frontend/internal-mcp-server.js",
            "Environment=INTERNAL_MCP_PORT=3101",
            "Environment=TODO_INTERNAL_DB_PATH=/opt/frontend/memories.db",
            "Environment=UH_A0_REPO_ROOT=/opt/frontend",
            "Environment=NODE_ENV=production",
            "Restart=always",
            "StandardOutput=append:/opt/frontend/internal-mcp.log",
            "StandardError=append:/opt/frontend/internal-mcp.log",
        ):
            self.assertIn(line, text)
        self.assertNotIn("0.0.0.0", text)
        self.assertNotIn("3100", text)

    def test_sha_and_dirty_guards_touch_no_systemd(self) -> None:
        for overrides in (
            {"head": "b" * 40},
            {"dirty": "1"},
        ):
            result = self.run_activation(self.env(**overrides))
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.log.exists() and self.log.read_text(encoding="utf-8").strip())

        bad_state = self.env()
        (self.state / "DEPLOYED_SHA").write_text("b" * 40 + "\n", encoding="utf-8")
        result = self.run_activation(bad_state)
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_first_activation_failure_leaves_no_unit(self) -> None:
        result = self.run_activation(self.env(readiness="1"))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.systemd / "internal-mcp.service").exists())
        self.assertFalse((self.fake_state / "active").exists())
        self.assertFalse((self.fake_state / "enabled").exists())

    def test_existing_unit_is_restored_after_readiness_failure(self) -> None:
        unit_path = self.systemd / "internal-mcp.service"
        unit_path.write_text("old-unit" + chr(10), encoding="utf-8")
        (self.fake_state / "active").write_text("active" + chr(10), encoding="utf-8")
        (self.fake_state / "enabled").write_text("enabled" + chr(10), encoding="utf-8")
        marker = self.state / "internal-mcp-activated-sha"
        marker.write_text(("b" * 40) + chr(10), encoding="utf-8")
        result = self.run_activation(self.env(readiness="1"))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unit_path.read_text(encoding="utf-8"), "old-unit" + chr(10))
        self.assertEqual((self.fake_state / "active").read_text(encoding="utf-8").strip(), "active")
        self.assertEqual((self.fake_state / "enabled").read_text(encoding="utf-8").strip(), "enabled")
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "b" * 40)
    def test_cross_sha_activation_restarts_same_unit_and_records_sha(self) -> None:
        unit_path = self.systemd / "internal-mcp.service"
        unit_path.write_bytes(UNIT.read_bytes())
        (self.fake_state / "active").write_text("active" + chr(10), encoding="utf-8")
        (self.fake_state / "enabled").write_text("enabled" + chr(10), encoding="utf-8")
        (self.state / "internal-mcp-activated-sha").write_text(("b" * 40) + chr(10), encoding="utf-8")
        result = self.run_activation(self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("start internal-mcp.service", log)
        self.assertEqual((self.state / "internal-mcp-activated-sha").read_text(encoding="utf-8").strip(), SHA)

    def test_repeated_ready_activation_is_idempotent(self) -> None:
        unit_path = self.systemd / "internal-mcp.service"
        unit_path.write_bytes(UNIT.read_bytes())
        (self.fake_state / "active").write_text("active" + chr(10), encoding="utf-8")
        (self.fake_state / "enabled").write_text("enabled" + chr(10), encoding="utf-8")
        (self.state / "internal-mcp-activated-sha").write_text(SHA + chr(10), encoding="utf-8")
        result = self.run_activation(self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log.read_text(encoding="utf-8")
        self.assertNotIn("daemon-reload", log)
        self.assertNotIn(" enable ", f" {log} ")
        self.assertNotIn(" start ", f" {log} ")

if __name__ == "__main__":
    unittest.main()
