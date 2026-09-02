import assert from 'node:assert/strict';
import {
  appendTextDelta,
  appendThinkingDelta,
  applyToolResult,
  createLiveState,
  isTextCaretActive,
  upsertToolUse,
} from '../src/lib/chatLiveTimeline.ts';

const tool = (name, extra = {}) => ({ name, ...extra });
const shape = (state) => state.segments.map((segment) => (
  segment.type === 'tool'
    ? { type: segment.type, idx: segment.idx, name: segment.tool.name, running: segment.tool.running, result: segment.tool.result }
    : { type: segment.type, text: segment.text }
));

let state = createLiveState();
for (const delta of ['A', 'B', 'C', 'D', 'E']) state = appendTextDelta(state, delta);
assert.deepEqual(shape(state), [{ type: 'text', text: 'ABCDE' }]);
assert.equal(state.segments.length, 1);

state = appendTextDelta(createLiveState(), '第一段');
state = upsertToolUse(state, 0, tool('tool-a'));
state = appendTextDelta(state, '第二段');
assert.deepEqual(shape(state), [
  { type: 'text', text: '第一段' },
  { type: 'tool', idx: 0, name: 'tool-a', running: true, result: undefined },
  { type: 'text', text: '第二段' },
]);

state = appendThinkingDelta(createLiveState(), 'thinking A');
state = appendTextDelta(state, 'text B');
state = upsertToolUse(state, 0, tool('tool-a'));
state = applyToolResult(state, 0, tool('tool-a', { result: 'done A', success: true }));
state = appendTextDelta(state, 'text C');
state = upsertToolUse(state, 1, tool('tool-b'));
state = applyToolResult(state, 1, tool('tool-b', { result: 'done B', success: true }));
state = appendTextDelta(state, 'text D');
assert.deepEqual(shape(state), [
  { type: 'thinking', text: 'thinking A' },
  { type: 'text', text: 'text B' },
  { type: 'tool', idx: 0, name: 'tool-a', running: false, result: 'done A' },
  { type: 'text', text: 'text C' },
  { type: 'tool', idx: 1, name: 'tool-b', running: false, result: 'done B' },
  { type: 'text', text: 'text D' },
]);
assert.equal(state.segments.filter((segment) => segment.type === 'tool' && segment.idx === 0).length, 1);

state = appendThinkingDelta(state, 'thinking E');
assert.deepEqual(shape(state).slice(-2), [
  { type: 'text', text: 'text D' },
  { type: 'thinking', text: 'thinking E' },
]);

const before = shape(state);
assert.deepEqual(shape(applyToolResult(state, 99, tool('missing', { result: 'ignored' }))), before);

let caretState = appendTextDelta(createLiveState(), 'text');
assert.equal(isTextCaretActive(caretState), true);
caretState = upsertToolUse(caretState, 0, tool('tool-a'));
assert.equal(isTextCaretActive(caretState), false);
caretState = appendTextDelta(caretState, ' after tool');
assert.equal(isTextCaretActive(caretState), true);
caretState = applyToolResult(caretState, 0, tool('tool-a', { result: 'late result' }));
assert.equal(isTextCaretActive(caretState), false);
assert.equal(isTextCaretActive(createLiveState()), false);

console.log('chat live timeline focused checks: PASS');


const originalAt = Array.prototype.at;
try {
  delete Array.prototype.at;
  const chrome78State = appendTextDelta(createLiveState(), 'Chrome 78');
  assert.equal(isTextCaretActive(chrome78State), true);
} finally {
  if (typeof originalAt === 'function') Array.prototype.at = originalAt;
}
assert.equal(isTextCaretActive(createLiveState()), false);
