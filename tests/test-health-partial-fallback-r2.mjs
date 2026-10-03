import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { createHealthCapabilities } = require('../tools/xiaomi_health/capability.js');

function leakedAdapterPayload() {
  return {
    status: 'PASS',
    partial: true,
    error_code: 'auth_expired',
    provider: 'mixed',
    source: 'mixed',
    steps: {
      sampledAt: '2026-10-03T01:00:00Z',
      dataDate: '2026-10-03',
      value: 779,
      unit: 'steps',
      source: 'health_connect',
      provider: 'health_connect',
    },
    sleep: {
      sampledAt: '2026-10-03T01:00:00Z',
      dataDate: '2026-10-03',
      value: 375,
      unit: 'minutes',
      source: 'health_connect',
      provider: 'health_connect',
    },
    heart_rate: null,
    cycle: {
      status: 'FAIL',
      provider: 'xiaomi_fitness_cloud',
      source: 'xiaomi_fitness_cloud',
      metric: 'cycle',
      days: 180,
      events: [],
      periods: [],
      symptoms: [],
      error_code: 'auth_expired',
    },
    metric_status: {
      steps: { status: 'PASS', source: 'health_connect', stale: false },
      sleep: { status: 'PASS', source: 'health_connect', stale: false },
      heart_rate: { status: 'FAIL', source: 'xiaomi_fitness_cloud', stale: false, error_code: 'auth_expired' },
      cycle: { status: 'FAIL', source: 'xiaomi_fitness_cloud', stale: false, error_code: 'auth_expired' },
    },
  };
}

function assertPreservedLocalHealth(result) {
  assert.equal(result.status, 'PASS');
  assert.equal(result.partial, true);
  assert.equal(result.error_code, undefined);
  assert.equal(result.provider, 'health_connect');
  assert.equal(result.source, 'health_connect');
  assert.equal(result.steps.value, 779);
  assert.equal(result.steps.source, 'health_connect');
  assert.equal(result.sleep.value, 375);
  assert.equal(result.sleep.source, 'health_connect');
  assert.equal(result.heart_rate, null);
  assert.equal(result.metric_status.steps.status, 'PASS');
  assert.equal(result.metric_status.steps.source, 'health_connect');
  assert.equal(result.metric_status.sleep.status, 'PASS');
  assert.equal(result.metric_status.sleep.source, 'health_connect');
  assert.equal(['FAIL', 'EMPTY'].includes(result.metric_status.heart_rate.status), true);
  assert.equal(result.cycle.status, 'FAIL');
  assert.equal(result.cycle.source, 'xiaomi_fitness_cloud');
  assert.equal(result.cycle.error_code, 'auth_expired');
  assert.equal(result.metric_status.cycle.status, 'FAIL');
  assert.equal(result.metric_status.cycle.error_code, 'auth_expired');
}

const leakedPartial = await createHealthCapabilities({
  now: () => 12_000,
  runAdapter: async (_operation, input) => {
    assert.equal(input.metric, 'all');
    assert.equal(input.days, 2);
    return leakedAdapterPayload();
  },
}).get({ metric: 'all', days: 2 });
assertPreservedLocalHealth(leakedPartial);

const totalCloudFail = await createHealthCapabilities({
  now: () => 13_000,
  runAdapter: async () => ({
    status: 'FAIL',
    error_code: 'auth_expired',
    provider: 'xiaomi_fitness_cloud',
    source: 'xiaomi_fitness_cloud',
  }),
}).get({ metric: 'all', days: 2 });
assert.equal(totalCloudFail.status, 'FAIL');
assert.equal(totalCloudFail.error_code, 'auth_expired');
assert.equal(totalCloudFail.provider, 'xiaomi_fitness_cloud');
assert.equal(totalCloudFail.steps, null);
assert.equal(totalCloudFail.sleep, null);

console.log('test-health-partial-fallback-r2: ok');
console.log(JSON.stringify({
  leakedPartial: {
    status: leakedPartial.status,
    partial: leakedPartial.partial,
    error_code: leakedPartial.error_code ?? null,
    provider: leakedPartial.provider,
    source: leakedPartial.source,
    steps: { value: leakedPartial.steps.value, source: leakedPartial.steps.source, status: leakedPartial.metric_status.steps.status },
    sleep: { value: leakedPartial.sleep.value, source: leakedPartial.sleep.source, status: leakedPartial.metric_status.sleep.status },
    heart_rate: leakedPartial.metric_status.heart_rate,
    cycle: { status: leakedPartial.cycle.status, source: leakedPartial.cycle.source, error_code: leakedPartial.cycle.error_code },
  },
  totalCloudFail: {
    status: totalCloudFail.status,
    error_code: totalCloudFail.error_code,
    provider: totalCloudFail.provider,
  },
}, null, 2));
