import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import internalServer from '../internal-mcp-server.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-03a-ledger-internal-'));
const dbPath = join(tempRoot, 'ledger.db');
const leasePath = join(tempRoot, 'turn-lease.json');
const month = new Date().toISOString().slice(0, 7);
const [year, monthNumber] = month.split('-').map(Number);
const previousMonth = monthNumber === 1
  ? `${year - 1}-12`
  : `${year}-${String(monthNumber - 1).padStart(2, '0')}`;
const noBudgetMonth = '1999-01';

process.env.UH_A0_REPO_ROOT = root;
process.env.UH_A0_TURN_LEASE_PATH = leasePath;

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: {
      ...process.env,
      UH_A0_REPO_ROOT: root,
      UH_A0_TURN_LEASE_PATH: leasePath,
    },
    encoding: 'utf8',
  }).trim();
}

function seedDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      'month, previous = sys.argv[2], sys.argv[3]',
      'conn.executescript("CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, done INTEGER DEFAULT 0, due_date TEXT, author TEXT, created_at TEXT); CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, category TEXT, note TEXT, date TEXT, author TEXT, meta TEXT, created_at TEXT); CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT UNIQUE, amount REAL NOT NULL)")',
      'conn.execute("INSERT INTO todos (content, done, author, created_at) VALUES (?,?,?,?)", ("Todo 回归",0,"test","2026-08-21"))',
      'conn.executemany("INSERT INTO ledger (amount, category, note, date, author, meta, created_at) VALUES (?,?,?,?,?,?,?)", [(100,"工资","收入",month+"-01","alice",None,"2026-08-01"),(-35,"餐饮","午餐",month+"-02","alice",None,"2026-08-02"),(-20,"交通","上月",previous+"-03","alice",None,"2026-07-03")])',
      'conn.execute("INSERT INTO ledger_budget (month, amount) VALUES (?,?)", (month, 5000))',
      'conn.commit(); conn.close()',
    ].join('; '),
    [dbPath, month, previousMonth],
  );
}

function countLedgerRows() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function lastLedgerRow() {
  return JSON.parse(python(
    'import json, sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); conn.row_factory=sqlite3.Row; print(json.dumps(dict(conn.execute("SELECT amount, category, note, date, author FROM ledger ORDER BY id DESC LIMIT 1").fetchone()))); conn.close()',
    [dbPath],
  ));
}

function installLease(capability) {
  python(
    [
      'import sys',
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="m3-03a-turn", turn_mode="chat", issued_from="explicit_user_intent", requested_capabilities=(sys.argv[2],), issued_at="2026-08-21T00:00:00Z")',
      'write_current_turn_lease(sys.argv[1], lease)',
    ].join('; '),
    [leasePath, capability],
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

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
  return client;
}

seedDb();
const { listener, port } = await internalServer.startInternalMcpServer({
  port: 0,
  dbPath,
  cwd: root,
  python: process.env.PYTHON || 'python3',
});

try {
  const noProfile = await connectClient('m3-03a-no-profile');
  const listed = await noProfile.listTools();
  assert.deepEqual(
    listed.tools.map((tool) => tool.name).sort(),
    ['add_ledger', 'add_todo', 'get_ledger', 'get_ledger_budget', 'get_todos', 'search_memories', 'write_memory'],
  );

  const specified = await noProfile.callTool({
    name: 'get_ledger',
    arguments: { month },
  });
  const specifiedPayload = JSON.parse(textOf(specified));
  assert.deepEqual(specifiedPayload.summary, {
    income: 100,
    expense: -35,
    balance: 65,
    prev_expense: -20,
  });
  assert.equal(specifiedPayload.records.length, 2);

  const defaultRead = await noProfile.callTool({
    name: 'get_ledger',
    arguments: {},
  });
  assert.deepEqual(JSON.parse(textOf(defaultRead)), specifiedPayload);

  const budget = await noProfile.callTool({
    name: 'get_ledger_budget',
    arguments: { month },
  });
  assert.equal(JSON.parse(textOf(budget)).amount, 5000);

  const defaultBudget = await noProfile.callTool({
    name: 'get_ledger_budget',
    arguments: {},
  });
  assert.equal(JSON.parse(textOf(defaultBudget)).amount, 5000);

  const emptyBudget = await noProfile.callTool({
    name: 'get_ledger_budget',
    arguments: { month: noBudgetMonth },
  });
  assert.equal(JSON.parse(textOf(emptyBudget)).amount, null);

  const before = countLedgerRows();
  const noProfileWrite = await noProfile.callTool({
    name: 'add_ledger',
    arguments: { amount: -12 },
  });
  assert.match(textOf(noProfileWrite), /PROFILE_REQUIRED/);
  assert.equal(countLedgerRows(), before);

  const withProfile = await connectClient('m3-03a-profile', 'uh_a0');
  installLease('ledger.read');
  const badLease = await withProfile.callTool({
    name: 'add_ledger',
    arguments: { amount: -12 },
  });
  assert.match(textOf(badLease), /UH-A0 DENIED_CAPABILITY/);
  assert.equal(countLedgerRows(), before);

  installLease('ledger.write');
  const written = await withProfile.callTool({
    name: 'add_ledger',
    arguments: { amount: -12 },
  });
  const writePayload = JSON.parse(textOf(written));
  assert.equal(writePayload.ok, true);
  assert.equal(Number.isInteger(writePayload.id), true);
  assert.equal(countLedgerRows(), before + 1);
  assert.deepEqual(lastLedgerRow(), {
    amount: -12,
    category: '其他',
    note: null,
    date: new Date().toISOString().slice(0, 10),
    author: 'fyodor_api',
  });

  await noProfile.close();
  await withProfile.close();
} finally {
  await new Promise((resolve) => listener.close(resolve));
  rmSync(tempRoot, { recursive: true, force: true });
}
const adapterSource = readFileSync(join(root, 'tools/ledger_internal_adapter.py'), 'utf8');
assert.match(adapterSource, /from tools\.product_handlers import/);
assert.doesNotMatch(adapterSource, /\b(SELECT|INSERT|UPDATE|DELETE)\b/i);
assert.doesNotMatch(adapterSource, /TODO_INTERNAL_DB_PATH/);
assert.doesNotMatch(adapterSource, /\/opt\/frontend\/memories\.db/);
console.log('test-m3-03a-ledger-internal: ok');
