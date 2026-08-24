"""Focused Wake freshness contract for the existing device-status consumer."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_get_device_status(db_row):
    source = (ROOT / "gateway.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="gateway.py")
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_get_device_status"
    )
    constant = ast.Assign(
        targets=[ast.Name(id="DEVICE_STATUS_STALE_AFTER_SEC", ctx=ast.Store())],
        value=ast.Constant(value=10 * 60),
    )
    namespace = {"get_db": lambda: _FakeConnection(db_row)}
    module = ast.fix_missing_locations(
        ast.Module(body=[constant, function], type_ignores=[]),
    )
    exec(compile(module, "gateway.py", "exec"), namespace)
    return namespace["_get_device_status"]


class _FakeConnection:
    def __init__(self, row):
        self.row = row

    def execute(self, *_args):
        return self

    def fetchone(self):
        return self.row

    def close(self):
        return None


class RealityLifecycleWakeTests(unittest.TestCase):
    def test_stale_status_is_not_presented_as_current_fact(self):
        get_device_status = _load_get_device_status({
            "battery_percent": 53,
            "battery_charging": 0,
            "charge_type": "none",
            "temp_c": 30.0,
            "screen_today_minutes": 12,
            "created_at": "2026-08-24 13:00:00",
            "age_sec": 10 * 60 + 1,
        })
        result = get_device_status()
        self.assertIn("设备状态已过期", result)
        self.assertIn("最后观察时间", result)
        self.assertIn("旧电量", result)
        self.assertNotIn("电量 53%", result)

    def test_recent_status_remains_available_with_observation_time(self):
        get_device_status = _load_get_device_status({
            "battery_percent": 53,
            "battery_charging": 1,
            "charge_type": "usb",
            "temp_c": 30.0,
            "screen_today_minutes": 12,
            "created_at": "2026-08-24 13:29:00",
            "age_sec": 60,
        })
        result = get_device_status()
        self.assertIn("电量 53%", result)
        self.assertIn("上次上报", result)
        self.assertIn("2026-08-24 13:29:00", result)

    def test_wake_consumer_has_fail_closed_stale_branch_before_value_formatting(self):
        source = (ROOT / "gateway.py").read_text(encoding="utf-8")
        tree = ast.parse(source, filename="gateway.py")
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_get_device_status"
        )
        stale_tests = [
            node
            for node in function.body
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "age"
        ]
        self.assertTrue(stale_tests)
        stale_branch = stale_tests[0]
        self.assertIn(
            "DEVICE_STATUS_STALE_AFTER_SEC",
            ast.unparse(stale_branch),
        )
        function_text = ast.get_source_segment(source, function)
        self.assertLess(
            function_text.index("DEVICE_STATUS_STALE_AFTER_SEC"),
            function_text.index("bp = int"),
        )


if __name__ == "__main__":
    unittest.main()
