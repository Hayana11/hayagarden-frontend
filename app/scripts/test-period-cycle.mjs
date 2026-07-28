/**
 * Period cycle algorithm + serial save-queue unit checks (no vitest).
 * Run: npm run test:period-cycle
 */
import assert from 'node:assert/strict';

const cycle = await import('../src/lib/cycle.ts');
const period = await import('../src/lib/period.ts');
const save = await import('../src/lib/periodSave.ts');

const {
  addDays,
  diffDays,
  groupFlowDates,
  deriveCycle,
  resolveInPeriod,
  collectPrePeriodTopStates,
  cyclePhaseLabel,
  shouldPredictPeriodDay,
  ymdToEpochDay,
} = cycle;

const { derivePeriod } = period;
const {
  bumpEditGeneration,
  schedulePeriodDaySave,
  shouldReloadAfterFailedSave,
  getActiveQueueCountForTests,
  resetPeriodSaveQueues,
} = save;

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

// ── date math (UTC epoch-day; DST-safe) ──
assert.equal(diffDays('2026-03-10', '2026-03-08'), 2);
assert.equal(addDays('2026-03-08', 2), '2026-03-10');
assert.equal(diffDays('2026-03-09', '2026-03-08'), 1);
assert.equal(addDays('2026-03-08', 1), '2026-03-09');
assert.equal(addDays('2026-01-31', 1), '2026-02-01');
assert.equal(addDays('2025-12-31', 1), '2026-01-01');
assert.equal(diffDays('2026-01-01', '2025-12-31'), 1);
assert.equal(ymdToEpochDay('2026-07-01') - ymdToEpochDay('2026-06-01'), 30);

const settings = { cycleLength: 28, periodLength: 5, lastStart: '2026-06-01' };
const emptySettings = { cycleLength: 28, periodLength: 5, lastStart: '' };

// ── empty anchor ──
{
  const c = deriveCycle({}, emptySettings, '2026-07-10');
  assert.equal(c.hasAnchor, false);
  assert.equal(cyclePhaseLabel(c), '暂无记录');
  assert.equal(c.predicted.size, 0);
}

// ── came:false not in predicted ──
{
  const days = { '2026-06-01': { came: true }, '2026-06-30': { came: false } };
  const c = deriveCycle(days, settings, '2026-06-10');
  assert.ok(!c.predicted.has('2026-06-30'));
  assert.equal(shouldPredictPeriodDay(days, '2026-06-30'), false);
}

// ── queue recovery after Promise rejection ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const g1 = bumpEditGeneration('2026-06-01');
  const p1 = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'A' }, g1, async (d, r) => {
    await delay(25);
    if (r.flow === 'A') throw new Error('network reject');
    server.set(d, structuredClone(r));
    return true;
  });
  await delay(5);
  const g2 = bumpEditGeneration('2026-06-01');
  const p2 = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'B' }, g2, async (d, r) => {
    server.set(d, structuredClone(r));
    return true;
  });
  const r1 = await p1;
  const r2 = await p2;
  assert.equal(r1.ok, false);
  assert.equal(r2.ok, true);
  assert.deepEqual(server.get('2026-06-01'), { came: true, flow: 'B' });
}

// ── failure must not reload over newer local edit ──
{
  resetPeriodSaveQueues();
  const server = { '2026-06-01': { came: true, flow: 'STALE' } };
  let local = { '2026-06-01': { came: true, flow: 'A' } };
  const gA = bumpEditGeneration('2026-06-01');
  const pA = schedulePeriodDaySave('2026-06-01', local['2026-06-01'], gA, async () => {
    await delay(30);
    throw new Error('reject A');
  });
  await delay(5);
  const gB = bumpEditGeneration('2026-06-01');
  local['2026-06-01'] = { came: true, flow: 'B' };
  const pB = schedulePeriodDaySave('2026-06-01', local['2026-06-01'], gB, async (d, r) => {
    server[d] = structuredClone(r);
    return true;
  });
  const rA = await pA;
  if (shouldReloadAfterFailedSave('2026-06-01', gA)) {
    local = { ...server };
  }
  assert.equal(rA.ok, false);
  assert.equal(shouldReloadAfterFailedSave('2026-06-01', gA), false);
  assert.equal(local['2026-06-01'].flow, 'B');
  const rB = await pB;
  assert.equal(rB.ok, true);
  assert.equal(server['2026-06-01'].flow, 'B');
}

// ── two rejects then third success ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const date = '2026-06-02';
  const g1 = bumpEditGeneration(date);
  const p1 = schedulePeriodDaySave(date, { came: true, flow: 'X' }, g1, async (d, r) => {
    await delay(20);
    if (r.flow === 'X') throw new Error('reject 1');
    return true;
  });
  await delay(5);
  const g2 = bumpEditGeneration(date);
  const p2 = schedulePeriodDaySave(date, { came: true, flow: 'Y' }, g2, async (d, r) => {
    await delay(5);
    if (r.flow === 'Y') throw new Error('reject 2');
    return true;
  });
  await delay(5);
  const g3 = bumpEditGeneration(date);
  const p3 = schedulePeriodDaySave(date, { came: true, flow: 'Z' }, g3, async (d, r) => {
    server.set(d, structuredClone(r));
    return true;
  });
  assert.equal((await p1).ok, false);
  assert.equal((await p2).ok, false);
  assert.equal((await p3).ok, true);
  assert.deepEqual(server.get(date), { came: true, flow: 'Z' });
}

// ── out-of-order completion keeps latest snapshot ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const delays = new Map([
    ['2026-06-01:A', 40],
    ['2026-06-01:B', 5],
  ]);
  const saveFn = (date, record) =>
    new Promise((resolve, reject) => {
      const key = `${date}:${record.flow || record.came}`;
      setTimeout(() => {
        if (record.flow === 'A') {
          reject(new Error('slow reject'));
          return;
        }
        server.set(date, structuredClone(record));
        resolve(true);
      }, delays.get(key) ?? 5);
    });
  const gA = bumpEditGeneration('2026-06-01');
  const gB = bumpEditGeneration('2026-06-01');
  const pA = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'A' }, gA, saveFn);
  const pB = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'B' }, gB, saveFn);
  await Promise.all([pA, pB]);
  assert.deepEqual(server.get('2026-06-01'), { came: true, flow: 'B' });
}

// ── idle queue cleanup: stale fields must not leak into later saves ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const date = '2026-06-04';
  const g1 = bumpEditGeneration(date);
  await schedulePeriodDaySave(date, { came: true, flow: '多' }, g1, async (d, r) => {
    server.set(d, structuredClone(r));
    return true;
  });
  assert.equal(getActiveQueueCountForTests(), 0);

  const g2 = bumpEditGeneration(date);
  await schedulePeriodDaySave(date, { came: false, note: '今天没来' }, g2, async (d, r) => {
    server.set(d, structuredClone(r));
    return true;
  });
  assert.deepEqual(server.get(date), { came: false, note: '今天没来' });
  assert.equal(getActiveQueueCountForTests(), 0);
}

// ── delayed reload must not overwrite newer edit C ──
{
  resetPeriodSaveQueues();
  const date = '2026-06-05';
  let local = { [date]: { came: true, flow: 'B' } };
  const serverStale = { [date]: { came: true, flow: 'STALE' } };

  async function reloadIfStillLatest(generation) {
    if (!shouldReloadAfterFailedSave(date, generation)) return;
    const fresh = await delay(40).then(() => structuredClone(serverStale));
    if (!shouldReloadAfterFailedSave(date, generation)) return;
    local = fresh;
  }

  const gB = bumpEditGeneration(date);
  const reloadPromise = reloadIfStillLatest(gB);
  await delay(10);
  const _gC = bumpEditGeneration(date);
  local[date] = { came: false, note: '今天没来' };
  await reloadPromise;

  assert.equal(shouldReloadAfterFailedSave(date, gB), false);
  assert.deepEqual(local[date], { came: false, note: '今天没来' });
}

console.log('period cycle tests: ok');
