/**
 * Soft Window FE-R1 state-machine / integration tests (no React, no Playwright browser).
 * Fake client + injectable scheduler cover race fencing and contract rules.
 *
 * Run: npm run test:daily-soft-window-machine
 */
import assert from 'node:assert/strict';
import {
  boundaryInsertIndex,
  parseCurrent,
  parseSelectCarryoverResponse,
  validateCanonicalRounds,
} from '../src/lib/dailySoftWindow.ts';
import {
  DailySoftWindowController,
  DEFERRED_RETRY_MS,
} from '../src/lib/dailySoftWindowController.ts';
import { HttpError } from '../src/lib/http.ts';

let passed = 0;
function ok(label) {
  passed += 1;
  void label;
}

function round(id, assistants = 1) {
  const messages = [
    { message_id: id, role: 'user', author: 'h', content_preview: 'u', created_at: 't' },
  ];
  const message_ids = [id];
  for (let i = 0; i < assistants; i++) {
    const mid = id + 1 + i;
    messages.push({
      message_id: mid,
      role: 'assistant',
      author: 'f',
      content_preview: 'a',
      created_at: 't',
    });
    message_ids.push(mid);
  }
  return { round_id: id, message_ids, messages };
}

const ROUNDS = [round(1), round(3, 2), round(6, 0)];

function baseCurrent(over = {}) {
  return {
    ok: true,
    context_id: 7,
    context_epoch: 42,
    local_day: '2026-07-28',
    boundary_message_id: 10,
    carryover_unit: 'round',
    requested_round_count: null,
    selected_round_count: 0,
    selected_message_count: 0,
    selected_message_ids: [],
    carryover_count: 0,
    selection_finalized: false,
    handoff_status: 'ABSENT',
    resident_generation: 1,
    ...over,
  };
}

function makeFakeClient(opts = {}) {
  const calls = {
    current: 0,
    candidates: 0,
    select: 0,
    selectBodies: [],
  };
  let currentImpl = opts.current ?? (async () => baseCurrent());
  let candidatesImpl =
    opts.candidates ??
    (async () => ({
      ok: true,
      context_id: 7,
      context_epoch: 42,
      carryover_unit: 'round',
      available_round_count: ROUNDS.length,
      rounds: ROUNDS,
      candidates: [],
    }));
  let selectImpl =
    opts.select ??
    (async (count) => ({
      ok: true,
      context_id: 7,
      context_epoch: 42,
      carryover_unit: 'round',
      requested_round_count: count,
      selected_round_count: count,
      selected_message_count: count,
      selected_message_ids: Array.from({ length: count }, (_, i) => i + 1),
      carryover_count: count,
      finalized_at: '2026-07-28 04:10:00',
    }));

  /** Pending resolvers for stalling requests. */
  const stalls = { current: [], candidates: [], select: [] };

  const client = {
    mode: 'live',
    calls,
    setCurrent(fn) {
      currentImpl = fn;
    },
    setCandidates(fn) {
      candidatesImpl = fn;
    },
    setSelect(fn) {
      selectImpl = fn;
    },
    stallCurrent() {
      return new Promise((resolve) => {
        stalls.current.push(resolve);
      });
    },
    releaseCurrent(value) {
      const r = stalls.current.shift();
      if (r) r(value);
    },
    getCurrent: async (init) => {
      calls.current += 1;
      if (init?.signal?.aborted) {
        const err = new Error('Aborted');
        err.name = 'AbortError';
        throw err;
      }
      return currentImpl(init);
    },
    getCandidates: async (init) => {
      calls.candidates += 1;
      if (init?.signal?.aborted) {
        const err = new Error('Aborted');
        err.name = 'AbortError';
        throw err;
      }
      return candidatesImpl(init);
    },
    selectCarryover: async (count, init) => {
      calls.select += 1;
      calls.selectBodies.push(count);
      if (init?.signal?.aborted) {
        const err = new Error('Aborted');
        err.name = 'AbortError';
        throw err;
      }
      return selectImpl(count, init);
    },
  };
  return client;
}

function makeScheduler() {
  const jobs = [];
  const scheduler = {
    schedule(fn, ms) {
      const job = { fn, ms, cancelled: false };
      jobs.push(job);
      return {
        cancel() {
          job.cancelled = true;
        },
      };
    },
    async flush() {
      const pending = jobs.filter((j) => !j.cancelled);
      jobs.length = 0;
      for (const j of pending) j.fn();
      await Promise.resolve();
    },
    pendingCount() {
      return jobs.filter((j) => !j.cancelled).length;
    },
  };
  return scheduler;
}

function waitMicrotasks(n = 5) {
  let p = Promise.resolve();
  for (let i = 0; i < n; i++) p = p.then(() => undefined);
  return p;
}

async function waitFor(pred, label, max = 40) {
  for (let i = 0; i < max; i++) {
    if (pred()) return;
    await waitMicrotasks(2);
  }
  throw new Error(`timeout waiting: ${label}`);
}

async function openDrawerWithCandidates(ctrl) {
  ctrl.openDrawer({ focus() {}, isConnected: true });
  await waitFor(() => ctrl.getSnapshot().candidatesReady === true, 'candidates ready');
}

// ── strict parsers ──
{
  assert.equal(parseCurrent({ context_id: 1, context_epoch: 1 }), null); // missing unit
  assert.equal(
    parseCurrent({
      context_id: 1,
      context_epoch: 1,
      carryover_unit: 'message',
      boundary_message_id: 1,
      selected_message_ids: [],
      selected_round_count: 0,
      selected_message_count: 0,
      carryover_count: 0,
      selection_finalized: false,
    }),
    null,
  );
  assert.ok(
    parseCurrent({
      context_id: 1,
      context_epoch: 1,
      carryover_unit: 'round',
      boundary_message_id: 1,
      selected_message_ids: [],
      selected_round_count: 0,
      selected_message_count: 0,
      carryover_count: 0,
      selection_finalized: false,
    }),
  );

  // message_ids must exist — cannot invent from messages
  assert.equal(
    validateCanonicalRounds(
      [
        {
          round_id: 1,
          messages: [
            { message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' },
          ],
        },
      ],
      'round',
    ),
    null,
  );
  // messages[1:] must be assistant
  assert.equal(
    validateCanonicalRounds(
      [
        {
          round_id: 1,
          message_ids: [1, 2],
          messages: [
            { message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' },
            { message_id: 2, role: 'user', author: 'h', content_preview: 'y', created_at: 't' },
          ],
        },
      ],
      'round',
    ),
    null,
  );
  // duplicate ids across rounds
  assert.equal(
    validateCanonicalRounds(
      [
        {
          round_id: 1,
          message_ids: [1, 2],
          messages: [
            { message_id: 1, role: 'user', author: 'h', content_preview: 'x', created_at: 't' },
            { message_id: 2, role: 'assistant', author: 'f', content_preview: 'y', created_at: 't' },
          ],
        },
        {
          round_id: 2,
          message_ids: [2, 3],
          messages: [
            { message_id: 2, role: 'user', author: 'h', content_preview: 'x', created_at: 't' },
            { message_id: 3, role: 'assistant', author: 'f', content_preview: 'y', created_at: 't' },
          ],
        },
      ],
      'round',
    ),
    null,
  );
  assert.ok(validateCanonicalRounds(ROUNDS, 'round'));
  assert.equal(validateCanonicalRounds(ROUNDS, null), null);
  assert.equal(validateCanonicalRounds(ROUNDS, undefined), null);

  assert.equal(parseSelectCarryoverResponse({ context_id: 1 }), null);
  assert.ok(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: 3,
      selected_round_count: 3,
      selected_message_count: 2,
      selected_message_ids: [1, 2],
      carryover_count: 3,
      finalized_at: 't',
    }),
  );
  // count mismatch
  assert.equal(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: 3,
      selected_round_count: 3,
      selected_message_count: 99,
      selected_message_ids: [1, 2],
      carryover_count: 3,
      finalized_at: 't',
    }),
    null,
  );
  // carryover_count must equal selected_round_count
  assert.equal(
    parseSelectCarryoverResponse({
      context_id: 1,
      context_epoch: 2,
      carryover_unit: 'round',
      requested_round_count: 3,
      selected_round_count: 3,
      selected_message_count: 2,
      selected_message_ids: [1, 2],
      carryover_count: 5,
      finalized_at: 't',
    }),
    null,
  );
}
ok('strict parsers');

// ── boundary both sides ──
{
  assert.equal(boundaryInsertIndex([10, 20, 30], 20), 2);
  assert.equal(boundaryInsertIndex([25, 30, 35], 20), null); // only new
  assert.equal(boundaryInsertIndex([5, 10], 20), null); // only old
  assert.equal(boundaryInsertIndex([10, 20, 30], 30), null); // boundary is last → no after
  assert.equal(boundaryInsertIndex([10, 20, 30], 5), null); // none ≤ boundary
}
ok('boundary both-sides');

// ── 404 hide card/modal/boundary ──
{
  const client = makeFakeClient({
    current: async () => {
      throw new HttpError(404, 'disabled', 'disabled');
    },
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'disabled', 'disabled');
  const snap = ctrl.getSnapshot();
  assert.equal(snap.showPickerCard, false);
  assert.equal(snap.showBoundary, false);
  assert.equal(snap.drawerOpen, false);
  assert.equal(snap.current, null);
  ctrl.dispose();
}
ok('404 hide');

// ── 423 = initial + 3 retries ──
{
  const client = makeFakeClient({
    current: async () => {
      throw new HttpError(423, 'deferred', 'deferred', { code: 'rollover_deferred' });
    },
  });
  const scheduler = makeScheduler();
  const ctrl = new DailySoftWindowController({ client, live: true, scheduler });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'deferred', 'deferred');
  assert.equal(client.calls.current, 1);
  for (let i = 0; i < DEFERRED_RETRY_MS.length; i++) {
    assert.equal(scheduler.pendingCount(), 1);
    await scheduler.flush();
    await waitMicrotasks(5);
  }
  assert.equal(client.calls.current, 1 + DEFERRED_RETRY_MS.length);
  assert.equal(scheduler.pendingCount(), 0);
  // focus can re-probe after exhaustion
  ctrl.onWindowFocus();
  await waitMicrotasks(5);
  assert.ok(client.calls.current >= 1 + DEFERRED_RETRY_MS.length + 1);
  ctrl.dispose();
}
ok('423 retries + focus');

// ── stale current does not overwrite new context ──
{
  const client = makeFakeClient();
  let resolveOld;
  let resolveNew;
  let phase = 0;
  client.setCurrent(async () => {
    phase += 1;
    if (phase === 1) {
      return new Promise((r) => {
        resolveOld = r;
      });
    }
    return new Promise((r) => {
      resolveNew = r;
    });
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start(); // gen1
  await waitMicrotasks(3);
  void ctrl.probeCurrent(); // gen2 aborts gen1 conceptually via probeGen
  await waitMicrotasks(3);
  // resolve stale first
  resolveOld(baseCurrent({ context_id: 1, context_epoch: 1 }));
  await waitMicrotasks(5);
  assert.equal(ctrl.current, null); // still probing / not applied stale
  resolveNew(baseCurrent({ context_id: 99, context_epoch: 7 }));
  await waitFor(() => ctrl.current?.context_id === 99, 'new context');
  assert.equal(ctrl.current.context_epoch, 7);
  ctrl.dispose();
}
ok('stale current');

// ── stale candidates do not write ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  let resolveCand;
  client.setCandidates(async () => {
    return new Promise((r) => {
      resolveCand = r;
    });
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  const opener = { focus() {}, isConnected: true };
  ctrl.openDrawer(opener);
  await waitMicrotasks(3);
  ctrl.closeDrawer(); // bumps candidatesGen + abort
  resolveCand({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: ROUNDS.length,
    rounds: ROUNDS,
    candidates: [],
  });
  await waitMicrotasks(5);
  assert.equal(ctrl.rounds.length, 0);
  assert.equal(ctrl.drawerOpen, false);
  ctrl.dispose();
}
ok('stale candidates');

// ── POST context mismatch → GET current, no lock ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  client.setSelect(async (count) => ({
    ok: true,
    context_id: 999,
    context_epoch: 1,
    carryover_unit: 'round',
    requested_round_count: count,
    selected_round_count: count,
    selected_message_count: 0,
    selected_message_ids: [],
    carryover_count: count,
    finalized_at: 't',
  }));
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  const beforeCurrentCalls = client.calls.current;
  await openDrawerWithCandidates(ctrl);
  ctrl.setDraftCount(3);
  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, false);
  assert.equal(ctrl.getSnapshot().locked, false);
  assert.equal(ctrl.drawerOpen, false);
  await waitFor(() => client.calls.current > beforeCurrentCalls, 'reload after mismatch');
  assert.equal(client.calls.select, 1);
  ctrl.dispose();
}
ok('POST context mismatch');

// ── malformed POST does not lock; GET current ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  client.setSelect(async () => {
    throw new HttpError(500, 'malformed select', 'malformed');
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  const before = client.calls.current;
  await openDrawerWithCandidates(ctrl);
  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, false);
  assert.equal(ctrl.getSnapshot().locked, false);
  await waitFor(() => client.calls.current > before, 'GET after malformed');
  ctrl.dispose();
}
ok('malformed POST');

// ── double click → 1 POST ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  let resolveSelect;
  client.setSelect(async (count) => {
    return new Promise((r) => {
      resolveSelect = () =>
        r({
          ok: true,
          context_id: 7,
          context_epoch: 42,
          carryover_unit: 'round',
          requested_round_count: count,
          selected_round_count: count,
          selected_message_count: 0,
          selected_message_ids: [],
          carryover_count: count,
          finalized_at: 't',
        });
    });
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  await openDrawerWithCandidates(ctrl);
  ctrl.setDraftCount(5);
  const p1 = ctrl.confirmSelection();
  const p2 = ctrl.confirmSelection();
  await waitMicrotasks(3);
  assert.equal(client.calls.select, 1);
  resolveSelect();
  await Promise.all([p1, p2]);
  assert.equal(client.calls.select, 1);
  ctrl.dispose();
}
ok('double click');

// ── 409 → 1 GET current, 0 POST retry ──
{
  const client = makeFakeClient();
  let curCalls = 0;
  client.setCurrent(async () => {
    curCalls += 1;
    if (curCalls === 1) return baseCurrent();
    return baseCurrent({ selection_finalized: true, requested_round_count: 5, selected_round_count: 2, carryover_count: 2, selected_message_ids: [1, 2] });
  });
  client.setSelect(async () => {
    throw new HttpError(409, 'locked', 'locked');
  });
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  await openDrawerWithCandidates(ctrl);
  await ctrl.confirmSelection();
  assert.equal(client.calls.select, 1);
  await waitFor(() => ctrl.getSnapshot().locked === true, 'locked via GET');
  assert.equal(client.calls.select, 1);
  ctrl.dispose();
}
ok('409 recover');

// ── send started: no select 0 ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  ctrl.notifySendStarted();
  assert.equal(client.calls.select, 0);
  assert.equal(ctrl.drawerOpen, false);
  assert.equal(ctrl.pickerSuppressed, true);
  ctrl.dispose();
}
ok('send no select 0');

// ── send settled refreshes current via probe ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  const before = client.calls.current;
  client.setCurrent(async () =>
    baseCurrent({
      selection_finalized: true,
      requested_round_count: 0,
      selected_round_count: 0,
      carryover_count: 0,
      selected_message_ids: [],
    }),
  );
  ctrl.notifySendSettled(true);
  await waitFor(() => ctrl.getSnapshot().locked === true, 'locked after send');
  assert.ok(client.calls.current > before);
  assert.equal(client.calls.select, 0);
  ctrl.dispose();
}
ok('send settled probe');

// ── dismiss / reconsider do not POST ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  const focusCalls = { n: 0 };
  const opener = {
    focus() {
      focusCalls.n += 1;
    },
    isConnected: true,
  };
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer(opener);
  ctrl.closeDrawer(); // × / overlay / 再想想 / Esc all go through closeDrawer
  assert.equal(client.calls.select, 0);
  assert.equal(focusCalls.n, 1);
  assert.equal(ctrl.opener, null);
  ctrl.dispose();
}
ok('dismiss no POST + focus return');

// ── cross-op: candidates A in flight, current B applies, A must not write ──
{
  const client = makeFakeClient();
  const ctxA = baseCurrent({ context_id: 1, context_epoch: 10, local_day: '2026-07-27' });
  const ctxB = baseCurrent({ context_id: 2, context_epoch: 20, local_day: '2026-07-28' });
  const roundsA = [
    {
      round_id: 101,
      message_ids: [101, 102],
      messages: [
        { message_id: 101, role: 'user', author: 'h', content_preview: 'A', created_at: 't' },
        { message_id: 102, role: 'assistant', author: 'f', content_preview: 'a', created_at: 't' },
      ],
    },
  ];
  const roundsB = [
    {
      round_id: 201,
      message_ids: [201, 202],
      messages: [
        { message_id: 201, role: 'user', author: 'h', content_preview: 'B', created_at: 't' },
        { message_id: 202, role: 'assistant', author: 'f', content_preview: 'b', created_at: 't' },
      ],
    },
  ];

  let resolveCandA;
  let currentCall = 0;
  client.setCurrent(async () => {
    currentCall += 1;
    if (currentCall === 1) return ctxA;
    return ctxB;
  });
  client.setCandidates(async () => {
    return new Promise((r) => {
      resolveCandA = () =>
        r({
          ok: true,
          context_id: 1,
          context_epoch: 10,
          carryover_unit: 'round',
          available_round_count: 1,
          rounds: roundsA,
          candidates: [],
        });
    });
  });

  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.current?.context_id === 1, 'context A');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  await waitMicrotasks(3);
  assert.equal(ctrl.drawerOpen, true);

  // Simulate focus re-probe applying context B while candidates A is in flight.
  void ctrl.probeCurrent();
  await waitFor(() => ctrl.current?.context_id === 2, 'context B');
  assert.equal(ctrl.drawerOpen, false, 'drawer closed when context changed');

  resolveCandA();
  await waitMicrotasks(8);

  assert.equal(ctrl.current?.context_id, 2);
  assert.equal(ctrl.rounds.length, 0, 'stale A rounds must not write under B');
  assert.ok(!ctrl.rounds.some((r) => r.round_id === 101));
  ctrl.dispose();
}
ok('cross candidates stale after context B');

// ── cross-op: select A in flight, current B applies, A must not mix/lock ──
{
  const client = makeFakeClient();
  const ctxA = baseCurrent({
    context_id: 1,
    context_epoch: 10,
    local_day: '2026-07-27',
    boundary_message_id: 50,
    handoff_status: 'ABSENT',
  });
  const ctxB = baseCurrent({
    context_id: 2,
    context_epoch: 20,
    local_day: '2026-07-28',
    boundary_message_id: 99,
    handoff_status: 'PENDING',
  });

  let resolveSelectA;
  let probeCount = 0;
  client.setCurrent(async () => {
    probeCount += 1;
    if (probeCount === 1) return ctxA;
    return ctxB;
  });
  client.setSelect(async (count) => {
    return new Promise((r) => {
      resolveSelectA = () =>
        r({
          ok: true,
          context_id: 1,
          context_epoch: 10,
          carryover_unit: 'round',
          requested_round_count: count,
          selected_round_count: count,
          selected_message_count: 2,
          selected_message_ids: [1, 2],
          carryover_count: count,
          finalized_at: 't',
        });
    });
  });
  client.setCandidates(async () => ({
    ok: true,
    context_id: 1,
    context_epoch: 10,
    carryover_unit: 'round',
    available_round_count: ROUNDS.length,
    rounds: ROUNDS,
    candidates: [],
  }));

  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  await openDrawerWithCandidates(ctrl);
  ctrl.setDraftCount(3);
  const selectPromise = ctrl.confirmSelection();
  await waitMicrotasks(3);

  // Current probe applies B while select A is still in flight.
  void ctrl.probeCurrent();
  await waitFor(() => ctrl.current?.context_id === 2, 'context B applied');

  resolveSelectA();
  const okSel = await selectPromise;
  assert.equal(okSel, false);
  assert.equal(ctrl.getSnapshot().locked, false);
  assert.equal(ctrl.current?.context_id, 2);
  assert.equal(ctrl.current?.local_day, '2026-07-28');
  assert.equal(ctrl.current?.boundary_message_id, 99);
  assert.equal(ctrl.current?.selection_finalized, false);
  ctrl.dispose();
}
ok('cross select stale after context B');

// ── focus same context: open drawer with loaded rounds must not silently clear ──
{
  const client = makeFakeClient();
  const ctx = baseCurrent();
  const loadedRounds = [
    {
      round_id: 11,
      message_ids: [11, 12],
      messages: [
        { message_id: 11, role: 'user', author: 'h', content_preview: 'u', created_at: 't' },
        { message_id: 12, role: 'assistant', author: 'f', content_preview: 'a', created_at: 't' },
      ],
    },
  ];
  client.setCurrent(async () => ctx);
  client.setCandidates(async () => ({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: 1,
    rounds: loadedRounds,
    candidates: [],
  }));

  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  await waitFor(() => ctrl.rounds.length === 1, 'rounds loaded');
  assert.equal(ctrl.drawerOpen, true);
  ctrl.setDraftCount(3);
  assert.equal(ctrl.draftCount, 3);

  const genBefore = ctrl.getSnapshot().contextGeneration;
  ctrl.onWindowFocus();
  await waitMicrotasks(8);

  assert.equal(ctrl.current?.context_id, 7);
  assert.equal(ctrl.getSnapshot().contextGeneration, genBefore, 'same context must not bump generation');
  assert.equal(ctrl.drawerOpen, true, 'drawer stays open on same-context focus');
  assert.equal(ctrl.rounds.length, 1, 'loaded rounds preserved');
  assert.equal(ctrl.rounds[0].round_id, 11);
  assert.equal(ctrl.draftCount, 3, 'user draft tier must survive same-context focus');

  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, true);
  assert.equal(client.calls.selectBodies.at(-1), 3, 'POST must use preserved draft count');
  ctrl.dispose();
}
ok('focus same context preserves drawer rounds and draft count');

// ── candidates-ready gating ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  let resolveCand;
  client.setCandidates(async () => new Promise((r) => { resolveCand = r; }));
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  assert.equal(ctrl.uiState, 'loading');
  assert.equal(ctrl.getSnapshot().candidatesReady, false);
  const immediate = await ctrl.confirmSelection();
  assert.equal(immediate, false);
  assert.equal(client.calls.select, 0);
  const dbl1 = ctrl.confirmSelection();
  const dbl2 = ctrl.confirmSelection();
  await Promise.all([dbl1, dbl2]);
  assert.equal(client.calls.select, 0);
  resolveCand({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: ROUNDS.length,
    rounds: ROUNDS,
    candidates: [],
  });
  await waitFor(() => ctrl.getSnapshot().candidatesReady === true, 'candidates ready');
  ctrl.setDraftCount(3);
  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, true);
  assert.equal(client.calls.select, 1);
  assert.equal(client.calls.selectBodies.at(-1), 3);
  ctrl.dispose();
}
ok('candidates-ready gating');

// ── empty candidates success allows confirm ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  client.setCandidates(async () => ({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: 0,
    rounds: [],
    candidates: [],
  }));
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  await waitFor(() => ctrl.uiState === 'empty', 'empty loaded');
  assert.equal(ctrl.getSnapshot().candidatesReady, true);
  ctrl.setDraftCount(0);
  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, true);
  assert.equal(client.calls.selectBodies.at(-1), 0);
  ctrl.dispose();
}
ok('empty candidates confirm');

// ── stale / closed candidates must not unlock confirm ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  let resolveLate;
  client.setCandidates(async () => new Promise((r) => { resolveLate = r; }));
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer({ focus() {}, isConnected: true });
  ctrl.closeDrawer();
  resolveLate({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: ROUNDS.length,
    rounds: ROUNDS,
    candidates: [],
  });
  await waitMicrotasks(8);
  assert.equal(ctrl.getSnapshot().candidatesReady, false);
  assert.equal(ctrl.rounds.length, 0);
  const okSel = await ctrl.confirmSelection();
  assert.equal(okSel, false);
  assert.equal(client.calls.select, 0);
  ctrl.dispose();
}
ok('late candidates no confirm');

// ── auth fail-hidden + focus return ──
{
  const client = makeFakeClient();
  client.setCurrent(async () => baseCurrent());
  client.setCandidates(async () => new Promise(() => {}));
  const focusCalls = { n: 0 };
  const opener = { focus() { focusCalls.n += 1; }, isConnected: true };
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer(opener);
  client.setCurrent(async () => {
    throw new HttpError(401, 'auth', 'auth');
  });
  await ctrl.probeCurrent();
  assert.equal(ctrl.uiState, 'unavailable');
  assert.equal(ctrl.current, null);
  assert.equal(ctrl.drawerOpen, false);
  assert.equal(ctrl.getSnapshot().showPickerCard, false);
  assert.equal(focusCalls.n, 1);
  assert.equal(ctrl.opener, null);
  ctrl.dispose();
}
ok('auth fail-hidden focus');

// ── passive external lock closes drawer + focus ──
{
  const client = makeFakeClient();
  const unlocked = baseCurrent();
  const locked = baseCurrent({
    selection_finalized: true,
    requested_round_count: 5,
    selected_round_count: 2,
    selected_message_count: 2,
    selected_message_ids: [1, 2],
    carryover_count: 2,
  });
  let phase = 0;
  client.setCurrent(async () => {
    phase += 1;
    return phase === 1 ? unlocked : locked;
  });
  client.setCandidates(async () => ({
    ok: true,
    context_id: 7,
    context_epoch: 42,
    carryover_unit: 'round',
    available_round_count: ROUNDS.length,
    rounds: ROUNDS,
    candidates: [],
  }));
  const focusCalls = { n: 0 };
  const opener = { focus() { focusCalls.n += 1; }, isConnected: true };
  const ctrl = new DailySoftWindowController({ client, live: true });
  ctrl.start();
  await waitFor(() => ctrl.uiState === 'ready', 'ready');
  ctrl.openDrawer(opener);
  await waitFor(() => ctrl.getSnapshot().candidatesReady, 'candidates ready');
  await ctrl.probeCurrent();
  assert.equal(ctrl.getSnapshot().locked, true);
  assert.equal(ctrl.drawerOpen, false);
  assert.equal(focusCalls.n, 1);
  assert.deepEqual(ctrl.current?.selected_message_ids, [1, 2]);
  ctrl.dispose();
}
ok('passive lock focus');

console.log(`daily-soft-window state-machine tests: ok (${passed} groups)`);
