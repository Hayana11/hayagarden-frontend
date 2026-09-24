import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import internalServer from '../internal-mcp-server.js';

const secrets = ['7400000012345678', 'service-token-private', 'ssecurity-private', 'pass-token-private', 'cookie-private', 'device-id-private'];
const calls = [];
const healthAdapter = async (operation, input) => {
  calls.push({ operation, input });
  if (input.metric === 'status') return {
    connected: true, provider: 'untrusted', auth_state: 'valid',
    last_success_at: '2026-09-24T01:00:00Z', last_error: null,
    user_id: secrets[0], service_token: secrets[1],
  };
  if (input.metric === 'all') return {
    sampledAt: '2026-09-24T01:00:00Z',
    dataDate: '2026-09-24',
    steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 8432, details: { service_token: secrets[1] }, ssecurity: secrets[2] },
    sleep: null,
    heart_rate: null,
    pass_token: secrets[3],
  };
  if (input.metric === 'cycle') return {
    status: 'PASS',
    events: [{ type: 'period_start', timestamp: '2000-01-01T00:00:00Z', updated_at: '2000-01-02T00:00:00Z' }],
    periods: [{ start: '2000-01-01T00:00:00Z', end: null, open: true, source: 'recorded' }],
    symptoms: [],
    predictions: { secret: secrets[1] },
    service_token: secrets[1],
  };
  return {
    status: 'PASS',
    metric: input.metric,
    records: [{ sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 8432, unit: 'invalid', service_token: secrets[1], details: { cookie: secrets[4], device_id: secrets[5] } }],
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

const legacyNames = [
  'health.status', 'health.latest', 'health.steps', 'health.sleep', 'health.heart_rate',
  'health_status', 'health_latest', 'health_steps', 'health_sleep', 'health_heart_rate',
];
try {
  await client.connect(transport);
  const listed = await client.listTools();
  const publicHealthTools = listed.tools.filter((tool) => (tool.name.startsWith('health_') || tool.name.startsWith('health.')) || tool.name === 'get.health');
  assert.deepEqual(publicHealthTools.map((tool) => tool.name), ['get.health']);
  for (const legacy of legacyNames) assert.equal(listed.tools.some((tool) => tool.name === legacy), false);

  const schema = publicHealthTools[0].inputSchema;
  assert.deepEqual(schema.properties.metric.enum, ['all', 'status', 'steps', 'sleep', 'heart_rate', 'cycle']);
  assert.equal(schema.properties.metric.default, 'all');
  assert.equal(schema.properties.days.minimum, 1);
  assert.equal(schema.properties.days.maximum, 365);
  assert.equal(schema.properties.days.default, undefined);
  assert.match(schema.properties.days.description, /1 到 30/);
  assert.match(schema.properties.days.description, /1 到 365/);
  assert.match(schema.properties.days.description, /180/);

  const status = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'status' } })));
  const all = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'all', days: 1 } })));
  const steps = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'steps', days: 2 } })));
  const sleep = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'sleep', days: 2 } })));
  const heartRate = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'heart_rate', days: 2 } })));
  const cycle = JSON.parse(textOf(await client.callTool({ name: 'get.health', arguments: { metric: 'cycle' } })));

  assert.equal(status.source, 'xiaomi_fitness_cloud');
  assert.equal(status.connected, true);
  assert.equal(all.source, 'xiaomi_fitness_cloud');
  assert.equal(all.steps.value, 8432);
  assert.equal(steps.status, 'PASS');
  assert.equal(steps.source, 'xiaomi_fitness_cloud');
  assert.equal(steps.records.length, 1);
  assert.equal(sleep.source, 'xiaomi_fitness_cloud');
  assert.equal(heartRate.source, 'xiaomi_fitness_cloud');
  assert.equal(cycle.source, 'xiaomi_fitness_cloud');
  assert.equal(cycle.days, 180);
  assert.equal(cycle.events.length, 1);
  assert.equal(cycle.periods[0].open, true);
  assert.equal(cycle.predictions, null);
  assert.deepEqual(calls.map((item) => item.operation), Array(6).fill('get_health'));
  assert.deepEqual(calls.map((item) => item.input.metric), ['status', 'all', 'steps', 'sleep', 'heart_rate', 'cycle']);
  assert.deepEqual(calls.map((item) => item.input.days), [7, 1, 2, 2, 2, 180]);

  const output = JSON.stringify({ status, all, steps, sleep, heartRate, cycle });
  for (const secret of secrets) assert.equal(output.includes(secret), false);
} finally {
  await client.close().catch(() => {});
  await new Promise((resolve) => listener.close(resolve));
}

console.log('test-xiaomi-health-internal-mcp: ok');

