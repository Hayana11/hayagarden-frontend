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
assert.match(settings, /updateDeepSeekKey/);
assert.match(settings, /saveDeepSeekKey/);
assert.match(settings, /ccOfficialModelShort/);
assert.doesNotMatch(settings, /catalogSource === 'fallback' \? catalog.length \+ ' 个安全 fallback'/);
const officialExpanded = settings.match(/officialExpanded && <div className="config-endpoint-expanded">[\s\S]*?<\/div>}/);
assert.ok(officialExpanded, 'expected official Claude expanded block');
assert.doesNotMatch(officialExpanded[0], /安全 fallback 清单/);
assert.doesNotMatch(settings, />默认<\/button>/);
const effortBlocks = settings.match(/<div className="config-effort">[\s\S]*?<\/div>\s*<\/div>/g) || [];
assert.ok(effortBlocks.length >= 1, 'expected at least one config-effort block');
for (const block of effortBlocks) {
  assert.doesNotMatch(block, /后端尚未接入/);
  assert.doesNotMatch(block, /disabled>LOW/);
  assert.doesNotMatch(block, />默认</);
}
assert.equal(settings.includes('disabled>LOW</button><button type="button" disabled>MED</button>'), false);

assert.match(css, /\.config-effort > div\s*\{[^}]*flex-wrap:\s*nowrap/);
assert.doesNotMatch(css, /\.config-effort[^{]*\{[^}]*\bgap:/);
assert.match(systemConfig, /export async function updateDeepSeekKey/);
assert.match(systemConfig, /'\/api\/config\/deepseek\/key'/);

for (const forbidden of ['Array.prototype.at', 'Object.hasOwn', 'replaceAll', 'structuredClone']) {
  assert.equal(
    settings.includes(forbidden) || systemConfig.includes(forbidden) || groupChat.includes(forbidden),
    false,
    `Chrome 78 builtin: ${forbidden}`,
  );
}

console.log('settings effort frontend contract: PASS');
