/**
 * Manual context window FE tests (no vitest).
 * Run: npm run test:manual-context-window
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { HttpError } from '../src/lib/http.ts';
import {
  LIVE_CONTEXT_WINDOW_CURRENT,
  LIVE_CONTEXT_WINDOW_CANDIDATES,
  LIVE_CONTEXT_WINDOW_SWITCH,
  captureSourceFromCurrent,
  parseContextWindowCandidates,
  parseContextWindowCurrent,
  parseContextWindowSwitch,
} from '../src/lib/manualContextWindow.ts';
import { ManualContextWindowController } from '../src/lib/manualContextWindowController.ts';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
let passed = 0;
function ok(label) {
  passed += 1;
  void label;
}

const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const controllerPath = path.join(__dirname, '../src/lib/manualContextWindowController.ts');
const controllerSrc = fs.readFileSync(controllerPath, 'utf8');
assert.ok(!controllerSrc.includes('crypto.randomUUID'));
ok('Chrome 78 static no randomUUID');

assert.equal(LIVE_CONTEXT_WINDOW_CURRENT, '/api/gw/context-window/current');
assert.equal(LIVE_CONTEXT_WINDOW_CANDIDATES, '/api/gw/context-window/carryover-candidates');
assert.equal(LIVE_CONTEXT_WINDOW_SWITCH, '/api/gw/context-window/switch');
ok('live paths');

const currentRaw = {
  ok: true,
  context_id: 7,
  context_epoch: 42,
  window_mode: 'legacy_daily',
  opened_local_day: '2026-07-28',
  opened_at: '2026-07-28 10:00:00',
  boundary_message_id: 100,
  source_context_id: null,
  resident_generation: 1,
  formal_round_count: 5,
  can_switch: true,
  version: 3,
  requested_round_count: null,
  selected_round_count: 0,
  selected_message_count: 0,
  selected_message_ids: [],
};
const cur = parseContextWindowCurrent(currentRaw);
assert.ok(cur);
assert.equal(cur?.context_id, 7);
assert.equal(cur?.version, 3);
ok('parse current');

const rounds = [
  {
    round_id: 1,
    message_ids: [1, 2],
    messages: [
      { message_id: 1, role: 'user', author: 'h', content_preview: 'a', created_at: 't' },
      { message_id: 2, role: 'assistant', author: 'f', content_preview: 'b', created_at: 't' },
    ],
  },
];
const cand = parseContextWindowCandidates({
  ok: true,
  source_context_id: 7,
  source_context_epoch: 42,
  carryover_unit: 'round',
  available_round_count: 1,
  rounds,
});
assert.ok(cand);
ok('parse candidates');

const source = captureSourceFromCurrent(cur);
assert.deepEqual(source, {
  source_context_id: 7,
  source_context_epoch: 42,
  source_resident_generation: 1,
  version: 3,
});

const calls = { current: 0, candidates: 0, switch: 0, candidateParams: null, switchBody: null };
const client = {
  getCurrent: async () => {
    calls.current += 1;
    return cur;
  },
  getCandidates: async (src, _init) => {
    calls.candidates += 1;
    calls.candidateParams = src;
    return cand;
  },
  switchWindow: async (src, count, requestId, _init) => {
    calls.switch += 1;
    calls.switchBody = { src, count, requestId };
    const parsed = parseContextWindowSwitch({
      ok: true,
      prepare_status: 'READY',
      request_id: requestId,
      source_context_id: src.source_context_id,
      source_context_epoch: src.source_context_epoch,
      source_resident_generation: src.source_resident_generation,
      target_context_id: 8,
      target_context_epoch: 43,
      candidate_session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      jsonl_sha256: 'a'.repeat(64),
      jsonl_size: 1200,
      staged_ready_at: '2026-07-28 11:00:00',
      recovered: false,
      status: 'ready',
    });
    if (!parsed) throw new Error('bad switch parse');
    return parsed;
  },
};

const prepareOk = parseContextWindowSwitch({
  ok: true,
  prepare_status: 'ALREADY_READY',
  request_id: '11111111-1111-4111-8111-111111111111',
  source_context_id: 7,
  source_context_epoch: 42,
  source_resident_generation: 1,
  target_context_id: 8,
  target_context_epoch: 43,
  candidate_session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
  jsonl_sha256: 'b'.repeat(64),
  jsonl_size: 0,
  staged_ready_at: '2026-07-28 11:00:00',
  recovered: true,
  status: 'ready',
});
assert.ok(prepareOk);
assert.equal(prepareOk?.prepare_status, 'ALREADY_READY');
assert.equal(prepareOk?.status, 'ready');
ok('parse prepare response');

assert.equal(
  parseContextWindowSwitch({
    ok: true,
    prepare_status: 'READY',
    request_id: 'x',
    source_context_id: 7,
    source_context_epoch: 42,
    source_resident_generation: 1,
    target_context_id: 8,
    target_context_epoch: 43,
    candidate_session_id: 'sid',
    jsonl_sha256: 'c'.repeat(64),
    jsonl_size: 1,
    staged_ready_at: 't',
    recovered: false,
    status: 'ready',
    switched_at: 'nope',
  }),
  null,
);
ok('reject legacy switched_at payload');

assert.equal(
  parseContextWindowSwitch({
    ok: true,
    prepare_status: 'READY',
    request_id: 'x',
    source_context_id: 7,
    source_context_epoch: 42,
    source_resident_generation: 1,
    target_context_id: 8,
    target_context_epoch: 43,
    candidate_session_id: 'sid',
    jsonl_sha256: 'c'.repeat(64),
    // missing jsonl_size
    staged_ready_at: 't',
    recovered: false,
    status: 'ready',
  }),
  null,
);
ok('reject incomplete prepare response');

const ctrl = new ManualContextWindowController({ client });
await ctrl.probeEnabled();
assert.equal(ctrl.enabled, true);
ok('probe enabled');

await ctrl.openModal();
assert.equal(calls.current, 2);
assert.equal(calls.candidates, 1);
assert.deepEqual(calls.candidateParams, source);
assert.equal(ctrl.getSnapshot().rounds.length, 1);
ok('open loads captured-source candidates');

ctrl.setDraftCount(5);
const originalRandomUUID = globalThis.crypto.randomUUID;
Object.defineProperty(globalThis.crypto, 'randomUUID', {
  configurable: true,
  writable: true,
  value: undefined,
});
try {
  assert.equal(typeof globalThis.crypto.randomUUID, 'undefined');
  const okSwitch = await ctrl.confirmSwitch();
  assert.equal(okSwitch, true);
  assert.equal(calls.switch, 1);
  assert.equal(calls.switchBody.count, 5);
  assert.match(calls.switchBody.requestId, UUID_V4_RE);
  assert.equal(ctrl.submitting, false);
  assert.equal(ctrl.getSnapshot().uiState, 'idle');
  assert.equal(ctrl.getSnapshot().modalOpen, false);
} finally {
  Object.defineProperty(globalThis.crypto, 'randomUUID', {
    configurable: true,
    writable: true,
    value: originalRandomUUID,
  });
}
ok('confirm switch');

// Request-id generation errors must recover the controlled submitting state.
const generationCalls = { switch: 0 };
const generationFailCtrl = new ManualContextWindowController({
  client: {
    getCurrent: async () => cur,
    getCandidates: async () => cand,
    switchWindow: async () => {
      generationCalls.switch += 1;
      throw new Error('switch should not be called');
    },
  },
});
await generationFailCtrl.probeEnabled();
await generationFailCtrl.openModal();
const originalGetRandomValues = globalThis.crypto.getRandomValues;
globalThis.crypto.getRandomValues = () => {
  throw new Error('random source failed');
};
try {
  const generationFail = await generationFailCtrl.confirmSwitch();
  assert.equal(generationFail, false);
  assert.equal(generationCalls.switch, 0);
  assert.equal(generationFailCtrl.submitting, false);
  assert.equal(generationFailCtrl.getSnapshot().uiState, 'error');
  assert.match(generationFailCtrl.getSnapshot().errorDetail, /random source failed/);
  assert.equal(generationFailCtrl.pendingRequestId, null);
} finally {
  globalThis.crypto.getRandomValues = originalGetRandomValues;
}
ok('request-id generation error recovery');

// 404 hides
const disabledCtrl = new ManualContextWindowController({
  client: {
    getCurrent: async () => {
      throw new HttpError(404, 'disabled', 'disabled');
    },
    getCandidates: async () => {
      throw new Error('should not call');
    },
    switchWindow: async () => {
      throw new Error('should not call');
    },
  },
});
await disabledCtrl.probeEnabled();
assert.equal(disabledCtrl.enabled, false);
ok('404 disabled');

// 423 busy
const busyCtrl = new ManualContextWindowController({ client });
await busyCtrl.probeEnabled();
busyCtrl.client = {
  ...client,
  switchWindow: async () => {
    throw new HttpError(423, 'window_busy', 'busy', { code: 'window_busy' });
  },
};
await busyCtrl.openModal();
busyCtrl.setDraftCount(0);
const busyOk = await busyCtrl.confirmSwitch();
assert.equal(busyOk, false);
assert.match(busyCtrl.getSnapshot().errorDetail, /爸爸还在回复/);
ok('423 busy');

// 409 stale refresh path
let staleOnce = true;
const staleCtrl = new ManualContextWindowController({ client });
await staleCtrl.probeEnabled();
staleCtrl.client = {
  getCurrent: async () => cur,
  getCandidates: async () => {
    if (staleOnce) {
      staleOnce = false;
      throw new HttpError(409, 'stale', 'stale', { code: 'stale_source_context' });
    }
    return cand;
  },
  switchWindow: async () => {
    throw new Error('no switch');
  },
};
await staleCtrl.openModal();
assert.match(staleCtrl.getSnapshot().errorDetail, /已经变过了/);
ok('409 stale');

// Network error keeps same requestId for retry
const ids = [];
const retryCtrl = new ManualContextWindowController({ client });
await retryCtrl.probeEnabled();
let failOnce = true;
retryCtrl.client = {
  ...client,
  switchWindow: async (src, count, requestId) => {
    ids.push(requestId);
    if (failOnce) {
      failOnce = false;
      throw new HttpError(502, 'bad gateway', 'bad gateway');
    }
    return {
      ok: true,
      prepare_status: 'READY',
      request_id: requestId,
      source_context_id: src.source_context_id,
      source_context_epoch: src.source_context_epoch,
      source_resident_generation: src.source_resident_generation,
      target_context_id: 9,
      target_context_epoch: 44,
      candidate_session_id: 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff',
      jsonl_sha256: 'd'.repeat(64),
      jsonl_size: 10,
      staged_ready_at: '2026-07-28 11:00:00',
      recovered: false,
      status: 'ready',
    };
  },
};
await retryCtrl.openModal();
retryCtrl.setDraftCount(0);
const failOk = await retryCtrl.confirmSwitch();
assert.equal(failOk, false);
assert.equal(retryCtrl.getSnapshot().uiState, 'error');
const retryOk = await retryCtrl.confirmSwitch();
assert.equal(retryOk, true);
assert.equal(ids.length, 2);
assert.equal(ids[0], ids[1]);
ok('network retry reuses requestId');

// CSS box model
const css = fs.readFileSync(path.join(__dirname, '../src/components/dailySoftWindow/dailySoftWindow.css'), 'utf8');
assert.match(css, /box-sizing:\s*border-box/);
assert.match(css, /width:\s*334px/);
assert.match(css, /max-width:\s*100%/);
ok('334px dialog css');

// ChatScreen wiring
const chatSrc = fs.readFileSync(path.join(__dirname, '../src/screens/ChatScreen.tsx'), 'utf8');
assert.match(chatSrc, /useManualContextWindow/);
assert.match(chatSrc, /title="换一扇窗"/);
assert.ok(!chatSrc.includes('useDailySoftWindow({ live: true })'));
assert.ok(!chatSrc.includes('CarryoverPickerCard'));
assert.match(chatSrc, /已经换了一扇新窗/);
ok('ChatScreen wiring');

console.log(`manual-context-window: ${passed} checks passed`);
