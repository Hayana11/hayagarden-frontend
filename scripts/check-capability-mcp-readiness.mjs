#!/usr/bin/env node
// Readiness for the resident capability MCP: initialize + tools/list only.
// No tool is called, so no adapter, fence or database is touched.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const require = createRequire(import.meta.url);
const { CAPABILITY_PROXY_TOOL_NAMES, SERVICE_TOKEN_ENV } = require('../capability-proxy-mcp-server.js');
const endpoint = new URL(process.env.CAPABILITY_MCP_URL || 'http://127.0.0.1:3102/mcp');
const leaseDir = path.resolve(
  String(process.env.UH_A0_TURN_LEASE_DIR || '').trim()
    || path.join(os.tmpdir(), 'hayagarden-uh-a0-turn-leases'),
);
// A well-formed path is enough for initialize; the file need not exist.
const probeLease = path.join(leaseDir, '0'.repeat(32) + '.json');

// The service reads its token from the same root-only env file (systemd
// EnvironmentFile); never print it.
function serviceToken() {
  const direct = String(process.env[SERVICE_TOKEN_ENV] || '').trim();
  if (direct) return direct;
  const envFile = process.env.CAPABILITY_MCP_ENV_FILE || '/opt/frontend/.env';
  const line = fs.readFileSync(envFile, 'utf8').split('\n')
    .find((item) => item.startsWith(SERVICE_TOKEN_ENV + '='));
  return line ? line.slice(SERVICE_TOKEN_ENV.length + 1).trim().replace(/^(['"])(.*)\1$/, '$2') : '';
}

async function main() {
  const token = serviceToken();
  assert.ok(token, SERVICE_TOKEN_ENV + ' is not configured');
  const base = { 'content-type': 'application/json', accept: 'application/json, text/event-stream' };
  const body = JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} });
  const noToken = await fetch(endpoint, { method: 'POST', headers: { ...base, 'X-UH-A0-Turn-Lease-Path': probeLease }, body });
  assert.equal(noToken.status, 401, 'requests without the service token must be refused');
  const noLease = await fetch(endpoint, { method: 'POST', headers: { ...base, Authorization: 'Bearer ' + token }, body });
  assert.equal(noLease.status, 403, 'requests without a turn lease header must be refused');

  const client = new Client({ name: 'capability-mcp-readiness', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(endpoint, {
    requestInit: {
      headers: {
        Authorization: 'Bearer ' + token,
        'X-UH-A0-Profile': 'uh_a0',
        'X-UH-A0-Turn-Lease-Path': probeLease,
      },
    },
  });
  try {
    await client.connect(transport);
    const listed = await client.listTools();
    const names = (listed?.tools || []).map((tool) => String(tool?.name || '')).sort();
    assert.deepEqual(names, [...CAPABILITY_PROXY_TOOL_NAMES].sort());
    process.stdout.write(JSON.stringify({ status: 'PASS', endpoint: String(endpoint), tools: names }) + '\n');
  } finally {
    client.close().catch(() => {});
  }
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write('CAPABILITY_MCP_READINESS_FAILED: ' + error.message + '\n');
    process.exit(1);
  },
);
