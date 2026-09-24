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
const runAdapter = async (operation, input) => {
  calls.push({ operation, input });
  if (fail) throw new Error(secrets.join('|'));
  if (input.metric === 'status') return {
    connected: true, provider: 'bad-source', auth_state: 'valid',
    last_success_at: '2026-09-24T01:00:00Z', last_error: null, ...secretFields,
  };
  if (input.metric === 'all') return {
    provider: 'bad-source',
    sampledAt: '2026-09-24T01:00:00Z',
    dataDate: '2026-09-24',
    steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000, details: secretFields },
    sleep: null,
    heart_rate: null,
    ...secretFields,
  };
  return {
    status: 'PASS',
    provider: 'bad-source',
    metric: input.metric,
    records: [{
      sampledAt: '2026-09-24T01:00:00Z',
      dataDate: '2026-09-24',
      value: 1000,
      details: secretFields,
      ...secretFields,
    }],
  };
};
const health = createHealthCapabilities({ runAdapter, now });

const status = await health.get({ metric: 'status' });
assert.equal(status.provider, 'xiaomi_fitness_cloud');
assert.equal(status.source, 'xiaomi_fitness_cloud');
assert.equal(status.connected, true);
assert.equal(status.auth_state, 'valid');
for (const secret of secrets) assert.equal(JSON.stringify(status).includes(secret), false);

const all = await health.get();
assert.equal(all.provider, 'xiaomi_fitness_cloud');
assert.equal(all.source, 'xiaomi_fitness_cloud');
assert.equal(all.days, 7);
assert.equal(all.steps.value, 1000);
assert.equal(all.steps.details, undefined);
for (const secret of secrets) assert.equal(JSON.stringify(all).includes(secret), false);
clock += LATEST_TTL_MS - 1;
assert.equal((await health.get({ metric: 'all' })).cached, true);
clock += 2;
fail = true;
const staleAll = await health.get({ metric: 'all' });
assert.equal(staleAll.cached, true);
assert.equal(staleAll.stale, true);
assert.equal(staleAll.steps.value, 1000);
for (const secret of secrets) assert.equal(JSON.stringify(staleAll).includes(secret), false);

fail = false;
calls = [];
const dayOne = await health.get({ metric: 'steps', days: 1 });
assert.equal(dayOne.status, 'PASS');
assert.deepEqual(calls[0], { operation: 'get_health', input: { metric: 'steps', days: 1 } });
const dayThirty = await health.get({ metric: 'sleep', days: 30 });
assert.equal(dayThirty.status, 'PASS');
assert.equal(calls[1].input.days, 30);
assert.equal((await health.get({ metric: 'steps', days: 1 })).cached, true);
clock += SERIES_TTL_MS + 1;
fail = true;
const staleSeries = await health.get({ metric: 'steps', days: 1 });
assert.equal(staleSeries.stale, true);
assert.equal(staleSeries.records.length, 1);
for (const secret of secrets) assert.equal(JSON.stringify(staleSeries).includes(secret), false);

for (const invalid of [0, 31, -1, 1.5, '2', true]) {
  await assert.rejects(() => health.get({ metric: 'steps', days: invalid }), RangeError);
}
await assert.rejects(() => health.get({ metric: 'unknown', days: 2 }), TypeError);
assert.equal(LATEST_TTL_MS, 60_000);
assert.equal(SERIES_TTL_MS, 15 * 60_000);
console.log('test-xiaomi-health-capability: ok');