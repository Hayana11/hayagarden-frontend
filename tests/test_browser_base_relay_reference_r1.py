from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "relay"
SOURCE = (RELAY / "relay.py").read_text(encoding="utf-8")
CLIENT = (RELAY / "client.html").read_text(encoding="utf-8")
LOGIN = (RELAY / "login.html").read_text(encoding="utf-8")
ENV = (RELAY / ".env.example").read_text(encoding="utf-8")
LICENSE = (RELAY / "LICENSE.ai-social-browser").read_text(encoding="utf-8")
SERVICE = (ROOT / "deploy/systemd/hayagarden-browser-base.service").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_reference_relay_structure_and_ownership():
    assert len(SOURCE.splitlines()) >= 900
    assert "async with serve(" in SOURCE
    assert "self.chrome_proc = subprocess.Popen(args)" in SOURCE
    assert "self.chrome_proc.terminate()" in SOURCE
    assert "Page.startScreencast" in SOURCE
    assert "Page.captureScreenshot" in SOURCE
    assert "_reconnect_loop" in SOURCE
    assert '"switch_tab"' in SOURCE
    assert "_calibrate_x11_input" in SOURCE
    assert "XTestFakeButtonEvent" in SOURCE
    assert "XTestFakeKeyEvent" in SOURCE


def test_fixed_browser_endpoints_profile_and_display():
    assert 'DEFAULT_HOST = "127.0.0.1"' in SOURCE
    assert "DEFAULT_PORT = 8271" in SOURCE
    assert "CDP_PORT = 9333" in SOURCE
    assert 'DEFAULT_CHROME_BIN = "/snap/bin/chromium"' in SOURCE
    assert 'PROFILE_DIR = Path("/root/snap/chromium/common/hayagarden-browser-base-profile")' in SOURCE
    assert '"--remote-debugging-address=127.0.0.1"' in SOURCE
    assert '"--remote-debugging-port={CDP_PORT}"' in SOURCE
    assert '"--user-data-dir={PROFILE_DIR}"' in SOURCE
    assert '"--window-size={chrome_window_width},{chrome_window_height}"' in SOURCE
    assert '"--window-position=0,0"' in SOURCE
    assert '"--lang=zh-CN"' in SOURCE
    assert '"--disable-blink-features=AutomationControlled"' in SOURCE
    assert '"--disable-backgrounding-occluded-windows"' in SOURCE
    assert '"--disable-renderer-backgrounding"' in SOURCE
    assert '"--disable-background-timer-throttling"' in SOURCE
    assert "--proxy-server={_PROXY_SERVER}" in SOURCE

    exec_line = next(line for line in SERVICE.splitlines() if line.startswith("ExecStart="))
    assert SERVICE.count("ExecStartPre=/usr/bin/install -d -o root -g root -m 0700 /run/user/0") == 1
    assert "xvfb-run -a -f /root/snap/chromium/common/hayagarden-browser-base.Xauthority -s \"-screen 0 1920x1080x24\"" in exec_line
    assert "/usr/bin/python3.11 /opt/frontend/relay/relay.py" in exec_line
    assert "/snap/bin/chromium" not in exec_line
    assert "browser-relay.service" not in SERVICE
    assert 'Environment=TZ=Asia/Shanghai' in SERVICE


def test_relay_pages_auth_dependency_and_generic_scope():
    assert 'Path(__file__).parent / "client.html"' in SOURCE
    assert 'Path(__file__).parent / "login.html"' in SOURCE
    assert "HttpOnly" in SOURCE
    assert "BROWSER_RELAY_PASS=" in ENV
    assert "websockets>=14" in REQUIREMENTS
    assert "BROWSER_RELAY_PROXY_SERVER=" in ENV
    assert "BROWSER_RELAY_HOST=0.0.0.0" not in ENV
    assert "/etc/hayagarden-browser-base.env" in ENV
    assert "/etc/browser-relay.env" not in ENV
    assert "MIT License" in LICENSE
    assert "Copyright (c) 2026 blueberriely" in LICENSE
    assert "LICENSE.ai-social-browser" in SOURCE

    forbidden = (
        "taobao", "tmall", "xiaohongshu", "twitter", "shopping", "cart",
        "checkout", "product", "feed", "like", "comment",
    )
    runtime_text = "\n".join((SOURCE, CLIENT, LOGIN, ENV, SERVICE)).lower()
    for term in forbidden:
        assert term not in runtime_text, term


def test_environment_file_path_has_one_authority():
    assert "EnvironmentFile=-/etc/hayagarden-browser-base.env" in SERVICE
    assert "/etc/hayagarden-browser-base.env" in ENV
    assert "/etc/browser-relay.env" not in ENV


def test_service_starts_one_relay_and_not_chromium():
    assert "browser-relay.service" not in SERVICE
    assert SERVICE.count("ExecStart=") == 1
    assert "xvfb-run" in SERVICE
    assert "relay.py" in SERVICE
    assert "/snap/bin/chromium" not in SERVICE
    assert "chromium --" not in SERVICE


def test_x11_calibration_wait_is_bounded_and_requires_trusted_event():
    assert 'calibration_target = self._open_target(calibration_url)' in SOURCE
    assert 'calibration_target = self._open_target("about:blank")' not in SOURCE
    assert '"about:blank",' in SOURCE
    assert SOURCE.count('calibration_html = (') == 1
    assert "await asyncio.sleep(0.15)" not in SOURCE
    assert "await asyncio.sleep(0.08)" not in SOURCE
    assert "calibration_ready_deadline = loop.time() + 1.0" in SOURCE
    assert "document.title" in SOURCE
    assert "relay-input-calibration" in SOURCE
    assert "document.readyState" in SOURCE
    assert "window.innerWidth" in SOURCE
    assert "window.innerHeight" in SOURCE
    assert 'raise RuntimeError("X11 坐标校准页面未就绪")' in SOURCE
    readiness_marker = "calibration_ready_deadline = loop.time() + 1.0"
    listener_marker = "window.__relayInputCalibration=null;"
    assert SOURCE.index(readiness_marker) < SOURCE.index(listener_marker)
    calibration_start = SOURCE.index("    async def _calibrate_x11_input")
    calibration_end = SOURCE.index("\n    def _fit_viewport_to_window", calibration_start)
    calibration_source = SOURCE[calibration_start:calibration_end]
    width_ready_marker = 'readiness_value.get("innerWidth") == self.width'
    height_ready_marker = 'readiness_value.get("innerHeight") == self.height'
    calibration_listener_marker = "window.__relayInputCalibration=null;"
    assert width_ready_marker in calibration_source
    assert height_ready_marker in calibration_source
    assert calibration_source.index(width_ready_marker) < calibration_source.index(calibration_listener_marker)
    assert calibration_source.index(height_ready_marker) < calibration_source.index(calibration_listener_marker)
    assert "Page.navigate" not in calibration_source
    assert "calibration_target = self._open_target(calibration_url)" in SOURCE
    assert "calibration_deadline = loop.time() + 1.0" in SOURCE
    assert "while loop.time() < calibration_deadline:" in SOURCE
    assert "await asyncio.sleep(0.02)" in SOURCE
    assert 'if point.get("trusted"):' in SOURCE
    assert 'if not point or not point.get("trusted"):' in SOURCE
    assert 'raise RuntimeError("X11 坐标校准没有收到可信鼠标事件")' in SOURCE
    assert SOURCE.count("self.x11.motion(") == 4
    assert "Page.startScreencast" in SOURCE
    assert 'DEFAULT_PORT = 8271' in SOURCE
    assert 'CDP_PORT = 9333' in SOURCE

def test_startup_page_readiness_uses_bounded_monotonic_deadline():
    assert "for _ in range(30):" not in SOURCE
    assert "loop = asyncio.get_running_loop()" in SOURCE
    assert "startup_deadline = loop.time() + 45.0" in SOURCE
    assert "while page_ws_url is None and loop.time() < startup_deadline:" in SOURCE
    assert 'f"http://127.0.0.1:{CDP_PORT}/json", timeout=0.5' in SOURCE
    assert "await asyncio.sleep(0.3)" in SOURCE
    assert 'if p.get("type") == "page" and ws_url:' in SOURCE
    assert 'page_ws_url = ws_url' in SOURCE
    assert 'print("Chrome 启动超时")' in SOURCE
    assert "sys.exit(1)" in SOURCE

if __name__ == "__main__":
    for test in (
        test_reference_relay_structure_and_ownership,
        test_fixed_browser_endpoints_profile_and_display,
        test_relay_pages_auth_dependency_and_generic_scope,
        test_environment_file_path_has_one_authority,
        test_service_starts_one_relay_and_not_chromium,
        test_x11_calibration_wait_is_bounded_and_requires_trusted_event,
        test_startup_page_readiness_uses_bounded_monotonic_deadline,
    ):
        test()
    print("BROWSER-BASE-RELAY-REFERENCE-PORT-R1 static validation: PASS")
