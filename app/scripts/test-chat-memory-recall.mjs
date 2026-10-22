import assert from 'node:assert/strict';
import { normalizeCacheInfo, normalizeMemoryRecall, streamChatReply } from '../src/lib/chat.ts';
import {
  appendTextDelta,
  appendThinkingDelta,
  createLiveState,
  replaceWithCanonicalProjection,
  setLiveMemoryRecall,
} from '../src/lib/chatLiveTimeline.ts';

const recallWire = {
  count: 2,
  items: [
    { type: 'MEMORY', date: '2026-06-27', preview: '凌晨聊赫泰拉' },
    { type: 'DIARY', date: '2026-06-26', preview: '钓鱼游戏' },
  ],
};

// normalizeMemoryRecall: wire shape in, nothing for empty or junk.
assert.deepEqual(normalizeMemoryRecall(recallWire), recallWire);
assert.equal(normalizeMemoryRecall(null), null);
assert.equal(normalizeMemoryRecall('x'), null);
assert.equal(normalizeMemoryRecall({ count: 0, items: [] }), null);
assert.deepEqual(
  normalizeMemoryRecall({ items: [{ type: 'MEMORY', preview: 'p' }, null, 3] }),
  { count: 1, items: [{ type: 'MEMORY', date: '', preview: 'p' }] },
);

// Persisted history: cache_info.memory_recall survives normalizeCacheInfo; absent stays absent.
assert.deepEqual(normalizeCacheInfo({ input_tokens: 5, memory_recall: recallWire }).memoryRecall, recallWire);
assert.equal(normalizeCacheInfo({ input_tokens: 5 }).memoryRecall, undefined);

// Live state: recall is not a segment, does not move lastEvent, and survives
// later deltas and the canonical reconcile.
let live = setLiveMemoryRecall(createLiveState(), recallWire);
assert.equal(live.segments.length, 0);
assert.equal(live.lastEvent, 'connecting');
live = appendThinkingDelta(live, 'thinking');
live = appendTextDelta(live, 'text');
assert.deepEqual(live.memoryRecall, recallWire);
live = replaceWithCanonicalProjection(live, {
  content: 'text',
  display_segments: [{ type: 'text', text: 'text' }],
  canonical_sha256: 'hash',
});
assert.deepEqual(live.memoryRecall, recallWire);

// SSE: memory_recall is dispatched ahead of thinking, and it is transport
// liveness only — never provider progress.
const originalFetch = globalThis.fetch;
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
const delays = [];
globalThis.setTimeout = (callback, delay) => {
  delays.push(delay);
  return { callback, delay };
};
globalThis.clearTimeout = () => {};
globalThis.fetch = async () => new Response([
  `data: ${JSON.stringify({ t: 'memory_recall', d: recallWire })}\n\n`,
  'data: {"t":"memory_recall","d":{"count":0,"items":[]}}\n\n',
  'data: {"t":"think","d":"hm"}\n\n',
  'data: {"t":"done","ok":true}\n\n',
].join(''), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });

try {
  const order = [];
  const recalls = [];
  const result = await streamChatReply(
    7,
    {
      onThink: () => order.push('think'),
      onText: () => order.push('text'),
      onToolUse: () => {},
      onToolResult: () => {},
      onMemoryRecall: (recall) => {
        order.push('recall');
        recalls.push(recall);
      },
    },
    new AbortController(),
  );
  assert.equal(result.ok, true);
  assert.deepEqual(order, ['recall', 'think']);
  assert.deepEqual(recalls, [recallWire]);
  assert.equal(delays.filter((d) => d === 420000).length, 2, 'provider watchdog: initial + think only');

  // A handler set without onMemoryRecall still streams normally.
  const plain = await streamChatReply(8, {
    onThink: () => {}, onText: () => {}, onToolUse: () => {}, onToolResult: () => {},
  }, new AbortController());
  assert.equal(plain.ok, true);
} finally {
  globalThis.fetch = originalFetch;
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
}

console.log('chat memory recall focused checks: PASS');
