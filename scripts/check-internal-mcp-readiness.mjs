#!/usr/bin/env node
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const DEFAULT_ENDPOINT = 'http://127.0.0.1:3101/mcp';
const endpoint = process.env.INTERNAL_MCP_URL || DEFAULT_ENDPOINT;

function schemaFor(tool) {
  return tool?.inputSchema || tool?.input_schema || {};
}

async function main() {
  const client = new Client({ name: 'internal-mcp-readiness', version: '1.0.0' });
  const transport = new StreamableHTTPClientTransport(
    new URL(endpoint),
    {
      requestInit: {
        headers: {
          'X-UH-A0-Profile': 'uh_a0',
        },
      },
    },
  );

  try {
    // Client.connect performs MCP initialize. No callTool is made in this check.
    await client.connect(transport);
    const listed = await client.listTools();
    const tools = Array.isArray(listed?.tools) ? listed.tools : [];
    const names = tools.map((tool) => String(tool?.name || ''));

    assert.deepEqual(
      [...names].sort(),
      ['add_todo', 'get_todos'],
      'Internal MCP catalog must contain exactly get_todos and add_todo',
    );

    const addTodo = tools.find((tool) => tool?.name === 'add_todo');
    assert.ok(addTodo, 'add_todo must be listed');
    const schema = schemaFor(addTodo);
    assert.deepEqual(schema.required || [], ['content']);
    assert.deepEqual(
      Object.keys(schema.properties || {}).sort(),
      ['content', 'due_date'],
    );
    assert.equal(schema.properties?.content?.type, 'string');
    assert.equal(schema.properties?.due_date?.type, 'string');

    process.stdout.write(
      JSON.stringify({
        status: 'PASS',
        endpoint,
        tools: names,
        write_tools_called: false,
      }) + '\n',
    );
  } finally {
    await client.close().catch(() => {});
  }
}

main().catch((error) => {
  process.stderr.write('INTERNAL_MCP_READINESS_FAILED: ' + error.message + '\n');
  process.exitCode = 1;
});
