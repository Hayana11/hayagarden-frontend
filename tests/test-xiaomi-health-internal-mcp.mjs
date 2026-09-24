import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import internalServer from '../internal-mcp-server.js';

const secrets = ['7400000012345678', 'service-token-private', 'ssecurity-private', 'pass-token-private', 'cookie-private', 'device-id-private'];
const calls = [];
const healthAdapter = async (operation, input) => {
  calls.push({ operation, input });
  if (operation === 'health_status') return {
    connected: true, provider: 'untrusted', auth_state: 'valid',
    last_success_at: '2026-09-24T01:00:00Z', last_error: null,
    user_id: secrets[0], service_token: secrets[1],
  };
  if (operation === 'health_latest') return {
    sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24',
    steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 8432, details: { service_token: secrets[1] }, ssecurity: secrets[2] },
    sleep: null, heart_rate: null, pass_token: secrets[3],
  };
  return {
    status: 'PASS', records: [{ sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 8432, unit: 'invalid', service_token: secrets[1], details: { cookie: secrets[4], device_id: secrets[5] } }],
  };
};

const { listener, port } = await internalServer.startInternalMcpServer({
  host: '127.0.0.1', port: 0, healthAdapter,
});
const client = new Client({ name: 'xiaomi-health-internal-mcp-test', version: '1.0.0' });
const transport = new StreamableHTTPClientTransport(new URL(`http://127.0.0.1:${port}/mcp`));

function textOf(result) {
  return result?.content?.find((item) => item.type === 'text')?.text ?? '';
}

try {
  await client.connect(transport);
  const listed = await client.listTools();
  const healthTools = listed.tools.filter((tool) => tool.name.startsWith('health_'));
  assert.deepEqual(healthTools.map((tool) => tool.name), [
    'health_status', 'health_latest', 'health_steps', 'health_sleep', 'health_heart_rate',
  ]);
  const daysSchema = healthTools.find((tool) => tool.name === 'health_steps').inputSchema.properties.days;
  assert.equal(daysSchema.minimum, 1);
  assert.equal(daysSchema.maximum, 30);

  const status = JSON.parse(textOf(await client.callTool({ name: 'health_status', arguments: {} })));
  assert.equal(status.provider, 'xiaomi_fitness_cloud');
  assert.equal(status.connected, true);
  const latest = JSON.parse(textOf(await client.callTool({ name: 'health_latest', arguments: {} })));
  assert.equal(latest.steps.value, 8432);
  assert.equal(latest.steps.unit, 'steps');
  const steps = JSON.parse(textOf(await client.callTool({ name: 'health_steps', arguments: { days: 1 } })));
  assert.equal(steps.status, 'PASS');
  assert.equal(steps.records.length, 1);
  const sleep = JSON.parse(textOf(await client.callTool({ name: 'health_sleep', arguments: { days: 30 } })));
  const heartRate = JSON.parse(textOf(await client.callTool({ name: 'health_heart_rate', arguments: {} })));
  assert.deepEqual(calls.map((item) => item.operation), [
    'health_status', 'health_latest', 'health_steps', 'health_sleep', 'health_heart_rate',
  ]);
  assert.deepEqual(calls[2].input, { days: 1 });
  assert.deepEqual(calls[3].input, { days: 30 });
  assert.deepEqual(calls[4].input, { days: 7 });
  const output = JSON.stringify({ status, latest, steps, sleep, heartRate });
  for (const secret of secrets) assert.equal(output.includes(secret), false);
} finally {
  await client.close().catch(() => {});
  await new Promise((resolve) => listener.close(resolve));
}

console.log('test-xiaomi-health-internal-mcp: ok');
