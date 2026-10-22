import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  decisionText,
  fetchReviewPage,
  normalizeReviewPage,
  normalizeReviewRow,
  reasonText,
  saveReviewLabel,
  verdictText,
} from '../src/lib/memoryReview.ts';

const wireRow = {
  id: 7,
  user_message_id: 12697,
  created_at: '2026-10-22 16:40:00',
  mode: 'shadow',
  decision: 'none',
  reason: 'gate_closed',
  verdict: 'below_evidence',
  evidence: 3.2,
  committed: false,
  message: '爸爸的眼睛就是石榴石的颜色',
  clues: ['石榴石'],
  chat_df: { 石榴石: 2, junk: 'x' },
  matched: ['石榴石'],
  top: { ref: 'kernel:ring', date: '2026-08-01', type: 'MEMORY', text: '《寂静》戒指' },
  runner_up: null,
  frozen: true,
  log_pruned: true,
  expected: { ref: 'kernel:ring', date: '2026-08-01', type: 'MEMORY', text: '《寂静》戒指' },
  label: { label: 'missed', note: '该想起戒指', expected_ref: 'kernel:ring', labeled_at: '2026-10-22 17:00:00' },
};

// Wire rows normalize to camelCase; junk is dropped, never invented.
const row = normalizeReviewRow(wireRow);
assert.equal(row.userMessageId, 12697);
assert.deepEqual(row.chatDf, { 石榴石: 2 });
assert.equal(row.top.ref, 'kernel:ring');
assert.equal(row.runnerUp, null);
assert.equal(row.frozen, true);
assert.equal(row.logPruned, true);
assert.equal(row.expected.ref, 'kernel:ring');
assert.equal(normalizeReviewRow({ id: 1 }).frozen, false);
assert.deepEqual(row.label, { label: 'missed', note: '该想起戒指', expectedRef: 'kernel:ring', labeledAt: '2026-10-22 17:00:00' });
assert.equal(normalizeReviewRow({ id: 'x' }), null);
assert.equal(normalizeReviewRow({ ...wireRow, label: { label: 'maybe' } }).label, null);

const page = normalizeReviewPage({ rows: [wireRow, null], counts: { total: 11, labeled: 1, missed: 1 }, next_before: null });
assert.equal(page.rows.length, 1);
assert.deepEqual(page.counts, { total: 11, labeled: 1, ok: 0, missed: 1, wrong: 0 });
assert.equal(page.nextBefore, null);
assert.deepEqual(normalizeReviewPage(undefined), { rows: [], counts: { total: 0, labeled: 0, ok: 0, missed: 0, wrong: 0 }, nextBefore: null });

// Human wording for the route log.
assert.equal(reasonText('gate_closed'), '被拦下');
assert.equal(reasonText('something_new'), 'something_new');
assert.equal(verdictText('below_evidence'), '证据不够');
assert.equal(decisionText({ decision: 'surfaced', mode: 'shadow', committed: false }), '会浮现（shadow）');
assert.equal(decisionText({ decision: 'surfaced', mode: 'on', committed: true }), '已浮现');
assert.equal(decisionText({ decision: 'surfaced', mode: 'on', committed: false }), '浮现未落库');

// API: real endpoints, filter and paging in the query, label body as the backend expects.
const calls = [];
globalThis.fetch = async (url, init = {}) => {
  calls.push({ url, method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : undefined });
  const body = String(url).includes('/label')
    ? (String(url).includes('/8/') ? { id: 8, removed: true } : wireRow)
    : { rows: [wireRow], counts: {}, next_before: 7 };
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
const fetched = await fetchReviewPage('blocked', 40);
assert.equal(calls[0].url, '/api/memory-surface/review?filter=blocked&limit=30&before=40');
assert.equal(fetched.nextBefore, 7);
await fetchReviewPage('all');
assert.equal(calls[1].url, '/api/memory-surface/review?filter=all&limit=30');
const saved = await saveReviewLabel(7, 'missed', '该想起戒指', 'kernel:ring');
assert.deepEqual(calls[2], {
  url: '/api/memory-surface/review/7/label',
  method: 'POST',
  body: { label: 'missed', note: '该想起戒指', expected_ref: 'kernel:ring' },
});
assert.equal(saved.id, 7);
// Clearing the label of a pruned decision removes it: no row comes back.
assert.equal(await saveReviewLabel(8, 'clear'), null);

// Wiring: one route authority, standard chrome without global nav, entry from the memory library.
const read = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), 'utf8');
assert.match(read('navigation.ts'), /memoryReview:\s*'\/memory-review'/);
assert.match(read('navigation.ts'), /memoryReview:\s*\{\s*chrome:\s*'standard',\s*globalNav:\s*false\s*\}/);
assert.match(read('App.tsx'), /<Route path=\{ROUTES\.memoryReview\} element=\{<MemoryReviewScreen \/>\} \/>/);
assert.match(read('screens/MemoryScreen.tsx'), /to=\{ROUTES\.memoryReview\}/);
const screen = read('screens/MemoryReviewScreen.tsx');
assert.match(screen, /<PageHeader[\s\S]*onBack=\{\(\) => navigate\(ROUTES\.memory\)\}/);
assert.doesNotMatch(screen, /\bgap\s*:/, 'Chrome78: no flex gap in the review page');
assert.doesNotMatch(screen, /mock/i, 'review data is real only');

console.log('test-memory-review: ok');
