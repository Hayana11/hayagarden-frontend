import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

function read(relativePath) {
  return fs.readFileSync(path.join(root, 'src', relativePath), 'utf8');
}

const client = read('lib/capabilityStates.ts');
const profile = read('screens/ProfileScreen.tsx');
const hints = read('lib/toolCompanionHints.ts');
const css = fs.readFileSync(path.join(root, 'src/screens/ProfileScreen.css'), 'utf8');

assert.match(client, /export function fetchCapabilityStates/);
assert.match(client, /\/api\/capabilities\/states/);
for (const field of [
  'capability_id',
  'static_enabled',
  'runtime_state',
  'effective_enabled',
  'writable',
]) {
  assert.match(client, new RegExp(field));
}
for (const state of ['INHERIT', 'ON', 'OFF', 'DENY']) {
  assert.match(client, new RegExp(state));
}

assert.match(profile, /fetchCapabilityStates/);
assert.match(profile, /Promise\.allSettled/);
assert.match(profile, /capabilityStateById\.get\(tool\.capability_id\)/);
for (const label of ['默认开启', '已开启', '已关闭', '不可用']) {
  assert.match(profile, new RegExp(label));
}
assert.match(profile, /状态未知/);
assert.match(profile, /当前不可确认/);
assert.match(profile, /const dirty = personaDirty \|\| displayThinkingDirty \|\| toolDirty \|\| hasResetIntent;/);
assert.match(profile, /profile-tool-card-toggle/);
assert.match(profile, /setOpenTools/);
assert.doesNotMatch(profile, /patchCapabilityState|\/api\/capabilities\/[^']+\/state|http\.patch/);

const persistStart = profile.indexOf('const persist =');
const editStart = profile.indexOf('const editTool =');
assert.ok(persistStart >= 0 && editStart > persistStart);
assert.doesNotMatch(profile.slice(persistStart, editStart), /capabilityState/);

assert.match(hints, /\/api\/tools\/companion-hints/);
assert.match(hints, /patchToolCompanionHint/);
assert.match(css, /profile-capability-state-badge/);
assert.doesNotMatch(css, /profile-capability-state-badge[\s\S]*profile-switch/);

console.log('test-capability-state-profile: ok');
