import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import proxy from '../capability-proxy-mcp-server.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'gallery-capability-smoke-'));
const fakePython = path.join(tempRoot, 'fake-python');
await fs.writeFile(fakePython, `#!/usr/bin/env node
const args = process.argv.slice(2).join(' ');
if (args.includes('verify-json')) {
  process.stdout.write(JSON.stringify({ lease_decision: 'ALLOW', verified_turn_id: 'smoke-turn' }) + '\\n');
} else if (args.includes('gallery_capability_adapter')) {
  await new Promise((resolve) => setTimeout(resolve, 6000));
  process.stdout.write(JSON.stringify({ ok: true, fake_adapter: true }) + '\\n');
} else {
  await new Promise((resolve) => setTimeout(resolve, 6000));
  process.stdout.write(JSON.stringify({ ok: true, fake_adapter: false }) + '\\n');
}
`);
await fs.chmod(fakePython, 0o755);

assert.equal(proxy.adapterTimeoutMs('memory_search'), 5000);
assert.equal(proxy.adapterTimeoutMs('gallery_save'), 50000);
assert.equal(proxy.adapterTimeoutMs('gallery_recall'), 25000);
assert.equal(proxy.adapterTimeoutMs('gallery_screenshot'), 50000);
for (const value of [
  proxy.adapterTimeoutMs('memory_search'),
  proxy.adapterTimeoutMs('gallery_save'),
  proxy.adapterTimeoutMs('gallery_recall'),
  proxy.adapterTimeoutMs('gallery_screenshot'),
]) {
  assert.ok(Number.isFinite(value) && value > 0);
}

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(root, 'capability-proxy-mcp-server.js')],
  env: {
    ...process.env,
    PYTHON: fakePython,
    UH_A0_REPO_ROOT: root,
    UH_A0_TURN_LEASE_PATH: path.join(root, '.test-missing-turn-lease.json'),
    HAYA_DB_PATH: path.join(root, '.test-missing-memories.db'),
    HAYAGARDEN_GALLERY_ROOT: root,
    HAYAGARDEN_ATTACHMENTS_ROOT: root,
  },
  stderr: 'pipe',
});
const client = new Client({ name: 'gallery-r1-schema-smoke', version: '1.0.0' });

try {
  await client.connect(transport);
  const result = await client.listTools();
  const tools = new Map(result.tools.map((tool) => [tool.name, tool]));
  for (const name of ['gallery_save', 'gallery_recall', 'gallery_screenshot']) {
    assert.ok(tools.has(name), `${name} missing from tools/list`);
  }
  const save = tools.get('gallery_save').inputSchema;
  assert.deepEqual(Object.keys(save.properties).sort(), [
    'album', 'attachment', 'first_impression', 'image_index', 'note',
  ]);
  assert.equal(save.properties.image_index.minimum, 0);
  assert.equal(save.properties.image_index.maximum, 3);
  assert.equal(save.properties.first_impression.maxLength, 800);
  const recall = tools.get('gallery_recall').inputSchema;
  assert.equal(recall.properties.inspect_question.maxLength, 500);
  assert.deepEqual(tools.get('gallery_screenshot').inputSchema.properties.viewpoint.enum, ['fyodor', 'hayana']);
  console.log('GALLERY_CAPABILITY_MCP_TOOLS_LIST=PASS');

  for (const [name, input] of [
    ['gallery_save', { note: 'smoke' }],
    ['gallery_recall', { inspect_question: 'smoke' }],
    ['gallery_screenshot', { viewpoint: 'fyodor' }],
  ]) {
    const started = Date.now();
    const execution = await client.callTool({ name, arguments: input });
    const elapsed = Date.now() - started;
    assert.equal(execution.isError, undefined, `${name} execution failed`);
    assert.match(execution.content?.[0]?.text || '', /fake_adapter/);
    assert.ok(elapsed >= 5000, `${name} did not exercise the >5s adapter path`);
    console.log(`GALLERY_${name.toUpperCase()}_EXECUTION=PASS elapsed_ms=${elapsed}`);
  }

  process.env.UH_A0_REPO_ROOT = root;
  process.env.PYTHON = fakePython;
  process.env.TODO_INTERNAL_DB_PATH = path.join(tempRoot, 'todo.db');
  assert.throws(
    () => proxy.callAdapter('memory_search', { keyword: 'timeout' }),
    /timed out|ETIMEDOUT|SIGTERM/i,
  );
  console.log('DEFAULT_PROXY_5S_TIMEOUT=PASS');
} finally {
  await client.close();
  await fs.rm(tempRoot, { recursive: true, force: true });
}

