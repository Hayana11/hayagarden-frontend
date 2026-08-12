"""Narrow Home MCP server-side fence and legacy Wake compatibility tests."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp-http-server.js"


class HomeMcpP4Tests(unittest.TestCase):
    def test_profile_marker_and_gate_are_narrow(self):
        source = SERVER.read_text(encoding="utf-8")
        self.assertIn("x-uh-a0-profile", source)
        self.assertIn("uh_a0", source)
        self.assertIn("runGatedHomeWrite", source)
        self.assertIn("buildServer({ uhA0Profile })", source)

    def test_legacy_and_uh_a0_post_counts_without_production_write(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable in this focused Windows environment")
        source_path = json.dumps(str(SERVER))
        script = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const filename = __FILENAME__;
const fakeApp = { use() {}, all() {}, listen() {} };
function express() { return fakeApp; }
express.json = () => ({});
class FakeMcpServer {}
class FakeTransport {}
const fakeZ = new Proxy({}, { get: () => () => ({
  describe() { return this; },
  optional() { return this; },
  int() { return this; },
  min() { return this; },
  max() { return this; },
}) });
const fakeRequire = (name) => {
  if (name === 'express') return express;
  if (name === '@modelcontextprotocol/sdk/server/mcp.js') return { McpServer: FakeMcpServer };
  if (name === '@modelcontextprotocol/sdk/server/streamableHttp.js') {
    return { StreamableHTTPServerTransport: FakeTransport };
  }
  if (name === 'child_process') return { execSync() {}, execFileSync() {} };
  if (name === 'crypto') return { randomUUID() { return 'test'; } };
  if (name === 'fs') return { readFileSync() { return ''; } };
  if (name === 'zod') return { z: fakeZ };
  return require(name);
};
fakeRequire.main = {};
const moduleValue = { exports: {} };
const text = fs.readFileSync(filename, 'utf8');
vm.runInNewContext(text, {
  require: fakeRequire,
  module: moduleValue,
  exports: moduleValue.exports,
  __dirname: path.dirname(filename),
  __filename: filename,
  process: { env: {} },
  console,
  fetch: async () => ({ text: async () => '' }),
});
const gate = moduleValue.exports.runGatedHomeWrite;
async function sample(uhA0Profile, verifyOk) {
  let posts = 0;
  let verifies = 0;
  const result = await gate({
    uhA0Profile,
    toolName: 'mcp__home__add_todo',
    toolInput: { content: 'test-only' },
    verify: () => { verifies += 1; return verifyOk
      ? { ok: true } : { ok: false, result: { denied: true } }; },
    post: async () => { posts += 1; return { posted: true }; },
  });
  return { posts, verifies, result };
}
(async () => {
  const legacyTodo = await sample(false, false);
  const legacyLedger = await sample(false, false);
  const uhA0DeniedTodo = await sample(true, false);
  const uhA0DeniedLedger = await sample(true, false);
  const uhA0Allowed = await sample(true, true);
  process.stdout.write(JSON.stringify({
    legacyTodo, legacyLedger, uhA0DeniedTodo, uhA0DeniedLedger, uhA0Allowed,
  }));
})();
""".replace("__FILENAME__", source_path)
        completed = subprocess.run(
            [node, "--eval", script],
            cwd=str(ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(completed.stdout)
        for key in ("legacyTodo", "legacyLedger"):
            self.assertEqual(result[key]["posts"], 1)
            self.assertEqual(result[key]["verifies"], 0)
        for key in ("uhA0DeniedTodo", "uhA0DeniedLedger"):
            self.assertEqual(result[key]["posts"], 0)
            self.assertEqual(result[key]["verifies"], 1)
        self.assertEqual(result["uhA0Allowed"]["posts"], 1)
        self.assertEqual(result["uhA0Allowed"]["verifies"], 1)


if __name__ == "__main__":
    unittest.main()
