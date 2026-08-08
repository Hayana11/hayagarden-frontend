import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  CHAT_AUTHORITATIVE_LIMIT,
  CHAT_LEGACY_INITIAL_LIMIT,
  CHAT_LEGACY_WARMUP_LIMIT,
  mergeOlderChatMessages,
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
    screen.indexOf('const refreshChat = useCallback'),
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
  assert.match(mountBlock, /if \(legacyCompat\)[\s\S]*runLegacyWarmUp/);
  assert.doesNotMatch(mountBlock, /runLegacyWarmUp[\s\S]*fetchChatMessages\(\{ limit/);
}

// F. Warm-up stale/race guard + dedupe
{
  const warmBlock = screen.slice(
    screen.indexOf('const runLegacyWarmUp = useCallback'),
    screen.indexOf('const flushLegacyWarmUp = useCallback'),
  );
  assert.match(warmBlock, /warmGen !== warmUpGenRef\.current/);
  assert.match(warmBlock, /anchorGen !== historyGenRef\.current/);
  assert.match(warmBlock, /mergeOlderChatMessages/);
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
  assert.match(mountBlock, /startModelCatalog/);
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

console.log('test:chat-cold-start — all checks passed');
