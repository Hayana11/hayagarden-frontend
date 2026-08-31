import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import { ToolSchema } from '@modelcontextprotocol/sdk/types.js';

import {
  buildServer,
  callSurface,
  listSurface,
  mcpCallResult,
} from '../external-mcp-surface-server.js';

test('list and call stay on the injected local bridge', () => {
  const requests = [];
  const runner = (request) => {
    requests.push(request);
    if (request.operation === 'list') {
      return {
        tools: [{
          name: 'calendar__list__abc123',
          description: 'List calendar entries',
          inputSchema: { type: 'object', properties: {} },
        }],
      };
    }
    return { status: 'SUCCESS', result: { entries: [] } };
  };

  assert.deepEqual(listSurface(runner).tools[0].name, 'calendar__list__abc123');
  assert.deepEqual(callSurface('calendar__list__abc123', {}, runner), {
    status: 'SUCCESS',
    result: { entries: [] },
  });
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1], {
    operation: 'call',
    name: 'calendar__list__abc123',
    arguments: {},
  });
  assert.equal(mcpCallResult({ status: 'TOOL_ERROR' }).isError, true);
  assert.ok(buildServer({ runner }));
});


test('surface names satisfy the installed MCP ToolSchema contract', () => {
  const generated = JSON.parse(execFileSync(
    'python3',
    ['-c', [
      'import json',
      'from tools.external_mcp_surface import surface_tool_name',
      'print(json.dumps({',
      '  "long": surface_tool_name("S" * 500, "T" * 500, "ext:long:roll"),',
      '  "non_ascii": surface_tool_name("中文服务器", "骰子工具", "ext:cn:roll"),',
      '}))',
    ].join('; '),
    { cwd: process.cwd(), encoding: 'utf8' },
  ));
  const toolRecord = (name) => ({
    name,
    description: 'bounded',
    inputSchema: { type: 'object', properties: {} },
  });
  assert.equal(ToolSchema.safeParse(toolRecord(generated.long)).success, true);
  assert.equal(ToolSchema.safeParse(toolRecord(generated.non_ascii)).success, true);

  let installedMax = 0;
  for (let length = 1; length <= 512; length += 1) {
    if (!ToolSchema.safeParse(toolRecord('x'.repeat(length))).success) {
      installedMax = length - 1;
      break;
    }
  }
  if (installedMax > 0) assert.ok(generated.long.length <= installedMax);
  assert.ok(generated.long.length <= 64);
});
