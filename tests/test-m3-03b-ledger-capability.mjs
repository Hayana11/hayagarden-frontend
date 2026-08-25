import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-03b-ledger-capability-'));
const dbPath = join(tempRoot, 'ledger.db');
const runtimeStateDbPath = join(tempRoot, 'runtime-state.db');
const leasePath = join(tempRoot, 'turn-lease.json');

process.env.UH_A0_REPO_ROOT = root;
process.env.HAYAGARDEN_CONFIG_DB_PATH = runtimeStateDbPath;
process.env.UH_A0_TURN_LEASE_PATH = leasePath;

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: {
      ...process.env,
      UH_A0_REPO_ROOT: root,
      HAYAGARDEN_CONFIG_DB_PATH: runtimeStateDbPath,
      UH_A0_TURN_LEASE_PATH: leasePath,
    },
    encoding: 'utf8',
  }).trim();
}

function seedRuntimeStateDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      `conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT (datetime('now')))")`,
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [runtimeStateDbPath],
  );
}

function seedDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      'conn.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL NOT NULL, category TEXT, note TEXT, date TEXT, author TEXT, meta TEXT)")',
      'conn.executemany("INSERT INTO ledger (amount, category, note, date, author, meta) VALUES (?,?,?,?,?,?)", [(100,"工资","八月收入","2026-08-20","alice",None),(-35,"餐饮","晚饭","2026-08-18","alice",None),(-20,"交通","七月交通","2026-07-28","alice",None)])',
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [dbPath],
  );
}

function countRows() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function installLease() {
  python(
    [
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="m3-03b-ledger-read", turn_mode="chat", issued_from="explicit_user_intent", requested_capabilities=("ledger.read",), issued_at="2026-08-25T00:00:00Z")',
      'write_current_turn_lease("' + leasePath.replaceAll('\\', '\\\\') + '", lease)',
    ].join('; '),
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

seedRuntimeStateDb();
seedDb();
const before = countRows();
const client = new Client({ name: 'm3-03b-ledger-capability', version: '1.0.0' });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [join(root, 'capability-proxy-mcp-server.js')],
  env: {
    ...process.env,
    UH_A0_REPO_ROOT: root,
    HAYAGARDEN_CONFIG_DB_PATH: runtimeStateDbPath,
    TODO_INTERNAL_DB_PATH: dbPath,
    UH_A0_TURN_LEASE_PATH: leasePath,
  },
});

try {
  await client.connect(transport);
  const listed = await client.listTools();
  const names = new Set(listed.tools.map((tool) => tool.name));
  assert.ok(names.has('ledger_read'));
  assert.ok(names.has('ledger_write'));
  const ledgerRead = listed.tools.find((tool) => tool.name === 'ledger_read');
  assert.equal(ledgerRead.inputSchema.properties.month.type, 'string');

  installLease();
  const result = await client.callTool({
    name: 'ledger_read',
    arguments: { month: '2026-08' },
  });
  const payload = JSON.parse(textOf(result));
  assert.deepEqual(payload.summary, {
    income: 100,
    expense: -35,
    balance: 65,
    prev_expense: -20,
  });
  assert.equal(payload.records.length, 2);
  assert.equal(payload.records[0].amount, 100);
  assert.equal(payload.records[1].amount, -35);
  assert.equal(countRows(), before);
} finally {
  await client.close();
  rmSync(tempRoot, { recursive: true, force: true });
}

console.log('test-m3-03b-ledger-capability: ok');
