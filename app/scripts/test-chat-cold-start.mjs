import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  bumpHistoryGenState,
  CHAT_AUTHORITATIVE_LIMIT,
  CHAT_LEGACY_INITIAL_LIMIT,
  CHAT_LEGACY_WARMUP_LIMIT,
  createColdStartRaceState,
  hasAuthoritativeCoverage,
  isHistoryGenCurrent,
  mergeOlderChatMessages,
  needsLegacyWarmUp,
  onAuthoritativeHistorySuccess,
  planWarmUpCommit,
  shouldMarkWarmUpSatisfiedAfterPage,
  tryConsumeDeferredInit,
} from '../src/lib/chatColdStart.ts';

const screenPath = new URL('../src/screens/ChatScreen.tsx', import.meta.url);
const apiPath = new URL('../src/lib/api.ts', import.meta.url);
const legacyPath = new URL('../src/lib/legacyTranscriptWindow.ts', import.meta.url);

const screen = fs.readFileSync(screenPath, 'utf8');
const api = fs.readFileSync(apiPath, 'utf8');
const legacy = fs.readFileSync(legacyPath, 'utf8');

assert.equal(CHAT_LEGACY_INITIAL_LIMIT, 24);
assert.equal(CHAT_AUTHORITATIVE_LIMIT, 80);
assert.equal(CHAT_LEGACY_WARMUP_LIMIT, 56);

// A. Legacy initial limit ~24, not 80
{
  const mountBlock = screen.slice(
    screen.indexOf('// Cold start: history first'),
    screen.indexOf('// media listeners'),
  );
  assert.match(mountBlock, /legacyCompat \? CHAT_LEGACY_INITIAL_LIMIT : CHAT_AUTHORITATIVE_LIMIT/);
  assert.doesNotMatch(mountBlock, /fetchChatMessages\(\{ limit: 80 \}\)/);
}

// B. Modern initial path still 80 via CHAT_AUTHORITATIVE_LIMIT
{
  const mountBlock = screen.slice(
    screen.indexOf('// Cold start: history first'),
    screen.indexOf('// media listeners'),
  );
  assert.match(mountBlock, /CHAT_AUTHORITATIVE_LIMIT/);
}

// C. Authoritative refetch still 80
{
  const refetchBlock = screen.slice(
    screen.indexOf('const refetchLatest = useCallback'),
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
  );
  assert.match(refetchBlock, /fetchChatMessages\(\{ limit: CHAT_AUTHORITATIVE_LIMIT \}\)/);
}

// D. load earlier still 80
{
  const loadBlock = screen.slice(
    screen.indexOf('const loadEarlier = useCallback'),
    screen.indexOf('const showEarlierLoaded = useCallback'),
  );
  assert.match(loadBlock, /limit: CHAT_AUTHORITATIVE_LIMIT/);
}

// E. Legacy warm-up only after first history + paint schedule
{
  const mountBlock = screen.slice(
    screen.indexOf('// Cold start: history first'),
    screen.indexOf('// media listeners'),
  );
  assert.match(mountBlock, /markChatColdStart\('initial_history_ready'\)/);
  assert.match(mountBlock, /scheduleAfterFirstPaint/);
  assert.match(mountBlock, /ensureDeferredColdStartInit/);
  assert.doesNotMatch(mountBlock, /runLegacyWarmUp[\s\S]*fetchChatMessages\(\{ limit/);
}

// F. Warm-up stale/race guard + dedupe
{
  const warmBlock = screen.slice(
    screen.indexOf('const runLegacyWarmUp = useCallback'),
    screen.indexOf('const ensureDeferredColdStartInit = useCallback'),
  );
  assert.match(warmBlock, /fetchChatMessagesOrNull/);
  assert.match(warmBlock, /if \(!warmPage\) return/);
  assert.match(warmBlock, /planWarmUpCommit/);
  assert.doesNotMatch(warmBlock, /let committed/);
  assert.doesNotMatch(warmBlock, /let mergedCount/);
  assert.match(warmBlock, /warmGen !== coldStartRaceRef\.current\.warmUpGen/);
  assert.match(warmBlock, /anchorGen !== coldStartRaceRef\.current\.historyGen/);
  assert.match(warmBlock, /mergeOlderChatMessages/);
  assert.match(warmBlock, /shouldMarkWarmUpSatisfiedAfterPage/);
  assert.match(warmBlock, /followLatestRef\.current/);
}

assert.deepEqual(
  mergeOlderChatMessages(
    [{ id: 25 }, { id: 26 }],
    [{ id: 23 }, { id: 24 }, { id: 25 }],
  ),
  [{ id: 23 }, { id: 24 }, { id: 25 }, { id: 26 }],
);
assert.deepEqual(
  mergeOlderChatMessages([{ id: 25 }], [{ id: 26 }]),
  [{ id: 25 }],
);

// G. Catalog no longer starts in same mount critical section as initial history
{
  const mountBlock = screen.slice(
    screen.indexOf('// Cold start: history first'),
    screen.indexOf('// media listeners'),
  );
  assert.doesNotMatch(mountBlock, /fetchModelCatalog/);
  assert.match(mountBlock, /ensureDeferredColdStartInit/);
  assert.match(screen, /ensureModelCatalog/);
  assert.match(api, /export function ensureModelCatalog/);
}

// H. Gateway / gen-lock first probe gated on initialHistoryReady
{
  const lockIdx = screen.indexOf('const pollLock = async () => {');
  const lockBlock = screen.slice(lockIdx - 120, lockIdx + 400);
  assert.match(lockBlock, /if \(!initialHistoryReady\) return/);

  const gwIdx = screen.indexOf('// gateway reachability');
  const gwBlock = screen.slice(gwIdx, gwIdx + 260);
  assert.match(gwBlock, /if \(!initialHistoryReady\) return/);
}

// I. legacyTranscriptWindow constants / mutation contracts untouched
{
  assert.match(legacy, /export const LEGACY_WINDOW_SIZE = 20/);
  assert.match(legacy, /export const LEGACY_WINDOW_STEP = 12/);
  const runStreamStart = screen.indexOf('const runStream = useCallback');
  const runStreamBody = screen.slice(runStreamStart, screen.indexOf('const send = useCallback', runStreamStart));
  assert.match(runStreamBody, /pinTranscriptToLatest\(\)/);
}

// J. loadEarlier discards stale responses
{
  const loadBlock = screen.slice(
    screen.indexOf('const loadEarlier = useCallback'),
    screen.indexOf('const showEarlierLoaded = useCallback'),
  );
  assert.match(loadBlock, /const gen = bumpHistoryGen\(\)/);
  assert.match(loadBlock, /if \(gen !== coldStartRaceRef\.current\.historyGen\) return/);
}

// K. superseded initial must not schedule deferred init; authoritative apply owns it
{
  const mountBlock = screen.slice(
    screen.indexOf('// Cold start: history first'),
    screen.indexOf('// media listeners'),
  );
  assert.match(mountBlock, /const superseded = gen !== coldStartRaceRef\.current\.historyGen/);
  assert.match(mountBlock, /if \(!superseded\)[\s\S]*scheduleAfterFirstPaint[\s\S]*ensureDeferredColdStartInit/);
  assert.doesNotMatch(mountBlock, /superseded \? CHAT_AUTHORITATIVE_LIMIT/);
  assert.doesNotMatch(mountBlock, /loadedCount:\s*superseded/);

  const refetchBlock = screen.slice(
    screen.indexOf('const refetchLatest = useCallback'),
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
  );
  assert.match(refetchBlock, /setMsgs\(page\.messages\)/);
  assert.match(refetchBlock, /ensureDeferredColdStartInit/);
}

// L. authoritative refetch does not reset warm-up satisfied coverage
{
  const refetchBlock = screen.slice(
    screen.indexOf('const refetchLatest = useCallback'),
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
  );
  assert.match(refetchBlock, /onAuthoritativeHistorySuccess/);
  assert.doesNotMatch(refetchBlock, /warmUpSatisfied = false|warmUpDoneRef\.current = false/);

  const flushBlock = screen.slice(
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
    screen.indexOf('const toggleModelPop = useCallback'),
  );
  assert.match(flushBlock, /hasAuthoritativeCoverage\(msgs\.length\)/);
}

// M. warm-up uses strict fetch that distinguishes failure from empty page
{
  assert.match(api, /export function fetchChatMessagesOrNull/);
  assert.match(api, /\.catch\(\(\) => null\)/);
}

// ── Executable race simulations (coordinator helpers) ──

// Blocker 1a: superseded initial must not fake-ready before authoritative history applied
{
  const race = createColdStartRaceState();
  bumpHistoryGenState(race); // initial gen=1
  bumpHistoryGenState(race); // authoritative requested gen=2
  assert.equal(race.deferredInitDone, false);
  assert.equal(tryConsumeDeferredInit(race), true); // authoritative history applied
  assert.equal(race.deferredInitDone, true);
}

// Blocker 2: stale loadEarlier response must not apply
{
  const race = createColdStartRaceState();
  const loadGen = bumpHistoryGenState(race);
  bumpHistoryGenState(race);
  assert.equal(isHistoryGenCurrent(race, loadGen), false);
}

// Blocker 3: authoritative 80 marks warm-up satisfied; search flush should skip
{
  const race = createColdStartRaceState();
  onAuthoritativeHistorySuccess(race, CHAT_AUTHORITATIVE_LIMIT);
  assert.equal(race.warmUpSatisfied, true);
  assert.equal(needsLegacyWarmUp(true, race, CHAT_AUTHORITATIVE_LIMIT), false);
  assert.equal(hasAuthoritativeCoverage(CHAT_AUTHORITATIVE_LIMIT), true);
}

// Warm-up transient failure must not mark satisfied
{
  const race = createColdStartRaceState();
  assert.equal(shouldMarkWarmUpSatisfiedAfterPage(24, true), false);
  assert.equal(race.warmUpSatisfied, false);
}

// Legitimate warm-up end-of-history marks satisfied
{
  assert.equal(shouldMarkWarmUpSatisfiedAfterPage(24, false), true);
  assert.equal(shouldMarkWarmUpSatisfiedAfterPage(CHAT_AUTHORITATIVE_LIMIT, true), true);
}

// N. warm-up commit planned before setMsgs — no updater side effects
{
  const plan = planWarmUpCommit(
    [{ id: 25 }, { id: 26 }],
    [{ id: 23 }, { id: 24 }],
  );
  assert.equal(plan.mergedCount, 4);
  assert.deepEqual(plan.merged, [{ id: 23 }, { id: 24 }, { id: 25 }, { id: 26 }]);
}

// O. cold-start lifecycle / unmount guard (StrictMode setup→cleanup→setup safe)
{
  assert.match(screen, /const mountedRef = useRef\(true\)/);

  const lifecycleBlock = screen.slice(
    screen.indexOf('// Cold-start lifecycle:'),
    screen.indexOf('const updateLive = useCallback'),
  );
  assert.match(lifecycleBlock, /mountedRef\.current = true/);
  assert.match(lifecycleBlock, /mountedRef\.current = false/);
  assert.match(lifecycleBlock, /cancelInFlightWarmUpState\(coldStartRaceRef\.current\)/);
  assert.match(lifecycleBlock, /bumpHistoryGenState\(coldStartRaceRef\.current\)/);
  assert.match(lifecycleBlock, /StrictMode/);

  const warmBlock = screen.slice(
    screen.indexOf('const runLegacyWarmUp = useCallback'),
    screen.indexOf('const ensureDeferredColdStartInit = useCallback'),
  );
  assert.match(warmBlock, /if \(!mountedRef\.current\) return/);
  assert.match(warmBlock, /setMsgs\([\s\S]*if \(!mountedRef\.current\) return latest/);

  const catalogBlock = screen.slice(
    screen.indexOf('const startModelCatalog = useCallback'),
    screen.indexOf('const runLegacyWarmUp = useCallback'),
  );
  assert.match(catalogBlock, /if \(!mountedRef\.current\) return[\s\S]*applyCatalog\(r\)/);

  const deferredBlock = screen.slice(
    screen.indexOf('const ensureDeferredColdStartInit = useCallback'),
    screen.indexOf('const refetchLatest = useCallback'),
  );
  assert.match(deferredBlock, /if \(!mountedRef\.current\) return/);

  const refetchBlock = screen.slice(
    screen.indexOf('const refetchLatest = useCallback'),
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
  );
  assert.match(refetchBlock, /if \(!mountedRef\.current\) return[\s\S]*setMsgs\(page\.messages\)/);
  assert.match(refetchBlock, /scheduleAfterFirstPaint\([\s\S]*if \(!mountedRef\.current\) return/);

  assert.equal(CHAT_LEGACY_INITIAL_LIMIT, 24);
  assert.equal(CHAT_LEGACY_WARMUP_LIMIT, 56);
  assert.equal(CHAT_AUTHORITATIVE_LIMIT, 80);
}

console.log('test:chat-cold-start — all checks passed');
