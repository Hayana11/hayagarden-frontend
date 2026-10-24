/**
 * Bilingual call lines: the gloss by default, the original on request, the
 * choice remembered (no vitest). Run: npm run test:call-subtitles
 */
import assert from 'node:assert/strict';

const store = new Map();
globalThis.window = { localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) } };
const { displayLineText, hasGloss, readSubtitleMode, writeSubtitleMode, subtitleToggleLabel } = await import('../src/lib/voiceCall.ts');

const line = { seq: 3, speaker: 'him', text: '[softly] Wake up, little cat.', gloss: '醒醒，小猫。' };
assert.equal(displayLineText(line), '醒醒，小猫。', 'the gloss by default');
assert.equal(displayLineText(line, 'original'), 'Wake up, little cat.', 'the original, tags never shown');
assert.equal(displayLineText({ ...line, gloss: null }), 'Wake up, little cat.', 'no gloss: what he said');
assert.equal(displayLineText({ ...line, gloss: '  ' }), 'Wake up, little cat.', 'a blank gloss is no gloss');
assert.equal(displayLineText({ seq: 4, speaker: 'her', text: '[早] 好', gloss: '不该出现' }), '[早] 好', 'her words as she said them');
assert.equal(hasGloss(line), true);
assert.equal(hasGloss({ speaker: 'her', gloss: 'x' }), false, 'only his lines carry subtitles');

assert.equal(readSubtitleMode(), 'gloss', 'Chinese until she chooses');
writeSubtitleMode('original');
assert.equal(readSubtitleMode(), 'original', 'the last choice is remembered');
writeSubtitleMode('gloss');
assert.equal(readSubtitleMode(), 'gloss');
assert.equal(subtitleToggleLabel('gloss'), '显示原文');
assert.equal(subtitleToggleLabel('original'), '显示中文');

globalThis.window = { localStorage: { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } } };
assert.equal(readSubtitleMode(), 'gloss', 'blocked storage: the default, no crash');
writeSubtitleMode('original');
const { readFileSync } = await import('node:fs');
for (const file of ['../src/components/CallTranscriptSheet.tsx', '../src/screens/CallScreen.tsx']) {
  const src = readFileSync(new URL(file, import.meta.url), 'utf8');
  assert.match(src, /useSubtitleMode\(\)/, `${file} shares the remembered choice`);
  assert.match(src, /displayLineText\(line, subtitleMode\)/, `${file} shows lines in the chosen mode`);
  assert.match(src, /hasGloss\(line\) \?/, `${file} offers the toggle only on lines with a gloss`);
  assert.doesNotMatch(src, /\bgap\s*:/, 'Chrome 78: no flex gap');
}
console.log('call subtitles: OK');
