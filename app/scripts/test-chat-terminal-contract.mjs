import assert from 'node:assert/strict';
import { streamChatReply } from '../src/lib/chat.ts';

const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const timers = [];
globalThis.setTimeout = (callback, delay) => {
  const timer = { callback, delay };
  timers.push(timer);
  return timer;
};
globalThis.clearTimeout = () => {};
globalThis.fetch = async () => new Response([
  'data: {"t":"ping"}\n\n',
  'data: {"t":"ping"}\n\n',
  'data: {"t":"text","d":"partial"}\n\n',
  'data: {"t":"err","d":"回复收尾异常，已保留收到的内容，可以继续发送。","code":"result_missing_after_end_turn"}\n\n',
].join(''), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });

try {
  const text = [];
  const result = await streamChatReply(8228, {
    onThink: () => {},
    onText: (value) => text.push(value),
    onToolUse: () => {},
    onToolResult: () => {},
  }, new AbortController());
  assert.equal(result.ok, false);
  assert.match(result.error, /回复收尾异常/);
  assert.deepEqual(text, ['partial']);
  assert.equal(timers.filter((timer) => timer.delay === 420000).length, 2);
  assert.ok(timers.filter((timer) => timer.delay === 150000).length >= 5);
} finally {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
}
console.log('chat terminal contract frontend checks passed: T1-T2, T6-T7');
