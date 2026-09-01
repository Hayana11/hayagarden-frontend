import assert from 'node:assert/strict';
import http from 'node:http';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const serverPath = join(root, 'browser-mcp-server.js');

let mode = 'normal';
const browseRequests = [];
const shopServer = http.createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) body += chunk;
  browseRequests.push({ method: request.method, path: request.url, body });
  const input = JSON.parse(body || '{}');
  const payload = mode === 'escape'
    ? { ok: true, finalUrl: 'https://evil.example/escaped', text: 'SECRET ESCAPED PAGE TEXT' }
    : { ok: true, url: input.url, text: '商品正文', need_login: false };
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(payload));
});

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server.address().port));
  });
}

async function openClient(shopDaemonUrl) {
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [serverPath],
    cwd: root,
    env: {
      ...process.env,
      PYTHON: process.env.PYTHON || 'python3',
      UH_A0_REPO_ROOT: root,
      SHOP_DAEMON_URL: shopDaemonUrl,
    },
  });
  const client = new Client({ name: 'browser-taobao-read-e2e', version: '1.0.0' });
  await client.connect(transport);
  return { client, transport };
}

async function closeClient(client, transport) {
  try {
    await client.close();
  } catch {
    // Closing an already-terminated child is harmless for this contract test.
  }
  try {
    await transport.close();
  } catch {
    // The SDK may already have closed the stdio transport with the client.
  }
}

function resultText(result) {
  return (result.content || [])
    .filter((item) => item.type === 'text')
    .map((item) => item.text)
    .join('\n');
}

const port = await listen(shopServer);
const daemonUrl = 'http://127.0.0.1:' + port;
const first = await openClient(daemonUrl);
try {
  const listed = await first.client.listTools();
  assert.deepEqual(listed.tools.map((tool) => tool.name), ['taobao_read']);

  const valid = await first.client.callTool({
    name: 'taobao_read',
    arguments: { url: 'https://www.taobao.com/item.htm?id=1' },
  });
  assert.notEqual(valid.isError, true);
  assert.match(resultText(valid), /商品正文/);

  const beforeInvalid = browseRequests.length;
  const invalid = await first.client.callTool({
    name: 'taobao_read',
    arguments: { url: 'https://example.com/' },
  });
  assert.equal(invalid.isError, true);
  assert.equal(browseRequests.length, beforeInvalid);

  mode = 'escape';
  const escaped = await first.client.callTool({
    name: 'taobao_read',
    arguments: { url: 'https://www.taobao.com/item.htm?id=2' },
  });
  assert.equal(escaped.isError, true);
  assert.doesNotMatch(resultText(escaped), /SECRET ESCAPED PAGE TEXT/);
} finally {
  await closeClient(first.client, first.transport);
}

const unavailable = await openClient('http://127.0.0.1:1');
try {
  const failed = await unavailable.client.callTool({
    name: 'taobao_read',
    arguments: { url: 'https://www.taobao.com/item.htm?id=3' },
  });
  assert.equal(failed.isError, true);
} finally {
  await closeClient(unavailable.client, unavailable.transport);
}

assert.ok(browseRequests.every(({ method, path }) => method === 'POST' && path === '/browse'));
const serverSource = readFileSync(serverPath, 'utf8');
for (const forbidden of ['playwright', 'chromium', 'click', 'checkout', 'evaluate']) {
  assert.equal(serverSource.includes(forbidden), false, 'read surface contains no ' + forbidden + ' operation');
}

await new Promise((resolve, reject) => {
  shopServer.close((error) => (error ? reject(error) : resolve()));
});
console.log('browser MCP stdio E2E passed: tools/list exact + valid read + unavailable + invalid domain + final URL escape');
