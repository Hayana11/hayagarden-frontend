import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { fileURLToPath } from 'node:url';
import { RealityPromptPreview } from '../src/components/RealityPromptPreviewCard.tsx';

function render(prompt) {
  return renderToStaticMarkup(
    React.createElement(RealityPromptPreview, { prompt }),
  );
}

function stripMarkup(markup) {
  return markup.replace(/<[^>]*>/g, '');
}

const canonicalPrompt = {
  schemaVersion: 1,
  text: '设备当前静止，正在充电，电量80%。',
  segments: [
    { kind: 'literal', text: '设备当前' },
    { kind: 'dynamic', key: 'motion', text: '静止' },
    { kind: 'literal', text: '，' },
    { kind: 'dynamic', key: 'charging', text: '正在充电' },
    { kind: 'literal', text: '，电量' },
    { kind: 'dynamic', key: 'batteryLevel', text: '80%' },
    { kind: 'literal', text: '。' },
  ],
};

const canonicalMarkup = render(canonicalPrompt);
const canonicalTextMarkup = canonicalMarkup.match(
  /<p class="reality-prompt-text"[^>]*>(.*?)<\/p>/,
)?.[1] ?? '';
assert.equal(stripMarkup(canonicalTextMarkup), canonicalPrompt.text);
assert.ok(canonicalMarkup.includes('<strong>静止</strong>'));
assert.ok(canonicalMarkup.includes('<strong>正在充电</strong>'));
assert.ok(canonicalMarkup.includes('<strong>80%</strong>'));
assert.equal(canonicalMarkup.includes('<strong>设备当前'), false);
assert.equal(canonicalMarkup.includes('<strong>，'), false);
assert.equal(canonicalMarkup.includes('**'), false);

const dynamicValues = [...canonicalMarkup.matchAll(/<strong>(.*?)<\/strong>/g)]
  .map((match) => match[1]);
assert.deepEqual(dynamicValues, ['静止', '正在充电', '80%']);

const emptyMarkup = render({
  schemaVersion: 1,
  text: '',
  segments: [],
});
assert.ok(emptyMarkup.includes('现实 Prompt 预览'));
assert.ok(emptyMarkup.includes('尚未接入 Chat'));
assert.ok(emptyMarkup.includes('暂无可用的设备现实状态'));
assert.equal(emptyMarkup.includes('设备当前未知。'), false);
assert.equal(stripMarkup(emptyMarkup).includes('设备当前未知。'), false);

const componentPath = fileURLToPath(
  new URL('../src/components/RealityPromptPreviewCard.tsx', import.meta.url),
);
const componentSource = readFileSync(componentPath, 'utf8');
assert.ok(componentSource.includes('realityPromptProjection'));
assert.ok(componentSource.includes('useSyncExternalStore'));
for (const forbidden of [
  'ElpisPhysical',
  'setInterval',
  'setTimeout',
  'new RealityStore',
  'RealityStore',
  'compileRealityContext',
  'accelerometer',
  'lux',
]) {
  assert.equal(
    componentSource.includes(forbidden),
    false,
    `preview component must not contain ${forbidden}`,
  );
}

const settingsPath = fileURLToPath(
  new URL('../src/screens/SettingsScreen.tsx', import.meta.url),
);
const settingsSource = readFileSync(settingsPath, 'utf8');
assert.equal(settingsSource.includes('RealityPromptPreviewCard'), false);
assert.equal(settingsSource.includes('<RealityPromptPreviewCard />'), false);
assert.equal(settingsSource.includes('现实 Prompt 预览'), false);

console.log('P2C.1g reality prompt preview tests: PASS');
