import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'calendar-capability-'));
const dbPath = join(tempRoot, 'memories.db');
const runtimeStateDbPath = join(tempRoot, 'runtime-state.db');
const leasePath = join(tempRoot, 'turn-lease.json');
const device = '6f1c2a7e-4b7f-4c9e-9d1e-3a2b1c0d9e8f';

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

function seed() {
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
  // One phone sync with write permission and one event in Fyodor's calendar.
  python(
    [
      'import sys, datetime as dt',
      'from tools import calendar_store as cs',
      'begin = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 3600000',
      'cs.sync_device(sys.argv[1], sys.argv[2], {"schemaVersion": 1, "permission": {"read": True, "write": True}, "timezone": "Asia/Shanghai", "events": [{"eventId": "8", "beginMs": begin, "endMs": begin + 1800000, "allDay": False, "own": True, "calendarName": "费佳", "title": "喝水"}]})',
    ].join('\n'),
    [dbPath, device],
  );
}

// Chat and Wake default leases carry calendar.read and calendar.write; a
// task lease carries neither.
function installLease(mode) {
  python(
    [
      'import sys',
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="calendar-capability", turn_mode=sys.argv[2], issued_from="default_policy", issued_at="2026-10-25T00:00:00Z")',
      'write_current_turn_lease(sys.argv[1], lease)',
    ].join('\n'),
    [leasePath, mode],
  );
}

function pendingCount() {
  return Number(python(
    'import sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute("SELECT COUNT(*) FROM calendar_commands WHERE state=\'pending\'").fetchone()[0]); conn.close()',
    [dbPath],
  ));
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

seed();
const client = new Client({ name: 'calendar-capability', version: '1.0.0' });
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
  const write = listed.tools.find((tool) => tool.name === 'calendar_write');
  assert.ok(listed.tools.some((tool) => tool.name === 'calendar_read'));
  assert.deepEqual(write.inputSchema.properties.op.enum, ['create', 'update', 'delete']);

  installLease('chat');
  const read = JSON.parse(textOf(await client.callTool({ name: 'calendar_read', arguments: { days: 2 } })));
  assert.equal(read.status, 'OK');
  assert.ok(read.events.some((event) => event.title === '喝水' && event.mine === true));

  installLease('task');
  const denied = await client.callTool({
    name: 'calendar_write',
    arguments: { op: 'create', title: '吃饭', start: '2030-01-01 12:00' },
  });
  assert.match(textOf(denied), /^UH-A0 /);
  assert.equal(pendingCount(), 0);

  installLease('chat');
  const invalid = JSON.parse(textOf(await client.callTool({
    name: 'calendar_write',
    arguments: { op: 'delete', event_id: 'not-mine' },
  })));
  assert.equal(invalid.status, 'INVALID_INPUT');
  const queued = JSON.parse(textOf(await client.callTool({
    name: 'calendar_write',
    arguments: { op: 'create', title: '吃饭', start: '2030-01-01 12:00' },
  })));
  assert.equal(queued.status, 'QUEUED');
  assert.equal(queued.write.start, '2030-01-01 12:00');
  assert.equal(pendingCount(), 1);
} finally {
  await client.close();
  rmSync(tempRoot, { recursive: true, force: true });
}

console.log('test-calendar-capability: ok');
