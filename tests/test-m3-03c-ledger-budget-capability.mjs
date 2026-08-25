import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'm3-03c-ledger-budget-capability-'));
const dbPath = join(tempRoot, 'ledger-budget.db');
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

function seedBudgetDb() {
  python(
    [
      'import sqlite3, sys',
      'conn = sqlite3.connect(sys.argv[1])',
      'conn.execute("CREATE TABLE ledger_budget (month TEXT PRIMARY KEY, amount REAL NOT NULL)")',
      'conn.execute("INSERT INTO ledger_budget (month, amount) VALUES (?, ?)", ("2026-08", 1500))',
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [dbPath],
  );
}

function countBudgetRows() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM ledger_budget").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function installLease() {
  python(
    [
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="m3-03c-ledger-budget-read", turn_mode="chat", issued_from="explicit_user_intent", requested_capabilities=("ledger.budget.read",), issued_at="2026-08-25T00:00:00Z")',
      'write_current_turn_lease("' + leasePath.replaceAll('\\', '\\\\') + '", lease)',
    ].join('; '),
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

seedRuntimeStateDb();
seedBudgetDb();
const before = countBudgetRows();
const client = new Client({ name: 'm3-03c-ledger-budget-capability', version: '1.0.0' });
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
  assert.ok(names.has('ledger_budget_read'));
  assert.ok(names.has('ledger_read'));
  assert.ok(names.has('ledger_write'));
  const budgetRead = listed.tools.find((tool) => tool.name === 'ledger_budget_read');
  assert.equal(budgetRead.inputSchema.properties.month.type, 'string');

  installLease();
  const existing = await client.callTool({
    name: 'ledger_budget_read',
    arguments: { month: '2026-08' },
  });
  assert.deepEqual(JSON.parse(textOf(existing)), { amount: 1500 });
  assert.equal(countBudgetRows(), before);

  const missing = await client.callTool({
    name: 'ledger_budget_read',
    arguments: { month: '2026-09' },
  });
  assert.deepEqual(JSON.parse(textOf(missing)), { amount: null });
  assert.equal(countBudgetRows(), before);
} finally {
  await client.close();
  rmSync(tempRoot, { recursive: true, force: true });
}

console.log('test-m3-03c-ledger-budget-capability: ok');
