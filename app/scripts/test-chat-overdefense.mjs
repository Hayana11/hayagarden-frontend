/**
 * Chat over-defense regressions:
 * - send failures keep the server's reason instead of a bare "发送失败";
 * - lock status is read without forcing anything;
 * - the lock poll no longer re-runs per streamed token or while hidden;
 * - refresh only aborts/force-unlocks on a confirmed second tap while a reply is live.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { sendChatMessage, takeLastChatSendError } from '../src/lib/api.ts';
import { decideChatRefresh, fetchChatLockStatus } from '../src/lib/chat.ts';

const originalFetch = globalThis.fetch;
const json = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

try {
  // T1 success returns the id and leaves no stale reason.
  globalThis.fetch = async () => json(200, { ok: true, message_id: 77 });
  assert.equal(await sendChatMessage('hi'), 77);
  assert.equal(takeLastChatSendError(), '');

  // T2 server JSON error is surfaced verbatim, then cleared on read.
  globalThis.fetch = async () => json(400, { error: '一次消息最多上传 4 个附件' });
  assert.equal(await sendChatMessage('hi'), null);
  assert.equal(takeLastChatSendError(), '一次消息最多上传 4 个附件');
  assert.equal(takeLastChatSendError(), '');

  // T3 non-JSON (nginx 413 / access gate 401) falls back to the HTTP status.
  globalThis.fetch = async () => new Response('<html>413</html>', { status: 413 });
  assert.equal(await sendChatMessage('hi'), null);
  assert.equal(takeLastChatSendError(), 'HTTP 413');

  // T4 transport failure.
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  assert.equal(await sendChatMessage('hi'), null);
  assert.equal(takeLastChatSendError(), '网络连接失败');

  // T5 lock status parse; unreadable → null (never treated as "busy").
  globalThis.fetch = async () => json(200, { busy: true, age_sec: 41.6, has_result: false });
  assert.deepEqual(await fetchChatLockStatus(), { busy: true, ageSec: 41.6 });
  globalThis.fetch = async () => json(200, { busy: false });
  assert.deepEqual(await fetchChatLockStatus(), { busy: false, ageSec: 0 });
  globalThis.fetch = async () => json(502, {});
  assert.equal(await fetchChatLockStatus(), null);
} finally {
  globalThis.fetch = originalFetch;
}

const screen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');

// T6 lock poll: deps only on history readiness; skips hidden page and live stream.
const pollStart = screen.indexOf('const pollLock = async () => {');
assert.ok(pollStart > 0, 'lock poll effect present');
const pollEffect = screen.slice(pollStart, screen.indexOf('}, [', pollStart) + 40);
assert.match(pollEffect, /if \(document\.hidden \|\| liveRef\.current\) return;/);
assert.match(pollEffect, /\}, \[initialHistoryReady\]\);/);
assert.doesNotMatch(pollEffect, /\}, \[initialHistoryReady, sending, live\]\);/);

// T7 refresh: the confirm gate precedes every destructive step.
const refreshStart = screen.indexOf('const refreshChat = useCallback(async () => {');
const refresh = screen.slice(refreshStart, screen.indexOf('}, [refreshing', refreshStart));
const decide = refresh.indexOf('decideChatRefresh({ armed, localStreaming, lock })');
const gate = refresh.indexOf("if (decision.kind === 'soft') {");
const earlyReturn = refresh.indexOf('return;', gate);
assert.ok(decide > 0 && gate > decide && earlyReturn > gate, 'soft decision returns early');
assert.ok(refresh.indexOf('abortRef.current?.abort()') > earlyReturn, 'abort only after the gate');
assert.ok(refresh.indexOf('forceUnlockChatGenLock()') > earlyReturn, 'force unlock only after the gate');
assert.match(refresh, /hardRefreshArmedUntilRef\.current = Date\.now\(\) \+ HARD_REFRESH_CONFIRM_MS;/);

// T8 unreadable lock (null) is never idle: first tap is soft; only the armed second tap is hard.
const unknown = decideChatRefresh({ armed: false, localStreaming: false, lock: null });
assert.deepEqual(unknown, {
  kind: 'soft',
  reason: 'unknown',
  toast: '无法确认服务器生成状态，再点一次强制刷新',
});
assert.deepEqual(decideChatRefresh({ armed: true, localStreaming: false, lock: null }), { kind: 'hard' });
// Existing behaviour unchanged.
assert.equal(decideChatRefresh({ armed: false, localStreaming: true, lock: null }).reason, 'streaming');
assert.equal(
  decideChatRefresh({ armed: false, localStreaming: false, lock: { busy: true, ageSec: 41.6 } }).toast,
  '服务器仍在生成（42s），再点一次强制解锁',
);
assert.deepEqual(
  decideChatRefresh({ armed: false, localStreaming: false, lock: { busy: false, ageSec: 0 } }),
  { kind: 'hard' },
);
// The screen feeds the fetched (possibly null) lock straight into the decision,
// so a null first tap can only reach the soft branch, never abort/forceUnlock.
assert.match(refresh, /const lock = armed \|\| localStreaming \? null : await fetchChatLockStatus\(\);/);
assert.doesNotMatch(refresh, /lock\?\.busy/);

console.log('chat over-defense focused tests passed: T1-T8');
