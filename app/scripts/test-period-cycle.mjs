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
const { schedulePeriodDaySave, resetPeriodSaveQueues } = save;

// ── date math (UTC epoch-day; DST-safe) ──
assert.equal(diffDays('2026-03-10', '2026-03-08'), 2);
assert.equal(addDays('2026-03-08', 2), '2026-03-10');
assert.equal(diffDays('2026-03-09', '2026-03-08'), 1);
assert.equal(addDays('2026-03-08', 1), '2026-03-09');
assert.equal(addDays('2026-01-31', 1), '2026-02-01');
assert.equal(addDays('2025-12-31', 1), '2026-01-01');
assert.equal(diffDays('2026-01-01', '2025-12-31'), 1);
assert.equal(ymdToEpochDay('2026-07-01') - ymdToEpochDay('2026-06-01'), 30);

// ── grouping ──
const groups = groupFlowDates({
  '2026-06-01': { came: true },
  '2026-06-02': { came: true },
  '2026-06-03': { came: true },
  '2026-06-05': { came: true },
  '2026-06-29': { came: true },
  '2026-06-30': { came: true },
});
assert.equal(groups.length, 3);

const settings = { cycleLength: 28, periodLength: 5, lastStart: '2026-06-01' };
const emptySettings = { cycleLength: 28, periodLength: 5, lastStart: '' };

// ── empty anchor: no fake day-1 period ──
{
  const c = deriveCycle({}, emptySettings, '2026-07-10');
  assert.equal(c.hasAnchor, false);
  assert.equal(c.inPeriod, false);
  assert.equal(c.cycleDay, null);
  assert.equal(c.nextStart, null);
  assert.equal(c.daysUntil, null);
  assert.equal(c.predicted.size, 0);
  assert.equal(c.ovulation.size, 0);
  assert.equal(cyclePhaseLabel(c), '暂无记录');
}

// ── came:true beats average period length ──
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-02': { came: true },
    '2026-06-03': { came: true },
    '2026-06-04': { came: true },
    '2026-06-05': { came: true },
    '2026-06-06': { came: true },
  };
  assert.equal(resolveInPeriod(days, '2026-06-06', 6, 5), true);
  assert.equal(deriveCycle(days, settings, '2026-06-06').inPeriod, true);
}

// ── came:false beats prediction ──
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-29': { came: false },
  };
  const c = deriveCycle(days, settings, '2026-06-29');
  assert.equal(c.inPeriod, false);
  assert.equal(shouldPredictPeriodDay(days, '2026-06-29'), false);
  assert.ok(!c.predicted.has('2026-06-29'));
}

// ── came:false excluded from predicted set ──
{
  const days = { '2026-06-01': { came: true }, '2026-06-30': { came: false } };
  const c = deriveCycle(days, settings, '2026-06-10');
  assert.ok(!c.predicted.has('2026-06-30'));
}

// ── overdue ──
{
  const days = { '2026-06-01': { came: true } };
  const c = deriveCycle(days, settings, '2026-07-01');
  assert.ok(c.daysUntil !== null && c.daysUntil < 0);
  assert.equal(c.overdue, true);
  assert.equal(cyclePhaseLabel(c), `逾期 ${-c.daysUntil} 天`);
}

// ── home and detail agree ──
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-02': { came: true },
    '2026-06-03': { came: true },
  };
  const today = '2026-06-10';
  const detail = deriveCycle(days, settings, today);
  const home = derivePeriod(
    {
      lastPeriodStart: '2026-06-01',
      cycleLengthAvgDays: 28,
      periodLengthAvgDays: 5,
      recordsCount: 3,
      nextPredicted: '2026-06-29',
    },
    new Date(2026, 5, 10),
  );
  assert.equal(home.phase, cyclePhaseLabel(detail));
  assert.equal(home.daysLeft, detail.daysUntil);
}

// ── ovulation = next start − 14 ──
{
  const days = { '2026-06-01': { came: true } };
  const c = deriveCycle(days, { cycleLength: 30, periodLength: 5, lastStart: '2026-06-01' }, '2026-06-10');
  assert.equal(c.nextStart, '2026-07-01');
  assert.ok(c.ovulation.has('2026-06-17'));
  assert.ok(!c.ovulation.has(addDays('2026-06-01', Math.round(30 / 2))));
}

// ── pre-period states ──
{
  const days = {
    '2026-05-28': { came: false, states: ['困'] },
    '2026-05-30': { came: false, states: ['情绪敏感', '想吃甜'] },
    '2026-06-01': { came: true },
    '2026-06-10': { came: false, states: ['腰酸'] },
    '2026-06-02': { came: true, states: ['困'] },
  };
  const g = groupFlowDates(days);
  const top = collectPrePeriodTopStates(days, g);
  assert.ok(!top.includes('腰酸'));
}

// ── serial save queue: out-of-order completion keeps latest snapshot ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const delays = new Map([
    ['2026-06-01:A', 40],
    ['2026-06-01:B', 5],
  ]);
  const saveFn = (date, record) =>
    new Promise((resolve) => {
      const key = `${date}:${record.flow || record.came}`;
      setTimeout(() => {
        server.set(date, structuredClone(record));
        resolve(true);
      }, delays.get(key) ?? 5);
    });

  const pA = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'A' }, saveFn);
  const pB = schedulePeriodDaySave('2026-06-01', { came: true, flow: 'B' }, saveFn);
  await Promise.all([pA, pB]);
  assert.deepEqual(server.get('2026-06-01'), { came: true, flow: 'B' });
}

// ── rapid triple edit ends with last state ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  const saveFn = async (date, record) => {
    server.set(date, structuredClone(record));
    return true;
  };
  schedulePeriodDaySave('2026-06-02', { came: true }, saveFn);
  schedulePeriodDaySave('2026-06-02', { came: true, flow: '少量' }, saveFn);
  await schedulePeriodDaySave('2026-06-02', { came: true, flow: '多', pain: '轻微' }, saveFn);
  assert.deepEqual(server.get('2026-06-02'), { came: true, flow: '多', pain: '轻微' });
}

// ── middle failure then later success keeps final good state ──
{
  resetPeriodSaveQueues();
  const server = new Map();
  let calls = 0;
  const saveFn = async (date, record) => {
    calls += 1;
    if (calls === 1) return false;
    server.set(date, structuredClone(record));
    return true;
  };
  const r1 = await schedulePeriodDaySave('2026-06-03', { came: true, flow: 'A' }, saveFn);
  const r2 = await schedulePeriodDaySave('2026-06-03', { came: true, flow: 'B' }, saveFn);
  assert.equal(r1, false);
  assert.equal(r2, true);
  assert.deepEqual(server.get('2026-06-03'), { came: true, flow: 'B' });
}

console.log('period cycle tests: ok');
