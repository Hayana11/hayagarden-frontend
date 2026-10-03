import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const {
  createHealthCapabilities,
  safeHeartRateDaily,
  safeHeartRateSnapshot,
  safeLatest,
  validSample,
} = require('../tools/xiaomi_health/capability.js');

assert.equal(validSample('2026-10-03T11:20:00Z'), '2026-10-03T11:20:00Z');
assert.equal(validSample('2026-10-03T11:20:00.458Z'), '2026-10-03T11:20:00.458Z');
assert.equal(validSample('2026-10-03T11:20:00.458000Z'), '2026-10-03T11:20:00.458000Z');
assert.equal(validSample('2026-10-03T11:20:00+00:00'), null);
assert.equal(validSample('2026-10-03T11:20:00.458Z SECRET'), null);
assert.equal(validSample('2026-02-30T11:20:00Z'), null);
assert.equal(validSample('2026-10-03T11:20:00.458123Z'), null);
assert.equal(validSample('not-a-timestamp'), null);

const snapshot = safeHeartRateSnapshot({
  status: 'PASS',
  view: 'snapshot',
  value: 74,
  sampledAt: '2026-10-03T11:20:00.458Z',
  dataDate: '2026-10-03',
  ageSeconds: 90,
  stale: false,
  lastHour: { min: 66, max: 80, avg: 72.1, samples: 12, secret: 'remove-me' },
  records: [{ sampledAt: '2026-10-03T11:20:00.458Z', value: 74, token: 'raw' }],
  token: 'private',
});
assert.equal(snapshot.status, 'PASS');
assert.equal(snapshot.view, 'snapshot');
assert.equal(snapshot.value, 74);
assert.equal(snapshot.sampledAt, '2026-10-03T11:20:00.458Z');
assert.equal(snapshot.ageSeconds, 90);
assert.deepEqual(snapshot.lastHour, { min: 66, max: 80, avg: 72.1, samples: 12 });
assert.equal(snapshot.records, undefined);
assert.equal(snapshot.token, undefined);

const daily = safeHeartRateDaily(7, {
  status: 'PASS',
  view: 'daily',
  records: [
    { dataDate: '2026-10-03', restingHeartRate: 60, min: 55, max: 120, sampleCount: 40, value: 74, sampledAt: 'nope' },
    { dataDate: '2026-10-02', min: 50, max: 90, sampleCount: 20 },
    { dataDate: 'bad', min: 1 },
  ],
});
assert.equal(daily.status, 'PASS');
assert.equal(daily.view, 'daily');
assert.equal(daily.records.length, 2);
assert.deepEqual(daily.records[0], {
  dataDate: '2026-10-03',
  restingHeartRate: 60,
  min: 55,
  max: 120,
  sampleCount: 40,
});
assert.equal(daily.records[0].value, undefined);
assert.equal(daily.records[0].sampledAt, undefined);

const latest = safeLatest({
  status: 'PASS',
  sleep: {
    sampledAt: '2026-10-03T08:00:00.458Z',
    dataDate: '2026-10-03',
    value: 375,
    unit: 'minutes',
    source: 'health_connect',
    details: {
      startAt: '2026-10-03T01:00:00.458Z',
      endAt: '2026-10-03T08:00:00.458Z',
      stages: [{ stage: 1, startAt: '2026-10-03T01:00:00.458Z', endAt: '2026-10-03T08:00:00.458Z' }],
      secret: 'nope',
    },
    heartRate: { avg: 55.2, min: 48, samples: 9, raw: [48, 60] },
  },
  heart_rate: {
    view: 'snapshot',
    value: 74,
    sampledAt: '2026-10-03T11:20:00.458Z',
    ageSeconds: 12,
    lastHour: { min: 66, max: 80, avg: 72, samples: 4 },
    source: 'health_connect',
  },
}, {}, 7);
assert.equal(latest.sleep.value, 375);
assert.equal(latest.sleep.details.stages.length, 1);
assert.equal(latest.sleep.details.secret, undefined);
assert.deepEqual(latest.sleep.heartRate, { avg: 55.2, min: 48, samples: 9 });
assert.equal(latest.sleep.heartRate.raw, undefined);
assert.equal(latest.heart_rate.value, 74);
assert.equal(latest.heart_rate.ageSeconds, 12);
assert.deepEqual(latest.heart_rate.lastHour, { min: 66, max: 80, avg: 72, samples: 4 });

const missingSleepHr = safeLatest({
  status: 'PASS',
  sleep: {
    sampledAt: '2026-10-03T08:00:00Z',
    dataDate: '2026-10-03',
    value: 375,
    source: 'health_connect',
    details: {
      startAt: '2026-10-03T01:00:00Z',
      endAt: '2026-10-03T08:00:00Z',
      stages: [{ stage: 5, startAt: '2026-10-03T01:00:00Z', endAt: '2026-10-03T08:00:00Z' }],
    },
  },
}, {}, 7);
assert.equal(missingSleepHr.sleep.value, 375);
assert.equal(missingSleepHr.sleep.details.stages[0].stage, 5);
assert.equal(missingSleepHr.sleep.heartRate, undefined);

const calls = [];
const health = createHealthCapabilities({
  runAdapter: async (operation, input) => {
    calls.push({ operation, input });
    if (input.metric === 'heart_rate' && input.days === undefined) {
      return {
        status: 'PASS',
        view: 'snapshot',
        value: 74,
        sampledAt: '2026-10-03T11:20:00.458Z',
        ageSeconds: 30,
        lastHour: { min: 66, max: 80, avg: 72, samples: 3 },
        records: [{ value: 74, sampledAt: '2026-10-03T11:20:00.458Z' }],
      };
    }
    if (input.metric === 'heart_rate') {
      return {
        status: 'PASS',
        view: 'daily',
        days: input.days,
        records: [
          { dataDate: '2026-10-03', restingHeartRate: 60, min: 55, max: 110, sampleCount: 8, value: 74 },
        ],
      };
    }
    return {
      status: 'PASS',
      heart_rate: {
        view: 'snapshot',
        value: 74,
        sampledAt: '2026-10-03T11:20:00.458Z',
        ageSeconds: 30,
        lastHour: { min: 66, max: 80, avg: 72, samples: 3 },
      },
      sleep: {
        sampledAt: '2026-10-03T08:00:00.458Z',
        dataDate: '2026-10-03',
        value: 375,
        details: { startAt: '2026-10-03T01:00:00Z', endAt: '2026-10-03T08:00:00Z', stages: [] },
        heartRate: { avg: 52, min: 48, samples: 2 },
      },
      steps: { sampledAt: '2026-10-03T08:00:00Z', dataDate: '2026-10-03', value: 1000 },
      cycle: { status: 'EMPTY', days: 180, events: [], periods: [], symptoms: [] },
    };
  },
});

const omittedAll = await health.get({ metric: 'all' });
assert.deepEqual(calls[0], { operation: 'get_health', input: { metric: 'all' } });
assert.equal(omittedAll.heart_rate.value, 74);
assert.equal(omittedAll.heart_rate.ageSeconds, 30);
assert.equal(omittedAll.heart_rate.lastHour.samples, 3);
assert.equal(omittedAll.heart_rate.records, undefined);
assert.deepEqual(omittedAll.sleep.heartRate, { avg: 52, min: 48, samples: 2 });

const omittedHr = await health.get({ metric: 'heart_rate' });
assert.deepEqual(calls[1], { operation: 'get_health', input: { metric: 'heart_rate' } });
assert.equal(omittedHr.view, 'snapshot');
assert.equal(omittedHr.value, 74);
assert.equal(omittedHr.records, undefined);

const explicitHr = await health.get({ metric: 'heart_rate', days: 7 });
assert.deepEqual(calls[2], { operation: 'get_health', input: { metric: 'heart_rate', days: 7 } });
assert.equal(explicitHr.view, 'daily');
assert.equal(explicitHr.records.length, 1);
assert.equal(explicitHr.records[0].restingHeartRate, 60);
assert.equal(explicitHr.records[0].value, undefined);

console.log('test-health-heart-rate-model-view-r4: ok');
