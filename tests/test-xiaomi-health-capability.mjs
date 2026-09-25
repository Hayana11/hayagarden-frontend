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
    sleep: {
      sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 420, unit: 'minutes',
      sleepWindow: {
        bedtime: '2026-09-24T15:10:00Z',
        wakeUpTime: '2026-09-24T22:45:00Z',
        token: secrets[1],
        timezone: 'private-timezone',
        note: 'private-note',
        raw: { cookie: secrets[4] },
      },
    },
    heart_rate: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 72, unit: 'bpm' },
    cycle: {
      status: 'PASS',
      days: 180,
      events: [{ type: 'period_start', timestamp: '2000-01-01T00:00:00Z', updated_at: '2000-01-02T00:00:00Z' }],
      periods: [{ start: '2000-01-01T00:00:00Z', end: null, open: true, source: 'untrusted' }],
      symptoms: [{ timestamp: '2000-01-01T00:00:00Z', hp: 'much', mood: 'happy', pain: 'heavy', note: secrets[1] }],
      predictions: { secret: secrets[2], next: '2026-10-01' },
      token: secrets[1],
      cookie: secrets[4],
      note: 'cycle-note-private',
      user_id: secrets[0],
    },
    ...secretFields,
  };
  if (input.metric === 'cycle') return {
    status: 'PASS',
    events: [{ type: 'period_start', timestamp: '2000-01-01T00:00:00Z', updated_at: '2000-01-02T00:00:00Z' }],
    periods: [{ start: '2000-01-01T00:00:00Z', end: null, open: true, source: 'untrusted' }],
    symptoms: [{ timestamp: '2000-01-01T00:00:00Z', hp: 'much', mood: 'happy', pain: 'heavy', note: secrets[1] }],
    predictions: { secret: secrets[2] },
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
      ...(input.metric === 'sleep' ? {
        sleepWindow: {
          bedtime: '2026-09-24T15:10:00Z',
          wakeUpTime: '2026-09-24T22:45:00Z',
          token: secrets[1],
          timezone: 'private-timezone',
          note: 'private-note',
          raw: { cookie: secrets[4] },
        },
      } : {}),
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
assert.equal(all.sleep.value, 420);
assert.deepEqual(all.sleep.sleepWindow, {
  bedtime: '2026-09-24T15:10:00Z',
  wakeUpTime: '2026-09-24T22:45:00Z',
});
assert.equal(all.heart_rate.value, 72);
assert.equal(all.cycle.status, 'PASS');
assert.equal(all.cycle.days, 180);
assert.equal(all.cycle.events[0].type, 'period_start');
assert.equal(all.cycle.periods[0].source, 'recorded');
assert.equal(all.cycle.symptoms[0].hp, 'much');
assert.equal(all.cycle.predictions, null);
assert.equal(all.cycle.note, undefined);
assert.equal(all.cycle.token, undefined);
assert.equal(all.partial, false);
assert.equal(all.metric_status.steps.status, 'PASS');
assert.equal(all.metric_status.sleep.status, 'PASS');
assert.equal(all.metric_status.heart_rate.status, 'PASS');
assert.equal(all.metric_status.cycle.status, 'PASS');
for (const secret of secrets) assert.equal(JSON.stringify(all).includes(secret), false);
assert.equal(JSON.stringify(all).includes('cycle-note-private'), false);
assert.equal(JSON.stringify(all).includes('2026-10-01'), false);
clock += LATEST_TTL_MS - 1;
assert.equal((await health.get({ metric: 'all' })).cached, true);
clock += 2;
fail = true;
const staleAll = await health.get({ metric: 'all' });
assert.equal(staleAll.cached, true);
assert.equal(staleAll.stale, true);
assert.equal(staleAll.steps.value, 1000);
assert.equal(staleAll.metric_status.steps.status, 'PASS');
assert.equal(staleAll.metric_status.cycle.status, 'PASS');
assert.equal(staleAll.partial, false);
assert.deepEqual(staleAll.sleep.sleepWindow, {
  bedtime: '2026-09-24T15:10:00Z',
  wakeUpTime: '2026-09-24T22:45:00Z',
});
for (const secret of secrets) assert.equal(JSON.stringify(staleAll).includes(secret), false);

fail = false;
calls = [];
const dayOne = await health.get({ metric: 'steps', days: 1 });
assert.equal(dayOne.status, 'PASS');
assert.deepEqual(calls[0], { operation: 'get_health', input: { metric: 'steps', days: 1 } });
const dayThirty = await health.get({ metric: 'sleep', days: 30 });
assert.equal(dayThirty.status, 'PASS');
assert.equal(calls[1].input.days, 30);
assert.deepEqual(dayThirty.records[0].sleepWindow, {
  bedtime: '2026-09-24T15:10:00Z',
  wakeUpTime: '2026-09-24T22:45:00Z',
});
assert.equal((await health.get({ metric: 'steps', days: 1 })).cached, true);
clock += SERIES_TTL_MS + 1;
fail = true;
const staleSeries = await health.get({ metric: 'steps', days: 1 });
assert.equal(staleSeries.stale, true);
assert.equal(staleSeries.records.length, 1);
for (const secret of secrets) assert.equal(JSON.stringify(staleSeries).includes(secret), false);

fail = false;
calls = [];
const cycle = await health.get({ metric: 'cycle' });
assert.equal(cycle.status, 'PASS');
assert.equal(cycle.days, 180);
assert.equal(cycle.events[0].type, 'period_start');
assert.equal(cycle.periods[0].open, true);
assert.equal(cycle.periods[0].source, 'recorded');
assert.equal(cycle.symptoms[0].hp, 'much');
assert.equal(cycle.predictions, null);
assert.equal(calls[0].input.days, 180);
assert.equal((await health.get({ metric: 'cycle', days: 365 })).days, 365);
for (const secret of secrets) assert.equal(JSON.stringify(cycle).includes(secret), false);

for (const invalid of [0, 31, -1, 1.5, '2', true]) {
  await assert.rejects(() => health.get({ metric: 'steps', days: invalid }), RangeError);
}
for (const invalid of [0, 366, -1, 1.5, '2', true]) {
  await assert.rejects(() => health.get({ metric: 'cycle', days: invalid }), RangeError);
}
await assert.rejects(() => health.get({ metric: 'unknown', days: 2 }), TypeError);
assert.equal(LATEST_TTL_MS, 60_000);
assert.equal(SERIES_TTL_MS, 15 * 60_000);

const emptyCycleHealth = createHealthCapabilities({
  now,
  runAdapter: async (_operation, input) => {
    if (input.metric !== 'all') throw new Error('unexpected');
    return {
      sampledAt: '2026-09-24T01:00:00Z',
      dataDate: '2026-09-24',
      steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000 },
      sleep: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 420 },
      heart_rate: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 72 },
      cycle: { status: 'EMPTY', days: 180, events: [], periods: [], symptoms: [], predictions: { secret: secrets[2] } },
    };
  },
});
const emptyAll = await emptyCycleHealth.get({ metric: 'all', days: 30 });
assert.equal(emptyAll.status, 'PASS');
assert.equal(emptyAll.days, 30);
assert.equal(emptyAll.steps.value, 1000);
assert.equal(emptyAll.cycle.status, 'EMPTY');
assert.equal(emptyAll.cycle.days, 180);
assert.equal(emptyAll.cycle.predictions, null);
assert.equal(JSON.stringify(emptyAll).includes(secrets[2]), false);

const failCycleHealth = createHealthCapabilities({
  now,
  runAdapter: async () => ({
    sampledAt: '2026-09-24T01:00:00Z',
    dataDate: '2026-09-24',
    steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000 },
    sleep: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 420 },
    heart_rate: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 72 },
    cycle: {
      status: 'FAIL',
      error_code: 'timeout',
      days: 180,
      token: secrets[1],
      cookie: secrets[4],
      note: 'cycle-note-private',
      user_id: secrets[0],
      predictions: { secret: secrets[2] },
    },
  }),
});
const failAll = await failCycleHealth.get({ metric: 'all', days: 7 });
assert.equal(failAll.status, 'PASS');
assert.equal(failAll.partial, true);
assert.equal(failAll.steps.value, 1000);
assert.equal(failAll.sleep.value, 420);
assert.equal(failAll.heart_rate.value, 72);
assert.equal(failAll.cycle.status, 'FAIL');
assert.equal(failAll.cycle.error_code, 'timeout');
assert.equal(failAll.cycle.days, 180);
assert.equal(failAll.cycle.predictions, null);
assert.equal(failAll.cycle.token, undefined);
assert.equal(failAll.metric_status.cycle.status, 'FAIL');
assert.equal(failAll.metric_status.cycle.error_code, 'timeout');
for (const secret of secrets) assert.equal(JSON.stringify(failAll).includes(secret), false);

const unsafeCycleHealth = createHealthCapabilities({
  now,
  runAdapter: async () => ({
    steps: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 1000 },
    cycle: { status: 'FAIL', error_code: 'stack-trace-private', days: 180 },
  }),
});
const unsafeAll = await unsafeCycleHealth.get({ metric: 'all', days: 7 });
assert.equal(unsafeAll.cycle.status, 'FAIL');
assert.equal(unsafeAll.cycle.error_code, 'unavailable');
assert.equal(JSON.stringify(unsafeAll).includes('stack-trace-private'), false);

let partialClock = 5_000;
const partialNow = () => partialClock;
let partialFail = false;
const partialHealth = createHealthCapabilities({
  now: partialNow,
  runAdapter: async () => {
    if (partialFail) throw new Error(secrets.join('|'));
    return {
      status: 'PASS',
      sampledAt: '2026-09-24T01:00:00Z',
      dataDate: '2026-09-24',
      steps: null,
      sleep: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 420, unit: 'minutes' },
      heart_rate: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 72, unit: 'bpm' },
      cycle: {
        status: 'PASS',
        days: 180,
        events: [{ type: 'period_start', timestamp: '2000-01-01T00:00:00Z', updated_at: '2000-01-02T00:00:00Z' }],
        periods: [{ start: '2000-01-01T00:00:00Z', end: null, open: true, source: 'recorded' }],
        symptoms: [],
        predictions: null,
      },
      metric_status: {
        steps: { status: 'FAIL', error_code: 'timeout', token: secrets[1], cookie: secrets[4], user_id: secrets[0], note: 'private-note' },
        sleep: { status: 'PASS' },
        heart_rate: { status: 'PASS' },
        cycle: { status: 'PASS' },
      },
      partial: 'trusted-false',
    };
  },
});
const partialAll = await partialHealth.get({ metric: 'all', days: 7 });
assert.equal(partialAll.status, 'PASS');
assert.equal(partialAll.partial, true);
assert.equal(partialAll.steps, null);
assert.equal(partialAll.sleep.value, 420);
assert.equal(partialAll.heart_rate.value, 72);
assert.equal(partialAll.metric_status.steps.status, 'FAIL');
assert.equal(partialAll.metric_status.steps.error_code, 'timeout');
assert.equal(partialAll.metric_status.steps.token, undefined);
assert.equal(partialAll.metric_status.steps.cookie, undefined);
assert.equal(partialAll.metric_status.steps.user_id, undefined);
assert.equal(partialAll.metric_status.steps.note, undefined);
assert.equal(partialAll.cycle.predictions, null);
partialClock += LATEST_TTL_MS + 1;
partialFail = true;
const stalePartial = await partialHealth.get({ metric: 'all', days: 7 });
assert.equal(stalePartial.stale, true);
assert.equal(stalePartial.cached, true);
assert.equal(stalePartial.sleep.value, 420);
assert.equal(stalePartial.heart_rate.value, 72);
assert.equal(stalePartial.metric_status.steps.status, 'FAIL');
assert.equal(stalePartial.metric_status.steps.error_code, 'timeout');
assert.equal(stalePartial.partial, true);
for (const secret of secrets) assert.equal(JSON.stringify(stalePartial).includes(secret), false);

const maliciousHealth = createHealthCapabilities({
  now: () => 9_000,
  runAdapter: async () => ({
    sampledAt: '2026-09-24T01:00:00Z',
    dataDate: '2026-09-24',
    steps: null,
    sleep: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 420 },
    heart_rate: { sampledAt: '2026-09-24T01:00:00Z', dataDate: '2026-09-24', value: 72 },
    cycle: {
      status: 'PASS',
      days: 180,
      events: [{ type: 'period_start', timestamp: '2000-01-01T00:00:00Z', updated_at: '2000-01-02T00:00:00Z' }],
      periods: [{ start: '2000-01-01T00:00:00Z', end: null, open: true, source: 'recorded' }],
      symptoms: [],
      predictions: { secret: secrets[2] },
    },
    metric_status: {
      steps: { status: 'SECRET_STATUS', error_code: 'private-stack-trace', token: secrets[1], cookie: secrets[4], user_id: secrets[0], note: 'note-private' },
      sleep: { status: 'PASS', cookie: secrets[4] },
      heart_rate: { status: 'PASS' },
      cycle: { status: 'PASS', token: secrets[1] },
    },
    partial: true,
  }),
});
const malicious = await maliciousHealth.get({ metric: 'all', days: 7 });
assert.equal(malicious.metric_status.steps.status, 'FAIL');
assert.equal(malicious.metric_status.steps.error_code, 'unavailable');
assert.equal(JSON.stringify(malicious).includes('SECRET_STATUS'), false);
assert.equal(JSON.stringify(malicious).includes('private-stack-trace'), false);
assert.equal(malicious.metric_status.steps.token, undefined);
assert.equal(malicious.metric_status.steps.cookie, undefined);
assert.equal(malicious.metric_status.steps.user_id, undefined);
assert.equal(malicious.metric_status.steps.note, undefined);
assert.equal(malicious.cycle.predictions, null);
assert.equal(malicious.partial, true);
for (const secret of secrets) assert.equal(JSON.stringify(malicious).includes(secret), false);

async function readSleepWindow(metric, sleepWindow) {
  const capability = createHealthCapabilities({
    now,
    runAdapter: async () => {
      const record = {
        sampledAt: '2026-09-24T01:00:00Z',
        dataDate: '2026-09-24',
        value: 420,
        unit: 'minutes',
        sleepWindow,
      };
      if (metric === 'all') {
        return {
          status: 'PASS',
          sleep: record,
          cycle: { status: 'EMPTY', days: 180, events: [], periods: [], symptoms: [] },
        };
      }
      return { status: 'PASS', records: [record] };
    },
  });
  return capability.get({ metric, days: 7 });
}

const canonicalSleepWindow = {
  bedtime: '2026-09-24T15:10:00Z',
  wakeUpTime: '2026-09-24T22:45:00Z',
};
const standaloneWindow = await readSleepWindow('sleep', {
  ...canonicalSleepWindow,
  token: secrets[1],
  timezone: 'private-timezone',
  note: 'private-note',
  raw: { cookie: secrets[4] },
});
assert.deepEqual(standaloneWindow.records[0].sleepWindow, canonicalSleepWindow);
assert.deepEqual(Object.keys(standaloneWindow.records[0].sleepWindow).sort(), ['bedtime', 'wakeUpTime']);
const allWindow = await readSleepWindow('all', {
  ...canonicalSleepWindow,
  token: secrets[1],
  timezone: 'private-timezone',
  note: 'private-note',
  raw: { cookie: secrets[4] },
});
assert.deepEqual(allWindow.sleep.sleepWindow, canonicalSleepWindow);
assert.deepEqual(Object.keys(allWindow.sleep.sleepWindow).sort(), ['bedtime', 'wakeUpTime']);
for (const secret of secrets) {
  assert.equal(JSON.stringify(standaloneWindow).includes(secret), false);
  assert.equal(JSON.stringify(allWindow).includes(secret), false);
}

const invalidSleepWindows = [
  null,
  'not-an-object',
  [],
  { bedtime: 'invalid', wakeUpTime: canonicalSleepWindow.wakeUpTime },
  { bedtime: canonicalSleepWindow.bedtime, wakeUpTime: 'invalid' },
  { bedtime: canonicalSleepWindow.bedtime, wakeUpTime: '2026-09-24T15:10:00Z' },
  { bedtime: canonicalSleepWindow.bedtime, wakeUpTime: '2026-09-24T14:00:00Z' },
  { bedtime: '2026-02-30T15:10:00Z', wakeUpTime: canonicalSleepWindow.wakeUpTime },
  { bedtime: '2026-09-24T15:10:00Z SECRET', wakeUpTime: canonicalSleepWindow.wakeUpTime },
];
for (const invalidWindow of invalidSleepWindows) {
  const invalid = await readSleepWindow('sleep', invalidWindow);
  assert.equal(invalid.records[0].sleepWindow, undefined);
}

console.log('test-xiaomi-health-capability: ok');
