"""Hotfix: gateway tools/ path must not shadow root internal_state_shadow."""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
TOOLS = str(Path(ROOT) / 'tools')


class GatewayShadowModuleResolutionTests(unittest.TestCase):
    def test_gateway_source_appends_tools_not_insert0(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        # Collect Call nodes: sys.path.insert(0, '.../tools') must be absent;
        # append('.../tools') must be present at module level path bootstrap.
        insert0_tools = []
        append_tools = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == 'insert':
                if (
                    len(node.args) >= 2
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 0
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and node.args[1].value.rstrip('/').endswith('/tools')
                ):
                    insert0_tools.append(node.lineno)
            if isinstance(func, ast.Attribute) and func.attr == 'append':
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.rstrip('/').endswith('/tools')
                ):
                    append_tools.append(node.lineno)
        self.assertEqual(
            insert0_tools, [],
            f'gateway still insert(0, tools) at lines {insert0_tools}',
        )
        self.assertGreaterEqual(len(append_tools), 1)

    def test_path_order_resolves_root_shadow_not_cli_stub(self):
        """Reproduce gateway path bootstrap; root module must win."""
        code = f"""
import sys
root = {ROOT!r}
tools = {TOOLS!r}
# Clear any prior shadow modules from the worker.
for name in list(sys.modules):
    if name == 'internal_state_shadow' or name.startswith('internal_state_shadow.'):
        del sys.modules[name]
if root in sys.path:
    sys.path.remove(root)
if tools in sys.path:
    sys.path.remove(tools)
sys.path.insert(0, root)
if tools not in sys.path:
    sys.path.append(tools)
import internal_state_shadow as s
print(s.__file__)
print(hasattr(s, '_read_bootstrap_gate'))
print(hasattr(s, 'ensure_bootstrapped'))
assert s.__file__.startswith(root + os.sep) or s.__file__ == root + '/internal_state_shadow.py'
assert '/tools/internal_state_shadow.py' not in s.__file__.replace('\\\\', '/')
assert hasattr(s, '_read_bootstrap_gate')
assert hasattr(s, 'ensure_bootstrapped')
# CASE 2: bare tools imports still resolve
import embedding_tool
import memory_tool
print(embedding_tool.__file__)
print(memory_tool.__file__)
assert embedding_tool.__file__.startswith(tools)
assert memory_tool.__file__.startswith(tools)
print('OK')
"""
        env = os.environ.copy()
        env['PYTHONPATH'] = ''
        proc = subprocess.run(
            [sys.executable, '-c', 'import os\n' + code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + '\n' + proc.stderr)
        self.assertIn('OK', proc.stdout)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        shadow_file = lines[0]
        self.assertTrue(
            shadow_file.endswith('internal_state_shadow.py'),
            shadow_file,
        )
        self.assertNotIn('/tools/internal_state_shadow.py', shadow_file)
        self.assertIn(f'{ROOT}/internal_state_shadow.py', shadow_file)
        self.assertEqual(lines[1], 'True')
        self.assertEqual(lines[2], 'True')

    def test_old_insert0_order_would_hit_stub(self):
        """Control: the pre-fix insert(0, tools) order resolves the CLI stub."""
        code = f"""
import sys
root = {ROOT!r}
tools = {TOOLS!r}
for name in list(sys.modules):
    if name == 'internal_state_shadow' or name.startswith('internal_state_shadow.'):
        del sys.modules[name]
if root in sys.path:
    sys.path.remove(root)
if tools in sys.path:
    sys.path.remove(tools)
sys.path.insert(0, root)
sys.path.insert(0, tools)  # pre-fix bug
import internal_state_shadow as s
print(s.__file__)
print(hasattr(s, 'ensure_bootstrapped'))
"""
        proc = subprocess.run(
            [sys.executable, '-c', code],
            cwd=ROOT,
            env={**os.environ, 'PYTHONPATH': ''},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertIn('/tools/internal_state_shadow.py', lines[0].replace('\\', '/'))
        self.assertEqual(lines[1], 'False')


if __name__ == '__main__':
    unittest.main()
