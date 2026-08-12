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

const warmSource = fs.readFileSync(new URL('../src/lib/chatWarmReturn.ts', import.meta.url), 'utf8');
const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const realNow = Date.now;
let now = 1_000_000;
Date.now = () => now;
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
  console.log('test:chat-warm-return — all checks passed');
} finally {
  Date.now = realNow;
  __resetChatWarmReturnForTests();
}
