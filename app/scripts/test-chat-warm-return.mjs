import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  __resetChatWarmReturnForTests,
  CHAT_WARM_RETURN_TTL_MS,
  clearChatWarmReturn,
  readChatWarmReturn,
  reconcileChatWarmReturn,
  writeChatWarmReturn,
} from '../src/lib/chatWarmReturn.ts';
import {
  CHAT_COMPOSER_DRAFT_STORAGE_KEY,
  CHAT_FOLLOW_LATEST_THRESHOLD_PX,
  clearChatComposerDraft,
  distanceFromBottom,
  followLatestFromGeometry,
  readChatComposerDraft,
  writeChatComposerDraft,
} from '../src/lib/chatNavigationState.ts';

const warmSource = fs.readFileSync(new URL('../src/lib/chatWarmReturn.ts', import.meta.url), 'utf8');
const navigationSource = fs.readFileSync(new URL('../src/lib/chatNavigationState.ts', import.meta.url), 'utf8');
const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const realNow = Date.now;
let now = 1_000_000;
Date.now = () => now;
const previousWindow = globalThis.window;
let storage = new Map();
function installStorage(overrides = {}) {
  globalThis.window = {
    sessionStorage: {
      getItem: (key) => storage.has(key) ? storage.get(key) : null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
      ...overrides,
    },
  };
}
const message = (id) => ({ id, role: 'assistant', content: String(id) });
const write = (messages, extra = {}) => writeChatWarmReturn({
  legacyCompat: false,
  messages,
  hasMoreBefore: false,
  txWin: { start: 0, end: messages.length },
  followLatest: true,
  scrollTop: 42,
  ...extra,
});

try {
  __resetChatWarmReturnForTests();
  const input = [message(1)];
  write(input);
  const first = readChatWarmReturn(false);
  assert.ok(first);
  assert.deepEqual(first.messages, input);
  assert.notEqual(first.messages, input);
  assert.notEqual(first.txWin, readChatWarmReturn(false).txWin);
  input.push(message(2));
  assert.deepEqual(readChatWarmReturn(false).messages.map((item) => item.id), [1]);

  now += CHAT_WARM_RETURN_TTL_MS - 1;
  assert.ok(readChatWarmReturn(false));
  now += 1;
  assert.equal(readChatWarmReturn(false), null);

  write([message(3)]);
  assert.equal(readChatWarmReturn(true), null);
  write([message(4)]);
  clearChatWarmReturn();
  assert.equal(readChatWarmReturn(false), null);
  write([]);
  assert.equal(readChatWarmReturn(false), null);

  assert.deepEqual(
    reconcileChatWarmReturn(
      { messages: [message(1), message(2)], hasMoreBefore: true },
      { messages: [message(2), message(3)], hasMoreBefore: false },
    ),
    { messages: [message(2), message(3)], hasMoreBefore: false },
  );

  const overlap = reconcileChatWarmReturn(
    { messages: [message(1), message(2), message(3)], hasMoreBefore: true },
    { messages: [message(2), message(3), message(4)], hasMoreBefore: true },
  );
  assert.deepEqual(overlap.messages.map((item) => item.id), [1, 2, 3, 4]);
  assert.equal(new Set(overlap.messages.map((item) => item.id)).size, overlap.messages.length);
  assert.equal(overlap.hasMoreBefore, true);

  assert.deepEqual(
    reconcileChatWarmReturn(
      { messages: [message(1), message(2)], hasMoreBefore: true },
      { messages: [message(8), message(9)], hasMoreBefore: true },
    ),
    { messages: [message(8), message(9)], hasMoreBefore: true },
  );

  for (const forbidden of [
    'localStorage', 'sessionStorage', 'indexedDB', 'caches.', 'CacheStorage',
    'fetch(', 'WebSocket', 'EventSource', 'structuredClone', '.at(', 'Object.hasOwn',
    'Promise.any', 'WeakRef', 'FinalizationRegistry', 'crypto.randomUUID',
  ]) {
    assert.equal(warmSource.includes(forbidden), false, forbidden);
  }
  assert.doesNotMatch(app, /KeepAlive|AliveScope|react-activation/);
  assert.match(app, /<Route path=\{ROUTES\.chat\} element=\{<ChatScreen \/>\} \/>/);
  assert.match(screen, /const \[warmSnapshot\] = useState\(\(\) => readChatWarmReturn\(legacyCompat\)\)/);
  assert.match(screen, /const \[msgs, setMsgs\] = useState<ChatMsg\[\]>\(\(\) => warmSnapshot \? warmSnapshot\.messages : \[\]\)/);
  assert.match(screen, /const \[initialHistoryReady, setInitialHistoryReady\] = useState\(\(\) => warmSnapshot !== null\)/);
  assert.match(screen, /const \[txWin, setTxWin\] = useState<TranscriptWindow>\(\(\) => warmSnapshot \? \{ \.\.\.warmSnapshot\.txWin \} : \{ start: 0, end: 0 \}\)/);
  assert.match(screen, /writeChatWarmReturn\(\{/);
  assert.match(screen, /msgsRef\.current/);
  assert.match(screen, /hasMoreBeforeRef\.current/);
  assert.match(screen, /txWinRef\.current/);
  assert.match(screen, /scheduleAfterFirstPaint\(\(\) => \{[\s\S]*revalidateWarmReturn/);
  const warmBlock = screen.slice(
    screen.indexOf('const revalidateWarmReturn = useCallback'),
    screen.indexOf('// Cold start: history first'),
  );
  assert.match(warmBlock, /const gen = bumpHistoryGen\(\)/);
  assert.match(warmBlock, /if \(gen !== coldStartRaceRef\.current\.historyGen\) return/);
  assert.match(warmBlock, /reconcileChatWarmReturn/);
  assert.doesNotMatch(warmBlock, /setMsgs\(\[\]\)/);

  assert.equal(CHAT_FOLLOW_LATEST_THRESHOLD_PX, 32);
  const geometry = (distance) => ({
    scrollHeight: 1000,
    scrollTop: 500 - distance,
    clientHeight: 500,
  });
  assert.equal(distanceFromBottom(geometry(0)), 0);
  assert.equal(followLatestFromGeometry(geometry(0)), true);
  assert.equal(followLatestFromGeometry(geometry(31)), true);
  assert.equal(followLatestFromGeometry(geometry(32)), true);
  assert.equal(followLatestFromGeometry(geometry(33)), false);
  assert.equal(followLatestFromGeometry(geometry(120)), false);
  assert.equal(distanceFromBottom({ scrollHeight: 1000, scrollTop: 505, clientHeight: 500 }), 0);
  assert.equal(followLatestFromGeometry(geometry(0), false), false);
  assert.equal(followLatestFromGeometry(geometry(31), false), false);
  assert.equal(followLatestFromGeometry(geometry(31), true), true);

  storage = new Map();
  installStorage();
  clearChatComposerDraft();
  assert.equal(readChatComposerDraft(), '');
  const exactDraft = '  leading\ntrailing  ';
  writeChatComposerDraft(exactDraft);
  assert.equal(readChatComposerDraft(), exactDraft);
  assert.equal(JSON.parse(storage.get(CHAT_COMPOSER_DRAFT_STORAGE_KEY)).text, exactDraft);
  writeChatComposerDraft('');
  assert.equal(storage.has(CHAT_COMPOSER_DRAFT_STORAGE_KEY), false);

  now = 2_000_000;
  writeChatComposerDraft('ttl');
  now += CHAT_WARM_RETURN_TTL_MS - 1;
  assert.equal(readChatComposerDraft(), 'ttl');
  now += 1;
  assert.equal(readChatComposerDraft(), '');
  assert.equal(storage.has(CHAT_COMPOSER_DRAFT_STORAGE_KEY), false);

  storage.set(CHAT_COMPOSER_DRAFT_STORAGE_KEY, '{bad json');
  assert.equal(readChatComposerDraft(), '');
  storage.set(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 2, text: 'bad', updatedAt: now }));
  assert.equal(readChatComposerDraft(), '');
  storage.set(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 1, text: 7, updatedAt: now }));
  assert.equal(readChatComposerDraft(), '');
  storage.set(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 1, text: 'bad', updatedAt: 'now' }));
  assert.equal(readChatComposerDraft(), '');
  storage.set(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 1, text: 'bad', updatedAt: Infinity }));
  assert.equal(readChatComposerDraft(), '');

  globalThis.window = { sessionStorage: { getItem: () => { throw new Error('get'); } } };
  assert.equal(readChatComposerDraft(), '');
  globalThis.window = { sessionStorage: { setItem: () => { throw new Error('set'); } } };
  writeChatComposerDraft('safe');
  globalThis.window = { sessionStorage: { removeItem: () => { throw new Error('remove'); } } };
  clearChatComposerDraft();

  assert.match(navigationSource, /CHAT_FOLLOW_LATEST_THRESHOLD_PX = 32/);
  assert.match(screen, /const \[input, setInput\] = useState\(readChatComposerDraft\)/);
  assert.match(screen, /const value = e\.target\.value/);
  assert.match(screen, /writeChatComposerDraft\(value\)/);
  assert.match(screen, /<div ref=\{scrollRef\} onScroll=\{handleTranscriptScroll\}/);
  const scrollHandler = screen.slice(
    screen.indexOf('const handleTranscriptScroll'),
    screen.indexOf('const placeholder', screen.indexOf('const handleTranscriptScroll')),
  );
  assert.match(scrollHandler, /followLatestRef\.current = followLatestFromGeometry/);
  assert.doesNotMatch(scrollHandler, /set[A-Z]|scrollBottom|setTimeout|requestAnimationFrame|fetch\(/);
  const sendSource = screen.slice(screen.indexOf('const send = useCallback'), screen.indexOf('const sendChoice'));
  assert.match(sendSource, /const rawText = input/);
  assert.match(sendSource, /const sendText = rawText\.trim\(\)/);
  assert.match(sendSource, /clearChatComposerDraft\(\)/);
  assert.match(sendSource, /writeChatComposerDraft\(rawText\)/);
  assert.equal((screen.match(/clearChatComposerDraft\(\)/g) || []).length, 1);
  assert.doesNotMatch(navigationSource, /localStorage|pendingImage|fetch\(|api\//);
  assert.doesNotMatch(navigationSource, /Object\.hasOwn|\.at\(|replaceAll\(|Promise\.any|structuredClone|crypto\.randomUUID|WeakRef|FinalizationRegistry/);

  console.log('test:chat-warm-return — all checks passed');
} finally {
  Date.now = realNow;
  __resetChatWarmReturnForTests();
  if (previousWindow === undefined) delete globalThis.window;
  else globalThis.window = previousWindow;
}
