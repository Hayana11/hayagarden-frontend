import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
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

function installAllowedLease() {
  python(
    [
      'import sys',
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="m3-01-turn", turn_mode="chat", issued_from="explicit_user_intent", requested_capabilities=("todo.write",), issued_at="2026-08-21T00:00:00Z")',
      'write_current_turn_lease(sys.argv[1], lease)',
    ].join('; '),
    [leasePath],
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

seedDb();
const { listener, port } = await internalServer.startInternalMcpServer({
  port: 0,
  dbPath,
  cwd: root,
  python: process.env.PYTHON || 'python3',
});
const client = new Client({ name: 'm3-01-test-client', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`));
await client.connect(transport);

const listed = await client.listTools();
assert.deepEqual(
  listed.tools.map((tool) => tool.name),
  ['get_todos', 'add_todo'],
);
assert.equal(listed.tools[1].inputSchema.required[0], 'content');

const read = await client.callTool({ name: 'get_todos', arguments: {} });
const readPayload = JSON.parse(textOf(read));
assert.equal(readPayload.todos.length, 2);
assert.equal(readPayload.todos[0].content, '未完成');

const denied = await client.callTool({
  name: 'add_todo',
  arguments: { content: '不得写入' },
});
assert.match(textOf(denied), /LEASE_MISMATCH/);
assert.equal(countRows(), 2);

installAllowedLease();
const written = await client.callTool({
  name: 'add_todo',
  arguments: { content: '只写一次', due_date: '2026-08-23' },
});
assert.deepEqual(JSON.parse(textOf(written)), { ok: true });
assert.equal(countRows(), 3);

await client.close();
await new Promise((resolve) => listener.close(resolve));
rmSync(tempRoot, { recursive: true, force: true });
console.log('test-m3-01-todo-internal: ok');
