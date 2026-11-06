import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  appendTextDelta,
  appendSelfStateDelta,
  appendThinkingDelta,
  applyToolResult,
  createLiveState,
  isTextCaretActive,
  replaceWithCanonicalProjection,
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
state = appendSelfStateDelta(state, 'self-state A');
state = appendTextDelta(state, 'text B');
state = upsertToolUse(state, 0, tool('tool-a'));
state = applyToolResult(state, 0, tool('tool-a', { result: 'done A', success: true }));
state = appendTextDelta(state, 'text C');
state = upsertToolUse(state, 1, tool('tool-b'));
state = applyToolResult(state, 1, tool('tool-b', { result: 'done B', success: true }));
state = appendTextDelta(state, 'text D');
assert.deepEqual(shape(state), [
  { type: 'thinking', text: 'thinking A' },
  { type: 'self_state', text: 'self-state A' },
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

let preview = appendTextDelta(createLiveState(), 'short prefix');
preview = upsertToolUse(preview, 0, tool('read', { result: 'done' }));
const reconciled = replaceWithCanonicalProjection(preview, {
  content: '完整正文',
  thinking: 'thought',
  display_segments: [
    { type: 'thinking', text: 'thought' },
    { type: 'self_state', text: 'self-state' },
    { type: 'text', text: '第一段' },
    { type: 'tool', tool_index: 0 },
    { type: 'text', text: '第二段' },
  ],
  tool_calls: [tool('read', { result: 'done', success: true })],
  canonical_sha256: 'hash',
});
assert.deepEqual(shape(reconciled), [
  { type: 'thinking', text: 'thought' },
  { type: 'self_state', text: 'self-state' },
  { type: 'text', text: '第一段' },
  { type: 'tool', idx: 0, name: 'read', running: false, result: 'done' },
  { type: 'text', text: '第二段' },
]);
assert.equal(reconciled.canonicalSha256, 'hash');

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


const chatScreen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const sendStart = chatScreen.indexOf('const send = useCallback(async () => {');
const sendEnd = chatScreen.indexOf('const sendChoice = useCallback', sendStart);
assert.ok(sendStart >= 0 && sendEnd > sendStart, 'ordinary send callback is present');
const sendBody = chatScreen.slice(sendStart, sendEnd);
const sendGuard = sendBody.indexOf('|| compressingImageCount > 0) return;');
const sendStartedAt = sendBody.indexOf('const sendStartedAt = performance.now();');
const sendPost = sendBody.indexOf('messageId = await sendChatMessage');
assert.ok(sendGuard >= 0 && sendStartedAt > sendGuard && sendStartedAt < sendPost);
assert.match(sendBody, /runStream\(messageId, \{ realityContext, birthStartedAt: sendStartedAt \}\)/);

const runStreamStart = chatScreen.indexOf('const runStream = useCallback');
const runStreamEnd = chatScreen.indexOf('const confirmDeferred = useCallback', runStreamStart);
const runStreamBody = chatScreen.slice(runStreamStart, runStreamEnd);
assert.match(runStreamBody, /birthStartedAt\?: number/);
assert.match(runStreamBody, /birthStartedAtRef\.current = opts\.birthStartedAt \?\? null/);
assert.match(runStreamBody, /\.finally\(\(\) => \{\s*setBirthNow\(null\);\s*\}\)/);
assert.match(chatScreen, /window\.setInterval\([\s\S]*?, 100\)/);
assert.match(chatScreen, /setBirthNow\(\(current\) => current === null \? null : performance\.now\(\)\)/, 'a late interval tick cannot revive a cleared no-segment timer');
assert.match(chatScreen, /return \(\) => window\.clearInterval\(intervalId\)/);
assert.match(chatScreen, /if \(hasLiveSegments\)[\s\S]*?birthStartedAtRef\.current = null/);

const renderLiveStart = chatScreen.indexOf('function renderLive(');
const renderLiveEnd = chatScreen.indexOf('// Modern: all loaded msgs.', renderLiveStart);
const renderLiveBody = chatScreen.slice(renderLiveStart, renderLiveEnd);
assert.match(renderLiveBody, /出生 \{Math\.max\(0, \(birthNow - birthStartedAtRef\.current\) \/ 1000\)\.toFixed\(1\)\}s/);
assert.ok(renderLiveBody.indexOf('renderMemoryRecall(l.memoryRecall') < renderLiveBody.indexOf('出生 {'));
assert.match(renderLiveBody, /birthStartedAtRef\.current !== null \?[\s\S]*?正在连接回复…/);

const sendChoiceStart = chatScreen.indexOf('const sendChoice = useCallback');
const sendChoiceEnd = chatScreen.indexOf('const chooseOption = useCallback', sendChoiceStart);
assert.doesNotMatch(chatScreen.slice(sendChoiceStart, sendChoiceEnd), /birthStartedAt/);
const confirmationStart = chatScreen.indexOf('const confirmDeferred = useCallback');
const confirmationEnd = chatScreen.indexOf('const send = useCallback', confirmationStart);
assert.doesNotMatch(chatScreen.slice(confirmationStart, confirmationEnd), /birthStartedAt/);
const redoStart = chatScreen.indexOf('const redo = useCallback');
const redoEnd = chatScreen.indexOf('const saveEdit = useCallback', redoStart);
assert.doesNotMatch(chatScreen.slice(redoStart, redoEnd), /birthStartedAt/);

console.log('chat birth timer wiring checks: PASS');
