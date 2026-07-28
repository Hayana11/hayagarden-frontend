/**
 * Soft Window FE-R1 unit checks (no vitest).
 * Run: npm run test:daily-soft-window
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { HttpError } from '../src/lib/http.ts';

const mod = await import('../src/lib/dailySoftWindow.ts');

const {
  chatDayKeyFromLocalTs,
  parseCanonicalRounds,
  validateCanonicalRounds,
  pickLastNRounds,
  flattenRoundMessageIds,
  roundSnippetMessages,
  selectedRoundMessageIds,
  isCarryoverCount,
  isDailySoftWindowFeEnabled,
  preferMockDailySoftWindow,
  lockedSummaryText,
  countLabel,
  createDailySoftWindowClient,
  classifySoftWindowError,
  draftCountFromCurrent,
  normalizeMessageId,
  boundaryInsertIndex,
  LIVE_DAILY_CONTEXT_CURRENT,
  LIVE_DAILY_CONTEXT_CANDIDATES,
  LIVE_DAILY_CONTEXT_SELECT,
  parseCurrent,
  parseSelectCarryoverResponse,
  parseCarryoverCandidatesResponse,
  normalizePositiveInt,
} = mod;

let passed = 0;
function ok(label) {
  passed += 1;
  // quiet per-assert; summary at end
  void label;
}

// ── chat day (preview transcript only) ──
assert.equal(chatDayKeyFromLocalTs('2026-07-28 03:59:59'), '2026-07-27');
assert.equal(chatDayKeyFromLocalTs('2026-07-28 04:00:00'), '2026-07-28');
ok('chatDay');

// ── live path constants ──
assert.equal(LIVE_DAILY_CONTEXT_CURRENT, '/api/gw/daily-context/current');
assert.equal(LIVE_DAILY_CONTEXT_CANDIDATES, '/api/gw/daily-context/carryover-candidates');
assert.equal(LIVE_DAILY_CONTEXT_SELECT, '/api/gw/daily-context/select-carryover');
assert.ok(!LIVE_DAILY_CONTEXT_CURRENT.includes('/api/daily-context/current') || LIVE_DAILY_CONTEXT_CURRENT.startsWith('/api/gw/'));
assert.ok(LIVE_DAILY_CONTEXT_CURRENT.startsWith('/api/gw/'));
ok('live paths');

// ── isDailySoftWindowFeEnabled always false ──
assert.equal(isDailySoftWindowFeEnabled(''), false);
assert.equal(isDailySoftWindowFeEnabled('?dailySoftWindowFe=1'), false);
assert.equal(preferMockDailySoftWindow('?dailySoftWindowMock=0'), false);
ok('fe gate');

// ── canonical parse/validate ──
const goodRounds = [
  {
    round_id: 1,
    message_ids: [1, 2],
    messages: [
      { message_id: 1, role: 'user', author: 'hayana', content_preview: 'a', created_at: 't' },
      { message_id: 2, role: 'assistant', author: 'fyodor', content_preview: 'b', created_at: 't' },
    ],
  },
  {
    round_id: 3,
    message_ids: [3, 4, 5],
    messages: [
      { message_id: 3, role: 'user', author: 'hayana', content_preview: 'c', created_at: 't' },
      { message_id: 4, role: 'assistant', author: 'fyodor', content_preview: 'd', created_at: 't' },
      { message_id: 5, role: 'assistant', author: 'fyodor', content_preview: 'e', created_at: 't' },
    ],
  },
  {
    round_id: 6,
    message_ids: [6],
    messages: [
      { message_id: 6, role: 'user', author: 'hayana', content_preview: 'f', created_at: 't' },
    ],
  },
];
assert.equal(validateCanonicalRounds(goodRounds, 'round')?.length, 3);
assert.equal(parseCanonicalRounds(goodRounds, 'message'), null);
assert.equal(validateCanonicalRounds(goodRounds, 'message'), null);
// first msg not user
assert.equal(
  validateCanonicalRounds(
    [{ round_id: 9, message_ids: [9], messages: [{ message_id: 9, role: 'assistant', author: 'f', content_preview: 'x', created_at: 't' }] }],
    'round',
  ),
  null,
);
// round_id mismatch
assert.equal(
  validateCanonicalRounds(
    [{
      round_id: 99,
      message_ids: [1],
      messages: [{ message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' }],
    }],
    'round',
  ),
  null,
);
// message_ids mismatch messages
assert.equal(
  validateCanonicalRounds(
    [{
      round_id: 1,
      message_ids: [1, 99],
      messages: [
        { message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' },
        { message_id: 2, role: 'assistant', author: 'f', content_preview: 'y', created_at: 't' },
      ],
    }],
    'round',
  ),
  null,
);
ok('canonical validate');

// ── pick last N / multi-assistant / user-only / flatten ──
assert.deepEqual(selectedRoundMessageIds(goodRounds, 0), []);
assert.deepEqual(selectedRoundMessageIds(goodRounds, 3), [1, 2, 3, 4, 5, 6]);
assert.equal(pickLastNRounds(goodRounds, 5).length, 3);
assert.equal(pickLastNRounds(goodRounds, 2).length, 2);
assert.deepEqual(flattenRoundMessageIds(pickLastNRounds(goodRounds, 2)), [3, 4, 5, 6]);
assert.deepEqual(
  roundSnippetMessages(pickLastNRounds(goodRounds, 3)).map((c) => c.message_id),
  [1, 6],
);
// multi-assistant middle round intact
assert.equal(goodRounds[1].messages.filter((m) => m.role === 'assistant').length, 2);
// user-only tail
assert.equal(goodRounds[2].messages.length, 1);
assert.equal(goodRounds[2].messages[0].role, 'user');
ok('pick/flatten');

assert.equal(isCarryoverCount(5), true);
assert.equal(isCarryoverCount(7), false);
assert.equal(countLabel(0), '不带');
assert.equal(countLabel(10), '10轮');
assert.equal(lockedSummaryText(0), '今天没有带走昨天的话。');
assert.equal(lockedSummaryText(5), '今天带来了 5 轮昨天的话。');
assert.equal(normalizeMessageId('42'), 42);
assert.equal(normalizeMessageId(0), null);
assert.equal(normalizeMessageId(1.5), null);
assert.equal(normalizeMessageId('1.5'), null);
assert.equal(normalizeMessageId(Number.NaN), null);
assert.equal(normalizeMessageId(Number.POSITIVE_INFINITY), null);
assert.equal(normalizeMessageId(-1), null);
assert.equal(normalizePositiveInt(1.5), null);
assert.equal(normalizePositiveInt('3'), null);
ok('labels');

// ── strict current / select / candidates parsers ──
{
  const base = {
    context_id: 1,
    context_epoch: 2,
    local_day: '2026-07-28',
    boundary_message_id: 0,
    carryover_unit: 'round',
    selected_round_count: 0,
    selected_message_count: 0,
    selected_message_ids: [],
    carryover_count: 0,
    selection_finalized: false,
    handoff_status: 'ABSENT',
    resident_generation: 1,
    requested_round_count: null,
  };
  assert.ok(parseCurrent({ ...base }));
  assert.equal(parseCurrent({ ...base, context_epoch: 1.5 }), null);
  assert.equal(parseCurrent({ ...base, selected_message_ids: [1, 1] }), null);
  assert.equal(parseCurrent({ ...base, selected_message_ids: [1.5] }), null);
  assert.equal(parseCurrent({ ...base, selected_message_count: 1 }), null);
  assert.equal(parseCurrent({ ...base, carryover_count: 1 }), null);
  assert.equal(parseCurrent({ ...base, requested_round_count: 3 }), null);
  assert.ok(
    parseCurrent({
      ...base,
      selection_finalized: true,
      requested_round_count: 5,
      selected_round_count: 2,
      selected_message_count: 2,
      selected_message_ids: [10, 11],
      carryover_count: 2,
    }),
  );
  assert.equal(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: 3,
      selected_round_count: 4,
      selected_message_count: 2,
      selected_message_ids: [1, 2],
      carryover_count: 4,
      finalized_at: 't',
    }),
    null,
  );
  assert.equal(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: '3',
      selected_round_count: 3,
      selected_message_count: 0,
      selected_message_ids: [],
      carryover_count: 3,
      finalized_at: 't',
    }),
    null,
  );
  assert.ok(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: 0,
      selected_round_count: 0,
      selected_message_count: 0,
      selected_message_ids: [],
      carryover_count: 0,
      finalized_at: 't',
    }),
  );
  assert.equal(
    parseCarryoverCandidatesResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      available_round_count: 0,
      rounds: [],
      candidates: [],
    })?.rounds.length,
    0,
  );
  assert.equal(
    parseCarryoverCandidatesResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      available_round_count: 0,
      rounds: goodRounds,
      candidates: [],
    }),
    null,
  );
}
ok('strict parsers');

// ── draftCountFromCurrent recovery ──
assert.equal(
  draftCountFromCurrent({
    context_id: 1,
    context_epoch: 1,
    local_day: '2026-07-28',
    boundary_message_id: 1,
    carryover_unit: 'round',
    requested_round_count: 5,
    selected_round_count: 2,
    selected_message_count: 4,
    selected_message_ids: [1, 2, 3, 4],
    carryover_count: 2,
    selection_finalized: true,
    handoff_status: 'ABSENT',
    resident_generation: 1,
  }),
  5,
);
assert.equal(
  draftCountFromCurrent({
    context_id: 1,
    context_epoch: 1,
    local_day: '2026-07-28',
    boundary_message_id: 1,
    carryover_unit: 'round',
    requested_round_count: null,
    selected_round_count: 3,
    selected_message_count: 6,
    selected_message_ids: [],
    carryover_count: 3,
    selection_finalized: true,
    handoff_status: 'ABSENT',
    resident_generation: 1,
  }),
  3,
);
ok('draft recovery');

// ── boundary insert: both sides required ──
assert.equal(boundaryInsertIndex([10, 20, 30], 20), 2);
assert.equal(boundaryInsertIndex([10, 20, 30], 30), null); // no id > boundary
assert.equal(boundaryInsertIndex([25, 30, 35], 20), null); // only new side
assert.equal(boundaryInsertIndex([5, 10], 20), null); // only old side
ok('boundary');

// ── missing carryover_unit / message_ids not synthesized ──
{
  const { parseCurrent, parseSelectCarryoverResponse } = mod;
  assert.equal(parseCurrent({ context_id: 1, context_epoch: 1, boundary_message_id: 1 }), null);
  assert.equal(
    validateCanonicalRounds(
      [{ round_id: 1, messages: [{ message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' }] }],
      'round',
    ),
    null,
  );
  assert.equal(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 1,
      carryover_unit: 'round',
      requested_round_count: 3,
      selected_round_count: 3,
      selected_message_count: 1,
      selected_message_ids: [1, 2],
      carryover_count: 3,
    }),
    null,
  );
}
ok('strict parse');

// ── classify 404/423/409/401 ──
assert.equal(classifySoftWindowError(new HttpError(404, 'disabled', 'x')), 'disabled');
assert.equal(
  classifySoftWindowError(
    new HttpError(423, 'deferred', 'x', { code: 'rollover_deferred' }),
  ),
  'deferred',
);
assert.equal(classifySoftWindowError(new HttpError(409, 'locked', 'x')), 'conflict');
assert.equal(classifySoftWindowError(new HttpError(401, 'auth', 'x')), 'auth_error');
assert.equal(classifySoftWindowError(new HttpError(403, 'auth', 'x')), 'auth_error');
assert.equal(classifySoftWindowError(new HttpError(500, 'boom', 'x')), 'error');
ok('classify');

// ── mock select carryover_count = rounds ──
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=ready' });
  client.resetMock?.('ready');
  const cur = await client.getCurrent();
  assert.equal(cur.selection_finalized, false);
  assert.equal(cur.carryover_unit, 'round');
  const cand = await client.getCandidates();
  assert.equal(cand.carryover_unit, 'round');
  assert.equal(cand.rounds.length, 10);
  // multi-assistant present
  assert.ok(cand.rounds.some((r) => r.messages.filter((m) => m.role === 'assistant').length >= 2));
  // user-only tail ok
  assert.ok(cand.rounds.some((r) => r.messages.length === 1 && r.messages[0].role === 'user'));
  const validated = validateCanonicalRounds(cand.rounds, cand.carryover_unit);
  assert.ok(validated);
  const sel = await client.selectCarryover(3);
  assert.equal(sel.carryover_count, 3);
  assert.equal(sel.selected_round_count, 3);
  assert.equal(sel.requested_round_count, 3);
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
ok('mock select');

// ── mock locked requested=5 selected=2 ──
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=locked' });
  client.resetMock?.('locked');
  const cur = await client.getCurrent();
  assert.equal(cur.selection_finalized, true);
  assert.equal(cur.requested_round_count, 5);
  assert.equal(cur.selected_round_count, 2);
  assert.equal(cur.carryover_count, 2);
  assert.ok(cur.selected_message_ids.length > 0);
  assert.equal(draftCountFromCurrent(cur), 5);
}
ok('mock locked');

// ── mock 404 / 423 ──
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
{
  const client = createDailySoftWindowClient({ forceMock: true, search: '?mockScenario=deferred' });
  client.resetMock?.('deferred');
  let state = 'idle';
  let code;
  try {
    await client.getCurrent();
  } catch (err) {
    state = classifySoftWindowError(err);
    code = err.code;
  }
  assert.equal(state, 'deferred');
  assert.equal(code, 'rollover_deferred');
}
ok('mock 404/423');

// ── token / VITE secrets must not appear in app/src ──
{
  const __dirname = path.dirname(fileURLToPath(import.meta.url));
  const srcRoot = path.resolve(__dirname, '../src');
  const offenders = [];
  function walk(dir) {
    for (const name of fs.readdirSync(dir)) {
      const p = path.join(dir, name);
      const st = fs.statSync(p);
      if (st.isDirectory()) walk(p);
      else if (/\.(ts|tsx|js|jsx|mjs|css)$/.test(name)) {
        const text = fs.readFileSync(p, 'utf8');
        if (text.includes('VITE_DAILY_SOFT_WINDOW')) offenders.push(`${p}: VITE_DAILY_SOFT_WINDOW`);
        // Must not embed Bearer + env token pattern as a client secret wiring
        if (/Authorization\s*[:=]\s*['`]Bearer\s*\$\{/.test(text)) {
          offenders.push(`${p}: Authorization Bearer template`);
        }
        if (/Bearer\s+\$\{?\s*import\.meta\.env/.test(text)) {
          offenders.push(`${p}: Bearer import.meta.env`);
        }
        // Literal assignment of DAILY_SOFT_WINDOW_TOKEN value into Authorization from FE
        if (/localStorage\.getItem\(\s*['"]DAILY_SOFT_WINDOW/.test(text)) {
          offenders.push(`${p}: localStorage DAILY_SOFT_WINDOW`);
        }
      }
    }
  }
  walk(srcRoot);
  // Also ensure live client file uses gw paths only
  const dsw = fs.readFileSync(path.join(srcRoot, 'lib/dailySoftWindow.ts'), 'utf8');
  assert.ok(dsw.includes('/api/gw/daily-context/'));
  assert.ok(!dsw.includes("http.get<") || dsw.includes('LIVE_DAILY_CONTEXT'));
  // No direct browser call to unprotected upstream path as live base
  const liveSection = dsw.slice(dsw.indexOf("mode: 'live'"));
  assert.ok(!liveSection.includes("'/api/daily-context/"));
  assert.equal(offenders.length, 0, offenders.join('\n'));
}
ok('no token in app/src');

console.log(`daily-soft-window FE-R1 tests: ok (${passed} groups)`);
