import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import proxy from '../capability-proxy-mcp-server.js';

const serverScript = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'capability-proxy-mcp-server.js');
const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'capability-mcp-http-'));
const leaseDir = path.join(tempRoot, 'hayagarden-uh-a0-turn-leases');
const fakePython = path.join(tempRoot, 'fake-python');
// The fence answer and the adapter payload both echo the lease path the
// subprocess actually received, so the test sees which turn a call ran under.
await fs.writeFile(fakePython, `#!/usr/bin/env node
const args = process.argv.slice(2).join(' ');
const lease = process.env.UH_A0_TURN_LEASE_PATH || '';
if (args.includes('verify-json')) {
  process.stdout.write(JSON.stringify({ lease_decision: 'ALLOW', verified_turn_id: lease }) + '\\n');
} else if (args.includes('gallery_capability_adapter')) {
  await new Promise((resolve) => setTimeout(resolve, 2500));
  process.stdout.write(JSON.stringify({ slow: true, lease_seen: lease }) + '\\n');
} else {
  process.stdout.write(JSON.stringify({ lease_seen: lease }) + '\\n');
}
`);
await fs.chmod(fakePython, 0o755);

const env = {
  ...process.env,
  PYTHON: fakePython,
  UH_A0_REPO_ROOT: tempRoot,
  UH_A0_TURN_LEASE_DIR: leaseDir,
  UH_A0_TURN_LEASE_PATH: path.join(tempRoot, 'must-not-be-used.json'),
  TODO_INTERNAL_DB_PATH: path.join(tempRoot, 'memories.db'),
  HAYA_DB_PATH: path.join(tempRoot, 'memories.db'),
  UH_A0_CAPABILITY_MCP_TOKEN: 'k'.repeat(48),
};
const auth = 'Bearer ' + env.UH_A0_CAPABILITY_MCP_TOKEN;
const leaseA = path.join(leaseDir, 'a'.repeat(32) + '.json');
const leaseB = path.join(leaseDir, 'b'.repeat(32) + '.json');

// Path acceptance: only <lease dir>/<32 hex>.json, exactly as written.
assert.equal(proxy.acceptedTurnLeasePath(leaseA, env), leaseA);
for (const rejected of [
  undefined,
  '',
  ['x'],
  'a'.repeat(32) + '.json',
  path.join(tempRoot, 'a'.repeat(32) + '.json'),
  path.join(leaseDir, 'sub', 'a'.repeat(32) + '.json'),
  leaseDir + '/../' + path.basename(leaseDir) + '/' + 'a'.repeat(32) + '.json',
  path.join(leaseDir, 'A'.repeat(32) + '.json'),
  path.join(leaseDir, 'a'.repeat(31) + '.json'),
  path.join(leaseDir, 'a'.repeat(32) + '.json.tmp'),
  path.join(leaseDir, '.' + 'a'.repeat(32) + '.json'),
]) {
  assert.equal(proxy.acceptedTurnLeasePath(rejected, env), null, `accepted ${String(rejected)}`);
}
console.log('CAPABILITY_LEASE_PATH_GATE=PASS');

// HTTP mode never starts without a real token.
for (const token of [undefined, '', 'k'.repeat(31)]) {
  assert.throws(() => proxy.createApp({ env: { ...env, UH_A0_CAPABILITY_MCP_TOKEN: token } }), /UH_A0_CAPABILITY_MCP_TOKEN/);
}
console.log('CAPABILITY_HTTP_REQUIRES_TOKEN_CONFIG=PASS');

// The server runs in its own process, as in production: a blocked event loop
// there must show up as latency here, not freeze the test's own clock.
const port = await new Promise((resolve) => {
  const probe = net.createServer().listen(0, '127.0.0.1', () => {
    const { port: free } = probe.address();
    probe.close(() => resolve(free));
  });
});
const url = new URL(`http://127.0.0.1:${port}/mcp`);

function startServer() {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [serverScript], {
      env: { ...env, CAPABILITY_MCP_PORT: String(port) },
      stdio: ['ignore', 'pipe', 'inherit'],
    });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { if (chunk.includes('listening')) resolve(child); });
    child.on('exit', (code) => reject(new Error('capability server exited ' + code)));
  });
}

function stopServer(child) {
  return new Promise((resolve) => {
    if (child.exitCode !== null) { resolve(); return; }
    child.removeAllListeners('exit');
    child.on('exit', () => resolve());
    child.kill('SIGTERM');
  });
}

let server = await startServer();

async function connect(leasePath, authorization = auth) {
  const headers = { 'X-UH-A0-Profile': 'uh_a0' };
  if (authorization !== null) headers.Authorization = authorization;
  if (leasePath !== undefined) headers['X-UH-A0-Turn-Lease-Path'] = leasePath;
  const client = new Client({ name: 'capability-http-test', version: '1.0.0' });
  await client.connect(new StreamableHTTPClientTransport(url, { requestInit: { headers } }));
  return client;
}

try {
  for (const leasePath of [undefined, path.join(tempRoot, 'a'.repeat(32) + '.json'), 'relative.json']) {
    await assert.rejects(connect(leasePath), `initialize accepted lease path ${String(leasePath)}`);
  }
  console.log('CAPABILITY_HTTP_INIT_REJECTS_BAD_LEASE=PASS');

  // A valid lease path is not enough: lease paths are visible to local users.
  for (const authorization of [null, 'Bearer ' + 'x'.repeat(48), 'Bearer ' + 'k'.repeat(47), 'Bearer ' + 'k'.repeat(49), 'Basic ' + 'k'.repeat(48)]) {
    await assert.rejects(connect(leaseA, authorization), `initialize accepted authorization ${String(authorization)}`);
  }
  console.log('CAPABILITY_HTTP_REJECTS_BAD_TOKEN=PASS');

  const clientA = await connect(leaseA);
  const clientB = await connect(leaseB);
  try {
    const listed = await clientA.listTools();
    assert.deepEqual(
      listed.tools.map((tool) => tool.name).sort(),
      [...proxy.CAPABILITY_PROXY_TOOL_NAMES].sort(),
    );
    console.log('CAPABILITY_HTTP_TOOLS_LIST=PASS');

    // Interleave calls: each client's requests run under its own lease.
    for (const [client, lease] of [[clientA, leaseA], [clientB, leaseB], [clientA, leaseA]]) {
      const result = await client.callTool({ name: 'todo_read', arguments: {} });
      assert.deepEqual(JSON.parse(result.content[0].text), { lease_seen: lease });
    }
    console.log('CAPABILITY_HTTP_LEASE_ISOLATION=PASS');

    // A slow adapter for one resident must not stall another resident.
    const slow = clientA.callTool({ name: 'gallery_screenshot', arguments: {} });
    await new Promise((resolve) => setTimeout(resolve, 300));
    const started = Date.now();
    const quick = await clientB.callTool({ name: 'todo_read', arguments: {} });
    const quickMs = Date.now() - started;
    assert.deepEqual(JSON.parse(quick.content[0].text), { lease_seen: leaseB });
    assert.ok(quickMs < 1500, `second resident waited ${quickMs}ms behind a slow adapter`);
    const slowResult = await slow;
    assert.deepEqual(JSON.parse(slowResult.content[0].text), { slow: true, lease_seen: leaseA });
    console.log(`CAPABILITY_HTTP_NON_BLOCKING=PASS quick_ms=${quickMs}`);

    // Stateless: a request is checked on its own headers, never on history.
    const listBody = JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/list', params: {} });
    const base = { 'content-type': 'application/json', accept: 'application/json, text/event-stream' };
    const noLease = await fetch(url, { method: 'POST', headers: { ...base, Authorization: auth }, body: listBody });
    assert.equal(noLease.status, 403);
    const noToken = await fetch(url, { method: 'POST', headers: { ...base, 'X-UH-A0-Turn-Lease-Path': leaseA }, body: listBody });
    assert.equal(noToken.status, 401);
    assert.equal((await fetch(url, { method: 'GET' })).status, 401);
    assert.equal((await fetch(url, { method: 'GET', headers: { Authorization: auth } })).status, 405);
    console.log('CAPABILITY_HTTP_PER_REQUEST_LEASE_GATE=PASS');

    // No session state: after a restart the next call needs no re-initialize.
    await stopServer(server);
    server = await startServer();
    // Fresh socket (agent: false): only the server side is under test here.
    const after = await new Promise((resolve, reject) => {
      const request = http.request(url, {
        method: 'POST',
        agent: false,
        headers: {
          'content-type': 'application/json',
          accept: 'application/json, text/event-stream',
          'X-UH-A0-Turn-Lease-Path': leaseA,
          Authorization: auth,
        },
      }, (response) => {
        let raw = '';
        response.setEncoding('utf8');
        response.on('data', (chunk) => { raw += chunk; });
        response.on('end', () => resolve({ status: response.statusCode, raw }));
      });
      request.on('error', reject);
      request.end(JSON.stringify({ jsonrpc: '2.0', id: 10, method: 'tools/call', params: { name: 'todo_read', arguments: {} } }));
    });
    assert.equal(after.status, 200);
    const line = after.raw.trim().startsWith('{')
      ? after.raw
      : after.raw.split('\n').find((item) => item.startsWith('data: ')).slice(6);
    assert.deepEqual(JSON.parse(JSON.parse(line).result.content[0].text), { lease_seen: leaseA });
    console.log('CAPABILITY_HTTP_NO_SESSION_AFTER_RESTART=PASS');
  } finally {
    await clientA.close().catch(() => {});
    await clientB.close().catch(() => {});
  }
} finally {
  await stopServer(server);
  await fs.rm(tempRoot, { recursive: true, force: true });
}
