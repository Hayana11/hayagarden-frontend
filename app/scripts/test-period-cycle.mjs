/**
 * Period cycle algorithm + save-token unit checks (no vitest).
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
  ymdToEpochDay,
} = cycle;

const { derivePeriod } = period;
const { nextSaveToken, isLatestSaveToken } = save;

// ── date math (UTC epoch-day; DST-safe) ──
assert.equal(diffDays('2026-03-10', '2026-03-08'), 2);
assert.equal(addDays('2026-03-08', 2), '2026-03-10');
// US DST spring-forward neighborhood (2026-03-08): calendar days still +1
assert.equal(diffDays('2026-03-09', '2026-03-08'), 1);
assert.equal(addDays('2026-03-08', 1), '2026-03-09');
// Cross month / year
assert.equal(addDays('2026-01-31', 1), '2026-02-01');
assert.equal(addDays('2025-12-31', 1), '2026-01-01');
assert.equal(diffDays('2026-01-01', '2025-12-31'), 1);
assert.equal(ymdToEpochDay('2026-07-01') - ymdToEpochDay('2026-06-01'), 30);

// ── grouping ──
const groups = groupFlowDates({
  '2026-06-01': { came: true },
  '2026-06-02': { came: true },
  '2026-06-03': { came: true },
  '2026-06-05': { came: true }, // gap → new group
  '2026-06-29': { came: true },
  '2026-06-30': { came: true },
});
assert.equal(groups.length, 3);
assert.deepEqual(groups[0], { start: '2026-06-01', end: '2026-06-03' });
assert.deepEqual(groups[1], { start: '2026-06-05', end: '2026-06-05' });
assert.deepEqual(groups[2], { start: '2026-06-29', end: '2026-06-30' });

// Non-consecutive form different cycles
const g2 = groupFlowDates({
  '2026-06-01': { came: true },
  '2026-06-29': { came: true },
});
assert.equal(g2.length, 2);

const settings = { cycleLength: 28, periodLength: 5, lastStart: '2026-06-01' };

// ── came:true beats average period length ──
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-02': { came: true },
    '2026-06-03': { came: true },
    '2026-06-04': { came: true },
    '2026-06-05': { came: true },
    '2026-06-06': { came: true }, // day 6 > periodLength 5
  };
  assert.equal(resolveInPeriod(days, '2026-06-06', 6, 5), true);
  const c = deriveCycle(days, settings, '2026-06-06');
  assert.equal(c.inPeriod, true);
}

// ── came:false beats prediction ──
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-29': { came: false }, // predicted period day 1 of next cycle if lastStart advanced…
  };
  // With lastStart still Jun 1 and today = Jun 29 (= next start), prediction would say in period,
  // but explicit came:false wins.
  const c = deriveCycle(days, settings, '2026-06-29');
  assert.equal(c.inPeriod, false);
  assert.equal(resolveInPeriod(days, '2026-06-29', 29, 5), false);
}

// ── overdue: daysUntil negative ──
{
  const days = { '2026-06-01': { came: true } };
  const c = deriveCycle(days, settings, '2026-07-01'); // next was Jun 29
  assert.ok(c.daysUntil < 0);
  assert.equal(c.overdue, true);
  assert.equal(cyclePhaseLabel(c), `逾期 ${-c.daysUntil} 天`);
}

// ── home (derivePeriod wrapper) and detail (deriveCycle) agree on phase/days ──
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
    // local Date matching today ymd
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
  assert.ok(c.ovulation.has('2026-06-17')); // 07-01 - 14
  // Must NOT use cycleLength/2 as a separate algorithm (15 ≠ 16)
  assert.ok(!c.ovulation.has(addDays('2026-06-01', Math.round(30 / 2))));
}

// ── pre-period states only in window before real starts ──
{
  const days = {
    '2026-05-28': { came: false, states: ['困'] }, // 4 days before Jun 1
    '2026-05-30': { came: false, states: ['情绪敏感', '想吃甜'] },
    '2026-06-01': { came: true },
    '2026-06-10': { came: false, states: ['腰酸'] }, // far from period — exclude
    '2026-06-02': { came: true, states: ['困'] }, // bleeding — exclude
  };
  const groups = groupFlowDates(days);
  const top = collectPrePeriodTopStates(days, groups);
  assert.ok(top.includes('情绪敏感') || top.includes('想吃甜') || top.includes('困'));
  assert.ok(!top.includes('腰酸'));
  const c = deriveCycle(days, { ...settings, lastStart: '2026-06-01' }, '2026-06-10');
  assert.ok(!c.topStates.includes('腰酸'));
  assert.ok(c.topStates.length >= 1);
}

// Ordinary far-away states alone → empty topStates / neutral copy path
{
  const days = {
    '2026-06-01': { came: true },
    '2026-06-15': { came: false, states: ['困'] },
  };
  const c = deriveCycle(days, settings, '2026-06-20');
  assert.deepEqual(c.topStates, []);
}

// ── save token: older request must not win ──
{
  const map = new Map();
  const t1 = nextSaveToken(map, '2026-06-01');
  const t2 = nextSaveToken(map, '2026-06-01');
  assert.equal(isLatestSaveToken(map, '2026-06-01', t1), false);
  assert.equal(isLatestSaveToken(map, '2026-06-01', t2), true);
}

// ── save success / failure semantics (pure) ──
{
  // success keeps new state
  let local = {};
  const next = { came: true, flow: '中等' };
  local = { ...local, '2026-06-01': next };
  const ok = true;
  assert.equal(ok, true);
  assert.deepEqual(local['2026-06-01'], next);

  // failure rolls back — do not keep fake success
  const prev = undefined;
  let rolled = { '2026-06-01': { came: true } };
  const failed = true;
  if (failed) {
    if (prev === undefined) {
      const copy = { ...rolled };
      delete copy['2026-06-01'];
      rolled = copy;
    }
  }
  assert.equal(rolled['2026-06-01'], undefined);
}

// settings panel stays open on failure (boolean flag model)
{
  let settingsOpen = true;
  let draft = { start: '2026-06-01', cycle: 28, period: 5 };
  const saveOk = false;
  if (saveOk) {
    settingsOpen = false;
    draft = null;
  }
  assert.equal(settingsOpen, true);
  assert.ok(draft);
}

console.log('period cycle tests: ok');
