import assert from 'node:assert/strict';
import {
  buildChatPresentationEntries,
  commitChatPresentationBinding,
  canHandoffToFinal,
  clearChatStreamHandoff,
  createChatStreamHandoff,
  findAssistantAfter,
  findPersistedAssistantForHandoff,
  isLiveSegmentFresh,
  markChatHandoffFailed,
  markChatHandoffFinalized,
  presentationSegmentKey,
  pruneChatPresentationBindings,
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
  assert.equal(commitChatPresentationBinding(handoff, 11, new Map()), true);
  const finalId = findPersistedAssistantForHandoff([
    message(10, 'user', 'prompt'),
    message(11, 'assistant', 'hello', [{ type: 'text', text: 'hello' }]),
  ], handoff);
  handoff.finalMessageId = finalId;
  assert.equal(finalId, 11);
  assert.equal(canHandoffToFinal(handoff), true);
}

// T3: an unrelated assistant row cannot win merely because it is newest.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  markChatHandoffFinalized(handoff);
  assert.equal(commitChatPresentationBinding(handoff, 12, new Map()), true);
  assert.equal(findPersistedAssistantForHandoff([
    message(10, 'user', 'prompt'),
    message(11, 'assistant', 'unrelated', [{ type: 'text', text: 'other' }]),
    message(12, 'assistant', 'hello', [{ type: 'text', text: 'hello' }]),
  ], handoff), 12);
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
  assert.equal(commitChatPresentationBinding(handoff, 11, new Map()), true);
  const final = message(11, 'assistant', 'AB', [
    { type: 'thinking', text: 'think' },
    { type: 'text', text: 'A' },
    { type: 'tool', toolIndex: 0 },
    { type: 'text', text: 'B' },
  ], [{ name: 'lookup', running: false }]);
  assert.equal(findPersistedAssistantForHandoff([message(10, 'user', 'prompt'), final], handoff), 11);
}

// T5: finalization disables live fresh animation eligibility.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  handoff.freshSegmentIds.add(0);
  assert.equal(isLiveSegmentFresh(handoff, liveText), true);
  markChatHandoffFinalized(handoff);
  assert.equal(isLiveSegmentFresh(handoff, liveText), false);
}

// T6: final-only content remains renderable without a live segment.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const entries = buildChatPresentationEntries({
    messages: [message(11, 'assistant', 'final-only', [{ type: 'text', text: 'final-only' }])],
    handoff,
    finalMessageId: 11,
    liveVisible: false,
  });
  assert.equal(entries.some((entry) => entry.kind === 'message' && entry.message.id === 11), true);
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

// T13: normal live and final payloads occupy one unified keyed sibling slot.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const liveEntries = buildChatPresentationEntries({
    messages: [message(10, 'user', 'prompt')],
    handoff,
    finalMessageId: null,
    liveVisible: true,
  });
  const finalEntries = buildChatPresentationEntries({
    messages: [
      message(10, 'user', 'prompt'),
      message(11, 'assistant', 'hello', [{ type: 'text', text: 'hello' }]),
    ],
    handoff,
    finalMessageId: 11,
    liveVisible: false,
  });
  const liveSlot = liveEntries.find((entry) => entry.kind === 'live');
  const finalSlot = finalEntries.find((entry) => entry.kind === 'message' && entry.message.id === 11);
  assert.equal(liveSlot?.key, handoff.presentationKey);
  assert.equal(finalSlot?.key, handoff.presentationKey);
  assert.equal(liveSlot?.key, finalSlot?.key);
}

// T14: canonical final text may differ from live markers while authoritative ID binds.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  markChatHandoffFinalized(handoff);
  assert.equal(commitChatPresentationBinding(handoff, 12, new Map()), true);
  const live = [{ id: 0, type: 'text', text: '正文[[SAVE: x]]' }];
  const final = message(12, 'assistant', '正文', [{ type: 'text', text: '正文' }]);
  assert.equal(findPersistedAssistantForHandoff([final], handoff), 12);
  assert.notEqual(live[0].text, final.text);
}

// T15: authoritative ID isolates the current Chat final from an unrelated assistant row.
{
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  markChatHandoffFinalized(handoff);
  assert.equal(commitChatPresentationBinding(handoff, 42, new Map()), true);
  const background = message(41, 'assistant', 'workspace update', [{ type: 'text', text: 'workspace update' }]);
  const current = message(42, 'assistant', 'current reply', [{ type: 'text', text: 'current reply' }]);
  assert.equal(findPersistedAssistantForHandoff([background, current], handoff), 42);
  assert.notEqual(findPersistedAssistantForHandoff([background, current], handoff), background.id);
}

// T16: production ChatScreen consumes the unified presentation-entry collection.
{
  const source = await (await import('node:fs/promises')).readFile(
    new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8',
  );
  assert.match(source, /buildChatPresentationEntries\(\{/);
  assert.match(source, /const rendered = presentationEntries\.map/);
  assert.doesNotMatch(source, /\{rendered\}[\\s\\S]*renderLive\(activeLive, activeHandoff\)/);
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const before = buildChatPresentationEntries({
    messages: [message(10, 'user', 'prompt')],
    handoff,
    finalMessageId: null,
    liveVisible: true,
  });
  const after = buildChatPresentationEntries({
    messages: [message(10, 'user', 'prompt'), message(11, 'assistant', 'canonical', [])],
    handoff,
    finalMessageId: 11,
    liveVisible: false,
  });
  assert.equal(
    before.find((entry) => entry.kind === 'live')?.key,
    after.find((entry) => entry.kind === 'message' && entry.message.role === 'assistant')?.key,
  );
}


// T17: authoritative presentation binding survives the next turn and a refetch.
{
  const presentationKeys = new Map();
  const first = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  const second = createChatStreamHandoff({ kind: 'send', userMessageId: 12 });
  markChatHandoffFinalized(first);
  assert.equal(commitChatPresentationBinding(first, 11, presentationKeys), true);
  pruneChatPresentationBindings(presentationKeys, [
    message(11, 'assistant', 'first reply', [{ type: 'text', text: 'first reply' }]),
  ]);
  assert.equal(presentationKeys.get(11), first.presentationKey);
  assert.notEqual(first.presentationKey, second.presentationKey);
}

// T18: refetch pruning removes stale bindings but retains every current message ID.
{
  const presentationKeys = new Map([
    [11, 'presentation-11'],
    [12, 'presentation-12'],
    [999, 'stale-presentation'],
  ]);
  pruneChatPresentationBindings(presentationKeys, [
    message(11, 'assistant', 'reply 11', []),
    message(12, 'assistant', 'reply 12', []),
  ]);
  assert.equal(presentationKeys.get(11), 'presentation-11');
  assert.equal(presentationKeys.get(12), 'presentation-12');
  assert.equal(presentationKeys.has(999), false);
  assert.equal(presentationKeys.size, 2);
}

// T19: an authoritative ID may arrive before its row; live stays visible until the row mounts.
{
  const presentationKeys = new Map();
  const handoff = createChatStreamHandoff({ kind: 'send', userMessageId: 10 });
  markChatHandoffFinalized(handoff);
  assert.equal(commitChatPresentationBinding(handoff, 11, presentationKeys), true);
  const beforeRow = buildChatPresentationEntries({
    messages: [message(10, 'user', 'prompt')],
    handoff,
    finalMessageId: handoff.finalMessageId,
    liveVisible: true,
    presentationKeys,
  });
  assert.equal(beforeRow.some((entry) => entry.kind === 'live'), true);
  const withRow = buildChatPresentationEntries({
    messages: [
      message(10, 'user', 'prompt'),
      message(11, 'assistant', 'reply', [{ type: 'text', text: 'reply' }]),
    ],
    handoff,
    finalMessageId: handoff.finalMessageId,
    liveVisible: false,
    presentationKeys,
  });
  assert.equal(withRow.some((entry) => entry.kind === 'live'), false);
  assert.equal(
    withRow.find((entry) => entry.kind === 'message' && entry.message.id === 11)?.key,
    handoff.presentationKey,
  );
}

// T20: live and final render through the same immediate .chat-msg shell.
{
  const source = await (await import('node:fs/promises')).readFile(
    new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8',
  );
  const liveSource = source.slice(source.indexOf('function renderLive'), source.indexOf('\n  // Modern:', source.indexOf('function renderLive')));
  assert.match(liveSource, /return \(\s*<div className="chat-msg/);
  assert.doesNotMatch(liveSource, /<div key=\{presentationKey\}>/);
  assert.match(source, /const knownFinalMessageId/);
  assert.match(source, /const mountedFinalMessageId/);
  assert.match(source, /presentationSegmentKey\(presentationKey, index\)/);
  assert.match(source, /thinkingStateKey\(presentationKey, index\)/);
}

console.log('test:chat-stream-handoff — T1-T20 all checks passed');
