import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'uh-a0-no-generic-ask-'));
const commandsPath = join(tempRoot, 'commands.db');
const runtimePath = join(tempRoot, 'runtime.db');
const leasePath = join(tempRoot, 'turn-lease.json');
const proxyPath = join(root, 'capability-proxy-mcp-server.js');

function python(code, args = []) {
  return execFileSync(process.env.PYTHON || 'python3', ['-c', code, ...args], {
    cwd: root,
    env: {
      ...process.env,
      UH_A0_REPO_ROOT: root,
      UH_A0_TURN_LEASE_PATH: leasePath,
      HAYAGARDEN_CONFIG_DB_PATH: runtimePath,
      TASK_TIMER_COMMANDS_DB_PATH: commandsPath,
    },
    encoding: 'utf8',
  }).trim();
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

try {
  python(
    [
      'import sqlite3, sys',
      'from command_store import _init',
      '_init(sys.argv[1])',
      'conn = sqlite3.connect(sys.argv[2])',
      'conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")',
      'conn.commit(); conn.close()',
    ].join('; '),
    [commandsPath, runtimePath],
  );

  python(
    [
      'import sys',
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="uh-a0-real-chat", turn_mode="chat", issued_from="default_policy", issued_at="2026-08-28T00:00:00Z")',
      'write_current_turn_lease(sys.argv[1], lease)',
    ].join('; '),
    [leasePath],
  );

  const client = new Client({ name: 'uh-a0-no-generic-ask', version: '1.0.0' });
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [proxyPath],
    env: {
      ...process.env,
      UH_A0_REPO_ROOT: root,
      UH_A0_TURN_LEASE_PATH: leasePath,
      HAYAGARDEN_CONFIG_DB_PATH: runtimePath,
      TASK_TIMER_COMMANDS_DB_PATH: commandsPath,
      TODO_INTERNAL_DB_PATH: runtimePath,
    },
  });
  await client.connect(transport);

  const result = await client.callTool({
    name: 'task_timer_start',
    arguments: { title: 'UH-A0 real timer', countdown_seconds: 60 },
  });
  assert.equal(textOf(result), 'TASK_TIMER_CREATED');
  assert.notEqual(textOf(result), 'UH-A0 CAPABILITY_ASK_REQUIRED');

  const row = JSON.parse(python(
    'import json, sqlite3, sys; conn=sqlite3.connect(sys.argv[1]); row=conn.execute("SELECT title, countdown_seconds, created_by FROM commands ORDER BY id DESC LIMIT 1").fetchone(); print(json.dumps(row)); conn.close()',
    [commandsPath],
  ));
  assert.deepEqual(row, ['UH-A0 real timer', 60, 'fyodor']);

  await client.close();
  console.log('test-uh-a0-remove-generic-ask: ok');
} finally {
  rmSync(tempRoot, { recursive: true, force: true });
}
