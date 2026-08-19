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
  isChatFollowLatest,
  readChatComposerDraft,
  writeChatComposerDraft,
} from '../src/lib/chatNavigationState.ts';

const warmSource = fs.readFileSync(new URL('../src/lib/chatWarmReturn.ts', import.meta.url), 'utf8');
const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const navigationState = fs.readFileSync(new URL('../src/lib/chatNavigationState.ts', import.meta.url), 'utf8');
const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const realNow = Date.now;
const previousWindow = globalThis.window;
let now = 1_000_000;
Date.now = () => now;
const message = (id) => ({ id, role: 'assistant', content: String(id) });

function fakeStorage(initial = {}, throws = {}) {
  const data = { ...initial };
  return {
    getItem(key) { if (throws.get) throw new Error('get failed'); return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null; },
    setItem(key, value) { if (throws.set) throw new Error('set failed'); data[key] = String(value); },
    removeItem(key) { if (throws.remove) throw new Error('remove failed'); delete data[key]; },
  };
}
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
  assert.equal(isChatFollowLatest({ scrollHeight: 1000, scrollTop: 700, clientHeight: 300 }, false, true), true);
  assert.equal(isChatFollowLatest({ scrollHeight: 1000, scrollTop: 669, clientHeight: 300 }, false, true), true);
  assert.equal(isChatFollowLatest({ scrollHeight: 1000, scrollTop: 668, clientHeight: 300 }, false, true), true);
  assert.equal(isChatFollowLatest({ scrollHeight: 1000, scrollTop: 667, clientHeight: 300 }, false, true), false);
  assert.equal(isChatFollowLatest({ scrollHeight: 1200, scrollTop: 200, clientHeight: 300 }, false, true), false);
  assert.equal(isChatFollowLatest({ scrollHeight: 100, scrollTop: 0, clientHeight: 120 }, false, true), true);
  assert.equal(isChatFollowLatest({ scrollHeight: 100, scrollTop: 0, clientHeight: 100 }, true, false), false);
  assert.equal(isChatFollowLatest({ scrollHeight: 100, scrollTop: 0, clientHeight: 100 }, true, true), true);

  globalThis.window = { sessionStorage: fakeStorage() };
  now = 2_000_000;
  clearChatComposerDraft();
  assert.equal(readChatComposerDraft(), '');
  const exactDraft = '  leading\\ntrailing  ';
  writeChatComposerDraft(exactDraft);
  assert.equal(readChatComposerDraft(), exactDraft);
  now += CHAT_WARM_RETURN_TTL_MS - 1;
  assert.equal(readChatComposerDraft(), exactDraft);
  now += 1;
  assert.equal(readChatComposerDraft(), '');
  writeChatComposerDraft('text');
  writeChatComposerDraft('');
  assert.equal(readChatComposerDraft(), '');
  window.sessionStorage.setItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY, '{bad json');
  assert.equal(readChatComposerDraft(), '');
  window.sessionStorage.setItem(CHAT_COMPOSER_DRAFT_STORAGE_KEY, JSON.stringify({ version: 2, text: 'old', updatedAt: now }));
  assert.equal(readChatComposerDraft(), '');
  globalThis.window = { sessionStorage: fakeStorage({}, { get: true, set: true, remove: true }) };
  assert.doesNotThrow(() => readChatComposerDraft());
  assert.doesNotThrow(() => writeChatComposerDraft('safe'));
  assert.doesNotThrow(() => clearChatComposerDraft());

  assert.match(navigationState, /window\\.sessionStorage/);
  assert.doesNotMatch(navigationState, /localStorage|indexedDB|CacheStorage|fetch\\(/);
  assert.match(screen, /const \\[input, setInput\\] = useState\\(\\(\\) => readChatComposerDraft\\(\\)\\)/);
  assert.match(screen, /writeChatComposerDraft\\(value\\)/);
  assert.match(screen, /clearChatComposerDraft\\(\\)/);
  assert.doesNotMatch(screen, /localStorage/);
  assert.match(screen, /onScroll=\\{updateFollowLatestFromScroll\\}/);
  const scrollHandler = screen.slice(screen.indexOf('const updateFollowLatestFromScroll'), screen.indexOf('const placeholder'));
  assert.match(scrollHandler, /scrollHeight|scrollTop|clientHeight/);
  assert.match(scrollHandler, /txWinRef\\.current/);
  assert.match(scrollHandler, /msgsRef\\.current/);
  assert.match(scrollHandler, /isTranscriptWindowAtLatest/);
  assert.match(scrollHandler, /followLatestRef\\.current/);
  assert.doesNotMatch(scrollHandler, /setState|setTxWin|setMsgs/);

  console.log('test:chat-warm-return — all checks passed');
} finally {
  Date.now = realNow;
  __resetChatWarmReturnForTests();
  if (previousWindow === undefined) delete globalThis.window;
  else globalThis.window = previousWindow;
}
