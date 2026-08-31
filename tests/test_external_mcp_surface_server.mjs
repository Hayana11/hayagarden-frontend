import assert from 'node:assert/strict';
import test from 'node:test';

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
