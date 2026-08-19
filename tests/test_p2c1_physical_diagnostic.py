"""P2C.1 frontend contract: Owner-only raw physical sensor diagnostics."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPAIR = ROOT / "static" / "repair.html"

PHYSICAL_HTML_BEGIN = "<!-- P2C1_PHYSICAL_DIAGNOSTIC_BEGIN -->"
PHYSICAL_HTML_END = "<!-- P2C1_PHYSICAL_DIAGNOSTIC_END -->"
PHYSICAL_JS_BEGIN = "// P2C1_PHYSICAL_DIAGNOSTIC_JS_BEGIN"
PHYSICAL_JS_END = "// P2C1_PHYSICAL_DIAGNOSTIC_JS_END"
PHYSICAL_CSS_BEGIN = "/* P2C1 Physical live diagnostic */"
PHYSICAL_CSS_END = "/* 生成锁急救 */"

FORBIDDEN_INTERPRETATION = (
    "正面朝上",
    "背面朝上",
    "横屏",
    "竖屏",
    "静止",
    "移动中",
    "黑暗",
    "明亮",
    "距离很近",
    "用户躺着",
    "用户正在走路",
)

FORBIDDEN_SIDE_EFFECTS = (
    "fetch(",
    "XMLHttpRequest",
    "sendBeacon",
    "WebSocket",
    "EventSource",
    "localStorage",
    "sessionStorage",
    "indexedDB",
    "prompt",
)

FORBIDDEN_SYNTAX = (
    "?.",
    "??",
    "crypto.randomUUID",
    "BigInt(",
)


def _between(source: str, start: str, end: str) -> str:
    start_i = source.index(start)
    end_i = source.index(end, start_i + len(start))
    return source[start_i:end_i + len(end)]


def _function_body(source: str, name: str) -> str:
    match = re.search(
        r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
        source,
    )
    if not match:
        raise AssertionError("missing function: " + name)
    depth = 1
    index = match.end()
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    if depth:
        raise AssertionError("unterminated function: " + name)
    return source[match.start():index]


class P2C1PhysicalDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = REPAIR.read_text(encoding="utf-8")
        cls.html = _between(cls.source, PHYSICAL_HTML_BEGIN, PHYSICAL_HTML_END)
        cls.js = _between(cls.source, PHYSICAL_JS_BEGIN, PHYSICAL_JS_END)
        cls.css = _between(cls.source, PHYSICAL_CSS_BEGIN, PHYSICAL_CSS_END)

    def test_a_card_and_owner_diagnostic_markers(self):
        self.assertIn("现实传感器诊断", self.html)
        self.assertIn("Elpis Canary · P2C.1 Physical State", self.html)
        self.assertIn("冻结", self.html)
        self.assertIn("复制 JSON", self.html)
        self.assertIn("继续", self.js)
        self.assertLess(
            self.source.index("<!-- P2B_NOTIFICATION_DIAG_END -->"),
            self.source.index(PHYSICAL_HTML_BEGIN),
        )
        self.assertLess(
            self.source.index(PHYSICAL_HTML_END),
            self.source.index("<!-- 生成锁急救 -->"),
        )

    def test_b_existing_p2a_p2b_cards_remain(self):
        self.assertIn("原生能力诊断", self.source)
        self.assertIn("原生通知诊断", self.source)

    def test_b_bridge_guard_and_json_return_contract(self):
        self.assertIn("window.ElpisPhysical", self.js)
        self.assertIn("typeof bridge.getPhysicalState === 'function'", self.js)
        self.assertIn("var raw = getPhysicalBridge().getPhysicalState();", self.js)
        self.assertIn("var snapshot = JSON.parse(raw);", self.js)
        self.assertIn("原生桥不可用", self.js)
        self.assertIn("读取失败", self.js)

    def test_c_single_snapshot_authority(self):
        self.assertIn("var currentPhysicalSnapshot = null;", self.js)
        self.assertEqual(self.js.count(".getPhysicalState()"), 1)
        self.assertIn("currentPhysicalSnapshot = snapshot;", self.js)
        self.assertIn("renderPhysicalSnapshot(currentPhysicalSnapshot);", self.js)
        self.assertIn("JSON.stringify(snapshot, null, 2)", self.js)
        copy_body = _function_body(self.js, "copyPhysicalJson")
        self.assertNotIn("getPhysicalState", copy_body)

    def test_d_required_fields_are_displayed(self):
        for field_id in (
            "physical-diag-monitoring",
            "physical-diag-schema",
            "physical-diag-updated-age",
            "physical-battery-available",
            "physical-battery-level",
            "physical-battery-charging",
            "physical-accelerometer-status",
            "physical-accelerometer-values",
            "physical-accelerometer-age",
            "physical-gyroscope-status",
            "physical-gyroscope-values",
            "physical-gyroscope-age",
            "physical-proximity-status",
            "physical-proximity-values",
            "physical-proximity-age",
            "physical-light-status",
            "physical-light-values",
            "physical-light-age",
        ):
            self.assertIn('id="' + field_id + '"', self.html)

    def test_e_polling_visibility_and_cleanup(self):
        self.assertRegex(
            self.js,
            r"setInterval\(function \(\) \{\s*readPhysicalState\(\);\s*\}, 500\)",
        )
        self.assertIn("physicalPollTimer !== null", self.js)
        self.assertIn("clearInterval(physicalPollTimer)", self.js)
        self.assertIn("document.addEventListener('visibilitychange'", self.js)
        self.assertIn("document.hidden", self.js)
        self.assertIn("window.addEventListener('pagehide'", self.js)
        self.assertIn("window.addEventListener('unload'", self.js)

    def test_f_freeze_and_copy_controls(self):
        self.assertIn('id="physical-diag-freeze"', self.html)
        self.assertIn('onclick="togglePhysicalFreeze()"', self.html)
        self.assertIn("physicalFrozen = true", self.js)
        self.assertIn("physicalFrozen = false", self.js)
        self.assertIn('id="physical-diag-copy"', self.html)
        self.assertIn('onclick="copyPhysicalJson()"', self.html)
        self.assertIn("function copyPhysicalJsonFallback(text)", self.js)
        fallback_body = _function_body(self.js, "copyPhysicalJsonFallback")
        self.assertIn("document.execCommand('copy')", fallback_body)
        copy_body = _function_body(self.js, "copyPhysicalJson")
        self.assertIn("navigator.clipboard.writeText(text)", copy_body)
        self.assertIn("pending.then", copy_body)
        self.assertIn("fallback();", copy_body)
        self.assertNotIn("getPhysicalState", copy_body)
        self.assertIn("JSON.stringify(currentPhysicalSnapshot, null, 2)", copy_body)

    def test_g_error_keeps_last_snapshot(self):
        body = _function_body(self.js, "readPhysicalState")
        self.assertIn("if (!currentPhysicalSnapshot) renderPhysicalEmpty();", body)
        self.assertIn("当前读取失败，保留最近一次有效快照。", body)

    def test_h_raw_panel_uses_same_rendered_snapshot(self):
        self.assertIn("原始 JSON", self.html)
        self.assertIn('id="physical-diag-json"', self.html)
        self.assertIn("setPhysicalText('physical-diag-json', JSON.stringify(snapshot, null, 2));", self.js)
        self.assertIn("renderPhysicalSnapshot(currentPhysicalSnapshot);", self.js)

    def test_i_no_interpretation_or_side_effects(self):
        physical = self.html + self.js
        for token in FORBIDDEN_INTERPRETATION:
            self.assertNotIn(token, physical, token)
        for token in FORBIDDEN_SIDE_EFFECTS:
            self.assertNotIn(token, physical, token)
        self.assertNotIn("ElpisNative", physical)
        self.assertNotIn("ElpisNotifications", physical)
        for token in FORBIDDEN_SYNTAX:
            self.assertNotIn(token, self.js, token)
        self.assertNotRegex(self.css, r"\bgap\s*:")


if __name__ == "__main__":
    unittest.main()
