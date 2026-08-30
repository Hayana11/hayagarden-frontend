import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BRIDGE_VERSION,
  MAX_BRIDGE_INPUT_BYTES,
  executeBridgeEnvelope,
} from '../tools/external_mcp_call_bridge.mjs';

const success = {
  status: 'SUCCESS',
  result: { content: [{ type: 'text', text: 'ok' }], isError: false },
  error: null,
  diagnostics: { phase: 'CALL', call_started: true, call_tool_count: 1 },
};

function envelope(auth = null) {
  return {
    bridge_version: BRIDGE_VERSION,
    endpoint: 'https://calendar.example/mcp',
    transport: 'streamable_http',
    tool_name: 'echo',
    tool_input: { value: 'hello' },
    auth,
  };
}

test('call bridge enforces exact input and forwards null/bearer auth once', async () => {
  const received = [];
  const nullResult = await executeBridgeEnvelope(envelope(), { invoke: async (input) => { received.push(input); return success; } });
  assert.equal(nullResult.bridge_version, BRIDGE_VERSION);
  assert.deepEqual(received[0], {
    endpoint: 'https://calendar.example/mcp',
    transport: 'streamable_http',
    tool_name: 'echo',
    tool_input: { value: 'hello' },
    auth: null,
  });
  await executeBridgeEnvelope(envelope({ scheme: 'bearer', credential: 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13' }), {
    invoke: async (input) => { received.push(input); return success; },
  });
  assert.equal(received.length, 2);
  await assert.rejects(
    () => executeBridgeEnvelope({ ...envelope(), headers: {} }, { invoke: async () => success }),
    /unsupported fields/,
  );
  await assert.rejects(
    () => executeBridgeEnvelope({ ...envelope(), auth: { scheme: 'basic', credential: 'x' } }, { invoke: async () => success }),
    /unsupported/,
  );
});

test('call bridge suppresses reflected credential from result and thrown error', async () => {
  const credential = 'M5_B1_CANARY_SECRET_DO_NOT_LEAK_7f13';
  const reflected = await executeBridgeEnvelope(
    envelope({ scheme: 'bearer', credential }),
    { invoke: async () => ({ ...success, result: { content: [{ type: 'text', text: credential }] } }) },
  );
  assert.equal(reflected.status, 'OUTCOME_UNKNOWN');
  assert.equal(reflected.error.code, 'SECRET_REFLECTION_BLOCKED');
  assert.equal(reflected.result, null);
  assert.equal(JSON.stringify(reflected).includes(credential), false);
  const thrown = await executeBridgeEnvelope(
    envelope({ scheme: 'bearer', credential }),
    { invoke: async () => { throw new Error(`provider ${credential}`); } },
  );
  assert.equal(thrown.error.code, 'SECRET_REFLECTION_BLOCKED');
  assert.equal(JSON.stringify(thrown).includes(credential), false);
});

test('bridge rejects malformed transport result', async () => {
  await assert.rejects(
    () => executeBridgeEnvelope(envelope(), { invoke: async () => ({ status: 'SUCCESS' }) }),
    /schema is incomplete/,
  );
});

test('finite call stdin bound covers canonical 256 KiB input plus bearer overhead', () => {
  assert.ok(MAX_BRIDGE_INPUT_BYTES > 256 * 1024 + 16 * 1024);
  assert.ok(MAX_BRIDGE_INPUT_BYTES < 1024 * 1024);
});
