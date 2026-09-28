import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [path.join(root, 'capability-proxy-mcp-server.js')],
  env: {
    ...process.env,
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
} finally {
  await client.close();
}

