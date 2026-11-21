from __future__ import annotations

import re
import unittest
from pathlib import Path

from tools.cc_capability_adapter import build_uh_a0_mcp_config

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "systemd" / "capability-mcp.service"
ACTIVATE = ROOT / "scripts" / "activate-capability-mcp.sh"
PRODUCTION_ROOT = "/opt/frontend"


def _unit_lines() -> set[str]:
    return {line.strip() for line in UNIT.read_text(encoding="utf-8").splitlines() if line.strip()}


def _unit_environment() -> dict[str, str]:
    env = {}
    for line in _unit_lines():
        if line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            env[key] = value
    return env


class CapabilityMcpServiceContractTests(unittest.TestCase):
    def test_activation_contract_lines_are_all_in_the_unit(self):
        script = ACTIVATE.read_text(encoding="utf-8")
        block = script[script.index("for required in"):script.index("\ndo\n")]
        required = re.findall(r"'([^']+)'", block)
        self.assertIn("PartOf=frontend-gw.service", required)
        self.assertIn("EnvironmentFile=/opt/frontend/.env", required)
        missing = [line for line in required if line not in _unit_lines()]
        self.assertEqual(missing, [])

    def test_unit_environment_matches_the_per_spawn_stdio_proxy(self):
        # The resident service must see the same adapter paths the stdio proxy
        # was handed per spawn; only the lease path moves to a request header.
        stdio_env = build_uh_a0_mcp_config(env={})["mcpServers"]["capability"]["env"]
        repo_root = stdio_env["UH_A0_REPO_ROOT"]
        unit_env = _unit_environment()
        for key, value in stdio_env.items():
            if key == "UH_A0_TURN_LEASE_PATH":
                self.assertNotIn(key, unit_env)
                continue
            self.assertEqual(unit_env.get(key), value.replace(repo_root, PRODUCTION_ROOT), key)
        self.assertEqual(unit_env["CAPABILITY_MCP_PORT"], "3102")
        self.assertEqual(unit_env["TZ"], "Asia/Shanghai")

    def test_unit_runs_the_capability_proxy_on_loopback_port(self):
        self.assertIn("ExecStart=/usr/bin/node /opt/frontend/capability-proxy-mcp-server.js", _unit_lines())
        server = (ROOT / "capability-proxy-mcp-server.js").read_text(encoding="utf-8")
        self.assertIn("host = '127.0.0.1'", server)


if __name__ == "__main__":
    unittest.main()
