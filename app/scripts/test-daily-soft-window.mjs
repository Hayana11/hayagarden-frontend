/**
 * Lightweight FE-R0 unit checks (no vitest yet).
 * Run: node --experimental-strip-types scripts/test-daily-soft-window.mjs
 * Fallback: npx vite-node scripts/test-daily-soft-window.mjs
 */
import assert from 'node:assert/strict';

const mod = await import('../src/lib/dailySoftWindow.ts');

const {
  chatDayKeyFromLocalTs,
  pickLastNCandidates,
  selectedMessageIds,
  isCarryoverCount,
  isDailySoftWindowFeEnabled,
  preferMockDailySoftWindow,
  lockedSummaryText,
  createDailySoftWindowClient,
  classifySoftWindowError,
} = mod;

// chat day 04:00 boundary
assert.equal(chatDayKeyFromLocalTs('2026-07-28 03:59:59'), '2026-07-27');
assert.equal(chatDayKeyFromLocalTs('2026-07-28 04:00:00'), '2026-07-28');
assert.equal(chatDayKeyFromLocalTs('2026-07-28 12:00:00'), '2026-07-28');

const cands = [
  { message_id: 1, role: 'user', content_preview: 'a', created_at: '2026-07-27 22:00:00' },
  { message_id: 2, role: 'assistant', content_preview: 'b', created_at: '2026-07-27 22:01:00' },
  { message_id: 3, role: 'user', content_preview: 'c', created_at: '2026-07-27 22:02:00' },
  { message_id: 4, role: 'assistant', content_preview: 'd', created_at: '2026-07-27 22:03:00' },
];
assert.deepEqual(selectedMessageIds(cands, 0), []);
assert.deepEqual(selectedMessageIds(cands, 3), [2, 3, 4]);
assert.deepEqual(
  pickLastNCandidates(cands, 10).map((c) => c.message_id),
  [1, 2, 3, 4],
);
assert.equal(isCarryoverCount(5), true);
assert.equal(isCarryoverCount(7), false);

assert.equal(isDailySoftWindowFeEnabled(''), false);
assert.equal(isDailySoftWindowFeEnabled('?dailySoftWindowFe=1'), true);
assert.equal(isDailySoftWindowFeEnabled('?dailySoftWindowFe=0'), false);
assert.equal(preferMockDailySoftWindow('?dailySoftWindowFe=1'), true);
assert.equal(preferMockDailySoftWindow('?dailySoftWindowMock=0&dailySoftWindowFe=1'), false);

assert.equal(lockedSummaryText(0), '今天没有带走昨天的话。');
assert.equal(lockedSummaryText(5), '今天带来了 5 句昨天的话。');

// mock client happy path
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=ready' });
  client.resetMock?.('ready');
  const cur = await client.getCurrent();
  assert.equal(cur.selection_finalized, false);
  const cand = await client.getCandidates();
  assert.ok(cand.candidates.length >= 3);
  const sel = await client.selectCarryover(3);
  assert.equal(sel.carryover_count, 3);
  assert.equal(sel.selected_message_ids.length, 3);
  let threw = false;
  try {
    await client.selectCarryover(10);
  } catch (err) {
    threw = true;
    assert.equal(classifySoftWindowError(err), 'conflict');
  }
  assert.equal(threw, true);
}

// mock 404
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=disabled' });
  client.resetMock?.('disabled');
  let state = 'idle';
  try {
    await client.getCurrent();
  } catch (err) {
    state = classifySoftWindowError(err);
  }
  assert.equal(state, 'disabled');
}

console.log('daily-soft-window FE-R0 tests: ok');
