"""Unit tests for workspace path containment."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class WorkspacePathTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["WORKSPACE_ROOT"] = self.tmpdir.name
        import importlib
        import tools.workspace_agent as wa
        self.wa = importlib.reload(wa)
        self.root = self.wa.WORKSPACE_ROOT

    def tearDown(self):
        self.tmpdir.cleanup()

    def _rejected(self, raw: str, *, for_create: bool = False) -> bool:
        try:
            self.wa.workspace_path(raw, for_create=for_create)
            return False
        except ValueError:
            return True
        except FileNotFoundError:
            return False

    def test_traversal_rejected(self):
        self.assertTrue(self._rejected("../etc/passwd", for_create=True))
        self.assertTrue(self._rejected("../../root/.ssh/id_ed25519", for_create=True))
        self.assertTrue(self._rejected("/etc/passwd", for_create=True))
        self.assertTrue(self._rejected("/opt/frontend/.env", for_create=True))
        self.assertTrue(self._rejected("projects/../../etc/shadow", for_create=True))

    def test_inside_allowed(self):
        p = self.wa.workspace_path("projects/demo/main.py", for_create=True)
        self.assertTrue(str(p).startswith(str(self.root)))
        p2 = self.wa.workspace_path(str(self.root / "artifacts" / "out.txt"), for_create=True)
        self.assertTrue(str(p2).startswith(str(self.root)))

    def test_empty_rejected(self):
        self.assertTrue(self._rejected("", for_create=True))
        self.assertTrue(self._rejected(" ", for_create=True))

    def test_write_and_read_roundtrip(self):
        import pwd
        try:
            pwd.getpwnam("wsandbox")
        except KeyError:
            self.skipTest("wsandbox user not present (VPS-only integration test)")
        projects = self.root / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        target = projects / "hello.txt"
        write_result = self.wa._ws_write({"path": "projects/hello.txt", "content": "hi"})
        self.assertIn('"ok": true', write_result.lower())
        read_result = self.wa._ws_read({"path": str(target)})
        self.assertIn("hi", read_result)

    def test_exec_group_defaults_to_workspace(self):
        import tools.workspace_executor as ex
        self.assertEqual(ex.EXEC_GROUP, "workspace")
        self.assertNotEqual(ex.EXEC_GROUP, ex.EXEC_USER)

    def test_shell_exec_disabled_by_default(self):
        import tools.workspace_executor as ex
        result = ex.run_exec("echo hi")
        self.assertIn("exec_disabled", result)

    def test_ws_diff_git_mode_disabled_by_default(self):
        projects = self.root / "projects"
        projects.mkdir(parents=True, exist_ok=True)
        result = self.wa._ws_diff({"path": "projects"})
        self.assertIn("git_diff_disabled", result)


if __name__ == "__main__":
    unittest.main()
