import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const {
  LATEST_TTL_MS,
  SERIES_TTL_MS,
  createHealthCapabilities,
} = require('../tools/xiaomi_health/capability.js');

const secrets = [
  '7400000012345678',
  'service-token-private',
  'ssecurity-private',
  'pass-token-private',
  'cookie-private',
  'device-id-private',
];
const secretFields = {
  user_id: secrets[0],
  service_token: secrets[1],
  ssecurity: secrets[2],
  pass_token: secrets[3],
  cookie: secrets[4],
  device_id: secrets[5],
};
let clock = 1_000;
let calls = [];
let fail = false;
const now = () => clock;
const runAdapter = async (operation, args) => {
  calls.push({ operation, args });
  if (fail) throw new Error(secrets.join('|'));
  if (operation === 'health_status') return { connected: true, provider: 'bad-source', auth_state: 'valid', last_success_at: '2026-09-24T01:00:00Z', last_error: null, ...secretFields };
  if (operation === 'health_latest') return { provider: 'bad-source', sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000, details: secretFields }, ...secretFields };
  return { status: 'PASS', provider: 'bad-source', metric: operation.slice(7), records: [{ sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000, details: secretFields, ...secretFields }] };
};
const health = createHealthCapabilities({ runAdapter, now });

const status = await health.status();
assert.equal(status.provider, 'xiaomi_fitness_cloud');
assert.equal(status.connected, true);
assert.equal(status.auth_state, 'valid');
assert.equal(JSON.stringify(status).includes(secrets[1]), false);
for (const secret of secrets) assert.equal(JSON.stringify(status).includes(secret), false);

const latest = await health.latest();
assert.equal(latest.provider, 'xiaomi_fitness_cloud');
assert.equal(latest.steps.value, 1000);
assert.equal(latest.steps.details, undefined);
for (const secret of secrets) assert.equal(JSON.stringify(latest).includes(secret), false);
clock += LATEST_TTL_MS - 1;
assert.equal((await health.latest()).cached, true);
clock += 2;
fail = true;
const staleLatest = await health.latest();
assert.equal(staleLatest.cached, true);
assert.equal(staleLatest.stale, true);
assert.equal(staleLatest.steps.value, 1000);
for (const secret of secrets) assert.equal(JSON.stringify(staleLatest).includes(secret), false);

fail = false;
calls = [];
const dayOne = await health.series('steps', 1);
assert.equal(dayOne.status, 'PASS');
assert.deepEqual(calls[0], { operation: 'health_steps', args: { days: 1 } });
const dayThirty = await health.series('sleep', 30);
assert.equal(dayThirty.status, 'PASS');
assert.equal(calls[1].args.days, 30);
assert.equal((await health.series('steps', 1)).cached, true);
clock += SERIES_TTL_MS + 1;
fail = true;
const staleSeries = await health.series('steps', 1);
assert.equal(staleSeries.stale, true);
assert.equal(staleSeries.records.length, 1);
for (const secret of secrets) assert.equal(JSON.stringify(staleSeries).includes(secret), false);

for (const invalid of [0, 31, -1, 1.5, '2', true]) {
  await assert.rejects(() => health.series('steps', invalid), RangeError);
}
assert.equal(LATEST_TTL_MS, 60_000);
assert.equal(SERIES_TTL_MS, 15 * 60_000);
console.log('test-xiaomi-health-capability: ok');

