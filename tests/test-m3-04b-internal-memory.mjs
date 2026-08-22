import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import internalServer from '../internal-mcp-server.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-04b-memory-internal-'));
const dbPath = join(tempRoot, 'memory.db');

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: { ...process.env, UH_A0_REPO_ROOT: root },
    encoding: 'utf8',
  }).trim();
}

const rows = [
  [1, 'MEMORY', 'old needle', '2026-08-01T01:02:03Z', 0, 'old', 'recent', 0],
  [2, 'MEMORY', 'pinned needle', '2026-08-02T01:02:03Z', 1, 'pin', 'recent', 0],
  [3, 'MEMORY', 'needle three', '2026-08-03T01:02:03Z', 0, 'three', 'recent', 0],
  [4, 'MEMORY', 'content does not match', '2026-08-04T01:02:03Z', 0, 'needle', 'recent', 0],
  [5, 'MEMORY', 'resolved needle', '2026-08-05T01:02:03Z', 0, 'resolved', 'recent', 1],
  [6, 'MEMORY', '中文关键词：海边', '2026-08-06T01:02:03Z', 0, '中文', 'recent', 0],
  [7, 'MEMORY', 'needle ' + 'x'.repeat(250), '2026-08-07T01:02:03Z', 0, 'long', 'recent', 0],
  [8, 'MEMORY', 'needle eight', '2026-08-08T01:02:03Z', 0, 'eight', 'recent', 0],
  [9, 'MEMORY', 'needle nine', '2026-08-09T01:02:03Z', 0, 'nine', 'recent', 0],
  [10, 'MEMORY', 'needle ten', '2026-08-10T01:02:03Z', 0, 'ten', 'recent', 0],
  [11, 'MEMORY', 'needle eleven', '2026-08-11T01:02:03Z', 0, 'eleven', 'recent', 0],
  [12, 'MEMORY', 'needle twelve', '2026-08-12T04:05:06Z', 0, 'twelve', 'recent', 0],
];

python(
  `import json, sqlite3, sys
data = json.loads(sys.argv[2])
conn = sqlite3.connect(sys.argv[1])
conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT NOT NULL, content TEXT NOT NULL, author TEXT DEFAULT 'fyodor', created_at TEXT, pinned INTEGER DEFAULT 0, tags TEXT DEFAULT '', layer TEXT DEFAULT 'recent', resolved INTEGER DEFAULT 0, recall_count INTEGER DEFAULT 0, last_recalled_at TEXT)")
conn.executemany("INSERT INTO posts (id,type,content,created_at,pinned,tags,layer,resolved) VALUES (?,?,?,?,?,?,?,?)", data)
conn.commit()
conn.close()`,
  [dbPath, JSON.stringify(rows)],
);

function snapshot() {
  return python(
    'import json, sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); rows=conn.execute("SELECT id,type,content,created_at,pinned,tags,layer,resolved,recall_count,last_recalled_at FROM posts ORDER BY id").fetchall(); print(json.dumps(rows, ensure_ascii=False)); conn.close()',
    [dbPath],
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

function homeFormat(selected) {
  return selected.map((post) => {
    const date = String(post[3] || '').slice(0, 10);
    const pinned = post[4] ? ' 📌' : '';
    return `[#${post[0]} ${post[1]} ${date}${pinned}] ${post[2].slice(0, 220)}`;
  }).join('\n---\n') || '没有找到相关记忆';
}

function selectedByIds(ids) {
  return ids.map((id) => rows.find((row) => row[0] === id));
}

const adapterSource = readFileSync(join(root, 'tools/memory_internal_adapter.py'), 'utf8');
assert.match(adapterSource, /from tools\.product_handlers import ProductHandlerError, search_memory_posts/);
assert.doesNotMatch(adapterSource, /SELECT|LIKE|ORDER BY|INSERT|UPDATE|DELETE|commit\s*\(/);
assert.doesNotMatch(adapterSource, /TODO_INTERNAL_DB_PATH|os\.environ|\/opt\/frontend\/memories\.db/);
assert.doesNotMatch(adapterSource, /memory_tool|memory_library|ombre_adapter/);

const serverSource = readFileSync(join(root, 'internal-mcp-server.js'), 'utf8');
assert.match(serverSource, /'search_memories'/);
assert.match(serverSource, /formatMemorySearch/);
assert.match(serverSource, /slice\(0, 220\)/);
assert.match(serverSource, /没有找到相关记忆/);
assert.match(serverSource, /Error: ' \+ error\.message/);
assert.match(serverSource, /limit: 8/);

const meta = JSON.parse(python(
  `import json, tempfile
from unittest.mock import patch
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import HOME_MCP_CAPABILITY_IDS, INTERNAL_MCP_CAPABILITY_IDS, INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS, build_uh_a0_spawn_plan
from tools.cc_tool_surface import _HOME_TOOL_SCHEMAS, _INTERNAL_TOOL_SCHEMAS
from tools.capability_state import RUNTIME_STATE_INHERIT, RUNTIME_STATE_OFF
with patch("tools.cc_capability_adapter.read_capability_state", return_value=RUNTIME_STATE_INHERIT):
    with tempfile.TemporaryDirectory() as root_dir:
        plan = build_uh_a0_spawn_plan(cwd=root_dir, write_mcp_config=False, env={})
with patch("tools.cc_capability_adapter.read_capability_state", return_value=RUNTIME_STATE_OFF):
    with tempfile.TemporaryDirectory() as root_dir:
        off_plan = build_uh_a0_spawn_plan(cwd=root_dir, write_mcp_config=False, env={})
print(json.dumps({"binding": get_capability("memory.search")["provider_bindings"], "home_ids": HOME_MCP_CAPABILITY_IDS, "internal_ids": INTERNAL_MCP_CAPABILITY_IDS, "shadow": INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS, "schema_equal": _HOME_TOOL_SCHEMAS["mcp__home__search_memories"] == _INTERNAL_TOOL_SCHEMAS["mcp__internal__search_memories"], "fingerprint": plan["physical_surface_fingerprint"], "off_allow": off_plan["surface_allowlist"], "off_disallow": off_plan["disallowed_tools"], "home_visible": plan["home_mcp_tools"]}, ensure_ascii=False))`,
));

assert.deepEqual(meta.binding, {
  claude_code: 'mcp__home__search_memories',
  internal_mcp: 'mcp__internal__search_memories',
});
assert.deepEqual(meta.home_ids, ['memory.search', 'diary.write', 'home.light.status', 'countdown.read']);
assert.deepEqual(meta.internal_ids, ['todo.read', 'todo.write', 'ledger.read', 'ledger.budget.read', 'ledger.write']);
assert.deepEqual(meta.shadow, ['mcp__internal__search_memories']);
assert.equal(meta.schema_equal, true);
assert.equal(meta.fingerprint, '8c3d88f947978c1f7bb8a8a9da6cc3ccb6adca37ace955e88567ab8482adda15');
assert.ok(meta.home_visible.includes('mcp__home__search_memories'));
assert.ok(!meta.off_allow.includes('mcp__home__search_memories'));
assert.ok(!meta.off_allow.includes('mcp__internal__search_memories'));
assert.ok(meta.off_disallow.includes('mcp__internal__search_memories'));
assert.ok(meta.off_disallow.includes('mcp__home__search_memories'));

const { listener, port } = await internalServer.startInternalMcpServer({
  port: 0,
  dbPath,
  cwd: root,
  python: process.env.PYTHON || 'python3',
});
const client = new Client({ name: 'm3-04b-internal-memory', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(
  new URL(`http://127.0.0.1:${port}/mcp`),
  { requestInit: { headers: { 'X-UH-A0-Profile': 'uh_a0' } } },
);

try {
  await client.connect(transport);
  const listed = await client.listTools();
  assert.deepEqual(
    listed.tools.map((tool) => tool.name),
    ['get_todos', 'add_todo', 'get_ledger', 'get_ledger_budget', 'add_ledger', 'search_memories'],
  );
  const searchSchema = listed.tools.find((tool) => tool.name === 'search_memories')?.inputSchema;
  assert.deepEqual(Object.keys(searchSchema.properties || {}), ['keyword']);
  assert.deepEqual(searchSchema.required, ['keyword']);
  assert.equal(searchSchema.properties.keyword.type, 'string');

  const before = snapshot();
  const needle = await client.callTool({ name: 'search_memories', arguments: { keyword: 'needle' } });
  assert.equal(textOf(needle), homeFormat(selectedByIds([12, 11, 10, 9, 8, 7, 5, 3])));
  assert.equal((textOf(needle).match(/\n---\n/g) || []).length, 7);
  assert.equal(textOf(needle).includes('x'.repeat(220)), true);
  assert.equal(textOf(needle).includes('x'.repeat(221)), false);
  assert.equal(snapshot(), before);

  const empty = await client.callTool({ name: 'search_memories', arguments: { keyword: '' } });
  assert.equal(textOf(empty), homeFormat(selectedByIds([12, 11, 10, 9, 8, 7, 6, 5])));
  assert.equal(snapshot(), before);

  const unicode = await client.callTool({ name: 'search_memories', arguments: { keyword: '海边' } });
  assert.equal(textOf(unicode), homeFormat(selectedByIds([6])));
  assert.equal(snapshot(), before);

  const resolved = await client.callTool({ name: 'search_memories', arguments: { keyword: 'resolved' } });
  assert.equal(textOf(resolved), homeFormat(selectedByIds([5])));
  assert.equal(snapshot(), before);

  const tagsOnly = await client.callTool({ name: 'search_memories', arguments: { keyword: 'needle' } });
  assert.equal(textOf(tagsOnly).includes('[#4 '), false);
  const pinned = await client.callTool({ name: 'search_memories', arguments: { keyword: 'pinned' } });
  assert.equal(textOf(pinned), homeFormat(selectedByIds([2])));
  assert.match(textOf(pinned), /^\[#2 MEMORY 2026-08-02 📌\]/);
  assert.equal(snapshot(), before);

  const noHit = await client.callTool({ name: 'search_memories', arguments: { keyword: '__no_hit__' } });
  assert.equal(textOf(noHit), '没有找到相关记忆');
  assert.equal(snapshot(), before);

  const readinessSource = readFileSync(join(root, 'scripts/check-internal-mcp-readiness.mjs'), 'utf8');
  assert.doesNotMatch(readinessSource, /callTool\s*\(/);

  const goneDb = dbPath + '.gone';
  rmSync(dbPath);
  const error = await client.callTool({ name: 'search_memories', arguments: { keyword: 'needle' } });
  assert.match(textOf(error), /^Error: /);
  rmSync(goneDb, { force: true });
} finally {
  await client.close().catch(() => {});
  await new Promise((resolve) => listener.close(resolve));
  rmSync(tempRoot, { recursive: true, force: true });
}

console.log('test-m3-04b-internal-memory: ok');
