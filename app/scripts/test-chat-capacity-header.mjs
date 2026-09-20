import assert from 'node:assert/strict';
import fs from 'node:fs';
import {
  CAPACITY_SOFT_LIMIT,
  DEFAULT_CHAT_CONTEXT_LIMITS,
  normalizeChatContextLimits,
  fmtCapacityK,
  formatCapacityLabel,
  findLatestRoundContext,
  normalizeCacheInfo,
} from '../src/lib/chat.ts';

assert.equal(fmtCapacityK(49515), '49.5k');
assert.equal(fmtCapacityK(150000), '150k');
assert.equal(fmtCapacityK(999), '999');
assert.equal(formatCapacityLabel(49515), '49.5k / 150k');
assert.equal(formatCapacityLabel(49515, 160000), '49.5k / 160k');
assert.equal(formatCapacityLabel(null), '— / 150k');
assert.equal(formatCapacityLabel(0), '— / 150k');
assert.equal(CAPACITY_SOFT_LIMIT, 150000);
assert.deepEqual(normalizeChatContextLimits({ context_soft_limit: 160000, max_resident_turns: 46 }), {
  softLimit: 160000,
  maxResidentTurns: 46,
  authoritative: true,
});
assert.equal(DEFAULT_CHAT_CONTEXT_LIMITS.softLimit, 150000);

const parsed = normalizeCacheInfo({ last_round_context: 49515, lastRoundContext: 0 });
assert.equal(parsed?.lastRoundContext, 49515);
const legacy = normalizeCacheInfo({ input_tokens: 12 });
assert.equal(legacy?.lastRoundContext, undefined);

const msgs = [
  { id: 1, role: 'user', cacheInfo: null },
  {
    id: 2,
    role: 'assistant',
    cacheInfo: { stream_interrupted: true },
  },
  {
    id: 3,
    role: 'assistant',
    cacheInfo: { lastRoundContext: 49515 },
  },
];
assert.equal(findLatestRoundContext(msgs), 49515);

const partialOnly = [
  { id: 1, role: 'assistant', cacheInfo: { partial_rescue: true } },
];
assert.equal(findLatestRoundContext(partialOnly), null);

const screen = fs.readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
assert.match(screen, /fetchChatContextLimits/);
assert.match(screen, /formatCapacityLabel\(findLatestRoundContext\(msgs\), contextLimits\.softLimit\)/);
assert.doesNotMatch(screen, /always here/);
assert.doesNotMatch(screen, /away for now/);
assert.match(screen, /chatBreathe 2\.2s ease-in-out infinite/);
assert.match(screen, /chatBreathe 3s ease-in-out infinite/);
assert.match(screen, /endpointOnline === false[\s\S]*?undefined/);
assert.match(screen, /capacityLabel/);
assert.doesNotMatch(
  screen.slice(screen.indexOf('capacityLabel'), screen.indexOf('capacityLabel') + 400),
  /compactToolbar/,
);
assert.match(screen, /contextLimits\.maxResidentTurns/);

console.log('test-chat-capacity-header: ok');
