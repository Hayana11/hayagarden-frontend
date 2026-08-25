"""Focused bedside-only /light/status product contract tests."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "tools" / "mijia_daemon.py"


class MijiaDaemonLightStatusTests(unittest.TestCase):
    def test_single_physical_bedside_status_contract(self):
        calls = []
        fake_light_control = types.ModuleType("light_control")

        def light_status(did, *, zone):
            calls.append((did, zone))
            return {"zone": zone, "power": True}

        fake_light_control.light_status = light_status
        previous = sys.modules.get("light_control")
        sys.modules["light_control"] = fake_light_control
        try:
            spec = importlib.util.spec_from_file_location("s4_mijia_daemon", DAEMON)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module._load_dids = lambda: ("bedroom2-physical-did", "bedroom2-physical-did")

            self.assertEqual(module.main_did(), module.bedside_did())
            response = module.app.test_client().get("/light/status")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()

            self.assertTrue(payload["ok"])
            self.assertIn("bedside", payload["result"])
            self.assertNotIn("main", payload["result"])
            self.assertEqual(payload["result"]["bedside"], {"zone": "bedside", "power": True})
            self.assertEqual(calls, [("bedroom2-physical-did", "bedside")])
        finally:
            if previous is None:
                sys.modules.pop("light_control", None)
            else:
                sys.modules["light_control"] = previous

    def test_control_compatibility_and_main_did_configuration_remain(self):
        source = DAEMON.read_text(encoding="utf-8")
        self.assertIn('bedroom2_did', source)
        for route in (
            "/light/on",
            "/light/off",
            "/light/brightness",
            "/light/color_temp",
            "/light/main/on",
            "/light/main/off",
            "/light/bedside/on",
            "/light/bedside/off",
            "/light/bedside/warm",
            "/light/bedside/neutral",
            "/light/all/on",
            "/light/all/off",
        ):
            self.assertIn(route, source)


if __name__ == "__main__":
    unittest.main()
