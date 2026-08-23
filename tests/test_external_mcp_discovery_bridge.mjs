import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BRIDGE_VERSION,
  executeBridgeEnvelope,
} from '../tools/external_mcp_discovery_bridge.mjs';

const success = {
  status: 'SUCCESS',
  catalog_complete: true,
  zero_tools: true,
  tools: [],
  diagnostics: { request_count: 1 },
  error: null,
};

test('versioned envelope accepts only trusted endpoint and transport fields', async () => {
  const received = [];
  const result = await executeBridgeEnvelope(
    {
      bridge_version: BRIDGE_VERSION,
      endpoint: 'https://calendar.example/mcp',
      transport: 'streamable_http',
    },
    {
      discover: async (input) => {
        received.push(input);
        return success;
      },
    },
  );
  assert.equal(result.bridge_version, BRIDGE_VERSION);
  assert.deepEqual(received, [{ endpoint: 'https://calendar.example/mcp', transport: 'streamable_http' }]);
  await assert.rejects(
    () => executeBridgeEnvelope(
      { bridge_version: BRIDGE_VERSION, endpoint: 'https://calendar.example/mcp', transport: 'streamable_http', tool_name: 'unexpected' },
      { discover: async () => success },
    ),
    /unsupported fields/,
  );
  await assert.rejects(
    () => executeBridgeEnvelope(
      { bridge_version: 99, endpoint: 'https://calendar.example/mcp', transport: 'streamable_http' },
      { discover: async () => success },
    ),
    /unsupported/,
  );
});

test('bridge rejects malformed discovery output instead of inventing a catalog', async () => {
  await assert.rejects(
    () => executeBridgeEnvelope(
      { bridge_version: BRIDGE_VERSION, endpoint: 'https://calendar.example/mcp', transport: 'streamable_http' },
      { discover: async () => ({ status: 'SUCCESS' }) },
    ),
    /schema is incomplete/,
  );
});

test('bridge production entrypoint is bound to the completed discovery core', async () => {
  const source = await import('node:fs/promises').then((fs) => fs.readFile(new URL('../tools/external_mcp_discovery_bridge.mjs', import.meta.url), 'utf8'));
  assert.match(source, /import \{ discoverExternalMcp \} from '\.\/external_mcp_discovery_transport\.mjs'/);
  assert.match(source, /executeBridgeEnvelope\(input\)/);
});
