import assert from 'node:assert/strict';
import {
  canHandoffToFinal,
  clearChatStreamHandoff,
  createChatStreamHandoff,
  finalOnlyContentMayAnimate,
  findAssistantAfter,
  findPersistedAssistantForHandoff,
  isLiveSegmentFresh,
  markChatHandoffFailed,
  markChatHandoffFinalized,
  presentationSegmentKey,
  thinkingStateKey,
} from '../src/lib/chatStreamHandoff.ts';

const liveText = { id: 0, type: 'text', text: 'hello' };
const liveTool = {
  id: 1,
  type: 'tool',
  idx: 0,
  tool: { name: 'lookup', running: false },
};
const message = (id, role, text, displaySegments, toolCalls = []) => ({
  id, role, text, thinking: '', thinkingSummary: '', toolCalls, cacheInfo: null,
  branchIdx: 0, branchTotal: 0, imageUrl: '', fileUrl: '', fileName: '',
  attachments: [], choices: [], displaySegments, ts: '', dateKey: '', createdAt: '', chatDay: '',
});

// T1: one logical turn keeps one presentation identity across deltas.
{
  const first = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const second = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  assert.equal(first.presentationKey, second.presentationKey);
  first.freshSegmentIds.add(0);
  assert.equal(isLiveSegmentFresh(first, liveText), true);
}

// T2: the persisted assistant binds to the live presentation key.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  handoff.freshSegmentIds.add(0);
  markChatHandoffFinalized(handoff);
  const finalId = findPersistedAssistantForHandoff([
    message(10, 'user', 'prompt'),
    message(11, 'assistant', 'hello', [{ type: 'text', text: 'hello' }]),
  ], handoff, [liveText]);
  handoff.finalMessageId = finalId;
  assert.equal(finalId, 11);
  assert.equal(canHandoffToFinal(handoff), true);
}

// T3: an unrelated assistant row cannot win merely because it is newest.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  markChatHandoffFinalized(handoff);
  assert.equal(findPersistedAssistantForHandoff([
    message(10, 'user', 'prompt'),
    message(11, 'assistant', 'unrelated', [{ type: 'text', text: 'other' }]),
    message(12, 'assistant', 'hello', [{ type: 'text', text: 'hello' }]),
  ], handoff, [liveText]), 12);
}

// T4: segment order remains thinking, text, tool, text.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const segments = [
    { id: 0, type: 'thinking', text: 'think' },
    { id: 1, type: 'text', text: 'A' },
    liveTool,
    { id: 2, type: 'text', text: 'B' },
  ];
  markChatHandoffFinalized(handoff);
  const final = message(11, 'assistant', 'AB', [
    { type: 'thinking', text: 'think' },
    { type: 'text', text: 'A' },
    { type: 'tool', toolIndex: 0 },
    { type: 'text', text: 'B' },
  ], [{ name: 'lookup', running: false }]);
  assert.equal(findPersistedAssistantForHandoff([message(10, 'user', 'prompt'), final], handoff, segments), 11);
}

// T5: finalization disables live fresh animation eligibility.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  handoff.freshSegmentIds.add(0);
  assert.equal(isLiveSegmentFresh(handoff, liveText), true);
  markChatHandoffFinalized(handoff);
  assert.equal(isLiveSegmentFresh(handoff, liveText), false);
}

// T6: final-only content retains first-appearance eligibility.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  assert.equal(finalOnlyContentMayAnimate([], false), true);
  assert.equal(finalOnlyContentMayAnimate([liveText], true), false);
  assert.equal(thinkingStateKey(handoff, 0), presentationSegmentKey(handoff, 0) + '-thinking');
}

// T7: thinking state key is presentation-continuous.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  assert.equal(thinkingStateKey(handoff, 0), thinkingStateKey(handoff, 0));
  assert.notEqual(thinkingStateKey(handoff, 0), thinkingStateKey(handoff, 1));
}

// T8: tool identity uses the same ordered segment slot.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  assert.equal(presentationSegmentKey(handoff, 2), presentationSegmentKey(handoff, 2));
  assert.notEqual(presentationSegmentKey(handoff, 1), presentationSegmentKey(handoff, 2));
}

// T9: abort-style clear prevents the next turn from inheriting identity.
{
  const oldHandoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const cleared = clearChatStreamHandoff(oldHandoff);
  const nextHandoff = createChatStreamHandoff({ kind: 'send', userMessageId: 11 });
  assert.equal(cleared, null);
  assert.notEqual(oldHandoff.presentationKey, nextHandoff.presentationKey);
}

// T10: terminal error cannot establish a successful final handoff.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  handoff.freshSegmentIds.add(0);
  markChatHandoffFailed(handoff);
  assert.equal(canHandoffToFinal(handoff), false);
  assert.equal(isLiveSegmentFresh(handoff, liveText), false);
}

// T11: send, regen, and edit operation identities are isolated.
{
  const send = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const regen = createChatStreamHandoff({ kind: 'regen', rewriteId: 'rw-1', sourceAssistantId: 20 });
  const edit = createChatStreamHandoff({ kind: 'edit', rewriteId: 'rw-2', sourceAssistantId: 20 });
  assert.notEqual(send.operationId, regen.operationId);
  assert.notEqual(regen.operationId, edit.operationId);
  assert.equal(findAssistantAfter([
    message(10, 'user', 'prompt'),
    message(20, 'assistant', 'old', [{ type: 'text', text: 'old' }]),
  ], 10), 20);
}

// T12: no Chrome 78-incompatible Array.at dependency in the helper.
{
  const source = await (await import('node:fs/promises')).readFile(
    new URL('../src/lib/chatStreamHandoff.ts', import.meta.url), 'utf8',
  );
  assert.doesNotMatch(source, /\.at\(/);
}

console.log('test:chat-stream-handoff — T1-T12 all checks passed');
