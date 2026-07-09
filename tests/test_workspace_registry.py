"""Unit tests for tools/workspace_registry.py (PR 3)."""

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


class WorkspaceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self._sandbox_ready = _sandboxize_path(root, is_dir=True)
        os.environ["EXEC_CWD"] = str(root)
        os.environ["WORKSPACE_ROOT"] = str(root)
        os.environ["EXEC_ENABLED"] = "1"
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_registry as wr
        import tools.workspace_agent as wa
        self.ex = importlib.reload(ex)
        self.wr = importlib.reload(wr)
        self.wa = importlib.reload(wa)

    def tearDown(self):
        os.environ.pop("EXEC_ENABLED", None)
        self.tmpdir.cleanup()

    def test_reserved_name_rejected(self):
        result = json.loads(self.wr.register_workspace_tool({
            "name": "mcp_search",
            "description": "bad",
            "script": "echo hi",
        }))
        self.assertEqual(result["error"], "reserved_name")

    def test_register_list_delete_roundtrip(self):
        reg = json.loads(self.wr.register_workspace_tool({
            "name": "hello_tool",
            "description": "Says hello",
            "script": 'echo "hello-from-tool"',
            "parameters": {"type": "object", "properties": {}},
        }))
        self.assertTrue(reg.get("ok"))
        self.assertFalse(reg.get("resident"))
        script = Path(self.tmpdir.name) / "tools" / "hello_tool.sh"
        self.assertTrue(script.exists())

        listed = json.loads(self.wr.list_workspace_tools())
        self.assertEqual(len(listed["tools"]), 1)
        self.assertEqual(listed["tools"][0]["name"], "hello_tool")

        deleted = json.loads(self.wr.delete_workspace_tool({"name": "hello_tool"}))
        self.assertTrue(deleted.get("deleted"))
        self.assertFalse(script.exists())
        self.assertEqual(json.loads(self.wr.list_workspace_tools())["tools"], [])

    def test_non_resident_not_in_resident_defs(self):
        self.wr.register_workspace_tool({
            "name": "hidden_tool",
            "description": "hidden",
            "script": "echo hidden",
        })
        names = {t["name"] for t in self.wr.build_resident_tool_defs()}
        self.assertNotIn("hidden_tool", names)

    def test_resident_in_defs(self):
        self.wr.register_workspace_tool({
            "name": "daily_tool",
            "description": "daily",
            "script": "echo daily",
            "resident": True,
        })
        names = {t["name"] for t in self.wr.build_resident_tool_defs()}
        self.assertIn("daily_tool", names)

    def test_mcp_search_workspace(self):
        result = json.loads(self.wr.mcp_search({"query": "workspace"}))
        self.assertEqual(len(result["servers"]), 1)
        self.assertEqual(result["servers"][0]["name"], "workspace")

    def test_mcp_load_includes_mgmt_tools(self):
        loaded = json.loads(self.wr.mcp_load({"server": "workspace"}))
        self.assertEqual(loaded["server"], "workspace")
        names = {t["name"] for t in loaded["tools"]}
        self.assertIn("register_workspace_tool", names)
        self.assertIn("list_workspace_tools", names)
        self.assertIn("delete_workspace_tool", names)

    def test_mcp_call_executes_custom_tool(self):
        if not self._sandbox_ready:
            self.skipTest("wsandbox/workspace not present (VPS-only integration test)")
        self.wr.register_workspace_tool({
            "name": "echo_args",
            "description": "echo json",
            "script": 'python3 -c "import os,json; print(json.loads(os.environ.get(\'TOOL_INPUT_JSON\') or \'{}\').get(\'msg\',\'\'))"',
        })
        result = json.loads(self.wr.mcp_call({
            "server": "workspace",
            "tool": "echo_args",
            "input": {"msg": "pr3-ok"},
        }, inner_dispatch=self.wa._dispatch_mgmt_or_custom))
        self.assertEqual(result.get("exit_code"), 0)
        self.assertIn("pr3-ok", result.get("stdout", ""))

    def test_execute_disabled_when_exec_off(self):
        self.wr.register_workspace_tool({
            "name": "blocked_tool",
            "description": "should not run",
            "script": 'echo "SHOULD_NOT_RUN"',
        })
        os.environ["EXEC_ENABLED"] = "0"
        import importlib
        import tools.workspace_executor as ex
        import tools.workspace_registry as wr
        importlib.reload(ex)
        importlib.reload(wr)
        result = json.loads(wr.execute_workspace_tool("blocked_tool", {}))
        self.assertEqual(result.get("error"), "exec_disabled")

    def test_custom_tool_output_file_mode(self):
        if not self._sandbox_ready:
            self.skipTest("wsandbox/workspace not present (VPS-only integration test)")
        projects = Path(self.tmpdir.name) / "projects"
        _sandboxize_path(projects, is_dir=True)
        self.wr.register_workspace_tool({
            "name": "write_probe",
            "description": "write probe file",
            "script": "mkdir -p projects/umask_probe && echo probe > projects/umask_probe/out.txt",
        })
        result = json.loads(self.wr.execute_workspace_tool("write_probe", {}))
        self.assertEqual(result.get("exit_code"), 0)
        out = projects / "umask_probe" / "out.txt"
        self.assertTrue(out.exists())
        mode = out.stat().st_mode & 0o777
        self.assertEqual(mode, 0o660, f"expected 660 group-only file, got {oct(mode)}")

    def test_mcp_call_register_via_mgmt(self):
        result = json.loads(self.wr.mcp_call({
            "server": "workspace",
            "tool": "register_workspace_tool",
            "input": {
                "name": "via_mcp",
                "description": "via mcp",
                "script": "echo via",
            },
        }, inner_dispatch=self.wa._dispatch_mgmt_or_custom))
        self.assertTrue(result.get("ok"))
        self.assertTrue((Path(self.tmpdir.name) / "tools" / "via_mcp.sh").exists())

    def test_agent_includes_meta_tools(self):
        names = {t["name"] for t in self.wa.get_workspace_tool_defs()}
        self.assertIn("mcp_search", names)
        self.assertIn("mcp_load", names)
        self.assertIn("mcp_call", names)
        self.assertNotIn("register_workspace_tool", names)

    def test_agent_routes_mcp_search(self):
        result = json.loads(self.wa.call_tool("mcp_search", {"query": "workspace"}))
        self.assertEqual(len(result["servers"]), 1)


if __name__ == "__main__":
    unittest.main()
