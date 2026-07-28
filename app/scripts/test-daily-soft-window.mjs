/**
 * Lightweight FE-R0 unit checks (no vitest yet).
 * Run: npm run test:daily-soft-window
 */
import assert from 'node:assert/strict';

const mod = await import('../src/lib/dailySoftWindow.ts');

const {
  chatDayKeyFromLocalTs,
  groupIntoRounds,
  pickLastNCandidates,
  pickLastNRounds,
  selectedMessageIds,
  selectedRoundMessageIds,
  roundSnippetMessages,
  isCarryoverCount,
  isDailySoftWindowFeEnabled,
  preferMockDailySoftWindow,
  lockedSummaryText,
  countLabel,
  createDailySoftWindowClient,
  classifySoftWindowError,
} = mod;

// chat day 04:00 boundary
assert.equal(chatDayKeyFromLocalTs('2026-07-28 03:59:59'), '2026-07-27');
assert.equal(chatDayKeyFromLocalTs('2026-07-28 04:00:00'), '2026-07-28');
assert.equal(chatDayKeyFromLocalTs('2026-07-28 12:00:00'), '2026-07-28');

// Round grouping: user + following assistants until next user
const cands = [
  { message_id: 1, role: 'user', content_preview: 'a', created_at: '2026-07-27 22:00:00' },
  { message_id: 2, role: 'assistant', content_preview: 'b', created_at: '2026-07-27 22:01:00' },
  { message_id: 3, role: 'user', content_preview: 'c', created_at: '2026-07-27 22:02:00' },
  { message_id: 4, role: 'assistant', content_preview: 'd', created_at: '2026-07-27 22:03:00' },
  { message_id: 5, role: 'assistant', content_preview: 'e', created_at: '2026-07-27 22:03:30' },
  { message_id: 6, role: 'user', content_preview: 'f', created_at: '2026-07-27 22:04:00' },
  { message_id: 7, role: 'assistant', content_preview: 'g', created_at: '2026-07-27 22:05:00' },
];
const rounds = groupIntoRounds(cands);
assert.equal(rounds.length, 3);
assert.equal(rounds[1].round_id, 3);
assert.equal(rounds[1].assistants.length, 2);
assert.deepEqual(selectedRoundMessageIds(rounds, 0), []);
assert.deepEqual(selectedRoundMessageIds(rounds, 3), [1, 2, 3, 4, 5, 6, 7]);
assert.deepEqual(selectedMessageIds(cands, 1), [6, 7]);
assert.deepEqual(
  pickLastNCandidates(cands, 2).map((c) => c.message_id),
  [3, 4, 5, 6, 7],
);
assert.equal(pickLastNRounds(rounds, 5).length, 3);
assert.deepEqual(
  roundSnippetMessages(pickLastNRounds(rounds, 3)).map((c) => c.message_id),
  [1, 7],
);
assert.equal(isCarryoverCount(5), true);
assert.equal(isCarryoverCount(7), false);
assert.equal(countLabel(0), '不带');
assert.equal(countLabel(10), '10轮');

// Chat live gate stays hard-off until backend R1.1
assert.equal(isDailySoftWindowFeEnabled(''), false);
assert.equal(isDailySoftWindowFeEnabled('?dailySoftWindowFe=1'), false);
assert.equal(preferMockDailySoftWindow('?dailySoftWindowFe=1'), true);
assert.equal(preferMockDailySoftWindow('?dailySoftWindowMock=0'), false);

assert.equal(lockedSummaryText(0), '今天没有带走昨天的话。');
assert.equal(lockedSummaryText(5), '今天带来了 5 轮昨天的话。');

// mock client happy path — count is rounds; message ids may exceed count
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=ready' });
  client.resetMock?.('ready');
  const cur = await client.getCurrent();
  assert.equal(cur.selection_finalized, false);
  const cand = await client.getCandidates();
  assert.equal(cand.rounds.length, 10);
  assert.ok(cand.candidates.length > cand.rounds.length); // multi-assistant round expands
  const sel = await client.selectCarryover(3);
  assert.equal(sel.carryover_count, 3);
  assert.ok(sel.selected_message_ids.length >= 3);
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
