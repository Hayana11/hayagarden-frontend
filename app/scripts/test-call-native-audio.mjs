/**
 * P2H-1 native call audio events: only well-formed shell events reach CallScreen
 * (no vitest). Run: npm run test:call-native-audio
 */
import assert from 'node:assert/strict';

const { parseCallAudioEvent, nativeCallAudio } = await import('../src/lib/nativeCallAudio.ts');

assert.deepEqual(parseCallAudioEvent('{"type":"started","seq":3}'), { type: 'started', seq: 3 });
assert.deepEqual(parseCallAudioEvent('{"type":"ended","seq":3}'), { type: 'ended', seq: 3 });
assert.deepEqual(parseCallAudioEvent('{"type":"error","seq":3,"code":"ERROR_CODE_IO_BAD_HTTP_STATUS"}'),
  { type: 'error', seq: 3, code: 'ERROR_CODE_IO_BAD_HTTP_STATUS' });
assert.deepEqual(parseCallAudioEvent('{"type":"error","seq":3,"code":7}'), { type: 'error', seq: 3, code: undefined });
for (const route of ['earpiece', 'speaker', 'other']) {
  assert.deepEqual(parseCallAudioEvent(JSON.stringify({ type: 'route', route })), { type: 'route', route });
}
for (const bad of ['', 'nope', 'null', '[]', '{"type":"route","route":"bluetooth"}', '{"type":"ended"}',
  '{"type":"ended","seq":0}', '{"type":"ended","seq":1.5}', '{"type":"ended","seq":"2"}', '{"type":"paused","seq":1}']) {
  assert.equal(parseCallAudioEvent(bad), null, bad);
}

// No shell bridge (browser, tests): unsupported, and every call is a safe no-op.
assert.equal(nativeCallAudio.supported(), false);
nativeCallAudio.stop();
nativeCallAudio.end();
console.log('call native audio tests passed');
