import assert from 'node:assert/strict';
import { streamChatReply } from '../src/lib/chat.ts';

const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const safetyDelays = [];

globalThis.setTimeout = (callback, delay) => {
  safetyDelays.push({ callback, delay });
  return { callback, delay };
};
globalThis.clearTimeout = () => {};
globalThis.fetch = async () => {
  const body = [
    'data: {"t":"ping"}\n\n',
    'data: {"t":"ping"}\n\n',
    'data: {"t":"text","d":"hello"}\n\n',
    'data: {"t":"done","ok":true}\n\n',
  ].join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
};

try {
  const seen = {
    think: [],
    text: [],
    toolUse: [],
    toolResult: [],
    notices: [],
  };
  const result = await streamChatReply(
    42,
    {
      onThink: (value) => seen.think.push(value),
      onText: (value) => seen.text.push(value),
      onToolUse: (idx, value) => seen.toolUse.push([idx, value]),
      onToolResult: (idx, value) => seen.toolResult.push([idx, value]),
      onNotice: (value) => seen.notices.push(value),
    },
    new AbortController(),
  );

  assert.equal(result.ok, true);
  assert.deepEqual(seen.text, ['hello']);
  assert.deepEqual(seen.think, []);
  assert.deepEqual(seen.toolUse, []);
  assert.deepEqual(seen.toolResult, []);
  assert.deepEqual(seen.notices, []);
  assert.ok(
    safetyDelays.filter(({ delay }) => delay === 150000).length >= 4,
    'transport watchdog is re-armed by the initial stream and every SSE event',
  );
  assert.equal(
    safetyDelays.filter(({ delay }) => delay === 420000).length,
    2,
    'provider watchdog is armed initially and only by real provider text',
  );
} finally {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
}

console.log('chat SSE heartbeat focused tests passed: 1 case');
