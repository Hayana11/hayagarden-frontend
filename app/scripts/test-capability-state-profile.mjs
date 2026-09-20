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

assert.doesNotMatch(profile, /fetchCapabilityStates/);
assert.doesNotMatch(profile, /capabilityStateById/);
assert.doesNotMatch(profile, /profile-tool-card-toggle/);
assert.doesNotMatch(profile, /setOpenTools/);
assert.doesNotMatch(profile, /patchCapabilityState|\/api\/capabilities\/[^']+\/state|http\.patch/);
assert.doesNotMatch(profile, /patchToolCompanionHint/);
assert.match(profile, /fetchToolCompanionHints/);
assert.match(profile, /Promise\.allSettled/);
assert.match(profile, /prompt_preview/);
assert.match(profile, /当前注入预览/);
assert.match(profile, /const dirty = personaDirty \|\| displayThinkingDirty;/);

assert.match(hints, /\/api\/tools\/companion-hints/);
assert.match(hints, /patchToolCompanionHint/);
assert.doesNotMatch(css, /profile-capability-state-badge/);
assert.doesNotMatch(css, /profile-tool-card-toggle/);

console.log('test-capability-state-profile: ok');
