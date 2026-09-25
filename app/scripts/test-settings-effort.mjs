import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const settings = fs.readFileSync(path.join(root, 'src/screens/SettingsScreen.tsx'), 'utf8');
const systemConfig = fs.readFileSync(path.join(root, 'src/lib/systemConfig.ts'), 'utf8');
const groupChat = fs.readFileSync(path.join(root, 'src/lib/groupChat.ts'), 'utf8');
const css = fs.readFileSync(path.join(root, 'src/index.css'), 'utf8');

assert.match(systemConfig, /export async function getCcEffort/);
assert.match(systemConfig, /export async function setCcEffort/);
assert.match(systemConfig, /'\/api\/config\/cc-effort'/);
assert.match(groupChat, /export const setCodexEffort/);
assert.match(groupChat, /'\/api\/group-chat\/codex-effort'/);
assert.match(groupChat, /configuredEffort:/);
assert.match(groupChat, /allowedEfforts:/);

assert.match(settings, /function EffortPills/);
assert.match(settings, /getCcEffort\(\)/);
assert.match(settings, /setCcEffort\(/);
assert.match(settings, /setCodexEffort\(/);
assert.match(settings, /switchCcLineEffort/);
assert.match(settings, /switchCodexLineEffort/);
const effortBlocks = settings.match(/<div className="config-effort">[\s\S]*?<\/div>\s*<\/div>/g) || [];
assert.ok(effortBlocks.length >= 1, 'expected at least one config-effort block');
for (const block of effortBlocks) {
  assert.doesNotMatch(block, /后端尚未接入/);
  assert.doesNotMatch(block, /disabled>LOW/);
}
assert.equal(settings.includes('disabled>LOW</button><button type="button" disabled>MED</button>'), false);

assert.match(css, /\.config-effort\s*\{[^}]*flex-wrap:\s*wrap/);
assert.doesNotMatch(css, /\.config-effort[^{]*\{[^}]*\bgap:/);

for (const forbidden of ['Array.prototype.at', 'Object.hasOwn', 'replaceAll', 'structuredClone']) {
  assert.equal(
    settings.includes(forbidden) || systemConfig.includes(forbidden) || groupChat.includes(forbidden),
    false,
    `Chrome 78 builtin: ${forbidden}`,
  );
}

console.log('settings effort frontend contract: PASS');
