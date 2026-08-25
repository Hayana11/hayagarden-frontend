import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { Socket } from 'node:net';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { readLightStatus } from '../light-status-adapter.js';

const root = process.cwd();
const tempRoot = mkdtempSync(join(tmpdir(), 'home-light-status-capability-'));
const runtimeStateDbPath = join(tempRoot, 'runtime-state.db');
const leasePath = join(tempRoot, 'turn-lease.json');

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
      'conn.execute("CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME DEFAULT (datetime(\'now\')))")',
      'conn.commit()',
      'conn.close()',
    ].join('; '),
    [runtimeStateDbPath],
  );
}

function installLease() {
  python(
    [
      'from tools.lease_signer import issue_turn_lease',
      'from tools.execution_fence import write_current_turn_lease',
      'lease = issue_turn_lease(turn_id="s4-home-light-status", turn_mode="chat", issued_from="explicit_user_intent", requested_capabilities=("home.light.status",), issued_at="2026-08-25T00:00:00Z")',
      'write_current_turn_lease("' + leasePath.replaceAll('\\', '\\\\') + '", lease)',
    ].join('; '),
  );
}

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

function waitForPort(port) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 10_000;
    const attempt = () => {
      const socket = new Socket();
      socket.setTimeout(250);
      socket.once('connect', () => {
        socket.destroy();
        resolve();
      });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() >= deadline) reject(new Error('timed out waiting for MCP server'));
        else setTimeout(attempt, 50);
      });
      socket.once('timeout', () => {
        socket.destroy();
        if (Date.now() >= deadline) reject(new Error('timed out waiting for MCP server'));
        else setTimeout(attempt, 50);
      });
      socket.connect(port, '127.0.0.1');
    };
    attempt();
  });
}

const requests = [];
const daemon = createServer((request, response) => {
  requests.push({ method: request.method, path: request.url });
  if (request.method !== 'GET' || request.url !== '/light/status') {
    response.writeHead(405);
    response.end('method not allowed');
    return;
  }
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end('{"ok":true,"result":{"bedside":{"zone":"bedside","power":true}}}');
});

seedRuntimeStateDb();
assert.equal(
  await readLightStatus({
    fetchImpl: async () => ({ text: async () => '' }),
  }),
  '(no output)',
);
assert.equal(
  await readLightStatus({
    fetchImpl: async () => { throw new Error('daemon unavailable'); },
  }),
  'Error: daemon unavailable',
);

let proxyClient;
let homeClient;
let homeProcess;
try {
  await new Promise((resolve) => daemon.listen(0, '127.0.0.1', resolve));
  const daemonPort = daemon.address().port;
  const daemonUrl = 'http://127.0.0.1:' + daemonPort;

  const commonEnv = {
    ...process.env,
    UH_A0_REPO_ROOT: root,
    HAYAGARDEN_CONFIG_DB_PATH: runtimeStateDbPath,
    UH_A0_TURN_LEASE_PATH: leasePath,
    HAYAGARDEN_LIGHT_DAEMON_URL: daemonUrl,
  };

  proxyClient = new Client({ name: 's4-capability-proxy', version: '1.0.0' });
  const proxyTransport = new StdioClientTransport({
    command: process.execPath,
    args: [join(root, 'capability-proxy-mcp-server.js')],
    env: { ...commonEnv, TODO_INTERNAL_DB_PATH: join(tempRoot, 'unused.db') },
  });
  await proxyClient.connect(proxyTransport);
  const proxyListed = await proxyClient.listTools();
  const proxyNames = new Set(proxyListed.tools.map((tool) => tool.name));
  for (const name of [
    'memory_search',
    'memory_write',
    'home_light_status',
    'todo_read',
    'todo_write',
    'ledger_read',
    'ledger_budget_read',
    'ledger_write',
  ]) assert.ok(proxyNames.has(name), name);
  const homeStatusSchema = proxyListed.tools.find((tool) => tool.name === 'home_light_status').inputSchema;
  assert.deepEqual(homeStatusSchema, {
    type: 'object',
    properties: {},
    $schema: 'http://json-schema.org/draft-07/schema#',
  });

  installLease();
  const proxyResult = await proxyClient.callTool({
    name: 'home_light_status',
    arguments: {},
  });
  const proxyPayload = JSON.parse(textOf(proxyResult));
  assert.equal(proxyPayload.ok, true);
  assert.ok(proxyPayload.result.bedside);
  assert.equal(Object.hasOwn(proxyPayload.result, 'main'), false);

  homeProcess = spawn(process.execPath, [join(root, 'mcp-http-server.js')], {
    cwd: root,
    env: commonEnv,
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  await waitForPort(3100);

  homeClient = new Client({ name: 's4-home-provider', version: '1.0.0' });
  const homeTransport = new StreamableHTTPClientTransport(
    new URL('http://127.0.0.1:3100/mcp'),
  );
  await homeClient.connect(homeTransport);
  const homeListed = await homeClient.listTools();
  assert.ok(homeListed.tools.some((tool) => tool.name === 'get_light_status'));
  const homeResult = await homeClient.callTool({
    name: 'get_light_status',
    arguments: {},
  });
  const homePayload = JSON.parse(textOf(homeResult));
  assert.deepEqual(homePayload, proxyPayload);
  assert.equal(homePayload.ok, true);
  assert.ok(homePayload.result.bedside);
  assert.equal(Object.hasOwn(homePayload.result, 'main'), false);

  assert.equal(requests.length, 2);
  assert.ok(requests.every((request) => request.method === 'GET'));
  assert.ok(requests.every((request) => request.path === '/light/status'));

  await new Promise((resolve) => daemon.close(resolve));
  const proxyError = await proxyClient.callTool({
    name: 'home_light_status',
    arguments: {},
  });
  const homeError = await homeClient.callTool({
    name: 'get_light_status',
    arguments: {},
  });
  assert.match(textOf(proxyError), /^Error: /);
  assert.equal(textOf(proxyError), textOf(homeError));
} finally {
  await proxyClient?.close().catch(() => {});
  await homeClient?.close().catch(() => {});
  if (homeProcess) {
    homeProcess.kill('SIGTERM');
    await new Promise((resolve) => homeProcess.once('exit', resolve));
  }
  if (daemon.listening) await new Promise((resolve) => daemon.close(resolve));
  rmSync(tempRoot, { recursive: true, force: true });
}

console.log('test-home-light-status-capability: ok');
