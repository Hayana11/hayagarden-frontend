/**
 * P2H-3B: CallScreen is UI only. The call itself (capture, playback, routing, the reply
 * stream) lives behind CallRuntime, so a native engine can replace it without the page
 * knowing. The web runtime stays the default and is never assumed away.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (file) => readFileSync(new URL(file, import.meta.url), 'utf8');
const screen = read('../src/screens/CallScreen.tsx');
const web = read('../src/lib/callRuntime/useWebCallRuntime.ts');
const selector = read('../src/lib/callRuntime/index.ts');
const types = read('../src/lib/callRuntime/types.ts');

const imports = screen.split('\n').filter((line) => /^import |^\} from /.test(line)).join('\n');
for (const module of ['nativeVoice', 'nativeSpeech', 'nativeCallAudio', 'http']) {
  assert.ok(!imports.includes(`/lib/${module}'`), `CallScreen does not import lib/${module}`);
}
const code = screen.replace(/^\s*\/\/.*$/gm, '');
for (const banned of [/new EventSource\(/, /new Audio\(/, /voiceCall\.(ring|answer|decline|hangup|hangupBeacon|say|hear|interrupt|discard|streamUrl|audioUrl|current|load)\b/,
  /sendBeacon/, /\.subscribe\(/, /addEventListener\(/]) {
  assert.ok(!banned.test(code), `CallScreen does not run the call itself (${banned})`);
}
assert.match(screen, /useCallRuntime\(\)/, 'CallScreen consumes the runtime');
assert.match(screen, /runtime\.say\(text\)/, 'typed text goes through the runtime');

// The web runtime owns the whole loop.
for (const piece of ['nativeVoice.subscribe(', 'nativeSpeech.subscribe(', 'nativeCallAudio.subscribe(',
  'new EventSource(', 'voiceCall.hangup(', 'pagehide']) {
  assert.ok(web.includes(piece), `the web runtime owns ${piece}`);
}
assert.match(web, /kind: 'web'/);

// Only the web runtime exists until the native engine lands; the page never assumes native.
assert.match(types, /kind: CallRuntimeKind/);
assert.match(selector, /return 'web';/);
assert.match(selector, /return useWebCallRuntime\(\);/);
assert.doesNotMatch(screen, /'native'/, 'CallScreen has no native branch');

console.log('call runtime: OK');
