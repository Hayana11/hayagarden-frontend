"""Unit tests for tools/workspace_apps.py (PR 4)."""

import json
import os
import stat
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _sandbox_ids() -> tuple[int, int] | None:
    try:
        import grp
        import pwd
        return pwd.getpwnam("wsandbox").pw_uid, grp.getgrnam("workspace").gr_gid
    except KeyError:
        return None


def _sandboxize_path(path: Path, *, is_dir: bool = True) -> bool:
    ids = _sandbox_ids()
    if ids is None:
        return False
    uid, gid = ids
    if is_dir:
        path.mkdir(parents=True, exist_ok=True)
    shutil.chown(path, uid, gid)
    mode = stat.S_IRWXU | stat.S_IRWXG | stat.S_ISGID if is_dir else stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP
    os.chmod(path, mode)
    return True


class WorkspaceAppsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self._sandbox_ready = _sandboxize_path(root, is_dir=True)
        os.environ["EXEC_CWD"] = str(root)
        os.environ["WORKSPACE_ROOT"] = str(root)
        os.environ["EXEC_ENABLED"] = "1"
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_apps as wa
        import tools.workspace_agent as agent
        self.ex = importlib.reload(ex)
        self.wa = importlib.reload(wa)
        self.agent = importlib.reload(agent)

    def tearDown(self):
        os.environ.pop("EXEC_ENABLED", None)
        self.tmpdir.cleanup()

    def _write_manifest(self, app_id: str, **extra):
        apps_root = Path(self.tmpdir.name) / "apps"
        if not _sandboxize_path(apps_root, is_dir=True):
            apps_root.mkdir(parents=True, exist_ok=True)
        app_path = apps_root / app_id
        if not _sandboxize_path(app_path, is_dir=True):
            app_path.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": app_id,
            "name": app_id,
            "port": extra.pop("port", 23456),
            "entry": "/",
            "start": extra.pop("start", "python3 -m http.server ${PORT} --bind 127.0.0.1"),
            "health": "/",
        }
        manifest.update(extra)
        manifest_path = app_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if self._sandbox_ready:
            shutil.chown(manifest_path, *_sandbox_ids())
            os.chmod(manifest_path, 0o660)
        return app_path

    def test_validate_app_id(self):
        self.assertEqual(self.wa.validate_app_id("demo"), "demo")
        with self.assertRaises(self.wa.WorkspaceAppError):
            self.wa.validate_app_id("../evil")

    def test_reserved_port_rejected(self):
        self._write_manifest("badport", port=5051)
        with self.assertRaises(self.wa.WorkspaceAppError):
            self.wa._port_from_manifest(self.wa.load_manifest("badport"))

    def test_proxy_url_format(self):
        self._write_manifest("hello")
        url = self.wa.proxy_url_for("hello")
        self.assertEqual(url, "/api/gw/workspace/apps/hello/proxy/")

    def test_workspace_app_list_empty(self):
        result = json.loads(self.wa.workspace_app({"action": "list"}))
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("apps"), [])

    def test_start_disabled_when_exec_off(self):
        self._write_manifest("demo")
        os.environ["EXEC_ENABLED"] = "0"
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_apps as wa
        importlib.reload(ex)
        importlib.reload(wa)
        result = json.loads(wa.workspace_app({"action": "start", "id": "demo"}))
        self.assertEqual(result.get("error"), "exec_disabled")

    def test_upstream_must_be_loopback(self):
        self._write_manifest("remote", port=24001, upstream="http://example.com:24001")
        with self.assertRaises(self.wa.WorkspaceAppError):
            self.wa.upstream_base(self.wa.load_manifest("remote"))

    def test_https_upstream_rejected(self):
        self._write_manifest("tls", port=24002, upstream="https://127.0.0.1:24002")
        with self.assertRaises(self.wa.WorkspaceAppError):
            self.wa.upstream_base(self.wa.load_manifest("tls"))

    def test_tampered_app_dir_runtime_ignored(self):
        if not self._sandbox_ready:
            self.skipTest("wsandbox/workspace not present (VPS-only integration test)")
        self._write_manifest("tamper")
        fake = self.wa.app_dir("tamper") / ".runtime.json"
        fake.write_text(json.dumps({
            "owner": "gateway",
            "runtime_nonce": "deadbeef",
            "id": "tamper",
            "pid": 1,
            "pgid": 1,
            "port": 24003,
            "upstream": "http://127.0.0.1:24003",
        }), encoding="utf-8")
        result = json.loads(self.wa.workspace_app({"action": "status", "id": "tamper"}))
        self.assertFalse(result["status"].get("running"))

    def test_proxy_requires_verified_running(self):
        self._write_manifest("nope")
        with self.assertRaises(self.wa.WorkspaceAppError) as ctx:
            self.wa.verified_proxy_upstream("nope")
        self.assertEqual(ctx.exception.code, "app_not_running")

    def test_runtime_dir_gateway_owned(self):
        self.wa.ensure_runtime_dir()
        st = self.wa.RUNTIME_DIR.stat()
        sandbox_uid = self.wa._sandbox_uid()
        if sandbox_uid is not None:
            self.assertNotEqual(st.st_uid, sandbox_uid)
        self.assertEqual(st.st_mode & 0o777, 0o750)
        gateway_uid, _ = self.wa._gateway_runtime_ids()
        self.assertEqual(st.st_uid, gateway_uid)

    def test_wsandbox_created_runtime_file_untrusted(self):
        if not self._sandbox_ready:
            self.skipTest("wsandbox/workspace not present (VPS-only integration test)")
        self.wa.ensure_runtime_dir()
        forged = self.wa.runtime_path("forged")
        forged.write_text(json.dumps({
            "owner": "gateway",
            "runtime_nonce": "fakefakefakefake",
            "id": "forged",
            "pid": os.getpid(),
            "pgid": os.getpid(),
            "port": 24099,
            "upstream": "http://127.0.0.1:24099",
        }), encoding="utf-8")
        ids = _sandbox_ids()
        assert ids is not None
        os.chown(forged, ids[0], ids[1])
        self.assertFalse(self.wa._runtime_file_trusted(forged))
        self.assertEqual(self.wa.read_runtime("forged"), {})
        names = {t["name"] for t in self.agent.get_workspace_tool_defs()}
        self.assertIn("workspace_app", names)

    def test_status_not_running(self):
        self._write_manifest("idle")
        result = json.loads(self.wa.workspace_app({"action": "status", "id": "idle"}))
        self.assertTrue(result.get("ok"))
        self.assertFalse(result["status"].get("running"))


if __name__ == "__main__":
    unittest.main()
