import assert from 'node:assert/strict';
import { streamChatReply } from '../src/lib/chat.ts';

const seen = [];
const frames = [
  { t: 'text', d: 'short prefix' },
  {
    t: 'turn_reconcile',
    d: {
      content: '完整正文',
      thinking: 'thought',
      display_segments: [
        { type: 'thinking', text: 'thought' },
        { type: 'text', text: '完整正文' },
      ],
      tool_calls: [],
      canonical_sha256: 'canonical-hash',
    },
  },
  { t: 'done', ok: true, canonical_sha256: 'canonical-hash' },
];

globalThis.fetch = async () => {
  const body = frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
};

const result = await streamChatReply(
  9190,
  {
    onThink: () => {},
    onText: (value) => seen.push(['text', value]),
    onToolUse: () => {},
    onToolResult: () => {},
    onTurnReconcile: (projection) => seen.push(['reconcile', projection]),
  },
  new AbortController(),
);

assert.equal(result.ok, true);
assert.deepEqual(seen[0], ['text', 'short prefix']);
assert.equal(seen[1][0], 'reconcile');
assert.equal(seen[1][1].content, '完整正文');
assert.equal(result.canonicalSha256, 'canonical-hash');
console.log('chat terminal reconcile focused checks: PASS');
