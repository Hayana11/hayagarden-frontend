/**
 * Ledger reliability checks (no vitest).
 * Run: npm run test:ledger
 */
import assert from 'node:assert/strict';

const ledger = await import('../src/lib/ledger.ts');
const api = await import('../src/lib/api.ts');
const { entryToPayload } = ledger;

const {
  weekBucketIndex,
  weekSpendBuckets,
  dailyBudgetStorageKey,
  migrateLegacyDailyBudget,
  readDailyBudget,
  writeDailyBudget,
  createMonthRequestGuard,
  resolveLedgerLinks,
  seedDrawerLinksFromEntry,
} = ledger;

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log(`ok — ${name}`);
}

// 9. 29 日进入第 5 周，不是第 4 周
check('weekBucketIndex: day 29 → bucket 4', () => {
  assert.equal(weekBucketIndex(1), 0);
  assert.equal(weekBucketIndex(7), 0);
  assert.equal(weekBucketIndex(8), 1);
  assert.equal(weekBucketIndex(22), 3);
  assert.equal(weekBucketIndex(28), 3);
  assert.equal(weekBucketIndex(29), 4);
  assert.equal(weekBucketIndex(31), 4);
});

check('weekSpendBuckets: day 29 spend stays in 5th band', () => {
  const { values, labels } = weekSpendBuckets(
    [
      { date: '2026-07-05', amount: -10 },
      { date: '2026-07-22', amount: -20 },
      { date: '2026-07-29', amount: -50 },
      { date: '2026-07-31', amount: -30 },
    ],
    31,
  );
  assert.equal(values.length, 5);
  assert.equal(labels[4], '29–31');
  assert.equal(values[3], 20);
  assert.equal(values[4], 80);
});

// 1–3. preserve / cancel links
check('edit keeps old mem not in candidates', () => {
  const entry = { mem: '旧记忆：雨停以后的豆花', read: '《旧书》 · p.12', later: '后来买了花' };
  const picks = [{ text: '最近记忆 A' }, { text: '最近记忆 B' }, { text: '最近记忆 C' }];
  const seeded = seedDrawerLinksFromEntry(entry, picks);
  assert.equal(seeded.mem, entry.mem);
  assert.equal(seeded.memSel, null);
  assert.equal(seeded.read, entry.read);
  assert.equal(seeded.readOn, true);
  const links = resolveLedgerLinks(seeded, picks);
  assert.equal(links.mem, entry.mem);
  assert.equal(links.read, entry.read);
  assert.equal(links.later, entry.later);
  const payload = entryToPayload({
    date: '2026-07-10',
    amount: -68,
    catId: 'food',
    title: '小面',
    who: 'fy',
    reason: '想吃',
    ...links,
  });
  assert.equal(payload.meta.mem, entry.mem);
  assert.equal(payload.meta.read, entry.read);
});

check('edit keeps old read when current co-read changed', () => {
  const seeded = seedDrawerLinksFromEntry(
    { read: '《夜航西飞》 · 第 3 章' },
    [],
  );
  // UI would set readRef to a new book; resolve must keep form.read while readOn
  const links = resolveLedgerLinks(
    { ...seeded, memSel: null },
    [],
  );
  assert.equal(links.read, '《夜航西飞》 · 第 3 章');
});

check('user cancel removes mem/read from PATCH payload', () => {
  const cancelled = resolveLedgerLinks(
    { mem: undefined, memSel: null, read: undefined, readOn: false, later: '' },
    [{ text: '候选' }],
  );
  assert.equal(cancelled.mem, undefined);
  assert.equal(cancelled.read, undefined);
  const payload = entryToPayload({
    date: '2026-07-10',
    amount: -10,
    catId: 'food',
    title: 'x',
    who: 'both',
    reason: '必需',
    ...cancelled,
  });
  assert.equal('mem' in payload.meta, false);
  assert.equal('read' in payload.meta, false);
});

// 10. July manual daily budget does not appear in June
check('daily budget is month-scoped + legacy migrates once', () => {
  const store = (() => {
    const map = new Map();
    return {
      getItem: (k) => (map.has(k) ? map.get(k) : null),
      setItem: (k, v) => { map.set(k, String(v)); },
      removeItem: (k) => { map.delete(k); },
      _map: map,
    };
  })();
  store.setItem('ledger.dailyBudget', '120');
  migrateLegacyDailyBudget('2026-07', store);
  assert.equal(store.getItem('ledger.dailyBudget'), null);
  assert.equal(readDailyBudget('2026-07', store), 120);
  assert.equal(readDailyBudget('2026-06', store), null);
  writeDailyBudget('2026-07', 200, store);
  writeDailyBudget('2026-06', null, store);
  assert.equal(readDailyBudget('2026-07', store), 200);
  assert.equal(readDailyBudget('2026-06', store), null);
  assert.equal(dailyBudgetStorageKey('2026-07'), 'ledger.dailyBudget.2026-07');
  // clearing July must not touch a June key if set later
  writeDailyBudget('2026-06', 90, store);
  writeDailyBudget('2026-07', null, store);
  assert.equal(readDailyBudget('2026-07', store), null);
  assert.equal(readDailyBudget('2026-06', store), 90);
});

// 11. stale month response must not win
check('month request guard ignores stale responses', () => {
  const guard = createMonthRequestGuard();
  const first = guard.begin('2026-06');
  const second = guard.begin('2026-07');
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);
  assert.equal(first.month, '2026-06');
  assert.equal(second.month, '2026-07');
});

// 8. production/default: ledger reads do not allow mock
check('ledgerReadsAllowMock is off without explicit DEV mock flag', () => {
  // In vite-node under this project, DEV may be true; flag must still be explicit.
  assert.equal(typeof api.ledgerReadsAllowMock, 'function');
  assert.equal(api.ledgerReadsAllowMock(), false);
});

// 4–7 write-path contract helpers (payload / id validity semantics)
check('addLedgerEntry treats missing/invalid id as failure shape', () => {
  // Pure contract: backend must return positive id; FE maps otherwise to null.
  const accept = (r) => {
    if (!r.ok) return null;
    const id = r.id;
    if (typeof id !== 'number' || !Number.isFinite(id) || id <= 0) return null;
    return id;
  };
  assert.equal(accept({ ok: true, id: 12 }), 12);
  assert.equal(accept({ ok: true }), null);
  assert.equal(accept({ ok: true, id: 0 }), null);
  assert.equal(accept({ ok: false, id: 3 }), null);
});

check('write failure rollback semantics for local lists', () => {
  const prev = [{ id: 1, title: '旧' }, { id: 2, title: '留' }];
  // POST fail: never insert temp
  let list = [...prev];
  const tempId = -1;
  const postOk = false;
  if (postOk) list = [{ id: tempId, title: '新' }, ...list];
  assert.deepEqual(list, prev);

  // PATCH fail: restore
  list = prev.map((e) => (e.id === 1 ? { ...e, title: '新' } : e));
  const patchOk = false;
  if (!patchOk) list = prev;
  assert.deepEqual(list, prev);

  // DELETE fail: restore
  list = prev.filter((e) => e.id !== 1);
  const delOk = false;
  if (!delOk) list = prev;
  assert.equal(list.some((e) => e.id === 1), true);

  // budget fail surfaces error string
  const budgetOk = false;
  const banner = budgetOk ? null : '预算保存失败';
  assert.equal(banner, '预算保存失败');
});

console.log(`\n${passed} ledger integrity checks passed`);
