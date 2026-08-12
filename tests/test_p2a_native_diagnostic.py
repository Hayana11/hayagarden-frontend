"""P2A-FE contract: repair.html NativeBridge Lite diagnostic card."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPAIR = ROOT / "static" / "repair.html"

# Markers for the P2A diagnostic block only (pre-existing Pocket URL helpers stay out of scope).
DIAG_START = "/* ===== P2A NativeBridge Lite diagnostic"
DIAG_END = "</script>"
HTML_START = "<!-- 原生能力诊断 -->"
HTML_END = "<!-- 生成锁急救 -->"
CSS_START = "/* 原生能力诊断（P2A NativeBridge Lite） */"
CSS_END = "/* 生成锁急救 */"

FORBIDDEN_NATIVE = (
    "takeScreenshot",
    "ScreenCapture",
    "setPocketConfig",
    "getPocketStatus",
    "openPocketBrowser",
    "ForegroundService",
    "NotificationWorker",
    "AppTracker",
)

FORBIDDEN_SYNTAX = (
    "?.",
    "??",
    ".at(",
    "replaceAll(",
    "Promise.any",
    "crypto.randomUUID",
)

FORBIDDEN_UPLOAD = (
    "fetch(",
    "sendBeacon",
    "WebSocket",
)


def _slice_between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


class P2ANativeDiagnosticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = REPAIR.read_text(encoding="utf-8")
        cls.diag_js = _slice_between(cls.html, DIAG_START, DIAG_END)
        cls.diag_html = _slice_between(cls.html, HTML_START, HTML_END)
        cls.diag_css = _slice_between(cls.html, CSS_START, CSS_END)

    def test_a_repair_mentions_elpis_native(self):
        self.assertIn("window.ElpisNative", self.html)

    def test_card_placement_between_status_and_genlock(self):
        status_i = self.html.index("SERVICE STATUS")
        diag_i = self.html.index(HTML_START)
        fix_i = self.html.index("聊天卡在「仍在生成」")
        self.assertLess(status_i, diag_i)
        self.assertLess(diag_i, fix_i)
        self.assertIn("原生能力诊断", self.diag_html)
        self.assertIn("Elpis Canary · NativeBridge Lite", self.diag_html)

    def test_b_safe_read_methods_present(self):
        for name in (
            "getBattery",
            "getScreenTime",
            "hasUsageAccess",
            "isIgnoringBatteryOptimizations",
        ):
            self.assertIn(name, self.diag_js, name)

    def test_c_explicit_action_methods_present(self):
        self.assertIn("openUsageAccessSettings", self.diag_js)
        self.assertIn("requestIgnoreBatteryOptimizations", self.diag_js)

    def test_d_no_legacy_native_surface_in_diag_block(self):
        blob = self.diag_html + self.diag_js + self.diag_css
        for token in FORBIDDEN_NATIVE:
            self.assertNotIn(token, blob, token)
        # "Pocket" as a capability token must not appear in the diagnostic block.
        self.assertNotIn("Pocket", blob)

    def test_e_browser_fallback_guards_method_calls(self):
        self.assertIn("function getElpisNative()", self.diag_js)
        self.assertIn("function hasNativeMethod(name)", self.diag_js)
        self.assertIn("typeof bridge[name] === 'function'", self.diag_js)
        self.assertIn("仅 App 可用", self.diag_js)
        self.assertIn("badge-warn", self.diag_js)

    def test_f_refresh_does_not_open_settings(self):
        # Extract refreshNativeDiagnostic function body.
        m = re.search(
            r"function refreshNativeDiagnostic\(\)\s*\{",
            self.diag_js,
        )
        self.assertIsNotNone(m)
        start = m.end()
        depth = 1
        i = start
        while i < len(self.diag_js) and depth:
            if self.diag_js[i] == "{":
                depth += 1
            elif self.diag_js[i] == "}":
                depth -= 1
            i += 1
        body = self.diag_js[start : i - 1]
        self.assertNotIn("openUsageAccessSettings", body)
        self.assertNotIn("requestIgnoreBatteryOptimizations", body)
        self.assertNotIn("requestUsageAccess(", body)
        self.assertNotIn("requestBatteryOptimizationExemption(", body)
        # Safe reads only.
        self.assertIn("refreshNativeBattery()", body)
        self.assertIn("refreshNativeUsageAndScreenTime()", body)
        self.assertIn("refreshNativeDoze()", body)

    def test_g_settings_actions_only_in_click_handlers(self):
        self.assertIn("function requestUsageAccess()", self.diag_js)
        self.assertIn("function requestBatteryOptimizationExemption()", self.diag_js)
        usage = re.search(
            r"function requestUsageAccess\(\)\s*\{(.*?)\n\}",
            self.diag_js,
            re.S,
        )
        doze = re.search(
            r"function requestBatteryOptimizationExemption\(\)\s*\{(.*?)\n\}",
            self.diag_js,
            re.S,
        )
        self.assertIsNotNone(usage)
        self.assertIsNotNone(doze)
        self.assertIn("openUsageAccessSettings", usage.group(1))
        self.assertIn("requestIgnoreBatteryOptimizations", doze.group(1))
        # Buttons wire to those handlers only.
        self.assertIn('onclick="requestUsageAccess()"', self.diag_html)
        self.assertIn('onclick="requestBatteryOptimizationExemption()"', self.diag_html)

    def test_h_no_upload_in_diag_block(self):
        for token in FORBIDDEN_UPLOAD:
            self.assertNotIn(token, self.diag_js, token)
            self.assertNotIn(token, self.diag_html, token)

    def test_i_chrome78_safe_diag_js(self):
        for token in FORBIDDEN_SYNTAX:
            self.assertNotIn(token, self.diag_js, token)
            self.assertNotIn(token, self.diag_css, token)

    def test_no_flex_gap_in_new_css(self):
        # New diagnostic CSS must not rely on flex gap (Chrome 78).
        self.assertNotRegex(self.diag_css, r"\bgap\s*:")

    def test_sw_js_untouched_by_this_pr_scope(self):
        # Contract reminder: this PR must not modify static/sw.js.
        # Existence check only — content changes would be a separate diff.
        self.assertTrue((ROOT / "static" / "sw.js").is_file())


if __name__ == "__main__":
    unittest.main()
