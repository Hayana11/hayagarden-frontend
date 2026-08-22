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
      ['add_ledger', 'add_todo', 'get_ledger', 'get_ledger_budget', 'get_todos', 'search_memories'],
      'Internal MCP catalog must contain exactly the six Todo, Ledger, and Memory tools',
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

    const getLedger = tools.find((tool) => tool?.name === 'get_ledger');
    assert.ok(getLedger, 'get_ledger must be listed');
    assert.deepEqual(Object.keys(schemaFor(getLedger).properties || {}).sort(), ['month']);
    assert.deepEqual(schemaFor(getLedger).required || [], []);

    const getLedgerBudget = tools.find((tool) => tool?.name === 'get_ledger_budget');
    assert.ok(getLedgerBudget, 'get_ledger_budget must be listed');
    assert.deepEqual(Object.keys(schemaFor(getLedgerBudget).properties || {}).sort(), ['month']);
    assert.deepEqual(schemaFor(getLedgerBudget).required || [], []);

    const addLedger = tools.find((tool) => tool?.name === 'add_ledger');
    assert.ok(addLedger, 'add_ledger must be listed');
    const ledgerSchema = schemaFor(addLedger);
    assert.deepEqual(ledgerSchema.required || [], ['amount']);
    assert.deepEqual(
      Object.keys(ledgerSchema.properties || {}).sort(),
      ['amount', 'category', 'date', 'note'],
    );
    assert.equal(ledgerSchema.properties?.amount?.type, 'number');
    assert.equal(ledgerSchema.properties?.category?.type, 'string');
    assert.equal(ledgerSchema.properties?.note?.type, 'string');
    assert.equal(ledgerSchema.properties?.date?.type, 'string');

    const searchMemories = tools.find((tool) => tool?.name === 'search_memories');
    assert.ok(searchMemories, 'search_memories must be listed');
    const memorySchema = schemaFor(searchMemories);
    assert.deepEqual(memorySchema.required || [], ['keyword']);
    assert.deepEqual(
      Object.keys(memorySchema.properties || {}).sort(),
      ['keyword'],
    );
    assert.equal(memorySchema.properties?.keyword?.type, 'string');

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
