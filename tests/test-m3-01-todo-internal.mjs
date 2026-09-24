import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import internalServer from '../internal-mcp-server.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-01-todo-internal-'));
const dbPath = join(tempRoot, 'todos.db');
const leasePath = join(tempRoot, 'turn-lease.json');

// The shadow server verifies against this isolated lease, never the production lease.
process.env.UH_A0_REPO_ROOT = root;
process.env.UH_A0_TURN_LEASE_PATH = leasePath;

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: { ...process.env, UH_A0_REPO_ROOT: root, UH_A0_TURN_LEASE_PATH: leasePath },
    encoding: 'utf8',
  }).trim();
}

function seedDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      'conn.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, done INTEGER DEFAULT 0, due_date TEXT, author TEXT, created_at TEXT)")',
      'conn.executemany("INSERT INTO todos (content, done, due_date, author, created_at) VALUES (?,?,?,?,?)", [("未完成",0,None,"alice","2026-08-01"),("已完成",1,"2026-08-02","alice","2026-08-02")])',
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [dbPath],
  );
}

function countRows() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function installLease(capability, mode = 'chat') {
  python(
    [
      'import sys',
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="m3-01-turn", turn_mode=sys.argv[3], issued_from="explicit_user_intent", requested_capabilities=(sys.argv[2],), issued_at="2026-08-21T00:00:00Z")',
      'write_current_turn_lease(sys.argv[1], lease)',
    ].join('; '),
    [leasePath, capability, mode],
  );
}
function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

seedDb();

const proxyClient = new Client({ name: 'm3-01-capability-proxy', version: '1.0.0' });
const proxyTransport = new StdioClientTransport({
  command: process.execPath,
  args: [join(root, 'capability-proxy-mcp-server.js')],
  env: {
    ...process.env,
    UH_A0_REPO_ROOT: root,
    TODO_INTERNAL_DB_PATH: dbPath,
    UH_A0_TURN_LEASE_PATH: leasePath,
  },
});
await proxyClient.connect(proxyTransport);
const proxyListed = await proxyClient.listTools();
assert.ok(proxyListed.tools.some((tool) => tool.name === 'todo_read'));
assert.ok(proxyListed.tools.some((tool) => tool.name === 'todo_write'));
await proxyClient.close();

const { listener, port } = await internalServer.startInternalMcpServer({
  port: 0,
  dbPath,
  cwd: root,
  python: process.env.PYTHON || 'python3',
});
async function connectClient(name, profile) {
  const client = new Client({ name, version: '1.0.0' });
  const requestInit = profile
    ? { headers: { 'x-uh-a0-profile': profile } }
    : undefined;
  const transport = new StreamableHTTPClientTransport(
    new URL(`http://127.0.0.1:${port}/mcp`),
    { requestInit },
  );
  await client.connect(transport);
  return { client, transport };
}

const noProfile = await connectClient('m3-01-no-profile', undefined);
const listed = await noProfile.client.listTools();
assert.deepEqual(
  listed.tools.filter((tool) => ['get_todos', 'add_todo'].includes(tool.name)).map((tool) => tool.name),
  ['get_todos', 'add_todo'],
);
assert.equal(listed.tools[1].inputSchema.required[0], 'content');

const read = await noProfile.client.callTool({ name: 'get_todos', arguments: {} });
const readPayload = JSON.parse(textOf(read));
assert.equal(readPayload.todos.length, 2);
assert.equal(readPayload.todos[0].content, '未完成');

const noLease = await noProfile.client.callTool({
  name: 'add_todo',
  arguments: { content: '没有 profile 不得写入' },
});
assert.match(textOf(noLease), /PROFILE_REQUIRED/);
assert.equal(countRows(), 2);

installLease('todo.write');
const noProfileWrite = await noProfile.client.callTool({
  name: 'add_todo',
  arguments: { content: '没有 profile 不得写入' },
});
assert.match(textOf(noProfileWrite), /PROFILE_REQUIRED/);
assert.equal(countRows(), 2);

const withProfile = await connectClient('m3-01-uh-a0-profile', 'uh_a0');
const written = await withProfile.client.callTool({
  name: 'add_todo',
  arguments: { content: 'profile 写一次', due_date: '2026-08-23' },
});
assert.deepEqual(JSON.parse(textOf(written)), { ok: true });
assert.equal(countRows(), 3);

installLease('todo.read', 'task');
const badLease = await withProfile.client.callTool({
  name: 'add_todo',
  arguments: { content: '坏 lease 不得写入' },
});
assert.match(textOf(badLease), /UH-A0 DENIED_CAPABILITY/);
assert.equal(countRows(), 3);

await noProfile.client.close();
await withProfile.client.close();
await new Promise((resolve) => listener.close(resolve));
rmSync(tempRoot, { recursive: true, force: true });
// Reuse this existing local shadow job to exercise Xiaomi's one-tool MCP
// contract with a mocked provider; no Xiaomi account or cloud request is used.
await import('./test-xiaomi-health-internal-mcp.mjs');

console.log('test-m3-01-todo-internal: ok');
