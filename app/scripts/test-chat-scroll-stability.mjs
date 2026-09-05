import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
  clampChatScrollTop,
  resizeChatTextarea,
  writeChatScroll,
} from '../src/lib/chatScrollCoordinator.ts';

const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
const coordinator = fs.readFileSync(new URL('../src/lib/chatScrollCoordinator.ts', import.meta.url), 'utf8');

function container({ scrollHeight = 1000, clientHeight = 500, scrollTop = 0 } = {}) {
  const writes = [];
  const value = {
    scrollHeight,
    clientHeight,
    scrollTop,
    writes,
    scrollTo({ top, behavior }) {
      value.scrollTop = top;
      writes.push({ top, behavior });
    },
  };
  return value;
}

function textarea(scrollHeight) {
  return { scrollHeight, style: { height: '96px' } };
}

function assertLegalScroll(c) {
  assert.ok(c.scrollTop >= 0);
  assert.ok(c.scrollTop <= Math.max(0, c.scrollHeight - c.clientHeight));
}

const probeRecords = [];

// T1: growth while following latest keeps the transcript at the current bottom.
{
  const c = container({ scrollTop: 500 });
  resizeChatTextarea(textarea(200), c, true, (record) => probeRecords.push(record));
  assert.equal(c.scrollTop, 500);
  assertLegalScroll(c);
}

// T2: shrink while following latest does not leave a bottom gap.
{
  const c = container({ scrollTop: 500 });
  resizeChatTextarea(textarea(20), c, true);
  assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
  assertLegalScroll(c);
}

// T3: resizing while browsing history restores the visual anchor, not the bottom.
{
  const c = container({ scrollTop: 220 });
  resizeChatTextarea(textarea(200), c, false);
  assert.equal(c.scrollTop, 220);
  assert.notEqual(c.scrollTop, c.scrollHeight - c.clientHeight);
  assertLegalScroll(c);
}

// T4: stream-follow and textarea-resize share the same bounded writer.
{
  const c = container({ scrollHeight: 1000, clientHeight: 500, scrollTop: 500 });
  writeChatScroll(c, {
    source: 'stream-follow',
    intent: 'follow-latest',
    followLatest: true,
  });
  c.scrollHeight = 1100;
  resizeChatTextarea(textarea(120), c, true);
  writeChatScroll(c, {
    source: 'stream-follow',
    intent: 'follow-latest',
    followLatest: true,
  });
  assert.equal(c.scrollTop, 600);
  assertLegalScroll(c);
}

// T5: explicit search/jump remains an explicit target and is not consumed as follow-latest.
{
  const c = container({ scrollTop: 600 });
  writeChatScroll(c, {
    source: 'search-jump',
    intent: 'explicit-target',
    followLatest: false,
    targetScrollTop: 280,
    behavior: 'smooth',
  });
  assert.equal(c.scrollTop, 280);
  assert.deepEqual(c.writes.at(-1), { top: 280, behavior: 'smooth' });
  assertLegalScroll(c);
}

// T6: warm restore preserves history position, or follows the latest edge when pinned.
{
  const historical = container({ scrollTop: 180 });
  writeChatScroll(historical, {
    source: 'warm-restore',
    intent: 'preserve-position',
    followLatest: false,
    targetScrollTop: 180,
  });
  assert.equal(historical.scrollTop, 180);

  const latest = container({ scrollTop: 180 });
  writeChatScroll(latest, {
    source: 'warm-restore',
    intent: 'follow-latest',
    followLatest: true,
  });
  assert.equal(latest.scrollTop, latest.scrollHeight - latest.clientHeight);
}

// T7: every coordinator write is clamped after geometry changes.
{
  const c = container({ scrollHeight: 900, clientHeight: 400, scrollTop: 200 });
  assert.equal(clampChatScrollTop(c, -50), 0);
  assert.equal(clampChatScrollTop(c, 9999), 500);
  c.scrollHeight = 250;
  c.clientHeight = 400;
  writeChatScroll(c, {
    source: 'textarea-resize',
    intent: 'preserve-position',
    followLatest: false,
    targetScrollTop: 9999,
  });
  assert.equal(c.scrollTop, 0);
  assertLegalScroll(c);
}

// T8: follow-latest intent is authoritative even when metadata says not following.
{
  const c = container({ scrollHeight: 1000, clientHeight: 500, scrollTop: 120 });
  writeChatScroll(c, {
    source: 'explicit-scroll-bottom',
    intent: 'follow-latest',
    followLatest: false,
  });
  assert.equal(c.scrollTop, c.scrollHeight - c.clientHeight);
  assertLegalScroll(c);
}

assert.equal((screen.match(/resizeChatTextarea\(/g) || []).length, 2);
assert.match(screen, /from ['"]\.\.\/lib\/chatScrollCoordinator['"]/);
assert.match(screen, /source: 'search-jump'/);
assert.match(screen, /source: 'history-window'/);
assert.match(screen, /source: 'warm-restore'/);
assert.match(screen, /source: 'background-poll'/);
assert.match(screen, /source: 'stream-follow'/);
assert.match(screen, /source: 'initial-history'/);
assert.doesNotMatch(screen, /\bscrollTo\s*\(/);
assert.doesNotMatch(screen, /\bscrollTop\s*=/);
assert.match(screen, /<div ref=\{scrollRef\} onScroll=\{handleTranscriptScroll\}/);
assert.match(screen, /renderUserMsg/);
const userRenderer = screen.slice(
  screen.indexOf('function renderUserMsg'),
  screen.indexOf('function renderAssistantMsg'),
);
assert.match(userRenderer, /chat-message-content chat-message-content-user/);
assert.match(userRenderer, /ChatMediaGroup/);
assert.match(userRenderer, /chat-message-bubble chat-message-bubble-user/);
assert.match(userRenderer, /ChatMediaGroup[\s\S]*chat-message-bubble/);
assert.match(screen, /live-segment-/);
assert.match(screen, /display-text-/);
assert.match(coordinator, /timestamp/);
assert.match(coordinator, /beforeScrollTop/);
assert.match(coordinator, /afterScrollTop/);
assert.match(coordinator, /clientHeight/);
assert.match(coordinator, /scrollHeight/);
assert.match(coordinator, /followLatest/);
assert.match(coordinator, /textareaHeight/);
assert.equal(probeRecords.length, 1);
assert.equal(probeRecords[0].source, 'textarea-resize');
assert.equal(probeRecords[0].followLatest, true);
assert.equal(probeRecords[0].textareaHeight, 120);

console.log('test:chat-scroll-stability — T1-T8 all checks passed');