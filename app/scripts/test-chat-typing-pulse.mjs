/**
 * Chat composer typing pulse: throttled, best effort (no vitest).
 * Run: npm run test:chat-typing-pulse
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const { createTypingPulse, TYPING_PULSE_URL, TYPING_PULSE_INTERVAL_MS } = await import('../src/lib/typingPulse.ts');

let t = 0;
const posts = [];
const pulse = createTypingPulse({ now: () => t, post: async (url) => { posts.push([t, url]); } });

pulse();
assert.deepEqual(posts, [[0, TYPING_PULSE_URL]], 'first edit pulses at once');
t = 1000; pulse(); t = 3999; pulse();
assert.equal(posts.length, 1, 'edits inside the interval do not pulse');
t = TYPING_PULSE_INTERVAL_MS; pulse();
assert.equal(posts.length, 2, 'the next edit after the interval pulses again');
assert.ok(TYPING_PULSE_INTERVAL_MS >= 3000 && TYPING_PULSE_INTERVAL_MS <= 5000, 'throttle is 3-5 s');

// A rejected or throwing post never escapes into the composer.
const rejecting = createTypingPulse({ now: () => 0, post: () => Promise.reject(new Error('offline')) });
assert.doesNotThrow(() => rejecting());
const throwing = createTypingPulse({ now: () => 0, post: () => { throw new Error('no fetch'); } });
assert.doesNotThrow(() => throwing());
await new Promise((r) => setTimeout(r, 0));

// The Chat composer pulses from its onChange, only for a non-empty draft.
const screen = readFileSync(new URL('../src/screens/ChatScreen.tsx', import.meta.url), 'utf8');
assert.match(screen, /createTypingPulse\(\)/, 'ChatScreen creates one pulse');
const onChange = screen.slice(screen.indexOf('writeChatComposerDraft(value);'), screen.indexOf('setInput(value);', screen.indexOf('writeChatComposerDraft(value);')) + 40);
assert.match(onChange, /if \(value\.trim\(\)\) typingPulseRef\.current\(\);/, 'composer onChange pulses for a non-empty draft');

console.log('chat typing pulse: OK');
