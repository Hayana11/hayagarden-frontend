import assert from 'node:assert/strict';
import { parseDisplaySegments, rowToMsg } from '../src/lib/chat.ts';

const ordered = [
  { type: 'thinking', text: 'think A' },
  { type: 'text', text: 'text B' },
  { type: 'tool', tool_index: 0 },
  { type: 'text', text: 'text C' },
  { type: 'tool', tool_index: 1 },
  { type: 'text', text: 'text D' },
];
assert.deepEqual(parseDisplaySegments(JSON.stringify(ordered), 2), [
  { type: 'thinking', text: 'think A' },
  { type: 'text', text: 'text B' },
  { type: 'tool', toolIndex: 0 },
  { type: 'text', text: 'text C' },
  { type: 'tool', toolIndex: 1 },
  { type: 'text', text: 'text D' },
]);

const msg = rowToMsg({
  id: 42,
  author: 'assistant',
  content: 'legacy fallback text',
  thinking: 'legacy fallback thinking',
  display_segments: JSON.stringify(ordered),
  tool_calls: JSON.stringify([{ name: 'tool-a' }, { name: 'tool-b' }]),
});
assert.deepEqual(msg.displaySegments?.map((segment) => segment.type), [
  'thinking', 'text', 'tool', 'text', 'tool', 'text',
]);
assert.deepEqual(msg.displaySegments?.filter((segment) => segment.type === 'tool'), [
  { type: 'tool', toolIndex: 0 },
  { type: 'tool', toolIndex: 1 },
]);

for (const raw of ['', 'not json', '{}', '[]', '[{"type":"unknown","text":"x"}]',
  '[{"type":"tool","tool_index":-1}]', '[{"type":"tool","tool_index":"0"}]',
  '[{"type":"text","text":3}]']) {
  assert.equal(parseDisplaySegments(raw), undefined, raw);
}

assert.equal(parseDisplaySegments(JSON.stringify([{ type: 'tool', tool_index: 99 }]), 1), undefined);
const invalidRow = rowToMsg({
  id: 44,
  author: 'assistant',
  content: 'legacy tool fallback',
  display_segments: JSON.stringify([{ type: 'tool', tool_index: 99 }]),
  tool_calls: JSON.stringify([{ name: 'tool-a' }]),
});
assert.equal(invalidRow.displaySegments, undefined);

const originalAt = Array.prototype.at;
try {
  Object.defineProperty(Array.prototype, 'at', { configurable: true, value: undefined });
  assert.equal(rowToMsg({ id: 43, author: 'assistant', content: 'legacy' }).displaySegments, undefined);
  assert.deepEqual(parseDisplaySegments(JSON.stringify([{ type: 'text', text: 'ok' }])), [
    { type: 'text', text: 'ok' },
  ]);
} finally {
  Object.defineProperty(Array.prototype, 'at', { configurable: true, value: originalAt });
}

const screen = await (await import('node:fs/promises')).readFile(
  new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8',
);
assert.match(screen, /renderOrderedAssistantContent/);
assert.ok(screen.includes('renderOrderedAssistantContent(m) ?? renderLegacyAssistantContent(m)'));
assert.match(screen, /renderToolItems/);
assert.match(screen, /renderLegacyAssistantContent/);
assert.ok(screen.includes('segment.toolIndex >= m.toolCalls.length'));
assert.match(screen, /clearLivePresentation/);
assert.match(screen, /onThink:/);
assert.match(screen, /onText:/);
assert.match(screen, /onToolUse:/);
assert.match(screen, /onToolResult:/);
assert.ok(screen.includes('await refetchLatest();\n    clearLivePresentation();'));
console.log('chat durable-order contract tests: PASS');
